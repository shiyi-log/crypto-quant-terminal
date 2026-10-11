"""Research metadata stays internally consistent as new strategy arms land."""
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPECS_PATH = ROOT / "research_multi_strategy" / "strategy_specs.json"


class ResearchSpecsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(SPECS_PATH.read_text(encoding="utf-8"))
        cls.strategies = cls.data["strategies"]

    def test_all_thirty_strategy_ids_are_unique_and_s31_is_explicitly_rejected(self):
        ids = [item["strategy_id"] for item in self.strategies]
        self.assertEqual(len(ids), 30)
        self.assertEqual(len(ids), len(set(ids)))
        s31 = next(item for item in self.strategies
                   if item["strategy_id"] == "s31_delta_neutral_carry")
        self.assertFalse(s31["data_ready"])
        self.assertEqual(s31["arm_id"], "E_carry_delta_neutral")
        self.assertEqual(s31["arm_status"], "REJECTED")
        self.assertEqual(s31["hypothesis_status"], "rejected")
        self.assertEqual(s31["data_status"], "funding_series_loaded_rejected")
        self.assertIsNone(s31["data_blocker"])
        self.assertIn("0.08%", s31["rejection_reason"])
        self.assertEqual(s31["execution_mode"], "two_leg_atomic")

    def test_timeframe_summary_covers_gen3_and_carry(self):
        summary = self.data["data_requirements_summary"]
        self.assertIn("s31", summary["by_timeframe"]["1h"])
        for strategy_id in ("s22", "s23", "s24", "s25", "s26",
                             "s27", "s28", "s29", "s30"):
            self.assertIn(strategy_id, summary["by_timeframe"]["1d"])
        self.assertNotIn("s31_delta_neutral_carry", summary["blocked"])
        self.assertIn("s31_delta_neutral_carry", summary["rejected"])

    def test_arm_catalog_matches_current_research_scope(self):
        arms = {item["arm_id"]: item for item in self.data["arms"]["list"]}
        self.assertEqual(set(arms), {
            "A_5m_100x_taker", "B_15m_maker", "C_gen2_multitf",
            "D_gen3_multitf", "E_carry_delta_neutral",
        })
        self.assertEqual(arms["E_carry_delta_neutral"]["status"],
                         "REJECTED")

    def test_rejection_preserves_funding_inventory_and_research_audit(self):
        s31 = next(item for item in self.strategies
                   if item["strategy_id"] == "s31_delta_neutral_carry")
        self.assertEqual(s31["data_inventory"]["storage"], "quant.series_points")
        self.assertEqual(s31["data_inventory"]["metric"], "funding_rate")
        self.assertEqual(s31["data_inventory"]["assets"], 57)
        self.assertEqual(s31["data_inventory"]["rows"], 236558)
        self.assertEqual(s31["known_evidence"]["periods"], 33)
        self.assertIn("0.08%", s31["verification_by_dsh"]["cost_correction"]["correct"])


if __name__ == "__main__":
    unittest.main()
