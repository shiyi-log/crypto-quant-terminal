#!/usr/bin/env python3
"""使用 uv 管理前后端服务；模型与策略研究进程由原负责人独立管理。"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import getpass
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import secrets
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import uuid

from runtime_config import load_environment

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Service:
    name: str
    label: str
    command: tuple[str, ...]
    cwd: Path
    script: Path | None = None
    mode: str | None = None
    port: int | None = None
    health_path: str = "/"


def services(root: Path = ROOT) -> dict[str, Service]:
    """只登记前后端与入库服务，禁止把模型守护加入生命周期操作。"""
    bot = root / "bot"
    legacy_python = bot / ".venv/bin/python"
    trading_python = str(legacy_python) if legacy_python.exists() else sys.executable
    python = sys.executable
    specs = [
        Service("api", "交易 API", (trading_python, "-m", "freqtrade", "trade", "--config",
                "user_data/config_trend_live.json", "--strategy", "TrendFollowing"), bot,
                mode="trade", port=8889, health_path="/api/v1/ping"),
        Service("webserver", "回测 API", (trading_python, "-m", "freqtrade", "webserver",
                "--config", "user_data/config_trend_webserver.json"), bot,
                mode="webserver", port=8891, health_path="/api/v1/ping"),
        Service("auth", "认证代理", (python, "-u", str(root / "auth_service.py"), "--port",
                "8890"), root, root / "auth_service.py", port=8890, health_path="/auth/health"),
        Service("web", "前端", (python, "-u", str(root / "serve_web.py"), "--port", "8888"),
                root, root / "serve_web.py", port=8888),
        Service("sync", "文件同步入库", (python, "-u", str(root / "data_sync.py"), "--daemon"),
                root, root / "data_sync.py"),
        Service("market", "逐笔与盘口采集", (python, "-u", str(root / "market_stream.py")),
                root, root / "market_stream.py"),
    ]
    return {spec.name: spec for spec in specs}


def process_cwd(pid: int) -> Path | None:
    """先使用 Linux procfs，macOS 则通过 lsof 读取进程实际工作目录。"""
    try:
        return Path(f"/proc/{pid}/cwd").resolve(strict=True)
    except OSError:
        pass
    try:
        result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                capture_output=True, text=True, check=False, timeout=5)
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                return Path(line[1:]).resolve()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def matches_process(spec: Service, command: str, cwd: Path | None) -> bool:
    """命令和目录同时核验，避免误杀其他项目或相同端口上的服务。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens or cwd is None or cwd.resolve() != spec.cwd.resolve():
        return False
    executable = Path(tokens[0]).name.lower()
    if not executable.startswith("python") and executable != "freqtrade":
        return False
    if spec.script:
        # 脚本必须是解释器直接执行的入口，不能只出现在 shell/测试参数中。
        remaining = tokens[1:]
        while remaining and remaining[0] in {"-u", "-B", "-s", "-E"}:
            remaining = remaining[1:]
        if not remaining or remaining[0].startswith("-"):
            return False
        script = Path(remaining[0])
        script = script if script.is_absolute() else cwd / script
        return script.resolve() == spec.script.resolve()
    # 兼容 python -m freqtrade 和旧 venv 的 freqtrade console script。
    entry = tokens[1:]
    if entry[:2] == ["-m", "freqtrade"]:
        entry = entry[2:]
    elif entry and Path(entry[0]).name == "freqtrade":
        entry = entry[1:]
    elif executable != "freqtrade":
        return False
    if not entry or entry[0] != spec.mode or "--config" not in entry:
        return False
    index = entry.index("--config")
    if index + 1 >= len(entry):
        return False
    config = Path(entry[index + 1])
    config = config if config.is_absolute() else cwd / config
    expected = spec.command[spec.command.index("--config") + 1]
    return config.resolve() == (spec.cwd / expected).resolve()


def process_commands() -> dict[int, str]:
    result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                            text=True, check=True, timeout=10)
    found = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            found[int(parts[0])] = parts[1]
    return found


def find_processes(spec: Service, commands: dict[int, str] | None = None) -> list[int]:
    found = []
    for pid, command in (commands if commands is not None else process_commands()).items():
        # 先筛选命令再调用 lsof，避免每次扫描全机所有进程。
        marker = spec.script.name if spec.script else "freqtrade"
        if pid != os.getpid() and marker in command:
            if matches_process(spec, command, process_cwd(pid)):
                found.append(pid)
    return sorted(found)


