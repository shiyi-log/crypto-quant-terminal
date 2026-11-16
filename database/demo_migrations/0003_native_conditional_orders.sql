-- 原生条件意图和事件独立于普通订单；条件触发不表示实际成交或空仓。
CREATE TABLE demo_conditional_intents (
    environment TEXT NOT NULL CHECK (environment='binance_demo'),
    virtual_account_key TEXT NOT NULL,
    client_algo_id TEXT NOT NULL CHECK (client_algo_id ~ '^qd-[a-f0-9]{32}$'),
    strategy_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    parent_entry_client_id TEXT NOT NULL,
    order_type TEXT NOT NULL CHECK (order_type IN ('STOP_MARKET','TAKE_PROFIT_MARKET')),
    revision BIGINT NOT NULL CHECK (revision>0),
    quantity NUMERIC NOT NULL CHECK (quantity>0 AND quantity NOT IN ('NaN','Infinity','-Infinity')),
    trigger_price NUMERIC NOT NULL CHECK (trigger_price>0 AND trigger_price NOT IN ('NaN','Infinity','-Infinity')),
    intent_hash TEXT NOT NULL CHECK (intent_hash ~ '^[a-f0-9]{64}$'),
    payload JSONB NOT NULL,
    PRIMARY KEY (environment,virtual_account_key,client_algo_id),
    UNIQUE (environment,virtual_account_key,strategy_id,run_id,parent_entry_client_id,order_type,revision)
);

CREATE TABLE demo_conditional_events (
    sequence_id BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    environment TEXT NOT NULL,
    virtual_account_key TEXT NOT NULL,
    client_algo_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN
        ('reserved','submission_claimed','submission_unknown','lookup_absent','observed','cancel_ack')),
    observed_ms BIGINT NOT NULL CHECK (observed_ms>=0),
    payload_hash TEXT NOT NULL CHECK (payload_hash ~ '^[a-f0-9]{64}$'),
    payload JSONB NOT NULL,
    PRIMARY KEY (environment,virtual_account_key,event_id),
    FOREIGN KEY (environment,virtual_account_key,client_algo_id)
        REFERENCES demo_conditional_intents(environment,virtual_account_key,client_algo_id)
);
CREATE INDEX demo_conditional_events_replay ON demo_conditional_events
    (environment,virtual_account_key,client_algo_id,sequence_id);

-- 可重建投影不具备独立授权效力；发送资格必须与不可变事件一起核对。
CREATE TABLE demo_conditional_current (
    environment TEXT NOT NULL,
    virtual_account_key TEXT NOT NULL,
    client_algo_id TEXT NOT NULL,
    status TEXT NOT NULL,
    submission_attempts SMALLINT NOT NULL CHECK (submission_attempts IN (0,1)),
    algo_id TEXT,
    payload JSONB NOT NULL,
    PRIMARY KEY (environment,virtual_account_key,client_algo_id),
    FOREIGN KEY (environment,virtual_account_key,client_algo_id)
        REFERENCES demo_conditional_intents(environment,virtual_account_key,client_algo_id),
    UNIQUE (environment,virtual_account_key,algo_id)
);

CREATE FUNCTION prevent_demo_conditional_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Demo conditional intents/events are immutable; append a new fact'
        USING ERRCODE='55000';
END;
$$;
CREATE TRIGGER demo_conditional_intents_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON demo_conditional_intents
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_demo_conditional_mutation();
CREATE TRIGGER demo_conditional_events_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON demo_conditional_events
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_demo_conditional_mutation();
ALTER TABLE demo_conditional_intents ENABLE ALWAYS TRIGGER demo_conditional_intents_immutable;
ALTER TABLE demo_conditional_events ENABLE ALWAYS TRIGGER demo_conditional_events_immutable;
