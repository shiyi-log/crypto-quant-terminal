"""Demo ledger tests; integration refuses every database except quant_test."""
import copy
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from data_store import DataStore
from demo_store import DemoStore, binance_fill, binance_order, scrub_exchange_raw, scrub_payload
from runtime_config import load_environment

load_environment()


def intent(**changes):
    return {"strategy_id": "s01_R50_k1.5", "signal_id": "closed-900000", "intent_key": "entry",
            "rule_hash": "frozen-rule", "execution_hash": "frozen-execution", "input_fingerprint": "closed-input",
            "pair": "BTC/USDT:USDT", "side": "buy", "type": "limit", "quantity": "2",
            "price": "100", "time_in_force": "GTX", "signal_ms": 900000,
            "raw": {"entry_atr": "1.5", "risk_stop_atr_mult": "1.5", "expires_ms": 990000}, **changes}


def observation(status="NEW", filled="0", version=1000, **changes):
    raw = {"orderId": 12, "clientOrderId": "client_1", "symbol": "BTCUSDT", "side": "BUY",
           "type": "LIMIT", "status": status, "origQty": "2", "executedQty": filled, "price": "100.0",
           "avgPrice": "0" if filled == "0" else "100", "time": 990, "updateTime": version, **changes}
    return {"raw": raw}


def fill(trade="7", commission=None, version=1000, **changes):
    return {"exchange_order_id": "12", "exchange_trade_id": trade, "price": "100", "quantity": "1",
            "commission": commission, "commission_asset": "USDT" if commission is not None else None,
            "timestamp_ms": 1000, "exchange_updated_ms": version, **changes}


class DemoStoreUnitTests(unittest.TestCase):
    def test_construction_has_no_environment_reads_or_database_connection(self):
        data = Mock()
        demo = DemoStore(data, "research-session")
        self.assertEqual(demo.virtual_account_key, "research-session")
        data.assert_not_called()
        data._connection.assert_not_called()

    def test_environment_and_host_fail_closed(self):
        for change in ({"environment": "live"}, {"endpoint_host": "fapi.binance.com"},
                       {"endpoint_host": "testnet.binancefuture.com"}):
            with self.assertRaises(ValueError):
                DemoStore(Mock(), **change)

    def test_strict_exchange_allowlist_preserves_facts_and_removes_credentials(self):
        raw = {"orderId": 9, "commission": "0.02", "commissionAsset": "USDT", "maker": True,
               "apiKey": "SYNTHETIC_KEY", "secret": "SYNTHETIC_SECRET", "headers": {"X-MBX-APIKEY": "SYNTHETIC_KEY"},
               "request": "https://demo-fapi.binance.com/fapi/v1/order?signature=SYNTHETIC_SIGNATURE"}
        self.assertEqual(scrub_exchange_raw(raw), {"orderId": 9, "commission": "0.02", "commissionAsset": "USDT", "maker": True})
        self.assertEqual(scrub_exchange_raw({"info": raw}), scrub_exchange_raw(raw))

    def test_payload_scrubs_nested_keys_urls_and_quoted_error_strings(self):
        payload = {"entry_atr": 14, "api_key": "SYNTHETIC_KEY", "reason":
                   'request failed: {"apiKey":"SYNTHETIC_KEY", "secret": "SYNTHETIC_SECRET"} Basic ABC123',
                   "url": "https://u:p@demo-fapi.binance.com/fapi/v1/order?signature=SYNTHETIC_SIGNATURE"}
        cleaned = str(scrub_payload(payload))
        for secret in ("SYNTHETIC_KEY", "SYNTHETIC_SECRET", "SYNTHETIC_SIGNATURE", "ABC123", "u:p"):
            self.assertNotIn(secret, cleaned)
        self.assertEqual(scrub_payload(payload)["entry_atr"], 14)

    def test_restricted_pairs_invalid_quantities_and_oversize_are_rejected_before_db(self):
        demo = DemoStore(Mock())
        for change in ({"pair": "SOLUSDT"}, {"quantity": "NaN"}, {"quantity": "0"},
                       {"type": "limit", "price": None}, {"raw": {"evidence": "x" * 300000}}):
            with self.assertRaises((ValueError, TypeError)):
                demo.reserve_order(intent(**change))
        demo.store._connection.assert_not_called()

    def test_invalid_comparison_view_is_rejected_before_database_reads(self):
        demo = DemoStore(Mock())
        for view in ("unknown", "", "SIGNALS", [], None):
            with self.subTest(view=view):
                with self.assertRaisesRegex(ValueError, "invalid comparison view"):
                    demo.list_comparisons(view=view)
        demo.store._connection.assert_not_called()


