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
        owners, gaps, identities = {}, set(), {}
        identity_conflicts = set()
        for namespace, by_pair in positions.items():
            for pair, quantity in by_pair.items():
                if Decimal(str(quantity)):
                    owners.setdefault(pair, set()).add(namespace)
        for row in orders:
            pair, namespace = row["pair"], row["virtual_account_key"]
            identity = (namespace, row["client_id"])
            keys = [("client", row["client_id"])]
            if row.get("exchange_order_id") is not None:
                keys.append(("exchange", pair, str(row["exchange_order_id"])))
            for key in keys:
                previous = identities.get(key)
                if previous is not None and previous[0] != identity:
                    identity_conflicts.update((pair, previous[1]))
                identities[key] = (identity, pair)
            gap = Decimal(str(row["filled_quantity"])) != Decimal(str(row["recorded_fill_quantity"]))
            if gap:
                gaps.add(pair)
            if row["status"] in PENDING_STATUSES or gap:
                owners.setdefault(pair, set()).add(namespace)
        return {"orders": orders, "positions": positions, "owners": owners, "gaps": gaps,
                "identity_conflicts": identity_conflicts}

    def ownership_conflicts(self, positions, open_orders):
        with self.lock:
            state = self._state()
            conflicts, actual, expected = [], {}, {}
            known, known_orders = {}, {}
            exchange_ids = {}
            for row in state["orders"]:
                namespace, client = row["virtual_account_key"], row["client_id"]
                if client in known and known[client] != namespace:
                    conflicts.append("shared_client_identity_collision")
                known[client] = namespace
                known_orders[client] = row
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
                client = order.get("client_id")
                owner = known.get(client)
                if owner not in self._members:
                    conflicts.append("unowned_open_order")
                    continue
                recorded = known_orders[client]
                if (order.get("pair") != recorded["pair"]
                        or (recorded.get("exchange_order_id") is not None
                            and str(order.get("order_id")) != str(recorded["exchange_order_id"]))):
                    conflicts.append("shared_open_order_identity_mismatch")
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
            if pair in state["identity_conflicts"]:
                return "shared_order_identity_collision"
            return None

    def exit_allowed(self, namespace, pair, actual_net):
        with self.lock:
            state = self._state()
            if namespace not in self._members or state["owners"].get(pair, set()) != {namespace}:
                return False
            owned = Decimal(str(state["positions"].get(namespace, {}).get(pair, 0)))
            actual = Decimal(str(actual_net))
            if (not actual.is_finite() or not owned or owned != actual or pair in state["gaps"]
                    or pair in state["identity_conflicts"]):
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
                    "fill_quantity_gap_pairs": sorted(state["gaps"]),
                    "order_identity_conflict_pairs": sorted(state["identity_conflicts"])}
