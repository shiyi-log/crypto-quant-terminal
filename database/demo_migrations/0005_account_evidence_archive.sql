-- Sanitized immutable facts; receipts do not certify exchange authenticity.
CREATE TABLE demo_evidence_archive (
 environment TEXT NOT NULL CHECK(environment='binance_demo'),
 account_id TEXT NOT NULL, stage_id TEXT NOT NULL,
 account_sequence BIGINT NOT NULL CHECK(account_sequence>0),
 ref TEXT NOT NULL UNIQUE, kind TEXT NOT NULL CHECK(kind IN
 ('account_snapshot','positions','normal_orders','conditional_orders','fills','cashflows',
  'clock','boundary','equity_reconciliation','completion','acceptance')),
 capture_key TEXT NOT NULL, sha256 TEXT NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
 previous_hash TEXT NOT NULL CHECK(previous_hash ~ '^[0-9a-f]{64}$'),
 record_sha256 TEXT NOT NULL CHECK(record_sha256 ~ '^[0-9a-f]{64}$'),
 body JSONB NOT NULL CHECK(jsonb_typeof(body)='object'),
 PRIMARY KEY(environment,account_id,account_sequence),
 UNIQUE(environment,account_id,stage_id,capture_key),
 UNIQUE(environment,account_id,stage_id,ref)
);
CREATE TABLE demo_completion_records (
 environment TEXT NOT NULL CHECK(environment='binance_demo'),
 account_id TEXT NOT NULL, stage_id TEXT NOT NULL,
 trade_id TEXT NOT NULL, proof_sha256 TEXT NOT NULL CHECK(proof_sha256 ~ '^[0-9a-f]{64}$'),
 ref TEXT NOT NULL UNIQUE, correction_required BOOLEAN NOT NULL, payload JSONB NOT NULL,
 PRIMARY KEY(environment,account_id,stage_id,trade_id,proof_sha256),
 FOREIGN KEY(environment,account_id,stage_id,ref)
 REFERENCES demo_evidence_archive(environment,account_id,stage_id,ref)
);
CREATE UNIQUE INDEX demo_completion_original_once
 ON demo_completion_records(environment,account_id,stage_id,trade_id) WHERE NOT correction_required;
CREATE FUNCTION prevent_demo_evidence_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Demo evidence and completion records are immutable' USING ERRCODE='55000';
END;$$;
CREATE TRIGGER demo_evidence_archive_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
 ON demo_evidence_archive FOR EACH STATEMENT EXECUTE FUNCTION prevent_demo_evidence_mutation();
CREATE TRIGGER demo_completion_records_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
 ON demo_completion_records FOR EACH STATEMENT EXECUTE FUNCTION prevent_demo_evidence_mutation();
ALTER TABLE demo_evidence_archive ENABLE ALWAYS TRIGGER demo_evidence_archive_immutable;
ALTER TABLE demo_completion_records ENABLE ALWAYS TRIGGER demo_completion_records_immutable;