def port_busy(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def health(spec: Service) -> str:
    if spec.port is None:
        return "无 HTTP 端口"
    try:
        with urlopen(f"http://127.0.0.1:{spec.port}{spec.health_path}", timeout=2) as response:
            return f"HTTP {response.status}"
    except HTTPError as exc:
        return f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError):
        return "未就绪"


def pid_file(root: Path, spec: Service) -> Path:
    return root / "logs" / f"manage-{spec.name}.pid"


def start_service(spec: Service, root: Path = ROOT) -> bool:
    existing = find_processes(spec)
    if existing:
        print(f"{spec.label}：复用 PID {','.join(map(str, existing))}，{health(spec)}")
        return True
    if spec.name == "market" and os.getenv("QUANT_STREAM_ENABLED", "true").lower() in {
        "false", "0", "no", "off",
    }:
        print("逐笔与盘口采集：QUANT_STREAM_ENABLED 已关闭，跳过启动")
        return True
    if spec.port and port_busy(spec.port):
        raise RuntimeError(f"端口 {spec.port} 已被其他进程占用；请查看端口所属服务")
    if spec.script and not spec.script.is_file():
        raise RuntimeError(f"缺少入口文件：{spec.script}")
    if spec.name == "web" and not (root / "web/index.html").is_file():
        raise RuntimeError("缺少前端产物，请先执行 uv run python manage.py build")
    if spec.mode:
        config = spec.cwd / spec.command[spec.command.index("--config") + 1]
        if not config.is_file():
            raise RuntimeError(f"缺少本地交易配置：{config}，请按运维文档创建")
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / f"{spec.name}.log").open("ab") as output:
        child = subprocess.Popen(spec.command, cwd=spec.cwd, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    pid_file(root, spec).write_text(str(child.pid) + "\n", encoding="utf-8")
    time.sleep(0.4)
    if child.poll() is not None:
        pid_file(root, spec).unlink(missing_ok=True)
        raise RuntimeError(f"{spec.label} 启动失败，查看 logs/{spec.name}.log")
    print(f"{spec.label}：已启动 PID {child.pid}")
    return True


def stop_service(spec: Service, root: Path = ROOT, timeout: float = 10) -> bool:
    pids = find_processes(spec)
    for pid in pids:
        # 发信号前再次核验 PID，防止旧 PID 文件或扫描后的进程退出导致误操作。
        command = process_commands().get(pid, "")
        if not matches_process(spec, command, process_cwd(pid)):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pid not in find_processes(spec):
                break
            time.sleep(0.2)
        else:
            print(f"{spec.label}：PID {pid} 尚未退出，保留进程并报告失败")
            return False
        print(f"{spec.label}：已停止 PID {pid}")
    pid_file(root, spec).unlink(missing_ok=True)
    if not pids:
        print(f"{spec.label}：未运行")
    return True


def status(selected: list[Service]) -> int:
    commands = process_commands()
    for spec in selected:
        pids = find_processes(spec, commands)
        if pids:
            print(f"{spec.label}：运行中 PID {','.join(map(str, pids))}，{health(spec)}")
        elif spec.port and port_busy(spec.port):
            print(f"{spec.label}：端口 {spec.port} 有其他服务占用")
        else:
            print(f"{spec.label}：未运行")
    print("前端地址：http://127.0.0.1:8888")
    return 0


def run_command(command: list[str], cwd: Path = ROOT) -> None:
    # 子命令可能接收临时连接串，日志只显示经过脱敏的参数。
    visible = []
    redact_next = False
    sensitive = {"--dsn", "--password", "--token", "--secret"}
    for argument in command:
        if redact_next:
            visible.append("<已隐藏>")
            redact_next = False
        elif argument in sensitive:
            visible.append(argument)
            redact_next = True
        elif "=" in argument and argument.split("=", 1)[0] in sensitive:
            visible.append(argument.split("=", 1)[0] + "=<已隐藏>")
        else:
            visible.append(argument)
    print("执行：" + shlex.join(visible), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def write_private_file(path: Path, content: str) -> None:
    """独占创建私有配置，权限在文件出现时即为 0600。"""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def initialize_configs(root: Path = ROOT) -> int:
    templates = root / "bot/config_examples"
    destination = root / "bot/user_data"
    mapping = {
        "config_trend_live.example.json": "config_trend_live.json",
        "config_trend_webserver.example.json": "config_trend_webserver.json",
        "config_dashboard.example.json": "config_dashboard.private.json",
    }
    if destination.is_symlink() or not destination.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("配置目录必须位于本项目内，不能使用符号链接")
    for name in mapping.values():
        target = destination / name
        if target.exists() or target.is_symlink():
            raise RuntimeError(f"本地配置已存在，拒绝覆盖：{target.name}")
    credentials = {"username": "quant-api", "password": secrets.token_urlsafe(32),
                   "jwt_secret_key": secrets.token_hex(32), "ws_token": secrets.token_urlsafe(32)}
    prepared = {}
    for template, name in mapping.items():
        payload = json.loads((templates / template).read_text(encoding="utf-8"))
        payload.setdefault("api_server", {}).update(credentials)
        payload["dry_run"] = True
        prepared[destination / name] = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    created = []
    try:
        for path, content in prepared.items():
            write_private_file(path, content)
            created.append(path)
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    print("已创建三份模拟盘配置，随机 API 凭据由服务端配置代持")
    return 0


def postgres_binary(name: str) -> str:
    # 显式安装目录优先；Homebrew 仅定位可执行文件，不管理全局服务。
    configured = os.getenv("PG_BIN") or os.getenv("QUANT_PG_BIN")
    if configured:
        candidate = Path(configured) / name
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise RuntimeError(f"PG_BIN 目录中找不到 {name}")
        return str(candidate)
    located = shutil.which(name)
    if located:
        return located
    for directory in ("/opt/homebrew/opt/postgresql@17/bin", "/usr/local/opt/postgresql@17/bin"):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise RuntimeError("找不到 PostgreSQL 17 可执行文件，请安装后设置 PG_BIN")


def postgres_cluster(root: Path = ROOT) -> Path:
    cluster = root / "database/runtime/postgres"
    # pg_ctl 的 -D 只能指向本项目真实目录，拒绝间接指向其他数据库的链接。
    for relative in ("database", "database/runtime", "database/runtime/postgres"):
        if (root / relative).is_symlink():
            raise RuntimeError("本项目 PostgreSQL 目录不能使用符号链接")
    if not cluster.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("PostgreSQL 数据目录不在本项目内")
    return cluster


def postgres_running(root: Path = ROOT) -> bool:
    cluster = postgres_cluster(root)
    if not (cluster / "PG_VERSION").is_file():
        return False
    result = subprocess.run([postgres_binary("pg_ctl"), "-D", str(cluster), "status"],
                            capture_output=True, text=True, check=False, timeout=10)
    if result.returncode not in {0, 3}:
        raise RuntimeError("无法确认本项目 PostgreSQL 状态，请检查数据库目录权限")
    return result.returncode == 0


def postgres_start(root: Path = ROOT) -> int:
    cluster = postgres_cluster(root)
    if not (cluster / "PG_VERSION").is_file():
        raise RuntimeError("数据库尚未初始化，请执行 uv run python manage.py db init")
    if postgres_running(root):
        print("本项目 PostgreSQL：已在运行，复用现有实例")
        return 0
    if port_busy(5433):
        raise RuntimeError("5433 端口已有其他服务，拒绝启动或操作该进程")
    run_command([postgres_binary("pg_ctl"), "-D", str(cluster), "-l",
                 str(cluster.parent / "postgres.log"), "-o", "-h 127.0.0.1 -p 5433",
                 "-w", "-t", "30", "start"], root)
    return 0


def postgres_stop(root: Path = ROOT) -> int:
    cluster = postgres_cluster(root)
    if not postgres_running(root):
        print("本项目 PostgreSQL：未运行")
        return 0
    run_command([postgres_binary("pg_ctl"), "-D", str(cluster), "-m", "fast", "-w",
                 "-t", "30", "stop"], root)
    return 0


def uses_local_postgres() -> bool:
    """只在明确配置到本项目 5433 端口时自动管理原生数据库。"""
    from psycopg.conninfo import conninfo_to_dict

    try:
        config = conninfo_to_dict(os.getenv("QUANT_DATABASE_URL", ""))
    except Exception:
        return False
    return (config.get("host") in {"127.0.0.1", "localhost"}
            and config.get("port") == "5433" and not config.get("service")
            and config.get("hostaddr", "127.0.0.1") == "127.0.0.1")


def ensure_local_postgres(root: Path = ROOT) -> None:
    if uses_local_postgres() and (postgres_cluster(root) / "PG_VERSION").is_file():
        postgres_start(root)


def postgres_initialize(root: Path = ROOT) -> int:
    """创建独立原生集群、应用角色与生产/测试库；不覆盖任何已有配置。"""
    import psycopg
    from psycopg import sql

    cluster = postgres_cluster(root)
    environment = root / ".env"
    if environment.exists() or environment.is_symlink():
        raise RuntimeError("私有 .env 已存在，拒绝覆盖；已有数据库请使用 db start")
    if cluster.exists():
        raise RuntimeError("PostgreSQL 数据目录已存在，拒绝初始化")
    if port_busy(5433):
        raise RuntimeError("5433 端口已有其他服务，拒绝初始化本地数据库")
    initdb = postgres_binary("initdb")
    postgres_binary("pg_ctl")
    runtime = cluster.parent
    runtime.mkdir(parents=True, mode=0o700, exist_ok=True)
    socket_dir = cluster / "socket"
    administrator = getpass.getuser()
    password = secrets.token_urlsafe(32)
    created_environment = False
    # 独占创建目录，阻止并发初始化误清理另一个命令的新集群。
    cluster.mkdir(mode=0o700)
    try:
        run_command([initdb, "-D", str(cluster), "--username", administrator,
                     "--auth-local=trust", "--auth-host=scram-sha-256",
                     "--encoding=UTF8", "--locale=C"], root)
        socket_dir.mkdir(mode=0o700)
        configuration = cluster / "postgresql.conf"
        with configuration.open("a", encoding="utf-8") as output:
            socket_path = str(socket_dir).replace("'", "''")
            output.write("\n# 本项目独立数据库，仅监听本机与私有 socket 目录。\n"
                         "listen_addresses = '127.0.0.1'\nport = 5433\n"
                         f"unix_socket_directories = '{socket_path}'\n")
        postgres_start(root)
        with psycopg.connect(host=str(socket_dir), port=5433, user=administrator,
                             dbname="postgres", autocommit=True) as connection:
            # DDL 的名称与密码分别转义，密码不会进入命令行或日志。
            connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier("quant_app"), sql.Literal(password)))
            for name in ("quant_terminal", "quant_test"):
                connection.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(name), sql.Identifier("quant_app")))
        content = ("# 本机私有数据库凭据，请勿提交或共享。\n"
                   f"QUANT_DATABASE_URL=postgresql://quant_app:{password}@127.0.0.1:5433/quant_terminal\n"
                   f"QUANT_TEST_DATABASE_URL=postgresql://quant_app:{password}@127.0.0.1:5433/quant_test\n"
                   "QUANT_STREAM_ENABLED=true\nQUANT_STREAM_PAIRS=BTC/USDT,ETH/USDT\n"
                   "QUANT_STREAM_MARKET=futures\n")
        write_private_file(environment, content)
        created_environment = True
    except Exception:
        # 只清理本次创建的新集群，保护其他现有目录和 .env。
        if (cluster / "PG_VERSION").exists():
            postgres_stop(root)
        if cluster.exists():
            shutil.rmtree(cluster)
        if created_environment:
            environment.unlink(missing_ok=True)
        raise RuntimeError("本项目数据库初始化失败，已清理本次新建集群") from None
    load_environment(root)
    print("已创建本机 PostgreSQL、quant_terminal 与 quant_test；凭据写入私有 .env")
    return 0


