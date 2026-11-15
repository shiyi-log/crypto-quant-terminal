"""Backtest success requires a validated result belonging to this invocation."""
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch
import zipfile

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "backtest_task_under_test", Path(__file__).resolve().parents[1] / "bot/run_backtest_task.py"
)
task = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(task)


def result_payload(strategy="ExampleStrategy", **updates):
    stats = {
        "backtest_start": "2026-01-01 00:00:00+00:00",
        "backtest_end": "2026-02-01 00:00:00+00:00",
        "backtest_days": 31, "total_trades": 2, "trades": [],
        "wins": 1, "losses": 1, "winrate": 0.5,
        "profit_total_abs": 10.0, "profit_total": 0.001,
        "final_balance": 10010.0, "starting_balance": 10000.0,
        "max_drawdown_account": 0.01, "sharpe": 0.8, "sortino": 1.0,
        "calmar": 1.0, "profit_factor": 1.2, "expectancy": 5.0,
        "cagr": 0.01, "results_per_pair": [], "exit_reason_summary": [],
        "timerange": "20260101-20260201",
    }
    stats.update(updates)
    return {"strategy": {strategy: stats}, "strategy_comparison": []}


def write_zip(path, payload=None, *, config_first=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        if config_first:
            archive.writestr("result_config.json", json.dumps({"dry_run": True}))
        archive.writestr("result.json", json.dumps(payload or result_payload()))


@pytest.fixture
def invocation(tmp_path):
    user_data = tmp_path / "user_data"
    strategy_dir = user_data / "strategies"
    strategy_dir.mkdir(parents=True)
    (strategy_dir / "ExampleStrategy.py").write_text("class ExampleStrategy: pass\n")
    data = user_data / "data" / "binance"
    data.mkdir(parents=True)
    (data / "BTC_USDT-1d.json").write_text("[[1767225600000,1,1,1,1,1]]")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "user_data_dir": str(user_data), "dry_run": True,
        "exchange": {"name": "binance", "pair_whitelist": ["BTC/USDT"]},
        "freqai": {"enabled": True, "identifier": "test-model"},
    }))
    progress = tmp_path / "progress.json"
    argv = [
        "run_backtest_task.py", "--config", str(config), "--strategy",
        "ExampleStrategy", "--strategy-path", str(strategy_dir),
        "--timerange", "20260101-20260201", "--progress", str(progress),
    ]
    return {"root": tmp_path, "user_data": user_data, "config": config,
            "progress": progress, "argv": argv, "commands": []}


def run_task(invocation, produce=None, *, returncode=0, extra_args=(), logs=()):
    # Only the external Freqtrade child is replaced. Result/progress/archive I/O stays real.
    class Child:
        stdout = iter(logs)

        def wait(self):
            return returncode

    def launch(command, **_kwargs):
        invocation["commands"].append(command)
        if "--backtest-directory" in command:
            destination = Path(command[command.index("--backtest-directory") + 1])
        else:
            destination = invocation["user_data"] / "backtest_results"
        if produce:
            produce(destination, command)
            # Mirror Freqtrade's model provenance fields without replacing parser I/O.
            config_path = Path(command[command.index("--config") + 1])
            effective = json.loads(config_path.read_text())["freqai"]["identifier"]
            for path in destination.glob("*.zip"):
                try:
                    with zipfile.ZipFile(path) as archive:
                        members = {name: archive.read(name) for name in archive.namelist()}
                    payload = json.loads(members["result.json"])
                    for stats in payload["strategy"].values():
                        stats.setdefault("freqaimodel", "LightGBMRegressor")
                        stats.setdefault("freqai_identifier", effective)
                    members["result.json"] = json.dumps(payload).encode()
                    with zipfile.ZipFile(path, "w") as archive:
                        for name, content in members.items():
                            archive.writestr(name, content)
                except (zipfile.BadZipFile, KeyError):
                    pass
        return Child()

    with patch.object(sys, "argv", invocation["argv"] + list(extra_args)), \
            patch.object(task.subprocess, "Popen", side_effect=launch):
        rc = task.main()
    return rc, json.loads(invocation["progress"].read_text())


