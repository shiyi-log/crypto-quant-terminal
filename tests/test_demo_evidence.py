"""Evidence cannot promote a quoted fee, short hold or local expiry into proof."""
from __future__ import annotations

import copy
from decimal import Decimal, localcontext
import unittest

from demo_evidence import DemoEvidenceError, build_demo_evidence


BINDING = {"strategy_id": "frozen_paper", "rule_hash": "rule", "execution_hash": "execution",
           "demo_policy_hash": "demo-policy"}


def order(client="entry", *, side="buy", reduce_only=False, quantity="1", filled="1",
          status="filled", exchange_id="1", reference="100", attempts=1):
    return {**{key: BINDING[key] for key in ("strategy_id", "rule_hash", "execution_hash")},
            "environment": "binance_demo", "endpoint_host": "demo-fapi.binance.com",
            "virtual_account_key": "demo_default", "client_id": client, "pair": "BTC/USDT:USDT",
            "side": side, "reduce_only": reduce_only, "quantity": quantity,
            "filled_quantity": filled, "status": status, "exchange_order_id": exchange_id,
            "submission_attempts": attempts,
            "intent_payload": {"demo_policy_hash": "demo-policy", "signal_reference_price": reference},
            "audit_observations": []}


def fill(order_row, *, trade="1", price="100", quantity="1", fee="0.02", asset="USDT",
         maker=True, pnl="0", timestamp=1000, quote="100", conflict=False):
    return {"environment": "binance_demo", "virtual_account_key": "demo_default",
            "client_id": order_row["client_id"], "pair": order_row["pair"],
            "exchange_order_id": order_row["exchange_order_id"], "exchange_trade_id": trade,
            "side": order_row["side"], "price": price, "quantity": quantity,
            "commission": fee, "commission_asset": asset, "is_maker": maker,
            "realized_pnl": pnl, "timestamp_ms": timestamp,
            "commission_conflict": conflict, "raw": {} if quote is None else {"quoteQty": quote}}


def snapshot(orders=(), fills=(), errors=()):
    return {"snapshotAt": "2026-10-12T00:00:00Z", "binding": copy.deepcopy(BINDING),
            "orders": list(orders), "fills": list(fills), "submissionErrors": list(errors)}


