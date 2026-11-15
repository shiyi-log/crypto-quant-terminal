"""Drain managed stdout without unbounded files or changing child argv."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import uuid


def drain(stream, path: Path, *, max_bytes: int = 10_485_760, backups: int = 5) -> None:
    if max_bytes <= 0 or backups < 1:
        raise ValueError("invalid log rotation limits")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > max_bytes:
        # Keep historical evidence intact during the first rollout.
        path.rename(path.with_name(f"{path.name}.preserved-{uuid.uuid4().hex}"))
    read = getattr(stream, "read1", stream.read)
    while chunk := read(65536):
        while chunk:
            size = path.stat().st_size if path.exists() else 0
            if size >= max_bytes:
                path.with_name(f"{path.name}.{backups}").unlink(missing_ok=True)
                for index in range(backups - 1, 0, -1):
                    older = path.with_name(f"{path.name}.{index}")
                    if older.exists():
                        older.replace(path.with_name(f"{path.name}.{index + 1}"))
                path.replace(path.with_name(f"{path.name}.1"))
                size = 0
            part, chunk = chunk[:max_bytes - size], chunk[max_bytes - size:]
            with path.open("ab") as output:
                output.write(part)


def start_sink(root: Path, path: Path):
    """Parent passes returned writer to stdout and closes its own copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--path", str(path)],
        cwd=root, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if process.stdin is None:
        raise RuntimeError("service_log_sink_failed")
    return process, process.stdin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=10_485_760)
    parser.add_argument("--backups", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        drain(sys.stdin.buffer, args.path, max_bytes=args.max_bytes, backups=args.backups)
    except (OSError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
