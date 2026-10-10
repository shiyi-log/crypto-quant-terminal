"""Legacy registry evidence remains auditable without becoming measured returns."""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


_SPEC = importlib.util.spec_from_file_location(
    "model_registry_retirement_under_test",
    Path(__file__).resolve().parents[1] / "bot" / "model_registry.py",
)
registry = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(registry)


class ModelRegistryRetirementTests(unittest.TestCase):
    def legacy_entry(self):
        return {
            "id": "live-trend-20-20", "layer": "live_strategy",
            "status": "champion", "metrics": {
                "research_annual": 0.147, "live_annual": 0.156,
                "impl_gap_pct": 0.06, "sharpe_live": 0.75,
                "calmar_live": None,
            },
            "gate": {"passed": True, "criteria": "C1", "reasons": ["old claim"]},
            "deployed": {"mode": "dry_run", "config": "same.json"},
            "history": [{"event": "registered"}],
        }

    def test_fresh_seed_has_no_claimed_live_measurements_or_c1_pass(self):
        reg = registry.seed({"versions": [], "champions": {}})
        entry = registry.get(reg, "live-trend-20-20")
        for field in ("live_annual", "sharpe_live", "impl_gap_pct", "calmar_live"):
            self.assertIsNone(entry["metrics"][field], field)
        self.assertFalse(entry["gate"]["passed"])
        self.assertEqual(entry["gate"]["status"], "invalidated")

    def test_existing_metrics_gate_and_history_remain_in_read_only_archive(self):
        entry = self.legacy_entry()
        original = deepcopy(entry)
        view = registry.public_entry(entry)
        self.assertEqual(entry, original)
        self.assertEqual(view["legacy_metrics"], original["metrics"])
        self.assertEqual(view["legacy_gate"], original["gate"])
        self.assertEqual(view["history"], original["history"])
        self.assertEqual(view["deployed"], original["deployed"])
        self.assertEqual(view["status"], "champion")
        self.assertIsNone(view["metrics"]["live_annual"])
        self.assertIsNone(view["metrics"]["research_annual"])
        self.assertFalse(view["gate"]["passed"])
        self.assertEqual(view["metrics_validity"], "invalidated")

    def test_public_view_keeps_registration_and_unrelated_versions(self):
        other = {"id": "new-measured-version", "metrics": {"live_annual": 0.02},
                 "gate": {"passed": True}}
        reg = {"updated": "before", "champions": {"live_strategy": "live-trend-20-20"},
               "versions": [self.legacy_entry(), other]}
        original = deepcopy(reg)
        view = registry.public_view(reg)
        self.assertEqual(reg, original)
        self.assertEqual(view["champions"], original["champions"])
        self.assertEqual(view["versions"][1], other)
        view["versions"][1]["metrics"]["live_annual"] = 99
        self.assertEqual(reg["versions"][1], other)

    def test_public_view_is_idempotent_without_losing_original_values(self):
        view = registry.public_entry(self.legacy_entry())
        self.assertEqual(registry.public_entry(view), view)
        self.assertEqual(view["legacy_metrics"]["live_annual"], 0.156)

    def test_seed_is_idempotent_and_does_not_migrate_existing_file_values(self):
        reg = {"champions": {"live_strategy": "live-trend-20-20"},
               "versions": [self.legacy_entry()]}
        legacy = deepcopy(reg["versions"][0])
        registry.seed(reg)
        self.assertEqual(reg["versions"][0], legacy)
        self.assertEqual(sum(v["id"] == legacy["id"] for v in reg["versions"]), 1)
        view = registry.public_view(reg)
        self.assertIsNone(view["versions"][0]["metrics"]["live_annual"])

    def test_read_and_public_view_do_not_write_back_registry(self):
        raw = {"champions": {}, "versions": [self.legacy_entry()]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            before = path.read_bytes()
            loaded = registry.load(str(path))
            registry.public_view(loaded)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(loaded["versions"][0]["metrics"]["live_annual"], 0.156)

    def test_formatter_does_not_render_legacy_constants_as_actual_results(self):
        text = registry.fmt_metrics(self.legacy_entry())
        self.assertIn("已作废", text)
        self.assertIn("未知", text)
        self.assertNotIn("15.6%", text)
        self.assertNotIn("14.7%", text)

    def test_formatter_handles_unknown_live_measurements_without_zero_or_error(self):
        text = registry.fmt_metrics({"id": "other", "metrics": {
            "research_annual": 0.04, "live_annual": None,
        }})
        self.assertEqual(text, "研究年化 4.0% · 实际年化 未知")
        missing = registry.fmt_metrics({"id": "other", "metrics": {"research_annual": 0.04}})
        self.assertIn("实际年化 未知", missing)

    def test_cli_list_and_show_use_invalidated_view(self):
        reg = {"updated": None, "champions": {}, "versions": [self.legacy_entry()]}
        output = io.StringIO()
        with redirect_stdout(output):
            registry.cmd_list(reg, argparse.Namespace(all=True, archived=True))
        self.assertIn("口径作废", output.getvalue())
        self.assertNotIn("✅达标", output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            registry.cmd_show(reg, argparse.Namespace(id="live-trend-20-20"))
        shown = json.loads(output.getvalue())
        self.assertFalse(shown["gate"]["passed"])
        self.assertIsNone(shown["metrics"]["live_annual"])
        self.assertEqual(shown["legacy_metrics"]["live_annual"], 0.156)


if __name__ == "__main__":
    unittest.main()
