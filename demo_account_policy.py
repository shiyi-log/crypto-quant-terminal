"""Verified account configuration for the 1000 USDT Demo acceptance policy.

Inputs are the frozen execution definition and normalized, signed snapshots
from BinanceDemoAdapter. This validator never changes an exchange setting;
unknown facts deny entries. Legacy paper experiment identities are read-only.
"""
from __future__ import annotations

import re


POLICY = "binance-demo-account-10x-v1"
ALLOWED_PAIRS = {"BTC/USDT:USDT", "ETH/USDT:USDT", "TRX/USDT:USDT"}


class DemoAccountPolicyError(ValueError):
    """Public fixed-code denial without connection or credential details."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _leverage(value, code):
    if (isinstance(value, bool) or not isinstance(value, (int, str))
            or not re.fullmatch(r"[0-9]+", str(value))
            or not 1 <= int(value) <= 10):
        raise DemoAccountPolicyError(code)


def validate_demo_account_policy(execution, position_mode, symbol_config):
    """Return a verified policy receipt or refuse new acceptance entries.

    Snapshot freshness belongs to the caller. Validation is required before
    first entry, resume, and after refreshed configuration becomes available.
    Refusal must not prevent protection or reduce-only recovery of owned risk.
    """
    pairs = execution.get("execution_universe") if isinstance(execution, dict) else None
    if (not isinstance(pairs, (list, tuple)) or not pairs
            or any(not isinstance(pair, str) or pair not in ALLOWED_PAIRS for pair in pairs)
            or len(set(pairs)) != len(pairs)):
        raise DemoAccountPolicyError("demo_execution_universe_invalid")
    _leverage(execution.get("leverage"), "demo_frozen_leverage_invalid")
    overrides = execution.get("leverage_by_symbol")
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise DemoAccountPolicyError("demo_frozen_leverage_invalid")
        for leverage in overrides.values():
            _leverage(leverage, "demo_frozen_leverage_invalid")
    if not isinstance(position_mode, dict) or position_mode.get("hedged") is not False:
        raise DemoAccountPolicyError("demo_one_way_mode_required")
    if not isinstance(symbol_config, list):
        raise DemoAccountPolicyError("demo_symbol_configuration_incomplete")
    checked = {}
    for row in symbol_config:
        if not isinstance(row, dict):
            raise DemoAccountPolicyError("demo_symbol_configuration_incomplete")
        pair = row.get("pair")
        if pair not in pairs:
            continue
        if pair in checked:
            raise DemoAccountPolicyError("demo_symbol_configuration_incomplete")
        checked[pair] = row
    if set(checked) != set(pairs):
        raise DemoAccountPolicyError("demo_symbol_configuration_incomplete")
    for pair in pairs:
        row = checked[pair]
        _leverage(row.get("leverage"), "demo_actual_leverage_invalid")
        if row.get("margin_type") != "ISOLATED":
            raise DemoAccountPolicyError("demo_isolated_margin_required")
        if row.get("is_auto_add_margin") is not False:
            raise DemoAccountPolicyError("demo_auto_add_margin_not_disabled")
    return {"policy": POLICY, "checked_pairs": list(pairs), "max_leverage": 10}
