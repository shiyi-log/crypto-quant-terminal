"""可重放的 Binance Demo 风控日状态与冷却规则。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
BASE_CAPITAL = Decimal("1000")


def _money(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("金额必须是有限十进制数")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("金额必须是有限十进制数") from exc
    if not result.is_finite():
        raise ValueError("金额必须是有限十进制数")
    return result


def _stamp(value) -> int:
    if isinstance(value, bool) or int(value) != value or value < 0:
        raise ValueError("时间戳必须是非负毫秒整数")
    return int(value)


def _bjt_day(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).astimezone(BEIJING).date().isoformat()


def _at_midnight(timestamp_ms: int) -> bool:
    local = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).astimezone(BEIJING)
    return (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)


class DemoRiskPolicy:
    """保存风险事实并从追加式事件日志恢复相同的门禁状态。

    净权益应包含标记盈亏、手续费、资金费和已确认现金流。外部净流单独
    传入，用来避免入金抬高收益或阶段净值峰值。
    """

    def __init__(self, *, stage_initial_capital_usdt="1000", events=None):
        capital = _money(stage_initial_capital_usdt)
        if capital <= 0:
            raise ValueError("阶段起始本金必须为正")
        self.stage_initial_capital = capital
        scale = capital / BASE_CAPITAL
        self.loss20, self.loss30, self.loss40, self.loss50 = (
            Decimal(value) * scale for value in (20, 30, 40, 50)
        )
        self.drawdown_limit = Decimal(150) * scale
        self._journal = []
        self._new_events = []
        self._event_ids = {}
        self._trade_ids = {}
        self._day_openings = {}
        self._daily_lows = {}
        self._latest_adjusted = {}
        self._latest_observed_ms = {}
        self._evidence_by_day = {}
        self._stage_peak = capital
        self._max_drawdown = Decimal(0)
        self._equity_marks = []
        self._hard_failure = None
        self._loss_streak = 0
        self._cooldown_until_ms = 0
        self._daily20_triggered = set()
        self._daily_stop_days = set()
        self._lock_day = None
        self._lock_reconciled = False
        if events:
            self._restore(events)

    @classmethod
    def from_events(cls, events, *, stage_initial_capital_usdt="1000"):
        return cls(stage_initial_capital_usdt=stage_initial_capital_usdt, events=events)

    @property
    def journal(self):
        return [dict(event, payload=dict(event["payload"])) for event in self._journal]

    def drain_events(self):
        events, self._new_events = self._new_events, []
        return [dict(event, payload=dict(event["payload"])) for event in events]

    def pending_events(self):
        """返回尚未确认持久化的风险事件副本。"""
        return [dict(event, payload=dict(event["payload"])) for event in self._new_events]

    def acknowledge_events(self, event_ids):
        acknowledged = {str(event_id) for event_id in event_ids}
        self._new_events = [event for event in self._new_events
                            if event["event_id"] not in acknowledged]

    def _append(self, event_type, timestamp_ms, payload, event_id=None):
        if event_id is None:
            raw = json.dumps([event_type, timestamp_ms, payload], sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
            event_id = hashlib.sha256(raw.encode()).hexdigest()
        event_id = str(event_id)
        event = {"event_id": event_id, "event_type": event_type,
                 "timestamp_ms": timestamp_ms, "payload": payload}
        existing = self._event_ids.get(event_id)
        if existing is not None:
            if existing != event:
                raise ValueError("相同风控事件 ID 的事实冲突")
            return self.status(timestamp_ms)
        self._validate_event(event)
        self._journal.append(event)
        self._new_events.append(event)
        self._event_ids[event_id] = event
        self._apply(event)
        return self.status(timestamp_ms)

    def _restore(self, events):
        for event in events:
            normalized = {"event_id": str(event["event_id"]),
                          "event_type": event["event_type"],
                          "timestamp_ms": _stamp(event["timestamp_ms"]),
                          "payload": dict(event["payload"])}
            self._append(normalized["event_type"], normalized["timestamp_ms"],
                         normalized["payload"], normalized["event_id"])
        self._new_events.clear()

    def _validate_event(self, event):
        """在线接收和重放共用验证，坏事件不得先写进内存日志。"""
        kind, payload, ts = event["event_type"], event["payload"], event["timestamp_ms"]
        day = _bjt_day(ts)
        if kind in {"equity_observed", "day_opening"}:
            equity = _money(payload["net_equity_usdt"])
            if not isinstance(payload["data_complete"], bool):
                raise ValueError("data_complete 必须是布尔值")
            if payload["data_complete"]:
                flow = _money(payload["cumulative_external_net_flow_usdt"])
                if kind == "day_opening" and not _at_midnight(ts):
                    raise ValueError("日初净值必须来自北京时间午夜")
                opening = self._day_openings.get(day)
                if _at_midnight(ts) and opening is not None and opening != equity - flow:
                    raise ValueError("相同交易日日初净值事实冲突")
            elif kind == "day_opening":
                raise ValueError("日初净值证据必须完整")
        elif kind == "round_trip_closed":
            pnl = _money(payload["net_pnl_usdt"])
            facts = (ts, pnl)
            existing = self._trade_ids.get(str(payload["trade_id"]))
            if existing is not None and existing != facts:
                raise ValueError("相同完整交易 ID 的净盈亏事实冲突")
        elif kind == "lock_recovered":
            if (self._lock_day is None or payload.get("lock_day") != self._lock_day
                    or day <= self._lock_day or self._hard_failure
                    or any(payload.get(key) is not True for key in
                           ("flat", "no_open_orders", "reconciled", "healthy"))):
                raise ValueError("锁日恢复事实不满足次日与完整对账条件")
        else:
            raise ValueError("未知风控事件类型")

    def _daily_loss(self, day):
        opening = self._day_openings.get(day)
        low = self._daily_lows.get(day)
        return max(Decimal(0), opening - low) if opening is not None and low is not None else Decimal(0)

    def _observe_stage_equity(self, timestamp_ms, adjusted):
        """日常顺序追加为常数时间；迟到净值按事实时间重算峰谷关系。"""
        latest = self._equity_marks[-1][0] if self._equity_marks else -1
        self._equity_marks.append((timestamp_ms, adjusted))
        if timestamp_ms >= latest:
            self._stage_peak = max(self._stage_peak, adjusted)
            self._max_drawdown = max(self._max_drawdown, self._stage_peak - adjusted)
        else:
            self._equity_marks.sort(key=lambda mark: mark[0])
            peak, drawdown = self.stage_initial_capital, Decimal(0)
            for _, value in self._equity_marks:
                peak = max(peak, value)
                drawdown = max(drawdown, peak - value)
            self._stage_peak, self._max_drawdown = peak, drawdown

    def _apply(self, event):
        ts, payload = event["timestamp_ms"], event["payload"]
        day = _bjt_day(ts)
        kind = event["event_type"]
        if kind in {"equity_observed", "day_opening"}:
            equity = _money(payload["net_equity_usdt"])
            complete = payload["data_complete"] is True
            evidence = self._evidence_by_day.get(day)
            if evidence is None or ts >= evidence[0]:
                self._evidence_by_day[day] = (ts, complete)
            if not complete:
                # 资金流未知时不把原始净值用于损失或峰值计算；只记录证据
                # 缺口，避免把未确认的出入金误判成策略收益或风险失败。
                return
            flow = _money(payload["cumulative_external_net_flow_usdt"])
            adjusted = equity - flow
            if day not in self._day_openings and _at_midnight(ts):
                self._day_openings[day] = adjusted
            self._daily_lows[day] = min(self._daily_lows.get(day, adjusted), adjusted)
            if ts >= self._latest_observed_ms.get(day, -1):
                self._latest_adjusted[day] = adjusted
                self._latest_observed_ms[day] = ts
            self._observe_stage_equity(ts, adjusted)
            if self._max_drawdown >= self.drawdown_limit:
                self._hard_failure = self._hard_failure or "drawdown_limit_reached"
            if complete:
                loss = self._daily_loss(day)
                if loss >= self.loss40:
                    if self._lock_day is None or day >= self._lock_day:
                        self._lock_day, self._lock_reconciled = day, False
                elif loss >= self.loss30:
                    self._daily_stop_days.add(day)
                elif loss >= self.loss20 and day not in self._daily20_triggered:
                    self._daily20_triggered.add(day)
                    self._cooldown_until_ms = max(self._cooldown_until_ms, ts + 7_200_000)
                if loss > self.loss50:
                    self._hard_failure = self._hard_failure or "daily_loss_exceeded"
        elif kind == "round_trip_closed":
            trade_id = str(payload["trade_id"])
            if trade_id in self._trade_ids:
                return
            pnl = _money(payload["net_pnl_usdt"])
            self._trade_ids[trade_id] = (ts, pnl)
            if pnl < 0:
                self._loss_streak += 1
                if self._daily_loss(day) >= self.loss20:
                    # 每笔新的完整亏损交易最多重新触发一次日亏冷却。
                    self._cooldown_until_ms = max(self._cooldown_until_ms, ts + 7_200_000)
            elif pnl > 0:
                self._loss_streak = 0
            if self._loss_streak >= 3:
                self._cooldown_until_ms = max(self._cooldown_until_ms, ts + 3_600_000)
                # 已触发的一组三笔立即消费，第四笔属于下一组，不会反复延长。
                self._loss_streak = 0
        elif kind == "lock_recovered":
            self._lock_reconciled = True
        else:
            raise ValueError("未知风控事件类型")

    def observe_equity(self, *, timestamp_ms, net_equity_usdt,
                       cumulative_external_net_flow_usdt="0", data_complete=True, event_id=None):
        ts = _stamp(timestamp_ms)
        if not isinstance(data_complete, bool):
            raise ValueError("data_complete 必须是布尔值")
        flow = (str(_money(cumulative_external_net_flow_usdt))
                if cumulative_external_net_flow_usdt is not None else None)
        complete = data_complete and flow is not None
        payload = {"net_equity_usdt": str(_money(net_equity_usdt)),
                   "cumulative_external_net_flow_usdt": flow,
                   "data_complete": complete}
        return self._append("equity_observed", ts, payload, event_id)

    def record_round_trip(self, *, timestamp_ms, net_pnl_usdt, trade_id=None, event_id=None):
        ts = _stamp(timestamp_ms)
        payload = {"trade_id": str(trade_id or uuid.uuid4().hex),
                   "net_pnl_usdt": str(_money(net_pnl_usdt))}
        return self._append("round_trip_closed", ts, payload, event_id)

    def record_day_opening(self, *, timestamp_ms, net_equity_usdt,
                           cumulative_external_net_flow_usdt="0", event_id=None):
        """补录可验证的午夜净值；午间余额不能替代日初基线。"""
        ts = _stamp(timestamp_ms)
        if not _at_midnight(ts):
            raise ValueError("日初净值必须来自北京时间午夜")
        return self._append("day_opening", ts, {
            "net_equity_usdt": str(_money(net_equity_usdt)),
            "cumulative_external_net_flow_usdt": str(_money(cumulative_external_net_flow_usdt)),
            "data_complete": True,
        }, event_id)

    def recover_next_day(self, *, timestamp_ms, flat, no_open_orders, reconciled, healthy):
        ts = _stamp(timestamp_ms)
        if any(not isinstance(item, bool) for item in (flat, no_open_orders, reconciled, healthy)):
            raise ValueError("恢复条件必须全部显式提供布尔值")
        day = _bjt_day(ts)
        if (self._lock_day is None or day <= self._lock_day or not all((flat, no_open_orders,
                reconciled, healthy)) or self._hard_failure):
            return False
        self._append("lock_recovered", ts, {"lock_day": self._lock_day,
                     "flat": flat, "no_open_orders": no_open_orders,
                     "reconciled": reconciled, "healthy": healthy},
                     event_id=f"recover:{self._lock_day}:{day}")
        return True

    def status(self, timestamp_ms):
        ts = _stamp(timestamp_ms)
        day = _bjt_day(ts)
        opening = self._day_openings.get(day)
        current = self._latest_adjusted.get(day)
        daily_pnl = current - opening if current is not None and opening is not None else None
        evidence = self._evidence_by_day.get(day)
        complete = evidence is not None and evidence[1] and daily_pnl is not None
        if self._hard_failure:
            mode = "terminated"
        elif self._lock_day and not self._lock_reconciled:
            mode = "lock_until_reconciled"
        elif day in self._daily_stop_days:
            mode = "stop_until_next_day"
        elif self._cooldown_until_ms > ts:
            mode = "cooldown"
        elif not complete:
            mode = "blocked_evidence_incomplete"
        else:
            mode = "enabled"
        return {"as_of_ms": ts, "bjt_day": day, "daily_boundary_timezone": "Asia/Shanghai",
                "daily_pnl_usdt": str(daily_pnl) if complete else None,
                "daily_loss_usdt": str(self._daily_loss(day)) if opening is not None else None,
                "stage_initial_capital_usdt": str(self.stage_initial_capital),
                "stage_drawdown_usdt": str(self._max_drawdown),
                "stage_drawdown_limit_usdt": str(self.drawdown_limit),
                "consecutive_losses": self._loss_streak,
                "cooldown_until_ms": self._cooldown_until_ms or None,
                "entries_mode": mode, "hard_failure": self._hard_failure,
                "verdict": "failed" if self._hard_failure else None,
                "evidence_complete": complete,
                "journal_event_count": len(self._journal)}
