"""离线统计的独立离散答案、完整实验族及时间隔离合同。"""
import copy
import importlib
import itertools
import hashlib
import json
import logging
from fractions import Fraction

import pytest

from research_protocol import DAY_MS, FrozenResearchEvidence, freeze_experiment_family


def api():
    try:
        return importlib.import_module("research_statistics")
    except ModuleNotFoundError:
        pytest.fail("research_statistics implementation is missing")


def family():
    hour = 3_600_000
    return freeze_experiment_family({
        "family_id": "statistics-fixture", "registered_ms": 0,
        "dataset": {"start_ms": 0, "end_ms": 365 * DAY_MS,
                    "source_ref": "dataset:fixture", "sha256": "b" * 64},
        "symbols": ["BTC/USDT:USDT"], "timeframes": ["1h"],
        "factor_sets": [{"id": "factor", "sha256": "1" * 64}],
        "exit_sets": [{"id": "exit", "sha256": "2" * 64}],
        "cost_scenarios": ["base", "stress"], "seeds": [7],
        "label_horizons": {"1h": 1}, "label_span_ms": 2 * hour,
        "purge_ms": 2 * hour, "embargo_ms": 2 * hour,
        "comparison": {"method": "time_block_bootstrap",
                       "block_ms": (30 * DAY_MS - 4 * hour) // 30,
                       "multiple_testing": "holm", "alpha": "0.05"},
        "execution_policy": {"min_fill_rate": "0.5", "max_partial_fill_rate": "0.5",
                             "max_latency_p99_ms": 2000, "max_adverse_selection": "0.01",
                             "max_unfilled_opportunity_cost": "10"},
        "identities": {key: letter * 64 for key, letter in zip(
            ("code_hash", "data_hash", "cost_hash", "risk_hash", "strategy_hash"), "abcde")},
    })


def policy(frozen):
    value = frozen.to_dict()
    return {
        "stage": "validation", "metric": "fixed_capital_equity_delta_usdt",
        "period_ms": value["comparison"]["block_ms"] // 2,
        "block_ms": value["comparison"]["block_ms"],
        "block_anchor": "fold_start", "scheme": "nonoverlapping_stratified",
        "initial_capital": "1000", "capital_policy": "fixed_no_compounding_no_transfers",
        "boundary_policy": "flat_before_and_after_fold_costs_included",
        "evaluation_as_of_ms": value["dataset"]["end_ms"],
        "method": "time_block_bootstrap", "replicates": 99999, "seed": "9" * 64,
        "rng": "sha256-counter-rejection-v1", "money_scale": 24,
        "null": "aggregate_mean_le_zero", "tail": "greater_equal",
        "multiple_testing": "holm", "alpha": "0.05",
        "confidence_bound": "bonferroni_basic_rank",
        "min_tail_count": 20, "min_blocks_per_fold": 30,
        "degeneracy": "any_config_fold_constant_blocks_reject_family",
        "max_input_rows": 2000, "max_index_draws": 20_000_000,
        "max_weighted_additions": 40_000_000, "max_distribution_cells": 200_000,
    }


