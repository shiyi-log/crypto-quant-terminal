"""
趋势跟踪策略（Donchian 突破 + 突破强度选币）

版本历史
────────
v1  纯 Donchian 突破，全部币等权 + ATR 风险仓位
    7 年 Sharpe 1.14（15币）/ 0.63（55币等权）/ 1.05（55币+波动率目标）

v2  本版本 —— 走查验证后的两项改进
    ① 【突破强度选币】只做突破幅度最大的 top_n 个币
    ② 【固定分数仓位】每笔 = 总资金 × 目标敞口 / top_n（不再用 ATR 动态定仓）

为什么这样改（walkforward.py 实测，55 币 7 年）
──────────────────────────────────────────────
    N 的敏感性（平滑单调，非过拟合）:
        N=3   Sharpe 1.46  t=3.79  回撤 -7.02%   7/7 正收益年
        N=8   Sharpe 1.30  t=3.38  回撤 -17.19%  7/7 正收益年
        N=12  Sharpe 1.19  t=3.10  回撤 -22.23%  7/7 正收益年
        N=30  Sharpe 0.69  t=1.79
    对比 v1（全部币 + 波动率目标）: Sharpe 1.05  t=2.74  回撤 -24.90%

    样本外走查（前段定参，后段验证）:
        训练 2020-2022 → 测试 2023-2026:  测试 Sharpe 1.12  回撤 -12.33%
        训练 2020-2023 → 测试 2024-2026:  测试 Sharpe 1.42  回撤  -8.36%
        训练 2020-2024 → 测试 2025-2026:  测试 Sharpe 1.25  回撤  -8.36%

    为什么「选最强的 N 个」有效:
        突破幅度 = 价格超出通道边界的距离，衡量趋势的果断程度。
        突破越果断，后续跟随的概率越高 —— 这是对趋势信号的强度过滤。

    为什么「固定分数」优于「ATR 动态定仓」:
        ATR 定仓会随波动率频繁调整仓位 → 换手率上升 → 成本侵蚀。
        固定分数只在信号变化时调仓，换手低得多。

风险提示（必读）
────────────────
    · 边际在衰减: 全样本 Sharpe 1.30，但 2023 年后约 1.0~1.4（走查测试期）
    · 不要为了 30% 加高杠杆: 实测把风险预算从 3% 加到 5%，近期年化反而从
      8.27% 降到 2.89%，加到 8% 变成 -5.25%
    · 建议目标波动 15~20%，对应年化约 12~20%、回撤约 -8%~-18%

参数
────
    enter_period     20     突破前 N 日最高价做多 / 最低价做空
    exit_period      20     反向突破 M 日通道离场
    top_n            8      只做突破强度最高的 N 个币（走查最优区间 5~12）
    target_exposure  0.30   组合目标敞口（总仓位占资金比例）
"""

from datetime import datetime, timezone
from time import monotonic

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.enums import RunMode
from freqtrade.exchange import timeframe_to_minutes, timeframe_to_prev_date
from freqtrade.loggers import logger as freqtrade_logger
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy

logger = freqtrade_logger.getChild("TrendFollowing")


