"""HTTP admission and independent, complete actual-fill strategy reporting."""
import json
from unittest import TestCase
from unittest.mock import Mock

from demo_api import DemoApi, DemoApiError, PREFIX, handle_demo_request
from demo_runner_controller import DemoHandoffError


PRIMARY = "demo_strategy_" + "a" * 32
COMPANION = "demo_strategy_" + "b" * 32
ORIGIN = "http://127.0.0.1:8888"


class ParallelHttpTests(TestCase):
    def request(self, suffix, payload, *, origin=ORIGIN, user="admin"):
        handler = Mock()
        handler.path = PREFIX + suffix
        raw = json.dumps(payload).encode()
        handler.headers = {"Origin": origin, "Content-Type": "application/json",
                           "Content-Length": str(len(raw))}
        handler._bearer.return_value = "test-bearer"
        handler._is_local.return_value = True
        handler._read_body.return_value = raw
        handler._send.side_effect = lambda code, result: (code, result)
        provider = Mock()
        provider.return_value.post.return_value = {"environment": "binance_demo"}
        result = handle_demo_request(handler, method="POST", verify_token=lambda _: user,
                                     allowed_origins={ORIGIN}, api_provider=provider)
        return result, handler, provider

    def test_parallel_actions_keep_local_bearer_origin_guard_before_body_read(self):
        for endpoint, payload in [("/attach", {"accountId": "s07"}),
                                  ("/adopt-session", {"session": COMPANION}),
                                  ("/start-session", {"session": COMPANION}),
                                  ("/stop-session", {"session": COMPANION})]:
            for options, status in [({"user": None}, 401),
                                    ({"origin": "https://foreign.invalid"}, 403)]:
                with self.subTest(endpoint=endpoint, options=options):
                    result, handler, provider = self.request(endpoint, payload, **options)
                    self.assertEqual(result[0], status)
                    handler._read_body.assert_not_called()
                    provider.assert_not_called()

    def test_arbitrary_namespace_and_extra_trading_fields_are_rejected(self):
        for endpoint, payload in [("/start-session", {"session": "fee_capture_123"}),
                                  ("/stop-session", {"session": "../private"}),
                                  ("/attach", {"accountId": "s07", "leverage": 999}),
                                  ("/adopt-session", {"session": COMPANION, "start": True})]:
            result, _, provider = self.request(endpoint, payload)
            self.assertEqual(result[0], 400)
            provider.assert_not_called()


def ledger(namespace, strategy, *, gross="5", fee="0.2", incomplete=False):
    result = Mock()
    result.virtual_account_key = namespace
    result.metrics.return_value = {"orders_by_status": {"filled": 2}, "fill_count": 2}
    result.net_fill_positions.return_value = {}
    result.runner_orders.return_value = [{"client_id": namespace + ":in", "filled_quantity": "2" if incomplete else "1"},
                                        {"client_id": namespace + ":out", "filled_quantity": "1"}]
    result.runner_fills.return_value = [
        {"client_id": namespace + ":in", "exchange_trade_id": "1", "pair": "BTC/USDT:USDT",
         "side": "buy", "quantity": "1", "timestamp_ms": 1000,
         "realized_pnl": "0", "commission": "0.1", "commission_asset": "USDT", "is_maker": True},
        {"client_id": namespace + ":out", "exchange_trade_id": "2", "pair": "BTC/USDT:USDT",
         "side": "sell", "quantity": "1", "timestamp_ms": 2000,
         "realized_pnl": gross, "commission": fee, "commission_asset": "USDT", "is_maker": False}]
    return result


class ParallelApiTests(TestCase):
    def setUp(self):
        self.primary = ledger(PRIMARY, "s02")
        self.companion = ledger(COMPANION, "s07", gross="-2", incomplete=True)
        self.primary.list_strategy_sessions.return_value = [
            {"virtualAccountKey": PRIMARY, "strategyId": "s02", "forwardBoundaryMs": 1},
            {"virtualAccountKey": COMPANION, "strategyId": "s07", "forwardBoundaryMs": 2}]
        self.controller = Mock()
        self.controller.status.return_value = {"selectedAccountId": "s02", "entriesEnabled": True,
            "status": "observing", "parallelSessions": [
                {"virtualAccountKey": PRIMARY, "strategyId": "s02", "entriesEnabled": True, "status": "observing"},
                {"virtualAccountKey": COMPANION, "strategyId": "s07", "entriesEnabled": False, "status": "paused_flat"}]}
        self.credentials = Mock()
        self.api = DemoApi(Mock(), self.credentials, self.primary, self.controller,
                           account_provider=lambda: [{"accountId": "s07", "eligible": True}],
                           session_factory=lambda key: self.companion if key == COMPANION else self.primary)

    def test_reports_strategy_net_from_all_fills_and_keeps_incomplete_pnl_unknown(self):
        result = self.api.get("/strategies", limit=7)
        by_key = {item["virtualAccountKey"]: item for item in result["items"]}
        self.assertEqual(by_key[PRIMARY]["metrics"]["closedNetTradingPnlUsdt"], "4.7")
        self.assertIsNone(by_key[COMPANION]["metrics"]["closedNetTradingPnlUsdt"])
        self.assertFalse(by_key[COMPANION]["roundTrips"][0]["fill_reconciliation_complete"])
        self.assertFalse(by_key[PRIMARY]["independentWallet"])
        self.primary.runner_fills.assert_called_once_with()
        self.companion.runner_fills.assert_called_once_with()
        self.credentials.load_credentials.assert_not_called()

    def test_current_member_survives_small_history_limit(self):
        self.primary.list_strategy_sessions.return_value = []
        result = self.api.get("/strategies", limit=1)
        self.assertEqual({r["virtualAccountKey"] for r in result["items"]}, {PRIMARY, COMPANION})

    def test_attach_does_not_implicitly_start_and_fixed_refusals_reach_client(self):
        self.api.post("/attach", {"accountId": "s07"}, actor="admin")
        self.controller.attach.assert_called_once_with("s07")
        self.controller.start.assert_not_called()
        self.controller.start_session.assert_not_called()
        self.controller.start_session.side_effect = DemoHandoffError("demo_shared_recovery_unresolved", "private")
        with self.assertRaises(DemoApiError) as caught:
            self.api.post("/start-session", {"session": COMPANION}, actor="admin")
        self.assertEqual(caught.exception.code, "demo_shared_recovery_unresolved")
        self.assertEqual(caught.exception.status, 409)

    def test_adoption_is_not_a_new_binding_or_start(self):
        self.api.post("/adopt-session", {"session": COMPANION}, actor="admin")
        self.controller.adopt_existing_session.assert_called_once_with(COMPANION)
        self.controller.attach.assert_not_called()
        self.controller.start_session.assert_not_called()
