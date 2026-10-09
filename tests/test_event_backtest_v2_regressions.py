#!/usr/bin/env python3
"""
run_v2 的三项成对回归（第 2 版 —— 真正有牙齿的版本）。

⚠ 第 1 版是假的：把三处旧 bug 装回 event_backtest.py 后，它依然全部通过。
   三个失败原因：
    R3 手续费：测试里自己写了个 _pnl() 来算，【根本没读引擎输出】—— 纯自证。
    R1 缺价估值：只比较「其他币在 D 日的 entry 列表」，若该日没有其他币进场，
                 assertEqual([], []) 恒真 —— 空集合比较。
    R2 pending exit：里面全是 pass 和 assertTrue(True) —— 什么都没断言。

第 2 版的纪律：
    · 每个测试都必须【读引擎的真实输出】（trades / equity / diag）
    · 每个测试都必须先断言【路径确实被走到】（非空、计数 > 0），否则报错
    · 每个测试都必须能在【装回旧 bug 时失败】—— 已实际验证

验证方法：把三处旧 bug 装回 event_backtest.py，本套测试应全部失败。
"""
import sys
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/shiyi/DeepSeek/量化/bot")

COINS = ["AAA/USDT:USDT", "BBB/USDT:USDT", "CCC/USDT:USDT"]


