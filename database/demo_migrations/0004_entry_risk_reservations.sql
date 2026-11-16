-- 账户预算在唯一账户事务锁内裁定，事件与Decision不可改写。
CREATE TABLE demo_risk_contexts (
 environment TEXT NOT NULL CHECK(environment='binance_demo'),
 account_scope TEXT NOT NULL, context_id TEXT NOT NULL, sequence_id BIGINT NOT NULL CHECK(sequence_id>0),
 context_hash TEXT NOT NULL, payload JSONB NOT NULL,
 PRIMARY KEY(environment,account_scope,context_id), UNIQUE(environment,account_scope,sequence_id)
);
CREATE TABLE demo_entry_decisions (
 environment TEXT NOT NULL CHECK(environment='binance_demo'), account_scope TEXT NOT NULL,
 decision_id TEXT NOT NULL, strategy_namespace TEXT NOT NULL, client_id TEXT NOT NULL,
 context_id TEXT NOT NULL, request_hash TEXT NOT NULL, decision_hash TEXT NOT NULL,
 accepted BOOLEAN NOT NULL, payload JSONB NOT NULL, request_payload JSONB NOT NULL,
 PRIMARY KEY(environment,account_scope,decision_id), UNIQUE(environment,strategy_namespace,client_id),
 UNIQUE(environment,account_scope,client_id),
 FOREIGN KEY(environment,account_scope,context_id) REFERENCES demo_risk_contexts(environment,account_scope,context_id)
);
CREATE TABLE demo_risk_reservations (
 environment TEXT NOT NULL, account_scope TEXT NOT NULL, decision_id TEXT NOT NULL,
 strategy_namespace TEXT NOT NULL, client_id TEXT NOT NULL, status TEXT NOT NULL,
 loss_usdt NUMERIC NOT NULL CHECK(loss_usdt>=0 AND loss_usdt NOT IN ('NaN','Infinity','-Infinity')),
 margin_usdt NUMERIC NOT NULL CHECK(margin_usdt>=0 AND margin_usdt NOT IN ('NaN','Infinity','-Infinity')),
 pair TEXT NOT NULL, payload JSONB NOT NULL,
 PRIMARY KEY(environment,account_scope,decision_id), UNIQUE(environment,strategy_namespace,client_id),
 FOREIGN KEY(environment,account_scope,decision_id) REFERENCES demo_entry_decisions(environment,account_scope,decision_id)
);
CREATE TABLE demo_risk_reservation_events (
 sequence_id BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
 environment TEXT NOT NULL, account_scope TEXT NOT NULL, decision_id TEXT NOT NULL,
 event_id TEXT NOT NULL, event_type TEXT NOT NULL CHECK(event_type IN('reserved','claimed','unknown','released')),
 observed_ms BIGINT NOT NULL CHECK(observed_ms>=0), payload_hash TEXT NOT NULL, payload JSONB NOT NULL,
 PRIMARY KEY(environment,account_scope,event_id),
 FOREIGN KEY(environment,account_scope,decision_id) REFERENCES demo_entry_decisions(environment,account_scope,decision_id)
);
CREATE INDEX demo_risk_reservation_events_replay ON demo_risk_reservation_events(environment,account_scope,decision_id,sequence_id);
CREATE TABLE demo_risk_flat_reconciliations (
 environment TEXT NOT NULL, account_scope TEXT NOT NULL, fact_id TEXT NOT NULL, payload_hash TEXT NOT NULL, payload JSONB NOT NULL,
 PRIMARY KEY(environment,account_scope,fact_id)
);
ALTER TABLE demo_orders ADD COLUMN risk_reservation_account_scope TEXT;
ALTER TABLE demo_orders ADD COLUMN risk_reservation_decision_id TEXT;
ALTER TABLE demo_orders ADD CONSTRAINT demo_order_reservation_decision FOREIGN KEY
 (environment,risk_reservation_account_scope,risk_reservation_decision_id)
 REFERENCES demo_entry_decisions(environment,account_scope,decision_id);
ALTER TABLE demo_orders ADD CONSTRAINT demo_order_reservation_binding CHECK(
 (risk_reservation_account_scope IS NULL)=(risk_reservation_decision_id IS NULL));
