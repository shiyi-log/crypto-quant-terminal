"""全局显式数据库迁移；兼容检查只有 SELECT，不安装或修复对象。

调用者显式传入 psycopg 连接并管理最终 commit。SQL、旧事实导入和版本追加
处于同一事务；本模块不读取凭据、不创建连接、不管理进程。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import re

from psycopg import sql

LOGGER = logging.getLogger(__name__)
BASELINE_ID = '0001_global_baseline'
MANIFEST = {BASELINE_ID: 'daf64f37478c24274d922efbdeafb181b37cdd1bf1e951c8dd9f2f877c9d5718'}
HISTORY_FUNCTION = """BEGIN
    RAISE EXCEPTION 'Global migration history is immutable; append a new migration'
        USING ERRCODE='55000';
END;"""
EXPECTED_BASELINE_JSON = '{"columns":[{"default_expression":"\'{}\'::jsonb","name":"extras","required":true,"table_name":"candles","type":"jsonb"},{"default_expression":"\'{}\'::jsonb","name":"extras","required":true,"table_name":"orderbooks","type":"jsonb"},{"default_expression":"\'{}\'::jsonb","name":"extras","required":true,"table_name":"series_points","type":"jsonb"},{"default_expression":"\'{}\'::jsonb","name":"extras","required":true,"table_name":"ticks","type":"jsonb"},{"default_expression":"\'{}\'::jsonb","name":"metadata","required":true,"table_name":"artifacts","type":"jsonb"},{"default_expression":"0","name":"row_count","required":true,"table_name":"sources","type":"bigint"},{"default_expression":"0","name":"rows","required":true,"table_name":"stream_stats","type":"bigint"},{"default_expression":"CURRENT_TIMESTAMP","name":"created_at","required":true,"table_name":"orderflow_price_candidate_results","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"created_at","required":true,"table_name":"orderflow_price_candidates","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"created_at","required":true,"table_name":"orderflow_surge_events","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"created_at","required":true,"table_name":"research_ledger_events","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"created_at","required":true,"table_name":"strategy_sim_accounts","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"created_at","required":true,"table_name":"strategy_sim_events","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"last_synced_at","required":true,"table_name":"research_ledger_sync","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"received_at","required":true,"table_name":"orderbooks","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"received_at","required":true,"table_name":"ticks","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"updated_at","required":true,"table_name":"artifacts","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"updated_at","required":true,"table_name":"candle_datasets","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"updated_at","required":true,"table_name":"candles","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"updated_at","required":true,"table_name":"documents","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"updated_at","required":true,"table_name":"event_sources","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"updated_at","required":true,"table_name":"external_records","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"updated_at","required":true,"table_name":"series_points","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"updated_at","required":true,"table_name":"sources","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"updated_at","required":true,"table_name":"store_metadata","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"updated_at","required":true,"table_name":"strategy_sim_accounts","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"updated_at","required":true,"table_name":"stream_stats","type":"timestamp with time zone"},{"default_expression":"CURRENT_TIMESTAMP","name":"updated_at","required":true,"table_name":"users","type":"timestamp with time zone"},{"default_expression":"nextval(\'research_ledger_events_sequence_id_seq\'::regclass)","name":"sequence_id","required":true,"table_name":"research_ledger_events","type":"bigint"},{"default_expression":"nextval(\'strategy_sim_events_sequence_id_seq\'::regclass)","name":"sequence_id","required":true,"table_name":"strategy_sim_events","type":"bigint"},{"default_expression":null,"name":"asks","required":true,"table_name":"orderbooks","type":"jsonb"},{"default_expression":null,"name":"asset","required":true,"table_name":"series_points","type":"text"},{"default_expression":null,"name":"bids","required":true,"table_name":"orderbooks","type":"jsonb"},{"default_expression":null,"name":"buyer_maker","required":true,"table_name":"ticks","type":"boolean"},{"default_expression":null,"name":"candle_ms","required":false,"table_name":"strategy_sim_events","type":"bigint"},{"default_expression":null,"name":"candle_type","required":true,"table_name":"candle_datasets","type":"text"},{"default_expression":null,"name":"candle_type","required":true,"table_name":"candles","type":"text"},{"default_expression":null,"name":"close","required":true,"table_name":"candles","type":"double precision"},{"default_expression":null,"name":"data_fingerprint","required":true,"table_name":"orderflow_price_candidate_results","type":"text"},{"default_expression":null,"name":"data_fingerprint","required":true,"table_name":"orderflow_price_candidates","type":"text"},{"default_expression":null,"name":"data_fingerprint","required":true,"table_name":"orderflow_surge_events","type":"text"},{"default_expression":null,"name":"data_fingerprint","required":true,"table_name":"strategy_sim_events","type":"text"},{"default_expression":null,"name":"definition","required":true,"table_name":"strategy_sim_accounts","type":"jsonb"},{"default_expression":null,"name":"error","required":false,"table_name":"sources","type":"text"},{"default_expression":null,"name":"event_id","required":true,"table_name":"orderflow_price_candidate_results","type":"text"},{"default_expression":null,"name":"event_id","required":true,"table_name":"orderflow_price_candidates","type":"text"},{"default_expression":null,"name":"event_id","required":true,"table_name":"orderflow_surge_events","type":"text"},{"default_expression":null,"name":"event_id","required":true,"table_name":"research_ledger_events","type":"text"},{"default_expression":null,"name":"event_id","required":true,"table_name":"strategy_sim_events","type":"text"},{"default_expression":null,"name":"event_type","required":true,"table_name":"orderflow_surge_events","type":"text"},{"default_expression":null,"name":"event_type","required":true,"table_name":"research_ledger_events","type":"text"},{"default_expression":null,"name":"event_type","required":true,"table_name":"strategy_sim_events","type":"text"},{"default_expression":null,"name":"exchange","required":true,"table_name":"candle_datasets","type":"text"},{"default_expression":null,"name":"exchange","required":true,"table_name":"candles","type":"text"},{"default_expression":null,"name":"exchange","required":true,"table_name":"orderbooks","type":"text"},{"default_expression":null,"name":"exchange","required":true,"table_name":"orderflow_price_candidates","type":"text"},{"default_expression":null,"name":"exchange","required":true,"table_name":"stream_stats","type":"text"},{"default_expression":null,"name":"exchange","required":true,"table_name":"ticks","type":"text"},{"default_expression":null,"name":"execution","required":true,"table_name":"strategy_sim_accounts","type":"jsonb"},{"default_expression":null,"name":"execution_hash","required":true,"table_name":"strategy_sim_accounts","type":"text"},{"default_expression":null,"name":"fact_id","required":true,"table_name":"orderflow_price_candidate_results","type":"text"},{"default_expression":null,"name":"first_timestamp","required":false,"table_name":"candle_datasets","type":"bigint"},{"default_expression":null,"name":"first_timestamp","required":false,"table_name":"stream_stats","type":"bigint"},{"default_expression":null,"name":"high","required":true,"table_name":"candles","type":"double precision"},{"default_expression":null,"name":"initial_capital","required":true,"table_name":"strategy_sim_accounts","type":"numeric(30,12)"},{"default_expression":null,"name":"key","required":true,"table_name":"artifacts","type":"text"},{"default_expression":null,"name":"key","required":true,"table_name":"documents","type":"text"},{"default_expression":null,"name":"key","required":true,"table_name":"event_sources","type":"text"},{"default_expression":null,"name":"key","required":true,"table_name":"events","type":"text"},{"default_expression":null,"name":"key","required":true,"table_name":"sources","type":"text"},{"default_expression":null,"name":"key","required":true,"table_name":"store_metadata","type":"text"},{"default_expression":null,"name":"kind","required":true,"table_name":"artifacts","type":"text"},{"default_expression":null,"name":"kind","required":true,"table_name":"sources","type":"text"},{"default_expression":null,"name":"kind","required":true,"table_name":"stream_stats","type":"text"},{"default_expression":null,"name":"last_attempt_mtime_ns","required":false,"table_name":"sources","type":"bigint"},{"default_expression":null,"name":"last_attempt_size","required":false,"table_name":"sources","type":"bigint"},{"default_expression":null,"name":"last_success_at","required":false,"table_name":"sources","type":"timestamp with time zone"},{"default_expression":null,"name":"last_timestamp","required":false,"table_name":"candle_datasets","type":"bigint"},{"default_expression":null,"name":"last_timestamp","required":false,"table_name":"stream_stats","type":"bigint"},{"default_expression":null,"name":"last_update_id","required":true,"table_name":"orderbooks","type":"text"},{"default_expression":null,"name":"low","required":true,"table_name":"candles","type":"double precision"},{"default_expression":null,"name":"market","required":true,"table_name":"candle_datasets","type":"text"},{"default_expression":null,"name":"market","required":true,"table_name":"candles","type":"text"},{"default_expression":null,"name":"market","required":true,"table_name":"orderbooks","type":"text"},{"default_expression":null,"name":"market","required":true,"table_name":"orderflow_price_candidates","type":"text"},{"default_expression":null,"name":"market","required":true,"table_name":"orderflow_surge_events","type":"text"},{"default_expression":null,"name":"market","required":true,"table_name":"stream_stats","type":"text"},{"default_expression":null,"name":"market","required":true,"table_name":"ticks","type":"text"},{"default_expression":null,"name":"method_version","required":true,"table_name":"orderflow_price_candidate_results","type":"text"},{"default_expression":null,"name":"method_version","required":true,"table_name":"orderflow_price_candidates","type":"text"},{"default_expression":null,"name":"method_version","required":true,"table_name":"orderflow_surge_events","type":"text"},{"default_expression":null,"name":"metric","required":true,"table_name":"series_points","type":"text"},{"default_expression":null,"name":"mtime_ns","required":false,"table_name":"sources","type":"bigint"},{"default_expression":null,"name":"mtime_ns","required":true,"table_name":"artifacts","type":"bigint"},{"default_expression":null,"name":"observation","required":true,"table_name":"orderflow_price_candidates","type":"jsonb"},{"default_expression":null,"name":"observation","required":true,"table_name":"orderflow_surge_events","type":"jsonb"},{"default_expression":null,"name":"observed_at","required":false,"table_name":"orderflow_price_candidates","type":"bigint"},{"default_expression":null,"name":"observed_at","required":false,"table_name":"orderflow_surge_events","type":"bigint"},{"default_expression":null,"name":"observed_at_ms","required":true,"table_name":"strategy_sim_events","type":"bigint"},{"default_expression":null,"name":"open","required":true,"table_name":"candles","type":"double precision"},{"default_expression":null,"name":"ordinal","required":true,"table_name":"events","type":"bigint"},{"default_expression":null,"name":"pair","required":true,"table_name":"candle_datasets","type":"text"},{"default_expression":null,"name":"pair","required":true,"table_name":"candles","type":"text"},{"default_expression":null,"name":"pair","required":true,"table_name":"orderbooks","type":"text"},{"default_expression":null,"name":"pair","required":true,"table_name":"orderflow_price_candidates","type":"text"},{"default_expression":null,"name":"pair","required":true,"table_name":"orderflow_surge_events","type":"text"},{"default_expression":null,"name":"pair","required":true,"table_name":"stream_stats","type":"text"},{"default_expression":null,"name":"pair","required":true,"table_name":"ticks","type":"text"},{"default_expression":null,"name":"path","required":true,"table_name":"artifacts","type":"text"},{"default_expression":null,"name":"path","required":true,"table_name":"sources","type":"text"},{"default_expression":null,"name":"payload","required":true,"table_name":"documents","type":"jsonb"},{"default_expression":null,"name":"payload","required":true,"table_name":"events","type":"jsonb"},{"default_expression":null,"name":"payload","required":true,"table_name":"external_records","type":"jsonb"},{"default_expression":null,"name":"payload","required":true,"table_name":"orderflow_surge_events","type":"jsonb"},{"default_expression":null,"name":"payload","required":true,"table_name":"research_ledger_events","type":"jsonb"},{"default_expression":null,"name":"payload","required":true,"table_name":"store_metadata","type":"jsonb"},{"default_expression":null,"name":"payload","required":true,"table_name":"strategy_sim_events","type":"jsonb"},{"default_expression":null,"name":"payload","required":true,"table_name":"users","type":"jsonb"},{"default_expression":null,"name":"price","required":true,"table_name":"ticks","type":"numeric(38,18)"},{"default_expression":null,"name":"quantity","required":true,"table_name":"ticks","type":"numeric(38,18)"},{"default_expression":null,"name":"record_key","required":true,"table_name":"external_records","type":"text"},{"default_expression":null,"name":"result","required":false,"table_name":"orderflow_surge_events","type":"jsonb"},{"default_expression":null,"name":"result","required":true,"table_name":"orderflow_price_candidate_results","type":"jsonb"},{"default_expression":null,"name":"result_kind","required":true,"table_name":"orderflow_price_candidate_results","type":"text"},{"default_expression":null,"name":"revision_id","required":true,"table_name":"orderflow_surge_events","type":"text"},{"default_expression":null,"name":"revision_kind","required":true,"table_name":"orderflow_surge_events","type":"text"},{"default_expression":null,"name":"revision_no","required":true,"table_name":"orderflow_surge_events","type":"bigint"},{"default_expression":null,"name":"rows","required":true,"table_name":"candle_datasets","type":"bigint"},{"default_expression":null,"name":"rule_hash","required":true,"table_name":"strategy_sim_accounts","type":"text"},{"default_expression":null,"name":"rule_hash","required":true,"table_name":"strategy_sim_events","type":"text"},{"default_expression":null,"name":"size","required":false,"table_name":"sources","type":"bigint"},{"default_expression":null,"name":"size","required":true,"table_name":"artifacts","type":"bigint"},{"default_expression":null,"name":"source","required":true,"table_name":"external_records","type":"text"},{"default_expression":null,"name":"source","required":true,"table_name":"series_points","type":"text"},{"default_expression":null,"name":"source_fingerprint","required":true,"table_name":"research_ledger_sync","type":"text"},{"default_expression":null,"name":"source_id","required":false,"table_name":"orderflow_price_candidates","type":"text"},{"default_expression":null,"name":"source_id","required":false,"table_name":"orderflow_surge_events","type":"text"},{"default_expression":null,"name":"source_id","required":false,"table_name":"research_ledger_events","type":"text"},{"default_expression":null,"name":"source_id","required":true,"table_name":"research_ledger_sync","type":"text"},{"default_expression":null,"name":"source_key","required":false,"table_name":"candles","type":"text"},{"default_expression":null,"name":"source_key","required":false,"table_name":"series_points","type":"text"},{"default_expression":null,"name":"source_mtime_ns","required":false,"table_name":"documents","type":"bigint"},{"default_expression":null,"name":"source_mtime_ns","required":false,"table_name":"event_sources","type":"bigint"},{"default_expression":null,"name":"source_mtime_ns","required":false,"table_name":"research_ledger_sync","type":"bigint"},{"default_expression":null,"name":"source_path","required":false,"table_name":"documents","type":"text"},{"default_expression":null,"name":"source_path","required":false,"table_name":"event_sources","type":"text"},{"default_expression":null,"name":"state","required":true,"table_name":"strategy_sim_accounts","type":"jsonb"},{"default_expression":null,"name":"status","required":true,"table_name":"orderflow_price_candidate_results","type":"text"},{"default_expression":null,"name":"status","required":true,"table_name":"orderflow_surge_events","type":"text"},{"default_expression":null,"name":"strategy_id","required":true,"table_name":"strategy_sim_accounts","type":"text"},{"default_expression":null,"name":"strategy_id","required":true,"table_name":"strategy_sim_events","type":"text"},{"default_expression":null,"name":"table_name","required":true,"table_name":"external_records","type":"text"},{"default_expression":null,"name":"timeframe","required":true,"table_name":"candle_datasets","type":"text"},{"default_expression":null,"name":"timeframe","required":true,"table_name":"candles","type":"text"},{"default_expression":null,"name":"timeframe","required":true,"table_name":"orderflow_price_candidates","type":"text"},{"default_expression":null,"name":"timeframe","required":true,"table_name":"orderflow_surge_events","type":"text"},{"default_expression":null,"name":"timestamp","required":true,"table_name":"candles","type":"bigint"},{"default_expression":null,"name":"timestamp","required":true,"table_name":"orderbooks","type":"bigint"},{"default_expression":null,"name":"timestamp","required":true,"table_name":"orderflow_price_candidates","type":"bigint"},{"default_expression":null,"name":"timestamp","required":true,"table_name":"orderflow_surge_events","type":"bigint"},{"default_expression":null,"name":"timestamp","required":true,"table_name":"series_points","type":"bigint"},{"default_expression":null,"name":"timestamp","required":true,"table_name":"ticks","type":"bigint"},{"default_expression":null,"name":"trade_id","required":true,"table_name":"ticks","type":"text"},{"default_expression":null,"name":"username","required":true,"table_name":"users","type":"text"},{"default_expression":null,"name":"value","required":true,"table_name":"series_points","type":"double precision"},{"default_expression":null,"name":"volume","required":true,"table_name":"candles","type":"double precision"}],"constraints":[{"definition":"CHECK (initial_capital > 0::numeric)","kind":"c","table_name":"strategy_sim_accounts","validated":true},{"definition":"CHECK (result_kind = ANY (ARRAY[\'outcome\'::text, \'profile\'::text]))","kind":"c","table_name":"orderflow_price_candidate_results","validated":true},{"definition":"CHECK (revision_kind = ANY (ARRAY[\'observed\'::text, \'maturity\'::text]))","kind":"c","table_name":"orderflow_surge_events","validated":true},{"definition":"CHECK (status = ANY (ARRAY[\'matured\'::text, \'invalid\'::text]))","kind":"c","table_name":"orderflow_price_candidate_results","validated":true},{"definition":"CHECK (status = ANY (ARRAY[\'unknown\'::text, \'matured\'::text, \'invalid\'::text]))","kind":"c","table_name":"orderflow_surge_events","validated":true},{"definition":"FOREIGN KEY (event_id) REFERENCES orderflow_price_candidates(event_id)","kind":"f","table_name":"orderflow_price_candidate_results","validated":true},{"definition":"FOREIGN KEY (key) REFERENCES event_sources(key) ON DELETE CASCADE","kind":"f","table_name":"events","validated":true},{"definition":"FOREIGN KEY (strategy_id) REFERENCES strategy_sim_accounts(strategy_id)","kind":"f","table_name":"strategy_sim_events","validated":true},{"definition":"PRIMARY KEY (event_id)","kind":"p","table_name":"orderflow_price_candidates","validated":true},{"definition":"PRIMARY KEY (event_id)","kind":"p","table_name":"research_ledger_events","validated":true},{"definition":"PRIMARY KEY (event_id)","kind":"p","table_name":"strategy_sim_events","validated":true},{"definition":"PRIMARY KEY (exchange, market, pair, \\"timestamp\\", last_update_id)","kind":"p","table_name":"orderbooks","validated":true},{"definition":"PRIMARY KEY (exchange, market, pair, timeframe, candle_type)","kind":"p","table_name":"candle_datasets","validated":true},{"definition":"PRIMARY KEY (exchange, market, pair, timeframe, candle_type, \\"timestamp\\")","kind":"p","table_name":"candles","validated":true},{"definition":"PRIMARY KEY (exchange, market, pair, trade_id)","kind":"p","table_name":"ticks","validated":true},{"definition":"PRIMARY KEY (fact_id)","kind":"p","table_name":"orderflow_price_candidate_results","validated":true},{"definition":"PRIMARY KEY (key)","kind":"p","table_name":"artifacts","validated":true},{"definition":"PRIMARY KEY (key)","kind":"p","table_name":"documents","validated":true},{"definition":"PRIMARY KEY (key)","kind":"p","table_name":"event_sources","validated":true},{"definition":"PRIMARY KEY (key)","kind":"p","table_name":"sources","validated":true},{"definition":"PRIMARY KEY (key)","kind":"p","table_name":"store_metadata","validated":true},{"definition":"PRIMARY KEY (key, ordinal)","kind":"p","table_name":"events","validated":true},{"definition":"PRIMARY KEY (kind, exchange, market, pair)","kind":"p","table_name":"stream_stats","validated":true},{"definition":"PRIMARY KEY (revision_id)","kind":"p","table_name":"orderflow_surge_events","validated":true},{"definition":"PRIMARY KEY (source, metric, asset, \\"timestamp\\")","kind":"p","table_name":"series_points","validated":true},{"definition":"PRIMARY KEY (source, table_name, record_key)","kind":"p","table_name":"external_records","validated":true},{"definition":"PRIMARY KEY (source_id)","kind":"p","table_name":"research_ledger_sync","validated":true},{"definition":"PRIMARY KEY (strategy_id)","kind":"p","table_name":"strategy_sim_accounts","validated":true},{"definition":"PRIMARY KEY (username)","kind":"p","table_name":"users","validated":true},{"definition":"UNIQUE (event_id, result_kind)","kind":"u","table_name":"orderflow_price_candidate_results","validated":true},{"definition":"UNIQUE (event_id, revision_no)","kind":"u","table_name":"orderflow_surge_events","validated":true},{"definition":"UNIQUE (sequence_id)","kind":"u","table_name":"research_ledger_events","validated":true},{"definition":"UNIQUE (sequence_id)","kind":"u","table_name":"strategy_sim_events","validated":true}],"indexes":[{"key_count":1,"keys":["\\"timestamp\\""],"method":"brin","options":[0],"predicate":null,"ready":true,"table_name":"orderbooks","unique":false,"valid":true},{"key_count":1,"keys":["\\"timestamp\\""],"method":"brin","options":[0],"predicate":null,"ready":true,"table_name":"ticks","unique":false,"valid":true},{"key_count":1,"keys":["event_id"],"method":"btree","options":[0],"predicate":"(revision_kind = \'observed\'::text)","ready":true,"table_name":"orderflow_surge_events","unique":true,"valid":true},{"key_count":1,"keys":["event_id"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"orderflow_price_candidates","unique":true,"valid":true},{"key_count":1,"keys":["event_id"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"research_ledger_events","unique":true,"valid":true},{"key_count":1,"keys":["event_id"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"strategy_sim_events","unique":true,"valid":true},{"key_count":1,"keys":["fact_id"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"orderflow_price_candidate_results","unique":true,"valid":true},{"key_count":1,"keys":["key"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"artifacts","unique":true,"valid":true},{"key_count":1,"keys":["key"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"documents","unique":false,"valid":true},{"key_count":1,"keys":["key"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"documents","unique":true,"valid":true},{"key_count":1,"keys":["key"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"event_sources","unique":true,"valid":true},{"key_count":1,"keys":["key"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"sources","unique":true,"valid":true},{"key_count":1,"keys":["key"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"store_metadata","unique":true,"valid":true},{"key_count":1,"keys":["revision_id"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"orderflow_surge_events","unique":true,"valid":true},{"key_count":1,"keys":["sequence_id"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"research_ledger_events","unique":true,"valid":true},{"key_count":1,"keys":["sequence_id"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"strategy_sim_events","unique":true,"valid":true},{"key_count":1,"keys":["source_id"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"research_ledger_sync","unique":true,"valid":true},{"key_count":1,"keys":["strategy_id"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"strategy_sim_accounts","unique":true,"valid":true},{"key_count":1,"keys":["username"],"method":"btree","options":[0],"predicate":null,"ready":true,"table_name":"users","unique":true,"valid":true},{"key_count":2,"keys":["event_id","result_kind"],"method":"btree","options":[0,0],"predicate":null,"ready":true,"table_name":"orderflow_price_candidate_results","unique":true,"valid":true},{"key_count":2,"keys":["event_id","revision_no"],"method":"btree","options":[0,0],"predicate":null,"ready":true,"table_name":"orderflow_surge_events","unique":true,"valid":true},{"key_count":2,"keys":["event_id","revision_no"],"method":"btree","options":[0,3],"predicate":null,"ready":true,"table_name":"orderflow_surge_events","unique":false,"valid":true},{"key_count":2,"keys":["event_type","created_at"],"method":"btree","options":[0,0],"predicate":null,"ready":true,"table_name":"research_ledger_events","unique":false,"valid":true},{"key_count":2,"keys":["key","ordinal"],"method":"btree","options":[0,0],"predicate":null,"ready":true,"table_name":"events","unique":true,"valid":true},{"key_count":2,"keys":["strategy_id","sequence_id"],"method":"btree","options":[0,3],"predicate":null,"ready":true,"table_name":"strategy_sim_events","unique":false,"valid":true},{"key_count":3,"keys":["result_kind","status","event_id"],"method":"btree","options":[0,0,0],"predicate":null,"ready":true,"table_name":"orderflow_price_candidate_results","unique":false,"valid":true},{"key_count":3,"keys":["source","table_name","record_key"],"method":"btree","options":[0,0,0],"predicate":null,"ready":true,"table_name":"external_records","unique":true,"valid":true},{"key_count":3,"keys":["strategy_id","event_type","sequence_id"],"method":"btree","options":[0,0,3],"predicate":null,"ready":true,"table_name":"strategy_sim_events","unique":false,"valid":true},{"key_count":4,"keys":["exchange","market","pair","\\"timestamp\\""],"method":"btree","options":[0,0,0,3],"predicate":null,"ready":true,"table_name":"orderbooks","unique":false,"valid":true},{"key_count":4,"keys":["exchange","market","pair","trade_id"],"method":"btree","options":[0,0,0,0],"predicate":null,"ready":true,"table_name":"ticks","unique":true,"valid":true},{"key_count":4,"keys":["kind","exchange","market","pair"],"method":"btree","options":[0,0,0,0],"predicate":null,"ready":true,"table_name":"stream_stats","unique":true,"valid":true},{"key_count":4,"keys":["market","pair","timeframe","\\"timestamp\\""],"method":"btree","options":[0,0,0,3],"predicate":null,"ready":true,"table_name":"orderflow_price_candidates","unique":false,"valid":true},{"key_count":4,"keys":["market","pair","timeframe","\\"timestamp\\""],"method":"btree","options":[0,0,0,3],"predicate":null,"ready":true,"table_name":"orderflow_surge_events","unique":false,"valid":true},{"key_count":4,"keys":["source","metric","asset","\\"timestamp\\""],"method":"btree","options":[0,0,0,0],"predicate":null,"ready":true,"table_name":"series_points","unique":true,"valid":true},{"key_count":5,"keys":["exchange","market","pair","\\"timestamp\\"","last_update_id"],"method":"btree","options":[0,0,0,0,0],"predicate":null,"ready":true,"table_name":"orderbooks","unique":true,"valid":true},{"key_count":5,"keys":["exchange","market","pair","\\"timestamp\\"","trade_id"],"method":"btree","options":[0,0,0,3,3],"predicate":null,"ready":true,"table_name":"ticks","unique":false,"valid":true},{"key_count":5,"keys":["exchange","market","pair","timeframe","candle_type"],"method":"btree","options":[0,0,0,0,0],"predicate":null,"ready":true,"table_name":"candle_datasets","unique":true,"valid":true},{"key_count":6,"keys":["exchange","market","pair","\\"timestamp\\"","length(last_update_id)","last_update_id"],"method":"btree","options":[0,0,0,3,3,3],"predicate":null,"ready":true,"table_name":"orderbooks","unique":false,"valid":true},{"key_count":6,"keys":["exchange","market","pair","\\"timestamp\\"","length(trade_id)","trade_id"],"method":"btree","options":[0,0,0,3,3,3],"predicate":null,"ready":true,"table_name":"ticks","unique":false,"valid":true},{"key_count":6,"keys":["exchange","market","pair","timeframe","candle_type","\\"timestamp\\""],"method":"btree","options":[0,0,0,0,0,0],"predicate":null,"ready":true,"table_name":"candles","unique":true,"valid":true}],"relations":[{"force_row_security":false,"kind":"r","row_security":false,"table_name":"artifacts"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"candle_datasets"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"candles"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"documents"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"event_sources"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"events"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"external_records"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"orderbooks"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"orderflow_price_candidate_results"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"orderflow_price_candidates"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"orderflow_surge_events"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"research_ledger_events"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"research_ledger_sync"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"series_points"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"sources"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"store_metadata"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"strategy_sim_accounts"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"strategy_sim_events"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"stream_stats"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"ticks"},{"force_row_security":false,"kind":"r","row_security":false,"table_name":"users"}],"rules":[],"triggers":[{"condition":null,"configuration":null,"enabled":"A","event_mask":58,"function_body":"\\nBEGIN\\n    RAISE EXCEPTION \'Research ledger events are immutable; append a new fact instead\'\\n        USING ERRCODE = \'55000\';\\nEND;\\n","function_name":"research_ledger_immutable_event","function_schema":"$schema","language":"plpgsql","name":"research_ledger_events_immutable","security_definer":false,"table_name":"research_ledger_events","volatility":"v"},{"condition":null,"configuration":null,"enabled":"O","event_mask":19,"function_body":"\\nBEGIN\\n    IF OLD.strategy_id IS DISTINCT FROM NEW.strategy_id\\n       OR OLD.rule_hash IS DISTINCT FROM NEW.rule_hash\\n       OR OLD.execution_hash IS DISTINCT FROM NEW.execution_hash\\n       OR OLD.initial_capital IS DISTINCT FROM NEW.initial_capital\\n       OR OLD.definition IS DISTINCT FROM NEW.definition\\n       OR (\\n            OLD.execution IS DISTINCT FROM NEW.execution\\n            AND NOT (\\n                (OLD.execution - \'execution_enabled\') IS NOT DISTINCT FROM\\n                    (NEW.execution - \'execution_enabled\')\\n                AND OLD.execution->>\'execution_enabled\' IS DISTINCT FROM \'false\'\\n                AND NEW.execution->>\'execution_enabled\' = \'false\'\\n            )\\n          ) THEN\\n        RAISE EXCEPTION \'Rule or capital change requires a new strategy identity\';\\n    END IF;\\n    RETURN NEW;\\nEND;\\n","function_name":"strategy_sim_immutable_account","function_schema":"$schema","language":"plpgsql","name":"strategy_sim_account_identity","security_definer":false,"table_name":"strategy_sim_accounts","volatility":"v"},{"condition":null,"configuration":null,"enabled":"O","event_mask":27,"function_body":"\\nBEGIN\\n    RAISE EXCEPTION \'Strategy simulation events are immutable; append a new fact instead\';\\nEND;\\n","function_name":"strategy_sim_immutable_event","function_schema":"$schema","language":"plpgsql","name":"strategy_sim_events_immutable","security_definer":false,"table_name":"strategy_sim_events","volatility":"v"}]}'


class MigrationError(RuntimeError):
    """有限诊断码，不包含 DSN、原始 SQL 或服务端异常文本。"""


@dataclass(frozen=True, slots=True)
class Migration:
    migration_id: str
    checksum: str
    source: str


def migration_bundle(migration_dir=None):
    directory = Path(migration_dir or Path(__file__).parent / 'database/migrations')
    actual = {path.stem: path for path in directory.glob('*.sql')} if directory.is_dir() else {}
    if set(actual) != set(MANIFEST):
        raise MigrationError('global migration bundle incomplete or unknown')
    bundle = []
    for identity, digest in MANIFEST.items():
        raw = actual[identity].read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise MigrationError('global migration bundle checksum mismatch')
        bundle.append(Migration(identity, digest, raw.decode('utf-8')))
    return tuple(bundle)


def _scalar(row, name):
    return row[name] if isinstance(row, dict) else row[0]


def _rows(conn, statement, params=()):
    cursor = conn.execute(statement, params)
    values = cursor.fetchall()
    if not values or isinstance(values[0], dict):
        return values
    keys = [column.name for column in cursor.description]
    return [dict(zip(keys, row)) for row in values]


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _normal(value, schema):
    # 函数体/条件表达式的字面量与空格均有语义，绝不做全局replace或split。
    if value is None:
        return None
    # SQL字面量原样保留，只移除非字面量中的准确schema标识符限定。
    parts = re.split(r"('(?:''|[^'])*')", str(value))
    quoted = '"' + schema.replace('"', '""') + '"'
    for index in range(0, len(parts), 2):
        parts[index] = parts[index].replace(quoted + '.', '')
        parts[index] = re.sub(r'(?<![A-Za-z0-9_$])' + re.escape(schema) + r'\.', '', parts[index])
    return ''.join(parts)


def _default_expression(value, schema):
    if value is None:
        return None
    # 仅处理catalog生成的nextval序列限定符；其它字面量逐字保留。
    quoted = '"' + schema.replace('"', '""') + '"'
    for qualifier in (quoted, schema):
        prefix = "nextval('" + qualifier + "."
        if value.startswith(prefix) and value.endswith("'::regclass)"):
            return "nextval('" + value[len(prefix):]
    return value


def catalog_shape(conn, *, schema):
    """读取规范catalog，不扫描业务大表，不生成DDL。"""
    relations = _rows(conn, """SELECT c.relname AS table_name,c.relkind AS kind,
        c.relrowsecurity AS row_security,c.relforcerowsecurity AS force_row_security
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind IN ('r','p') ORDER BY c.relname
        """, (schema,))
    columns = _rows(conn, """SELECT c.relname AS table_name,a.attname AS name,
        pg_catalog.format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull AS required,
        pg_get_expr(d.adbin,d.adrelid) AS default_expression
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE n.nspname=%s AND c.relkind='r' ORDER BY c.relname,a.attnum
        """, (schema,))
    constraints = _rows(conn, """SELECT c.relname AS table_name,ct.contype AS kind,
        pg_get_constraintdef(ct.oid,true) AS definition,ct.convalidated AS validated
        FROM pg_constraint ct JOIN pg_class c ON c.oid=ct.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s ORDER BY c.relname,ct.contype,pg_get_constraintdef(ct.oid,true)
        """, (schema,))
    indexes = _rows(conn, """SELECT c.relname AS table_name,am.amname AS method,
        i.indisunique AS unique,i.indisvalid AS valid,i.indisready AS ready,i.indnkeyatts AS key_count,
        i.indoption::smallint[] AS options,
        ARRAY(SELECT pg_get_indexdef(i.indexrelid,k,true)
              FROM generate_series(1,i.indnatts) k ORDER BY k) AS keys,
        pg_get_expr(i.indpred,i.indrelid) AS predicate
        FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
        JOIN pg_class ic ON ic.oid=i.indexrelid JOIN pg_am am ON am.oid=ic.relam
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s ORDER BY c.relname,i.indexrelid
        """, (schema,))
    triggers = _rows(conn, """SELECT c.relname AS table_name,t.tgname AS name,
        t.tgtype AS event_mask,t.tgenabled AS enabled,fn.nspname AS function_schema,
        p.proname AS function_name,p.prosrc AS function_body,l.lanname AS language,
        p.prosecdef AS security_definer,p.provolatile AS volatility,
        p.proconfig AS configuration,pg_get_expr(t.tgqual,t.tgrelid) AS condition
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid
        JOIN pg_namespace fn ON fn.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname=%s AND NOT t.tgisinternal ORDER BY c.relname,t.tgname
        """, (schema,))
    rules = _rows(conn, """SELECT c.relname AS table_name,r.rulename AS name,
        pg_get_ruledef(r.oid,true) AS definition FROM pg_rewrite r
        JOIN pg_class c ON c.oid=r.ev_class JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s ORDER BY c.relname,r.rulename """, (schema,))
    result = {}
    for kind, rows in (('relations', relations), ('columns', columns), ('constraints', constraints),
                       ('indexes', indexes), ('triggers', triggers), ('rules', rules)):
        normalized = []
        for row in rows:
            entry = dict(row)
            for key in ('default_expression', 'definition', 'predicate', 'function_body', 'condition'):
                if key in entry:
                    entry[key] = (_default_expression(entry[key], schema) if key == 'default_expression'
                                  else entry[key] if key == 'function_body' else _normal(entry[key], schema))
            if 'function_schema' in entry:
                entry['function_schema'] = '$schema' if entry['function_schema'] == schema else entry['function_schema']
            if 'keys' in entry:
                entry['keys'] = [_normal(key, schema) for key in entry['keys']]
            normalized.append(entry)
        result[kind] = sorted(normalized, key=_canonical)
    return result


def _expected():
    value = json.loads(EXPECTED_BASELINE_JSON)
    if not value or not all(key in value for key in ('columns', 'constraints', 'indexes', 'triggers', 'relations', 'rules')):
        raise MigrationError('global baseline shape anchor missing')
    return value


def _verify_shape(conn, schema):
    expected, actual = _expected(), catalog_shape(conn, schema=schema)
    # 独立Demo域允许附加表/约束；冻结全局对象的语义必须仍然成立。
    for kind in expected:
        present = {_canonical(row) for row in actual[kind]}
        if any(_canonical(row) not in present for row in expected[kind]):
            raise MigrationError('global baseline shape mismatch: ' + kind)
    frozen_tables = {row['table_name'] for row in expected['relations']}
    for kind in ('relations', 'columns', 'triggers', 'rules'):
        wanted = {_canonical(row) for row in expected[kind]}
        present = {_canonical(row) for row in actual[kind] if row['table_name'] in frozen_tables}
        if wanted != present:
            raise MigrationError('global baseline shape unexpected objects: ' + kind)
    return _hash(expected)


def _history_exists(conn, schema):
    row = conn.execute("""SELECT EXISTS(SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relname='global_schema_migrations' AND c.relkind='r') AS found
        """, (schema,)).fetchone()
    return _scalar(row, 'found')


def _history_records(conn, schema):
    return _rows(conn, sql.SQL('SELECT migration_id,checksum FROM {}.global_schema_migrations ORDER BY migration_id')
                 .format(sql.Identifier(schema)))


def _validate_history(rows, bundle, *, require_current):
    expected = {item.migration_id: item.checksum for item in bundle}
    existing = {}
    for row in rows:
        identity = row['migration_id']
        if identity in existing or identity not in expected:
            raise MigrationError('unknown or duplicate global migration history')
        if row['checksum'] != expected[identity]:
            raise MigrationError('global migration history checksum mismatch')
        existing[identity] = row['checksum']
    seen_missing = False
    for identity in expected:
        if identity not in existing:
            seen_missing = True
        elif seen_missing:
            raise MigrationError('global migration history gap')
    if require_current and set(existing) != set(expected):
        raise MigrationError('global migrations pending; explicit migrate required')
    return existing


def _verify_history_guard(conn, schema):
    rows = _rows(conn, """SELECT t.tgtype AS mask,t.tgenabled AS enabled,
        p.prosrc AS source,p.prosecdef AS security_definer,p.proconfig AS configuration,
        fn.nspname AS function_schema,l.lanname AS language,
        pg_get_expr(t.tgqual,t.tgrelid) AS condition
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid
        JOIN pg_namespace fn ON fn.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname=%s AND c.relname='global_schema_migrations'
          AND t.tgname='global_schema_migrations_immutable' AND NOT t.tgisinternal
        """, (schema,))
    if (len(rows) != 1 or rows[0]['mask'] != 58 or rows[0]['enabled'] != 'A'
            or rows[0]['function_schema'] != schema or rows[0]['language'] != 'plpgsql'
            or rows[0]['security_definer'] or rows[0]['configuration'] is not None
            or rows[0]['condition'] is not None
            or _normal(rows[0]['source'], schema) != _normal(HISTORY_FUNCTION, schema)):
        raise MigrationError('global migration history shape protection mismatch')


def check_schema(conn, *, schema='quant', migration_dir=None):
    """只读核对完整版本、不可变历史及冻结物理语义，不自动修复。"""
    bundle = migration_bundle(migration_dir)
    if not _history_exists(conn, schema):
        raise MigrationError('global migrations missing; explicit migrate required')
    _verify_history_guard(conn, schema)
    records = _history_records(conn, schema)
    _validate_history(records, bundle, require_current=True)
    digest = _verify_shape(conn, schema)
    return {'status': 'compatible', 'versions': [item.migration_id for item in bundle],
            'shape_sha256': digest}


def _backup_evidence(value):
    if (not isinstance(value, dict) or value.get('business_validation') != 'passed'
            or not isinstance(value.get('reference'), str)
            or not re.fullmatch(r'[A-Za-z0-9_.:/@-]{1,256}', value['reference'])
            or not isinstance(value.get('archive_sha256'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', value['archive_sha256'])):
        raise MigrationError('verified backup and independent restore evidence required for adoption')
    return {key: value[key] for key in ('reference', 'archive_sha256', 'business_validation')}


def migrate_schema(conn, *, schema='quant', migration_dir=None, adopt=False,
                   backup_evidence=None, legacy_import=None):
    """显式安装空schema，或凭备份证据接管已验证的无版本schema。

    失败回滚本次savepoint，调用者提交外层事务。legacy_import无网络且须幂等；
    存在旧订单流表时未提供该回调则拒绝登记成功。
    """
    if not isinstance(schema, str) or not schema or '\x00' in schema or len(schema.encode()) > 63:
        raise MigrationError('invalid schema identifier')
    if type(adopt) is not bool or (legacy_import is not None and not callable(legacy_import)):
        raise MigrationError('explicit migration arguments invalid')
    bundle = migration_bundle(migration_dir)
    from psycopg.types.json import Jsonb
    applied, adopted = [], False
    with conn.transaction():
        previous = _scalar(conn.execute('SHOW search_path').fetchone(), 'search_path')
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('quant-schema:' + schema,))
        conn.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(schema)))
        if _history_exists(conn, schema):
            check_schema(conn, schema=schema, migration_dir=migration_dir)
        else:
            tables = _rows(conn, """SELECT c.relname AS name FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f')
                """, (schema,))
            backup = None
            if tables:
                if not adopt:
                    raise MigrationError('unversioned existing schema requires explicit adopt')
                backup = _backup_evidence(backup_evidence)
                _verify_shape(conn, schema)
                adopted = True
            else:
                if adopt:
                    raise MigrationError('adopt requires existing baseline shape')
                conn.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)))
                conn.execute(bundle[0].source, prepare=False)
                _verify_shape(conn, schema)
            legacy = _rows(conn, """SELECT c.relname AS name FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relname IN
                ('orderflow_surge_observations','orderflow_surge_events_legacy_v3')
                """, (schema,))
            if legacy and legacy_import is None:
                raise MigrationError('legacy facts require explicit legacy import callback')
            if legacy_import is not None:
                legacy_import(conn)
            conn.execute("""CREATE TABLE global_schema_migrations(
                migration_id TEXT PRIMARY KEY,checksum TEXT NOT NULL,
                shape_sha256 TEXT NOT NULL,adopted BOOLEAN NOT NULL,backup_evidence JSONB,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)
                """)
            conn.execute(sql.SQL('CREATE FUNCTION prevent_global_migration_mutation() RETURNS trigger LANGUAGE plpgsql AS {}')
                         .format(sql.Literal(HISTORY_FUNCTION)))
            conn.execute("""CREATE TRIGGER global_schema_migrations_immutable
                BEFORE UPDATE OR DELETE OR TRUNCATE ON global_schema_migrations
                FOR EACH STATEMENT EXECUTE FUNCTION prevent_global_migration_mutation()""")
            conn.execute('ALTER TABLE global_schema_migrations ENABLE ALWAYS TRIGGER global_schema_migrations_immutable')
            digest = _verify_shape(conn, schema)
            conn.execute("""INSERT INTO global_schema_migrations
                (migration_id,checksum,shape_sha256,adopted,backup_evidence) VALUES(%s,%s,%s,%s,%s)""",
                (bundle[0].migration_id, bundle[0].checksum, digest, adopted, Jsonb(backup) if backup else None))
            applied.append(bundle[0].migration_id)
            check_schema(conn, schema=schema, migration_dir=migration_dir)
        conn.execute('SELECT set_config(%s,%s,true)', ('search_path', previous))
    LOGGER.info('数据库显式迁移 schema=%s applied_count=%s adopted=%s；未启动交易或修改账户状态',
                schema, len(applied), adopted)
    return {'status': 'migrated', 'applied': applied, 'adopted': adopted}
