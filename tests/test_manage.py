"""覆盖运维边界与发布回滚，不启动或终止真实服务。"""

from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import tempfile
import json
import os
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import manage
from runtime_config import load_environment


class ProcessIdentityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/tmp/quant-test-project")
        self.specs = manage.services(self.root)

    def test_accepts_relative_project_python_entry(self):
        self.assertTrue(manage.matches_process(
            self.specs["auth"], "python3 -u auth_service.py --port 8890", self.root))

    def test_rejects_same_script_in_another_project(self):
        self.assertFalse(manage.matches_process(
            self.specs["auth"], "python3 auth_service.py --port 8890", Path("/tmp/other")))

    def test_rejects_script_mentioned_as_argument(self):
        self.assertFalse(manage.matches_process(
            self.specs["auth"], "python3 test.py auth_service.py", self.root))
        self.assertFalse(manage.matches_process(
            self.specs["auth"], "zsh -c 'python3 auth_service.py'", self.root))

    def test_accepts_both_freqtrade_entry_styles(self):
        bot = self.root / "bot"
        spec = self.specs["api"]
        self.assertTrue(manage.matches_process(spec,
            "python -m freqtrade trade --config user_data/config_trend_live.json", bot))
        self.assertTrue(manage.matches_process(spec,
            "python /tmp/quant-test-project/bot/.venv/bin/freqtrade trade "
            "--config user_data/config_trend_live.json", bot))

    def test_rejects_other_freqtrade_mode_or_config(self):
        bot = self.root / "bot"
        spec = self.specs["api"]
        self.assertFalse(manage.matches_process(spec,
            "python -m freqtrade webserver --config user_data/config_trend_live.json", bot))
        self.assertFalse(manage.matches_process(spec,
            "python -m freqtrade trade --config user_data/other.json", bot))

    def test_model_processes_are_never_managed(self):
        for command in ("python auto_research_daemon.py", "python auto_iterate.py --daemon",
                        "python refresh_research.py --daemon", "python monitor.py --daemon"):
            for spec in self.specs.values():
                self.assertFalse(manage.matches_process(spec, command, self.root))
                self.assertFalse(manage.matches_process(spec, command, self.root / "bot"))
        self.assertEqual(set(self.specs), {"auth", "web", "api", "webserver", "sync", "market"})

    def test_start_reuses_existing_service(self):
        with patch("manage.find_processes", return_value=[42]), \
             patch("manage.health", return_value="HTTP 200"), \
             patch("manage.subprocess.Popen") as spawn:
            self.assertTrue(manage.start_service(self.specs["auth"], self.root))
            spawn.assert_not_called()

    def test_start_refuses_port_owned_by_other_service(self):
        with patch("manage.find_processes", return_value=[]), \
             patch("manage.port_busy", return_value=True), \
             patch("manage.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "其他进程"):
                manage.start_service(self.specs["auth"], self.root)
            spawn.assert_not_called()

    def test_stop_rechecks_identity_before_signal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = manage.services(root)["auth"]
            with patch("manage.find_processes", return_value=[42]), \
                 patch("manage.process_commands", return_value={42: "python auth_service.py"}), \
                 patch("manage.process_cwd", return_value=Path("/tmp/other-project")), \
                 patch("manage.os.kill") as kill:
                self.assertTrue(manage.stop_service(spec, root))
                kill.assert_not_called()


class FrontendDeploymentTests(unittest.TestCase):
    def prepare(self, root):
        dist = root / "vben/apps/web-antd/dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("new page", encoding="utf-8")
        (dist / "_app-config-test.js").write_text(
            'window.config={"VITE_GLOB_API_URL":"/api","other":"kept"}', encoding="utf-8")
        live = root / "web"
        live.mkdir()
        (live / "index.html").write_text("old page", encoding="utf-8")
        return dist, live

    def test_deployment_preserves_old_version_and_updates_runtime_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _dist, live = self.prepare(root)
            backup = manage.deploy_frontend(root, "http://127.0.0.1:8890/api")
            self.assertEqual((live / "index.html").read_text(), "new page")
            self.assertIsNotNone(backup)
            self.assertEqual((backup / "index.html").read_text(), "old page")
            self.assertIn('"VITE_GLOB_API_URL":"http://127.0.0.1:8890/api"',
                          (live / "_app-config-test.js").read_text())
            self.assertIn('"other":"kept"', (live / "_app-config-test.js").read_text())

    def test_invalid_artifact_never_replaces_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist, live = self.prepare(root)
            (dist / "_app-config-test.js").unlink()
            with self.assertRaisesRegex(RuntimeError, "缺少运行时配置"):
                manage.deploy_frontend(root)
            self.assertEqual((live / "index.html").read_text(), "old page")
            self.assertEqual(list(root.glob(".web-stage-*")), [])

    def test_failed_directory_switch_restores_previous_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _dist, live = self.prepare(root)
            original_rename = Path.rename

            def fail_stage_rename(path, destination):
                if path.name.startswith(".web-stage-"):
                    raise OSError("模拟新产物发布失败")
                return original_rename(path, destination)

            with patch.object(Path, "rename", fail_stage_rename):
                with self.assertRaisesRegex(OSError, "发布失败"):
                    manage.deploy_frontend(root)
            self.assertEqual((live / "index.html").read_text(), "old page")
            self.assertEqual(list(root.glob(".web-stage-*")), [])


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_dotenv_does_not_override_existing_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text("QUANT_TEST_ONE=file\nQUANT_TEST_TWO=file\n")
            with patch.dict("os.environ", {"QUANT_TEST_ONE": "terminal"}, clear=True):
                self.assertEqual(load_environment(root), root.resolve())
                import os
                self.assertEqual(os.environ["QUANT_TEST_ONE"], "terminal")
                self.assertEqual(os.environ["QUANT_TEST_TWO"], "file")

    def test_command_log_redacts_secrets_without_changing_subprocess_arguments(self):
        command = ["python", "data_sync.py", "--dsn", "private-connection-string",
                   "--token=private-token"]
        output = StringIO()
        with patch("manage.subprocess.run") as run, redirect_stdout(output):
            manage.run_command(command)
        self.assertNotIn("private-connection-string", output.getvalue())
        self.assertNotIn("private-token", output.getvalue())
        run.assert_called_once_with(command, cwd=manage.ROOT, check=True)


class PrivateConfigTests(unittest.TestCase):
    def prepare(self, root):
        directory = root / "bot/config_examples"
        directory.mkdir(parents=True)
        for name in ("config_trend_live", "config_trend_webserver", "config_dashboard"):
            (directory / f"{name}.example.json").write_text(
                json.dumps({"dry_run": False, "api_server": {"listen_port": 8889}}))

    def test_generated_configs_share_credentials_and_are_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            output = StringIO()
            with redirect_stdout(output):
                manage.initialize_configs(root)
            files = sorted((root / "bot/user_data").glob("*.json"))
            self.assertEqual(len(files), 3)
            configs = [json.loads(file.read_text()) for file in files]
            for file, config in zip(files, configs):
                self.assertTrue(config["dry_run"])
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(config["api_server"]["password"],
                                 configs[0]["api_server"]["password"])
                self.assertNotIn(config["api_server"]["password"], output.getvalue())

    def test_existing_config_prevents_any_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            directory = root / "bot/user_data"
            directory.mkdir()
            existing = directory / "config_trend_live.json"
            existing.write_text("existing")
            with self.assertRaisesRegex(RuntimeError, "拒绝覆盖"):
                manage.initialize_configs(root)
            self.assertEqual(existing.read_text(), "existing")
            self.assertEqual(len(list(directory.iterdir())), 1)


class PostgresOperationsTests(unittest.TestCase):
    def initialized(self, root):
        cluster = root / "database/runtime/postgres"
        cluster.mkdir(parents=True)
        (cluster / "PG_VERSION").write_text("17")
        return cluster

    def test_initialize_refuses_existing_environment_or_cluster(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = root / ".env"
            environment.write_text("existing")
            with patch("manage.run_command") as run:
                with self.assertRaisesRegex(RuntimeError, "拒绝覆盖"):
                    manage.postgres_initialize(root)
                run.assert_not_called()
            environment.unlink()
            cluster = self.initialized(root)
            with patch("manage.run_command") as run:
                with self.assertRaisesRegex(RuntimeError, "拒绝初始化"):
                    manage.postgres_initialize(root)
                run.assert_not_called()
            self.assertEqual((cluster / "PG_VERSION").read_text(), "17")

    def test_symlink_cluster_is_never_managed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            external = root / "unrelated"
            external.mkdir()
            runtime = root / "database/runtime"
            runtime.mkdir(parents=True)
            (runtime / "postgres").symlink_to(external, target_is_directory=True)
            with patch("manage.run_command") as run:
                with self.assertRaisesRegex(RuntimeError, "符号链接"):
                    manage.postgres_start(root)
                run.assert_not_called()

    def test_start_and_stop_target_project_cluster_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cluster = self.initialized(root)
            with patch("manage.postgres_running", return_value=False), \
                 patch("manage.port_busy", return_value=False), \
                 patch("manage.postgres_binary", return_value="/pg/bin/pg_ctl"), \
                 patch("manage.run_command") as run:
                manage.postgres_start(root)
                self.assertEqual(run.call_args.args[0][:3], ["/pg/bin/pg_ctl", "-D", str(cluster)])
                self.assertEqual(run.call_args.args[0][-1], "start")
            with patch("manage.postgres_running", return_value=True), \
                 patch("manage.postgres_binary", return_value="/pg/bin/pg_ctl"), \
                 patch("manage.run_command") as run:
                manage.postgres_stop(root)
                self.assertEqual(run.call_args.args[0],
                    ["/pg/bin/pg_ctl", "-D", str(cluster), "-m", "fast", "-w", "-t", "30", "stop"])

    def test_start_rejects_unrelated_port_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.initialized(root)
            with patch("manage.postgres_running", return_value=False), \
                 patch("manage.port_busy", return_value=True), patch("manage.run_command") as run:
                with self.assertRaisesRegex(RuntimeError, "其他服务"):
                    manage.postgres_start(root)
                run.assert_not_called()

    def test_automatic_start_requires_local_dsn_and_owned_cluster(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.initialized(root)
            for dsn in ("postgresql://example.org:5433/db", "postgresql://localhost:5432/db",
                        "postgresql://localhost.evil:5433/db", "not a connection string",
                        "host=localhost hostaddr=203.0.113.1 port=5433 dbname=db"):
                with self.subTest(dsn=dsn), patch.dict(os.environ, {"QUANT_DATABASE_URL": dsn}), \
                     patch("manage.postgres_start") as start:
                    manage.ensure_local_postgres(root)
                    start.assert_not_called()
            with patch.dict(os.environ, {"QUANT_DATABASE_URL": "postgresql://127.0.0.1:5433/db"}), \
                 patch("manage.postgres_start") as start:
                manage.ensure_local_postgres(root)
                start.assert_called_once_with(root)

    def test_initialize_creates_private_credentials_and_separate_databases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cluster = root / "database/runtime/postgres"
            connection = MagicMock()

            def initialize_files(_command, _cwd):
                (cluster / "PG_VERSION").write_text("17")
                (cluster / "postgresql.conf").write_text("# 默认配置\n")

            output = StringIO()
            with patch("manage.port_busy", return_value=False), \
                 patch("manage.postgres_binary", side_effect=lambda name: f"/pg/bin/{name}"), \
                 patch("manage.run_command", side_effect=initialize_files), \
                 patch("manage.postgres_start"), \
                 patch("manage.secrets.token_urlsafe", return_value="PRIVATE_DB_PASSWORD"), \
                 patch("psycopg.connect", return_value=connection), \
                 patch("manage.load_environment"), redirect_stdout(output):
                manage.postgres_initialize(root)
            self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)
            self.assertEqual((cluster / "socket").stat().st_mode & 0o777, 0o700)
            config = (root / ".env").read_text()
            self.assertIn("/quant_terminal", config)
            self.assertIn("/quant_test", config)
            self.assertNotIn("PRIVATE_DB_PASSWORD", output.getvalue())
            self.assertEqual(connection.__enter__.return_value.execute.call_count, 3)

    def test_failed_initialization_cleans_only_new_cluster(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sentinel = root / "unrelated.txt"
            sentinel.write_text("preserved")
            with patch("manage.port_busy", return_value=False), \
                 patch("manage.postgres_binary", return_value="/pg/bin/initdb"), \
                 patch("manage.run_command", side_effect=subprocess.CalledProcessError(1, "initdb")):
                with self.assertRaisesRegex(RuntimeError, "初始化失败"):
                    manage.postgres_initialize(root)
            self.assertFalse((root / "database/runtime/postgres").exists())
            self.assertFalse((root / ".env").exists())
            self.assertEqual(sentinel.read_text(), "preserved")


if __name__ == "__main__":
    unittest.main()