CREATE FUNCTION prevent_demo_risk_reservation_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Demo risk contexts, decisions, facts and events are immutable' USING ERRCODE='55000';
END;$$;
CREATE TRIGGER demo_risk_contexts_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON demo_risk_contexts FOR EACH STATEMENT EXECUTE FUNCTION prevent_demo_risk_reservation_mutation();
CREATE TRIGGER demo_entry_decisions_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON demo_entry_decisions FOR EACH STATEMENT EXECUTE FUNCTION prevent_demo_risk_reservation_mutation();
CREATE TRIGGER demo_risk_reservation_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON demo_risk_reservation_events FOR EACH STATEMENT EXECUTE FUNCTION prevent_demo_risk_reservation_mutation();
CREATE TRIGGER demo_risk_flat_facts_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON demo_risk_flat_reconciliations FOR EACH STATEMENT EXECUTE FUNCTION prevent_demo_risk_reservation_mutation();
ALTER TABLE demo_risk_contexts ENABLE ALWAYS TRIGGER demo_risk_contexts_immutable;
ALTER TABLE demo_entry_decisions ENABLE ALWAYS TRIGGER demo_entry_decisions_immutable;
ALTER TABLE demo_risk_reservation_events ENABLE ALWAYS TRIGGER demo_risk_reservation_events_immutable;
ALTER TABLE demo_risk_flat_reconciliations ENABLE ALWAYS TRIGGER demo_risk_flat_facts_immutable;
CREATE FUNCTION guard_demo_reserved_order_claim() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.risk_reservation_decision_id IS NOT NULL AND
  ROW(NEW.environment,NEW.virtual_account_key,NEW.client_id,NEW.strategy_id,NEW.signal_id,NEW.intent_key,
      NEW.rule_hash,NEW.execution_hash,NEW.input_fingerprint,NEW.endpoint_host,NEW.pair,NEW.side,
      NEW.order_type,NEW.quantity,NEW.price,NEW.time_in_force,NEW.reduce_only,NEW.signal_ms,NEW.created_ms,NEW.intent_payload)
  IS DISTINCT FROM
  ROW(OLD.environment,OLD.virtual_account_key,OLD.client_id,OLD.strategy_id,OLD.signal_id,OLD.intent_key,
      OLD.rule_hash,OLD.execution_hash,OLD.input_fingerprint,OLD.endpoint_host,OLD.pair,OLD.side,
      OLD.order_type,OLD.quantity,OLD.price,OLD.time_in_force,OLD.reduce_only,OLD.signal_ms,OLD.created_ms,OLD.intent_payload) THEN
  RAISE EXCEPTION 'Reserved order frozen identity cannot change' USING ERRCODE='55000';
 END IF;
 IF NEW.submission_attempts<OLD.submission_attempts THEN
  RAISE EXCEPTION 'Submission attempt cannot be reset' USING ERRCODE='55000';
 END IF;
 IF NEW.risk_reservation_account_scope IS DISTINCT FROM OLD.risk_reservation_account_scope
 OR NEW.risk_reservation_decision_id IS DISTINCT FROM OLD.risk_reservation_decision_id THEN
  IF OLD.submission_attempts>0 OR OLD.risk_reservation_decision_id IS NOT NULL THEN
   RAISE EXCEPTION 'Cannot change durable risk reservation binding' USING ERRCODE='55000';
  END IF;
 END IF;
 IF NEW.risk_reservation_decision_id IS NOT NULL AND NEW.submission_attempts>OLD.submission_attempts THEN
  IF NOT EXISTS(SELECT 1 FROM demo_risk_reservations r
   JOIN demo_risk_reservation_events e ON e.environment=r.environment AND e.account_scope=r.account_scope AND e.decision_id=r.decision_id
   WHERE r.environment=NEW.environment AND r.account_scope=NEW.risk_reservation_account_scope
    AND r.decision_id=NEW.risk_reservation_decision_id AND r.client_id=NEW.client_id
    AND r.strategy_namespace=NEW.virtual_account_key AND r.status='claimed' AND e.event_type='claimed'
    AND e.observed_ms=NEW.submission_claimed_ms AND NEW.status='submitting') THEN
   RAISE EXCEPTION 'Reserved order requires account transaction claim' USING ERRCODE='55000';
  END IF;
 END IF;
 RETURN NEW;
END;$$;
CREATE TRIGGER demo_reserved_order_claim BEFORE UPDATE ON demo_orders FOR EACH ROW EXECUTE FUNCTION guard_demo_reserved_order_claim();
ALTER TABLE demo_orders ENABLE ALWAYS TRIGGER demo_reserved_order_claim;
