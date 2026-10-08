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

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy


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
        取所有白名单币在【最后一根 K 线】上的突破强度。只用已收盘数据，无前视。

        ⚡ 缓存：confirm_trade_entry 对每个入场信号调用一次，
        每次都遍历全部白名单是 O(信号数 × 币数)。实测 100 币时最坏 1.5 秒。
        这里按「最后一根 K 线的时间戳」缓存 —— 同一根 K 线内只算一次，
        复杂度降到 O(币数)。
        """
        # 先取一个币的时间戳作为缓存键（同一次分析周期内所有币一致）
        key = None
        try:
            wl = self.dp.current_whitelist()
            if wl:
                df0, _ = self.dp.get_analyzed_dataframe(wl[0], self.timeframe)
                if df0 is not None and not df0.empty:
                    key = str(df0.index[-1])
        except Exception:
            key = None
        if key is not None and getattr(self, "_strength_cache_key", None) == key:
            return self._strength_cache

        out = {}
        for pair in self.dp.current_whitelist():
            try:
                df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            except Exception:
                continue
            if df is None or df.empty or "break_strength" not in df.columns:
                continue
            if df["trend_state"].iloc[-1] == 0:
                continue                      # 无趋势 → 不参与排名
            v = df["break_strength"].iloc[-1]
            if pd.notna(v) and np.isfinite(v):
                out[pair] = float(v)

        self._strength_cache_key = key
        self._strength_cache = out
        return out

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                            rate: float, time_in_force: str, current_time,
                            entry_tag, side: str, **kwargs) -> bool:
        """
        只在突破强度排名前 top_n 的币上开仓。
        这是 v2 的核心改进：把「谁都能做」改成「只做最果断的突破」。
        """
        strengths = self._current_strengths()
        n = self.top_n.value

        # ⚠️ 原实现的 bug：pair 不在 strengths 里时 return True（放行），
        #    导致「取不到强度」或「无趋势」的币绕过排名直接开仓 ——
        #    实测把强度最低的 UNI/ZEC 放了进来，而 SOL/BTC 被拒。
        #    正确逻辑：只有进入前 N 名的才放行，其余一律拒绝。
        if len(strengths) <= n:
            return pair in strengths or len(strengths) == 0  # 候选不足时按有无强度判断

        ranked = sorted(strengths.items(), key=lambda kv: -kv[1])
        top = {p for p, _ in ranked[:n]}
        allowed = pair in top
        if not allowed:
            self._last_denied = getattr(self, "_last_denied", {})
            self._last_denied[pair] = strengths.get(pair)
        return allowed

    # ══════════════════ 仓位 ══════════════════

    def custom_stake_amount(self, pair: str, current_time, current_rate: float,
                            proposed_stake: float, min_stake, max_stake,
                            leverage: float, entry_tag, side: str, **kwargs) -> float:
        """
        固定分数仓位：
            每笔 = 可用资金 × 目标敞口 / top_n
        目标敞口 30% / 8 个仓位 → 每笔约 3.75% 资金。
        比 ATR 动态定仓换手低得多（实测 Sharpe 更高）。
        """
        wallet = self.wallets.get_total_stake_amount() if self.wallets else proposed_stake
        stake = wallet * self.target_exposure.value / max(self.top_n.value, 1)
        if min_stake:
            stake = max(stake, min_stake)
        if max_stake:
            stake = min(stake, max_stake)
        return stake
