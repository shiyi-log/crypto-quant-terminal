"""Offline contracts for the candle flow-field and series-mirror repair tools."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.backfill_candle_flow_fields import (
    archive_rows,
    derived_extras,
    mislabeled_repairs,
    official_extras,
    rest_rows,
)
from tools.mirror_orderflow_candles import KeyJournal

ARCHIVE_HEADER = ("open_time,open,high,low,close,volume,close_time,quote_volume,"
                  "num_trades,taker_buy_base,taker_buy_quote,ignore")


def archive_csv(rows: list[str], header: bool = True) -> str:
    lines = [ARCHIVE_HEADER] if header else []
    return "\n".join([*lines, *rows]) + "\n"


class OfficialExtrasTests(unittest.TestCase):
    def test_quote_convention_fields_are_mapped(self):
        extras = official_extras(quote_volume=100.0, num_trades=12,
                                 taker_buy_base=6.0, taker_buy_quote=40.0)
        self.assertEqual(extras, {"quote_volume": 100.0, "taker_buy_base": 6.0,
                                  "taker_buy_quote": 40.0, "num_trades": 12})

    def test_invalid_values_are_dropped_not_fabricated(self):
        self.assertEqual(official_extras(quote_volume="bad", num_trades=1.5,
                                         taker_buy_base=-1, taker_buy_quote=None), {})

    def test_zero_quote_volume_is_a_real_observation(self):
        extras = official_extras(quote_volume=0.0, num_trades=0,
                                 taker_buy_base=0.0, taker_buy_quote=0.0)
        self.assertEqual(extras["quote_volume"], 0.0)
        self.assertEqual(extras["num_trades"], 0)


class DerivedExtrasTests(unittest.TestCase):
    def test_quote_is_canonical_and_base_is_kept_separate(self):
        extras = derived_extras(10.0, {"quote_volume": 100.0, "taker_buy_base": 6.0,
                                       "taker_buy_quote": 40.0})
        self.assertAlmostEqual(extras["flow_imbal"], -0.2)
        self.assertAlmostEqual(extras["flow_imbal_q"], -0.2)
        self.assertAlmostEqual(extras["flow_imbal_base"], 0.2)

    def test_volume_can_come_from_extras_for_legacy_rows(self):
        extras = derived_extras(None, {"volume": 10.0, "quote_volume": 100.0,
                                       "taker_buy_base": 6.0, "taker_buy_quote": 40.0})
        self.assertAlmostEqual(extras["flow_imbal_base"], 0.2)

    def test_missing_source_columns_produce_nothing(self):
        self.assertEqual(derived_extras(10.0, {"quote_volume": 100.0}), {})
        self.assertEqual(derived_extras(None, None), {})

    def test_zero_denominator_is_not_a_division_error(self):
        self.assertEqual(derived_extras(10.0, {"quote_volume": 0.0,
                                              "taker_buy_quote": 0.0}), {})


class MislabeledRepairTests(unittest.TestCase):
    def test_quote_value_stored_as_base_is_repaired(self):
        # volume 10, base bought 6 -> base imbalance 0.2; quote imbalance -0.2.
        fixes = mislabeled_repairs(10.0, {"quote_volume": 100.0, "taker_buy_base": 6.0,
                                          "taker_buy_quote": 40.0,
                                          "flow_imbal": -0.2, "flow_imbal_base": -0.2})
        self.assertAlmostEqual(fixes["flow_imbal_base"], 0.2)
        self.assertNotIn("flow_imbal", fixes)

    def test_base_value_stored_as_quote_is_repaired(self):
        fixes = mislabeled_repairs(10.0, {"quote_volume": 100.0, "taker_buy_base": 6.0,
                                          "taker_buy_quote": 40.0,
                                          "flow_imbal": 0.2, "flow_imbal_base": 0.2})
        self.assertAlmostEqual(fixes["flow_imbal"], -0.2)
        self.assertAlmostEqual(fixes["flow_imbal_q"], -0.2)

    def test_correct_and_ambiguous_rows_are_left_alone(self):
        correct = {"quote_volume": 100.0, "taker_buy_base": 6.0, "taker_buy_quote": 40.0,
                   "flow_imbal": -0.2, "flow_imbal_base": 0.2}
        self.assertEqual(mislabeled_repairs(10.0, correct), {})
        unknown = {"quote_volume": 100.0, "taker_buy_base": 6.0, "taker_buy_quote": 40.0,
                   "flow_imbal": 0.9, "flow_imbal_base": 0.9}
        self.assertEqual(mislabeled_repairs(10.0, unknown), {})
        coincident = {"quote_volume": 100.0, "taker_buy_base": 50.0, "taker_buy_quote": 50.0,
                      "flow_imbal": 0.0, "flow_imbal_base": 0.0}
        self.assertEqual(mislabeled_repairs(10.0, coincident), {})
        self.assertEqual(mislabeled_repairs(0.0, unknown), {})


class ArchiveRowsTests(unittest.TestCase):
    def test_millisecond_archive_is_reconciled(self):
        payload = archive_csv(["1791474000000,1,2,0.5,1.5,10,1791474299999,100,12,6,40,0"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "BTCUSDT-5m-2026-10.csv"
            path.write_text(payload, encoding="utf-8")
            rows = archive_rows(path)
        self.assertEqual(len(rows), 1)
        symbol, timestamp, extras = rows[0]
        self.assertEqual(symbol, "BTCUSDT")
        self.assertEqual(timestamp, 1791474000000)
        self.assertEqual(extras["num_trades"], 12)
        self.assertEqual(extras["quote_volume"], 100.0)
        self.assertNotIn("flow_imbal", extras)

    def test_microsecond_archive_is_normalized_to_milliseconds(self):
        payload = archive_csv([], header=False)
        payload += "1791474000000000,1,2,0.5,1.5,10,0,100,12,6,40,0\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ETHUSDT-5m-2026-10.csv"
            path.write_text(payload, encoding="utf-8")
            rows = archive_rows(path)
        self.assertEqual(rows[0][1], 1791474000000)

    def test_zip_archive_with_one_csv_is_read(self):
        payload = archive_csv(["1791474000000,1,2,0.5,1.5,10,0,100,12,6,40,0"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TRXUSDT-5m-2026-10.zip"
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("TRXUSDT-5m-2026-10.csv", payload)
            rows = archive_rows(path)
        self.assertEqual(rows[0][2]["taker_buy_quote"], 40.0)

    def test_archive_without_csv_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.zip"
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("README.txt", "no csv here")
            with self.assertRaises(ValueError):
                archive_rows(path)


class RestRowsTests(unittest.TestCase):
    def test_rest_payload_is_keyed_by_timestamp_and_normalized(self):
        def fetch(pair, timeframe, limit):
            self.assertEqual((pair, timeframe, limit), ("BTC/USDT:USDT", "5m", 2))
            return [{"timestamp": 1000, "quote_volume": 10.0, "num_trades": 3,
                     "taker_buy_base": 1.0, "taker_buy_quote": 4.0},
                    {"timestamp": 1300, "quote_volume": 20.0, "num_trades": 4,
                     "taker_buy_base": 2.0, "taker_buy_quote": 8.0}]

        rows = rest_rows("BTC/USDT:USDT", "5m", 2, fetch=fetch)
        self.assertEqual(sorted(rows), [1000, 1300])
        self.assertEqual(rows[1000]["num_trades"], 3)
        self.assertNotIn("flow_imbal", rows[1000])


class KeyJournalTests(unittest.TestCase):
    def test_insert_keys_are_journalled_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mirror-backup.jsonl"
            journal = KeyJournal(path)
            journal.write({"type": "inserted", "timestamp": 1})
            journal.close()
            with self.assertRaises(FileExistsError):
                KeyJournal(path)
            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(records[0]["type"], "metadata")
        self.assertEqual(records[-1]["timestamp"], 1)

    def test_missing_backup_path_keeps_journal_disabled(self):
        journal = KeyJournal(None)
        journal.write({"type": "inserted"})
        journal.close()


if __name__ == "__main__":
    unittest.main()
