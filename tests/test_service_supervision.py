"""Service intent survives supervision without changing business gates."""

import json
from unittest.mock import patch

import pytest

import manage


def test_manual_stop_is_persistent_and_supervisor_does_not_resume_it(tmp_path):
    # Break caught: supervisor starts a service after an explicit operator stop.
    spec = manage.services(tmp_path)["auth"]
    with patch("manage.stop_service", return_value=True):
        assert manage.run_lifecycle("stop", [spec], tmp_path) == 0
    with patch("manage.start_service", side_effect=AssertionError("paused service restarted")), \
            patch("manage.find_processes", return_value=[]):
        record = manage.supervise_once([spec], tmp_path)[0]
    assert record["desired_state"] == "stopped"
    assert record["running"] is False
    assert record["health_reason"] == "operator_stopped"


def test_failed_stop_still_prevents_supervisor_from_starting_replacement(tmp_path):
    # Break caught: a timed-out stop accidentally clears the operator's intent.
    spec = manage.services(tmp_path)["sync"]
    with patch("manage.stop_service", return_value=False):
        assert manage.run_lifecycle("stop", [spec], tmp_path) == 1
    with patch("manage.find_processes", return_value=[]), \
            patch("manage.start_service", side_effect=AssertionError("replacement started")):
        record = manage.supervise_once([spec], tmp_path)[0]
    assert record["desired_state"] == "stopped"


def test_explicit_start_resumes_paused_service_without_modifying_gates(tmp_path):
    # Break caught: explicit start leaves a persistent pause behind.
    spec = manage.services(tmp_path)["sync"]
    gates = tmp_path / "auth/gates.json"
    gates.parent.mkdir()
    gates.write_text('{"fixture":"unchanged"}')
    with patch("manage.stop_service", return_value=True), \
            patch("manage.start_service", return_value=True), \
            patch("manage.ensure_local_postgres"), \
            patch("service_supervision.check_service_schema", return_value=True):
        assert manage.run_lifecycle("stop", [spec], tmp_path) == 0
        assert manage.run_lifecycle("start", [spec], tmp_path) == 0
    with patch("manage.find_processes", return_value=[87]), \
            patch("manage.health", return_value="HTTP 200"):
        record = manage.service_snapshot(tmp_path, [spec])[0]
    assert record["desired_state"] == "running"
    assert record["pids"] == [87]
    assert gates.read_text() == '{"fixture":"unchanged"}'


def test_corrupt_desired_state_fails_closed_and_reports_error(tmp_path):
    # Break caught: corrupt intent is treated as absence and services restart.
    state = tmp_path / "database/runtime/service_control.json"
    state.parent.mkdir(parents=True)
    state.write_text('{broken')
    spec = manage.services(tmp_path)["sync"]
    with patch("manage.start_service", side_effect=AssertionError("corrupt state restarted")), \
            patch("manage.find_processes", return_value=[]):
        record = manage.supervise_once([spec], tmp_path)[0]
    assert record["desired_state"] == "unknown"
    assert record["health_reason"] == "service_control_invalid"
    assert record["last_error"] == "service_control_invalid"


def test_supervision_continues_after_one_service_start_fails(tmp_path):
    # Break caught: one startup failure prevents independent services recovering.
    specs = manage.services(tmp_path)
    selected = [specs["sync"], specs["auth"]]
    def start(spec, root):
        if spec.name == "sync":
            raise RuntimeError("private error text")
        return True
    with patch("manage.start_service", side_effect=start), \
            patch("manage.find_processes", return_value=[]), \
            patch("service_supervision.check_service_schema", return_value=True):
        records = manage.supervise_once(selected, tmp_path)
    assert [row["key"] for row in records] == ["sync", "auth"]
    assert records[0]["last_error"] == "service_start_failed"
    assert "private error text" not in json.dumps(records)
    saved = json.loads((tmp_path / "database/runtime/service_supervisor.json").read_text())
    assert saved["services"][0]["last_error"] == "service_start_failed"


def test_supervisor_parser_accepts_once_and_rejects_nonpositive_interval():
    assert manage.parser().parse_args(["supervise", "--once", "sync"]).once is True
    with pytest.raises(SystemExit):
        manage.parser().parse_args(["supervise", "--interval", "0"])


def test_supervisor_log_rotation_keeps_new_messages_and_bounded_history(tmp_path):
    # Break caught: repeated supervision grows its own log without bound.
    for index in range(12):
        manage.write_supervisor_log(tmp_path, f"round-{index}: " + "x" * 30,
                                    max_bytes=100, backups=2)
    logs = sorted((tmp_path / "logs").glob("supervisor.log*"))
    assert len(logs) == 3
    assert "round-11" in (tmp_path / "logs/supervisor.log").read_text()
    assert all(path.stat().st_size <= 100 for path in logs)