def deploy_frontend(root: Path = ROOT, api_url: str | None = None) -> Path | None:
    """先准备完整新产物，再替换目录；失败时立即还原，旧版本留作回滚。"""
    dist = root / "vben/apps/web-antd/dist"
    if not (dist / "index.html").is_file():
        raise RuntimeError("前端构建未产生 index.html")
    stage = root / f".web-stage-{uuid.uuid4().hex}"
    live = root / "web"
    backups = root / "logs/web-backups"
    backup = None
    try:
        shutil.copytree(dist, stage)
        configs = list(stage.glob("_app-config*.js"))
        if not configs:
            raise RuntimeError("前端产物缺少运行时配置 _app-config*.js")
        target_url = api_url or os.getenv("API_URL", "http://127.0.0.1:8890/api")
        for config in configs:
            content = config.read_text(encoding="utf-8")
            replacement = '"VITE_GLOB_API_URL":' + json.dumps(target_url)
            content, replaced = re.subn(r'"VITE_GLOB_API_URL"\s*:\s*"[^"\\]*"',
                                       lambda _match: replacement, content)
            if not replaced:
                raise RuntimeError(f"无法识别运行时 API 配置：{config.name}")
            config.write_text(content, encoding="utf-8")
        if live.exists():
            backups.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = backups / f"{stamp}-{uuid.uuid4().hex[:8]}"
            live.rename(backup)
        try:
            stage.rename(live)
        except OSError:
            if backup is not None:
                backup.rename(live)
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print(f"前端已发布到 {live}，API：{target_url}")
    if backup:
        print(f"旧版本保存在 {backup}")
    return backup


