"""P0(c) 验收工具自身的反证回归；已知前视恢复后必须被检出。"""
import contextlib
import inspect
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import event_backtest as EB  # noqa: E402
import p0c_perturbation as P  # noqa: E402
from test_event_backtest_v2_regressions import staged  # noqa: E402


def run(data):
    return EB.run_v2(data, top_n=3, max_open=6, exposure=0.60)


def trade():
    return {
        "pair": "AAA", "open_date": "2022-01-02", "close_date": "2022-01-04",
        "open_rate": 100.0, "close_rate": 100.0, "stake": 100.0,
        "is_short": False, "was_pending": False,
    }


def fixed_output(rows, opened=None):
    opened = opened or []
    idx = pd.date_range("2022-01-01", periods=5)
    eq = pd.Series(10000.0, index=idx)
    diag = {
        "closed_trades": len(rows), "open_positions_at_end": len(opened),
        "open_positions": opened, "pending_exit_days": 0, "missing_open_fill_skips": 0,
    }
    return pd.DataFrame(rows), eq, eq.pct_change(), diag


class DecisionEvidenceTests(unittest.TestCase):
    def test_zero_closed_keeps_open_entry_with_direction_and_size(self):
        p = trade()
        p.update(is_short=True, pending_exit=False)
        events, _ = P.decisions(lambda _: fixed_output([], [p]), {})
        self.assertEqual(events, {("in", "AAA", "2022-01-02", 100.0,
                                  "short", 100.0, -1.0)})

    def test_entry_is_identical_when_trade_remains_open_at_end(self):
        closed, _ = P.decisions(lambda _: fixed_output([trade()]), {})
        opened, _ = P.decisions(lambda _: fixed_output([], [trade()]), {})
        self.assertEqual({e for e in closed if e[0] == "in"}, opened)
        self.assertEqual({e[0] for e in opened}, {"in"})

    def test_future_size_and_side_changes_are_each_detected(self):
        idx = pd.date_range("2022-01-01", periods=5)
        data = {"AAA": pd.DataFrame({"open": 100.0, "close": 100.0}, index=idx)}
        for defect in ("stake", "side"):
            with self.subTest(defect=defect):
                def broken(d):
                    row = trade()
                    future = float(d["AAA"]["close"].iloc[-1])
                    if defect == "stake":
                        row["stake"] = future
                    else:
                        row["is_short"] = future > 120
                    return fixed_output([row])

                result = P.T2_future(broken, data, [idx[-1]], log=lambda _: None)
                self.assertTrue(result[0]["changed"],
                                "只改方向/金额也必须被识别为过去决策改变")
                self.assertGreater(result[0]["n_base_before"], 0)

    def test_empty_t1_sample_has_no_coverage(self):
        idx = pd.date_range("2022-01-01", periods=3)
        data = {"AAA": pd.DataFrame({"open": 100.0, "close": 100.0}, index=idx)}
        def empty(_):
            return fixed_output([])

        rows = P.T1_sharp(empty, data, [idx[1]], log=lambda _: None)
        self.assertEqual(rows[0]["n_base"], 0)
        self.assertFalse(any(row["n_base"] > 0 for row in rows))

    def test_t2_marks_empty_baseline_history_as_uncovered(self):
        idx = pd.date_range("2022-01-01", periods=3)
        data = {"AAA": pd.DataFrame({"open": 100.0, "close": 100.0}, index=idx)}
        rows = P.T2_future(lambda _: fixed_output([]), data, [idx[1]],
                           log=lambda _: None)
        self.assertEqual(rows[0]["n_base_before"], 0)
        self.assertFalse(rows[0]["has_history_coverage"])


class KnownLookaheadMutationTests(unittest.TestCase):
    def test_t1_rejects_restored_missing_open_valuation_lookahead(self):
        data = staged()
        baseline, _, _, _ = run(data)
        a = baseline[baseline["pair"] == "AAA"].iloc[0]
        overlap = baseline[(baseline["pair"] == "BBB")
                           & (baseline["open_date"] > a["open_date"])
                           & (baseline["open_date"] < a["close_date"])]
        self.assertGreater(len(overlap), 0, "必须覆盖已持仓估值影响另一币定仓的路径")
        day = pd.Timestamp(overlap.iloc[0]["open_date"])
        data["AAA/USDT:USDT"].loc[day, "open"] = np.nan

        # 只在内存编译变体，绝不把旧 bug 写回共享生产文件。
        source = inspect.getsource(EB.run_v2)
        self.assertEqual(source.count("lp = last_px[k]"), 1,
                         "变体必须精确恢复 known_price 那一处已知 bug")
        scope = dict(EB.__dict__)
        exec(source.replace("lp = last_px[k]", "lp = px[i, k]"), scope)

        def broken(d):
            return scope["run_v2"](d, top_n=3, max_open=6, exposure=0.60)

        good = P.T1_sharp(run, data, [day], log=lambda _: None)[0]
        bad = P.T1_sharp(broken, data, [day], log=lambda _: None)[0]
        self.assertGreater(good["n_base"], 0, "比较不得是空集合")
        self.assertFalse(good["changed"], "修复版应通过")
        self.assertTrue(bad["changed"], "恢复真实定仓前视后必须失败")
        self.assertEqual(bad["n_base"], bad["n_pert"],
                         "本反例同币同日同价，只改变金额，不能靠成交数量抓到")