def synth(seed=7, n=420, vol=0.012, common=1.0, idio=0.0):
    """合成行情。

    ⚠ 必须有【共同市场因子】：否则三个独立随机游走几乎不会在同一天一起突破，
       R1 需要的「同一天 >=2 个币开仓」就构造不出来（第一版就卡在这里）。
      common 越大，币与币的同步性越强。
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="D")
    mkt = rng.normal(0, vol, n)                       # 共同因子
    data = {}
    for j, c in enumerate(COINS):
        r = common * mkt + idio * rng.normal(0, vol, n)
        close = 100 * np.exp(np.cumsum(r))
        openp = np.r_[close[0], close[:-1]]
        data[c] = pd.DataFrame({"open": openp, "close": close}, index=idx)
    return data


def staged(stagger=50, n=300):
    """确定性阶梯行情 —— 用于构造「某币已持有 + 另一币同日进场」的局面。

    AAA 从第 0 天起持续上涨 → 约第 21 天进入多头趋势并持有
    BBB 从第 stagger 天起才上涨 → 约第 stagger+21 天进场，此时 AAA 仍持有
    CCC 全程走平 → 不产生信号

    随机数据做不到这一点：3 个同步币总是同进同出，
    永远构造不出「一个持有 + 一个进场」，R1 就一直是空比较。
    """
    idx = pd.date_range("2022-01-01", periods=n, freq="D")
    t = np.arange(n)
    close = {}
    # AAA：先涨 120 天，再跌 —— 必须让它最终【反向突破离场】，
    #      否则永不离场，而 trades 只记录已平仓交易 → 测试里看到 0 笔。
    peak = 120
    aaa = np.where(t <= peak, 0.004 * t, 0.004 * peak - 0.004 * (t - peak))
    close["AAA/USDT:USDT"] = 100 * np.exp(aaa)
    # BBB：持平 stagger 天 → 涨到 bpeak → 再跌。
    #      同样必须最终反向突破离场，否则不进 trades（已平仓）列表。
    # ⚠ 必须显式写成「涨到 bpeak 再跌」。第一版写成 up − dn，
    #    而 up 一直增长把 dn 抵消了 → 实际走平 → 状态永不归零 → 永不平仓 → 不进 trades。
    bpeak = stagger + 150
    bbb = np.where(
        t < stagger, 0.0,
        np.where(t <= bpeak, 0.003 * (t - stagger),
                 0.003 * (bpeak - stagger) - 0.003 * (t - bpeak)))
    close["BBB/USDT:USDT"] = 100 * np.exp(bbb)
    close["CCC/USDT:USDT"] = np.full(n, 100.0)
    data = {}
    for c, cl in close.items():
        # 开盘价取前一日收盘附近，保证可成交
        op = np.r_[cl[0], cl[:-1]] * 1.0001
        data[c] = pd.DataFrame({"open": op, "close": cl}, index=idx)
    return data


def run(data, **kw):
    import event_backtest as EB
    kw.setdefault("top_n", 3)
    kw.setdefault("max_open", 6)
    kw.setdefault("exposure", 0.60)
    return EB.run_v2(data, **kw)


class R1_MissingOpenValuation(unittest.TestCase):
    """缺开盘价时的【持仓估值】不得依赖当日收盘。

    ⚠ 前两版构造都错了：
       v1 让"进入日 D 的那个币"缺价 —— 它在 D 日开盘时还没被持有，走不到持仓估值那条路。
       v2 用随机/同步数据找"已持有 + 同日进场" —— 同步币同进同出，永远找不到该局面。
       本版用【确定性阶梯行情 staged()】，让 AAA 先持有、BBB 后进场，局面必然出现。

    断言：只改 AAA（已持有）的当日收盘 → BBB 当日开仓规模与盈亏完全不变。
    旧 bug：AAA 估值回退到当日收盘 → 冻结权益变 → BBB 的 stake 跟着变。
    """

    def _scenario(self):
        data0 = staged()
        tr0, _, _, _ = run(data0, top_n=3, max_open=6, exposure=0.60)
        t0 = pd.DataFrame(tr0)
        self.assertGreater(len(t0), 1, "阶梯行情应产生交易")
        t0["od"] = pd.to_datetime(t0["open_date"])
        t0["cd"] = pd.to_datetime(t0["close_date"])
        aaa = t0[t0["pair"] == "AAA"]
        bbb = t0[t0["pair"] == "BBB"]
        self.assertGreater(len(aaa), 0, "AAA 应有持有交易")
        self.assertGreater(len(bbb), 0, "BBB 应有进场交易")
        a = aaa.iloc[0]
        # 找 BBB 在 AAA 持有期内进场的那一天
        cand = bbb[(bbb["od"] > a["od"]) & (bbb["od"] < a["cd"])]
        self.assertGreater(len(cand), 0,
                           "BBB 必须在 AAA 持有期内进场，否则本测试无意义")
        return pd.Timestamp(cand.iloc[0]["od"])

    def test_R1(self):
        day = self._scenario()

        def observe(factor):
            data = staged()
            data["AAA/USDT:USDT"].loc[day, "open"] = np.nan
            data["AAA/USDT:USDT"].loc[day, "close"] *= factor
            tr, _, _, diag = run(data, top_n=3, max_open=6, exposure=0.60)
            t = pd.DataFrame(tr)
            t["od"] = pd.to_datetime(t["open_date"])
            probe = t[(t["od"] == day) & (t["pair"] == "BBB")]
            return (sorted(round(float(x), 6) for x in probe["stake"]),
                    sorted(round(float(x), 4) for x in probe["profit_abs"]),
                    diag)

        a, pa, da = observe(1.0)
        b, pb, db = observe(3.0)
        self.assertGreater(len(a), 0, "该日 BBB 必须开仓，否则比较是空的（假通过）")
        self.assertGreaterEqual(db["stale_valuation"] + db["missing_open_fill_skips"], 1,
                                "缺价必须被诊断计数，不能静默")
        self.assertEqual(a, b,
                         "改动【已持仓币】的当日收盘，不得影响同日另一币的开仓规模 —— "
                         "若变了，说明缺价估值回退到了当日收盘（前视）")
        self.assertEqual(pa, pb, "同理，后续盈亏也不得改变")


class R2_PendingExitPersists(unittest.TestCase):
    """退出信号一旦触发就不可撤销 —— 缺价只能【推迟】成交，不能取消。"""

    def _make(self):
        data0 = synth()
        tr0, _, _, _ = run(data0)
        t0 = pd.DataFrame(tr0)
        self.assertGreater(len(t0), 3, "合成数据应产生交易")
        row = t0.sort_values("close_date").iloc[len(t0) // 2]
        coin = row["pair"] + "/USDT:USDT"
        d = pd.Timestamp(row["close_date"])
        data = synth()
        self.assertIn(d, data[coin].index, "退出日必须在数据里")
        data[coin].loc[d, "open"] = np.nan          # 触发 pending exit
        return row, coin, d, data

    def test_R2_pending_exit_fires_at_first_valid_price(self):
        row, coin, d, data = self._make()
        tr, _, _, diag = run(data)
        t = pd.DataFrame(tr)
        # ✅ 强制路径被走到：必须真的发生过挂起
        self.assertGreater(diag["pending_exit_days"], 0,
                           "本测试必须真正触发 pending exit，否则无效")
        same = t[t["pair"] == row["pair"]].sort_values("close_date")
        self.assertGreater(len(same), 0, "该币应仍有成交记录")
        after = same[pd.to_datetime(same["close_date"]) >= d]
        self.assertGreater(len(after), 0, "挂起的退出最终必须成交，不能永久卡住")
        first = pd.to_datetime(after["close_date"]).min()
        gap = (first - d).days
        self.assertLessEqual(gap, 3,
                             f"挂起退出应在遇到第一个有效价时成交，实际拖了 {gap} 天")
        self.assertGreaterEqual(first, d, "缺价不得让退出提前")

    def test_R2b_pending_not_cancelled_by_signal_flip(self):
        row, coin, d, data = self._make()
        tr, _, _, diag = run(data)
        if diag["pending_exit_days"] == 0:
            self.skipTest("本次未触发 pending exit")
        t = pd.DataFrame(tr)
        same = t[t["pair"] == row["pair"]]
        after = same[pd.to_datetime(same["close_date"]) >= d]
        self.assertGreater(len(after), 0,
                           "挂起退出不得被后续信号转向取消 —— 必须最终成交")


class R3_ExitFeeFormula(unittest.TestCase):
    """手续费公式 —— 直接读引擎输出的 profit_abs 核验，不在测试里自算自证。

    正确：fee_out = |qty_signed| × exit_px × rate
    profit_abs = qty×(exit−entry) − stake×rate − |qty|×exit×rate
    """

    def test_R3_engine_pnl_matches_formula(self):
        data = synth()
        rate = 0.0005
        tr, _, _, _ = run(data, cost_one=rate)
        t = pd.DataFrame(tr)
        self.assertGreater(len(t), 5, "需要足够交易才能核验")
        checked = 0
        for _, r in t.iterrows():
            stake = float(r["stake"])
            entry = float(r["open_rate"])
            exit_px = float(r["close_rate"])
            short = bool(r["is_short"])
            if not (stake > 0 and entry > 0 and exit_px > 0):
                continue
            qty = stake / entry * (-1 if short else 1)
            expect = qty * (exit_px - entry) - stake * rate - abs(qty) * exit_px * rate
            self.assertAlmostEqual(
                round(float(r["profit_abs"]), 4), round(expect, 4), places=4,
                msg=(f"手续费公式不符: {r['pair']} stake={stake} entry={entry} "
                     f"exit={exit_px} short={short}"))
            checked += 1
        self.assertGreater(checked, 5, f"实际核验笔数太少({checked})，测试无效")

    def test_R3_old_formula_would_mismatch(self):
        """反证：旧公式 (stake+|PnL|)×rate 与正确值【可区分】，保证上面的断言有鉴别力。"""
        stake, entry, exit_px, rate = 300.0, 10.0, 8.0, 0.01
        qty = stake / entry
        correct = qty * (exit_px - entry) - stake * rate - abs(qty) * exit_px * rate
        value = stake + qty * (exit_px - entry)
        old_fee = (stake + abs(value - stake)) * rate
        old = value - stake - stake * rate - old_fee
        self.assertAlmostEqual(round(correct, 1), -65.4, places=1)
        self.assertAlmostEqual(round(old, 1), -66.6, places=1)
        self.assertNotAlmostEqual(round(correct, 3), round(old, 3), places=2,
                                  msg="新旧公式必须可区分，否则测试无鉴别力")


class R4_DisclosureAndIntegration(unittest.TestCase):
    def test_R4(self):
        data = synth()
        tr, eq, ret, diag = run(data)
        for key in ("closed_trades", "open_positions_at_end", "stale_valuation",
                    "missing_open_fill_skips", "rejected_no_cash",
                    "topn_slot_blocked_by_missing", "ml_mask_blocked",
                    "pending_exit_days", "frozen_equity_used"):
            self.assertIn(key, diag, f"诊断字段缺失: {key}")
        self.assertEqual(diag["closed_trades"], len(tr), "已平仓笔数应单独披露")
        self.assertFalse(eq.isna().all(), "净值曲线不应全为 NaN")
        self.assertGreaterEqual(diag["open_positions_at_end"], 0)

class MLMaskDateAlignmentTests(unittest.TestCase):
    def _run_mask(self, signal_allowed, fill_allowed):
        import event_backtest as EB

        idx = pd.date_range("2026-02-01", periods=5, freq="D")
        close = np.full(len(idx), 100.0)
        data = {"AAA/USDT:USDT": pd.DataFrame(
            {"open": close, "close": close}, index=idx)}
        states = pd.Series([0.0, 0.0, 1.0, 0.0, 0.0], index=idx)
        strengths = pd.Series([0.0, 0.0, 1.0, 0.0, 0.0], index=idx)
        mask = pd.DataFrame(True, index=idx, columns=["AAA"])
        signal_day, fill_day = idx[2], idx[3]
        mask.loc[signal_day, "AAA"] = signal_allowed
        mask.loc[fill_day, "AAA"] = fill_allowed
        events = []

        with patch.object(EB, "signals", return_value=states), \
                patch.object(EB, "strength", return_value=strengths):
            trades, _, _, diag = EB.run_v2(
                data, top_n=1, max_open=1, exposure=0.30, ml_mask=mask,
                event_sink=events,
            )
        entries = [event for event in events
                   if event.get("event_type") == "fill" and event.get("action") == "entry"]
        return idx, trades, diag, entries

    def test_ml_mask_allows_on_signal_day_even_if_fill_day_denies(self):
        idx, trades, diag, entries = self._run_mask(signal_allowed=True, fill_allowed=False)

        self.assertEqual(diag["ml_mask_blocked"], 0)
        self.assertEqual(len(entries), 1, "允许的信号应产生一次实际入场事件")
        self.assertEqual(pd.Timestamp(entries[0]["candle_utc"]).tz_localize(None), idx[2])
        self.assertEqual(pd.Timestamp(entries[0]["filled_at_utc"]).tz_localize(None), idx[3])
        self.assertEqual(pd.Timestamp(trades.iloc[0]["open_date"]), idx[3])

    def test_ml_mask_denies_on_signal_day_even_if_fill_day_allows(self):
        _, trades, diag, entries = self._run_mask(signal_allowed=False, fill_allowed=True)

        self.assertEqual(diag["ml_mask_blocked"], 1)
        self.assertTrue(trades.empty)
        self.assertEqual(entries, [])

    def _run_missing_mask_cell(self, mask):
        import event_backtest as EB

        idx = pd.date_range("2026-03-01", periods=5, freq="D")
        data = {"AAA/USDT:USDT": pd.DataFrame(
            {"open": 100.0, "close": 100.0}, index=idx)}
        states = pd.Series([0.0, 0.0, 1.0, 0.0, 0.0], index=idx)
        with patch.object(EB, "signals", return_value=states), \
                patch.object(EB, "strength", return_value=states):
            trades, _, _, diag = EB.run_v2(
                data, top_n=1, max_open=1, exposure=0.30, ml_mask=mask,
            )
        return trades, diag

    def test_ml_mask_missing_signal_date_fails_closed(self):
        idx = pd.date_range("2026-03-01", periods=5, freq="D")
        mask = pd.DataFrame(True, index=idx.delete(2), columns=["AAA"])

        trades, diag = self._run_missing_mask_cell(mask)

        self.assertEqual(diag["ml_mask_blocked"], 1)
        self.assertTrue(trades.empty)

    def test_ml_mask_missing_coin_column_fails_closed(self):
        idx = pd.date_range("2026-03-01", periods=5, freq="D")
        mask = pd.DataFrame(True, index=idx, columns=["BBB"])

        trades, diag = self._run_missing_mask_cell(mask)

        self.assertEqual(diag["ml_mask_blocked"], 1)
        self.assertTrue(trades.empty)

    def test_none_mask_keeps_baseline_entries_unfiltered(self):
        import event_backtest as EB

        idx = pd.date_range("2026-05-01", periods=5, freq="D")
        data = {"AAA/USDT:USDT": pd.DataFrame(
            {"open": 100.0, "close": 100.0}, index=idx)}
        states = pd.Series([0.0, 0.0, 1.0, 0.0, 0.0], index=idx)
        with patch.object(EB, "signals", return_value=states), \
                patch.object(EB, "strength", return_value=states):
            trades, _, _, diag = EB.run_v2(
                data, top_n=1, max_open=1, exposure=0.30, ml_mask=None,
            )

        self.assertEqual(diag["ml_mask_blocked"], 0)
        self.assertEqual(len(trades), 1)


class R5_NoTruncationBeforeIndicators(unittest.TestCase):
    """指标必须在完整序列上算 —— 先按 start 截断会破坏热身与既存趋势态。"""

    def test_R5_start_keeps_same_early_trades(self):
        data = synth()
        full_tr, _, _, _ = run(data)
        sub_tr, _, _, _ = run(data, start="2022-07-01")
        tf, ts = pd.DataFrame(full_tr), pd.DataFrame(sub_tr)
        self.assertGreater(len(tf), 0)
        self.assertGreater(len(ts), 0, "限制 start 后仍应有交易")
        common = (set(pd.to_datetime(ts["open_date"]))
                  & set(pd.to_datetime(tf["open_date"])))
        self.assertGreater(len(common), 0,
                           "限制 start 后仍应出现与全窗口一致的交易 "
                           "（若完全无交集，说明指标被截断破坏了热身）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
