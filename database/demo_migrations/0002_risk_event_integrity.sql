-- 风险事实按持久化顺序重放；更新、删除和整表截断均禁止。
-- 旧记录按原有 created_at/time/id 顺序赋号，升级后不再重排。
ALTER TABLE demo_risk_events ADD COLUMN sequence_id BIGINT;
WITH ordered AS (
    SELECT environment, virtual_account_key, event_id,
           row_number() OVER (ORDER BY created_at,timestamp_ms,event_id,
                                      environment,virtual_account_key) AS value
    FROM demo_risk_events
)
UPDATE demo_risk_events AS target SET sequence_id=ordered.value
FROM ordered WHERE target.environment=ordered.environment
    AND target.virtual_account_key=ordered.virtual_account_key
    AND target.event_id=ordered.event_id;
CREATE SEQUENCE demo_risk_event_sequence;
SELECT setval('demo_risk_event_sequence',
    COALESCE((SELECT MAX(sequence_id) FROM demo_risk_events),0)+1, false);
ALTER TABLE demo_risk_events ALTER COLUMN sequence_id
    SET DEFAULT nextval('demo_risk_event_sequence');
ALTER TABLE demo_risk_events ALTER COLUMN sequence_id SET NOT NULL;
ALTER SEQUENCE demo_risk_event_sequence OWNED BY demo_risk_events.sequence_id;
CREATE UNIQUE INDEX demo_risk_events_sequence ON demo_risk_events(sequence_id);

CREATE FUNCTION prevent_demo_risk_event_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Demo risk events are immutable; append a new fact instead'
        USING ERRCODE='55000';
END;
$$;
CREATE TRIGGER demo_risk_events_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON demo_risk_events
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_demo_risk_event_mutation();
ALTER TABLE demo_risk_events ENABLE ALWAYS TRIGGER demo_risk_events_immutable;
