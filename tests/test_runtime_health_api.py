"""Authenticated operational views preserve independent sources and ownership."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def auth(tmp_path):
    source = Path(__file__).resolve().parents[1] / "auth_service.py"
    spec = importlib.util.spec_from_file_location("runtime_health_auth_test", source)
    value = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"QUANT_AUTH_DIR": str(tmp_path)}):
        spec.loader.exec_module(value)
    return value


def handler(auth, path):
    value = object.__new__(auth.Handler)
    value.path = path
    value._bearer = lambda: "fixture"
    value._read_body = lambda: b""
    value._send = lambda code, payload: (code, payload)
    return value


def test_runtime_health_requires_auth_before_reading_sources(auth):
    with patch.object(auth, "verify_token", return_value=None), patch.object(
        auth, "get_store", side_effect=AssertionError("unauthorized source read")):
        assert handler(auth, "/api/locals/runtime-health").do_GET()[0] == 401


def test_health_database_failure_still_returns_collection_and_storage_status(auth, tmp_path):
    with patch.object(auth, "verify_token", return_value="admin"), patch.object(
        auth, "ROOT", tmp_path), patch.object(auth, "get_store", side_effect=RuntimeError("secret db detail")):
        code, payload = handler(auth, "/api/locals/runtime-health").do_GET()
    assert code == 200
    components = {item["key"]: item for item in payload["components"]}
    assert "collection" in components and "storage" in components
    assert components["sync"]["state"] == "failed"
    assert "secret db detail" not in str(payload)


def test_service_view_uses_owned_processes_and_current_iteration_entry(auth, tmp_path):
    import manage
    owned = [{"key": "web", "name": "前端", "port": 8888,
              "desired_state": "stopped", "running": False, "pid": None,
              "pids": [], "health_reason": "operator_stopped", "last_error": None}]
    class Store:
        def get_document(self, name, default=None):
            return {"run_id": "run-new", "status": "failed"} if name == "auto_iteration_status.json" else default
    def process(command, **kwargs):
        if command[:2] == ["pgrep", "-f"]:
            return SimpleNamespace(stdout="777\n" if "auto_iteration.py" in command[2] else "")
        raise AssertionError("port listeners cannot prove managed process ownership")
    with patch.object(auth, "verify_token", return_value="admin"), patch.object(
        auth, "ROOT", tmp_path), patch.object(auth, "get_store", return_value=Store()), patch.object(
        manage, "service_snapshot", return_value=owned), patch("subprocess.run", side_effect=process):
        code, payload = handler(auth, "/api/locals/services").do_GET()
    assert code == 200
    services = {item["key"]: item for item in payload["services"]}
    assert services["web"]["desired_state"] == "stopped"
    assert services["web"]["running"] is False
    assert services["auto_iterate"]["pid"] == "777"
    assert services["auto_iterate"]["state"]["run_id"] == "run-new"
