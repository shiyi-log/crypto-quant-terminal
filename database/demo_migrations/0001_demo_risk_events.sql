-- 0001_demo_risk_events: append-only account risk facts. Missing funding or
-- cash-flow evidence is recorded as incomplete and is never inferred.
CREATE TABLE IF NOT EXISTS demo_risk_events (
    environment TEXT NOT NULL CHECK (environment = 'binance_demo'),
    virtual_account_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp_ms BIGINT NOT NULL CHECK (timestamp_ms >= 0),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (environment, virtual_account_key, event_id)
);
CREATE INDEX IF NOT EXISTS demo_risk_events_account_time ON demo_risk_events
    (environment, virtual_account_key, timestamp_ms, event_id);