def inputs(frozen, inference):
    value, spec = frozen.to_dict(), inference.to_dict()
    result = []
    for ordinal, config in enumerate(value["configurations"]):
        rows = []
        for fold_index, fold in enumerate(value["folds"]):
            start, end = fold["validation_start_ms"], fold["validation_end_ms"]
            for index, timestamp in enumerate(range(start, end, spec["period_ms"])):
                # 每两period一个同号块，拆成IID会改变真实block分布。
                amount = "3.000000000000000000000001" if (index // 2) % 2 else "-1"
                rows.append({"fold_id": fold_index, "start_ms": timestamp,
                             "end_ms": timestamp + spec["period_ms"],
                             "available_ms": timestamp + spec["period_ms"],
                             "complete": True, "net_pnl": amount})
        result.append({
            "family_sha256": frozen.sha256, "inference_sha256": inference.sha256,
            "config_id": config["config_id"], "attempt_id": f"attempt-{ordinal}",
            "producer_id": "offline-fixture", "identities": value["identities"],
            "stage": "validation", "state": "completed",
            "execution_sha256": "4" * 64, "cost_sha256": "5" * 64,
            "equity_sha256": "6" * 64, "rows": rows,
        })
    return result


def test_freeze_requires_explicit_complete_immutable_policy():
    frozen = family()
    spec = policy(frozen)
    result = api().freeze_validation_inference(frozen, spec)
    value = result.to_dict()
    assert value["family_sha256"] == frozen.sha256
    assert value["folds"] == frozen.to_dict()["folds"]
    assert value["family_size"] == 2
    assert value["inference_registration_verified"] is False
    spec["seed"] = "1" * 64
    value["folds"].clear()
    assert result.to_dict()["seed"] == "9" * 64
    assert len(result.to_dict()["folds"]) == 5


@pytest.mark.parametrize(("field", "value"), [
    ("replicates", 15), ("replicates", True), ("seed", "x" * 64),
    ("method", "hac"), ("stage", "final_holdout"), ("money_scale", 23),
    ("min_tail_count", 0), ("min_blocks_per_fold", 1),
    ("max_input_rows", True), ("max_index_draws", 10**30),
    ("initial_capital", "1e3"), ("initial_capital", 1000.0),
    ("confidence_bound", "percentile"), ("alpha", "0.01"),
    ("block_ms", 1), ("period_ms", 1),
])
def test_unsupported_or_ad_hoc_inference_policy_is_rejected(field, value):
    frozen = family()
    spec = policy(frozen)
    spec[field] = value
    with pytest.raises(ValueError):
        api().freeze_validation_inference(frozen, spec)


def test_missing_extra_policy_and_forged_frozen_family_are_rejected():
    frozen = family()
    spec = policy(frozen)
    del spec["seed"]
    with pytest.raises(ValueError):
        api().freeze_validation_inference(frozen, spec)
    spec = policy(frozen) | {"source_authenticated": True}
    with pytest.raises(ValueError):
        api().freeze_validation_inference(frozen, spec)
    fake = FrozenResearchEvidence(frozen.canonical_json, "0" * 64)
    with pytest.raises(ValueError):
        api().freeze_validation_inference(fake, policy(frozen))


def test_exact_enumerated_unequal_folds_preserve_center_and_total_weight():
    # 独立枚举：2块fold {0,2}，1块fold {3}，原总数5。
    # 完整4抽样结果T*={3,5,5,7}，D={-2,0,0,2}，不是fold均值等权。
    module = api()
    draws = [((a, b), (0,)) for a, b in itertools.product(range(2), repeat=2)]
    assert [module._resampled_delta(((0, 2), (3,)), draw) for draw in draws] == [-2, 0, 0, 2]
    assert sum(module._resampled_delta(((0, 2), (3,)), draw) for draw in draws) == 0
    assert module._tail_summary(2, [-2, 0, 0, 2], Fraction(1, 5)) == {
        "raw_p": Fraction(2, 5), "lower_total": 0, "tail_count": 1, "tail_rank": 4}


@pytest.mark.parametrize(("total", "expected_p", "lower"), [
    (3, Fraction(1, 5), 1), (2, Fraction(2, 5), 0),
    (0, Fraction(4, 5), -2), (-3, Fraction(1), -5),
])
def test_rank_lower_bound_ties_match_plus_one_decision(total, expected_p, lower):
    result = api()._tail_summary(total, [-2, 0, 0, 2], Fraction(1, 5))
    assert result["raw_p"] == expected_p
    assert result["lower_total"] == lower
    assert (lower > 0) == (expected_p <= Fraction(1, 5))


def test_holm_known_vector_ties_and_bonferroni_use_complete_family():
    values = {"c": Fraction(4, 100), "a": Fraction(1, 100), "b": Fraction(3, 100)}
    assert api()._adjust(values, "holm") == {
        "a": Fraction(3, 100), "b": Fraction(6, 100), "c": Fraction(6, 100)}
    assert api()._adjust({"b": Fraction(1, 100), "a": Fraction(1, 100)}, "holm") == {
        "a": Fraction(2, 100), "b": Fraction(2, 100)}
    assert api()._adjust(values, "bonferroni")["c"] == Fraction(12, 100)


@pytest.mark.parametrize("amount", [True, 1.1, "1e2", "NaN", "Infinity", "-0",
                                     "0.0000000000000000000000001", "1" * 19, " 1", "+1"])
def test_money_rejects_lossy_or_unbounded_numbers(amount):
    with pytest.raises(ValueError):
        api()._money(amount)


def test_integer_money_keeps_42_digit_precision_without_decimal_context():
    module = api()
    amount = "999999999999999999.123456789012345678901234"
    assert module._money(amount) == 999999999999999999123456789012345678901234
    assert module._money_text(module._money(amount)) == amount
    assert module._money_text(module._money("0")) == "0"


@pytest.mark.parametrize("malformed", [None, [], 7, True])
def test_malformed_frozen_objects_produce_bounded_diagnostic(malformed):
    module = api()
    frozen = family()
    bad = FrozenResearchEvidence(malformed, "0" * 64)
    result = module.calculate_validation_family(bad, bad, []).to_dict()
    assert result["calculation_status"] == "not_estimable"
    assert result["reason_codes"] == ["family_invalid"]
    assert result["results"] == []
    inference = module.freeze_validation_inference(frozen, policy(frozen))
    result = module.calculate_validation_family(frozen, bad, inputs(frozen, inference)).to_dict()
    assert result["reason_codes"] == ["inference_invalid"]


def test_isolated_unicode_and_huge_family_span_are_bounded_before_rebuild():
    module, frozen = api(), family()
    bad = FrozenResearchEvidence("\ud800", "0" * 64)
    result = module.calculate_validation_family(frozen, bad, []).to_dict()
    assert result["reason_codes"] == ["inference_invalid"]
    value = frozen.to_dict()
    value["dataset"]["end_ms"] = 9_000_000_000_000_000_000
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    malformed = FrozenResearchEvidence(text, hashlib.sha256(text.encode()).hexdigest())
    result = module.calculate_validation_family(malformed, bad, []).to_dict()
    assert result["reason_codes"] == ["family_invalid"]


def test_rank_tail_insufficient_resolution_cannot_create_an_infinite_or_zero_p():
    with pytest.raises(ValueError, match="resolution"):
        api()._tail_summary(1, [0, 0, 0], Fraction(1, 10))


def test_fold_block_remainders_are_rejected_without_trimming():
    frozen = family()
    value = frozen.to_dict()
    # family本身合法，但当前比较块不整除有效验证窗口。
    source = {key: value[key] for key in ("family_id", "registered_ms", "dataset",
              "label_span_ms", "purge_ms", "embargo_ms", "comparison",
              "identities", "execution_policy", "label_horizons")}
    source.update(value["axes"])
    source["comparison"] = source["comparison"] | {"block_ms": 86_400_000}
    other = freeze_experiment_family(source)
    with pytest.raises(ValueError, match="fold_blocks"):
        api().freeze_validation_inference(other, policy(other))


def test_clustered_observations_remain_whole_time_blocks():
    # 两个完整块(+1,+1)与(-1,-1)，仅四种重采样误差。
    draws = [((a, b),) for a, b in itertools.product(range(2), repeat=2)]
    deltas = [api()._resampled_delta(((2, -2),), draw) for draw in draws]
    assert deltas == [4, 0, 0, -4]
    assert 2 not in deltas and -2 not in deltas


@pytest.mark.parametrize(("replicate", "fold", "draw", "count", "expected"), [
    (0, 0, 0, 2, 0), (0, 0, 1, 30, 5),
    (1, 2, 7, 30, 27), (99998, 4, 29, 30, 22),
])
def test_rng_golden_vectors_from_independent_sha256_bytes(replicate, fold, draw, count, expected):
    # 独立struct/SHA脚本先计算的固定答案，不调用被测函数生成期望。
    assert api()._draw_index(b"golden-fixture", replicate, fold, draw, count) == (expected, 0)


def test_rng_rejection_uses_next_retry_and_exhaustion_is_bounded(monkeypatch):
    class Digest:
        def __init__(self, value):
            self.value = value

        def digest(self):
            return self.value.to_bytes(32, "big")

    module = api()
    def controlled(raw):
        retry = int.from_bytes(raw[-4:], "big")
        return Digest(2**256 - 1 if retry == 0 else 17)
    # K=3时2**256-1恰在拒绝区，下一digest17映射为2。
    monkeypatch.setattr(module.hashlib, "sha256", controlled)
    assert module._draw_index(b"fixture", 0, 0, 0, 3) == (2, 1)
    monkeypatch.setattr(module.hashlib, "sha256", lambda raw: Digest(2**256 - 1))
    with pytest.raises(ValueError, match="rng_rejection_limit"):
        module._draw_index(b"fixture", 0, 0, 0, 3)


def test_real_bootstrap_uses_identical_draws_for_proportional_configurations():
    # 独立于完整输入fixture，完整B验证跨配置关系，若各配置单独抽样将失败。
    _, distributions, digest, _ = api()._bootstrap([((0, 2), (3,)), ((0, 4), (6,))],
                                                 [2, 1], b"shared-block-oracle")
    assert set(distributions[0]) == {-2, 0, 2}
    assert all(second == 2 * first for first, second in zip(*distributions, strict=True))
    assert len(digest) == 64


def test_actual_validated_blocks_keep_clusters_and_explicit_zero_observations():
    module, frozen = api(), family()
    inference = module.freeze_validation_inference(frozen, policy(frozen))
    series = inputs(frozen, inference)
    series[0]["rows"][0]["net_pnl"] = "0"
    prepared = module._validated_inputs(frozen, inference, series)
    assert prepared["blocks"][0][0][:3] == (-10**24, 6000000000000000000000002, -2 * 10**24)
    assert prepared["period_count"] == 300


@pytest.mark.parametrize("change", ["missing_stress", "extra", "duplicate", "attempt_duplicate",
                                  "missing_row", "duplicate_row", "future", "holdout",
                                  "wrong_identity", "incomplete", "float", "unknown",
                                  "constant", "missing_available", "forged_policy"])
def test_invalid_family_has_only_diagnostics_and_no_survivor_results(change):
    module, frozen = api(), family()
    inference = module.freeze_validation_inference(frozen, policy(frozen))
    series = inputs(frozen, inference)
    if change == "missing_stress":
        series.pop()
    elif change == "extra":
        series.append(copy.deepcopy(series[0]) | {"config_id": "f" * 64})
    elif change == "duplicate":
        series.append(copy.deepcopy(series[0]))
    elif change == "attempt_duplicate":
        series[1]["attempt_id"] = series[0]["attempt_id"]
    elif change == "missing_row":
        series[0]["rows"].pop()
    elif change == "duplicate_row":
        series[0]["rows"].append(copy.deepcopy(series[0]["rows"][0]))
    elif change == "future":
        series[0]["rows"][0]["available_ms"] = 365 * DAY_MS + 1
    elif change == "holdout":
        series[0]["rows"][0]["start_ms"] = 280 * DAY_MS
    elif change == "wrong_identity":
        series[0]["identities"] = series[0]["identities"] | {"risk_hash": "0" * 64}
    elif change == "incomplete":
        series[0]["rows"][0]["complete"] = False
    elif change == "float":
        series[0]["rows"][0]["net_pnl"] = 1.0
    elif change == "unknown":
        series[0]["state"] = "unknown"
    elif change == "constant":
        for row in series[0]["rows"]:
            row["net_pnl"] = "1"
    elif change == "missing_available":
        del series[0]["rows"][0]["available_ms"]
    else:
        inference = FrozenResearchEvidence(inference.canonical_json, "0" * 64)
    result = module.calculate_validation_family(frozen, inference, series).to_dict()
    assert result["calculation_status"] == "not_estimable"
    assert result["reason_codes"]
    assert result["results"] == []
    assert result["source_authenticated"] is False
    assert result["production_authorized"] is False


def test_resource_preflight_rejects_before_producing_partial_results():
    frozen = family()
    spec = policy(frozen) | {"max_index_draws": 10}
    inference = api().freeze_validation_inference(frozen, spec)
    result = api().calculate_validation_family(frozen, inference, inputs(frozen, inference)).to_dict()
    assert result["calculation_status"] == "not_estimable"
    assert "resource_limit_exceeded" in result["reason_codes"]
    assert result["results"] == []


def test_validated_input_snapshot_does_not_follow_caller_mutation():
    module, frozen = api(), family()
    inference = module.freeze_validation_inference(frozen, policy(frozen))
    series = inputs(frozen, inference)
    prepared = module._validated_inputs(frozen, inference, series)
    series[0]["identities"]["code_hash"] = "0" * 64
    series[0]["rows"][0]["net_pnl"] = "123"
    assert prepared["series"][0]["identities"]["code_hash"] == "a" * 64
    assert prepared["series"][0]["rows"][0]["net_pnl"] == "-1"


@pytest.fixture(scope="module")
def computed():
    module, frozen = api(), family()
    inference = module.freeze_validation_inference(frozen, policy(frozen))
    series = inputs(frozen, inference)
    return frozen, inference, series, module.calculate_validation_family(frozen, inference, series)


def test_actual_99999_resample_pipeline_preserves_exact_total_and_shared_dependence(computed):
    frozen, inference, series, result = computed
    value = result.to_dict()
    assert value["calculation_status"] == "computed"
    assert value["family_size"] == 2
    assert value["replicates"] == 99999
    assert value["index_draws"] == 14_999_850
    assert len(value["results"]) == 2
    assert len(value["input_manifest"]) == 2
    assert value["input_manifest"][0]["execution_sha256"] == "4" * 64
    assert value["input_manifest"][0]["attempt_id"] == "attempt-0"
    assert value["results"][0]["raw_p"] == value["results"][1]["raw_p"]
    assert value["results"][0]["net_profit"] == "300.00000000000000000000015"
    assert value["source_authenticated"] is False
    assert value["inference_registration_verified"] is False
    assert value["acceptance_eligible"] is False
    assert value["production_authorized"] is False
    assert value["status"] == "not_estimable"
    assert inference.sha256 == value["inference_sha256"]
    assert frozen.sha256 == value["family_sha256"]
    # 规范输入映射不随调用者的配置排列而变。
    assert api()._validated_inputs(frozen, inference, list(reversed(series)))["input_sha256"] == value["input_sha256"]


def test_repeated_full_calculation_and_config_reordering_have_identical_bytes(computed, caplog):
    frozen, inference, series, result = computed
    with caplog.at_level(logging.INFO, logger="research_statistics"):
        repeated = api().calculate_validation_family(frozen, inference, list(reversed(series)))
    assert repeated == result
    records = [record for record in caplog.records if record.name == "research_statistics"]
    messages = [record.getMessage() for record in records]
    # 一次长计算只产出输入/完成两条摘要，不逐重采样或输出盈利结论。
    assert len(messages) == 2
    assert "校验完成" in messages[0] and "B=99999" in messages[0]
    assert "M=2" in messages[0] and "index_draws=14999850" in messages[0]
    assert "计算完成" in messages[1] and "来源未认证" in messages[1]
    for sensitive in (frozen.sha256, inference.sha256, "9" * 64, "attempt-0",
                      "offline-fixture", "300.00000000000000000000015"):
        assert sensitive not in "\n".join(messages)
    assert all(record.exc_info is None and record.stack_info is None for record in records)


def test_rejection_logs_only_fixed_reason_and_never_malicious_input(caplog):
    module, frozen = api(), family()
    inference = module.freeze_validation_inference(frozen, policy(frozen))
    series = inputs(frozen, inference)
    secret = "https://sensitive.invalid/private?token=secret-log-marker\n伪造日志正文"
    series[0]["producer_id"] = secret
    with caplog.at_level(logging.INFO, logger="research_statistics"):
        result = module.calculate_validation_family(frozen, inference, series).to_dict()
    assert result["reason_codes"] == ["reference_invalid"]
    assert result["results"] == []
    records = [record for record in caplog.records if record.name == "research_statistics"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "不可估计" in message and "reference_invalid" in message
    assert secret not in caplog.text and "secret-log-marker" not in caplog.text
    assert "伪造日志正文" not in caplog.text
    assert frozen.sha256 not in message and inference.sha256 not in message
    assert records[0].exc_info is None and records[0].stack_info is None


def test_resource_rejection_is_logged_before_any_compute_summary(caplog):
    module, frozen = api(), family()
    inference = module.freeze_validation_inference(frozen, policy(frozen) | {"max_index_draws": 10})
    with caplog.at_level(logging.INFO, logger="research_statistics"):
        result = module.calculate_validation_family(frozen, inference, inputs(frozen, inference)).to_dict()
    assert result["reason_codes"] == ["resource_limit_exceeded"]
    messages = [record.getMessage() for record in caplog.records if record.name == "research_statistics"]
    assert len(messages) == 1
    assert "resource_limit_exceeded" in messages[0]
    assert "不可估计" in messages[0]
    assert "计算完成" not in messages[0]


def test_unexpected_compute_failure_is_not_hidden_or_logged_with_raw_error(caplog, monkeypatch):
    module, frozen = api(), family()
    inference = module.freeze_validation_inference(frozen, policy(frozen))
    secret = "unexpected-secret-error-marker"

    def broken_bootstrap(*args):
        raise RuntimeError(secret)

    monkeypatch.setattr(module, "_bootstrap", broken_bootstrap)
    with caplog.at_level(logging.INFO, logger="research_statistics"):
        with pytest.raises(RuntimeError, match=secret):
            module.calculate_validation_family(frozen, inference, inputs(frozen, inference))
    messages = [record.getMessage() for record in caplog.records if record.name == "research_statistics"]
    assert len(messages) == 1 and "校验完成" in messages[0]
    assert "不可估计" not in messages[0] and "计算完成" not in messages[0]
    assert secret not in caplog.text


def test_behavioral_frozen_subclass_cannot_move_validation_into_holdout():
    module, original = api(), family()

    class MovedFold(FrozenResearchEvidence):
        def to_dict(self):
            value = super().to_dict()
            for key in ("validation_start_ms", "validation_end_ms"):
                value["folds"][0][key] += 155 * DAY_MS
            return value

    forged = MovedFold(original.canonical_json, original.sha256)
    with pytest.raises(ValueError, match="family_invalid"):
        module.freeze_validation_inference(forged, policy(original))
    inference = module.freeze_validation_inference(original, policy(original))
    result = module.calculate_validation_family(forged, inference, []).to_dict()
    assert result["reason_codes"] == ["family_invalid"]
    assert result["results"] == []


@pytest.mark.parametrize("location", ["family_json", "family_sha", "inference_json", "inference_sha",
                                      "inference_subclass"])
def test_behavioral_string_and_inference_subclasses_are_not_frozen_proof(location):
    module, original = api(), family()
    inference = module.freeze_validation_inference(original, policy(original))

    class BehavioralString(str):
        def encode(self, *args, **kwargs):
            return super().encode(*args, **kwargs)

    class BehavioralEvidence(FrozenResearchEvidence):
        pass

    frozen, candidate = original, inference
    if location == "family_json":
        frozen = FrozenResearchEvidence(BehavioralString(original.canonical_json), original.sha256)
    elif location == "family_sha":
        frozen = FrozenResearchEvidence(original.canonical_json, BehavioralString(original.sha256))
    elif location == "inference_json":
        candidate = FrozenResearchEvidence(BehavioralString(inference.canonical_json), inference.sha256)
    elif location == "inference_sha":
        candidate = FrozenResearchEvidence(inference.canonical_json, BehavioralString(inference.sha256))
    else:
        candidate = BehavioralEvidence(inference.canonical_json, inference.sha256)
    result = module.calculate_validation_family(frozen, candidate, []).to_dict()
    expected = "family_invalid" if location.startswith("family") else "inference_invalid"
    assert result["reason_codes"] == [expected]


def full_policy(frozen):
    spec = policy(frozen)
    del spec["max_weighted_additions"], spec["max_distribution_cells"]
    return spec | {
        "engine": "exact-signed54-counts-v1", "batch_size": 1024,
        "limb_bits": 54, "max_limbs": 3, "max_blocks": 300,
        "max_input_rows": 2_000_000, "max_index_draws": 30_000_000,
        "max_native_macs": 10_799_892_000, "max_reconstruction_cells": 35_999_640,
        "max_heap_cells": 5000, "max_workspace_bytes": 64_000_000,
    }


def larger_family():
    frozen = family().to_dict()
    source = {key: frozen[key] for key in ("family_id", "registered_ms", "dataset",
              "label_span_ms", "purge_ms", "embargo_ms", "comparison",
              "identities", "execution_policy", "label_horizons")}
    source.update(frozen["axes"])
    source["factor_sets"] = [{"id": f"factor-{n}", "sha256": f"{n:064x}"} for n in range(60)]
    return freeze_experiment_family(source)


def test_new_engine_requires_explicit_versioned_resource_policy_and_preserves_v1():
    module, frozen = api(), family()
    old = module.freeze_validation_inference(frozen, policy(frozen))
    new = module.freeze_validation_inference(frozen, full_policy(frozen))
    assert old.to_dict()["protocol_version"] == "validation-block-bootstrap-v1"
    assert "engine" not in old.to_dict()
    assert new.to_dict()["protocol_version"] == "validation-block-bootstrap-v2"
    assert new.to_dict()["engine"] == "exact-signed54-counts-v1"
    assert old.sha256 != new.sha256
    assert module._checked_inference(frozen, new)["max_native_macs"] == 10_799_892_000


@pytest.mark.parametrize(("field", "value"), [("batch_size", True), ("batch_size", 1025),
    ("limb_bits", 53), ("max_limbs", 4), ("max_blocks", 301),
    ("max_native_macs", 10_799_892_001), ("max_workspace_bytes", 0)])
def test_new_engine_resource_fields_are_bounded_before_any_numpy_allocation(field, value):
    frozen = family()
    with pytest.raises(ValueError):
        api().freeze_validation_inference(frozen, full_policy(frozen) | {field: value})


def test_signed54_products_match_hand_enumeration_with_negative_high_limbs():
    module = api()
    huge = (1 << 140) + (1 << 60) + 3
    blocks = [((huge, -huge), (7,)), ((-2 * huge, 2 * huge), (-14,))]
    # 四个等概率draw: (0,0)/(0,1)/(1,0)/(1,1)，第二fold唯一块。
    counts = [[2, 0, 1], [1, 1, 1], [1, 1, 1], [0, 2, 1]]
    digits = module._signed54_digits(blocks, 3)
    products = module._signed54_product(counts, digits)
    observed = [[sum(int(products[limb, row, config]) << (54 * limb) for limb in range(3))
                 - sum(map(sum, blocks[config])) for row in range(4)] for config in range(2)]
    assert observed == [[2 * huge, 0, 0, -2 * huge], [-4 * huge, 0, 0, 4 * huge]]


def test_signed54_intermediate_bound_is_proved_at_s300_even_with_cancellation():
    module = api()
    peak = (1 << 54) - 1
    blocks = [((peak,) * 300,), ((-peak,) * 300,), ((peak, -peak) * 150,)]
    digits = module._signed54_digits(blocks, 1)
    result = module._signed54_product([[1] * 300], digits)
    assert [int(value) for value in result[0, 0]] == [300 * peak, -300 * peak, 0]
    with pytest.raises(ValueError, match="overflow"):
        module._signed54_product([[1] * 513], module._signed54_digits([((peak,) * 513,)], 1))


def test_streaming_rank_heap_matches_complete_sort_with_ties_and_negative_values():
    module = api()
    for values, total, rank in [([-9, -9, -5, -1], -5, 1),
                                ([1, 2, 2, 2, 3], 2, 3), ([0] * 41 + [1] * 41, 1, 41)]:
        heap = []
        tail = module._stream_tail(values, total, rank, heap)
        assert heap[0] == sorted(values)[-rank]
        assert tail == sum(value >= total for value in values)


@pytest.mark.parametrize("budget", ["max_native_macs", "max_reconstruction_cells",
                                   "max_heap_cells", "max_workspace_bytes", "max_blocks"])
def test_new_engine_budgets_refuse_whole_family_before_kernel(budget):
    module, frozen = api(), family()
    spec = full_policy(frozen) | {budget: 1}
    inference = module.freeze_validation_inference(frozen, spec)
    result = module.calculate_validation_family(frozen, inference, inputs(frozen, inference)).to_dict()
    assert result["calculation_status"] == "not_estimable"
    assert result["reason_codes"] == ["resource_limit_exceeded"]
    assert result["results"] == []


def test_new_engine_invalid_last_configuration_refuses_before_kernel(monkeypatch):
    module, frozen = api(), larger_family()
    inference = module.freeze_validation_inference(frozen, full_policy(frozen))
    series = inputs(frozen, inference)
    series[-1]["rows"].pop()
    def forbidden(*args):
        pytest.fail("invalid complete family reached new kernel")
    monkeypatch.setattr(module, "_bootstrap_counts", forbidden)
    result = module.calculate_validation_family(frozen, inference, series).to_dict()
    assert result["reason_codes"] == ["series_grid_incomplete"]
    assert result["results"] == []


def test_same_domain_batch_counts_keep_original_index_stream_and_exact_full_m_oracle():
    module = api()
    import numpy as np
    import struct
    counts, domain = [2, 1], b"same-domain-oracle"
    block = ((10**42 + 7, -10**42 - 3), (1 << 127,))
    blocks = [tuple(tuple(value * (config + 1) for value in fold) for fold in block)
              for config in range(120)]
    digits = module._signed54_digits(blocks, 3)
    expected, expected_indexes = [], bytearray()
    for replicate in range(17):
        selected = []
        for fold, count in enumerate(counts):
            chosen = []
            for draw in range(count):
                index = module._draw_index(domain, replicate, fold, draw, count)[0]
                expected_indexes.extend(struct.pack(">I", index))
                chosen.append(index)
            selected.append(chosen)
        expected.append([sum(sum(values[index] for index in indexes) - sum(values)
                             for values, indexes in zip(config, selected, strict=True))
                         for config in blocks])
    for batch in (1, 3, 17):
        fingerprint = hashlib.sha256()
        found = []
        for start in range(0, 17, batch):
            matrix, _ = module._count_batch(domain, start, min(17, start + batch), counts, fingerprint)
            products = module._signed54_product(matrix, digits)
            for row in range(len(matrix)):
                found.append([sum(int(products[limb, row, config]) << (54 * limb)
                                  for limb in range(len(products))) - sum(map(sum, blocks[config]))
                              for config in range(120)])
            assert np.all(matrix[:, :2].sum(axis=1) == 2)
            assert np.all(matrix[:, 2:].sum(axis=1) == 1)
        assert found == expected
        assert fingerprint.hexdigest() == hashlib.sha256(expected_indexes).hexdigest()


def test_s300_extreme_delta_can_exceed_int64_after_safe_nonnegative_count_product():
    module = api()
    peak = (1 << 54) - 1
    blocks = [tuple((peak,) + (-peak,) * 59 for _ in range(5))]
    matrix = [[60 if index % 60 == 0 else 0 for index in range(300)]]
    products = module._signed54_product(matrix, module._signed54_digits(blocks, 1))
    assert int(products[0, 0, 0]) == 300 * peak
    delta = int(products[0, 0, 0]) - sum(map(sum, blocks[0]))
    assert delta == 590 * peak
    assert delta > (1 << 63) - 1


@pytest.mark.parametrize("invalid", [[[1.5, 1]], [[True, 1]], [[-1, 2]]])
def test_exact_matrix_product_refuses_lossy_or_negative_counts(invalid):
    module = api()
    with pytest.raises(ValueError):
        module._signed54_product(invalid, module._signed54_digits([((1, -1),)], 1))


def test_exact_matrix_product_proves_ndarray_counts_before_int64_sum_wrap():
    import numpy as np
    module = api()
    malicious = np.array([[2**62, 2**62, 2**62, 2**62, 1]], dtype=np.int64)
    with pytest.raises(ValueError, match="block_counts_invalid"):
        module._signed54_product(malicious, module._signed54_digits([((1, -1, 1, -1, 1),)], 1))


def test_actual_full_120_configuration_pipeline_has_bounded_logs_and_false_qualifications(caplog):
    import resource
    import time
    module, frozen = api(), larger_family()
    inference = module.freeze_validation_inference(frozen, full_policy(frozen))
    series = inputs(frozen, inference)
    start = time.perf_counter()
    with caplog.at_level(logging.INFO, logger="research_statistics"):
        evidence = module.calculate_validation_family(frozen, inference, series)
    elapsed = time.perf_counter() - start
    result = evidence.to_dict()
    assert result["calculation_status"] == "computed"
    assert result["protocol_version"] == "validation-block-bootstrap-v2"
    assert result["compute_engine"] == "exact-signed54-counts-v1"
    assert result["replicates"] == 99999 and result["family_size"] == 120
    assert len(result["results"]) == len(result["input_manifest"]) == 120
    assert {row["config_id"] for row in result["results"]} == {row["config_id"] for row in frozen.to_dict()["configurations"]}
    assert result["compute_resources"]["heap_cells"] == 4920
    assert result["compute_resources"]["native_macs"] == 3_599_964_000
    assert result["compute_resources"]["limbs"] == 2
    assert all(row["net_profit"] == "300.00000000000000000000015" for row in result["results"])
    assert len({tuple(row["raw_p"].values()) for row in result["results"]}) == 1
    assert result["status"] == "not_estimable"
    for flag in ("source_authenticated", "inference_registration_verified", "acceptance_eligible", "production_authorized"):
        assert result[flag] is False
    messages = [record.getMessage() for record in caplog.records if record.name == "research_statistics"]
    assert len(messages) == 2 and "M=120" in messages[0]
    for secret in (frozen.sha256, inference.sha256, "9" * 64, "attempt-0", "300.00000000000000000000015"):
        assert secret not in "\n".join(messages)
    # 只给临时验证日志输出固定量测；不写运行时产物、不作为统计证据字段。
    print(json.dumps({"full_family_acceptance": True, "M": 120, "B": 99999,
                      "elapsed_seconds": elapsed, "max_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      "resample_digest": result["resample_digest"], "evidence_sha256": evidence.sha256,
                      "all_120_results": True, "all_qualification_flags_false": True}, sort_keys=True))
