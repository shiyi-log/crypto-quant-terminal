from fractions import Fraction

import pytest

from research_protocol import freeze_experiment_family
from research_two_stage import (
    claim_holdout_v2,
    freeze_validation_plan,
    select_validation_pair,
)
from tests.test_research_protocol import family_spec


def _evidence(family):
    return {
        "protocol_version": "validation-block-bootstrap-v2",
        "family_sha256": family.sha256,
        "inference_sha256": "1" * 64,
        "identities": {key: char * 64 for key, char in zip(
            ("code_hash", "data_hash", "cost_hash", "risk_hash", "strategy_hash"),
            "abcde",
        )},
        "selection_policy_sha256": "2" * 64,
        "holdout_policy_sha256": "3" * 64,
        "seed": 7,
    }


def _attempts(family):
    return [
        {
            "config_id": row["config_id"],
            "attempt_id": f"attempt-{index}",
            "producer_id": "worker-1",
            "inference_sha256": "1" * 64,
            "stage": "validation",
        }
        for index, row in enumerate(family.to_dict()["configurations"])
    ]


def test_validation_plan_logs_phase_duration_and_non_authorizing_state(caplog):
    family = freeze_experiment_family(family_spec())
    with caplog.at_level("INFO", logger="research_two_stage"):
        plan = freeze_validation_plan(
            family,
            _evidence(family),
            attempt_mappings=_attempts(family),
            selection_policy={"version": "pair-select-v1", "alpha": "0.05"},
            holdout_policy={"version": "holdout-pair-bootstrap-v1", "replicates": 99999},
        )
    value = plan.to_dict()
    assert value["validation"]["configuration_count"] == 12
    assert value["validation"]["registration_order_verified"] is False
    assert value["holdout"]["calculator_available"] is False
    assert value["acceptance_eligible"] is False
    assert value["production_authorized"] is False
    baseline = plan.canonical_json
    value["validation"]["attempts"].clear()
    assert plan.canonical_json == baseline
    assert "研究验证计划已冻结" in caplog.text
    assert "阶段=validation" in caplog.text
    assert "配置数=12" in caplog.text
    assert "耗时_ms=" in caplog.text
    assert "执行票据=false" in caplog.text


def test_validation_plan_rejects_missing_or_misbound_attempts():
    family = freeze_experiment_family(family_spec())
    attempts = _attempts(family)
    attempts[-1]["inference_sha256"] = "9" * 64
    with pytest.raises(ValueError):
        freeze_validation_plan(
            family,
            _evidence(family),
            attempt_mappings=attempts,
            selection_policy={"version": "pair-select-v1", "alpha": "0.05"},
            holdout_policy={"version": "holdout-pair-bootstrap-v1", "replicates": 99999},
        )


def test_pair_selection_uses_exact_thresholds_and_stable_tie_breaking():
    policy = {"version": "pair-select-v1", "alpha": "0.05"}
    equal_to_threshold = {
        "config_id": "base-equal",
        "stress_config_id": "stress-equal",
        "adjusted_p": Fraction(1, 20),
        "lower_bound": Fraction(1, 10),
        "stress_adjusted_p": Fraction(1, 20),
        "stress_lower_bound": Fraction(1, 10),
        "execution_gates_passed": True,
    }
    above_threshold = dict(
        equal_to_threshold, config_id="base-over", adjusted_p=Fraction(501, 10000)
    )
    assert select_validation_pair([equal_to_threshold], policy) == equal_to_threshold
    assert select_validation_pair([above_threshold], policy) is None

    tied = [
        dict(equal_to_threshold, config_id="base-b", stress_config_id="stress-b"),
        dict(equal_to_threshold, config_id="base-a", stress_config_id="stress-a"),
    ]
    assert select_validation_pair(tied, policy)["config_id"] == "base-a"