class DemoEvidenceTests(unittest.TestCase):
    def test_empty_ledger_has_no_measured_fee_net_or_nonfill_ratio(self):
        report = build_demo_evidence({"orders": [], "fills": [], "binding": None})
        questions = report["questions"]
        self.assertEqual(questions["fees"]["state"], "no_actual_fills")
        self.assertIsNone(questions["secondsProfit"]["closedNetTradingPnlUsdt"])
        self.assertIsNone(questions["execution"]["acceptedTerminalNonfillRatio"])
        self.assertFalse(questions["execution"]["liveLaunchAllowed"])

    def test_actual_fee_uses_reported_quote_quantity_without_a_configured_rate(self):
        entry = order()
        row = fill(entry, quote="99.9999", fee="0.03")
        data = snapshot([entry], [row])
        data["fee_rate_per_side"] = "0.0002"
        rate = build_demo_evidence(data)["questions"]["fees"]["rates"][0]
        with localcontext() as context:
            context.prec = 60
            expected = Decimal("0.03") / Decimal("99.9999")
        self.assertEqual(Decimal(rate["effectiveFeeRate"]), expected)
        self.assertEqual(rate["actualReportedQuoteUsdt"], "99.9999")
        self.assertNotEqual(rate["effectiveFeeRate"], "0.0002")

    def test_fee_populations_are_separate_for_role_and_liquidity(self):
        entry = order()
        exit_order = order("exit", side="sell", reduce_only=True, exchange_id="2")
        data = snapshot([entry, exit_order], [fill(entry),
            fill(exit_order, trade="2", maker=False, fee="0.05", timestamp=2000)])
        rates = build_demo_evidence(data)["questions"]["fees"]["rates"]
        self.assertEqual([(row["liquidity"], row["role"], row["effectiveFeeRate"]) for row in rates],
                         [("maker", "entry", "0.0002"), ("taker", "exit", "0.0005")])

    def test_unknown_and_unconverted_fee_fields_cannot_be_zero_rates(self):
        for fields in ({"fee": None}, {"asset": None}, {"conflict": True},
                       {"asset": "BNB", "fee": "0.00001"}, {"quote": None}):
            with self.subTest(fields=fields):
                entry = order()
                report = build_demo_evidence(snapshot([entry], [fill(entry, **fields)]))
                self.assertIsNone(report["questions"]["fees"]["rates"][0]["effectiveFeeRate"])

    def test_reported_zero_fee_remains_known_zero(self):
        entry = order()
        rate = build_demo_evidence(snapshot([entry], [fill(entry, fee="0.00000000")]))["questions"]["fees"]["rates"][0]
        self.assertEqual(Decimal(rate["effectiveFeeRate"]), 0)
        self.assertNotIn("E", rate["effectiveFeeRate"])

    def test_live_signal_reference_differs_from_submitted_limit_reference(self):
        entry = order(reference="100")
        entry["intent_payload"].update(submitted_limit_price="100.5", reference_price="100.5")
        deviation = build_demo_evidence(snapshot([entry], [fill(entry, price="101", quote="101")]))["questions"]["execution"]["signalPriceDeviation"]
        self.assertEqual(Decimal(deviation["meanAdverseDeviationBps"]), 100)
        self.assertIn("includes_environment_and_latency", deviation["scope"])
        entry["intent_payload"].pop("signal_reference_price")
        unknown = build_demo_evidence(snapshot([entry], [fill(entry)]))["questions"]["execution"]["signalPriceDeviation"]
        self.assertIsNone(unknown["meanAdverseDeviationBps"])

    def test_sell_reference_uses_transaction_direction_and_net_does_not_deduct_twice(self):
        entry = order(reference="100")
        exit_order = order("exit", side="sell", reduce_only=True, exchange_id="2", reference="104")
        report = build_demo_evidence(snapshot([entry, exit_order], [
            fill(entry, price="101", quote="101"),
            fill(exit_order, trade="2", price="103", quote="103", maker=False,
                 fee="0.05", pnl="2", timestamp=3500)]))
        profit = report["questions"]["secondsProfit"]
        self.assertEqual(profit["closedNetTradingPnlUsdt"], "1.93")
        self.assertEqual(profit["state"], "seconds_experiment_not_configured")
        self.assertFalse(profit["secondsExperimentConfigured"])
        self.assertFalse(profit["fundingIncluded"])
        self.assertFalse(profit["liveProfitabilityVerified"])

    def test_partial_order_fill_history_cannot_produce_complete_net(self):
        entry = order(quantity="2", filled="2")
        exit_order = order("exit", side="sell", reduce_only=True, exchange_id="2")
        report = build_demo_evidence(snapshot([entry, exit_order], [fill(entry),
            fill(exit_order, trade="2", pnl="1", timestamp=2000)]))
        profit = report["questions"]["secondsProfit"]
        self.assertEqual(profit["unknownNetRoundTripCount"], 1)
        self.assertIsNone(profit["closedNetTradingPnlUsdt"])

    def test_nonfill_denominator_requires_exchange_accepted_terminal_entries(self):
        rows = [order(), order("unfilled", status="canceled", filled="0", exchange_id="2"),
                order("partial", quantity="2", filled="1", status="canceled", exchange_id="3"),
                order("local", status="expired", filled="0", exchange_id=None, attempts=0),
                order("rejected", status="rejected", filled="0", exchange_id=None),
                order("unknown", status="unknown", filled="0", exchange_id=None)]
        error = {**{key: BINDING[key] for key in ("strategy_id", "rule_hash", "execution_hash")},
                 "payload": {"client_id": "rejected", "error_kind": "exchange_rejected"}}
        execution = build_demo_evidence(snapshot(rows, errors=[error]))["questions"]["execution"]
        self.assertEqual(execution["terminalAcceptedEntryCount"], 3)
        self.assertEqual(execution["acceptedTerminalNonfillRatio"], 1 / 3)
        self.assertEqual(execution["partialTerminalAcceptedEntryCount"], 1)
        self.assertEqual(execution["exchangeRejectedEntryCount"], 1)
        self.assertEqual(execution["localTerminalUnconfirmedEntryCount"], 1)
        self.assertEqual(execution["unknownEntryCount"], 1)

    def test_duplicate_wrong_environment_or_identity_fail_closed(self):
        entry = order()
        base = snapshot([entry], [fill(entry)])
        invalid = [copy.deepcopy(base) for _ in range(4)]
        invalid[0]["fills"].append(copy.deepcopy(invalid[0]["fills"][0]))
        invalid[1]["orders"][0]["endpoint_host"] = "fapi.binance.com"
        invalid[2]["orders"][0]["rule_hash"] = "different-rule"
        invalid[3]["orders"][0]["intent_payload"]["demo_policy_hash"] = "different-policy"
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(DemoEvidenceError):
                build_demo_evidence(data)


if __name__ == "__main__":
    unittest.main()
