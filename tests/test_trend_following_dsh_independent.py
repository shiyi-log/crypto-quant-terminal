#!/usr/bin/env python3
"""
独立验收 TrendFollowing 的启动门控/排名修复（DSH 侧，不复用 Codex 的测试）。

⚠ 为什么必须另写一套：
   Codex 的核心回归测试断言「_current_strengths() 返回 {} → confirm_trade_entry 为 False」。
   但**如果把 confirm_trade_entry 改成恒返回 False，那个测试同样会通过，而策略就废了。**
   所以本套测试的重点是【证伪「一律拒绝」】——即数据就绪时必须正常放行 top_n。

覆盖：
  T1 空 strengths → 拒绝                        （它的核心声明）
  T2 【数据就绪 + 在 top_n 内 → 放行】           ← 证伪「一律拒绝」的关键
  T3 数据就绪 + 在 top_n 外 → 拒绝
  T4 live 模式 + 数据未就绪 → 拒绝
  T5 【回测模式 + 空 strengths → 也拒绝】        （不能只在 live 下 fail-closed）
  T6 _current_strengths 抛异常 → 拒绝            （Freqtrade wrapper 默认 True）
  T7 side 与 trend_state 不一致 → 拒绝
  T8 同分时排序确定（按币名 tie-break）
  T9 回测/Hyperopt 无墙钟等待（_entry_not_before 为 None）
  T10 live 启动后 30 秒内拒绝，且回测不受该 30 秒影响
  T11 默认参数未被改动（20/20/8/0.30）
"""
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, "/Users/shiyi/DeepSeek/量化/bot")

PAIR = "BTC/USDT:USDT"
OTHER = "ETH/USDT:USDT"


def load_strategy(live: bool):
    """构造一个仅够跑门控逻辑的策略实例。"""
    from user_data.strategies.TrendFollowing import TrendFollowing
    from freqtrade.enums import RunMode
    st = TrendFollowing.__new__(TrendFollowing)
    st.config = {"runmode": RunMode.DRY_RUN if live else RunMode.BACKTEST}
    st.dp = MagicMock()
    st.dp.runmode = st.config["runmode"]
    st.dp.whitelist = [PAIR, OTHER]
    st.bot_start()
    return st


