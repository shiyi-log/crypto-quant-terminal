-- 统一数据仓库；调用方在事务内设置独立 schema，交易引擎原数据库不变。
CREATE TABLE IF NOT EXISTS store_metadata (
    key TEXT PRIMARY KEY, payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS documents (
    key TEXT PRIMARY KEY, payload JSONB NOT NULL,
    source_path TEXT, source_mtime_ns BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS documents_key_prefix ON documents (key text_pattern_ops);

CREATE TABLE IF NOT EXISTS event_sources (
    key TEXT PRIMARY KEY, source_path TEXT, source_mtime_ns BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS events (
    key TEXT NOT NULL REFERENCES event_sources(key) ON DELETE CASCADE,
    ordinal BIGINT NOT NULL, payload JSONB NOT NULL,
    PRIMARY KEY (key, ordinal)
);

CREATE TABLE IF NOT EXISTS candles (
    exchange TEXT NOT NULL, market TEXT NOT NULL, pair TEXT NOT NULL,
    timeframe TEXT NOT NULL, candle_type TEXT NOT NULL, timestamp BIGINT NOT NULL,
    open DOUBLE PRECISION NOT NULL, high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL, close DOUBLE PRECISION NOT NULL,
    volume DOUBLE PRECISION NOT NULL, extras JSONB NOT NULL DEFAULT '{}',
    source_key TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (exchange, market, pair, timeframe, candle_type, timestamp)
);
-- 每次导入仅刷新对应数据集，状态接口只需扫描约数百行摘要。
CREATE TABLE IF NOT EXISTS candle_datasets (
    exchange TEXT NOT NULL, market TEXT NOT NULL, pair TEXT NOT NULL,
    timeframe TEXT NOT NULL, candle_type TEXT NOT NULL,
    rows BIGINT NOT NULL, first_timestamp BIGINT, last_timestamp BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (exchange, market, pair, timeframe, candle_type)
);

CREATE TABLE IF NOT EXISTS series_points (
    source TEXT NOT NULL, metric TEXT NOT NULL, asset TEXT NOT NULL,
    timestamp BIGINT NOT NULL, value DOUBLE PRECISION NOT NULL,
    extras JSONB NOT NULL DEFAULT '{}', source_key TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source, metric, asset, timestamp)
);
CREATE TABLE IF NOT EXISTS external_records (
    source TEXT NOT NULL, table_name TEXT NOT NULL, record_key TEXT NOT NULL,
    payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source, table_name, record_key)
);

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY, payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS artifacts (
    key TEXT PRIMARY KEY, path TEXT NOT NULL, size BIGINT NOT NULL,
    mtime_ns BIGINT NOT NULL, kind TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sources (
    key TEXT PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL,
    mtime_ns BIGINT, size BIGINT, row_count BIGINT NOT NULL DEFAULT 0,
    error TEXT, last_attempt_mtime_ns BIGINT, last_attempt_size BIGINT,
    last_success_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticks (
    exchange TEXT NOT NULL, market TEXT NOT NULL, pair TEXT NOT NULL,
    trade_id TEXT NOT NULL, timestamp BIGINT NOT NULL,
    price NUMERIC(38,18) NOT NULL, quantity NUMERIC(38,18) NOT NULL,
    buyer_maker BOOLEAN NOT NULL, extras JSONB NOT NULL DEFAULT '{}',
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (exchange, market, pair, trade_id)
);
CREATE INDEX IF NOT EXISTS ticks_pair_time ON ticks (exchange, market, pair, timestamp DESC, trade_id DESC);
CREATE INDEX IF NOT EXISTS ticks_time_brin ON ticks USING BRIN (timestamp);

CREATE TABLE IF NOT EXISTS orderbooks (
    exchange TEXT NOT NULL, market TEXT NOT NULL, pair TEXT NOT NULL,
    timestamp BIGINT NOT NULL, last_update_id TEXT NOT NULL,
    bids JSONB NOT NULL, asks JSONB NOT NULL, extras JSONB NOT NULL DEFAULT '{}',
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (exchange, market, pair, timestamp, last_update_id)
);
CREATE INDEX IF NOT EXISTS orderbooks_pair_time ON orderbooks (exchange, market, pair, timestamp DESC);
CREATE INDEX IF NOT EXISTS orderbooks_time_brin ON orderbooks USING BRIN (timestamp);
CREATE TABLE IF NOT EXISTS stream_stats (
    kind TEXT NOT NULL, exchange TEXT NOT NULL, market TEXT NOT NULL, pair TEXT NOT NULL,
    rows BIGINT NOT NULL DEFAULT 0, first_timestamp BIGINT, last_timestamp BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kind, exchange, market, pair)
);

-- 研究与实盘证据账：事件只追加，修订以新事件表达，不覆盖旧事实。
CREATE TABLE IF NOT EXISTS research_ledger_events (
    sequence_id BIGSERIAL UNIQUE,
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    source_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE research_ledger_events ADD COLUMN IF NOT EXISTS sequence_id BIGSERIAL;
CREATE INDEX IF NOT EXISTS research_ledger_events_type ON research_ledger_events (event_type, created_at);
CREATE TABLE IF NOT EXISTS research_ledger_sync (
    source_id TEXT PRIMARY KEY,
    source_mtime_ns BIGINT,
    source_fingerprint TEXT NOT NULL,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 订单流突增观测与成熟结果。每个 revision 都是不可变事实；成熟结果
-- 通过追加 revision 表达，原始 observation 永不被覆盖。
-- 旧版 event_id/observation_id/payload 表完整保留，再由 DataStore 导入。
-- 必须先改名，避免 IF NOT EXISTS 掩盖旧结构后索引引用缺失字段。
DO $$
BEGIN
    IF to_regclass('orderflow_surge_events') IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name='orderflow_surge_events'
          AND column_name='revision_id'
    ) THEN
        IF to_regclass('orderflow_surge_events_legacy_v3') IS NOT NULL THEN
            RAISE EXCEPTION 'Both legacy orderflow event tables exist; refusing to overwrite historical evidence';
        END IF;
        ALTER TABLE orderflow_surge_events RENAME TO orderflow_surge_events_legacy_v3;
        -- Index names survive a table rename and would otherwise suppress
        -- creation of the canonical indexes via IF NOT EXISTS.
        ALTER INDEX IF EXISTS orderflow_surge_events_pkey RENAME TO orderflow_surge_events_legacy_v3_pkey;
        ALTER INDEX IF EXISTS orderflow_surge_events_type RENAME TO orderflow_surge_events_legacy_v3_type;
        ALTER INDEX IF EXISTS orderflow_surge_events_observation RENAME TO orderflow_surge_events_legacy_v3_observation;
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS orderflow_surge_events (
    event_id TEXT NOT NULL,
    revision_id TEXT PRIMARY KEY,
    revision_no BIGINT NOT NULL,
    revision_kind TEXT NOT NULL CHECK (revision_kind IN ('observed', 'maturity')),
    event_type TEXT NOT NULL,
    method_version TEXT NOT NULL,
    data_fingerprint TEXT NOT NULL,
    market TEXT NOT NULL,
    pair TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp BIGINT NOT NULL,
    observed_at BIGINT,
    status TEXT NOT NULL CHECK (status IN ('unknown', 'matured', 'invalid')),
    observation JSONB NOT NULL,
    result JSONB,
    payload JSONB NOT NULL,
    source_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (event_id, revision_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS orderflow_surge_observation
    ON orderflow_surge_events (event_id) WHERE revision_kind='observed';
CREATE INDEX IF NOT EXISTS orderflow_surge_latest
    ON orderflow_surge_events (event_id, revision_no DESC);
CREATE INDEX IF NOT EXISTS orderflow_surge_query
    ON orderflow_surge_events (market, pair, timeframe, timestamp DESC);

-- Every live eligible bar is a price-spike trial, including bars below 2x.
-- Outcome and profile are separate immutable facts so the event-time ratio is
-- never rewritten when its forward label or +/-12-bar context matures.
CREATE TABLE IF NOT EXISTS orderflow_price_candidates (
    event_id TEXT PRIMARY KEY,
    method_version TEXT NOT NULL,
    data_fingerprint TEXT NOT NULL,
    exchange TEXT NOT NULL,
    market TEXT NOT NULL,
    pair TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp BIGINT NOT NULL,
    observed_at BIGINT,
    observation JSONB NOT NULL,
    source_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- Earlier deployments created this table before provenance was added. CREATE
-- TABLE IF NOT EXISTS does not upgrade that shape; the collector retries the
-- oldest spooled batch until this nullable column exists.
ALTER TABLE orderflow_price_candidates ADD COLUMN IF NOT EXISTS source_id TEXT;
CREATE INDEX IF NOT EXISTS orderflow_price_candidates_query
    ON orderflow_price_candidates (market, pair, timeframe, timestamp DESC);
CREATE TABLE IF NOT EXISTS orderflow_price_candidate_results (
    event_id TEXT NOT NULL REFERENCES orderflow_price_candidates(event_id),
    result_kind TEXT NOT NULL CHECK (result_kind IN ('outcome', 'profile')),
    fact_id TEXT PRIMARY KEY,
    method_version TEXT NOT NULL,
    data_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('matured', 'invalid')),
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (event_id, result_kind)
);
CREATE INDEX IF NOT EXISTS orderflow_price_candidate_results_kind
    ON orderflow_price_candidate_results (result_kind, status, event_id);

INSERT INTO store_metadata (key, payload) VALUES ('schema_version', '1')
ON CONFLICT (key) DO NOTHING;

-- 版本迁移只执行一次；已有数据库升级，不重复锁住持续增长的成交表。
DO $$
BEGIN
    IF (SELECT (payload::TEXT)::INTEGER FROM store_metadata WHERE key='schema_version') < 2 THEN
        ALTER TABLE ticks
            ALTER COLUMN price TYPE NUMERIC(38,18) USING price::NUMERIC(38,18),
            ALTER COLUMN quantity TYPE NUMERIC(38,18) USING quantity::NUMERIC(38,18);
        UPDATE store_metadata SET payload='2'::JSONB,updated_at=CURRENT_TIMESTAMP WHERE key='schema_version';
    END IF;
END $$;
DO $$
BEGIN
    IF (SELECT (payload::TEXT)::INTEGER FROM store_metadata WHERE key='schema_version') < 3 THEN
        UPDATE store_metadata SET payload='3'::JSONB,updated_at=CURRENT_TIMESTAMP WHERE key='schema_version';
    END IF;
END $$;

-- 数字 ID 以长度及字典序共同排序，9/10 在同毫秒下保持真实成交顺序。
CREATE INDEX IF NOT EXISTS ticks_pair_time_numeric_id
    ON ticks (exchange, market, pair, timestamp DESC, length(trade_id) DESC, trade_id DESC);
CREATE INDEX IF NOT EXISTS orderbooks_pair_time_numeric_id
    ON orderbooks (exchange, market, pair, timestamp DESC, length(last_update_id) DESC, last_update_id DESC);

-- Each research strategy has permanent independent capital and rule identity.
-- Mutable state is a transactional projection of the append-only event ledger.
CREATE TABLE IF NOT EXISTS strategy_sim_accounts (
    strategy_id TEXT PRIMARY KEY,
    rule_hash TEXT NOT NULL,
    execution_hash TEXT NOT NULL,
    definition JSONB NOT NULL,
    execution JSONB NOT NULL,
    initial_capital NUMERIC(30,12) NOT NULL CHECK (initial_capital > 0),
    state JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS strategy_sim_events (
    sequence_id BIGSERIAL UNIQUE,
    event_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL REFERENCES strategy_sim_accounts(strategy_id),
    event_type TEXT NOT NULL,
    candle_ms BIGINT,
    observed_at_ms BIGINT NOT NULL,
    rule_hash TEXT NOT NULL,
    data_fingerprint TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS strategy_sim_events_account
    ON strategy_sim_events (strategy_id, sequence_id DESC);
CREATE INDEX IF NOT EXISTS strategy_sim_events_type
    ON strategy_sim_events (strategy_id, event_type, sequence_id DESC);

CREATE OR REPLACE FUNCTION strategy_sim_immutable_event() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Strategy simulation events are immutable; append a new fact instead';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS strategy_sim_events_immutable ON strategy_sim_events;
CREATE TRIGGER strategy_sim_events_immutable BEFORE UPDATE OR DELETE ON strategy_sim_events
    FOR EACH ROW EXECUTE FUNCTION strategy_sim_immutable_event();

CREATE OR REPLACE FUNCTION strategy_sim_immutable_account() RETURNS trigger AS $$
BEGIN
    IF OLD.strategy_id IS DISTINCT FROM NEW.strategy_id
       OR OLD.rule_hash IS DISTINCT FROM NEW.rule_hash
       OR OLD.execution_hash IS DISTINCT FROM NEW.execution_hash
       OR OLD.initial_capital IS DISTINCT FROM NEW.initial_capital
       OR OLD.definition IS DISTINCT FROM NEW.definition
       OR OLD.execution IS DISTINCT FROM NEW.execution THEN
        RAISE EXCEPTION 'Rule or capital change requires a new strategy identity';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS strategy_sim_account_identity ON strategy_sim_accounts;
CREATE TRIGGER strategy_sim_account_identity BEFORE UPDATE ON strategy_sim_accounts
    FOR EACH ROW EXECUTE FUNCTION strategy_sim_immutable_account();
