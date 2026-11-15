"""Health views keep independent pipeline failures visible without database I/O."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib
import importlib.util
import json

import pytest


NOW = datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)
STAMP = "2026-11-16T00:00:00Z"
MS = 1794787200000


def module():
    assert importlib.util.find_spec("runtime_health"), "runtime health capability missing"
    return importlib.import_module("runtime_health")


def write(root, path, value):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value))


def snapshots(root):
    for path in ("market_stream_status.json", "trx/market_stream_status.json",
                 "spot/market_stream_status.json"):
        write(root, "database/runtime/" + path, {
            "updated_at": MS, "state": "running", "last_message": MS,
            "last_flush": MS, "data_gap_possible": False,
            "last_error": None, "database_error": None,
            "write_failure_batches": 0, "dead_letter_batches": 0})
    write(root, "database/runtime/candle_integrity_status.json", {
        "generated_at_utc": STAMP, "ok": True, "error_count": 0, "dataset_count": 5})


def rows(result):
    return {item["key"]: item for item in result["components"]}


def build(root, **changes):
    return module().build_runtime_health(root, now=NOW, environ={},
        sync={"status": "completed", "finished_at": STAMP, "last_success_at": STAMP,
              "errors": 0, "storage_errors": 0},
        archive={"running": False, "closed": False, "pending_rows": 0,
                 "pending_datasets": 0, "last_error": None}, **changes)


def test_completed_file_sync_cannot_hide_failed_collection_or_integrity(tmp_path):
    snapshots(tmp_path)
    write(tmp_path, "database/runtime/market_stream_status.json", {
        "updated_at": MS, "last_message": MS, "last_flush": MS,
        "state": "storage_error", "last_error": "password=sensitive /private/path",
        "database_error": "secret database detail", "write_failure_batches": 2})
    write(tmp_path, "database/runtime/candle_integrity_status.json", {
        "generated_at_utc": STAMP, "ok": False, "error_count": 8, "dataset_count": 5})
    result = build(tmp_path)
    by_key = rows(result)
    assert by_key["sync"]["state"] == "ok"
    assert by_key["collection"]["state"] == "failed"
    assert by_key["integrity"]["state"] == "failed"
    assert by_key["collection"]["metrics"]["write_failure_batches"] == 2
    assert result["ok"] is False
    assert "sensitive" not in json.dumps(result)
    assert "/private/path" not in json.dumps(result)


def test_old_heartbeat_and_recent_heartbeat_without_messages_are_not_healthy(tmp_path):
    snapshots(tmp_path)
    write(tmp_path, "database/runtime/market_stream_status.json", {
        "updated_at": MS - 120_000, "last_message": MS - 120_000,
        "last_flush": MS - 120_000, "state": "running"})
    write(tmp_path, "database/runtime/trx/market_stream_status.json", {
        "updated_at": MS, "state": "running", "last_message": None, "last_flush": None})
    by_key = rows(build(tmp_path))
    assert by_key["collection"]["state"] == "failed"
    assert "status_stale" in by_key["collection"]["error_codes"]
    assert by_key["collection_trx"]["state"] == "warning"
    assert "collection_no_messages" in by_key["collection_trx"]["error_codes"]


def test_missing_corrupt_and_future_status_remain_explicit(tmp_path):
    snapshots(tmp_path)
    (tmp_path / "database/runtime/market_stream_status.json").unlink()
    (tmp_path / "database/runtime/trx/market_stream_status.json").write_text("{broken")
    write(tmp_path, "database/runtime/spot/market_stream_status.json", {
        "updated_at": MS + 120_000, "state": "running", "last_message": MS})
    by_key = rows(build(tmp_path))
    assert by_key["collection"]["error_codes"] == ["status_missing"]
    assert by_key["collection_trx"]["error_codes"] == ["status_invalid"]
    assert "status_clock_skew" in by_key["collection_spot"]["error_codes"]
    assert by_key["collection_spot"]["state"] != "ok"


def test_archive_pending_is_reported_separately_without_inventing_success_time(tmp_path):
    snapshots(tmp_path)
    result = module().build_runtime_health(tmp_path, now=NOW, environ={},
        sync={"status": "completed", "finished_at": STAMP},
        archive={"running": True, "pending_rows": 17, "pending_datasets": 2,
                 "last_error": "DatabaseError", "closed": False})
    archive = rows(result)["archive"]
    assert archive["state"] == "failed"
    assert archive["metrics"]["pending_rows"] == 17
    assert archive["last_success_at"] is None
    assert "archive_error" in archive["error_codes"]


def test_storage_mount_identity_mismatch_is_failed_and_read_only(tmp_path):
    module_ = module()
    destination = tmp_path / "unmounted" / "quant-data"
    result = module_.build_runtime_health(tmp_path, now=NOW, environ={
        "QUANT_DATA_ROOT": str(destination), "QUANT_DATA_MOUNT": str(destination.parent),
        "QUANT_DATA_VOLUME_UUID": "expected"},
        volume_probe=lambda mount: {"mounted": True, "uuid": "wrong", "writable": True})
    storage = rows(result)["storage"]
    assert storage["state"] == "failed"
    assert "storage_identity_mismatch" in storage["error_codes"]
    assert not destination.exists()


def test_local_storage_is_explicit_migration_pending_and_never_zero_free_space(tmp_path):
    snapshots(tmp_path)
    storage = rows(build(tmp_path))["storage"]
    assert storage["state"] == "warning"
    assert storage["error_codes"] == ["storage_external_not_configured"]
    assert storage["metrics"]["external"] is False
    assert "free_bytes" not in storage["metrics"]


def test_storage_permission_and_free_space_failures_are_not_hidden(tmp_path, monkeypatch):
    module_ = module()
    disk = tmp_path / "disk"
    disk.mkdir()
    settings = {"QUANT_DATA_ROOT": str(disk / "data"), "QUANT_DATA_MOUNT": str(disk),
                "QUANT_DATA_VOLUME_UUID": "known"}
    result = module_.build_runtime_health(tmp_path, now=NOW, environ=settings,
        volume_probe=lambda mount: {"mounted": True, "uuid": "known", "writable": False})
    assert "storage_not_writable" in rows(result)["storage"]["error_codes"]
    monkeypatch.setattr(module_.shutil, "disk_usage", lambda path: type("Disk", (), {"free": 10})())
    result = module_.build_runtime_health(tmp_path, now=NOW, environ=settings,
        volume_probe=lambda mount: {"mounted": True, "uuid": "known", "writable": True})
    assert "storage_space_low" in rows(result)["storage"]["error_codes"]
    assert rows(result)["storage"]["metrics"]["free_bytes"] == 10
    assert not (disk / "data").exists()


@pytest.mark.parametrize("invalid", [[], "success", {"generated_at_utc": STAMP}])
def test_malformed_integrity_never_reports_ok(tmp_path, invalid):
    snapshots(tmp_path)
    write(tmp_path, "database/runtime/candle_integrity_status.json", invalid)
    integrity = rows(build(tmp_path))["integrity"]
    assert integrity["state"] != "ok"
    assert "status_invalid" in integrity["error_codes"]


def test_nonfinite_poll_interval_cannot_disable_staleness_detection(tmp_path):
    snapshots(tmp_path)
    write(tmp_path, "database/runtime/candle_integrity_status.json", {
        "generated_at_utc": "2026-11-15T00:00:00Z", "ok": True,
        "error_count": 0, "dataset_count": 5, "interval_seconds": float("inf")})
    integrity = rows(build(tmp_path))["integrity"]
    assert integrity["state"] == "failed"
    assert "status_stale" in integrity["error_codes"]
    assert "status_invalid" in integrity["error_codes"]