def audit(root: Path = ROOT, skip_frontend: bool = False) -> int:
    # 只检查运维与业务数据层，避免审计命令调用模型研究或训练入口。
    names = ("manage.py", "runtime_config.py", "auth_service.py", "serve_web.py", "shot.py",
             "data_store.py", "data_sync.py", "market_stream.py")
    for name in names:
        file = root / name
        if file.is_file():
            ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    print("后端入口语法检查通过")
    run_command([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], root)
    if not skip_frontend:
        run_command(["pnpm", "typecheck"], root / "vben/apps/web-antd")
    return 0


def database_status() -> int:
    from data_store import DataStore

    cluster = postgres_cluster()
    if uses_local_postgres() and (cluster / "PG_VERSION").is_file():
        if not postgres_running():
            print(json.dumps({"managed_cluster": {"path": str(cluster), "port": 5433,
                             "initialized": True, "running": False}}, ensure_ascii=False, indent=2))
            return 0
    store = DataStore()
    try:
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2, default=str))
    except Exception as exc:
        raise RuntimeError("PostgreSQL 状态读取失败，请检查私有 .env 与数据库服务") from exc
    finally:
        store.close()
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="量化终端 Python 运维入口（不管理模型迭代）")
    commands = result.add_subparsers(dest="action", required=True)
    for action in ("start", "stop", "restart", "status"):
        command = commands.add_parser(action)
        command.add_argument("services", nargs="*", choices=tuple(services()))
    commands.add_parser("build", help="类型检查、构建并发布前端")
    commands.add_parser("init-config", help="从公开模板创建随机凭据的本地模拟盘配置")
    command = commands.add_parser("audit", help="后端测试与前端类型检查")
    command.add_argument("--skip-frontend", action="store_true")
    command = commands.add_parser("db", help="查看 PostgreSQL 状态")
    command.add_argument("operation", nargs="?", default="status",
                         choices=("init", "start", "stop", "status"))
    commands.add_parser("sync", help="单次同步文件到数据库，额外参数原样传给 data_sync.py")
    commands.add_parser("shot", help="前端截图，额外参数原样传给 shot.py")
    return result


