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
-- 数字 ID 以长度及字典序共同排序，9/10 在同毫秒下保持真实成交顺序。
CREATE INDEX IF NOT EXISTS ticks_pair_time_numeric_id
    ON ticks (exchange, market, pair, timestamp DESC, length(trade_id) DESC, trade_id DESC);
CREATE INDEX IF NOT EXISTS orderbooks_pair_time_numeric_id
    ON orderbooks (exchange, market, pair, timestamp DESC, length(last_update_id) DESC, last_update_id DESC);
