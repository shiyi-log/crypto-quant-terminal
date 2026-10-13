"""The deployment gate audits only requested frozen accounts without writes."""

import copy
from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from tools import check_frozen_strategy_identities as gate
from strategy_lab_service import normalize_account_spec
from strategy_simulation import execution_identity
from tests.test_strategy_frozen_resume import FROZEN_S07, stored_account


class FrozenIdentityGateTests(unittest.TestCase):
    def test_production_flags_match_paired_lab_and_2x2_is_explicit(self):
        production = gate.population_flags()
        self.assertTrue(production["fnew_paired_execution"])
        self.assertFalse(production["risk_execution_2x2"])
        requested = gate.build_requested_specs(production)
        experiment = gate.build_requested_specs(gate.population_flags(risk_execution_2x2=True))
        self.assertEqual(len(requested), 335)
        self.assertEqual(len(experiment), len(requested) + 24)
        self.assertFalse(gate.population_flags("lab-default")["fnew_paired_execution"])
        with self.assertRaises(ValueError):
            gate.population_flags(fnew=False)

    def test_corrupt_archived_rows_are_reported_outside_selected_population(self):
        archived = {"strategy_id": "archived_1x", "definition": {"invalid": True},
                    "rule_hash": "old", "execution_hash": "old"}
        rows = [stored_account(FROZEN_S07), archived]
        before = copy.deepcopy(rows)
        report = gate.audit_frozen_rows([FROZEN_S07], rows)
        self.assertTrue(report["ok"])
        self.assertEqual(report["population"]["selected_frozen"], 1)
        self.assertEqual(report["population"]["skipped"], 1)
        self.assertEqual(report["skipped"], [{"strategy_id": "archived_1x", "group": "legacy",
                                             "reason": "outside_requested_lab_population"}])
        self.assertEqual(rows, before)

    def test_all_selected_failures_are_reported_and_fail_gate(self):
        second = copy.deepcopy(FROZEN_S07)
        second["strategy_id"] += "_bad"
        bad_rule = stored_account(FROZEN_S07)
        bad_rule["rule_hash"] = "bad"
        bad_execution = {"strategy_id": second["strategy_id"], "definition": second,
                         "rule_hash": "bad", "execution_hash": "bad"}
        report = gate.audit_frozen_rows([FROZEN_S07, second], [bad_rule, bad_execution])
        self.assertFalse(report["ok"])
        self.assertEqual({item["strategy_id"] for item in report["failures"] if item["strategy_id"]},
                         {FROZEN_S07["strategy_id"], second["strategy_id"]})
        self.assertEqual(report["population"]["skipped"], 0)

    def test_execution_mismatch_and_conflicting_canonical_alias_fail(self):
        for mutation, code in (({"execution_hash": "bad"}, "stored_execution_hash_mismatch"),
                               ({}, "canonical_signal_hash_mismatch")):
            row = stored_account(FROZEN_S07)
            row.update(mutation)
            if not mutation:
                row["definition"]["canonical_signal_rule_hash"] = "contradictory"
            report = gate.audit_frozen_rows([FROZEN_S07], [row])
            self.assertFalse(report["ok"])
            self.assertIn(code, {item["code"] for item in report["failures"]})

    def test_new_account_is_validated_without_creation_and_require_existing_fails(self):
        report = gate.audit_frozen_rows([FROZEN_S07], [])
        self.assertTrue(report["ok"])
        self.assertEqual(report["new_not_persisted"], [FROZEN_S07["strategy_id"]])
        required = gate.audit_frozen_rows([FROZEN_S07], [], require_existing=True)
        self.assertFalse(required["ok"])
        self.assertEqual(required["failures"][0]["code"], "required_account_not_persisted")

    def test_research_change_is_deferred_by_exact_production_resume_contract(self):
        request = copy.deepcopy(FROZEN_S07)
        request.pop("rule_hash")
        request["params"]["H"] = 5
        request.update(normalize_account_spec(request))
        report = gate.audit_frozen_rows([request], [stored_account(FROZEN_S07)])
        self.assertTrue(report["ok"])
        self.assertTrue(report["resume_contract_passed"])
        self.assertEqual(report["identity_changes_deferred"], [request["strategy_id"]])

    def test_fixed_canonical_fixture_detects_real_hash_regression(self):
        fixture = gate.canonical_fixture()
        normalized = normalize_account_spec(fixture)
        row = {"strategy_id": fixture["strategy_id"], "definition": fixture,
               "rule_hash": fixture["rule_hash"],
               "execution_hash": execution_identity(fixture)["execution_hash"]}
        request = {**fixture, **normalized}
        self.assertTrue(gate.audit_frozen_rows([request], [row])["ok"])
        from strategy_factorial_signals import core
        original = core.normalize_spec
        def changed_inner(spec):
            return {**original(spec), "rule_hash": "a-new-incompatible-canonical-hash"}
        with patch.object(core, "normalize_spec", side_effect=changed_inner):
            report = gate.audit_frozen_rows([request], [row])
        self.assertFalse(report["ok"])
        self.assertIn("canonical_signal_hash_mismatch",
                      {item["code"] for item in report["failures"]})

    def test_all_population_anchors_are_fixed_and_detect_other_family_regressions(self):
        anchors = gate.check_ci_population_anchors()
        self.assertEqual({group: row["count"] for group, row in anchors.items()},
                         {"legacy": 29, "factorial": 261, "fnew": 45, "risk_execution_2x2": 24})
        self.assertTrue(all(row["passed"] for row in anchors.values()))
        real_identity = execution_identity
        def changed_s14(spec):
            identity = real_identity(spec)
            if spec["strategy_id"].startswith("s14_"):
                identity["execution_hash"] = "incompatible-s14-execution"
            return identity
        with patch("strategy_simulation.execution_identity", side_effect=changed_s14):
            anchors = gate.check_ci_population_anchors()
        self.assertFalse(anchors["factorial"]["passed"])
        self.assertFalse(anchors["risk_execution_2x2"]["passed"])
        self.assertTrue(anchors["fnew"]["passed"])

    def test_snapshot_resume_adapter_refuses_writes(self):
        with self.assertRaises(AssertionError):
            gate._SnapshotStore([]).execute("UPDATE strategy_sim_accounts SET definition='{}'", ())

    def test_database_reader_never_uses_datastore_and_starts_read_only(self):
        import psycopg
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchall.return_value = []
        with patch.object(psycopg, "connect", return_value=connection), \
                patch("data_store.DataStore.initialize", side_effect=AssertionError("DDL forbidden")):
            self.assertEqual(gate.read_frozen_rows("postgresql://private"), [])
        commands = [call.args[0] for call in connection.execute.call_args_list]
        self.assertEqual(commands[0], "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        self.assertTrue(str(commands[-1]).startswith("Composed([SQL('SELECT"))

    def test_cli_does_not_expose_connection_error_credentials(self):
        secret = "postgresql://user:secret@private-host/private-db"
        output = io.StringIO()
        with patch.object(gate, "build_requested_specs", return_value=[FROZEN_S07]), \
                patch.object(gate, "read_frozen_rows", side_effect=RuntimeError(secret)), \
                redirect_stdout(output), patch("sys.stderr", io.StringIO()):
            code = gate.main(["--json"])
        self.assertEqual(code, 1)
        self.assertNotIn(secret, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["error_class"], "RuntimeError")

    def test_ci_entry_point_never_opens_database_or_loads_environment(self):
        output = io.StringIO()
        with patch.object(gate, "read_frozen_rows", side_effect=AssertionError("DB forbidden")), \
                patch("runtime_config.load_environment", side_effect=AssertionError("env forbidden")), \
                redirect_stdout(output):
            code = gate.main(["--ci", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["canonical_anchor_passed"])
        self.assertTrue(all(row["passed"] for row in report["population_anchors"].values()))
        self.assertFalse(report["database_queried"])
        self.assertGreater(report["tests_run"], 30)


if __name__ == "__main__":
    unittest.main()
