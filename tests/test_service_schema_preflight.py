"""Startup checks existing schema before any database-dependent child runs."""

import json
from types import SimpleNamespace

import pytest

import data_store
import service_supervision
from service_state import desired_state, set_desired


class Manager:
    def __init__(self, events, *, running=(), stop_ok=True):
        self.events = events
        self.running = set(running)
        self.stop_ok = stop_ok

    def ensure_local_postgres(self, root=None):
        self.events.append(("postgres", root))

    def start_service(self, spec, root):
        self.events.append(("start", spec.name))
        self.running.add(spec.name)
        return True

    def stop_service(self, spec, root):
        self.events.append(("stop", spec.name))
        if self.stop_ok:
            self.running.discard(spec.name)
        return self.stop_ok

    def find_processes(self, spec, *, allow_legacy=False):
        return [42] if spec.name in self.running else []

    def health(self, spec):
        return "healthy"


def spec(name):
    return SimpleNamespace(name=name, label=name, port=None)


@pytest.fixture
def database(monkeypatch):
    events = []
    state = {"error": None, "result": {"status": "compatible"}}

    class Store:
        def __init__(self, *, root):
            events.append(("store", root))

        def check_schema(self):
            events.append(("check", None))
            if state["error"]:
                raise state["error"]
            return state["result"]

        def initialize(self):
            raise AssertionError("preflight initialized business pool")

        def migrate_schema(self, **kwargs):
            raise AssertionError("preflight migrated schema")

        def close(self):
            events.append(("close", None))

    monkeypatch.setattr(data_store, "DataStore", Store)
    return events, state


def test_start_checks_schema_after_postgres_and_before_any_selected_child(tmp_path, database):
    # Break caught: even an independent child starts before the selected DB batch passes.
    events, _ = database
    manager = Manager(events)
    selected = [spec("web"), spec("auth"), spec("sync")]
    assert service_supervision.run_lifecycle(manager, "start", selected, tmp_path) == 0
    assert events == [
        ("postgres", tmp_path), ("store", tmp_path), ("check", None), ("close", None),
        ("start", "web"), ("start", "auth"), ("start", "sync"),
    ]
    assert desired_state(tmp_path, "auth")[0] == "running"


@pytest.mark.parametrize("reason", ["missing", "pending", "checksum drift", "unreachable"])
def test_failed_start_preserves_stopped_intent_and_starts_no_child(
        tmp_path, database, reason):
    # Break caught: failed migration check leaves a future supervisor resume intent.
    events, state = database
    state["error"] = RuntimeError(f"{reason}: postgresql://u:secret@private/db")
    set_desired(tmp_path, ["auth", "web"], "stopped")
    manager = Manager(events)
    with pytest.raises(RuntimeError, match="service_schema_preflight_failed") as error:
        service_supervision.run_lifecycle(
            manager, "start", [spec("web"), spec("auth")], tmp_path)
    assert "secret" not in str(error.value)
    assert "private" not in str(error.value)
    assert not any(event[0] == "start" for event in events)
    assert events[-1] == ("close", None)
    assert desired_state(tmp_path, "auth")[0] == "stopped"
    assert desired_state(tmp_path, "web")[0] == "stopped"


def test_restart_preflight_failure_leaves_existing_child_and_intent_untouched(
        tmp_path, database):
    # Break caught: restart stops a healthy service before discovering a bad schema.
    events, state = database
    state["error"] = RuntimeError("missing migration")
    set_desired(tmp_path, ["auth"], "running")
    manager = Manager(events, running=["auth"])
    before = (tmp_path / "database/runtime/service_control.json").read_bytes()
    with pytest.raises(RuntimeError, match="service_schema_preflight_failed"):
        service_supervision.run_lifecycle(manager, "restart", [spec("auth")], tmp_path)
    assert manager.running == {"auth"}
    assert not any(event[0] in {"start", "stop"} for event in events)
    assert (tmp_path / "database/runtime/service_control.json").read_bytes() == before


def test_restart_checks_once_before_reverse_stop_and_normal_start(tmp_path, database):
    # Break caught: restart repeats/misorders the schema probe or stops out of order.
    events, _ = database
    manager = Manager(events, running=["auth", "sync"])
    assert service_supervision.run_lifecycle(
        manager, "restart", [spec("auth"), spec("sync")], tmp_path) == 0
    assert events == [
        ("postgres", tmp_path), ("store", tmp_path), ("check", None), ("close", None),
        ("stop", "sync"), ("stop", "auth"), ("start", "auth"), ("start", "sync"),
    ]


def test_stop_never_requires_database_and_failed_stop_does_not_restart(tmp_path, database):
    # Break caught: unavailable schema prevents an operator from stopping children.
    events, state = database
    state["error"] = RuntimeError("database unavailable")
    manager = Manager(events, running=["auth"], stop_ok=False)
    assert service_supervision.run_lifecycle(manager, "restart", [], tmp_path) == 0
    assert service_supervision.run_lifecycle(manager, "stop", [spec("auth")], tmp_path) == 1
    assert events == [("stop", "auth")]
    assert desired_state(tmp_path, "auth")[0] == "stopped"


