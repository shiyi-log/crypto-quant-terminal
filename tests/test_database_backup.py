"""数据库备份必须可验证，恢复只允许新建隔离库；不连接真实数据库。"""

import importlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


DSN = "postgresql://quant_app:private-password@127.0.0.1:5433/quant_terminal"
RESTORE_DSN = "postgresql://quant_app:other-password@127.0.0.1:5433/quant_restore_drill"
DUMP = b"PGDMP\x01synthetic-database-archive"


def module():
    # 缺少备份入口时以明确断言失败，避免把收集异常误当成故障复现。
    assert importlib.util.find_spec("tools.database_backup") is not None, "database backup capability missing"
    return importlib.import_module("tools.database_backup")


class PgCommands:
    """仅替代外部 PostgreSQL 命令；文件、清单和校验均执行真实代码。"""

    def __init__(self, *, fail: str | None = None):
        self.calls = []
        self.fail = fail

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        tool = next((Path(item).name for item in command if Path(item).name in
                     {"pg_dump", "pg_restore", "createdb"}), "")
        if tool == self.fail:
            raise subprocess.CalledProcessError(1, command, stderr=b"private-password server-error")
        if tool == "pg_dump":
            kwargs["stdout"].write(DUMP)
        return subprocess.CompletedProcess(command, 0, stdout=b"archive contents\n", stderr=b"")


def fake_commands(monkeypatch, *, fail=None):
    capability = module()
    commands = PgCommands(fail=fail)
    monkeypatch.setattr(capability.subprocess, "run", commands)
    monkeypatch.setattr(capability, "postgres_binary", lambda name: name)
    return capability, commands


def test_backup_publishes_hashed_manifest_only_after_archive_validation(tmp_path, monkeypatch):
    capability, commands = fake_commands(monkeypatch)
    result = capability.backup_database(DSN, tmp_path / "backup")
    manifest = json.loads((tmp_path / "backup/manifest.json").read_text())
    assert (tmp_path / "backup/database.dump").read_bytes() == DUMP
    assert manifest["schema"] == "quant-database-backup-v1"
    assert manifest["status"] == "verified"
    assert manifest["archive"]["sha256"] == "3dc932b00c6c140cf6ed027098002b273aa20c7df67ee34e0ce1f10579beb8ae"
    assert manifest["archive"]["size_bytes"] == len(DUMP)
    assert result == tmp_path / "backup/manifest.json"
    serialized = json.dumps(manifest)
    assert "private-password" not in serialized
    assert "quant_terminal" in serialized
    assert any("--list" in args for args, _ in commands.calls)


def test_failed_dump_keeps_no_success_manifest_and_redacts_subprocess_error(tmp_path, monkeypatch):
    capability, _ = fake_commands(monkeypatch, fail="pg_dump")
    with pytest.raises(capability.BackupError) as failure:
        capability.backup_database(DSN, tmp_path / "failed")
    assert "private-password" not in str(failure.value)
    assert not (tmp_path / "failed/manifest.json").exists()


def test_unreadable_dump_is_never_published_as_verified(tmp_path, monkeypatch):
    capability, _ = fake_commands(monkeypatch, fail="pg_restore")
    with pytest.raises(capability.BackupError):
        capability.backup_database(DSN, tmp_path / "invalid")
    assert not (tmp_path / "invalid/manifest.json").exists()


def test_existing_backup_is_not_overwritten(tmp_path, monkeypatch):
    capability, commands = fake_commands(monkeypatch)
    target = tmp_path / "backup"
    target.mkdir()
    (target / "database.dump").write_bytes(b"existing")
    with pytest.raises(capability.BackupError):
        capability.backup_database(DSN, target)
    assert (target / "database.dump").read_bytes() == b"existing"
    assert not commands.calls


def test_corrupt_archive_is_rejected_before_any_restore_command(tmp_path, monkeypatch):
    capability, commands = fake_commands(monkeypatch)
    capability.backup_database(DSN, tmp_path / "backup")
    (tmp_path / "backup/database.dump").write_bytes(b"modified")
    commands.calls.clear()
    with pytest.raises(capability.BackupError, match="integrity"):
        capability.restore_database(tmp_path / "backup", RESTORE_DSN)
    assert not commands.calls


@pytest.mark.parametrize("target", [
    DSN, "postgresql://u@127.0.0.1/quant_test",
    "postgresql://u@remote.example/quant_restore_drill",
    "postgresql://u@127.0.0.1/postgres",
])
def test_restore_rejects_production_shared_and_remote_targets(tmp_path, monkeypatch, target):
    capability, commands = fake_commands(monkeypatch)
    capability.backup_database(DSN, tmp_path / "backup")
    commands.calls.clear()
    with pytest.raises(capability.BackupError, match="isolated"):
        capability.restore_database(tmp_path / "backup", target)
    assert not commands.calls


def test_restore_creates_new_isolated_database_and_never_cleans_existing_data(tmp_path, monkeypatch):
    capability, commands = fake_commands(monkeypatch)
    capability.backup_database(DSN, tmp_path / "backup")
    commands.calls.clear()
    receipt = capability.restore_database(tmp_path / "backup", RESTORE_DSN)
    assert receipt["database"] == "quant_restore_drill"
    assert receipt["status"] == "restored"
    assert any("createdb" in args for args, _ in commands.calls)
    restored = [(args, kw) for args, kw in commands.calls if "--single-transaction" in args]
    assert len(restored) == 1
    assert restored[0][1]["stdin"].closed
    assert restored[0][1]["env"]["PGDATABASE"] == "quant_restore_drill"
    assert all("--clean" not in args and "--create" not in args for args, _ in commands.calls)
    assert all("other-password" not in str(args) for args, _ in commands.calls)


def test_existing_restore_database_failure_does_not_run_data_restore(tmp_path, monkeypatch):
    capability, commands = fake_commands(monkeypatch)
    capability.backup_database(DSN, tmp_path / "backup")
    commands.calls.clear()
    commands.fail = "createdb"
    with pytest.raises(capability.BackupError):
        capability.restore_database(tmp_path / "backup", RESTORE_DSN)
    assert not any("--single-transaction" in args for args, _ in commands.calls)


def test_docker_backup_passes_password_in_environment_not_command_or_manifest(tmp_path, monkeypatch):
    capability, commands = fake_commands(monkeypatch)
    capability.backup_database(DSN, tmp_path / "backup", container="quant-postgres")
    assert all(args[:3] == ["docker", "exec", "-i"] for args, _ in commands.calls)
    assert all("private-password" not in str(args) for args, _ in commands.calls)
    assert all(kw["env"]["PGPASSWORD"] == "private-password" for _, kw in commands.calls)
    assert all(kw["env"]["PGHOST"] == "127.0.0.1" for _, kw in commands.calls)
    assert all(kw["env"]["PGPORT"] == "5432" for _, kw in commands.calls)
    assert all("quant-postgres" in args for args, _ in commands.calls)
    manifest = (tmp_path / "backup/manifest.json").read_text()
    assert "private-password" not in manifest
