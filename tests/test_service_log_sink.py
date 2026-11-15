"""Exercise the real detached stdout sink with byte streams."""

from pathlib import Path
import subprocess
import sys
import time


SINK = Path(__file__).resolve().parents[1] / "service_logs.py"


def run_sink(path, payload):
    return subprocess.run(
        [sys.executable, str(SINK), "--path", str(path),
         "--max-bytes", "32", "--backups", "2"],
        input=payload, capture_output=True, check=False, timeout=5,
    )


def test_log_sink_rotates_binary_output_and_keeps_latest_bytes(tmp_path):
    # Break caught: stdout grows unbounded, drops non-UTF8, or loses final EOF chunk.
    path = tmp_path / "worker.log"
    payload = bytes(range(100))
    result = run_sink(path, payload)
    assert result.returncode == 0, result.stderr
    logs = [tmp_path / "worker.log.2", tmp_path / "worker.log.1", path]
    assert all(log.stat().st_size <= 32 for log in logs)
    assert b"".join(log.read_bytes() for log in logs) == payload[32:]


def test_log_sink_preserves_oversized_existing_log_before_rotation(tmp_path):
    # Break caught: a rollout silently deletes or truncates historical active logs.
    path = tmp_path / "worker.log"
    old = b"historical evidence\n" * 10
    path.write_bytes(old)
    result = run_sink(path, b"new run\n")
    assert result.returncode == 0, result.stderr
    archives = list(tmp_path.glob("worker.log.preserved-*"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == old
    assert path.read_bytes() == b"new run\n"


def test_log_sink_publishes_small_output_before_worker_exits(tmp_path):
    # Break caught: buffered reads hide low-volume logs until 64 KiB or EOF.
    path = tmp_path / "worker.log"
    child = subprocess.Popen([sys.executable, str(SINK), "--path", str(path)],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        child.stdin.write(b"ready\n")
        child.stdin.flush()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not path.exists():
            time.sleep(0.01)
        assert path.exists(), "log stayed buffered until worker exit"
        assert path.read_bytes() == b"ready\n"
    finally:
        child.stdin.close()
        child.wait(timeout=5)
