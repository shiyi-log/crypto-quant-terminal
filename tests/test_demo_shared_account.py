"""One-way ownership gates, without credentials, HTTP or a database."""
import copy
from decimal import Decimal
from types import SimpleNamespace
import unittest

from demo_shared_account import SharedDemoAccount

BTC, ETH = "BTC/USDT:USDT", "ETH/USDT:USDT"


def order(namespace="s35", *, pair=BTC, status="filled", reported="1", recorded="1",
          side="buy", client=None, reduce=False):
    return {"virtual_account_key": namespace, "pair": pair, "status": status,
            "filled_quantity": reported, "recorded_fill_quantity": recorded,
            "side": side, "client_id": client or namespace + "-entry",
            "reduce_only": reduce, "exchange_order_id": client or namespace + "-order"}


class History:
    def __init__(self):
        self.orders = []

    def shared_account_ownership(self):
        positions = {}
        for row in self.orders:
            owned = positions.setdefault(row["virtual_account_key"], {})
            signed = Decimal(row["recorded_fill_quantity"]) * (1 if row["side"] == "buy" else -1)
            owned[row["pair"]] = owned.get(row["pair"], Decimal(0)) + signed
        return {"orders": copy.deepcopy(self.orders), "net_positions": positions}


class SharedAccountTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.shared = SharedDemoAccount(self.history)
        for namespace in ("s35", "s07"):
            self.shared.register(SimpleNamespace(config=SimpleNamespace(virtual_account_key=namespace),
                                                store=SimpleNamespace(virtual_account_key=namespace)))

    def test_filled_position_blocks_both_same_direction_and_opposite_new_strategy(self):
        self.history.orders = [order()]
        self.assertEqual(self.shared.entry_denial("s07", BTC), "shared_pair_busy")
        self.assertIsNone(self.shared.entry_denial("s07", ETH))
        self.assertFalse(self.shared.exit_allowed("s07", BTC, "1"))
        self.assertTrue(self.shared.exit_allowed("s35", BTC, "1"))

    def test_every_nonterminal_intent_holds_pair_even_without_fills(self):
        for status in ("intent", "submitting", "unknown", "open", "partially_filled"):
            with self.subTest(status=status):
                self.history.orders = [order(status=status, reported="0", recorded="0")]
                self.assertEqual(self.shared.entry_denial("s07", BTC), "shared_pair_busy")

    def test_canceled_partial_or_terminal_quantity_gap_never_releases_pair(self):
        for reported, recorded in (("1", "1"), ("1", "0"), ("0", "1")):
            with self.subTest(reported=reported, recorded=recorded):
                self.history.orders = [order(status="canceled", reported=reported, recorded=recorded)]
                self.assertEqual(self.shared.entry_denial("s07", BTC), "shared_pair_busy")

    def test_complete_round_trip_releases_pair_and_pnl_stays_namespace_scoped(self):
        self.history.orders = [order(), order(side="sell", client="s35-exit", reduce=True)]
        self.assertIsNone(self.shared.entry_denial("s07", BTC))
        self.assertEqual(self.shared.ownership_conflicts([], []), [])
        self.shared.unregister("s35")
        self.assertEqual(self.shared.status()["members"], ["s07"])

    def test_account_net_zero_does_not_hide_opposing_namespace_positions(self):
        self.history.orders = [order(), order("s07", side="sell")]
        self.assertIn("shared_pair_multiple_owners:" + BTC, self.shared.ownership_conflicts([], []))
        self.assertFalse(self.shared.exit_allowed("s35", BTC, "0"))

    def test_foreign_namespace_and_manual_orders_block_entries_but_other_pair_can_protect(self):
        self.history.orders = [order("manual"), order("s07", pair=ETH)]
        positions = [{"pair": BTC, "signed_qty": "1"}, {"pair": ETH, "signed_qty": "1"}]
        conflicts = self.shared.ownership_conflicts(positions, [{"client_id": "not-in-ledger"}])
        self.assertIn("unregistered_demo_owner:" + BTC, conflicts)
        self.assertIn("unowned_open_order", conflicts)
        self.assertTrue(self.shared.exit_allowed("s07", ETH, "1"))

    def test_exit_requires_exact_exchange_quantity_and_fully_canceled_residual_entry(self):
        self.history.orders = [order(status="partially_filled")]
        self.assertFalse(self.shared.exit_allowed("s35", BTC, "1"))
        self.history.orders[0]["status"] = "canceled"
        self.assertFalse(self.shared.exit_allowed("s35", BTC, "2"))
        self.assertFalse(self.shared.exit_allowed("s35", BTC, "-1"))
        self.assertTrue(self.shared.exit_allowed("s35", BTC, "1"))

    def test_registered_other_strategy_positions_and_orders_are_recognized(self):
        self.history.orders = [order(), order("s07", pair=ETH, status="open", reported="0", recorded="0")]
        self.assertEqual(self.shared.assert_attach_safe([{"pair": BTC, "signed_qty": "1"}],
            [{"client_id": "s07-entry", "pair": ETH, "order_id": "s07-order"}]), [])

    def test_known_client_cannot_adopt_wrong_pair_or_exchange_order_identity(self):
        self.history.orders = [order(status="open", reported="0", recorded="0")]
        for facts in ({"pair": ETH, "order_id": "s35-order"}, {"pair": BTC, "order_id": "different-order"}):
            with self.subTest(facts=facts):
                self.assertIn("shared_open_order_identity_mismatch", self.shared.ownership_conflicts([], [
                    {"client_id": "s35-entry", **facts}]))

    def test_duplicate_client_or_exchange_identity_is_never_silently_attributed(self):
        self.history.orders = [order(), order("s07", pair=ETH, client="s35-entry")]
        self.assertIn("shared_client_identity_collision", self.shared.ownership_conflicts([], []))
        self.history.orders[1]["pair"] = BTC
        self.history.orders[1]["client_id"] = "s07-entry"
        self.history.orders[1]["exchange_order_id"] = self.history.orders[0]["exchange_order_id"]
        self.assertIn("shared_exchange_identity_collision", self.shared.ownership_conflicts([], []))

    def test_unknown_order_and_gap_are_explicit_and_members_with_exposure_cannot_detach(self):
        self.history.orders = [order(status="unknown", reported="1", recorded="0")]
        conflicts = self.shared.ownership_conflicts([], [])
        self.assertIn("unknown_submission", conflicts)
        self.assertIn("shared_fill_quantity_gap:" + BTC, conflicts)
        with self.assertRaisesRegex(ValueError, "still owns"):
            self.shared.unregister("s35")
        self.assertFalse(self.shared.status()["exchange_position_independence"])


if __name__ == "__main__":
    unittest.main()
