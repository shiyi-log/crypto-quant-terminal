-- Demo Trading audit ledger. Explicitly installed by DemoStore.ensure_schema().
-- Names deliberately do not overlap the live or internal simulation ledgers.
CREATE TABLE IF NOT EXISTS demo_orders (
    environment TEXT NOT NULL CHECK (environment = 'binance_demo'),
    virtual_account_key TEXT NOT NULL,
    client_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL, signal_id TEXT NOT NULL, intent_key TEXT NOT NULL,
    rule_hash TEXT NOT NULL, execution_hash TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
    endpoint_host TEXT NOT NULL CHECK (endpoint_host = 'demo-fapi.binance.com'),
    pair TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('buy','sell')),
    order_type TEXT NOT NULL CHECK (order_type IN ('limit','market')),
    quantity NUMERIC(38,18) NOT NULL CHECK (quantity > 0),
    price NUMERIC(38,18) CHECK (price > 0),
    time_in_force TEXT, reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    signal_ms BIGINT NOT NULL CHECK (signal_ms >= 0), created_ms BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'intent' CHECK (status IN
      ('intent','submitting','unknown','open','partially_filled','filled','canceled','rejected','expired',
       'abandoned_after_absent_lookup')),
    submission_attempts INTEGER NOT NULL DEFAULT 0 CHECK (submission_attempts BETWEEN 0 AND 1),
    submission_claimed_ms BIGINT, exchange_order_id TEXT,
    filled_quantity NUMERIC(38,18) NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    average_price NUMERIC(38,18) CHECK (average_price > 0),
    exchange_updated_ms BIGINT NOT NULL DEFAULT -1,
    intent_payload JSONB NOT NULL DEFAULT '{}',
    raw JSONB NOT NULL DEFAULT '{}', audit_observations JSONB NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (environment,virtual_account_key,client_id),
    UNIQUE (environment,virtual_account_key,strategy_id,signal_id,intent_key)
);
ALTER TABLE demo_orders ADD COLUMN IF NOT EXISTS intent_payload JSONB NOT NULL DEFAULT '{}';
-- Migration: an absent client-id lookup may be closed only after the runner's
-- independent position/open-order checks and a conservative time/count gate.
-- It is a local reconciliation terminal state, never an exchange status.
DO $demo_unknown_submission_terminal_v1$
DECLARE check_name TEXT;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'demo_orders'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%abandoned_after_absent_lookup%'
    ) THEN
        FOR check_name IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'demo_orders'::regclass AND contype = 'c'
              AND pg_get_constraintdef(oid) ILIKE '%status%'
        LOOP
            EXECUTE format('ALTER TABLE demo_orders DROP CONSTRAINT %I', check_name);
        END LOOP;
        ALTER TABLE demo_orders ADD CONSTRAINT demo_orders_status_check CHECK (status IN
          ('intent','submitting','unknown','open','partially_filled','filled','canceled','rejected','expired',
           'abandoned_after_absent_lookup'));
    END IF;
END;
$demo_unknown_submission_terminal_v1$;
CREATE UNIQUE INDEX IF NOT EXISTS demo_orders_exchange_identity ON demo_orders
    (environment,virtual_account_key,pair,exchange_order_id) WHERE exchange_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS demo_orders_pending ON demo_orders (environment,virtual_account_key,status,created_ms);

CREATE TABLE IF NOT EXISTS demo_fills (
    environment TEXT NOT NULL CHECK (environment = 'binance_demo'),
    virtual_account_key TEXT NOT NULL, client_id TEXT NOT NULL, pair TEXT NOT NULL,
    exchange_order_id TEXT NOT NULL, exchange_trade_id TEXT NOT NULL,
    price NUMERIC(38,18) NOT NULL CHECK (price > 0),
    quantity NUMERIC(38,18) NOT NULL CHECK (quantity > 0),
    commission NUMERIC(38,18), commission_asset TEXT, is_maker BOOLEAN,
    commission_conflict BOOLEAN NOT NULL DEFAULT FALSE,
    realized_pnl NUMERIC(38,18), timestamp_ms BIGINT NOT NULL CHECK (timestamp_ms >= 0),
    exchange_updated_ms BIGINT NOT NULL DEFAULT -1,
    raw JSONB NOT NULL DEFAULT '{}', audit_observations JSONB NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (environment,virtual_account_key,pair,exchange_order_id,exchange_trade_id),
    FOREIGN KEY (environment,virtual_account_key,client_id)
      REFERENCES demo_orders(environment,virtual_account_key,client_id)
);
ALTER TABLE demo_fills ADD COLUMN IF NOT EXISTS commission_conflict BOOLEAN NOT NULL DEFAULT FALSE;
-- Migration demo_fee_fields_independent_v2: retain partially known actual fees.
-- Match ONLY the old coupling expression, not unrelated checks or other tables.
DO $demo_fee_fields_independent_v2$
DECLARE old_check TEXT;
BEGIN
    FOR old_check IN SELECT conname FROM pg_constraint
      WHERE conrelid='demo_fills'::regclass AND contype='c'
        AND regexp_replace(pg_get_constraintdef(oid), '[[:space:]()]', '', 'g')
          = 'CHECKcommissionISNULL=commission_assetISNULL'
    LOOP
        EXECUTE format('ALTER TABLE demo_fills DROP CONSTRAINT %I', old_check);
    END LOOP;
END;
$demo_fee_fields_independent_v2$;
CREATE INDEX IF NOT EXISTS demo_fills_client ON demo_fills (environment,virtual_account_key,client_id,timestamp_ms);

CREATE TABLE IF NOT EXISTS demo_positions (
    observation_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL CHECK (environment = 'binance_demo'),
    virtual_account_key TEXT NOT NULL,
    endpoint_host TEXT NOT NULL CHECK (endpoint_host = 'demo-fapi.binance.com'),
    observed_ms BIGINT NOT NULL CHECK (observed_ms >= 0),
    payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS demo_positions_account_time ON demo_positions
    (environment,virtual_account_key,observed_ms DESC);
CREATE TABLE IF NOT EXISTS demo_account (
    observation_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL CHECK (environment = 'binance_demo'),
    virtual_account_key TEXT NOT NULL,
    endpoint_host TEXT NOT NULL CHECK (endpoint_host = 'demo-fapi.binance.com'),
    observed_ms BIGINT NOT NULL CHECK (observed_ms >= 0),
    payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS demo_account_account_time ON demo_account
    (environment,virtual_account_key,observed_ms DESC);

-- Additional fifth table: immutable paper/Demo observations of the SAME frozen signal.
-- It records comparison evidence only; it never changes either ledger's positions.
CREATE TABLE IF NOT EXISTS demo_signal_comparisons (
    observation_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL CHECK (environment = 'binance_demo'),
    virtual_account_key TEXT NOT NULL, strategy_id TEXT NOT NULL, signal_id TEXT NOT NULL,
    rule_hash TEXT NOT NULL, execution_hash TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
    stage TEXT NOT NULL, observed_ms BIGINT NOT NULL CHECK (observed_ms >= 0),
    payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS demo_comparison_signal ON demo_signal_comparisons
    (environment,virtual_account_key,strategy_id,signal_id,observed_ms);
CREATE UNIQUE INDEX IF NOT EXISTS demo_one_account_binding ON demo_signal_comparisons
    (environment,virtual_account_key) WHERE stage='binding';
