"""在线逻辑备份与隔离恢复；不管理数据库进程，不覆盖已有备份或数据库。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from psycopg.conninfo import conninfo_to_dict


class BackupError(RuntimeError):
    """公开错误只描述失败步骤，不包含 DSN、命令或服务端异常文本。"""


def postgres_binary(name: str) -> str:
    """查找客户端；不初始化或启动本地集群。"""
    configured = os.getenv("QUANT_PG_BIN") or os.getenv("PG_BIN")
    directories = ([configured] if configured else []) + [
        "/opt/homebrew/opt/postgresql@17/bin",
        "/usr/local/opt/postgresql@17/bin",
    ]
    if not configured and (located := shutil.which(name)):
        return located
    for directory in directories:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise BackupError("PostgreSQL client unavailable; configure QUANT_PG_BIN or container")


def _connection(dsn: str, container: str | None) -> tuple[dict[str, str], dict[str, str]]:
    try:
        config = conninfo_to_dict(dsn)
    except Exception:
        raise BackupError("invalid database connection") from None
    if not config.get("dbname") or config.get("service"):
        raise BackupError("explicit database connection required")
    # 不继承进程中另一个数据库的 libpq 选项，避免隐式切换目标。
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    mapping = {"host": "PGHOST", "hostaddr": "PGHOSTADDR", "port": "PGPORT",
               "user": "PGUSER", "password": "PGPASSWORD", "dbname": "PGDATABASE",
               "sslmode": "PGSSLMODE", "sslcert": "PGSSLCERT",
               "sslkey": "PGSSLKEY", "sslrootcert": "PGSSLROOTCERT"}
    for key, variable in mapping.items():
        if key in config:
            environment[variable] = config[key]
    environment["PGCONNECT_TIMEOUT"] = "10"
    if container:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
            raise BackupError("invalid database container")
        if config.get("host") not in {"localhost", "127.0.0.1", "::1"}:
            raise BackupError("container requires local database connection")
        environment.update(PGHOST="127.0.0.1", PGPORT="5432")
        environment.pop("PGHOSTADDR", None)
    return config, environment


def _command(name: str, arguments: list[str], environment: dict[str, str],
             container: str | None) -> list[str]:
    if container is None:
        return [postgres_binary(name), *arguments]
    # docker -e NAME 从客户端环境继承值；凭据从不进入进程参数。
    forwarding = [item for key in sorted(environment) if key.startswith("PG")
                  for item in ("-e", key)]
    return ["docker", "exec", "-i", *forwarding, container, name, *arguments]


def _run(name: str, arguments: list[str], environment: dict[str, str],
         container: str | None, *, source: BinaryIO | None = None,
         output: BinaryIO | None = None) -> None:
    command = _command(name, arguments, environment, container)
    try:
        subprocess.run(command, env=environment, stdin=source,
                       stdout=output if output is not None else subprocess.PIPE,
                       stderr=subprocess.PIPE, check=True, timeout=7200)
    except (OSError, subprocess.SubprocessError):
        raise BackupError(f"database operation failed: {name}") from None


def _hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def backup_database(dsn: str, destination: str | Path, *,
                    container: str | None = None) -> Path:
    """新建完整备份目录，格式验证成功后才发布可恢复清单。"""
    config, environment = _connection(dsn, container)
    target = Path(destination).absolute()
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=False)
    except OSError:
        raise BackupError("backup destination must be a new writable directory") from None
    archive = target / "database.dump"
    try:
        with archive.open("xb") as output:
            os.chmod(archive, 0o600)
            _run("pg_dump", ["--format=custom", "--no-password"], environment,
                 container, output=output)
            output.flush()
            os.fsync(output.fileno())
        # 子进程直接继承文件描述符；无缓冲读取保证 magic 检查后的 seek 真正回到文件头。
        with archive.open("rb", buffering=0) as source:
            if source.read(5) != b"PGDMP":
                raise BackupError("database archive format invalid")
            source.seek(0)
            _run("pg_restore", ["--list"], environment, container, source=source)
        manifest: dict[str, Any] = {
            "schema": "quant-database-backup-v1", "status": "verified",
            "created_at": _now(),
            "source": {key: config[key] for key in ("host", "port", "dbname")
                       if key in config},
            "transport": "docker" if container else "client",
            "archive": {"file": archive.name, "sha256": _hash(archive),
                        "size_bytes": archive.stat().st_size},
        }
        stage = target / "manifest.json.partial"
        with stage.open("x", encoding="utf-8") as output:
            os.chmod(stage, 0o600)
            json.dump(manifest, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        stage.replace(target / "manifest.json")
        return target / "manifest.json"
    except BackupError:
        raise
    except (OSError, ValueError):
        raise BackupError("database backup file operation failed") from None


def _verified_archive(backup_dir: str | Path) -> tuple[Path, dict[str, Any]]:
    directory = Path(backup_dir)
    archive = directory / "database.dump"
    manifest_path = directory / "manifest.json"
    try:
        if archive.is_symlink() or manifest_path.is_symlink():
            raise ValueError
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = manifest["archive"]
        if (manifest["schema"] != "quant-database-backup-v1"
                or manifest["status"] != "verified"
                or record["file"] != "database.dump"
                or record["size_bytes"] != archive.stat().st_size
                or record["sha256"] != _hash(archive)):
            raise ValueError
        return archive, manifest
    except (OSError, ValueError, KeyError, TypeError):
        raise BackupError("database backup integrity validation failed") from None


def restore_database(backup_dir: str | Path, target_dsn: str, *,
                     container: str | None = None) -> dict[str, Any]:
    """只恢复到新建本机 quant_restore_* 库，拒绝覆盖或清理已有数据库。"""
    config, environment = _connection(target_dsn, container)
    database = config["dbname"]
    if (config.get("host") not in {"localhost", "127.0.0.1", "::1"}
            or config.get("hostaddr", "127.0.0.1") not in {"127.0.0.1", "::1"}
            or not re.fullmatch(r"quant_restore_[a-zA-Z0-9_]{1,48}", database)):
        raise BackupError("restore requires isolated local quant_restore_* target")
    archive, manifest = _verified_archive(backup_dir)
    with archive.open("rb") as source:
        _run("pg_restore", ["--list"], environment, container, source=source)
    create_environment = {**environment, "PGDATABASE": "postgres"}
    _run("createdb", ["--no-password", "--maintenance-db=postgres", database],
         create_environment, container)
    with archive.open("rb") as source:
        _run("pg_restore", ["--no-password", "--exit-on-error",
                            "--single-transaction", "--no-owner", "--no-privileges",
                            "--dbname", database], environment, container, source=source)
    return {"status": "restored", "database": database, "restored_at": _now(),
            "archive_sha256": manifest["archive"]["sha256"],
            "business_validation": "pending"}