def test_successful_child_without_result_is_failed(invocation):
    rc, state = run_task(invocation)
    assert rc != 0
    assert state["status"] == "failed"
    assert state["percent"] < 100
    assert state["result"] is None


def test_unverified_old_cache_cannot_be_reported_as_success(invocation):
    write_zip(invocation["user_data"] / "backtest_results" / "cached.zip")
    rc, state = run_task(invocation)
    assert rc != 0
    assert state["status"] == "failed"
    command = invocation["commands"][0]
    assert command[command.index("--cache") + 1] == "none"


def test_concurrent_other_task_result_is_never_selected(invocation):
    def produce(_destination, _command):
        write_zip(invocation["user_data"] / "backtest_results" / "foreign.zip",
                  result_payload("OtherStrategy"))
    rc, state = run_task(invocation, produce)
    assert rc != 0
    assert state["status"] == "failed"
    assert state["result"] is None


def test_valid_result_isolated_and_archived_before_done(invocation):
    rc, state = run_task(invocation, lambda directory, _: write_zip(directory / "result.zip"))
    assert rc == 0
    assert state["status"] == "done"
    assert state["result"]["profit_abs"] == 10.0
    assert state["run_id"]
    assert state["execution_status"] == "succeeded"
    manifest = json.loads(Path(state["manifest_path"]).read_text())
    assert manifest["run_id"] == state["run_id"]
    assert manifest["result_sha256"]
    assert manifest["identity"]["data_sha256"]
    assert manifest["identity"]["strategy"] == "ExampleStrategy"
    assert Path(state["artifact_dir"]).parent.name == "runs"


@pytest.mark.parametrize("payload", [result_payload("OtherStrategy"), result_payload(sharpe=float("nan"))])
def test_invalid_or_wrong_strategy_result_fails(invocation, payload):
    rc, state = run_task(invocation, lambda directory, _: write_zip(directory / "result.zip", payload))
    assert rc != 0
    assert state["status"] == "failed"
    assert state["result"] is None


def test_corrupt_result_fails(invocation):
    def produce(directory, _command):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "broken.zip").write_bytes(b"not a zip")
    rc, state = run_task(invocation, produce)
    assert rc != 0
    assert state["status"] == "failed"


def test_zero_trades_is_valid_execution_and_keeps_zero(invocation):
    payload = result_payload(total_trades=0, wins=0, losses=0, winrate=0,
                             profit_total_abs=0, profit_total=0, final_balance=10000)
    rc, state = run_task(invocation, lambda directory, _: write_zip(directory / "zero.zip", payload))
    assert rc == 0
    assert state["result"]["trades"] == 0
    assert state["execution_status"] == "succeeded"


def test_failed_child_cannot_publish_result(invocation):
    rc, state = run_task(invocation, lambda directory, _: write_zip(directory / "result.zip"), returncode=7)
    assert rc == 7
    assert state["status"] == "failed"
    assert state["result"] is None


def test_parser_finds_stats_when_config_json_is_first(tmp_path):
    path = tmp_path / "result.zip"
    write_zip(path, config_first=True)
    result = task.parse_result(str(path))
    assert result["strategy"] == "ExampleStrategy"
    assert result["trades"] == 2


def test_requested_timerange_must_match(invocation):
    rc, state = run_task(invocation, lambda directory, _: write_zip(
        directory / "wrong-period.zip", result_payload(timerange="20250101-20250201")))
    assert rc != 0
    assert state["status"] == "failed"


def test_explicit_run_id_is_kept_and_cannot_overwrite_an_existing_run(invocation):
    def producer(directory, _command):
        write_zip(directory / "result.zip")
    rc, state = run_task(invocation, producer, extra_args=["--run-id", "first-run"])
    assert rc == 0
    assert state["run_id"] == "first-run"
    manifest_path = Path(state["manifest_path"])
    before = manifest_path.read_bytes()
    rc, state = run_task(invocation, producer, extra_args=["--run-id", "first-run"])
    assert rc != 0
    assert manifest_path.read_bytes() == before