@unittest.skipUnless(os.getenv("QUANT_TEST_DATABASE_URL"), "independent quant_test database required")
class DemoStoreIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg.conninfo import conninfo_to_dict
        config = conninfo_to_dict(os.environ["QUANT_TEST_DATABASE_URL"])
        if config.get("dbname") != "quant_test":
            raise RuntimeError("Demo tests may write only independent quant_test database")
        production = os.environ.get("QUANT_DATABASE_URL")
        if production:
            live = conninfo_to_dict(production)
            if tuple(config.get(k, "") for k in ("host", "port", "dbname")) == tuple(live.get(k, "") for k in ("host", "port", "dbname")):
                raise RuntimeError("test database must differ from production")
        cls.schema = "demo_test_" + uuid.uuid4().hex
        cls.data = DataStore(dsn=os.environ["QUANT_TEST_DATABASE_URL"], schema=cls.schema)
        cls.demo = DemoStore(cls.data, "session_a")
        cls.demo.ensure_schema()

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql
        cls.data.close()
        with psycopg.connect(os.environ["QUANT_TEST_DATABASE_URL"]) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        with self.data._connection() as conn:
            conn.execute("TRUNCATE demo_fills,demo_orders,demo_positions,demo_account,demo_signal_comparisons")

    def reserve(self, **changes):
        row, created = self.demo.reserve_order(intent(client_id="client_1", **changes))
        self.assertTrue(created)
        return row

    def test_schema_install_is_explicit_idempotent_and_existing_ledger_untouched(self):
        self.demo.ensure_schema()
        with self.data._connection() as conn:
            existing = conn.execute("SELECT COUNT(*) AS n FROM strategy_sim_accounts").fetchone()["n"]
        self.assertEqual(existing, 0)

    def test_reservation_commits_frozen_intent_and_exact_retry_does_not_duplicate(self):
        row = self.reserve()
        self.assertEqual(row["status"], "intent")
        self.assertEqual(row["intent_payload"]["entry_atr"], "1.5")
        duplicate, created = self.demo.reserve_order(intent(client_id="client_1"))
        self.assertFalse(created)
        self.assertEqual(row, duplicate)
        self.assertEqual(len(self.demo.runner_orders()), 1)
        with self.assertRaisesRegex(ValueError, "different frozen"):
            self.demo.reserve_order(intent(client_id="client_1", quantity="3"))

    def test_canonical_pair_alias_cannot_duplicate_exchange_identity(self):
        first, made = self.demo.reserve_order(intent(pair="BTCUSDT"))
        second, new = self.demo.reserve_order(intent())
        self.assertTrue(made)
        self.assertFalse(new)
        self.assertEqual(first["client_id"], second["client_id"])
        self.assertEqual(second["pair"], "BTC/USDT:USDT")

    def test_concurrent_submission_claim_has_exactly_one_winner(self):
        self.reserve()
        with ThreadPoolExecutor(max_workers=4) as executor:
            claims = list(executor.map(lambda _: self.demo.claim_submission("client_1", 1000), range(8)))
        self.assertEqual(sum(claims), 1)
        self.assertEqual(self.demo.get_order("client_1")["submission_attempts"], 1)

    def test_unknown_submission_survives_restart_and_never_resubmits(self):
        self.reserve()
        self.assertTrue(self.demo.claim_submission("client_1", 1000))
        self.demo.mark_submission_unknown("client_1", "timeout", 1001)
        reopened = DemoStore(self.data, "session_a")
        self.assertFalse(reopened.claim_submission("client_1", 1002))
        self.assertEqual(reopened.pending_orders()[0]["status"], "unknown")
        row = reopened.record_order_observation("client_1", observation())
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["intent_payload"]["expires_ms"], 990000)

    def test_absent_unknown_terminalizes_only_after_threshold_and_flat_snapshots(self):
        self.reserve()
        self.assertTrue(self.demo.claim_submission("client_1", 1000))
        self.demo.mark_submission_unknown("client_1", "timeout", 1001)
        for observed in (2000, 3000, 4000):
            self.demo.record_absent_submission_lookup("client_1", "OrderNotFound", observed)
        still_unknown = self.demo.finalize_absent_submission(
            "client_1", observed_ms=301001,
            exchange_position_flat=False, exchange_open_order_absent=True)
        self.assertEqual(still_unknown["status"], "unknown")
        finalized = self.demo.finalize_absent_submission(
            "client_1", observed_ms=301001,
            exchange_position_flat=True, exchange_open_order_absent=True)
        self.assertEqual(finalized["status"], "abandoned_after_absent_lookup")
        self.assertEqual(finalized["filled_quantity"], "0E-18")
        self.assertEqual(finalized["audit_observations"][-1]["kind"],
                         "submission_unknown_terminalized")

    def test_order_observations_validate_echoed_identity(self):
        self.reserve()
        for change in ({"clientOrderId": "other"}, {"symbol": "ETHUSDT"}, {"side": "SELL"},
                       {"origQty": "3"}, {"type": "MARKET"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "conflicts"):
                self.demo.record_order_observation("client_1", observation(**change))
        self.assertIsNone(self.demo.get_order("client_1")["exchange_order_id"])

    def test_partial_fill_latest_projection_never_regresses_terminal_or_cumulative(self):
        self.reserve()
        row = self.demo.record_order_observation("client_1", observation("PARTIALLY_FILLED", "1", 2000))
        self.assertEqual(row["status"], "partially_filled")
        row = self.demo.record_order_observation("client_1", observation("NEW", "0", 1000))
        self.assertEqual(row["filled_quantity"], "1.000000000000000000")
        self.demo.record_order_observation("client_1", observation("FILLED", "2", 3000))
        row = self.demo.record_order_observation("client_1", observation("NEW", "2", 4000))
        self.assertEqual(row["status"], "filled")
        self.assertEqual(len(row["audit_observations"]), 4)
        self.assertFalse(row["audit_observations"][-1]["projection_accepted"])

    def test_postonly_expired_in_match_preserves_original_binance_status(self):
        self.reserve()
        row = self.demo.record_order_observation("client_1", observation("EXPIRED_IN_MATCH"))
        self.assertEqual(row["status"], "expired")
        exported = binance_order(row)
        self.assertEqual(exported["status"], "EXPIRED_IN_MATCH")
        self.assertEqual(exported["time"], 990)
        self.assertEqual(exported["avgPrice"], "0")
        self.assertEqual(exported["price"], "100.0")
        self.assertIsInstance(exported["origQty"], str)

    def test_fill_actual_unknown_fees_are_null_and_exact_retry_idempotent(self):
        self.reserve()
        first = self.demo.record_fill("client_1", fill())
        second = self.demo.record_fill("client_1", fill())
        self.assertEqual(first, second)
        self.assertIsNone(first["commission"])
        self.assertIsNone(first["is_maker"])
        self.assertIsNone(binance_fill(first)["commission"])
        self.assertEqual(len(self.demo.runner_fills()), 1)
        metrics = self.demo.metrics()
        self.assertIsNone(metrics["actual_commission_by_asset"][0]["commission"])

    def test_partial_unknown_commission_amount_and_asset_are_independently_preserved(self):
        self.reserve()
        amount_only = self.demo.record_fill("client_1", fill(commission="0.02", commission_asset=None))
        asset_only = self.demo.record_fill("client_1", fill(trade="8", commission=None, commission_asset="USDT"))
        self.assertEqual(amount_only["commission"], "0.020000000000000000")
        self.assertIsNone(amount_only["commission_asset"])
        self.assertEqual(asset_only["commission_asset"], "USDT")
        self.assertIsNone(asset_only["commission"])
        self.assertEqual(binance_fill(amount_only)["commission"], "0.020000000000000000")
        self.assertIsNone(binance_fill(asset_only)["commission"])
        metrics = self.demo.metrics()
        self.assertEqual(metrics["fill_count"], 2)
        self.assertEqual(metrics["unknown_commission_count"], 1)
        self.assertEqual(metrics["unknown_commission_asset_count"], 1)
        self.assertFalse(metrics["commission_complete"])
        self.assertTrue(all(row["commission"] is None for row in metrics["actual_commission_by_asset"]))
        # Same-version reconciliation supplements each missing field.
        first = self.demo.record_fill("client_1", fill(commission=None, commission_asset="USDT"))
        second = self.demo.record_fill("client_1", fill(trade="8", commission="0.02"))
        self.assertEqual(first["commission"], "0.020000000000000000")
        self.assertEqual(second["commission_asset"], "USDT")
        metrics = self.demo.metrics()
        self.assertTrue(metrics["commission_complete"])
        self.assertEqual(metrics["actual_commission_by_asset"][0]["commission"], "0.040000000000000000")

    def test_named_v2_migration_removes_only_legacy_fee_coupling_check(self):
        with self.data._connection() as conn:
            conn.execute("ALTER TABLE demo_fills ADD CONSTRAINT legacy_demo_fee_coupling "
                         "CHECK ((commission IS NULL)=(commission_asset IS NULL))")
        self.demo.ensure_schema()
        with self.data._connection() as conn:
            constraints = conn.execute("SELECT conname FROM pg_constraint WHERE conrelid='demo_fills'::regclass AND contype='c'").fetchall()
        names = {row["conname"] for row in constraints}
        self.assertNotIn("legacy_demo_fee_coupling", names)
        self.assertIn("demo_fills_quantity_check", names)
        self.reserve()
        self.demo.record_fill("client_1", fill(commission=None, commission_asset="USDT"))

    def test_unknown_incoming_fee_fields_never_erase_known_and_same_version_conflicts_are_audited(self):
        self.reserve()
        self.demo.record_fill("client_1", fill(commission="0.02"))
        unknown = self.demo.record_fill("client_1", fill(commission=None, version=2000))
        self.assertEqual(unknown["commission"], "0.020000000000000000")
        self.assertEqual(unknown["commission_asset"], "USDT")
        conflict = self.demo.record_fill("client_1", fill(commission="0.03", commission_asset="BNB", version=2000))
        self.assertEqual(conflict["commission"], "0.020000000000000000")
        self.assertEqual(conflict["commission_asset"], "USDT")
        self.assertEqual(len(conflict["audit_observations"]), 3)

    def test_abandoned_never_submitted_intent_is_internal_only_and_not_unfilled_exchange_sample(self):
        self.reserve()
        self.assertTrue(self.demo.abandon_unclaimed_order("client_1", "TTL reached", 1000))
        self.assertFalse(self.demo.abandon_unclaimed_order("client_1", "TTL reached", 1001))
        self.assertFalse(self.demo.claim_submission("client_1", 1001))
        row = self.demo.get_order("client_1")
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["raw"], {})
        self.assertIsNone(binance_order(row)["status"])
        self.assertIsNone(binance_order(row)["time"])
        self.assertEqual(self.demo.metrics()["terminal_entry_count"], 0)
        self.assertIsNone(self.demo.metrics()["unfilled_entry_ratio"])

    def test_abandon_does_not_expire_submitting_or_unknown_exchange_orders(self):
        self.reserve()
        self.assertTrue(self.demo.claim_submission("client_1", 1000))
        self.assertFalse(self.demo.abandon_unclaimed_order("client_1", "paused", 1001))
        self.demo.mark_submission_unknown("client_1", "timeout", 1002)
        self.assertFalse(self.demo.abandon_unclaimed_order("client_1", "TTL reached", 2000))
        self.assertEqual(self.demo.get_order("client_1")["status"], "unknown")

    def test_partial_currency_change_does_not_synthesize_fee_and_complete_reconciliation_resolves_it(self):
        self.reserve()
        self.demo.record_fill("client_1", fill(commission="0.02"))
        partial = self.demo.record_fill("client_1", fill(commission=None, commission_asset="BNB", version=2000))
        self.assertTrue(partial["commission_conflict"])
        self.assertEqual(partial["commission_asset"], "USDT")
        exported = binance_fill(partial)
        self.assertIsNone(exported["commission"])
        self.assertEqual(exported["commissionAsset"], "BNB")
        self.assertFalse(self.demo.metrics()["commission_complete"])
        self.assertIsNone(self.demo.metrics()["actual_commission_by_asset"][0]["commission"])
        self.assertIsNone(self.demo.metrics()["actual_commission_by_asset"][0]["known_commission_subtotal"])
        # Complete real evidence at the same reconciliation version supersedes
        # the old coherent pair, which was deliberately preserved as history.
        resolved = self.demo.record_fill("client_1", fill(commission="0.0001", commission_asset="BNB", version=2000))
        self.assertFalse(resolved["commission_conflict"])
        self.assertEqual(resolved["commission_asset"], "BNB")
        self.assertEqual(binance_fill(resolved)["commission"], "0.000100000000000000")
        self.assertTrue(self.demo.metrics()["commission_complete"])

    def test_unlabelled_new_amount_is_partial_and_existing_fill_blocks_submission_claim(self):
        self.reserve()
        self.demo.record_fill("client_1", fill(commission="0.02"))
        partial = self.demo.record_fill("client_1", fill(commission="0.03", commission_asset=None, version=2000))
        self.assertTrue(partial["commission_conflict"])
        self.assertEqual(partial["commission"], "0.020000000000000000")
        self.assertEqual(binance_fill(partial)["commission"], "0.03")
        self.assertIsNone(binance_fill(partial)["commissionAsset"])
        self.assertFalse(self.demo.claim_submission("client_1", 3000))
        self.assertFalse(self.demo.abandon_unclaimed_order("client_1", "paused", 3000))
        self.assertEqual(self.demo.get_order("client_1")["submission_attempts"], 0)

    def test_commission_enrichment_same_version_and_later_updates_preserve_history(self):
        self.reserve()
        self.demo.record_fill("client_1", fill())
        known = self.demo.record_fill("client_1", fill(commission="0.02", is_maker=True))
        self.assertEqual(known["commission"], "0.020000000000000000")
        later = self.demo.record_fill("client_1", fill(commission="0.03", version=2000))
        old = self.demo.record_fill("client_1", fill(commission="0.01", version=500, is_maker=False))
        self.assertEqual(old["commission"], later["commission"])
        self.assertTrue(old["is_maker"])
        self.assertEqual(len(old["audit_observations"]), 4)
        self.assertEqual(binance_fill(old)["commission"], "0.030000000000000000")

    def test_fill_immutable_price_and_quantity_cannot_be_rewritten(self):
        self.reserve()
        self.demo.record_fill("client_1", fill())
        for change in ({"price": "101"}, {"quantity": "0.9"}, {"timestamp_ms": 1001}):
            with self.assertRaisesRegex(ValueError, "immutable"):
                self.demo.record_fill("client_1", fill(**change))
        self.assertEqual(len(self.demo.list_fills()), 1)

    def test_fill_pair_side_order_and_cumulative_quantity_are_checked(self):
        self.reserve()
        for change in ({"pair": "ETHUSDT"}, {"side": "sell"}, {"quantity": "3"}):
            with self.assertRaises(ValueError):
                self.demo.record_fill("client_1", fill(**change))
        self.demo.record_fill("client_1", fill())
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.demo.record_fill("client_1", fill(exchange_order_id="13", trade="8"))
        self.demo.record_fill("client_1", fill(trade="8"))
        with self.assertRaisesRegex(ValueError, "exceed"):
            self.demo.record_fill("client_1", fill(trade="9"))
        self.assertEqual(self.demo.net_fill_positions(), {"BTC/USDT:USDT": "2.000000000000000000"})

    def test_snapshots_are_immutable_observations_not_positions_autoclose(self):
        before = {"positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}], "apiKey": "SYNTHETIC_KEY"}
        first = self.demo.append_position_snapshot(before, 1000)
        self.assertEqual(self.demo.append_position_snapshot(copy.deepcopy(before), 1000), first)
        self.demo.append_position_snapshot({"positions": []}, 2000)
        self.demo.append_account_snapshot({"totalWalletBalance": "1000"}, 2000)
        self.assertEqual(self.demo.get_latest_position_snapshot()["payload"], {"positions": []})
        self.assertEqual(self.demo.get_latest_account_snapshot()["payload"]["totalWalletBalance"], "1000")
        with self.data._connection() as conn:
            rows = conn.execute("SELECT payload FROM demo_positions ORDER BY observed_ms").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotIn("apiKey", rows[0]["payload"])
        self.assertEqual(self.demo.list_orders(), [])

    def test_frozen_bindings_cannot_be_replaced_or_bypassed(self):
        binding = {key: intent()[key] for key in ("strategy_id", "signal_id", "rule_hash", "execution_hash", "input_fingerprint")}
        self.demo.bind_account(binding)
        self.assertEqual(self.demo.bind_account(binding), binding)
        self.assertEqual(self.demo.get_binding(), binding)
        with self.assertRaisesRegex(ValueError, "different session"):
            self.demo.bind_account({**binding, "rule_hash": "another"})
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.demo.append_comparison({**binding, "rule_hash": "another"}, "binding", 1000)

    def test_comparison_is_same_frozen_signal_and_virtual_accounts_are_isolated(self):
        comparison = {key: intent()[key] for key in ("strategy_id", "signal_id", "rule_hash", "execution_hash", "input_fingerprint")}
        comparison["paper_entry_price"] = "100"
        self.demo.append_comparison(comparison, "paper_signal", 1000)
        self.demo.append_comparison({**comparison, "demo_fill_price": "100.1"}, "actual_fill", 2000)
        rows = self.demo.list_comparisons(signal_id=comparison["signal_id"])
        self.assertEqual([row["stage"] for row in rows], ["actual_fill", "paper_signal"])
        other = DemoStore(self.data, "session_b")
        self.reserve()
        self.assertEqual(other.runner_orders(), [])
        self.assertEqual(other.list_comparisons(), [])
        self.assertIsNone(other.get_binding())

    def test_signal_focus_filters_before_limit_without_removing_audit_or_error_evidence(self):
        comparison = {key: intent()[key] for key in
                      ("strategy_id", "signal_id", "rule_hash", "execution_hash", "input_fingerprint")}
        self.demo.bind_account(comparison)
        for observed_ms in range(2000, 2101):
            self.demo.append_comparison(comparison, "runner_checkpoint", observed_ms)
        self.demo.append_comparison(comparison, "actual_execution_config", 2200)
        for stage, observed_ms in (("paper_event", 1000), ("snapshot_error", 1100),
                                  ("future_business_event", 1200)):
            self.demo.append_comparison(comparison, stage, observed_ms)
        other = DemoStore(self.data, "session_b")
        other.append_comparison(comparison, "paper_event", 1300)

        # More than one page of newer routine rows must not hide older signals.
        focused = self.demo.list_comparisons(view="signals", limit=2)
        self.assertEqual([row["stage"] for row in focused], ["future_business_event", "snapshot_error"])
        self.assertTrue(all(row["virtual_account_key"] == "session_a" for row in focused))
        self.assertEqual([row["stage"] for row in self.demo.list_comparisons(
            view="signals", signal_id=comparison["signal_id"], limit=100)],
            ["future_business_event", "snapshot_error", "paper_event"])
        self.assertEqual(self.demo.list_comparisons(view="signals", stage="runner_checkpoint"), [])
        self.assertEqual(len(self.demo.list_comparisons(stage="runner_checkpoint")), 101)
        audit = self.demo.list_comparisons()
        self.assertEqual(len(audit), 106)
        self.assertEqual(audit[0]["stage"], "binding")
        self.assertEqual(self.demo.list_comparisons(view="audit"), audit)

    def test_switch_gate_includes_pending_orders_in_other_namespaces(self):
        self.assertTrue(self.demo.account_reconciliation_status()["safeToSwitch"])
        other = DemoStore(self.data, "manual_smoke_offline_other")
        other.reserve_order(intent(client_id="other_1"))
        for status in ("intent", "submitting", "unknown", "open", "partially_filled"):
            with self.subTest(status=status):
                with self.data._connection() as conn:
                    conn.execute("UPDATE demo_orders SET status=%s WHERE virtual_account_key=%s",
                                 (status, other.virtual_account_key))
                facts = self.demo.account_reconciliation_status()
                self.assertFalse(facts["safeToSwitch"])
                self.assertEqual(facts["pendingOrderCount"], 1)
                self.assertEqual(self.demo.pending_orders(), [])

    def test_switch_gate_does_not_net_opposing_session_positions(self):
        other = DemoStore(self.data, "second_level_offline_other")

        def traded(ledger, client, side, number):
            ledger.reserve_order(intent(client_id=client, signal_id=client,
                                        quantity="1", side=side))
            ledger.record_order_observation(client, observation("FILLED", "1", origQty="1",
                                                               side=side.upper(), orderId=number,
                                                               clientOrderId=client))
            ledger.record_fill(client, fill(exchange_order_id=str(number), trade=str(number),
                                           side=side))

        traded(self.demo, "buy_1", "buy", 101)
        traded(other, "sell_1", "sell", 102)
        facts = self.demo.account_reconciliation_status()
        self.assertFalse(facts["safeToSwitch"])
        self.assertEqual(facts["nonflatLedgerPairCount"], 2)
        self.assertEqual(facts["fillQuantityGapCount"], 0)
        traded(self.demo, "sell_2", "sell", 103)
        traded(other, "buy_2", "buy", 104)
        self.assertTrue(self.demo.account_reconciliation_status()["safeToSwitch"])
        self.assertIsNone(other.list_fills()[0]["commission"])

    def test_switch_gate_refuses_terminal_orders_with_missing_fill_quantity(self):
        self.reserve()
        self.demo.record_order_observation("client_1", observation("CANCELED", "1"))
        facts = self.demo.account_reconciliation_status()
        self.assertFalse(facts["safeToSwitch"])
        self.assertEqual(facts["pendingOrderCount"], 0)
        self.assertEqual(facts["fillQuantityGapCount"], 1)

    def test_strategy_session_history_excludes_manual_experiments_and_keeps_bindings(self):
        binding = {key: intent()[key] for key in
                   ("strategy_id", "signal_id", "rule_hash", "execution_hash", "input_fingerprint")}
        namespaces = ("demo_default", "demo_strategy_" + "a" * 32, "manual_smoke_offline")
        for index, key in enumerate(namespaces):
            DemoStore(self.data, key).bind_account({**binding, "observed_ms": 1000 + index,
                                                   "forward_boundary_ms": 1000 + index})
        sessions = self.demo.list_strategy_sessions()
        self.assertEqual([row["virtualAccountKey"] for row in sessions],
                         [namespaces[1], namespaces[0]])
        self.assertEqual(sessions[0]["forwardBoundaryMs"], 1001)
        with self.assertRaisesRegex(ValueError, "different session"):
            DemoStore(self.data, namespaces[0]).bind_account({**binding, "strategy_id": "other"})

    def test_reconciliation_is_append_only_and_metrics_use_terminal_entry_denominator(self):
        self.reserve()
        self.demo.record_order_observation("client_1", observation("PARTIALLY_FILLED", "1"))
        self.demo.record_fill("client_1", fill(commission="0.02", is_maker=True))
        first = self.demo.append_reconciliation("client_1", {"lookup": "found", "known_fills": 1}, 1100)
        self.assertEqual(self.demo.append_reconciliation("client_1", {"lookup": "found", "known_fills": 1}, 1100), first)
        row = self.demo.get_order("client_1")
        self.assertEqual(row["status"], "partially_filled")
        self.assertEqual(row["audit_observations"][-1]["kind"], "reconciliation")
        self.assertIsNone(self.demo.metrics()["unfilled_entry_ratio"])
        self.demo.record_order_observation("client_1", observation("CANCELED", "1", 2000))
        metrics = self.demo.metrics()
        self.assertEqual(metrics["terminal_entry_count"], 1)
        self.assertEqual(metrics["unfilled_entry_count"], 0)
        self.assertEqual(metrics["unfilled_entry_ratio"], 0)
        self.assertEqual(metrics["fill_count"], 1)
        self.assertTrue(metrics["has_exposure"])
        self.assertIsNone(metrics["median_latency_ms"])

    def test_adapter_native_order_and_fill_contract_roundtrips(self):
        from binance_demo_adapter import normalize_fill, normalize_order
        self.reserve()
        order_raw = observation("PARTIALLY_FILLED", "1")["raw"]
        order = self.demo.record_order_observation("client_1", normalize_order(order_raw, "BTC/USDT:USDT"))
        self.assertEqual(order["status"], "partially_filled")
        trade_raw = {"id": 7, "orderId": 12, "symbol": "BTCUSDT", "side": "BUY", "price": "100.0",
                     "qty": "1.000", "quoteQty": "100.000", "commission": "0.02", "commissionAsset": "USDT",
                     "realizedPnl": "0", "maker": True, "buyer": True, "time": 1000}
        trade = self.demo.record_fill("client_1", normalize_fill(trade_raw, "BTC/USDT:USDT"))
        exported = binance_fill(trade)
        for key in ("id", "orderId", "quoteQty", "buyer", "maker", "time"):
            self.assertEqual(exported[key], trade_raw[key])
        self.assertEqual(exported["commissionAsset"], "USDT")