@pytest.mark.parametrize("name", ["api", "webserver", "web"])
def test_independent_service_does_not_require_application_schema(tmp_path, database, name):
    # Break caught: static/legacy services depend on unrelated PG DDL.
    events, state = database
    state["error"] = RuntimeError("database unavailable")
    assert service_supervision.run_lifecycle(Manager(events), "start", [spec(name)], tmp_path) == 0
    assert events == [("start", name)]


@pytest.mark.parametrize("name", [
    "auth", "sync", "market", "market-trx", "market-spot", "market-breadth",
    "strategy-lab", "spot-basis", "integrity", "market-live",
])
def test_each_database_dependent_service_is_blocked_by_invalid_schema(
        tmp_path, database, name):
    # Break caught: a collector or direct-SQL tool is omitted from the PG dependency map.
    events, state = database
    state["error"] = RuntimeError("unversioned schema")
    with pytest.raises(RuntimeError, match="service_schema_preflight_failed"):
        service_supervision.run_lifecycle(Manager(events), "start", [spec(name)], tmp_path)
    assert not any(event[0] == "start" for event in events)


@pytest.mark.parametrize("result", [None, {}, {"status": "pending"}, True])
def test_preflight_rejects_missing_or_incompatible_result(tmp_path, database, result):
    # Break caught: a returning-but-incomplete check result authorizes children.
    events, state = database
    state["result"] = result
    with pytest.raises(RuntimeError, match="service_schema_preflight_failed"):
        service_supervision.run_lifecycle(Manager(events), "start", [spec("auth")], tmp_path)
    assert not any(event[0] == "start" for event in events)


def test_unknown_service_has_no_implicit_database_exemption(tmp_path, database):
    # Break caught: newly registered services silently skip the mandatory classification.
    events, _ = database
    with pytest.raises(RuntimeError, match="service_dependency_unclassified"):
        service_supervision.run_lifecycle(Manager(events), "start", [spec("new-worker")], tmp_path)
    assert events == []


def test_supervisor_schema_failure_blocks_db_children_but_recovers_independent_service(
        tmp_path, database):
    # Break caught: automated recovery bypasses the startup check or leaks private errors.
    events, state = database
    state["error"] = RuntimeError("postgresql://u:secret@private/db")
    manager = Manager(events)
    records = service_supervision.supervise_once(
        manager, [spec("auth"), spec("sync"), spec("web")], tmp_path)
    assert manager.running == {"web"}
    assert [row["last_error"] for row in records] == [
        "service_schema_preflight_failed", "service_schema_preflight_failed", None]
    saved = (tmp_path / "database/runtime/service_supervisor.json").read_text()
    logged = (tmp_path / "logs/supervisor.log").read_text()
    assert "secret" not in saved + logged
    assert "private" not in saved + logged
    assert json.loads(saved)["services"][0]["running"] is False
    assert events.count(("check", None)) == 1


def test_supervisor_only_checks_for_missing_running_intent_db_services(tmp_path, database):
    # Break caught: paused or already running DB children trigger unnecessary probes.
    events, state = database
    state["error"] = RuntimeError("database unavailable")
    set_desired(tmp_path, ["sync"], "stopped")
    manager = Manager(events, running=["auth"])
    records = service_supervision.supervise_once(
        manager, [spec("auth"), spec("sync"), spec("web")], tmp_path)
    assert records[1]["health_reason"] == "operator_stopped"
    assert events == [("start", "web")]


def test_supervisor_checks_once_before_launching_database_children(tmp_path, database):
    # Break caught: automatic recovery starts DB children before a shared successful check.
    events, _ = database
    manager = Manager(events)
    records = service_supervision.supervise_once(
        manager, [spec("auth"), spec("market-live")], tmp_path)
    assert events == [
        ("postgres", tmp_path), ("store", tmp_path), ("check", None), ("close", None),
        ("start", "auth"), ("start", "market-live"),
    ]
    assert all(row["running"] for row in records)
    assert all(row["last_error"] is None for row in records)


def test_unclassified_supervisor_service_is_blocked_without_blocking_static_recovery(
        tmp_path, database):
    # Break caught: an unknown new worker is implicitly exempt from DB classification.
    events, _ = database
    manager = Manager(events)
    records = service_supervision.supervise_once(
        manager, [spec("new-worker"), spec("web")], tmp_path)
    assert manager.running == {"web"}
    assert records[0]["last_error"] == "service_dependency_unclassified"
    assert events == [("start", "web")]


def test_database_process_failure_is_sanitized_before_any_child(tmp_path, database):
    # Break caught: local pg_ctl errors expose connection details or allow child startup.
    events, _ = database
    manager = Manager(events)

    def unavailable(root):
        raise OSError("postgresql://u:secret@private/db")

    manager.ensure_local_postgres = unavailable
    with pytest.raises(RuntimeError, match="service_schema_preflight_failed") as error:
        service_supervision.run_lifecycle(manager, "start", [spec("auth")], tmp_path)
    assert "secret" not in str(error.value)
    assert events == []
