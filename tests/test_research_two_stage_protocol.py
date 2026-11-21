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


def test_validation_plan_freezes_complete_attempt_grid_without_granting_authority():
    family = freeze_experiment_family(family_spec())
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
    rows = [
        {
            "config_id": "base-b",
            "stress_config_id": "stress-b",
            "adjusted_p": Fraction(1, 100),
            "lower_bound": Fraction(3, 10),
            "stress_adjusted_p": Fraction(1, 100),
            "stress_lower_bound": Fraction(3, 10),
            "execution_gates_passed": True,
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
    assert select_validation_pair(rows, policy)["config_id"] == "base-a"


def test_holdout_claim_never_issues_an_execution_ticket():
    family = freeze_experiment_family(family_spec())
    plan = freeze_validation_plan(
        family,
        _evidence(family),
        attempt_mappings=_attempts(family),
        selection_policy={"version": "pair-select-v1", "alpha": "0.05"},
        holdout_policy={"version": "holdout-pair-bootstrap-v1", "replicates": 99999},
    )
    assert claim_holdout_v2(plan, selection={"status": "selected"}) == {
        "execution_ticket": False,
        "status": "not_estimable",
        "reason_codes": ["holdout_calculator_unavailable"],
    }
