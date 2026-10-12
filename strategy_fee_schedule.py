"""Versioned paper fee assumptions, grounded in Binance Demo evidence.

The Demo account quotes both rates. Only the taker rate has been checked
against actual Demo fills; a maker quote is not maker-fill verification.
Existing paper definitions without a fee-model version retain their original
execution assumptions and never inherit this newer schedule automatically.
"""

DEMO_FEE_MODEL_VERSION = "binance-demo-commission-20261012-v1"
LEGACY_FEE_MODEL_VERSION = "legacy-paper-fees-v1"
MAKER_FEE_PER_SIDE = 0.0002
TAKER_FEE_PER_SIDE = 0.0004
LEGACY_TAKER_FEE_PER_SIDE = 0.0005
FEE_BASIS = (
    "binance_demo_20261012:maker_0.0002_commissionRate_quote_requires_actual_fills;"
    "taker_0.0004_verified_commission_over_notional_USDT_actual_user_trades_"
    "manual_smoke_20261012_035400"
)
