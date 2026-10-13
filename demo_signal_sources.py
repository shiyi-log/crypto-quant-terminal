"""Dispatch frozen paper mirrors and the independent forward burst experiment."""
from demo_execution_service import PaperEventSource
from demo_price_burst import BURST_DEFINITION_KEY, BURST_STRATEGY_ID, PriceBurstEventSource


class DemoSignalSources:
    def __init__(self, store, *, paper=None, burst=None, clock_ms=None):
        self.store = store
        self.paper = paper or PaperEventSource(store, clock_ms=clock_ms)
        self.burst = burst or PriceBurstEventSource(store, clock_ms=clock_ms)

    def _source(self, strategy_id):
        return self.burst if strategy_id == BURST_STRATEGY_ID else self.paper

    def account(self, strategy_id):
        return self._source(strategy_id).account(strategy_id)

    def latest_sequence(self, strategy_id):
        return self._source(strategy_id).latest_sequence(strategy_id)

    def events(self, strategy_id, after_sequence, limit=1000):
        return self._source(strategy_id).events(strategy_id, after_sequence, limit)

    def closed_candle(self, pair, timeframe, candle_ms=None):
        # Both sources use the same continuously collected, closed futures bars.
        return self.paper.closed_candle(pair, timeframe, candle_ms)

    def additional_accounts(self):
        # Listing is read only. Account bootstrap is an explicit operator
        # action and is never hidden in a dashboard GET. Identity corruption
        # remains an error instead of silently hiding a registered account.
        if self.store.get_document(BURST_DEFINITION_KEY) is None:
            return []
        row = self.burst.account(BURST_STRATEGY_ID)
        return [{"accountId": row["strategy_id"], "strategyId": row["strategy_id"],
                 "ruleHash": row["rule_hash"], "executionHash": row["execution_hash"],
                 "enabled": True, "eligible": True, "blockedReason": None,
                 "description": "价格爆发确认后延续：连续 3 根 5m 达到 ±1.5%",
                 "timeframe": row["execution"]["timeframe"],
                 "source": "forward_burst_actual_closed_candles"}]
