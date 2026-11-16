-- Frozen research facts and irreversible final-holdout consumption; never execution approval.
CREATE TABLE refactor_research_versions (
 account_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN('dataset','factor','strategy','exit')),
 version_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL, payload JSONB NOT NULL,
 PRIMARY KEY(account_id,kind,version_id)
);
CREATE TABLE refactor_research_families (
 account_id TEXT NOT NULL, family_id TEXT NOT NULL, family_sha256 TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, payload JSONB NOT NULL,
 PRIMARY KEY(account_id,family_id)
);
CREATE TABLE refactor_research_attempts (
 sequence_id BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
 account_id TEXT NOT NULL, attempt_id TEXT NOT NULL, family_id TEXT NOT NULL,
 claim_id TEXT, config_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL, payload JSONB NOT NULL,
 PRIMARY KEY(account_id,attempt_id),
 FOREIGN KEY(account_id,family_id) REFERENCES refactor_research_families(account_id,family_id)
);
CREATE TABLE refactor_research_attempt_events (
 sequence_id BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
 account_id TEXT NOT NULL, attempt_id TEXT NOT NULL, event_id TEXT NOT NULL,
 event_type TEXT NOT NULL CHECK(event_type IN('started','completed','failed','canceled','not_estimable')),
 observed_ms BIGINT NOT NULL CHECK(observed_ms>=0), payload_sha256 TEXT NOT NULL,payload JSONB NOT NULL,
 PRIMARY KEY(account_id,event_id),
 FOREIGN KEY(account_id,attempt_id) REFERENCES refactor_research_attempts(account_id,attempt_id)
);
CREATE TABLE refactor_research_artifacts (
 account_id TEXT NOT NULL, artifact_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, payload JSONB NOT NULL,
 PRIMARY KEY(account_id,artifact_id),
 FOREIGN KEY(account_id,attempt_id) REFERENCES refactor_research_attempts(account_id,attempt_id)
);
CREATE TABLE refactor_research_holdout_claims (
 account_id TEXT NOT NULL, family_id TEXT NOT NULL, claim_id TEXT NOT NULL,
 dataset_sha256 TEXT NOT NULL, holdout_start_ms BIGINT NOT NULL, holdout_end_ms BIGINT NOT NULL,
 payload_sha256 TEXT NOT NULL, payload JSONB NOT NULL,
 PRIMARY KEY(account_id,family_id), UNIQUE(account_id,claim_id),
 UNIQUE(account_id,dataset_sha256,holdout_start_ms,holdout_end_ms),
 FOREIGN KEY(account_id,family_id) REFERENCES refactor_research_families(account_id,family_id)
);
CREATE TABLE refactor_research_holdout_events (
 sequence_id BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
 account_id TEXT NOT NULL, claim_id TEXT NOT NULL, event_id TEXT NOT NULL,
 event_type TEXT NOT NULL CHECK(event_type IN('unknown','failed','completed')),
 observed_ms BIGINT NOT NULL CHECK(observed_ms>=0),payload_sha256 TEXT NOT NULL,payload JSONB NOT NULL,
 PRIMARY KEY(account_id,event_id),
 FOREIGN KEY(account_id,claim_id) REFERENCES refactor_research_holdout_claims(account_id,claim_id)
);
CREATE TABLE refactor_research_candidate_results (
 account_id TEXT NOT NULL, claim_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL,payload JSONB NOT NULL,
 PRIMARY KEY(account_id,claim_id),
 FOREIGN KEY(account_id,claim_id) REFERENCES refactor_research_holdout_claims(account_id,claim_id)
);
CREATE FUNCTION prevent_refactor_research_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Research registry facts are immutable; append a new event' USING ERRCODE='55000';
END;$$;
CREATE TRIGGER refactor_research_versions_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_versions FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_mutation();
CREATE TRIGGER refactor_research_families_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_families FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_mutation();
CREATE TRIGGER refactor_research_attempts_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_attempts FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_mutation();
CREATE TRIGGER refactor_research_attempt_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_attempt_events FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_mutation();
CREATE TRIGGER refactor_research_artifacts_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_artifacts FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_mutation();
CREATE TRIGGER refactor_research_holdout_claims_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_holdout_claims FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_mutation();
CREATE TRIGGER refactor_research_holdout_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_holdout_events FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_mutation();
CREATE TRIGGER refactor_research_candidate_results_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_candidate_results FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_mutation();
ALTER TABLE refactor_research_versions ENABLE ALWAYS TRIGGER refactor_research_versions_immutable;
ALTER TABLE refactor_research_families ENABLE ALWAYS TRIGGER refactor_research_families_immutable;
ALTER TABLE refactor_research_attempts ENABLE ALWAYS TRIGGER refactor_research_attempts_immutable;
ALTER TABLE refactor_research_attempt_events ENABLE ALWAYS TRIGGER refactor_research_attempt_events_immutable;
ALTER TABLE refactor_research_artifacts ENABLE ALWAYS TRIGGER refactor_research_artifacts_immutable;
ALTER TABLE refactor_research_holdout_claims ENABLE ALWAYS TRIGGER refactor_research_holdout_claims_immutable;
ALTER TABLE refactor_research_holdout_events ENABLE ALWAYS TRIGGER refactor_research_holdout_events_immutable;
ALTER TABLE refactor_research_candidate_results ENABLE ALWAYS TRIGGER refactor_research_candidate_results_immutable;