def test_validated_cache_is_reused_without_running_another_backtest(invocation):
    def producer(directory, _command):
        write_zip(directory / "result.zip")
    rc, first = run_task(invocation, producer)
    assert rc == 0
    prior_commands = len(invocation["commands"])
    rc, cached = run_task(invocation, extra_args=["--reuse-run", first["artifact_dir"]])
    assert rc == 0
    assert cached["result_source"] == "cache"
    assert cached["run_id"] != first["run_id"]
    assert len(invocation["commands"]) == prior_commands
    assert cached["result"] == first["result"]


def test_cache_data_mismatch_recomputes_instead_of_reusing(invocation):
    def producer(directory, _command):
        write_zip(directory / "result.zip")
    rc, first = run_task(invocation, producer)
    assert rc == 0
    (invocation["user_data"] / "data/binance/BTC_USDT-1d.json").write_text("[[1767225600000,2,2,2,2,2]]")
    rc, second = run_task(invocation, producer, extra_args=["--reuse-run", first["artifact_dir"]])
    assert rc == 0
    assert second["result_source"] == "fresh"
    assert second["cache_rejection"]


def test_inputs_changed_during_backtest_cannot_be_published(invocation):
    def produce(directory, _command):
        write_zip(directory / "result.zip")
        (invocation["user_data"] / "data/binance/BTC_USDT-1d.json").write_text("changed")
    rc, state = run_task(invocation, produce)
    assert rc != 0
    assert state["status"] == "failed"
    assert state["result"] is None


@pytest.mark.parametrize("updates", [{"freqaimodel": "OtherModel"},
                                      {"freqai_identifier": "foreign-model-cache"}])
def test_result_model_and_effective_identifier_must_match(invocation, updates):
    rc, state = run_task(invocation, lambda directory, _: write_zip(
        directory / "mismatch.zip", result_payload(**updates)))
    assert rc != 0
    assert state["status"] == "failed"


def test_archive_failure_prevents_success_manifest(invocation):
    model_parent = invocation["user_data"] / "models"
    model_parent.mkdir()
    (model_parent / "test-model").write_text("blocks archive directory")
    rc, state = run_task(invocation, lambda directory, _: write_zip(directory / "result.zip"))
    assert rc != 0
    assert state["status"] == "failed"
    assert state["result"] is None
    assert not Path(state["manifest_path"]).exists()


def test_effective_config_freezes_include_environment_and_identifier(invocation, monkeypatch):
    included = invocation["root"] / "include.json"
    included.write_text(json.dumps({"stake_amount": 50, "freqai": {"train_period_days": 30}}))
    config = json.loads(invocation["config"].read_text())
    config.update({"add_config_files": ["include.json"], "stake_amount": 100})
    invocation["config"].write_text(json.dumps(config))
    monkeypatch.setenv("FREQTRADE__STAKE_AMOUNT", "200")
    rc, state = run_task(invocation, lambda directory, _: write_zip(directory / "result.zip"))
    assert rc == 0
    frozen = json.loads((Path(state["artifact_dir"]) / "config.json").read_text())
    assert frozen["stake_amount"] == 200
    assert frozen["freqai"]["train_period_days"] == 30
    assert frozen["freqai"]["identifier"] != "test-model"
    assert "add_config_files" not in frozen
    assert (Path(state["artifact_dir"]) / "config.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("artifact", ["config.json", "inputs.json"])
def test_cache_with_damaged_input_evidence_is_recomputed(invocation, artifact):
    def producer(directory, _command):
        write_zip(directory / "result.zip")
    rc, first = run_task(invocation, producer)
    assert rc == 0
    (Path(first["artifact_dir"]) / artifact).write_text("corrupted evidence")
    rc, second = run_task(invocation, producer, extra_args=["--reuse-run", first["artifact_dir"]])
    assert rc == 0
    assert second["result_source"] == "fresh"
    assert second["cache_rejection"]


def test_freqai_swallowed_training_exception_is_execution_failure(invocation):
    rc, state = run_task(invocation, lambda directory, _: write_zip(directory / "result.zip"),
                         logs=["Training BTC/USDT raised exception ValueError. Message: bad data, skipping.\n"])
    assert rc != 0
    assert state["status"] == "failed"
    assert state["error_code"] == "model_training_failed"