def main(argv: list[str] | None = None) -> int:
    load_environment(ROOT)
    cli = parser()
    args, extras = cli.parse_known_args(argv)
    if extras and args.action not in {"sync", "shot"}:
        cli.error("无法识别参数：" + " ".join(extras))
    try:
        if args.action in {"start", "stop", "restart", "status"}:
            registered = services()
            selected = [registered[name] for name in (args.services or registered)]
            if args.action == "status":
                return status(selected)
            if args.action in {"stop", "restart"}:
                if not all([stop_service(spec) for spec in reversed(selected)]):
                    return 1
            if args.action in {"start", "restart"}:
                ensure_local_postgres()
                for spec in selected:
                    start_service(spec)
            return 0
        if args.action == "build":
            run_command(["pnpm", "typecheck"], ROOT / "vben/apps/web-antd")
            run_command(["pnpm", "build:antd"], ROOT / "vben")
            deploy_frontend()
            return 0
        if args.action == "init-config":
            return initialize_configs()
        if args.action == "audit":
            return audit(skip_frontend=args.skip_frontend)
        if args.action == "db":
            operations = {"init": postgres_initialize, "start": postgres_start,
                          "stop": postgres_stop, "status": database_status}
            return operations[args.operation]()
        if args.action in {"sync", "shot"}:
            file = "data_sync.py" if args.action == "sync" else "shot.py"
            run_command([sys.executable, str(ROOT / file), *extras])
            return 0
    except subprocess.CalledProcessError as exc:
        print(f"操作失败：子命令退出码 {exc.returncode}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
