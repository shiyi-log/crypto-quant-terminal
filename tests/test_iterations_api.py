"""真实路由的迭代只读视图；不启动服务、不读取真实数据库。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def auth(tmp_path):
    spec = importlib.util.spec_from_file_location("iterations_auth_test",
                                               Path(__file__).resolve().parents[1] / "auth_service.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"QUANT_AUTH_DIR": str(tmp_path)}):
        spec.loader.exec_module(module)
    return module


def request(auth, path, rows, current=None, authorized=True, progress=None):
    handler = object.__new__(auth.Handler)
    handler.path = path
    handler._bearer = lambda: "test-token"
    handler._read_body = lambda: b""
    handler._send = lambda code, payload: (code, payload)
    store = Mock()
    store.read_events.return_value = rows
    store.get_document.side_effect = lambda name, default=None: (
        current if name == "auto_iteration_status.json"
        else progress if name == "iterations/run-a/progress.json" else default)
    with patch.object(auth, "verify_token", return_value="admin" if authorized else None), \
            patch.object(auth, "get_store", return_value=store):
        return handler.do_GET()


def test_list_keeps_execution_failure_separate_from_research_rejection(auth):
    rows = [{"run_id": "run-a", "status": "rejected",
             "gate": {"valid": True, "passed": False,
                       "values": {"trades": 120, "profit_pct": -1}},
             "entry": {"spec": {"config": "/private/path/config.json",
                                   "password": "secret-test"}}},
            {"run_id": "run-b", "status": "failed"}]
    code, payload = request(auth, "/api/locals/iterations", rows)
    assert code == 200
    assert payload["total"] == 2
    assert payload["items"][0]["run_id"] == "run-b"
    assert payload["items"][0]["execution_status"] == "failed"
    assert payload["items"][1]["execution_status"] == "succeeded"
    assert payload["items"][1]["research_verdict"] == "rejected"
    assert "secret-test" not in str(payload)
    assert "/private/path" not in str(payload)


def test_detail_uses_requested_run_and_current_progress_supersedes_old_snapshot(auth):
    rows = [{"run_id": "run-a", "status": "running"},
            {"run_id": "run-b", "status": "registered"}]
    current = {"run_id": "run-a", "status": "failed", "result_source": "fresh",
               "error_code": "missing_result"}
    code, payload = request(auth, "/api/locals/iterations/run-a", rows, current)
    assert code == 200
    assert payload["run_id"] == "run-a"
    assert payload["execution_status"] == "failed"
    assert payload["error_code"] == "missing_result"
    assert payload["result_source"] == "fresh"


@pytest.mark.parametrize("suffix, expected", [
    ("/missing", 404), ("/%2e%2e%2fsecret", 400),
    ("?limit=0", 400), ("?limit=2&limit=3", 400)])
def test_missing_and_invalid_queries_are_explicit(auth, suffix, expected):
    code, payload = request(auth, "/api/locals/iterations" + suffix, [])
    assert code == expected
    assert "detail" in payload


def test_iterations_requires_authentication(auth):
    assert request(auth, "/api/locals/iterations", [], authorized=False)[0] == 401


def test_stale_running_snapshot_cannot_replace_completed_history(auth):
    rows = [{"run_id": "run-a", "status": "failed",
             "finished_at": "2026-11-16T01:00:00Z", "error_code": "subprocess_failed"}]
    current = {"run_id": "run-a", "status": "running"}
    code, payload = request(auth, "/api/locals/iterations/run-a", rows, current)
    assert code == 200
    assert payload["execution_status"] == "failed"
    assert payload["finished_at"] == "2026-11-16T01:00:00Z"


def test_running_detail_uses_only_its_matching_synced_progress(auth):
    current = {"run_id": "run-a", "status": "running"}
    progress = {"run_id": "run-a", "status": "training", "percent": 42,
                "epoch": 3, "epochs": 10}
    code, payload = request(auth, "/api/locals/iterations/run-a", [], current, progress=progress)
    assert code == 200
    assert payload["execution_status"] == "running"
    assert payload["progress"]["percent"] == 42
    progress["run_id"] = "run-other"
    _, payload = request(auth, "/api/locals/iterations/run-a", [], current, progress=progress)
    assert "progress" not in payload