def test_pair_selection_logs_counts_and_rejection_reason_codes(caplog):
    policy = {"version": "pair-select-v1", "alpha": "0.05"}
    rows = [
        {
            "config_id": "base-b",
            "stress_config_id": "stress-b",
            "adjusted_p": Fraction(1, 20),
            "lower_bound": Fraction(3, 10),
            "stress_adjusted_p": Fraction(1, 100),
            "stress_lower_bound": Fraction(3, 10),
            "execution_gates_passed": True,
        },
        {
            "config_id": "base-rejected",
            "stress_config_id": "stress-rejected",
            "adjusted_p": Fraction(1, 10),
            "lower_bound": Fraction(1, 10),
            "stress_adjusted_p": Fraction(1, 100),
            "stress_lower_bound": Fraction(1, 10),
            "execution_gates_passed": True,
            "api_secret": "DO_NOT_LOG_THIS_FIELD",
        },
        {
            "config_id": "base-a",
            "stress_config_id": "stress-a",
            "adjusted_p": Fraction(1, 100),
            "lower_bound": Fraction(3, 10),
            "stress_adjusted_p": Fraction(1, 100),
            "stress_lower_bound": Fraction(3, 10),
            "execution_gates_passed": True,
        },
    ]
    with caplog.at_level("INFO", logger="research_two_stage"):
        assert select_validation_pair(rows, policy)["config_id"] == "base-a"
        assert select_validation_pair(
            [dict(rows[0], config_id="base-blocked", execution_gates_passed=False)], policy
        ) is None
        assert select_validation_pair(
            [dict(rows[0], config_id="base-zero-bound", lower_bound=Fraction(0))], policy
        ) is None
    assert "研究候选筛选完成" in caplog.text
    assert "阶段=validation" in caplog.text
    assert "输入数=3" in caplog.text
    assert "合格数=2" in caplog.text
    assert '拒绝原因码统计={"adjusted_probability_exceeds_alpha":1}' in caplog.text
    assert "耗时_ms=" in caplog.text
    assert "DO_NOT_LOG_THIS_FIELD" not in caplog.text


def test_holdout_claim_logs_refusal_duration_and_never_issues_ticket(caplog):
    family = freeze_experiment_family(family_spec())
    plan = freeze_validation_plan(
        family,
        _evidence(family),
        attempt_mappings=_attempts(family),
        selection_policy={"version": "pair-select-v1", "alpha": "0.05"},
        holdout_policy={"version": "holdout-pair-bootstrap-v1", "replicates": 99999},
    )
    with caplog.at_level("INFO", logger="research_two_stage"):
        assert claim_holdout_v2(plan, selection={"status": "selected"}) == {
            "execution_ticket": False,
            "status": "not_estimable",
            "reason_codes": ["holdout_calculator_unavailable"],
        }
    assert "研究留出请求已拒绝" in caplog.text
    assert "阶段=final_holdout" in caplog.text
    assert "原因码=holdout_calculator_unavailable" in caplog.text
    assert "耗时_ms=" in caplog.text
    assert "持久消费=未接线" in caplog.text
    assert "执行票据=false" in caplog.text


def test_holdout_claim_rejects_unselected_fact_before_consumption():
    family = freeze_experiment_family(family_spec())
    plan = freeze_validation_plan(
        family,
        _evidence(family),
        attempt_mappings=_attempts(family),
        selection_policy={"version": "pair-select-v1", "alpha": "0.05"},
        holdout_policy={"version": "holdout-pair-bootstrap-v1", "replicates": 99999},
    )
    with pytest.raises(ValueError, match="候选必须已选中"):
        claim_holdout_v2(plan, selection={"status": "pending"})


def test_holdout_claim_rejects_misbound_identity_before_consumption():
    family = freeze_experiment_family(family_spec())
    plan = freeze_validation_plan(
        family,
        _evidence(family),
        attempt_mappings=_attempts(family),
        selection_policy={"version": "pair-select-v1", "alpha": "0.05"},
        holdout_policy={"version": "holdout-pair-bootstrap-v1", "replicates": 99999},
    )
    with pytest.raises(ValueError, match="候选实验族哈希不匹配"):
        claim_holdout_v2(
            plan,
            selection={
                "status": "selected",
                "family_sha256": "f" * 64,
            },
        )
