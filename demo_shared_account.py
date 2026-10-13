"""One-way Demo account scheduling without mixing strategy positions.

Orders/fills in PostgreSQL are the durable pair reservations. A lease lasts
until every order is terminal, fills reconcile and that namespace is flat.
The exchange still has one position per symbol and one shared margin account.
"""
from __future__ import annotations

from decimal import Decimal
import threading

from demo_store import PENDING_STATUSES

SHARED_ACCOUNT_POLICY = "one_way_dynamic_pair_exclusive_v1"


class SharedDemoAccount:
    def __init__(self, ledger):
        self.ledger = ledger
        self.lock = threading.RLock()
        self._members = {}

    def register(self, service):
        with self.lock:
            namespace = service.config.virtual_account_key
            previous = self._members.get(namespace)
            if previous is not None and previous is not service:
                raise ValueError("shared Demo namespace already registered")
            if service.store.virtual_account_key != namespace:
                raise ValueError("shared Demo ledger identity mismatch")
            self._members[namespace] = service
            service.account_coordinator = self

    def unregister(self, namespace):
        with self.lock:
            state = self._state()
            if any(namespace in owners for owners in state["owners"].values()):
                raise ValueError("shared Demo member still owns orders, fills or gaps")
            self._members.pop(namespace, None)

    def _state(self):
        facts = self.ledger.shared_account_ownership()
        orders = facts["orders"]
        positions = facts["net_positions"]
        owners, gaps = {}, set()
        for namespace, by_pair in positions.items():
            for pair, quantity in by_pair.items():
                if Decimal(str(quantity)):
                    owners.setdefault(pair, set()).add(namespace)
        for row in orders:
            pair, namespace = row["pair"], row["virtual_account_key"]
            gap = Decimal(str(row["filled_quantity"])) != Decimal(str(row["recorded_fill_quantity"]))
            if gap:
                gaps.add(pair)
            if row["status"] in PENDING_STATUSES or gap:
                owners.setdefault(pair, set()).add(namespace)
        return {"orders": orders, "positions": positions, "owners": owners, "gaps": gaps}

    def ownership_conflicts(self, positions, open_orders):
        with self.lock:
            state = self._state()
            conflicts, actual, expected = [], {}, {}
            known = {}
            exchange_ids = {}
            for row in state["orders"]:
                namespace, client = row["virtual_account_key"], row["client_id"]
                if client in known and known[client] != namespace:
                    conflicts.append("shared_client_identity_collision")
                known[client] = namespace
                exchange_id = row.get("exchange_order_id")
                if exchange_id is not None:
                    key = (row["pair"], str(exchange_id))
                    if key in exchange_ids and exchange_ids[key] != (namespace, client):
                        conflicts.append("shared_exchange_identity_collision")
                    exchange_ids[key] = (namespace, client)
                if row["status"] in {"submitting", "unknown"}:
                    conflicts.append("unknown_submission")
            for pair, owners in state["owners"].items():
                if len(owners) > 1:
                    conflicts.append("shared_pair_multiple_owners:" + pair)
                if owners - set(self._members):
                    conflicts.append("unregistered_demo_owner:" + pair)
            for pair in state["gaps"]:
                conflicts.append("shared_fill_quantity_gap:" + pair)
            for namespace, by_pair in state["positions"].items():
                for pair, quantity in by_pair.items():
                    expected[pair] = expected.get(pair, Decimal(0)) + Decimal(str(quantity))
            for position in positions:
                quantity = Decimal(str(position.get("signed_qty", 0)))
                if not quantity.is_finite():
                    raise ValueError("invalid shared Demo position quantity")
                if quantity:
                    pair = position.get("pair") or position.get("symbol") or "unknown_symbol"
                    if position.get("position_side", "BOTH") != "BOTH":
                        conflicts.append("hedge_mode_position")
                    actual[pair] = actual.get(pair, Decimal(0)) + quantity
            for pair in set(expected) | set(actual):
                if expected.get(pair, Decimal(0)) != actual.get(pair, Decimal(0)):
                    conflicts.append("unowned_or_unreconciled_position:" + pair)
            for order in open_orders:
                owner = known.get(order.get("client_id"))
                if owner not in self._members:
                    conflicts.append("unowned_open_order")
            return sorted(set(conflicts))

    def assert_attach_safe(self, positions, open_orders):
        """A healthy existing member may hold an exclusively owned position."""
        return self.ownership_conflicts(positions, open_orders)

    def entry_denial(self, namespace, pair):
        with self.lock:
            if namespace not in self._members:
                return "shared_namespace_not_registered"
            state = self._state()
            if state["owners"].get(pair, set()) - {namespace}:
                return "shared_pair_busy"
            if pair in state["gaps"]:
                return "shared_fill_quantity_gap"
            return None

    def exit_allowed(self, namespace, pair, actual_net):
        with self.lock:
            state = self._state()
            if namespace not in self._members or state["owners"].get(pair, set()) != {namespace}:
                return False
            owned = Decimal(str(state["positions"].get(namespace, {}).get(pair, 0)))
            actual = Decimal(str(actual_net))
            if not actual.is_finite() or not owned or owned != actual or pair in state["gaps"]:
                return False
            # A residual/unknown entry could fill after the exit. Cancel and
            # reconcile it first; another strategy must never inherit that risk.
            return not any(row["pair"] == pair and row["status"] in PENDING_STATUSES
                and not row["reduce_only"] for row in state["orders"])

    def status(self):
        with self.lock:
            state = self._state()
            return {"policy": SHARED_ACCOUNT_POLICY,
                    "exchange_position_independence": False,
                    "shared_margin_account": True,
                    "same_pair_concurrent_strategies": False,
                    "members": sorted(self._members),
                    "pair_owners": {pair: sorted(owners) for pair, owners in state["owners"].items()},
                    "fill_quantity_gap_pairs": sorted(state["gaps"])}
