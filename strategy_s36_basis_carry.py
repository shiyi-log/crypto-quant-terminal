"""S36 15m 资金费/基差 carry 的 paper 规则。

本模块与历史 Fnew 身份独立，只产生 paper 意图和会计事实，不会向交易所下单。
信号时钟是已完成的 15m K 线，成交使用下一根 K 线开盘价，并显式加入 1 tick
不利成交调整和 1000ms 有界执行窗口。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
from typing import Any, Iterable, Mapping


STRATEGY_ID = "s36_basis_carry_15m_v1"
TIMEFRAME = "15m"
BAR_MS = 900_000
FUNDING_INTERVAL_MS = 28_800_000
FUNDING_SETTLEMENT_HOURS = 8
PAIR_NOTIONAL_CAP = Decimal("25000")
REQUESTED_PAIR_NOTIONAL = Decimal("25000")
N_MAX_GROSS = Decimal("50000")
INITIAL_EQUITY = Decimal("10000")
PERP_LEVERAGE = Decimal("5")
RESERVE_RATIO = Decimal("0.10")
FUNDING_RATE_MIN = Decimal("0.00020")
HOLD_SETTLEMENTS = 8
EXECUTION_WINDOW_MS = 1000
MAKER_ROUND_TRIP_BPS_PER_LEG = Decimal("6")
TAKER_ROUND_TRIP_BPS_PER_LEG = Decimal("8")
TICK_SIZE = {
    "BTC/USDT": Decimal("0.1"),
    "ETH/USDT": Decimal("0.01"),
    "TRX/USDT": Decimal("0.0001"),
}
LOT_SIZE = {
    "BTC/USDT": Decimal("0.001"),
    "ETH/USDT": Decimal("0.001"),
    "TRX/USDT": Decimal("1"),
}
PERP_TO_SPOT = {
    "BTC/USDT:USDT": "BTC/USDT",
    "ETH/USDT:USDT": "ETH/USDT",
    "TRX/USDT:USDT": "TRX/USDT",
}
UNIVERSE = tuple(PERP_TO_SPOT)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                  allow_nan=False, default=str).encode("utf-8")
    ).hexdigest()


def _decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("non-finite decimal")
    return result


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _iso_ms(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, tz=UTC).isoformat()


@dataclass(frozen=True)
class CarryConfig:
    initial_equity: Decimal = INITIAL_EQUITY
    requested_pair_notional: Decimal = REQUESTED_PAIR_NOTIONAL
    perp_leverage: Decimal = PERP_LEVERAGE
    reserve_ratio: Decimal = RESERVE_RATIO
    funding_rate_min: Decimal = FUNDING_RATE_MIN
    hold_settlements: int = HOLD_SETTLEMENTS
    execution_window_ms: int = EXECUTION_WINDOW_MS
    universe: tuple[str, ...] = UNIVERSE
    version: str = "s36-spec-v1"

    def __post_init__(self) -> None:
        if self.initial_equity <= 0 or self.requested_pair_notional <= 0:
            raise ValueError("capital and notional must be positive")
        if not Decimal("1") <= self.perp_leverage <= Decimal("5"):
            raise ValueError("perp leverage must be in [1,5]")
        if not Decimal("0") <= self.reserve_ratio < Decimal("1"):
            raise ValueError("reserve ratio must be in [0,1)")
        if self.funding_rate_min <= 0 or self.hold_settlements < 1:
            raise ValueError("funding threshold and hold settlements must be positive")
        if self.execution_window_ms != 1000:
            raise ValueError("s36 freezes a 1000ms execution window")
        if tuple(self.universe) != UNIVERSE:
            raise ValueError("s36 freezes the BTC/ETH/TRX universe")

    @property
    def executable_pair_notional(self) -> Decimal:
        """按现货现金、永续保证金和储备金约束缩放请求名义。"""
        capital_limited = self.initial_equity * (Decimal("1") - self.reserve_ratio)
        capital_limited /= Decimal("1") + Decimal("1") / self.perp_leverage
        return min(self.requested_pair_notional, capital_limited, PAIR_NOTIONAL_CAP)

    @property
    def gross_notional(self) -> Decimal:
        return self.executable_pair_notional * Decimal("2")

    @property
    def rule_hash(self) -> str:
        return _hash(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": STRATEGY_ID,
            "timeframe": TIMEFRAME,
            "initial_equity": str(self.initial_equity),
            "requested_pair_notional": str(self.requested_pair_notional),
            "executable_pair_notional": str(self.executable_pair_notional),
            "gross_notional": str(self.gross_notional),
            "n_max_gross": str(N_MAX_GROSS),
            "single_coin_cap": str(PAIR_NOTIONAL_CAP),
            "perp_leverage": str(self.perp_leverage),
            "reserve_ratio": str(self.reserve_ratio),
            "funding_rate_min": str(self.funding_rate_min),
            "hold_settlements": self.hold_settlements,
            "execution_window_ms": self.execution_window_ms,
            "execution": "taker_plus_1_tick",
            "margin": "isolated_no_auto_top_up",
            "universe": list(self.universe),
            "version": self.version,
        }


@dataclass(frozen=True)
class FundingSettlement:
    timestamp: int
    pair: str
    rate: Decimal
    source: str = "binance.series_points.actual_settlement"
    mark_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.timestamp % FUNDING_INTERVAL_MS != 0:
            raise ValueError("funding settlement must be aligned to UTC 8h boundary")
        if self.pair not in UNIVERSE or not self.rate.is_finite():
            raise ValueError("invalid funding settlement")
        if self.mark_price is not None and (not self.mark_price.is_finite() or self.mark_price <= 0):
            raise ValueError("invalid settlement mark price")

    def as_dict(self) -> dict[str, Any]:
        return {"timestamp": self.timestamp, "timestamp_utc": _iso_ms(self.timestamp),
                "pair": self.pair, "rate": str(self.rate), "source": self.source,
                "mark_price": str(self.mark_price) if self.mark_price is not None else None}


@dataclass(frozen=True)
class PaperIntent:
    event_id: str
    pair: str
    signal_timestamp: int
    fill_timestamp: int
    funding_rate: Decimal
    basis: Decimal
    pair_notional: Decimal
    spot_qty: Decimal
    perp_qty: Decimal
    evidence_status: str

    def as_dict(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "pair": self.pair,
                "signal_timestamp": self.signal_timestamp,
                "fill_timestamp": self.fill_timestamp,
                "funding_rate": str(self.funding_rate), "basis": str(self.basis),
                "pair_notional": str(self.pair_notional), "spot_qty": str(self.spot_qty),
                "perp_qty": str(self.perp_qty), "evidence_status": self.evidence_status}


@dataclass
class CarryPosition:
    intent: PaperIntent
    spot_entry: Decimal
    perp_entry: Decimal
    entry_timestamp: int
    funding_events: list[dict[str, Any]] = field(default_factory=list)
    spot_margin: Decimal = Decimal("0")
    perp_margin: Decimal = Decimal("0")

    def basis_pnl(self, spot_exit: Decimal, perp_exit: Decimal) -> Decimal:
        """按 base 数量计算双腿价格损益，并作为 basis PnL 单列。"""
        return self.intent.spot_qty * ((spot_exit - self.spot_entry) -
                                       (perp_exit - self.perp_entry))

    def funding_pnl(self) -> Decimal:
        return sum((_decimal(item["cashflow"]) for item in self.funding_events), Decimal("0"))

    def as_dict(self) -> dict[str, Any]:
        return {"event_id": self.intent.event_id, "pair": self.intent.pair,
                "entry_timestamp": self.entry_timestamp,
                "spot_entry": str(self.spot_entry), "perp_entry": str(self.perp_entry),
                "spot_qty": str(self.intent.spot_qty), "perp_qty": str(self.intent.perp_qty),
                "spot_margin": str(self.spot_margin), "perp_margin": str(self.perp_margin),
                "funding_events": list(self.funding_events)}


def latest_visible_funding(
    settlements: Iterable[FundingSettlement], pair: str, timestamp: int,
) -> FundingSettlement | None:
    visible = [row for row in settlements if row.pair == pair and row.timestamp <= timestamp]
    return max(visible, key=lambda row: row.timestamp) if visible else None


def funding_between(
    settlements: Iterable[FundingSettlement], pair: str, start_exclusive: int,
    end_inclusive: int,
) -> list[FundingSettlement]:
    rows = [row for row in settlements if row.pair == pair and
            start_exclusive < row.timestamp <= end_inclusive]
    rows.sort(key=lambda row: row.timestamp)
    seen: set[int] = set()
    for row in rows:
        if row.timestamp in seen:
            raise ValueError("duplicate funding settlement")
        seen.add(row.timestamp)
    return rows


def make_intent(
    pair: str, signal_bar: Mapping[str, Any], next_bar: Mapping[str, Any],
    previous_bar: Mapping[str, Any], funding: FundingSettlement,
    pair_notional: Decimal, *, execution_evidence: str = "unknown",
    funding_rate_min: Decimal = FUNDING_RATE_MIN,
) -> PaperIntent:
    if pair not in PERP_TO_SPOT:
        raise ValueError("unsupported s36 pair")
    spot_close = _decimal(signal_bar["spot_close"])
    perp_close = _decimal(signal_bar["perp_close"])
    prior_spot = _decimal(previous_bar["spot_close"])
    prior_perp = _decimal(previous_bar["perp_close"])
    if min(spot_close, perp_close, prior_spot, prior_perp) <= 0:
        raise ValueError("prices must be positive")
    if funding.rate < funding_rate_min:
        raise ValueError("funding threshold not met")
    # 按 base 数量匹配；lot size 截断后重新得到实际名义，不能静默当成目标名义。
    raw_qty = pair_notional / max(spot_close, perp_close)
    qty = _floor_step(raw_qty, LOT_SIZE[PERP_TO_SPOT[pair]])
    if qty <= 0:
        raise ValueError("pair notional is below the lot-size minimum")
    basis = prior_perp / prior_spot - Decimal("1")
    event_id = _hash({"strategy_id": STRATEGY_ID, "pair": pair,
                      "signal_timestamp": int(signal_bar["timestamp"]),
                      "fill_timestamp": int(next_bar["timestamp"]),
                      "funding_timestamp": funding.timestamp})[:24]
    return PaperIntent(
        event_id=event_id, pair=pair,
        signal_timestamp=int(signal_bar["timestamp"]),
        fill_timestamp=int(next_bar["timestamp"]), funding_rate=funding.rate,
        basis=basis, pair_notional=pair_notional, spot_qty=qty, perp_qty=qty,
        evidence_status=execution_evidence,
    )


def execution_prices(
    spot_open: Decimal, perp_open: Decimal, pair: str,
    *, phase: str = "entry",
) -> tuple[Decimal, Decimal]:
    """按双腿方向加入 1 tick 的保守成交调整。"""
    tick = TICK_SIZE[PERP_TO_SPOT[pair]]
    if phase == "entry":
        # 现货多腿买入上浮，永续空腿卖出下调。
        return spot_open + tick, perp_open - tick
    if phase == "exit":
        # 现货多腿卖出下调，永续空腿买入上浮。
        return spot_open - tick, perp_open + tick
    raise ValueError("phase must be entry or exit")


def leg_cost_bps(*, role: str) -> Decimal:
    if role == "hybrid":
        return MAKER_ROUND_TRIP_BPS_PER_LEG
    if role == "taker":
        return TAKER_ROUND_TRIP_BPS_PER_LEG
    raise ValueError("unsupported execution role")


def cost_summary(
    spot_entry: Decimal, spot_exit: Decimal, perp_entry: Decimal, perp_exit: Decimal,
    qty: Decimal, funding_cashflow: Decimal, *, role: str = "taker",
) -> dict[str, Decimal]:
    bps = leg_cost_bps(role=role) / Decimal("10000")
    # 配置中的 6/8bp 是每腿完整往返口径，因此按进出场平均名义计费一次。
    spot_fee = ((spot_entry + spot_exit) / Decimal("2")) * qty * bps
    perp_fee = ((perp_entry + perp_exit) / Decimal("2")) * qty * bps
    # Taker + 1 tick 已体现在成交价；滑点另列，不能重复计入手续费。
    return {
        "spot_fee": spot_fee, "perp_fee": perp_fee,
        "fee_total": spot_fee + perp_fee,
        "funding_cashflow": funding_cashflow,
        "gross_basis_pnl": qty * ((spot_exit - spot_entry) - (perp_exit - perp_entry)),
        "net_pnl": qty * ((spot_exit - spot_entry) - (perp_exit - perp_entry))
        + funding_cashflow - spot_fee - perp_fee,
    }


def config_manifest(config: CarryConfig) -> dict[str, Any]:
    return {"strategy_id": STRATEGY_ID, "rule_hash": config.rule_hash,
            "parameters": config.as_dict(), "generated_at_utc": datetime.now(UTC).isoformat()}


__all__ = [
    "BAR_MS", "CarryConfig", "CarryPosition", "FUNDING_INTERVAL_MS", "FundingSettlement",
    "PaperIntent", "STRATEGY_ID", "UNIVERSE", "config_manifest", "cost_summary",
    "execution_prices", "funding_between", "latest_visible_funding", "make_intent",
]
