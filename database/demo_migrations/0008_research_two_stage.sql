-- P2 研究二阶段的不可变事实；不记录验收通过或生产授权。
CREATE TABLE refactor_research_inferences_v2 (
    account_id TEXT NOT NULL,
    inference_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    family_sha256 TEXT NOT NULL CHECK (family_sha256 ~ '^[a-f0-9]{64}$'),
    inference_sha256 TEXT NOT NULL CHECK (inference_sha256 ~ '^[a-f0-9]{64}$'),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_ms BIGINT NOT NULL CHECK (created_ms >= 0),
    PRIMARY KEY (account_id, inference_id),
    UNIQUE (account_id, inference_id, family_id),
    FOREIGN KEY (account_id, family_id)
        REFERENCES refactor_research_families (account_id, family_id)
);

CREATE TABLE refactor_research_validation_results_v2 (
    account_id TEXT NOT NULL,
    inference_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    config_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    result_sha256 TEXT NOT NULL CHECK (result_sha256 ~ '^[a-f0-9]{64}$'),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    completed_ms BIGINT NOT NULL CHECK (completed_ms >= 0),
    PRIMARY KEY (account_id, inference_id, config_id),
    UNIQUE (account_id, inference_id, attempt_id),
    FOREIGN KEY (account_id, inference_id, family_id)
        REFERENCES refactor_research_inferences_v2 (account_id, inference_id, family_id)
);

CREATE TABLE refactor_research_selections_v2 (
    account_id TEXT NOT NULL,
    selection_id TEXT NOT NULL,
    inference_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('selected', 'not_estimable', 'rejected')) ,
    selection_sha256 TEXT NOT NULL CHECK (selection_sha256 ~ '^[a-f0-9]{64}$'),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_ms BIGINT NOT NULL CHECK (created_ms >= 0),
    PRIMARY KEY (account_id, selection_id),
    UNIQUE (account_id, selection_id, inference_id),
    FOREIGN KEY (account_id, inference_id, family_id)
        REFERENCES refactor_research_inferences_v2 (account_id, inference_id, family_id)
);

CREATE TABLE refactor_research_candidate_results_v2 (
    account_id TEXT NOT NULL,
    candidate_result_id TEXT NOT NULL,
    selection_id TEXT NOT NULL,
    inference_id TEXT NOT NULL,
    candidate_sha256 TEXT NOT NULL CHECK (candidate_sha256 ~ '^[a-f0-9]{64}$'),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_ms BIGINT NOT NULL CHECK (created_ms >= 0),
    PRIMARY KEY (account_id, candidate_result_id),
    UNIQUE (account_id, selection_id, candidate_sha256),
    FOREIGN KEY (account_id, selection_id, inference_id)
        REFERENCES refactor_research_selections_v2 (account_id, selection_id, inference_id)
);

CREATE FUNCTION prevent_refactor_research_v2_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Research v2 facts are immutable; append a new observation'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER refactor_research_inferences_v2_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_inferences_v2
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_v2_mutation();
CREATE TRIGGER refactor_research_validation_results_v2_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_validation_results_v2
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_v2_mutation();
CREATE TRIGGER refactor_research_selections_v2_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_selections_v2
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_v2_mutation();
CREATE TRIGGER refactor_research_candidate_results_v2_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_candidate_results_v2
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_v2_mutation();

ALTER TABLE refactor_research_inferences_v2 ENABLE ALWAYS TRIGGER refactor_research_inferences_v2_immutable;
ALTER TABLE refactor_research_validation_results_v2 ENABLE ALWAYS TRIGGER refactor_research_validation_results_v2_immutable;
ALTER TABLE refactor_research_selections_v2 ENABLE ALWAYS TRIGGER refactor_research_selections_v2_immutable;
ALTER TABLE refactor_research_candidate_results_v2 ENABLE ALWAYS TRIGGER refactor_research_candidate_results_v2_immutable;