class IndependentGateTests(unittest.TestCase):
    def setUp(self):
        self.st = load_strategy(live=False)

    def confirm(self, pair=PAIR, side="long"):
        return self.st.confirm_trade_entry(
            pair=pair, order_type="limit", amount=100.0, rate=1.0,
            time_in_force="gtc", current_time=None, entry_tag=None, side=side)

    def ready_snapshot(self, strengths, states=None, ready=True):
        return {"ready": ready, "reason": "ok",
                "states": states or {p: 1 for p in strengths},
                "candle_dates": {p: "2026-10-08T00:00:00+00:00" for p in strengths},
                "missing": {}}

    # ── T1 它的核心声明 ──
    def test_T1_empty_strengths_denied(self):
        with patch.object(self.st, "_current_strengths", return_value={}):
            self.st._strength_snapshot = {"ready": False, "reason": "no_candidates"}
            self.assertIs(self.confirm(), False)

    # ── T2 关键：证伪「一律拒绝」──
    def test_T2_ready_and_inside_top_n_is_allowed(self):
        s = {PAIR: 0.9, OTHER: 0.5}
        with patch.object(self.st, "_current_strengths", return_value=s):
            self.st._strength_snapshot = self.ready_snapshot(s)
            self.assertIs(self.confirm(PAIR), True,
                          "数据就绪且在 top_n 内必须放行 —— 否则修复把策略改废了")
            self.assertIs(self.confirm(OTHER, side="long"), True,
                          "top_n=8 内第 2 名也应放行")

    # ── T3 ──
    def test_T3_outside_top_n_denied(self):
        s = {f"C{i}/USDT:USDT": 1.0 - i * 0.01 for i in range(12)}
        with patch.object(self.st, "_current_strengths", return_value=s):
            self.st._strength_snapshot = self.ready_snapshot(s)
            worst = "C11/USDT:USDT"          # 第 12 名，top_n=8 之外
            self.assertIs(self.confirm(worst), False)

    # ── T4 ──
    # ⚠ 第一版这里写 self.st._entry_not_before = None，是【假通过】：
    #    confirm_trade_entry 里有防御分支 `if live and _entry_not_before is None: 重新计时`，
    #    把它设成 None 反而会重新起算 30 秒 → 被 startup_warmup 拦掉，
    #    根本没走到 ready 门控。正确做法是把启动等待推到【过去】。
    def test_T4_live_not_ready_denied(self):
        st = load_strategy(live=True)
        st._entry_not_before = time.monotonic() - 1.0   # 让 startup 门控放行
        s = {PAIR: 0.9}
        with patch.object(st, "_current_strengths", return_value=s):
            st._strength_snapshot = self.ready_snapshot(s, ready=False)
            self.assertIs(st.confirm_trade_entry(
                PAIR, "limit", 100.0, 1.0, "gtc", None, None, "long"), False)

    # ── T5 回测下也必须拒绝空 strengths ──
    def test_T5_backtest_empty_strengths_also_denied(self):
        self.assertFalse(self.st._is_live_mode(), "本用例应为回测模式")
        with patch.object(self.st, "_current_strengths", return_value={}):
            self.st._strength_snapshot = {"ready": True, "reason": "no_candidates"}
            self.assertIs(self.confirm(), False,
                          "空 strengths 的拒绝必须是【无条件】的，不能只在 live 生效")

    # ── T6 ──
    def test_T6_exception_is_fail_closed(self):
        with patch.object(self.st, "_current_strengths", side_effect=RuntimeError("boom")):
            self.assertIs(self.confirm(), False,
                          "Freqtrade 外层 wrapper 异常默认 True，入口必须自行返回 False")

    # ── T7 方向一致性 ──
    def test_T7_side_mismatch_denied(self):
        s = {PAIR: 0.9}
        with patch.object(self.st, "_current_strengths", return_value=s):
            # states 说该币是空头趋势(-1)，却请求做多 → 拒绝
            self.st._strength_snapshot = self.ready_snapshot(s, states={PAIR: -1})
            self.assertIs(self.confirm(PAIR, side="long"), False)
            self.st._strength_snapshot = self.ready_snapshot(s, states={PAIR: -1})
            self.assertIs(self.confirm(PAIR, side="short"), True)

    # ── T8 排序确定性 ──
    def test_T8_tie_break_is_deterministic(self):
        s = {PAIR: 0.5, OTHER: 0.5}           # 完全同分
        with patch.object(self.st, "_current_strengths", return_value=s):
            self.st._strength_snapshot = self.ready_snapshot(s)
            results = {self.confirm(PAIR) for _ in range(5)}
            self.assertEqual(len(results), 1, "同分必须给出确定结果（按币名 tie-break）")

    # ── T9 回测无墙钟等待 ──
    def test_T9_backtest_has_no_walltime_wait(self):
        st = load_strategy(live=False)
        self.assertIsNone(st._entry_not_before, "回测模式下不得设置启动等待")
        s = {PAIR: 0.9}
        with patch.object(st, "_current_strengths", return_value=s):
            st._strength_snapshot = self.ready_snapshot(s)
            self.assertIs(st.confirm_trade_entry(
                PAIR, "limit", 100.0, 1.0, "gtc", None, None, "long"), True,
                "回测必须不受 30 秒启动门控影响")

    # ── T10 启动等待只作用于 live ──
    def test_T10_startup_wait_only_affects_live(self):
        st = load_strategy(live=True)
        self.assertIsNotNone(st._entry_not_before, "live 模式应设置启动等待")
        s = {PAIR: 0.9}
        with patch.object(st, "_current_strengths", return_value=s):
            st._strength_snapshot = self.ready_snapshot(s)
            self.assertIs(st.confirm_trade_entry(
                PAIR, "limit", 100.0, 1.0, "gtc", None, None, "long"), False,
                "live 启动 30 秒内应拒绝")

    # ── T12 白名单任意一个币缺数据 → 全策略停止开仓（操作风险，需监控）──
    def test_T12_one_missing_whitelist_pair_locks_out_everything(self):
        st = load_strategy(live=True)
        st._entry_not_before = time.monotonic() - 1.0
        st.dp.whitelist = [PAIR, OTHER, "XRP/USDT:USDT"]
        s = {PAIR: 0.9, OTHER: 0.5}
        with patch.object(st, "_current_strengths", return_value=s):
            st._strength_snapshot = {
                "ready": False, "reason": "incomplete_data",
                "states": {p: 1 for p in s}, "candle_dates": {},
                "missing": {"XRP/USDT:USDT": "empty_dataframe"}}
            self.assertIs(st.confirm_trade_entry(
                PAIR, "limit", 100.0, 1.0, "gtc", None, None, "long"), False)
        # 补上数据后恢复
        with patch.object(st, "_current_strengths", return_value=s):
            st._strength_snapshot = {
                "ready": True, "reason": "ok", "states": {p: 1 for p in s},
                "candle_dates": {}, "missing": {}}
            self.assertIs(st.confirm_trade_entry(
                PAIR, "limit", 100.0, 1.0, "gtc", None, None, "long"), True)
        # 这是【设计选择】（宁可不开也不开错），但必须被监控到：
        # 任意一个白名单币缺数据会让策略静默地完全停止开仓。

    # ── T13 回测模式不受 ready 门控约束（设计选择）──
    def test_T13_backtest_ignores_ready_gate(self):
        st = load_strategy(live=False)
        s = {PAIR: 0.9}
        with patch.object(st, "_current_strengths", return_value=s):
            st._strength_snapshot = {"ready": False, "reason": "incomplete_data",
                                     "states": {p: 1 for p in s},
                                     "candle_dates": {}, "missing": {}}
            self.assertIs(st.confirm_trade_entry(
                PAIR, "limit", 100.0, 1.0, "gtc", None, None, "long"), True)

    # ── T11 默认参数未被改动 ──
    def test_T11_defaults_unchanged(self):
        from user_data.strategies.TrendFollowing import TrendFollowing
        self.assertEqual(TrendFollowing.enter_period.value, 20)
        self.assertEqual(TrendFollowing.exit_period.value, 20)
        self.assertEqual(TrendFollowing.top_n.value, 8)
        self.assertAlmostEqual(float(TrendFollowing.target_exposure.value), 0.30, places=6)
        self.assertEqual(TrendFollowing.timeframe, "1d")
        self.assertTrue(TrendFollowing.can_short)
        self.assertEqual(TrendFollowing.stoploss, -0.60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
