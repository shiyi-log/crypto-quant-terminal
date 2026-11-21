-- 留出集按账户和推断身份不可逆领取；领取只消费机会，不产生计算/执行票据。
ALTER TABLE refactor_research_candidate_results_v2
    ADD CONSTRAINT refactor_research_candidate_results_v2_claim_identity_unique
    UNIQUE (account_id, candidate_result_id, selection_id, inference_id);

CREATE TABLE refactor_research_holdout_claims_v2 (
    account_id TEXT NOT NULL,
    inference_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    selection_id TEXT NOT NULL,
    candidate_result_id TEXT NOT NULL,
    family_sha256 TEXT NOT NULL CHECK (family_sha256 ~ '^[a-f0-9]{64}$'),
    inference_sha256 TEXT NOT NULL CHECK (inference_sha256 ~ '^[a-f0-9]{64}$'),
    selection_sha256 TEXT NOT NULL CHECK (selection_sha256 ~ '^[a-f0-9]{64}$'),
    candidate_sha256 TEXT NOT NULL CHECK (candidate_sha256 ~ '^[a-f0-9]{64}$'),
    dataset_sha256 TEXT NOT NULL CHECK (dataset_sha256 ~ '^[a-f0-9]{64}$'),
    strategy_sha256 TEXT NOT NULL CHECK (strategy_sha256 ~ '^[a-f0-9]{64}$'),
    validation_plan_sha256 TEXT NOT NULL CHECK (validation_plan_sha256 ~ '^[a-f0-9]{64}$'),
    selection_policy_sha256 TEXT NOT NULL CHECK (selection_policy_sha256 ~ '^[a-f0-9]{64}$'),
    holdout_policy_sha256 TEXT NOT NULL CHECK (holdout_policy_sha256 ~ '^[a-f0-9]{64}$'),
    claimed_ms BIGINT NOT NULL CHECK (claimed_ms >= 0),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    PRIMARY KEY (account_id, inference_id),
    UNIQUE (account_id, inference_sha256),
    FOREIGN KEY (account_id, inference_id, family_id)
        REFERENCES refactor_research_inferences_v2 (account_id, inference_id, family_id),
    FOREIGN KEY (account_id, selection_id, inference_id)
        REFERENCES refactor_research_selections_v2 (account_id, selection_id, inference_id),
    FOREIGN KEY (account_id, candidate_result_id, selection_id, inference_id)
        REFERENCES refactor_research_candidate_results_v2
            (account_id, candidate_result_id, selection_id, inference_id)
);

CREATE TRIGGER refactor_research_holdout_claims_v2_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON refactor_research_holdout_claims_v2
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_refactor_research_v2_mutation();
ALTER TABLE refactor_research_holdout_claims_v2
    ENABLE ALWAYS TRIGGER refactor_research_holdout_claims_v2_immutable;
