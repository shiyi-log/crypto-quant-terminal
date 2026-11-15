"""迭代执行故障与研究裁定分离，所有文件和门禁均隔离。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bot import auto_iteration as iteration


def metrics(**changes):
    return {"trades": 120, "profit_pct": 5, "max_drawdown_pct": 10,
            "profit_factor": 1.2, "sharpe": 0.8, **changes}


def paths(tmp_path):
    return {"registry_path": tmp_path / "versions.json",
            "status_path": tmp_path / "status.json",
            "progress_path": tmp_path / "progress.json",
            "lock_path": tmp_path / "iteration.lock",
            "history_path": tmp_path / "history.jsonl"}


def run_generic(tmp_path, values):
    with patch.object(iteration, "auto_iteration_gate_enabled", return_value=True):
        return iteration.run_iteration(
            candidate={"id": "candidate-a"}, train_fn=lambda: {"trained": True},
            evaluate_fn=lambda artifact: values, run_id="run-a",
            enable_auto_iteration=True, **paths(tmp_path))


def test_losing_candidate_is_successful_execution_with_rejected_verdict(tmp_path):
    result = run_generic(tmp_path, metrics(profit_pct=-2))
    assert result["execution_status"] == "succeeded"
    assert result["research_verdict"] == "rejected"
    assert result["entry"]["evaluation_status"] == "rejected"


def test_small_sample_is_not_estimable_even_when_metrics_look_profitable(tmp_path):
    result = run_generic(tmp_path, metrics(trades=0))
    assert result["execution_status"] == "succeeded"
    assert result["research_verdict"] == "not_estimable"
    assert result["entry"]["evaluation_status"] == "not_estimable"


@pytest.mark.parametrize("invalid", [{}, metrics(sharpe=float("nan"))])
def test_invalid_metrics_fail_execution_without_registering_candidate(tmp_path, invalid):
    result = run_generic(tmp_path, invalid)
    assert result["execution_status"] == "failed"
    assert result["research_verdict"] == "not_estimable"
    assert not (tmp_path / "versions.json").exists()


def config_file(tmp_path):
    path = tmp_path / "training.json"
    path.write_text(json.dumps({"dry_run": True, "freqai": {"enabled": True},
                                "exchange": {"pair_whitelist": ["BTC/USDT"]}}))
    return path


def test_stale_progress_from_another_run_cannot_register_a_candidate(tmp_path):
    options = paths(tmp_path)
    options["progress_path"].write_text(json.dumps(
        {"run_id": "run-old", "status": "done", "result": metrics()}))
    with patch.object(iteration, "auto_iteration_gate_enabled", return_value=True):
        result = iteration.run_iteration(
            config_path=config_file(tmp_path), runner=lambda *args: 0,
            run_id="run-new", enable_auto_iteration=True, **options)
    assert result["status"] == "failed"
    assert result["execution_status"] == "failed"
    assert not options["registry_path"].exists()


def test_current_run_result_retains_evidence_and_never_changes_champion(tmp_path):
    options = paths(tmp_path)
    original = {"champions": {"ml_model": "current"}, "versions": [
        {"id": "current", "layer": "ml_model", "status": "champion"}]}
    options["registry_path"].write_text(json.dumps(original))
    command_seen = []

    def runner(command, cwd, progress):
        command_seen.extend(command)
        progress.write_text(json.dumps({"run_id": "run-new", "status": "done",
                                        "result": metrics(),
                                        "result_source": "fresh",
                                        "evidence": {"manifest": "run-new/manifest.json"}}))
        return 0

    with patch.object(iteration, "auto_iteration_gate_enabled", return_value=True):
        result = iteration.run_iteration(
            config_path=config_file(tmp_path), runner=runner,
            run_id="run-new", enable_auto_iteration=True, **options)
    assert "--run-id" in command_seen
    assert command_seen[command_seen.index("--run-id") + 1] == "run-new"
    assert result["execution_status"] == "succeeded"
    assert result["research_verdict"] == "passed"
    assert result["evidence"]["manifest"] == "run-new/manifest.json"
    assert result["result_source"] == "fresh"
    stored = json.loads(options["registry_path"].read_text())
    assert stored["champions"] == {"ml_model": "current"}


def test_default_subprocess_progress_is_per_run_and_does_not_use_sharedui(tmp_path):
    options = paths(tmp_path)
    del options["progress_path"]
    observed = []

    def runner(command, cwd, progress):
        observed.append(Path(progress))
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text(json.dumps({"run_id": "run-a", "status": "done",
                                        "result": metrics()}))
        return 0

    with patch.object(iteration, "auto_iteration_gate_enabled", return_value=True), \
            patch.object(iteration, "discover_progress_path",
                         side_effect=AssertionError("shared progress must not be read")):
        result = iteration.run_iteration(config_path=config_file(tmp_path), runner=runner,
                                         run_id="run-a", enable_auto_iteration=True, **options)
    assert result["execution_status"] == "succeeded"
    assert "run-a" in observed[0].parts


def test_terminal_evidence_is_preserved_when_failed_run_id_is_reused(tmp_path):
    options = paths(tmp_path)
    attempts = []

    def train():
        attempts.append(1)
        raise RuntimeError("fixture failure")

    with patch.object(iteration, "auto_iteration_gate_enabled", return_value=True):
        first = iteration.run_iteration(
            candidate={"id": "candidate-a"}, train_fn=train,
            evaluate_fn=lambda artifact: metrics(), run_id="run-a",
            enable_auto_iteration=True, **options)
        frozen = (tmp_path / "iterations" / "run-a" / "result.json").read_bytes()
        second = iteration.run_iteration(
            candidate={"id": "candidate-a"}, train_fn=train,
            evaluate_fn=lambda artifact: metrics(), run_id="run-a",
            enable_auto_iteration=True, **options)
    assert first["execution_status"] == "failed"
    assert second["status"] == "skipped"
    assert attempts == [1]
    assert (tmp_path / "iterations" / "run-a" / "result.json").read_bytes() == frozen


def test_registered_version_references_frozen_run_result(tmp_path):
    result = run_generic(tmp_path, metrics())
    assert "iterations/run-a/result.json" in result["entry"]["artifacts"]
    frozen = json.loads((tmp_path / "iterations" / "run-a" / "result.json").read_text())
    assert frozen["metrics"]["profit_pct"] == 5
    assert frozen["research_verdict"] == "passed"


def test_execution_error_retains_requested_identity_for_iteration_list(tmp_path):
    options = paths(tmp_path)
    with patch.object(iteration, "auto_iteration_gate_enabled", return_value=True):
        result = iteration.run_iteration(
            config_path=config_file(tmp_path), runner=lambda *args: 3,
            strategy="ExampleStrategy", freqaimodel="ExampleModel", timerange="20260101-20260201",
            run_id="run-bad", enable_auto_iteration=True, **options)
    assert result["strategy"] == "ExampleStrategy"
    assert result["model"] == "ExampleModel"
    assert result["timerange"] == "20260101-20260201"
    assert result["started_at"]
    assert result["finished_at"]
    assert result["error_code"] == "subprocess_failed"


def test_archive_write_failure_cannot_register_a_successful_candidate(tmp_path):
    real_open = Path.open

    def unavailable(path, mode="r", *args, **kwargs):
        if path.name.startswith(".result-") and mode == "x":
            raise OSError("archive full")
        return real_open(path, mode, *args, **kwargs)

    with patch.object(Path, "open", unavailable):
        result = run_generic(tmp_path, metrics())
    assert result["execution_status"] == "failed"
    assert result["error_code"] == "archive_failed"
    assert not (tmp_path / "versions.json").exists()


def test_truncated_frozen_file_cannot_make_a_run_silently_skipped(tmp_path):
    directory = tmp_path / "iterations" / "run-a"
    directory.mkdir(parents=True)
    (directory / "result.json").write_text("{")
    result = run_generic(tmp_path, metrics())
    assert result["execution_status"] == "failed"
    assert result["error_code"] == "archive_failed"
    assert not (tmp_path / "versions.json").exists()


def test_registry_failure_is_explicit_and_retry_reuses_frozen_metrics(tmp_path):
    with patch.object(iteration.registry, "save", side_effect=OSError("read only")):
        first = run_generic(tmp_path, metrics())
    assert first["execution_status"] == "failed"
    assert first["error_code"] == "registry_write_failed"
    with patch.object(iteration, "auto_iteration_gate_enabled", return_value=True):
        second = iteration.run_iteration(
            candidate={"id": "candidate-a"},
            train_fn=lambda: pytest.fail("registration retry must reuse evidence"),
            evaluate_fn=lambda artifact: pytest.fail("must reuse frozen metrics"),
            run_id="run-a", enable_auto_iteration=True, **paths(tmp_path))
    assert second["execution_status"] == "succeeded"
    assert second["research_verdict"] == "passed"
    assert (tmp_path / "versions.json").exists()


def test_registration_receipt_recovers_missing_history_without_retraining(tmp_path):
    real_open = Path.open

    def unavailable(path, mode="r", *args, **kwargs):
        if path.name == "history.jsonl" and mode == "a":
            raise OSError("history unavailable")
        return real_open(path, mode, *args, **kwargs)

    with patch.object(Path, "open", unavailable):
        first = run_generic(tmp_path, metrics())
    assert first["execution_status"] == "failed"
    assert first["error_code"] == "publication_failed"
    with patch.object(iteration, "auto_iteration_gate_enabled", return_value=True):
        second = iteration.run_iteration(
            candidate={"id": "candidate-a"},
            train_fn=lambda: pytest.fail("must recover from frozen evidence"),
            evaluate_fn=lambda artifact: pytest.fail("must reuse frozen metrics"),
            run_id="run-a", enable_auto_iteration=True, **paths(tmp_path))
    assert second["execution_status"] == "succeeded"
    rows = [json.loads(row) for row in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert rows[-1]["run_id"] == "run-a"
    assert rows[-1]["execution_status"] == "succeeded"
