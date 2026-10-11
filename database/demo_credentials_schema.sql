-- Explicit opt-in installation; independent of all live-exchange credentials.
-- The encryption master key exists only on the local filesystem, never here.
CREATE TABLE IF NOT EXISTS binance_demo_credentials (
    environment TEXT PRIMARY KEY CHECK (environment = 'binance_demo'),
    endpoint_host TEXT NOT NULL CHECK (endpoint_host = 'demo-fapi.binance.com'),
    ciphertext TEXT NOT NULL,
    encryption_version INTEGER NOT NULL CHECK (encryption_version = 1),
    fingerprint TEXT NOT NULL,
    masked_api_key TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