class TrendFollowing(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = True
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count = 130

    # 不用固定止损/ROI：离场完全由通道突破决定
    # ⚠️ 已验证的回测【没有止损】。趋势跟踪是正偏策略，
    #    加止损会砍掉大盈利单 —— 只设极宽兜底防极端事故。
    minimal_roi = {"0": 100.0}
    stoploss = -0.60
    trailing_stop = False

    # ── 可优化参数 ──
    enter_period = IntParameter(10, 100, default=20, space="buy", optimize=True)
    exit_period = IntParameter(5, 50, default=20, space="sell", optimize=True)
    top_n = IntParameter(3, 15, default=8, space="buy", optimize=True)
    target_exposure = DecimalParameter(0.10, 0.60, default=0.30, decimals=2,
                                       space="buy", optimize=True)

    # 运行门控，不参与策略参数优化；历史回测不等待真实时间。
    _startup_entry_delay_seconds = 30.0

    def _is_live_mode(self) -> bool:
        mode = self.config.get("runmode")
        if mode is None and getattr(self, "dp", None) is not None:
            mode = self.dp.runmode
        return mode in (RunMode.LIVE, RunMode.DRY_RUN)

    def bot_start(self, **kwargs) -> None:
        self._strength_cache_key = None
        self._strength_cache = {}
        self._strength_snapshot = {}
        self._entry_not_before = (
            monotonic() + self._startup_entry_delay_seconds if self._is_live_mode() else None
        )
        logger.info(
            "TrendFollowing startup entry_delay_seconds=%s live_mode=%s",
            self._startup_entry_delay_seconds if self._is_live_mode() else 0,
            self._is_live_mode(),
        )

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        # 此钩子在 analyze() 之前调用。只使上一轮快照失效，不能在这里宣告数据 ready。
        self._strength_cache_key = None
        self._strength_reference_time = current_time

    # ══════════════════ 指标 ══════════════════

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        # ⚠️ 通道用【收盘价】而不是最高/最低价。
        #
        # 这是一个真实发生过的实现/验证不一致：
        #   研究框架（walkforward.py）验证时用的是 close 通道，Calmar 0.82；
        #   而实盘策略最初写成 high/low 通道，Freqtrade 实测 Calmar 仅 0.26。
        #   用研究框架复算 high/low 通道 → Calmar 0.27，与 Freqtrade 完全吻合。
        #   即：验证过的策略和部署的策略不是同一个。
        #
        # 收盘价通道更窄 → 突破更容易触发 → 信号更多，实测在两个股票池上都更好：
        #   51 币: close 通道 Calmar 0.95 (t=2.18) vs high/low 0.67 (t=1.60)
        #   20 币: close 通道 Calmar 0.82 (t=1.53) vs high/low 0.27 (t=0.82)
        #
        # 统一为 close 通道，与已验证的研究结论对齐。
        df["hh_entry"] = df["close"].rolling(self.enter_period.value).max().shift(1)
        df["ll_entry"] = df["close"].rolling(self.enter_period.value).min().shift(1)
        df["hh_exit"] = df["close"].rolling(self.exit_period.value).max().shift(1)
        df["ll_exit"] = df["close"].rolling(self.exit_period.value).min().shift(1)

        # 突破强度 = 超出通道边界的幅度 / 通道宽度
        # 衡量这次突破有多果断，用于横向选币
        span = (df["hh_entry"] - df["ll_entry"]).replace(0, np.nan)
        up = (df["close"] - df["hh_entry"]) / span
        dn = (df["ll_entry"] - df["close"]) / span
        df["break_strength"] = pd.concat([up, dn], axis=1).max(axis=1)

        # ── 趋势状态（关键）──
        # 突破后【一直持有】到反向突破，而不是只在突破当天开仓。
        # 回测就是这样做的（平均每天有 ~92% 的币处于趋势状态），
        # 若用「只在突破日开仓」，实盘启动时会漏掉所有已在进行中的趋势，
        # 与已验证的回测结果不一致。
        c = df["close"].values
        hhe, lle = df["hh_entry"].values, df["ll_entry"].values
        hxe, lxe = df["hh_exit"].values, df["ll_exit"].values
        state = np.zeros(len(c))
        cur = 0.0
        for i in range(len(c)):
            if cur == 0.0:
                if np.isfinite(hhe[i]) and c[i] > hhe[i]:
                    cur = 1.0
                elif np.isfinite(lle[i]) and c[i] < lle[i]:
                    cur = -1.0
            else:
                if cur > 0 and np.isfinite(lxe[i]) and c[i] < lxe[i]:
                    cur = 0.0
                elif cur < 0 and np.isfinite(hxe[i]) and c[i] > hxe[i]:
                    cur = 0.0
            state[i] = cur
        df["trend_state"] = state

        # ATR（仅用于日志/诊断，不再用于定仓）
        prev = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev).abs(),
            (df["low"] - prev).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = tr.rolling(20).mean()
        df["atr_pct"] = df["atr"] / df["close"]
        return df

    # ══════════════════ 信号 ══════════════════

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        只要处于趋势状态就可开仓（不是只在突破当天）。
        这样机器人中途启动时能补进【已在进行的趋势】，与回测一致。
        跨币排名由 confirm_trade_entry 完成（只放行强度前 top_n）。
        """
        df = dataframe
        long_cond = (df["trend_state"] > 0) & (df["volume"] > 0)
        short_cond = (df["trend_state"] < 0) & (df["volume"] > 0)
        df.loc[long_cond, ["enter_long", "enter_tag"]] = (1, "trend_up")
        df.loc[short_cond, ["enter_short", "enter_tag"]] = (1, "trend_dn")
        return df

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """趋势状态结束即离场"""
        df = dataframe
        df.loc[df["trend_state"] <= 0, ["exit_long", "exit_tag"]] = (1, "trend_end")
        df.loc[df["trend_state"] >= 0, ["exit_short", "exit_tag"]] = (1, "trend_end")
        return df

    # ══════════════════ 横向选币（核心改进） ══════════════════

    def _current_strengths(self) -> dict:
        """
        读取可审计的排名快照。live/dry 必须全池同一根已收盘 K 线就绪。

        完整快照按真实 date、白名单及参数缓存，每轮分析前失效；
        不完整快照不缓存，数据恢复后即使 date 不变也会重算。
        历史模式沿用 DataProvider 的逐币历史切片，不套用 live 全池同步门控。
        """
        live = self._is_live_mode()
        expected_candle = None
        snapshot = {
            "ready": False, "reason": "incomplete_data", "strengths": {},
            "states": {}, "candle_dates": {}, "missing": {},
        }
        frames = {}
        try:
            wl = list(self.dp.current_whitelist())
            if live:
                reference = pd.Timestamp(getattr(
                    self, "_strength_reference_time", datetime.now(timezone.utc)
                ))
                reference = (reference.tz_localize("UTC") if reference.tzinfo is None
                             else reference.tz_convert("UTC"))
                expected_candle = pd.Timestamp(timeframe_to_prev_date(
                    self.timeframe, reference.to_pydatetime()
                )) - pd.Timedelta(minutes=timeframe_to_minutes(self.timeframe))
            key = None
            if wl:
                df0, _ = self.dp.get_analyzed_dataframe(wl[0], self.timeframe)
                frames[wl[0]] = df0
                required = {"date", "trend_state", "break_strength"}
                if df0 is not None and not df0.empty and required.issubset(df0.columns):
                    try:
                        first_row = df0.iloc[-1]
                        first_date = pd.Timestamp(first_row["date"])
                        first_state = float(first_row["trend_state"])
                        first_strength = (float(first_row["break_strength"])
                                          if first_state != 0 else None)
                        valid_first_row = (
                            not pd.isna(first_date) and np.isfinite(first_state)
                            and first_state in (-1.0, 0.0, 1.0)
                            and (first_strength is None or np.isfinite(first_strength))
                        )
                        if valid_first_row:
                            first_date = (first_date.tz_localize("UTC") if first_date.tzinfo is None
                                          else first_date.tz_convert("UTC"))
                            # 同轮首币字段失效或指标改变，也不能命中旧的 ready 快照。
                            key = (first_date.isoformat(), tuple(wl), self.enter_period.value,
                                   self.exit_period.value,
                                   expected_candle.isoformat() if live else None,
                                   first_state, first_strength)
                    except (TypeError, ValueError, OverflowError):
                        # 只禁用缓存，交给完整扫描按 pair 记原因并区分 live/历史模式。
                        key = None
            if key is not None and getattr(self, "_strength_cache_key", None) == key:
                return self._strength_cache
        except Exception as exc:
            # 包括 dp 未安装、白名单不可读；绝不向入口返回部分排名。
            snapshot["missing"]["whitelist"] = type(exc).__name__
            self._strength_cache_key = None
            self._strength_snapshot = snapshot
            logger.warning(
                "TrendFollowing ranking ready=False count=0 strengths={} "
                "entry_blocked_all_live=%s missing=%s", live, snapshot["missing"],
            )
            return {}

        snapshot["expected_candle"] = expected_candle.isoformat() if live else None
        out = {}
        for pair in wl:
            try:
                if pair in frames:
                    df = frames[pair]
                else:
                    df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
                if df is None or df.empty:
                    snapshot["missing"][pair] = "empty_dataframe"
                    continue
                required = {"date", "trend_state", "break_strength"}
                if not required.issubset(df.columns):
                    snapshot["missing"][pair] = "missing_columns:" + ",".join(
                        sorted(required.difference(df.columns))
                    )
                    continue
                row = df.iloc[-1]
                candle = pd.Timestamp(row["date"])
                if pd.isna(candle):
                    snapshot["missing"][pair] = "invalid_candle_date"
                    continue
                candle = (candle.tz_localize("UTC") if candle.tzinfo is None
                          else candle.tz_convert("UTC"))
                snapshot["candle_dates"][pair] = candle.isoformat()
                if live and candle != expected_candle:
                    snapshot["missing"][pair] = "candle_not_current_closed"
                    continue
                state = float(row["trend_state"])
                if not np.isfinite(state) or state not in (-1.0, 0.0, 1.0):
                    snapshot["missing"][pair] = "invalid_trend_state"
                    continue
                snapshot["states"][pair] = state
                if state == 0:
                    continue
                v = float(row["break_strength"])
                if not np.isfinite(v):
                    snapshot["missing"][pair] = "invalid_break_strength"
                    continue
                out[pair] = float(v)
            except Exception as exc:
                snapshot["missing"][pair] = type(exc).__name__

        if not wl:
            snapshot["missing"]["whitelist"] = "empty_whitelist"
        # 历史 DP 逐币推进，保留原有可见切片聚合；live 不容许拿局部池冒充完整排名。
        complete = bool(wl) and not snapshot["missing"]
        snapshot["ready"] = complete if live else bool(wl)
        if live and not complete:
            out = {}
        snapshot["strengths"] = out
        if snapshot["ready"]:
            snapshot["reason"] = "ready" if out else "no_candidates"
        self._strength_snapshot = snapshot
        self._strength_cache_key = key if complete else None
        self._strength_cache = out
        logger.log(
            20 if snapshot["ready"] else 30,
            "TrendFollowing ranking ready=%s reason=%s count=%s whitelist_count=%s "
            "strengths=%s candle_dates_utc=%s expected_candle_utc=%s "
            "entry_blocked_all_live=%s missing=%s",
            snapshot["ready"], snapshot["reason"], len(out), len(wl),
            sorted(out.items(), key=lambda kv: (-kv[1], kv[0])),
            snapshot["candle_dates"], snapshot["expected_candle"],
            live and not snapshot["ready"], snapshot["missing"],
        )

        return out

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                            rate: float, time_in_force: str, current_time,
                            entry_tag, side: str, **kwargs) -> bool:
        """
        只在突破强度排名前 top_n 的币上开仓。
        这是 v2 的核心改进：把「谁都能做」改成「只做最果断的突破」。
        """
        # Freqtrade 外层 wrapper 的异常默认值为 True，因此整个入口必须自行拒绝异常。
        try:
            self._strength_reference_time = current_time
            live = self._is_live_mode()
            if live and getattr(self, "_entry_not_before", None) is None:
                # 防御未执行 bot_start 的调用路径，同样不能绕过首次等待。
                self._entry_not_before = monotonic() + self._startup_entry_delay_seconds
            remaining = max(0.0, self._entry_not_before - monotonic()) if live else 0.0
            strengths = self._current_strengths()
            n = self.top_n.value
            ranked = sorted(strengths, key=lambda p: (-strengths[p], p))
            rank = ranked.index(pair) + 1 if pair in strengths else None
            snapshot = getattr(self, "_strength_snapshot", {})
            allowed = rank is not None and rank <= n
            reason = "top_n" if allowed else ("outside_top_n" if rank else "no_candidate")
            if not strengths:
                allowed = False
                reason = snapshot.get("reason", "no_candidates")
            if live and not snapshot.get("ready", False):
                allowed, reason = False, "incomplete_data"
            if allowed:
                state = snapshot.get("states", {}).get(pair)
                if state is None or (side == "long" and state <= 0) or (
                    side == "short" and state >= 0
                ) or side not in ("long", "short"):
                    allowed, reason = False, "side_mismatch"
            if remaining > 0:
                allowed, reason = False, "startup_warmup"
            logger.info(
                "TrendFollowing entry pair=%s side=%s rank=%s count=%s top_n=%s "
                "allowed=%s reason=%s candle_dates_utc=%s current_time=%s "
                "startup_remaining_seconds=%.3f",
                pair, side, rank, len(strengths), n, allowed, reason,
                snapshot.get("candle_dates", {}), current_time, remaining,
            )
            if not allowed:
                self._last_denied = getattr(self, "_last_denied", {})
                self._last_denied[pair] = strengths.get(pair)
            return allowed
        except Exception:
            logger.exception(
                "TrendFollowing entry pair=%s side=%s rank=None allowed=False "
                "reason=callback_error current_time=%s", pair, side, current_time,
            )
            return False

    # ══════════════════ 仓位 ══════════════════

    def custom_stake_amount(self, pair: str, current_time, current_rate: float,
                            proposed_stake: float, min_stake, max_stake,
                            leverage: float, entry_tag, side: str, **kwargs) -> float:
        """
        组合级预算仓位：
            剩余预算 = 钱包总额 × 目标敞口 − 已持仓 stake
            每笔 = 剩余预算 / 剩余开仓槽位
        ``top_n`` 只用于候选排名，不能作为组合仓位上限。这样即使
        ``max_open_trades`` 放宽，组合总 stake 仍不超过目标敞口。
        """
        wallet = self.wallets.get_total_stake_amount() if self.wallets else proposed_stake
        target_budget = max(float(wallet) * float(self.target_exposure.value), 0.0)
        try:
            from freqtrade.persistence import Trade

            open_stake = max(float(Trade.total_open_trades_stakes()), 0.0)
            open_count = max(int(Trade.get_open_trade_count()), 0)
        except Exception:
            logger.exception("TrendFollowing stake sizing unavailable; deny entry")
            return 0.0

        configured_slots = self.config.get("max_open_trades") if isinstance(self.config, dict) else None
        if (not isinstance(configured_slots, (int, float))
                or not np.isfinite(configured_slots) or configured_slots <= 0):
            configured_slots = max(self.top_n.value, 1)
        remaining_slots = max(int(configured_slots) - open_count, 0)
        remaining_budget = max(target_budget - open_stake, 0.0)
        if remaining_slots == 0 or remaining_budget <= 0:
            logger.info(
                "TrendFollowing stake denied pair=%s open_count=%s open_stake=%.8f "
                "target_budget=%.8f remaining_slots=%s",
                pair, open_count, open_stake, target_budget, remaining_slots,
            )
            return 0.0

        stake = remaining_budget / remaining_slots
        if min_stake and stake < min_stake:
            logger.info(
                "TrendFollowing stake below exchange minimum pair=%s stake=%.8f min_stake=%.8f",
                pair, stake, min_stake,
            )
            return 0.0
        if max_stake:
            stake = min(stake, max_stake)
        return stake