class BoundaryEvidenceTests(unittest.TestCase):
    def test_both_bare_coin_and_full_pair_keys_really_inject(self):
        for bare in (False, True):
            with self.subTest(bare=bare):
                data = staged()
                if bare:
                    data = {k.split("/")[0]: v for k, v in data.items()}
                evidence = P.T3_boundaries(run, data, log=lambda _: None)
                injected = evidence["injected_missing_open"]
                self.assertTrue(evidence["passed"])
                self.assertIn(injected["data_key"], data)
                self.assertGreater(injected["pending_exit_days"], 0)
                self.assertGreater(injected["missing_open_fill_skips"], 0)
                self.assertNotEqual(injected["day"], injected["resolution"])

    def test_zero_closed_cannot_silently_skip_boundary_test(self):
        with self.assertRaisesRegex(AssertionError, "无已平仓样本"):
            P.T3_boundaries(lambda _: fixed_output([], [trade()]), {}, log=lambda _: None)

    def test_ignoring_missing_open_fails_even_when_global_counts_are_positive(self):
        # 恒正诊断计数不能证明目标仓位真的挂起。
        def broken(data):
            filled = {k: v.copy() for k, v in data.items()}
            for frame in filled.values():
                frame["open"] = frame["open"].fillna(frame["close"].shift(1))
            tr, eq, ret, diag = run(filled)
            diag.update(pending_exit_days=7, missing_open_fill_skips=7)
            return tr, eq, ret, diag

        with self.assertRaisesRegex(AssertionError, "未保留 pending_exit 标记"):
            P.T3_boundaries(broken, staged(), log=lambda _: None)

    def test_missing_open_without_pending_counts_fails(self):
        def broken(data):
            tr, eq, ret, diag = run(data)
            diag.update(pending_exit_days=0, missing_open_fill_skips=0)
            return tr, eq, ret, diag

        with self.assertRaisesRegex(AssertionError, "未触发退出挂起诊断"):
            P.T3_boundaries(broken, staged(), log=lambda _: None)

    def test_no_future_valid_open_retains_pending_position(self):
        idx = pd.date_range("2022-01-01", periods=5)
        data = {"AAA": pd.DataFrame({"open": 100.0, "close": 100.0}, index=idx)}

        def engine(d):
            row = trade()
            row["close_date"] = "2022-01-05"
            if np.isnan(d["AAA"]["open"].iloc[-1]):
                row["pending_exit"] = True
                tr, eq, ret, diag = fixed_output([], [row])
                diag.update(pending_exit_days=1, missing_open_fill_skips=1)
                return tr, eq, ret, diag
            return fixed_output([row])

        result = P.T3_boundaries(engine, data, log=lambda _: None)
        self.assertEqual(result["injected_missing_open"]["resolution"], "pending_at_end")
        self.assertTrue(result["passed"])

    def test_t3_failure_makes_cli_verdict_and_exit_status_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "verdict.json"
            with (mock.patch.object(P, "load", return_value=staged()),
                  mock.patch.object(P, "T1_sharp", return_value=[{
                      "day": "2022-02-23", "changed": False, "n_base": 1,
                  }]),
                  mock.patch.object(P, "T2_future", return_value=[{
                      "changed": False, "has_history_coverage": True,
                  }]),
                  mock.patch.object(P, "T3_boundaries", side_effect=AssertionError("boundary")),
                  mock.patch.object(sys, "argv", ["p0c", "--quick", "--json", str(target)]),
                  contextlib.redirect_stdout(io.StringIO())):
                status = P.main()
            result = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(status, 1)
        self.assertTrue(result["verdict"]["T1_pass"])
        self.assertTrue(result["verdict"]["T2_pass"])
        self.assertFalse(result["verdict"]["T3_pass"])
        self.assertEqual(result["T3"]["error"], "boundary")

    def test_empty_t1_or_t2_coverage_fails_cli_verdict(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "verdict.json"
            with (mock.patch.object(P, "load", return_value=staged()),
                  mock.patch.object(P, "T1_sharp", return_value=[{
                      "day": "2022-02-23", "changed": False, "n_base": 0,
                  }]),
                  mock.patch.object(P, "T2_future", return_value=[{
                      "changed": False, "n_base_before": 0,
                      "has_history_coverage": False,
                  }]),
                  mock.patch.object(P, "T3_boundaries", return_value={"passed": True}),
                  mock.patch.object(sys, "argv", ["p0c", "--quick", "--json", str(target)]),
                  contextlib.redirect_stdout(io.StringIO())):
                status = P.main()
            result = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(status, 1)
        self.assertFalse(result["T1_coverage"]["has_decision_coverage"])
        self.assertFalse(result["T2_coverage"]["all_cuts_covered"])
        self.assertFalse(result["verdict"]["T1_pass"])
        self.assertFalse(result["verdict"]["T2_pass"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
