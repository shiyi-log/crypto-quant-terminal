"""The new acceptance policy refuses extra leverage or unknown margin protection."""
import importlib
import importlib.util

import pytest


BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"


@pytest.fixture
def policy():
    return (importlib.import_module("demo_account_policy")
            if importlib.util.find_spec("demo_account_policy") else None)


def execution(**changes):
    return {"execution_universe": [BTC, ETH], "leverage": 10, **changes}


def config(pair, **changes):
    return {"pair": pair, "leverage": 10, "margin_type": "ISOLATED",
            "is_auto_add_margin": False, **changes}


def verify(policy, *, frozen=None, mode=None, actual=None):
    assert policy is not None, "acceptance-round account-policy validation is missing"
    return policy.validate_demo_account_policy(
        execution() if frozen is None else frozen,
        {"hedged": False} if mode is None else mode,
        [config(BTC), config(ETH)] if actual is None else actual,
    )


def denied(policy, code, **values):
    assert policy is not None, "acceptance-round account-policy validation is missing"
    with pytest.raises(policy.DemoAccountPolicyError) as refusal:
        verify(policy, **values)
    assert refusal.value.code == code


def test_accepts_boundary_leverage_and_returns_only_verified_identity(policy):
    assert verify(policy) == {"policy": "binance-demo-account-10x-v1",
                              "checked_pairs": [BTC, ETH], "max_leverage": 10}


@pytest.mark.parametrize("value", [11, 100, 0, -1, True, None, "NaN", "10.5"])
def test_frozen_leverage_cannot_bypass_limit_or_validation(policy, value):
    denied(policy, "demo_frozen_leverage_invalid", frozen=execution(leverage=value))


def test_per_symbol_override_cannot_bypass_frozen_limit(policy):
    denied(policy, "demo_frozen_leverage_invalid",
           frozen=execution(leverage_by_symbol={BTC: 10, ETH: 11}))


@pytest.mark.parametrize("mode", [{"hedged": True}, {}, {"hedged": "false"}])
def test_unknown_or_hedged_account_is_not_one_way_evidence(policy, mode):
    denied(policy, "demo_one_way_mode_required", mode=mode)


@pytest.mark.parametrize("value", [11, 100, 0, True, None, "NaN"])
def test_actual_leverage_is_checked_independently_from_frozen_strategy(policy, value):
    denied(policy, "demo_actual_leverage_invalid",
           actual=[config(BTC, leverage=value), config(ETH)])


@pytest.mark.parametrize("value", ["CROSSED", None, "isolated"])
def test_only_verified_adapter_isolated_margin_is_accepted(policy, value):
    denied(policy, "demo_isolated_margin_required",
           actual=[config(BTC, margin_type=value), config(ETH)])


@pytest.mark.parametrize("value", [True, None, "false", 0])
def test_automatic_margin_addition_must_be_explicitly_disabled(policy, value):
    denied(policy, "demo_auto_add_margin_not_disabled",
           actual=[config(BTC, is_auto_add_margin=value), config(ETH)])


@pytest.mark.parametrize("actual", [[], [config(BTC)], [config(BTC), config(BTC), config(ETH)]])
def test_missing_or_ambiguous_symbol_configuration_is_rejected(policy, actual):
    denied(policy, "demo_symbol_configuration_incomplete", actual=actual)


@pytest.mark.parametrize("pairs", [[], [BTC, BTC], ["DOGE/USDT:USDT"]])
def test_execution_universe_must_be_complete_distinct_and_in_first_batch(policy, pairs):
    denied(policy, "demo_execution_universe_invalid",
           frozen=execution(execution_universe=pairs))
