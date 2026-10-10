"""Contract tests for the restartable forward paper runner."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from forward_paper_runner import ForwardPaperRunner  # noqa: E402


def fixture(periods=70):
    index = pd.date_range("2025-01-01", periods=periods, freq="h", tz="UTC")
    close = np.full(len(index), 100.0)
    close[25] = 110.0
    close[26:48] = np.linspace(109.0, 80.0, min(22, max(0, periods - 26)))
    if periods > 48:
        close[48:] = 80.0
    opening = np.r_[100.0, close[:-1]]
    if periods > 26:
        opening[26] = 110.0
    return {"LOSS/USDT:USDT": pd.DataFrame(
        {"open": opening, "close": close}, index=index)}


class ForwardPaperRunnerTests(unittest.TestCase):
    def test_same_snapshot_after_observation_gap_records_continuity_loss(self):
        data = {key: frame.iloc[:10] for key, frame in fixture().items()}
        now = [datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)]
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(
                tmp, [{"variant_id": "base"}], top_n=1, max_open=1,
                wallet=1_000.0, clock=lambda: now[0],
            )
            first = runner.update(data)
            self.assertFalse(first["replay_completed"])

            now[0] += timedelta(hours=2)
            repeated = runner.update(data)

            self.assertFalse(repeated["replay_completed"])
            self.assertFalse(repeated["strategy_usable"])
            self.assertEqual(repeated["strategy_unusable_reason"], "replay_deferred")
            self.assertEqual(repeated["decision_count"], 0)
            checkpoint = json.loads((Path(tmp) / "checkpoint.json").read_text())
            self.assertEqual(checkpoint["history_gap_count"], 1)
            self.assertTrue(checkpoint["continuity_lost"])
            snapshots = [json.loads(line) for line in
                         (Path(tmp) / "manifest.jsonl").read_text().splitlines()]
            snapshots = [row for row in snapshots if row.get("event_type") == "snapshot"]
            self.assertEqual(len(snapshots), 2)

    def test_database_outbox_retries_after_store_failure(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as tmp, \
                patch("research_ledger.append_forward_paper_events",
                      side_effect=[RuntimeError("database offline"), None, None, None]) as append:
            runner = ForwardPaperRunner(
                tmp, [{"variant_id": "base"}], top_n=1, max_open=1,
                wallet=1_000.0, ledger_store=object()
            )
            self.assertEqual(append.call_count, 1)
            first = runner.update(data)
            self.assertEqual(append.call_count, 2)
            self.assertTrue(first["database_ledger"]["enabled"])
            self.assertIsNone(first["database_ledger"]["sync_error"])
            events = append.call_args.args[2]
            self.assertEqual({event["event_type"] for event in events},
                             {"manifest", "snapshot", "checkpoint"})
            # A restart retries from the same JSONL outbox and does not create
            # a second set of mutable or synthetic event identities.
            restarted = ForwardPaperRunner(
                tmp, [{"variant_id": "base"}], top_n=1, max_open=1,
                wallet=1_000.0, ledger_store=object()
            )
            self.assertEqual(append.call_count, 3)
            restarted.update(data)
            # The immutable same-snapshot fast path retries the outbox without
            # changing any JSONL/checkpoint bytes.
            self.assertEqual(append.call_count, 4)

    def test_growth_replay_is_append_only_and_restartable(self):
        full = fixture()
        first = {key: frame.iloc[:10] for key, frame in full.items()}
        variants = [{"variant_id": "base", "chan_entry": 20, "chan_exit": 20}]
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, variants, top_n=1, max_open=1,
                                        wallet=1_000.0)
            initial = runner.update(first)
            self.assertTrue(initial["data_ready"])
            self.assertFalse(initial["replay_completed"])
            self.assertFalse(initial["strategy_usable"])
            self.assertEqual(initial["fill_count"], 0)
            paths = [Path(tmp) / name for name in
                     ("manifest.jsonl", "decisions.jsonl", "fills.jsonl", "checkpoint.json")]
            before = {path.name: path.read_bytes() for path in paths}

            same = runner.update(first)
            self.assertEqual(same["decision_count"], initial["decision_count"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in paths})

            grown = runner.update(full)
            self.assertGreater(grown["decision_count"], initial["decision_count"])
            self.assertIn("unrealized_pnl_before_unknown_costs",
                          grown["variants"]["base"])
            grown_bytes = {path.name: path.read_bytes() for path in paths}
            self.assertNotEqual(before["decisions.jsonl"], grown_bytes["decisions.jsonl"])

            # A new process reads the ledgers/checkpoint and must not append
            # duplicate events for the same cumulative input.
            resumed = ForwardPaperRunner(tmp, variants, top_n=1, max_open=1,
                                         wallet=1_000.0)
            result = resumed.update(full)
            self.assertEqual(result["decision_count"], grown["decision_count"])
            self.assertEqual(result["fill_count"], grown["fill_count"])
            self.assertEqual(grown_bytes, {path.name: path.read_bytes() for path in paths})

            decisions = [json.loads(line) for line in
                         (Path(tmp) / "decisions.jsonl").read_text().splitlines()]
            fills = [json.loads(line) for line in
                     (Path(tmp) / "fills.jsonl").read_text().splitlines()]
            self.assertTrue(decisions)
            self.assertEqual(len({row["event_id"] for row in decisions}), len(decisions))
            self.assertTrue(all(row["run_id"] == result["run_id"] for row in decisions + fills))
            self.assertTrue(all("actual_fill" not in candidate
                                for row in decisions for candidate in row.get("candidates", [])))

            entry_fills = [row for row in fills if row["action"] == "entry"]
            exit_fills = [row for row in fills if row["action"] == "exit"]
            self.assertTrue(entry_fills)
            self.assertTrue(all(row.get("decision_event_id") for row in decisions))
            self.assertTrue(all(row.get("decision_event_id") for row in fills))
            self.assertTrue(all(row.get("position_id") and row.get("paper_trade_id")
                                for row in fills))
            self.assertTrue(
                {row["position_id"] for row in exit_fills}
                <= {row["position_id"] for row in entry_fills}
            )
            self.assertTrue(
                {row["paper_trade_id"] for row in exit_fills}
                <= {row["paper_trade_id"] for row in entry_fills}
            )

    def test_lifecycle_ids_survive_restart_and_are_stable(self):
        full = fixture()
        variants = [{"variant_id": "base", "chan_entry": 20, "chan_exit": 20}]
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, variants, top_n=1, max_open=1,
                                        wallet=1_000.0)
            runner.update({key: frame.iloc[:10] for key, frame in full.items()})
            runner.update(full)
            fills_before = [json.loads(line) for line in
                            (Path(tmp) / "fills.jsonl").read_text().splitlines()]
            ids_before = [
                (row["event_id"], row["decision_event_id"], row["position_id"],
                 row["paper_trade_id"])
                for row in fills_before
            ]
            resumed = ForwardPaperRunner(tmp, variants, top_n=1, max_open=1,
                                         wallet=1_000.0)
            resumed.update(full)
            fills_after = [json.loads(line) for line in
                           (Path(tmp) / "fills.jsonl").read_text().splitlines()]
            self.assertEqual(ids_before, [
                (row["event_id"], row["decision_event_id"], row["position_id"],
                 row["paper_trade_id"])
                for row in fills_after
            ])

    def test_recovery_after_partial_snapshot_survives_restart(self):
        full = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, [{"variant_id": "base"}], top_n=1,
                                        max_open=1, wallet=1_000.0)
            good = runner.update(full)
            partial = runner.update({"LOSS/USDT:USDT": full["LOSS/USDT:USDT"].iloc[0:0]})
            self.assertFalse(partial["data_ready"])
            after_partial_restart = ForwardPaperRunner(
                tmp, [{"variant_id": "base"}], top_n=1,
                max_open=1, wallet=1_000.0,
            ).summary()
            self.assertFalse(after_partial_restart["data_ready"])
            self.assertFalse(after_partial_restart["strategy_usable"])
            recovered = runner.update(full)
            self.assertTrue(recovered["data_ready"])
            self.assertEqual(recovered["decision_count"], good["decision_count"])

            resumed = ForwardPaperRunner(tmp, [{"variant_id": "base"}], top_n=1,
                                         max_open=1, wallet=1_000.0)
            after_restart = resumed.summary()
            self.assertTrue(after_restart["data_ready"])
            self.assertFalse(after_restart["replay_completed"])
            self.assertFalse(after_restart["strategy_usable"])

    def test_prefix_rewrite_is_rejected(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, [{"variant_id": "base"}], top_n=1,
                                        max_open=1, wallet=1_000.0)
            runner.update(data)
            tampered = {key: frame.copy() for key, frame in data.items()}
            tampered["LOSS/USDT:USDT"].iloc[10, tampered["LOSS/USDT:USDT"].columns.get_loc("close")] += 0.01
            with self.assertRaisesRegex(ValueError, "prefix conflict"):
                runner.update(tampered)

    def test_empty_and_partial_snapshots_are_recorded_not_traded(self):
        full = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, [{"variant_id": "base"}], top_n=1,
                                        max_open=1, wallet=1_000.0)
            empty = runner.update({})
            self.assertFalse(empty["data_ready"])
            self.assertFalse(empty["strategy_usable"])
            self.assertEqual(empty["decision_count"], 0)
            partial = runner.update({"LOSS/USDT:USDT": full["LOSS/USDT:USDT"],
                                     "MISSING/USDT:USDT": full["LOSS/USDT:USDT"].iloc[0:0]})
            self.assertFalse(partial["data_ready"])
            self.assertEqual(partial["decision_count"], 0)
            snapshots = [json.loads(line) for line in
                          (Path(tmp) / "manifest.jsonl").read_text().splitlines()]
            self.assertTrue(any(row.get("event_type") == "snapshot" and not row["data_ready"]
                                for row in snapshots))
            self.assertEqual((Path(tmp) / "fills.jsonl").read_text(), "")

            # A later complete snapshot must recover from a partial first
            # observation; the empty symbol is part of the locked universe.
            complete = {
                "LOSS/USDT:USDT": full["LOSS/USDT:USDT"],
                "MISSING/USDT:USDT": full["LOSS/USDT:USDT"],
            }
            recovered = runner.update(complete)
            self.assertTrue(recovered["data_ready"])
            self.assertFalse(recovered["replay_completed"])
            self.assertFalse(recovered["strategy_usable"])
            self.assertIn("base", recovered["variants"])
            # The complete snapshot must not backfill orders from the period
            # observed while the whitelist was incomplete.
            self.assertEqual(recovered["fill_count"], 0)
            checkpoint = json.loads((Path(tmp) / "checkpoint.json").read_text())
            self.assertEqual(checkpoint["blocked_until_utc"],
                             full["LOSS/USDT:USDT"].index[-1].isoformat())

    def test_first_complete_snapshot_only_seeds_forward_boundary(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, [{"variant_id": "base"}], top_n=1,
                                        max_open=1, wallet=1_000.0)
            result = runner.update(data)
            self.assertEqual(result["decision_count"], 0)
            self.assertEqual(result["fill_count"], 0)
            checkpoint = json.loads((Path(tmp) / "checkpoint.json").read_text())
            self.assertEqual(checkpoint["forward_from_utc"],
                             data["LOSS/USDT:USDT"].index[-1].isoformat())

    def test_first_partial_prefix_is_frozen(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, [{"variant_id": "base"}], top_n=1,
                                        max_open=1, wallet=1_000.0)
            partial = {
                "LOSS/USDT:USDT": data["LOSS/USDT:USDT"].iloc[:30],
                "MISSING/USDT:USDT": data["LOSS/USDT:USDT"].iloc[0:0],
            }
            runner.update(partial)
            changed = {key: frame.copy() for key, frame in partial.items()}
            changed["LOSS/USDT:USDT"].iloc[
                10, changed["LOSS/USDT:USDT"].columns.get_loc("close")
            ] += 0.01
            with self.assertRaisesRegex(ValueError, "prefix conflict"):
                runner.update(changed)

    def test_partial_watermark_allows_only_future_candles(self):
        full = fixture(72)
        partial = {
            "LOSS/USDT:USDT": full["LOSS/USDT:USDT"].iloc[:20],
            "MISSING/USDT:USDT": full["LOSS/USDT:USDT"].iloc[0:0],
        }
        later_partial = {
            "LOSS/USDT:USDT": full["LOSS/USDT:USDT"].iloc[:26],
            "MISSING/USDT:USDT": full["LOSS/USDT:USDT"].iloc[0:0],
        }
        complete = {
            "LOSS/USDT:USDT": full["LOSS/USDT:USDT"],
            "MISSING/USDT:USDT": full["LOSS/USDT:USDT"],
        }
        variants = [{"variant_id": "base", "chan_entry": 20, "chan_exit": 20}]
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, variants, top_n=1, max_open=1,
                                         wallet=1_000.0)
            first = runner.update(partial)
            self.assertFalse(first["data_ready"])
            runner.update(later_partial)
            watermark = later_partial["LOSS/USDT:USDT"].index[-1].isoformat()
            checkpoint = json.loads((Path(tmp) / "checkpoint.json").read_text())
            self.assertEqual(checkpoint["blocked_until_utc"], watermark)
            resumed = runner.update(complete)
            self.assertTrue(resumed["data_ready"])
            events = [json.loads(line) for line in
                      (Path(tmp) / "decisions.jsonl").read_text().splitlines()]
            self.assertTrue(all(row["execution_at_utc"] > watermark for row in events))
            fills = [json.loads(line) for line in
                     (Path(tmp) / "fills.jsonl").read_text().splitlines()]
            exit_fills = [row for row in fills if row["action"] == "exit"]
            self.assertEqual(resumed["variants"]["base"]["closed_trade_count"],
                             len(exit_fills))
            self.assertAlmostEqual(
                resumed["variants"]["base"][
                    "realized_profit_after_fee_before_unknown_costs"
                ],
                sum(row["profit_abs"] for row in exit_fills),
            )
            self.assertEqual(resumed["variants"]["base"]["summary_scope_start_utc"],
                             watermark)

    def test_first_candle_after_replay_boundary_is_executed(self):
        full = fixture(72)
        first = {key: frame.iloc[:30] for key, frame in full.items()}
        grown = {key: frame.iloc[:31] for key, frame in full.items()}
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(
                tmp, [{"variant_id": "base", "chan_entry": 20, "chan_exit": 20}],
                top_n=1, max_open=1, wallet=1_000.0,
            )
            seeded = runner.update(first)
            self.assertFalse(seeded["replay_completed"])
            result = runner.update(grown)
            self.assertTrue(result["replay_completed"])
            decisions = [json.loads(line) for line in
                         (Path(tmp) / "decisions.jsonl").read_text().splitlines()]
            boundary = first["LOSS/USDT:USDT"].index[-1].isoformat()
            first_execution = grown["LOSS/USDT:USDT"].index[-1].isoformat()
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0]["candle_utc"], boundary)
            self.assertEqual(decisions[0]["execution_at_utc"], first_execution)

    def test_open_position_survives_partial_watermark_and_restart(self):
        loss = fixture(72)["LOSS/USDT:USDT"]
        quiet = pd.DataFrame(
            {"open": np.full(len(loss), 100.0), "close": np.full(len(loss), 100.0)},
            index=loss.index,
        )
        variants = [{"variant_id": "base", "chan_entry": 20, "chan_exit": 20}]
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, variants, top_n=1, max_open=1,
                                        wallet=1_000.0)
            initial = runner.update({
                "LOSS/USDT:USDT": loss.iloc[:10],
                "QUIET/USDT:USDT": quiet.iloc[:10],
            })
            self.assertFalse(initial["replay_completed"])

            before_gap = runner.update({
                "LOSS/USDT:USDT": loss.iloc[:31],
                "QUIET/USDT:USDT": quiet.iloc[:31],
            })
            fills_path = Path(tmp) / "fills.jsonl"
            fills = [json.loads(line) for line in fills_path.read_text().splitlines()]
            entries_before_gap = [row for row in fills if row["action"] == "entry"]
            self.assertEqual(len(entries_before_gap), 1)
            original = entries_before_gap[0]

            partial = runner.update({
                "LOSS/USDT:USDT": loss.iloc[:31],
                "QUIET/USDT:USDT": quiet.iloc[0:0],
            })
            self.assertFalse(partial["data_ready"])
            checkpoint = json.loads((Path(tmp) / "checkpoint.json").read_text())
            watermark = loss.index[30].isoformat()
            self.assertEqual(checkpoint["blocked_until_utc"], watermark)

            resumed = ForwardPaperRunner(tmp, variants, top_n=1, max_open=1,
                                         wallet=1_000.0)
            recovered = resumed.update({
                "LOSS/USDT:USDT": loss,
                "QUIET/USDT:USDT": quiet,
            })
            self.assertTrue(recovered["replay_completed"])
            fills = [json.loads(line) for line in fills_path.read_text().splitlines()]
            original_entries = [row for row in fills
                                if row["action"] == "entry"
                                and row["position_id"] == original["position_id"]]
            original_exits = [row for row in fills
                              if row["action"] == "exit"
                              and row["position_id"] == original["position_id"]]
            self.assertEqual(len(original_entries), 1)
            self.assertEqual(len(original_exits), 1)
            decisions = [json.loads(line) for line in
                         (Path(tmp) / "decisions.jsonl").read_text().splitlines()]
            exit_decision = next(row for row in decisions
                                 if row["execution_at_utc"] == original_exits[0]["filled_at_utc"])
            self.assertEqual(original_exits[0]["decision_event_id"], exit_decision["event_id"])
            self.assertGreater(original_exits[0]["filled_at_utc"], watermark)
            self.assertEqual(recovered["variants"]["base"]["closed_trade_count"], 1)
            self.assertAlmostEqual(
                recovered["variants"]["base"][
                    "realized_profit_after_fee_before_unknown_costs"
                ],
                sum(row["profit_abs"] for row in fills if row["action"] == "exit"),
            )
            # The initial long position is restored with its original entry
            # date and lifecycle ID, then exited once after the watermark.
            self.assertEqual(original["filled_at_utc"], loss.index[27].isoformat())
            self.assertGreater(before_gap["fill_count"], 0)

    def test_rule_change_conflicts_with_existing_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ForwardPaperRunner(tmp, [{"variant_id": "base", "chan_entry": 20}])
            with self.assertRaisesRegex(ValueError, "immutable manifest conflict"):
                ForwardPaperRunner(tmp, [{"variant_id": "base", "chan_entry": 21}])

    def test_v1_ledger_is_preserved_and_requires_new_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = {
                "event_type": "manifest", "schema_version": "forward-paper-v1",
                "run_id": "legacy-run", "variants": [], "rules": {},
            }
            manifest_path = Path(tmp) / "manifest.jsonl"
            decisions_path = Path(tmp) / "decisions.jsonl"
            fills_path = Path(tmp) / "fills.jsonl"
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            decisions_path.write_text("legacy decision\n", encoding="utf-8")
            fills_path.write_text("legacy fill\n", encoding="utf-8")
            before = {path.name: path.read_bytes() for path in
                      (manifest_path, decisions_path, fills_path)}

            with self.assertRaisesRegex(ValueError, "v1 ledgers cannot be resumed"):
                ForwardPaperRunner(tmp, [{"variant_id": "base"}])

            self.assertEqual(before, {path.name: path.read_bytes() for path in
                                      (manifest_path, decisions_path, fills_path)})

    def test_process_alias_state_and_variant_summary_survive_restart(self):
        data = fixture()
        variants = [{"variant_id": "base", "chan_entry": 20, "chan_exit": 20}]
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, variants, top_n=1, max_open=1,
                                        wallet=1_000.0)
            first = runner.process(data)
            self.assertIn("base", first["variants"])
            self.assertEqual(runner.variant_by_id["base"]["chan_entry"], 20)
            self.assertEqual(runner.state["decision_count"], first["decision_count"])

            resumed = ForwardPaperRunner(tmp, variants, top_n=1, max_open=1,
                                          wallet=1_000.0)
            repeated = resumed.process(data)
            self.assertIn("base", repeated["variants"])
            self.assertEqual(repeated["variants"]["base"]["closed_trade_count"],
                             first["variants"]["base"]["closed_trade_count"])

    def test_new_symbol_after_checkpoint_is_rejected(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, [{"variant_id": "base"}], top_n=1,
                                        max_open=1, wallet=1_000.0)
            runner.update(data)
            expanded = dict(data)
            expanded["NEW/USDT:USDT"] = data["LOSS/USDT:USDT"].copy()
            with self.assertRaisesRegex(ValueError, "symbol universe conflict"):
                runner.update(expanded)

    def test_new_empty_symbol_after_checkpoint_is_rejected(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, [{"variant_id": "base"}], top_n=1,
                                        max_open=1, wallet=1_000.0)
            runner.update(data)
            expanded = dict(data)
            expanded["NEW/USDT:USDT"] = data["LOSS/USDT:USDT"].iloc[0:0]
            with self.assertRaisesRegex(ValueError, "symbol universe conflict"):
                runner.update(expanded)

    def test_shared_interior_gap_is_not_ready(self):
        data = fixture()
        gap = {symbol: frame.drop(frame.index[30]) for symbol, frame in data.items()}
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, [{"variant_id": "base"}], top_n=1,
                                        max_open=1, wallet=1_000.0)
            result = runner.update(gap)
            self.assertFalse(result["data_ready"])
            self.assertFalse(result["strategy_usable"])
            self.assertEqual(result["decision_count"], 0)

    def test_non_finite_prices_are_not_ready(self):
        data = fixture()
        invalid = {symbol: frame.copy() for symbol, frame in data.items()}
        invalid["LOSS/USDT:USDT"].iloc[
            30, invalid["LOSS/USDT:USDT"].columns.get_loc("close")
        ] = np.nan
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, [{"variant_id": "base"}], top_n=1,
                                        max_open=1, wallet=1_000.0)
            result = runner.update(invalid)
            self.assertFalse(result["data_ready"])
            self.assertFalse(result["strategy_usable"])
            self.assertEqual(result["decision_count"], 0)
            self.assertEqual(result["fill_count"], 0)

    def test_fill_lifecycle_ids_link_decision_entry_and_exit(self):
        data = fixture()
        variants = [{"variant_id": "base", "chan_entry": 20, "chan_exit": 20}]
        with tempfile.TemporaryDirectory() as tmp:
            runner = ForwardPaperRunner(tmp, variants, top_n=1, max_open=1,
                                        wallet=1_000.0)
            runner.update({key: frame.iloc[:10] for key, frame in data.items()})
            runner.update(data)
            decisions = [json.loads(line) for line in
                          (Path(tmp) / "decisions.jsonl").read_text().splitlines()]
            fills = [json.loads(line) for line in
                     (Path(tmp) / "fills.jsonl").read_text().splitlines()]
            decision_ids = {row["event_id"] for row in decisions}
            self.assertTrue(fills)
            for fill in fills:
                self.assertIn(fill["decision_event_id"], decision_ids)
                self.assertTrue(fill["position_id"])
                self.assertEqual(fill["paper_trade_id"], fill["position_id"])
            entries = {row["paper_trade_id"] for row in fills
                       if row["action"] == "entry"}
            exits = {row["paper_trade_id"] for row in fills
                     if row["action"] == "exit"}
            self.assertTrue(entries)
            self.assertTrue(exits)
            self.assertTrue(exits.issubset(entries))


if __name__ == "__main__":
    unittest.main()
