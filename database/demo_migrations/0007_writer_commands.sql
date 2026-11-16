-- 被动命令保存请求与处理观察；不授予交易、账户互斥或验收资格。
CREATE TABLE demo_command_bindings (
 environment TEXT NOT NULL CHECK(environment='binance_demo'), account_id TEXT NOT NULL,
 binding_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL CHECK(payload_sha256 ~ '^[a-f0-9]{64}$'),
 payload JSONB NOT NULL CHECK(jsonb_typeof(payload)='object'),
 PRIMARY KEY(environment,account_id,binding_id)
);
CREATE TABLE demo_command_intents (
 environment TEXT NOT NULL CHECK(environment='binance_demo'), account_id TEXT NOT NULL,
 command_id TEXT NOT NULL, binding_id TEXT NOT NULL,
 request_sha256 TEXT NOT NULL CHECK(request_sha256 ~ '^[a-f0-9]{64}$'),
 payload_sha256 TEXT NOT NULL CHECK(payload_sha256 ~ '^[a-f0-9]{64}$'),
 payload JSONB NOT NULL CHECK(jsonb_typeof(payload)='object'),
 PRIMARY KEY(environment,account_id,command_id),
 FOREIGN KEY(environment,account_id,binding_id) REFERENCES demo_command_bindings(environment,account_id,binding_id)
);
CREATE TABLE demo_command_events (
 environment TEXT NOT NULL CHECK(environment='binance_demo'), account_id TEXT NOT NULL,
 account_sequence BIGINT NOT NULL CHECK(account_sequence>0), command_id TEXT NOT NULL, event_id TEXT NOT NULL,
 event_type TEXT NOT NULL CHECK(event_type IN('accepted','claimed','unknown','applied','rejected','observed')),
 observed_ms BIGINT NOT NULL CHECK(observed_ms>=0),
 previous_hash TEXT NOT NULL CHECK(previous_hash ~ '^[a-f0-9]{64}$'),
 payload_sha256 TEXT NOT NULL CHECK(payload_sha256 ~ '^[a-f0-9]{64}$'),
 record_sha256 TEXT NOT NULL CHECK(record_sha256 ~ '^[a-f0-9]{64}$'),
 payload JSONB NOT NULL CHECK(jsonb_typeof(payload)='object'),
 PRIMARY KEY(environment,account_id,account_sequence), UNIQUE(environment,account_id,event_id),
 FOREIGN KEY(environment,account_id,command_id) REFERENCES demo_command_intents(environment,account_id,command_id)
);
CREATE INDEX demo_command_events_by_command ON demo_command_events(environment,account_id,command_id,account_sequence);
CREATE FUNCTION prevent_demo_command_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Demo command facts are immutable; append another observation' USING ERRCODE='55000';
END;$$;
CREATE TRIGGER demo_command_bindings_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON demo_command_bindings FOR EACH STATEMENT EXECUTE FUNCTION prevent_demo_command_mutation();
CREATE TRIGGER demo_command_intents_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON demo_command_intents FOR EACH STATEMENT EXECUTE FUNCTION prevent_demo_command_mutation();
CREATE TRIGGER demo_command_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON demo_command_events FOR EACH STATEMENT EXECUTE FUNCTION prevent_demo_command_mutation();
ALTER TABLE demo_command_bindings ENABLE ALWAYS TRIGGER demo_command_bindings_immutable;
ALTER TABLE demo_command_intents ENABLE ALWAYS TRIGGER demo_command_intents_immutable;
ALTER TABLE demo_command_events ENABLE ALWAYS TRIGGER demo_command_events_immutable;
