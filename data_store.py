"""前后端统一 PostgreSQL 数据层，不改变模型文件与 Freqtrade 数据库。"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from price_spike import PRICE_SPIKE_METHOD_VERSION


def _json_default(value):
    """兼容采集端的日期及 NumPy 标量，拒绝无法确定含义的对象。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"不能写入 JSON 的类型: {type(value).__name__}")


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"), default=_json_default)


def _number(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是有限数值")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} 必须是有限数值")
    return result


def _decimal(value, name):
    """逐笔价量保持交易所十进制精度，拒绝数据库隐式舍入。"""
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是有限十进制数")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} 必须是有限十进制数") from exc
    if not result.is_finite():
        raise ValueError(f"{name} 必须是有限十进制数")
    digits = result.as_tuple()
    if result != 0 and (result.adjusted() >= 20 or digits.exponent < -18):
        # 数据库字段 NUMERIC(38,18) 允许 20 位整数与 18 位小数。
        raise ValueError(f"{name} 超出 NUMERIC(38,18) 精度")
    return result


def _timestamp(value):
    result = int(value)
    if isinstance(value, bool) or float(value) != result or result < 0:
        raise ValueError("timestamp 必须是非负整数毫秒")
    return result


def _serializable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


class DataStore:
    """池化连接；每个公开写操作原子提交，失败完整回滚。"""

    def __init__(self, root=None, dsn=None, db_path=None, schema="quant", max_connections=6):
        self.root = Path(root or Path(__file__).resolve().parent)
        try:
            from runtime_config import load_environment
        except ImportError:
            load_environment = None
        if load_environment is not None:
            load_environment(self.root)
        # db_path 仅保留旧调用位置兼容性，内容仍必须是 PostgreSQL 连接串。
        self.dsn = str(dsn or db_path or os.getenv("QUANT_DATABASE_URL", ""))
        self.schema = str(schema)
        self.max_connections = max(1, int(max_connections))
        self._pool = None
        self._init_lock = threading.RLock()
        self._stats_lock = threading.Lock()
        self._stats_cache = None
        self._stats_cache_at = 0.0
        self._last_query_ms = None

    def initialize(self):
        with self._init_lock:
            if self._pool is not None:
                return self
            if not self.dsn:
                raise RuntimeError("请在私有 .env 中设置 QUANT_DATABASE_URL")
            try:
                from psycopg import sql
                from psycopg.rows import dict_row
                from psycopg.types.json import Jsonb
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError("请安装 psycopg[binary,pool] 数据库依赖") from exc
            self._sql, self._Jsonb = sql, Jsonb

            def configure(conn):
                # schema 名作为 SQL 标识符转义，不拼接未经转义的连接配置。
                conn.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(self.schema)))
                conn.execute("SET statement_timeout TO '120s'")
                conn.execute("SET lock_timeout TO '15s'")
                conn.commit()

            pool = ConnectionPool(
                conninfo=self.dsn, min_size=1, max_size=self.max_connections,
                timeout=20, open=False, configure=configure,
                kwargs={"row_factory": dict_row, "connect_timeout": 10},
                name="quant-data",
            )
            try:
                pool.open(wait=True, timeout=20)
                with pool.connection() as conn:
                    with conn.transaction():
                        # 多个服务首次启动时串行执行 DDL，防止系统目录冲突。
                        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"quant-schema:{self.schema}",))
                        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
                        schema_file = Path(__file__).resolve().parent / "database" / "schema.sql"
                        conn.execute(schema_file.read_text(encoding="utf-8"), prepare=False)
                        self._migrate_orderflow_surges(conn)
                self._pool = pool
            except Exception:
                pool.close()
                raise
        return self

    def close(self):
        with self._init_lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None

    @contextmanager
    def _connection(self):
        self.initialize()
        started = time.perf_counter()
        with self._pool.connection() as conn:
            with conn.transaction():
                yield conn
        self._last_query_ms = round((time.perf_counter() - started) * 1000, 3)

    def _json(self, value):
        return self._Jsonb(value, dumps=_json_text)

    def _metadata(self, conn, key, payload):
        conn.execute("""INSERT INTO store_metadata (key,payload) VALUES (%s,%s)
            ON CONFLICT (key) DO UPDATE SET payload=EXCLUDED.payload,updated_at=CURRENT_TIMESTAMP""",
                     (key, self._json(payload)))

    def get_document(self, key, default=None):
        with self._connection() as conn:
            row = conn.execute("SELECT payload FROM documents WHERE key=%s", (key,)).fetchone()
        return row["payload"] if row is not None else default

    def put_document(self, key, payload, source_path=None, source_mtime_ns=None):
        with self._connection() as conn:
            conn.execute("""INSERT INTO documents (key,payload,source_path,source_mtime_ns)
                VALUES (%s,%s,%s,%s) ON CONFLICT (key) DO UPDATE SET
                payload=EXCLUDED.payload,source_path=EXCLUDED.source_path,
                source_mtime_ns=EXCLUDED.source_mtime_ns,updated_at=CURRENT_TIMESTAMP""",
                         (key, self._json(payload), str(source_path) if source_path else None, source_mtime_ns))

    def list_documents(self, prefix="", suffix=None):
        # 以前缀长度比较，避免路径内的 % 和 _ 被误解为通配符。
        conditions, params = ["left(key,%s)=%s"], [len(prefix), prefix]
        if suffix is not None:
            conditions.append("right(key,%s)=%s")
            params.extend((len(suffix), suffix))
        with self._connection() as conn:
            rows = conn.execute("SELECT key,payload,updated_at,source_path,source_mtime_ns FROM documents WHERE "
                                + " AND ".join(conditions) + " ORDER BY key", params).fetchall()
        return _serializable(rows)

    def read_events(self, key, limit=None):
        params = [key]
        query = "SELECT payload FROM events WHERE key=%s ORDER BY ordinal DESC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(0, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row["payload"] for row in reversed(rows)]

    def replace_events(self, key, rows, source_path=None, source_mtime_ns=None):
        with self._connection() as conn:
            conn.execute("""INSERT INTO event_sources (key,source_path,source_mtime_ns) VALUES (%s,%s,%s)
                ON CONFLICT (key) DO UPDATE SET source_path=EXCLUDED.source_path,
                source_mtime_ns=EXCLUDED.source_mtime_ns,updated_at=CURRENT_TIMESTAMP""",
                         (key, str(source_path) if source_path else None, source_mtime_ns))
            conn.execute("DELETE FROM events WHERE key=%s", (key,))
            count = 0
            with conn.cursor().copy("COPY events (key,ordinal,payload) FROM STDIN") as writer:
                for count, row in enumerate(rows, start=1):
                    writer.write_row((key, count - 1, self._json(row)))
        return count

    def _copy_upsert(self, conn, table, columns, keys, rows, updated_column="updated_at"):
        """COPY 批量暂存，每键保留最新行（K 线按版本排序），再统一 UPSERT。"""
        sql = self._sql
        stage_name = "_quant_batch_" + table
        table_id, stage_id = sql.Identifier(table), sql.Identifier(stage_name)
        # 暂存表按池连接复用，提交自动清空，避免持续采集反复写系统目录。
        exists = conn.execute("SELECT to_regclass(%s) AS oid", ("pg_temp." + stage_name,)).fetchone()["oid"]
        if exists is None:
            conn.execute(sql.SQL("CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS) ON COMMIT DELETE ROWS")
                         .format(stage_id, table_id))
            conn.execute(sql.SQL("ALTER TABLE {} ADD COLUMN _ordinal BIGINT").format(stage_id))
        cols = sql.SQL(",").join(map(sql.Identifier, columns))
        key_cols = sql.SQL(",").join(map(sql.Identifier, keys))
        copy_cols = sql.SQL(",").join(map(sql.Identifier, [*columns, "_ordinal"]))
        with conn.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN").format(stage_id, copy_cols)) as writer:
            for ordinal, row in enumerate(rows):
                writer.write_row((*row, ordinal))
        updates = []
        for column in columns:
            if column not in keys:
                if column == "source_key":
                    updates.append(sql.SQL("{}=COALESCE(EXCLUDED.{},{}.{})").format(
                        sql.Identifier(column), sql.Identifier(column), table_id, sql.Identifier(column)))
                else:
                    updates.append(sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column)))
        if updated_column:
            updates.append(sql.SQL("{}=CURRENT_TIMESTAMP").format(sql.Identifier(updated_column)))
        # 已收盘 WS K 线是完整的交易所事实，优先于任意 REST 快照。
        # REST 抓取时刻可能更晚，但其 OHLCV 快照没有订单流字段；覆盖会丢失
        # quote_volume 与 live_event_timestamp，使检测器重启后无法恢复基线。
        # 尚未收盘的更新仍按版本排序，允许 REST 修复断线期间的行情。
        update_guard = sql.SQL("")
        batch_order = sql.SQL("{keys},_ordinal DESC").format(keys=key_cols)
        if table == "candles":
            update_guard = sql.SQL("""WHERE
                (EXCLUDED.extras->>'live_event_timestamp' IS NOT NULL
                    AND COALESCE(EXCLUDED.extras->'closed'='true'::JSONB,false),
                 GREATEST(COALESCE((EXCLUDED.extras->>'live_event_timestamp')::NUMERIC,-1),
                    COALESCE((EXCLUDED.extras->>'live_snapshot_timestamp')::NUMERIC,-1)),
                    COALESCE(EXCLUDED.extras->'closed'='true'::JSONB,false))
                >= (candles.extras->>'live_event_timestamp' IS NOT NULL
                    AND COALESCE(candles.extras->'closed'='true'::JSONB,false),
                    GREATEST(COALESCE((candles.extras->>'live_event_timestamp')::NUMERIC,-1),
                    COALESCE((candles.extras->>'live_snapshot_timestamp')::NUMERIC,-1)),
                    COALESCE(candles.extras->'closed'='true'::JSONB,false))""")
            batch_order = sql.SQL("""{keys},
                (extras->>'live_event_timestamp' IS NOT NULL
                    AND COALESCE(extras->'closed'='true'::JSONB,false)) DESC,
                GREATEST(COALESCE((extras->>'live_event_timestamp')::NUMERIC,-1),
                    COALESCE((extras->>'live_snapshot_timestamp')::NUMERIC,-1)) DESC,
                COALESCE(extras->'closed'='true'::JSONB,false) DESC,_ordinal DESC""").format(keys=key_cols)
        query = sql.SQL("""WITH changed AS (
            INSERT INTO {table} ({cols})
            SELECT {cols} FROM (SELECT DISTINCT ON ({keys}) {cols} FROM {stage}
                ORDER BY {batch_order}) AS latest
            ON CONFLICT ({keys}) DO UPDATE SET {updates} {update_guard}
            RETURNING xmax=0 AS inserted
        ) SELECT COUNT(*) AS written,COUNT(*) FILTER (WHERE inserted) AS inserted FROM changed""").format(
            table=table_id, cols=cols, keys=key_cols, stage=stage_id,
            updates=sql.SQL(",").join(updates), update_guard=update_guard, batch_order=batch_order)
        return conn.execute(query).fetchone()

    def upsert_candles(self, exchange, market, pair, timeframe, candle_type, rows, source_key=None):
        identity = (exchange, market, pair, timeframe, candle_type)
        with self._connection() as conn:
            # 同一数据集写入串行化，摘要计数与行情事务保持一致。
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("candles:" + _json_text(identity),))
            bounds = [None, None]
            def values():
                for row in rows:
                    timestamp = _timestamp(row["timestamp"])
                    bounds[0] = timestamp if bounds[0] is None else min(bounds[0], timestamp)
                    bounds[1] = timestamp if bounds[1] is None else max(bounds[1], timestamp)
                    extras = row.get("extras") or {}
                    if any(name in extras for name in ("live_event_timestamp", "live_snapshot_timestamp")):
                        extras = dict(extras)
                        for name in ("live_event_timestamp", "live_snapshot_timestamp"):
                            if name in extras:
                                extras[name] = _timestamp(extras[name])
                    yield (*identity, timestamp,
                           *(_number(row[name], name) for name in ("open", "high", "low", "close", "volume")),
                           self._json(extras), source_key)
            result = self._copy_upsert(conn, "candles",
                ["exchange", "market", "pair", "timeframe", "candle_type", "timestamp",
                 "open", "high", "low", "close", "volume", "extras", "source_key"],
                ["exchange", "market", "pair", "timeframe", "candle_type", "timestamp"], values())
            if bounds[0] is not None:
                # 时间戳属于主键，不会被 UPDATE 移动；用新增行数与批次边界
                # 增量更新摘要，实时归档不再对整段历史 COUNT/MIN/MAX。
                summary = conn.execute("""UPDATE candle_datasets SET rows=rows+%s,
                    first_timestamp=LEAST(first_timestamp,%s),
                    last_timestamp=GREATEST(last_timestamp,%s),updated_at=CURRENT_TIMESTAMP
                    WHERE exchange=%s AND market=%s AND pair=%s AND timeframe=%s AND candle_type=%s
                    RETURNING rows""", (result["inserted"], *bounds, *identity)).fetchone()
                if summary is None:
                    # 首次创建或摘要缺失时恢复一次完整计数，同一事务锁下安全。
                    conn.execute("""INSERT INTO candle_datasets
                    (exchange,market,pair,timeframe,candle_type,rows,first_timestamp,last_timestamp)
                    SELECT %s,%s,%s,%s,%s,COUNT(*),MIN(timestamp),MAX(timestamp) FROM candles
                    WHERE exchange=%s AND market=%s AND pair=%s AND timeframe=%s AND candle_type=%s
                    ON CONFLICT (exchange,market,pair,timeframe,candle_type) DO UPDATE SET
                    rows=EXCLUDED.rows,first_timestamp=EXCLUDED.first_timestamp,
                    last_timestamp=EXCLUDED.last_timestamp,updated_at=CURRENT_TIMESTAMP""", (*identity, *identity))
        return int(result["written"])

    def _time_query(self, table, fields, conditions, params, limit, start, end, tie="timestamp"):
        if start is not None:
            conditions.append("timestamp >= %s")
            params.append(_timestamp(start))
        if end is not None:
            conditions.append("timestamp <= %s")
            params.append(_timestamp(end))
        query = f"SELECT {fields} FROM {table} WHERE " + " AND ".join(conditions) + f" ORDER BY {tie} DESC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(0, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return _serializable(list(reversed(rows)))

    def get_candles(self, exchange, market, pair, timeframe, candle_type="futures", limit=200, start=None, end=None):
        return self._time_query("candles", "timestamp,open,high,low,close,volume,extras",
            ["exchange=%s", "market=%s", "pair=%s", "timeframe=%s", "candle_type=%s"],
            [exchange, market, pair, timeframe, candle_type], limit, start, end)

    def upsert_series(self, source, metric, asset, rows, source_key=None):
        with self._connection() as conn:
            def values():
                for row in rows:
                    yield (source, metric, asset, _timestamp(row["timestamp"]), _number(row["value"], "value"),
                           self._json(row.get("extras") or {}), source_key)
            result = self._copy_upsert(conn, "series_points",
                ["source", "metric", "asset", "timestamp", "value", "extras", "source_key"],
                ["source", "metric", "asset", "timestamp"], values())
        return int(result["written"])

    def get_series(self, source, metric, asset, limit=200, start=None, end=None):
        return self._time_query("series_points", "timestamp,value,extras",
            ["source=%s", "metric=%s", "asset=%s"], [source, metric, asset], limit, start, end)

    def replace_external(self, source, table, rows):
        return self.replace_external_snapshot(source, {table: rows}, complete=False)

    def replace_external_snapshot(self, source, snapshot, complete=True):
        """同一来源的所有表一次提交，避免交易和订单来自不同快照。"""
        written = 0
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"external:{source}",))
            if complete:
                conn.execute("DELETE FROM external_records WHERE source=%s", (source,))
            for table, rows in snapshot.items():
                if not complete:
                    conn.execute("DELETE FROM external_records WHERE source=%s AND table_name=%s", (source, table))
                def values():
                    for row in rows:
                        if not isinstance(row, dict):
                            raise ValueError("外部记录必须是 dict")
                        key = row.get("_key", row.get("id"))
                        if key is None:
                            key = hashlib.sha256(_json_text(row).encode()).hexdigest()
                        yield (source, table, str(key), self._json(row))
                result = self._copy_upsert(conn, "external_records",
                    ["source", "table_name", "record_key", "payload"],
                    ["source", "table_name", "record_key"], values())
                written += int(result["written"])
        return written

    def read_external(self, source, table, limit=None):
        params = [source, table]
        query = "SELECT payload FROM external_records WHERE source=%s AND table_name=%s ORDER BY record_key"
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(0, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row["payload"] for row in rows]

    def load_users(self):
        with self._connection() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            rows = conn.execute("SELECT username,payload FROM users ORDER BY username").fetchall()
            meta = conn.execute("SELECT payload FROM store_metadata WHERE key='users_metadata'").fetchone()
        if not rows and meta is None:
            return None
        data = dict(meta["payload"] if meta else {})
        data["users"] = {row["username"]: row["payload"] for row in rows}
        return data

    def _save_users(self, conn, data):
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
            raise ValueError("用户数据必须包含 users 字典")
        users = data["users"]
        for name, record in users.items():
            if not name or not isinstance(record, dict) or not isinstance(record.get("salt"), str) or not isinstance(record.get("hash"), str):
                raise ValueError("用户记录必须保留 salt 和 hash 字符串")
        self._metadata(conn, "users_metadata", {key: value for key, value in data.items() if key != "users"})
        conn.execute("DELETE FROM users WHERE NOT (username=ANY(%s))", (list(users),))
        with conn.cursor() as cursor:
            cursor.executemany("""INSERT INTO users (username,payload) VALUES (%s,%s)
                ON CONFLICT (username) DO UPDATE SET payload=EXCLUDED.payload,updated_at=CURRENT_TIMESTAMP""",
                [(name, self._json(record)) for name, record in users.items()])

    def save_users(self, data):
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('quant-users'))")
            self._save_users(conn, data)

    def update_user(self, username, record, expected_hash=None):
        """只更新单个用户，旧哈希比较保证并发改密不会覆盖新密码。"""
        if not isinstance(record.get("salt"), str) or not isinstance(record.get("hash"), str):
            raise ValueError("用户记录必须包含 salt 和 hash")
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('quant-users'))")
            query = "UPDATE users SET payload=%s,updated_at=CURRENT_TIMESTAMP WHERE username=%s"
            params = [self._json(record), username]
            if expected_hash is not None:
                query += " AND payload->>'hash'=%s"
                params.append(expected_hash)
            return conn.execute(query, params).rowcount == 1

    def migrate_users(self, path):
        """首次导入原密码哈希，已迁移或已有数据库用户时绝不覆盖。"""
        path = Path(path)
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('quant-users'))")
            if conn.execute("SELECT 1 FROM store_metadata WHERE key='users_migration'").fetchone():
                return False
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                self._metadata(conn, "users_migration", {"state": "database_exists"})
                return False
            if not path.is_file():
                return False
            data = json.loads(path.read_text(encoding="utf-8"))
            self._save_users(conn, data)
            self._metadata(conn, "users_migration", {"state": "imported", "source_path": str(path)})
        return True

    def save_artifact(self, key, path, size, mtime_ns, kind, metadata=None):
        with self._connection() as conn:
            conn.execute("""INSERT INTO artifacts (key,path,size,mtime_ns,kind,metadata) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (key) DO UPDATE SET path=EXCLUDED.path,size=EXCLUDED.size,
                mtime_ns=EXCLUDED.mtime_ns,kind=EXCLUDED.kind,metadata=EXCLUDED.metadata,updated_at=CURRENT_TIMESTAMP""",
                         (key, str(path), size, mtime_ns, kind, self._json(metadata or {})))
        return 1

    def list_artifacts(self, prefix=""):
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM artifacts WHERE left(key,%s)=%s ORDER BY key", (len(prefix), prefix)).fetchall()
        return _serializable(rows)

    def source_state(self, key):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM sources WHERE key=%s", (key,)).fetchone()
        return _serializable(row)

    def record_source(self, key, path, kind, mtime_ns, size, row_count, error=None):
        with self._connection() as conn:
            conn.execute("""INSERT INTO sources
                (key,path,kind,mtime_ns,size,row_count,error,last_attempt_mtime_ns,last_attempt_size,last_success_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s::text IS NULL THEN CURRENT_TIMESTAMP END)
                ON CONFLICT (key) DO UPDATE SET path=EXCLUDED.path,kind=EXCLUDED.kind,
                mtime_ns=CASE WHEN EXCLUDED.error IS NULL THEN EXCLUDED.mtime_ns ELSE sources.mtime_ns END,
                size=CASE WHEN EXCLUDED.error IS NULL THEN EXCLUDED.size ELSE sources.size END,
                row_count=CASE WHEN EXCLUDED.error IS NULL THEN EXCLUDED.row_count ELSE sources.row_count END,
                error=EXCLUDED.error,last_attempt_mtime_ns=EXCLUDED.last_attempt_mtime_ns,
                last_attempt_size=EXCLUDED.last_attempt_size,
                last_success_at=CASE WHEN EXCLUDED.error IS NULL THEN CURRENT_TIMESTAMP ELSE sources.last_success_at END,
                updated_at=CURRENT_TIMESTAMP""",
                (key, str(path), kind, mtime_ns if error is None else None, size if error is None else None,
                 row_count if error is None else 0, str(error) if error is not None else None,
                 mtime_ns, size, str(error) if error is not None else None))

    def set_sync_state(self, state):
        with self._connection() as conn:
            # JSONB 合并让不同采集进程更新状态时保留已有字段。
            conn.execute("""INSERT INTO store_metadata (key,payload) VALUES ('sync_state',%s)
                ON CONFLICT (key) DO UPDATE SET payload=store_metadata.payload || EXCLUDED.payload,
                updated_at=CURRENT_TIMESTAMP""", (self._json(state),))

    def _update_stream_stats(self, conn, kind, exchange, market, pair, inserted):
        # 利用交易对时间索引取边界；重复成交修正时间后也能收缩旧边界。
        bounds = conn.execute(self._sql.SQL("""SELECT MIN(timestamp) AS first,MAX(timestamp) AS last FROM {}
            WHERE exchange=%s AND market=%s AND pair=%s""").format(self._sql.Identifier(kind)),
            (exchange, market, pair)).fetchone()
        if bounds["first"] is None:
            return
        conn.execute("""INSERT INTO stream_stats (kind,exchange,market,pair,rows,first_timestamp,last_timestamp)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (kind,exchange,market,pair) DO UPDATE SET
            rows=stream_stats.rows+EXCLUDED.rows,
            first_timestamp=EXCLUDED.first_timestamp,
            last_timestamp=EXCLUDED.last_timestamp,updated_at=CURRENT_TIMESTAMP""",
                     (kind, exchange, market, pair, inserted, bounds["first"], bounds["last"]))

    def upsert_ticks(self, exchange, market, pair, rows):
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("ticks:" + _json_text((exchange,market,pair)),))
            def values():
                for row in rows:
                    if not isinstance(row["buyer_maker"], bool):
                        raise ValueError("buyer_maker 必须是布尔值")
                    yield (exchange, market, pair, str(row["trade_id"]), _timestamp(row["timestamp"]),
                           _decimal(row["price"], "price"), _decimal(row["quantity"], "quantity"),
                           row["buyer_maker"], self._json(row.get("extras") or {}))
            result = self._copy_upsert(conn, "ticks",
                ["exchange", "market", "pair", "trade_id", "timestamp", "price", "quantity", "buyer_maker", "extras"],
                ["exchange", "market", "pair", "trade_id"], values(), "received_at")
            self._update_stream_stats(conn, "ticks", exchange, market, pair, result["inserted"])
        return int(result["written"])

    def insert_orderbooks(self, exchange, market, pair, rows):
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("orderbooks:" + _json_text((exchange,market,pair)),))
            def values():
                for row in rows:
                    for side in ("bids", "asks"):
                        if not isinstance(row[side], (list, tuple)):
                            raise ValueError(f"{side} 必须是价量列表")
                        for level in row[side]:
                            if not isinstance(level, (list, tuple)) or len(level) < 2:
                                raise ValueError("盘口档位必须包含价格和数量")
                            _number(level[0], "price")
                            _number(level[1], "quantity")
                    yield (exchange, market, pair, _timestamp(row["timestamp"]), str(row["last_update_id"]),
                           self._json(row["bids"]), self._json(row["asks"]), self._json(row.get("extras") or {}))
            result = self._copy_upsert(conn, "orderbooks",
                ["exchange", "market", "pair", "timestamp", "last_update_id", "bids", "asks", "extras"],
                ["exchange", "market", "pair", "timestamp", "last_update_id"], values(), "received_at")
            self._update_stream_stats(conn, "orderbooks", exchange, market, pair, result["inserted"])
        return int(result["written"])

    def get_ticks(self, exchange, market, pair, limit=100, start=None, end=None):
        return self._time_query("ticks", "trade_id,timestamp,price,quantity,buyer_maker,extras",
            ["exchange=%s", "market=%s", "pair=%s"], [exchange, market, pair], limit, start, end,
            tie="timestamp DESC,length(trade_id) DESC,trade_id")

    def get_orderbooks(self, exchange, market, pair, limit=20, start=None, end=None):
        return self._time_query("orderbooks", "timestamp,last_update_id,bids,asks,extras",
            ["exchange=%s", "market=%s", "pair=%s"], [exchange, market, pair], limit, start, end,
            tie="timestamp DESC,length(last_update_id) DESC,last_update_id")

    @staticmethod
    def _surge_event_id(observation):
        event_id = observation.get("event_id", observation.get("id"))
        if event_id is None or not str(event_id).strip():
            raise ValueError("突增观测必须包含 event_id")
        return str(event_id)

    @staticmethod
    def _surge_required_text(value, name):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"突增事件必须包含 {name}")
        return value.strip()

    def _normalize_surge_event(self, event, source_id=None):
        """Freeze the detector or compatibility shape into one revision fact."""
        if not isinstance(event, dict):
            raise ValueError("突增事件必须是 dict")
        payload = copy.deepcopy(event)
        original_id = self._surge_event_id(payload)
        event_id = str(payload.get("observation_id", original_id))
        observation = payload.get("observation")
        if not isinstance(observation, dict):
            raise ValueError("突增事件必须包含 observation 字典")
        event_type = self._surge_required_text(payload.get("event_type"), "event_type")
        kind = payload.get("revision_kind", payload.get("revision"))
        if kind is None:
            kind = "maturity" if event_type in {"matured", "maturity", "orderflow_volume_surge_matured"} else "observed"
        if kind == "matured":
            kind = "maturity"
        if kind not in {"observed", "maturity"}:
            raise ValueError("revision_kind 必须是 observed 或 maturity")
        status = payload.get("status", "matured" if kind == "maturity" else "unknown")
        if status not in {"unknown", "matured", "invalid"}:
            raise ValueError("突增事件 status 必须是 unknown、matured 或 invalid")
        if kind == "observed" and (status != "unknown" or payload.get("result") is not None):
            raise ValueError("原始突增观测必须保持 unknown 且不包含成熟结果")
        if kind == "maturity" and (status == "unknown" or not isinstance(payload.get("result"), dict)):
            raise ValueError("成熟 revision 必须包含 result 字典与已知 status")
        timestamp = _timestamp(payload.get("timestamp", observation.get("timestamp")))
        if "timestamp" in observation and _timestamp(observation["timestamp"]) != timestamp:
            raise ValueError("事件 timestamp 与 observation 不一致")
        method_version = self._surge_required_text(payload.get("method_version"), "method_version")
        fingerprint = self._surge_required_text(payload.get("data_fingerprint"), "data_fingerprint")
        market = self._surge_required_text(payload.get("market", "futures"), "market")
        pair = self._surge_required_text(payload.get("pair", observation.get("pair")), "pair").upper()
        timeframe = self._surge_required_text(payload.get("timeframe", "5m"), "timeframe")
        observed_at = payload.get("observed_at")
        if observed_at is not None:
            observed_at = _timestamp(observed_at)
        revision_id = payload.pop("revision_id", None)
        payload.pop("revision_no", None)
        payload.pop("revision", None)
        payload.pop("observation_id", None)
        payload.update(event_id=event_id, event_type=event_type, revision_kind=kind,
                       method_version=method_version, data_fingerprint=fingerprint,
                       market=market, pair=pair, timeframe=timeframe,
                       timestamp=timestamp, observed_at=observed_at, status=status,
                       observation=observation, result=payload.get("result"))
        # Serialize before taking a connection: mutable or non-finite input
        # cannot leave a partially written fact behind.
        _json_text(payload)
        if revision_id is None:
            if kind == "observed":
                revision_id = event_id + ":observed"
            elif original_id != event_id:
                revision_id = original_id
            else:
                revision_id = event_id + ":maturity:" + hashlib.sha256(
                    _json_text(payload).encode("utf-8")).hexdigest()
        revision_id = self._surge_required_text(revision_id, "revision_id")
        return {"event_id": event_id, "revision_id": revision_id,
                "revision_kind": kind, "event_type": event_type,
                "method_version": method_version, "data_fingerprint": fingerprint,
                "market": market, "pair": pair, "timeframe": timeframe,
                "timestamp": timestamp, "observed_at": observed_at, "status": status,
                "observation": observation, "result": payload.get("result"),
                "payload": payload, "source_id": source_id or event.get("source_id")}

    def _append_surge_on_connection(self, conn, values):
        """Return (revision_id, inserted) under one per-observation lock."""
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("orderflow-surge:" + values["event_id"],))
        same_revision = conn.execute("SELECT event_id,payload FROM orderflow_surge_events WHERE revision_id=%s",
                                     (values["revision_id"],)).fetchone()
        if same_revision is not None:
            if same_revision["event_id"] != values["event_id"] or _json_text(same_revision["payload"]) != _json_text(values["payload"]):
                raise ValueError("相同 revision_id 的突增事件内容冲突")
            return values["revision_id"], False
        original = conn.execute("SELECT * FROM orderflow_surge_events WHERE event_id=%s AND revision_kind='observed'",
                                (values["event_id"],)).fetchone()
        if values["revision_kind"] == "observed" and original is not None:
            if _json_text(original["payload"]) != _json_text(values["payload"]):
                raise ValueError("相同 event_id 的原始突增观测内容冲突")
            return original["revision_id"], False
        if values["revision_kind"] == "maturity":
            if original is None:
                raise ValueError("成熟结果必须引用已保存的突增观测")
            for key in ("method_version", "data_fingerprint", "market", "pair", "timeframe", "timestamp", "observed_at"):
                if values[key] != original[key]:
                    raise ValueError(f"成熟结果不能修改原始观测 {key}")
            if _json_text(values["observation"]) != _json_text(original["observation"]):
                raise ValueError("成熟结果不能修改原始 observation")
        latest = conn.execute("SELECT COALESCE(MAX(revision_no),0) AS n FROM orderflow_surge_events WHERE event_id=%s",
                              (values["event_id"],)).fetchone()["n"]
        columns = ("event_id", "revision_id", "revision_no", "revision_kind", "event_type",
                   "method_version", "data_fingerprint", "market", "pair", "timeframe", "timestamp",
                   "observed_at", "status", "observation", "result", "payload", "source_id")
        values = {**values, "revision_no": int(latest) + 1}
        params = [self._json(values[key]) if key in {"observation", "result", "payload"} and values[key] is not None
                  else values[key] for key in columns]
        # The column and parameter counts share one source to prevent drift.
        conn.execute("INSERT INTO orderflow_surge_events (" + ",".join(columns) + ") VALUES ("
                     + ",".join("%s" for _ in columns) + ")", params)
        return values["revision_id"], True

    def append_orderflow_surge(self, event, source_id=None):
        """Append an immutable observation/revision; exact retries return its ID."""
        values = self._normalize_surge_event(event, source_id)
        with self._connection() as conn:
            revision_id, _inserted = self._append_surge_on_connection(conn, values)
        return revision_id

    def _surge_result_event(self, conn, event_id, result, source_id=None, revision_id=None,
                            method_version=None, data_fingerprint=None, **metadata):
        if not isinstance(result, dict):
            raise ValueError("成熟结果必须是 dict")
        original = conn.execute("SELECT * FROM orderflow_surge_events WHERE event_id=%s AND revision_kind='observed'",
                                (str(event_id),)).fetchone()
        if original is None:
            raise ValueError("成熟结果必须引用已保存的突增观测")
        event = {key: copy.deepcopy(original[key]) for key in
                 ("event_id", "method_version", "data_fingerprint", "market", "pair", "timeframe",
                  "timestamp", "observed_at", "observation")}
        event.update(event_type="orderflow_volume_surge_matured", revision_kind="maturity",
                     status=result.get("status", "matured"), result=copy.deepcopy(result))
        if method_version is not None:
            event["method_version"] = method_version
        if data_fingerprint is not None:
            event["data_fingerprint"] = data_fingerprint
        event.update(copy.deepcopy(metadata))
        if revision_id is not None:
            event["revision_id"] = revision_id
        return self._normalize_surge_event(event, source_id)

    def append_orderflow_surge_result(self, event_id, result, *, source_id=None, revision_id=None,
                                      method_version=None, data_fingerprint=None, **metadata):
        """Append maturity without overwriting the original unknown observation."""
        with self._connection() as conn:
            values = self._surge_result_event(conn, event_id, result, source_id, revision_id,
                                             method_version, data_fingerprint, **metadata)
            stored_id, _inserted = self._append_surge_on_connection(conn, values)
        return stored_id

    def read_orderflow_surges(self, market="futures", pair=None, timeframe="5m", status=None,
                              limit=100, include_revisions=False, start=None, end=None, event_id=None):
        """Project the latest fact before status filtering and final limiting."""
        conditions, params = [], []
        for key, value in (("market", market), ("pair", pair), ("timeframe", timeframe), ("event_id", event_id)):
            if value is not None:
                conditions.append(key + "=%s")
                params.append(str(value).upper() if key == "pair" else str(value))
        if start is not None:
            conditions.append("timestamp >= %s")
            params.append(_timestamp(start))
        if end is not None:
            conditions.append("timestamp <= %s")
            params.append(_timestamp(end))
        if start is not None and end is not None and _timestamp(start) > _timestamp(end):
            raise ValueError("start 不能晚于 end")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        if include_revisions:
            query = "SELECT * FROM orderflow_surge_events" + where
            status_prefix = " AND " if conditions else " WHERE "
        else:
            # Latest revision facts and observation completion are separate:
            # a single 5m result is mature, while the observation stays unknown
            # until the full default set is available.  This SQL projection
            # must run before filtering status or applying the caller's limit.
            query = ("""WITH scoped AS (SELECT * FROM orderflow_surge_events""" + where + """),
                latest AS (SELECT DISTINCT ON (event_id) * FROM scoped
                    ORDER BY event_id,revision_no DESC),
                latest_horizons AS (SELECT DISTINCT ON (event_id,result->>'horizon')
                    event_id,result->>'horizon' AS horizon,status,result
                    FROM scoped WHERE revision_kind='maturity' AND result->>'horizon' IS NOT NULL
                    ORDER BY event_id,result->>'horizon',revision_no DESC),
                completion AS (SELECT event_id,
                    array_agg(horizon ORDER BY CASE horizon WHEN '5m' THEN 1 WHEN '30m' THEN 2
                        WHEN '1h' THEN 3 WHEN '4h' THEN 4 ELSE 5 END)
                    FILTER (WHERE status='matured' AND COALESCE(result->>'status','matured')='matured')
                        AS completed_horizons,
                    COUNT(*) FILTER (WHERE horizon IN ('5m','30m','1h','4h') AND status='matured'
                        AND COALESCE(result->>'status','matured')='matured') AS completed_count
                    FROM latest_horizons GROUP BY event_id)
                SELECT latest.*,
                    CASE WHEN COALESCE(completion.completed_count,0)=4 THEN 'matured'
                         WHEN latest.status='invalid' THEN 'invalid' ELSE 'unknown' END AS projected_status,
                    COALESCE(completion.completed_horizons,ARRAY[]::TEXT[]) AS completed_horizons,
                    ARRAY['5m','30m','1h','4h']::TEXT[] AS required_horizons
                    FROM latest LEFT JOIN completion USING (event_id)""")
            query = "SELECT * FROM (" + query + ") AS projected"
            status_prefix = " WHERE "
        if status is not None:
            if status not in {"unknown", "matured", "invalid"}:
                raise ValueError("突增事件 status 必须是 unknown、matured 或 invalid")
            query += status_prefix + ("status=%s" if include_revisions else "projected_status=%s")
            params.append(status)
        query += " ORDER BY timestamp DESC,event_id,revision_no DESC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(0, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            # Flat fields keep older importers useful; row metadata and nested
            # immutable facts take precedence over similarly named payload keys.
            projected = copy.deepcopy(row.get("observation") or {})
            projected.update(copy.deepcopy(row.get("payload") or {}))
            projected.update(row)
            if not include_revisions:
                projected["revision_status"] = projected["status"]
                projected["status"] = projected.pop("projected_status")
            result.append(projected)
        return _serializable(result)

    def _surge_observation_values(self, observation):
        """Validate and flatten one immutable online surge observation.

        The complete input is retained in ``payload``.  Typed columns are kept
        beside it so API filters and aggregate queries do not need JSON casts.
        """
        if not isinstance(observation, dict):
            raise ValueError("突增观测必须是 dict")
        event_id = self._surge_event_id(observation)
        pair = observation.get("pair")
        if not isinstance(pair, str) or not pair.strip():
            raise ValueError("突增观测必须包含 pair")
        if "timestamp" not in observation:
            raise ValueError("突增观测必须包含 timestamp")
        if "quote_volume" not in observation:
            raise ValueError("突增观测必须包含 quote_volume")
        if "volume_ratio" not in observation and "ratio" not in observation:
            raise ValueError("突增观测必须包含 volume_ratio")
        method_version = observation.get("method_version")
        if not isinstance(method_version, str) or not method_version.strip():
            raise ValueError("突增观测必须包含 method_version")

        quote_volume = _decimal(observation["quote_volume"], "quote_volume")
        if quote_volume < 0:
            raise ValueError("quote_volume 必须是非负数")
        baseline_raw = observation.get("baseline_quote_volume",
                                       observation.get("baseline"))
        baseline = None if baseline_raw is None else _decimal(baseline_raw, "baseline_quote_volume")
        if baseline is not None and baseline < 0:
            raise ValueError("baseline_quote_volume 必须是非负数")
        ratio = _number(observation.get("volume_ratio", observation.get("ratio")), "volume_ratio")
        if ratio < 0:
            raise ValueError("volume_ratio 必须是非负数")

        num_trades = observation.get("num_trades")
        if num_trades is not None:
            if isinstance(num_trades, bool):
                raise ValueError("num_trades 必须是非负整数")
            try:
                parsed_trades = int(num_trades)
            except (TypeError, ValueError) as exc:
                raise ValueError("num_trades 必须是非负整数") from exc
            if parsed_trades < 0 or float(num_trades) != parsed_trades:
                raise ValueError("num_trades 必须是非负整数")
            num_trades = parsed_trades
        flow_imbal = observation.get("flow_imbal")
        if flow_imbal is not None:
            flow_imbal = _number(flow_imbal, "flow_imbal")
        threshold_multiple = observation.get("threshold_multiple",
                                             observation.get("threshold_x"))
        if threshold_multiple is not None:
            threshold_multiple = _number(threshold_multiple, "threshold_multiple")
            if threshold_multiple < 0:
                raise ValueError("threshold_multiple 必须是非负数")
        threshold_level = observation.get("threshold_level",
                                         observation.get("threshold"))
        if threshold_level is not None:
            threshold_level = str(threshold_level)
        snapshot = observation.get("orderbook_snapshot",
                                   observation.get("orderbook", {}))
        if snapshot is None:
            snapshot = {}
        extras = observation.get("extras") or {}
        if not isinstance(extras, dict):
            raise ValueError("突增观测 extras 必须是 dict")
        payload = copy.deepcopy(observation)
        return {
            "event_id": event_id,
            "exchange": str(observation.get("exchange", "binance")),
            "market": str(observation.get("market", "futures")),
            "pair": pair.strip().upper(),
            "timeframe": str(observation.get("timeframe", "5m")),
            "timestamp": _timestamp(observation["timestamp"]),
            "quote_volume": quote_volume,
            "baseline_quote_volume": baseline,
            "volume_ratio": ratio,
            "threshold_level": threshold_level,
            "threshold_multiple": threshold_multiple,
            "num_trades": num_trades,
            "flow_imbal": flow_imbal,
            "orderbook_snapshot": snapshot,
            "method_version": method_version,
            "data_fingerprint": observation.get("data_fingerprint"),
            "status": str(observation.get("status", "unknown")),
            "extras": extras,
            "payload": payload,
        }

    def insert_surge_observation(self, observation):
        """Append a flat observation through the revision event store.

        This compatibility method predates the revision schema.  Keeping its
        validation and boolean return contract is useful to importers, while
        storing the fact in the same immutable table as live detector events.
        """
        event = self._surge_observation_event(observation)
        values = self._normalize_surge_event(event)
        with self._connection() as conn:
            _revision_id, inserted = self._append_surge_on_connection(conn, values)
        return inserted

    def _surge_observation_event(self, observation):
        if isinstance(observation, dict) and isinstance(observation.get("observation"), dict):
            return copy.deepcopy(observation)
        values = self._surge_observation_values(observation)
        event = copy.deepcopy(values["payload"])
        event.update(event_id=values["event_id"],
                     event_type=event.get("event_type", "orderflow_volume_surge_observed"),
                     method_version=values["method_version"],
                     data_fingerprint=values["data_fingerprint"] or hashlib.sha256(
                         _json_text(values["payload"]).encode("utf-8")).hexdigest(),
                     market=values["market"], pair=values["pair"], timeframe=values["timeframe"],
                     timestamp=values["timestamp"], status="unknown", observation=values["payload"])
        return event

    def _migrate_orderflow_surges(self, conn):
        """Import old facts once while retaining every legacy table unchanged."""
        if conn.execute("SELECT 1 FROM store_metadata WHERE key='orderflow_surge_migration_v3'").fetchone():
            return
        imported, skipped = 0, []
        for table in ("orderflow_surge_observations", "orderflow_surge_events_legacy_v3"):
            if conn.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()["name"] is None:
                continue
            rows = conn.execute(self._sql.SQL("SELECT to_jsonb(legacy) AS row FROM {} AS legacy")
                                .format(self._sql.Identifier(table))).fetchall()
            facts = [row["row"] for row in rows]
            facts.sort(key=lambda row: (str(row.get("created_at", "")), int(row.get("sequence_id") or 0),
                                        str(row.get("event_id", ""))))
            # Observations must be imported before maturity even when legacy
            # timestamps are equal or were received out of order.
            facts.sort(key=lambda row: row.get("event_type", "") in
                       {"matured", "maturity", "orderflow_volume_surge_matured"})
            for row in facts:
                fact_id = str(row.get("event_id", ""))
                try:
                    legacy_payload = row.get("payload") or row
                    if not isinstance(legacy_payload, dict):
                        raise ValueError("legacy payload is not an object")
                    # Keep the legacy payload as the immutable observation.
                    # Row columns are only fallback metadata for validation;
                    # putting them into a flat payload changes its historical
                    # meaning (and used to leak event_type into observation).
                    payload = copy.deepcopy(legacy_payload)
                    event_type = row.get("event_type", payload.get("event_type", "orderflow_volume_surge_observed"))
                    observation_id = row.get("observation_id", payload.get("observation_id"))
                    if event_type in {"matured", "maturity", "orderflow_volume_surge_matured"}:
                        if observation_id is None:
                            observation_id = payload.get("event_id")
                        result = payload.get("result", payload)
                        values = self._surge_result_event(conn, observation_id, result,
                                                         row.get("source_id"), fact_id or None,
                                                         event_type=event_type)
                    else:
                        validation_payload = copy.deepcopy(payload)
                        validation_payload.setdefault("event_id", fact_id)
                        for key in ("method_version", "data_fingerprint", "market", "pair", "timeframe", "timestamp"):
                            if key in row and row[key] is not None:
                                validation_payload.setdefault(key, row[key])
                        if not validation_payload.get("data_fingerprint"):
                            # A flat legacy payload has no nested observation
                            # for _surge_observation_event() to hash.  Derive
                            # its fallback fingerprint before adding row
                            # metadata to the validation envelope.
                            validation_payload["data_fingerprint"] = hashlib.sha256(
                                _json_text(payload).encode("utf-8")).hexdigest()
                        event = self._surge_observation_event(validation_payload)
                        event["event_type"] = event_type
                        if "observation" not in payload:
                            event["observation"] = copy.deepcopy(payload)
                        if not event.get("data_fingerprint"):
                            event["data_fingerprint"] = hashlib.sha256(
                                _json_text(event["observation"]).encode("utf-8")).hexdigest()
                        values = self._normalize_surge_event(event, row.get("source_id"))
                    # A bad legacy record must not roll back valid evidence;
                    # its original bytes remain in the retained backup table.
                    with conn.transaction():
                        _revision_id, inserted = self._append_surge_on_connection(conn, values)
                    imported += int(inserted)
                except (ValueError, TypeError, KeyError) as exc:
                    skipped.append({"table": table, "event_id": fact_id, "reason": str(exc)})
        self._metadata(conn, "orderflow_surge_migration_v3", {"imported": imported, "skipped": skipped,
                                                             "legacy_tables_retained": True})

    def append_surge_event(self, event):
        """Append a maturity/revision event through the canonical event store."""
        if not isinstance(event, dict):
            raise ValueError("突增事件必须是 dict")
        event_id = event.get("event_id", event.get("id"))
        event_type = event.get("event_type", event.get("type"))
        if event_id is None or not str(event_id).strip() or not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("突增事件必须包含 event_id 和 event_type")
        observation_id = event.get("observation_id", event.get("surge_id"))
        payload = copy.deepcopy(event.get("payload", event))
        if not isinstance(payload, dict):
            raise ValueError("突增事件 payload 必须是 dict")
        # Detector events already satisfy the canonical contract.  For the
        # older {observation_id, payload} shape, use the stored observation as
        # the base and append a maturity result when one is supplied.
        if observation_id is not None and "observation" not in payload:
            if event_type in {"matured", "orderflow_volume_surge_matured", "maturity"}:
                result = payload.get("result", payload)
                with self._connection() as conn:
                    values = self._surge_result_event(conn, str(observation_id), result,
                                                     event.get("source_id"), str(event_id),
                                                     event_type=event_type)
                    _revision_id, inserted = self._append_surge_on_connection(conn, values)
                return inserted
            payload = dict(payload)
            payload["event_id"] = str(observation_id)
        if "observation" not in payload:
            payload["observation"] = dict(payload)
        payload.setdefault("event_id", str(event_id))
        payload.setdefault("event_type", event_type)
        payload.setdefault("market", "futures")
        payload.setdefault("pair", payload["observation"].get("pair", ""))
        payload.setdefault("timeframe", "5m")
        payload.setdefault("timestamp", payload["observation"].get("timestamp"))
        payload.setdefault("method_version", "orderflow-volume-surge-v1")
        payload.setdefault("data_fingerprint", hashlib.sha256(
            _json_text(payload["observation"]).encode("utf-8")).hexdigest())
        payload.setdefault("status", "matured" if event_type in {
            "matured", "orderflow_volume_surge_matured", "maturity"
        } else "unknown")
        if payload["status"] == "matured":
            payload.setdefault("revision", "maturity")
            payload.setdefault("revision_kind", "maturity")
        values = self._normalize_surge_event(payload, event.get("source_id"))
        with self._connection() as conn:
            _revision_id, inserted = self._append_surge_on_connection(conn, values)
        return inserted

    def list_surge_observations(self, pair=None, timeframe=None, status=None,
                                limit=100, start=None, end=None):
        return self.read_orderflow_surges(
            market="futures", pair=pair, timeframe=timeframe or "5m",
            status=status, limit=limit, include_revisions=False, start=start, end=end)

    def get_surge_observation(self, event_id):
        revisions = self.read_orderflow_surges(limit=None, include_revisions=True,
                                               event_id=str(event_id))
        if not revisions:
            return None
        revisions.sort(key=lambda row: (row.get("revision_no", 0), row.get("created_at") or ""))
        # Keep the immutable observed fact as the detail's identity while
        # applying the same four-horizon overall projection as list reads.
        original = next((row for row in revisions if row.get("revision_kind") == "observed"), None)
        if original is None:
            original = revisions[0]
        observation = copy.deepcopy(original)
        required = ["5m", "30m", "1h", "4h"]
        latest_horizons = {}
        for revision in revisions:
            result = revision.get("result")
            if revision.get("revision_kind") != "maturity" or not isinstance(result, dict):
                continue
            horizon = result.get("horizon")
            if horizon in required:
                latest_horizons[horizon] = revision
        completed = [horizon for horizon in required
                     if latest_horizons.get(horizon, {}).get("status") == "matured"
                     and (latest_horizons.get(horizon, {}).get("result") or {}).get("status", "matured") == "matured"]
        observation["completed_horizons"] = completed
        observation["required_horizons"] = required
        latest_status = str(revisions[-1].get("status") or "unknown")
        observation["status"] = (
            "matured" if len(completed) == len(required)
            else "invalid" if latest_status == "invalid"
            else "unknown"
        )
        result = copy.deepcopy(observation)
        result["revisions"] = revisions
        return result

    def list_surge_events(self, observation_id=None, event_type=None, limit=100):
        conditions = []
        params = []
        if observation_id is not None:
            conditions.append("event_id=%s")
            params.append(str(observation_id))
        if event_type is not None:
            conditions.append("event_type=%s")
            params.append(str(event_type))
        query = "SELECT event_id,revision_id,revision_no,revision_kind,event_type,payload,created_at FROM orderflow_surge_events"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC,revision_no DESC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(0, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return _serializable(rows)

    def surge_stats(self, pair=None, timeframe=None, horizon="1h"):
        """Counts use latest observations; returns use one result per horizon.

        The pair summary names its selected horizon explicitly.  All other
        horizons remain available separately, and costs stay unknown unless
        an execution/fee evidence source is introduced.
        """
        rows = self.read_orderflow_surges(market="futures", pair=pair,
                                          timeframe=timeframe or "5m", limit=None)
        def counts():
            return {"total": 0, "unknown": 0, "matured": 0, "invalid": 0, "known": 0}
        result = counts()
        result["total"] = len(rows)
        pair_names = [pair] if pair is not None else ["BTC/USDT:USDT", "ETH/USDT:USDT"]
        by_pair = {name: {"pair": name, **counts()} for name in pair_names}
        for row in rows:
            status = row.get("status", "unknown")
            result[status] = result.get(status, 0) + 1
            if status == "matured":
                result["known"] += 1
            pair_key = row.get("pair")
            entry = by_pair.setdefault(pair_key, {"pair": pair_key, **counts()})
            entry["total"] += 1
            entry[status] = entry.get(status, 0) + 1
            if status == "matured":
                entry["known"] += 1
        revisions = self.read_orderflow_surges(market="futures", pair=pair,
                                               timeframe=timeframe or "5m", limit=None,
                                               include_revisions=True)
        latest_results = {}
        for revision in revisions:
            mature_result = revision.get("result")
            if revision.get("revision_kind") != "maturity" or not isinstance(mature_result, dict):
                continue
            horizon_key = mature_result.get("horizon")
            if not isinstance(horizon_key, str) or not horizon_key:
                continue
            key = (revision["event_id"], horizon_key)
            previous = latest_results.get(key)
            if previous is None or revision["revision_no"] > previous["revision_no"]:
                latest_results[key] = revision
        samples = {}
        for (_event_id, horizon_key), revision in latest_results.items():
            mature_result = revision["result"]
            if revision.get("status") != "matured" or mature_result.get("status", "matured") != "matured":
                continue
            pair_key = revision["pair"]
            for bucket in ((None, horizon_key), (pair_key, horizon_key)):
                values = samples.setdefault(bucket, {"return": [], "mfe": [], "mae": []})
                for metric in values:
                    value = mature_result.get(metric)
                    if value is not None:
                        try:
                            values[metric].append(_number(value, metric))
                        except (ValueError, TypeError, OverflowError):
                            continue
        def metric_summary(values=None):
            values = values or {"return": [], "mfe": [], "mae": []}
            returns = values["return"]
            return {"sample_count": len(returns),
                    "gross_return_mean": sum(returns) / len(returns) if returns else None,
                    "win_rate": sum(value > 0 for value in returns) / len(returns) if returns else None,
                    "mfe_mean": sum(values["mfe"]) / len(values["mfe"]) if values["mfe"] else None,
                    "mae_mean": sum(values["mae"]) / len(values["mae"]) if values["mae"] else None,
                    "required_fee_rate": None, "actual_fee_rate": None}
        horizons = list(dict.fromkeys(["5m", "30m", "1h", "4h", *sorted({key[1] for key in samples})]))
        result["horizons"] = {key: metric_summary(samples.get((None, key))) for key in horizons}
        result.update(metric_summary(samples.get((None, str(horizon)))))
        result["statistics_horizon"] = str(horizon)
        result["unknown_excluded_from_performance_statistics"] = True
        result["invalid_excluded_from_performance_statistics"] = True
        for pair_key, entry in by_pair.items():
            entry["horizons"] = {key: metric_summary(samples.get((pair_key, key))) for key in horizons}
            entry.update(metric_summary(samples.get((pair_key, str(horizon)))))
            entry["statistics_horizon"] = str(horizon)
        result["by_pair"] = list(by_pair.values())
        return result

    get_surge_stats = surge_stats

    @staticmethod
    def _price_candidate_payload(event):
        if not isinstance(event, dict) or not isinstance(event.get("observation"), dict):
            raise ValueError("价格爆发候选必须包含 observation 字典")
        observation = copy.deepcopy(event["observation"])
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("价格爆发候选必须包含 event_id")
        method_version = str(event.get("method_version") or "").strip()
        fingerprint = str(event.get("data_fingerprint") or "").strip()
        if not method_version or not fingerprint:
            raise ValueError("价格爆发候选必须包含方法版本和数据指纹")
        if event.get("event_type") != "price_spike_candidate_observed" or event.get("status") != "unknown":
            raise ValueError("价格爆发候选必须是 unknown 的原始观测")
        market = str(event.get("market") or "futures").strip()
        exchange = str(event.get("exchange") or "binance").strip()
        pair = str(event.get("pair") or "").strip().upper()
        timeframe = str(event.get("timeframe") or "5m").strip()
        timestamp = _timestamp(event.get("timestamp", observation.get("timestamp")))
        if not pair or timeframe != "5m" or market != "futures":
            raise ValueError("在线价格爆发候选只接受 futures 5m")
        if _timestamp(observation.get("timestamp")) != timestamp:
            raise ValueError("候选 timestamp 与 observation 不一致")
        ratio = _number(observation.get("volume_ratio"), "volume_ratio")
        quote_volume = _number(observation.get("quote_volume"), "quote_volume")
        baseline = _number(observation.get("baseline_quote_volume"), "baseline_quote_volume")
        expected_band = (
            "lt1" if ratio < 1 else "1to2" if ratio < 2 else
            "2to4" if ratio < 4 else "4to8" if ratio < 8 else
            "8to20" if ratio < 20 else "ge20"
        )
        if quote_volume < 0 or baseline <= 0 or ratio < 0 or not math.isclose(
            ratio, quote_volume / baseline, rel_tol=1e-10, abs_tol=1e-12,
        ) or observation.get("ratio_band") != expected_band:
            raise ValueError("价格爆发候选成交额、基线或量比无效")
        expected_fingerprint = hashlib.sha256(
            _json_text(observation).encode("utf-8")
        ).hexdigest()
        if fingerprint != expected_fingerprint:
            raise ValueError("价格爆发候选数据指纹与观测内容不匹配")
        close = _number(observation.get("close"), "close")
        if close <= 0:
            raise ValueError("价格爆发候选 close 必须为正数")
        observed_at = event.get("observed_at")
        if observed_at is not None:
            observed_at = _timestamp(observed_at)
        payload = {"event_id": event_id, "method_version": method_version,
                   "data_fingerprint": fingerprint, "exchange": exchange,
                   "market": market, "pair": pair, "timeframe": timeframe,
                   "timestamp": timestamp, "observed_at": observed_at,
                   "observation": observation}
        _json_text(payload)
        return payload

    def append_price_candidate(self, event, source_id=None):
        """Append one immutable all-bars trial; exact spool retries are idempotent."""
        value = self._price_candidate_payload(event)
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         ("orderflow-price:" + value["event_id"],))
            existing = conn.execute(
                "SELECT * FROM orderflow_price_candidates WHERE event_id=%s",
                (value["event_id"],),
            ).fetchone()
            if existing is not None:
                stored = {key: existing[key] for key in value}
                if _json_text(stored) != _json_text(value):
                    raise ValueError("相同价格爆发候选身份的内容冲突")
                return value["event_id"]
            conn.execute("""INSERT INTO orderflow_price_candidates
                (event_id,method_version,data_fingerprint,exchange,market,pair,timeframe,
                 timestamp,observed_at,observation,source_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (value["event_id"], value["method_version"], value["data_fingerprint"],
                 value["exchange"], value["market"], value["pair"], value["timeframe"],
                 value["timestamp"], value["observed_at"], self._json(value["observation"]),
                 source_id or event.get("source_id")))
        return value["event_id"]

    def append_price_candidate_result(self, event_id, result_kind, result, *,
                                      method_version=None, data_fingerprint=None):
        """Append the delayed outcome or +/-12-bar profile without changing the candidate."""
        event_id = str(event_id)
        if result_kind not in {"outcome", "profile"} or not isinstance(result, dict):
            raise ValueError("价格爆发结果类型或内容无效")
        status = result.get("status")
        if status not in {"matured", "invalid"}:
            raise ValueError("价格爆发结果 status 必须是 matured 或 invalid")
        if result_kind == "outcome" and status == "matured" and result.get("direction") not in {"up", "down", "none"}:
            raise ValueError("已成熟价格结果必须包含 up、down 或 none 方向")
        if result_kind == "profile" and status == "matured":
            points = result.get("points")
            offsets = [point.get("offset") for point in points if isinstance(point, dict)] if isinstance(points, list) else []
            if offsets != list(range(-12, 13)):
                raise ValueError("已成熟价格剖面必须完整包含 t-12 至 t+12")
        _json_text(result)
        method_version = str(method_version or result.get("method_version") or "").strip()
        fingerprint = str(data_fingerprint or result.get("data_fingerprint") or "").strip()
        if not method_version or not fingerprint:
            raise ValueError("价格爆发结果必须包含方法版本和数据指纹")
        result_hash = hashlib.sha256(_json_text(result).encode("utf-8")).hexdigest()
        fact_id = f"{event_id}:{result_kind}:{result_hash}"
        with self._connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                         ("orderflow-price:" + event_id,))
            candidate = conn.execute(
                "SELECT method_version,data_fingerprint FROM orderflow_price_candidates WHERE event_id=%s",
                (event_id,),
            ).fetchone()
            if candidate is None:
                raise ValueError("价格爆发结果必须引用已保存的候选观测")
            if method_version != candidate["method_version"]:
                raise ValueError("价格爆发结果方法版本不能改变候选口径")
            if result_kind == "profile":
                outcome = conn.execute("""SELECT result FROM orderflow_price_candidate_results
                    WHERE event_id=%s AND result_kind='outcome' AND status='matured'""",
                    (event_id,)).fetchone()
                if outcome is None or outcome["result"].get("direction") not in {"up", "down"}:
                    raise ValueError("价格剖面必须引用已成熟的涨跌爆发")
            existing = conn.execute("""SELECT fact_id,method_version,data_fingerprint,status,result
                FROM orderflow_price_candidate_results WHERE event_id=%s AND result_kind=%s""",
                (event_id, result_kind)).fetchone()
            if existing is not None:
                expected = (fact_id, method_version, fingerprint, status, result)
                actual = (existing["fact_id"], existing["method_version"],
                          existing["data_fingerprint"], existing["status"], existing["result"])
                if _json_text(actual) != _json_text(expected):
                    raise ValueError("相同价格爆发结果类型的内容冲突")
                return fact_id
            conn.execute("""INSERT INTO orderflow_price_candidate_results
                (event_id,result_kind,fact_id,method_version,data_fingerprint,status,result)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (event_id, result_kind, fact_id, method_version, fingerprint,
                 status, self._json(result)))
        return fact_id

    def read_price_candidates(self, *, pair=None, direction=None, status=None,
                              limit=100, start=None, end=None):
        conditions, params = ["c.method_version=%s"], [PRICE_SPIKE_METHOD_VERSION]
        if pair is not None:
            conditions.append("c.pair=%s")
            params.append(str(pair).upper())
        if start is not None:
            conditions.append("c.timestamp >= %s")
            params.append(_timestamp(start))
        if end is not None:
            conditions.append("c.timestamp <= %s")
            params.append(_timestamp(end))
        if status is not None:
            conditions.append("COALESCE(o.status,'unknown')=%s")
            params.append(status)
        if direction == "any":
            conditions.append("(o.status IS NULL OR o.status='invalid' OR "
                              "o.result->>'direction' IN ('up','down'))")
        elif direction is not None:
            conditions.append("o.result->>'direction'=%s")
            params.append(direction)
        where = " WHERE " + " AND ".join(conditions)
        query = """SELECT c.*,o.status AS outcome_status,o.result AS outcome,
                    p.status AS profile_status,p.result AS profile
            FROM orderflow_price_candidates c
            LEFT JOIN orderflow_price_candidate_results o
              ON o.event_id=c.event_id AND o.result_kind='outcome'
            LEFT JOIN orderflow_price_candidate_results p
              ON p.event_id=c.event_id AND p.result_kind='profile'""" + where
        query += " ORDER BY c.timestamp DESC,c.event_id"
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(0, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            value = copy.deepcopy(row)
            value["status"] = value.pop("outcome_status") or "unknown"
            result.append(_serializable(value))
        return result

    def read_pending_price_candidates(self, limit=None):
        query = """SELECT c.* FROM orderflow_price_candidates c
            LEFT JOIN orderflow_price_candidate_results o
              ON o.event_id=c.event_id AND o.result_kind='outcome'
            WHERE o.event_id IS NULL AND c.method_version=%s ORDER BY c.timestamp ASC"""
        params = [PRICE_SPIKE_METHOD_VERSION]
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(0, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return _serializable(rows)

    def read_pending_price_profiles(self, limit=None):
        query = """SELECT c.*,o.result AS outcome
            FROM orderflow_price_candidates c
            JOIN orderflow_price_candidate_results o
              ON o.event_id=c.event_id AND o.result_kind='outcome'
            LEFT JOIN orderflow_price_candidate_results p
              ON p.event_id=c.event_id AND p.result_kind='profile'
            WHERE c.method_version=%s AND o.status='matured'
              AND o.result->>'direction' IN ('up','down') AND p.event_id IS NULL
            ORDER BY c.timestamp ASC"""
        params = [PRICE_SPIKE_METHOD_VERSION]
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(0, int(limit)))
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return _serializable(rows)

    def read_price_observations(self, pair, start, end):
        with self._connection() as conn:
            rows = conn.execute("""SELECT event_id,timestamp,observation
                FROM orderflow_price_candidates WHERE method_version=%s AND pair=%s
                    AND timestamp >= %s AND timestamp <= %s ORDER BY timestamp""",
                (PRICE_SPIKE_METHOD_VERSION, str(pair).upper(), _timestamp(start), _timestamp(end))).fetchall()
        return _serializable(rows)

    def price_spike_stats(self, pair=None, *, limit_profiles=5000):
        params = []
        where_parts = ["c.method_version=%s"]
        params.append(PRICE_SPIKE_METHOD_VERSION)
        if pair is not None:
            where_parts.append("c.pair=%s")
            params.append(str(pair).upper())
        where = " WHERE " + " AND ".join(where_parts)
        with self._connection() as conn:
            grouped = conn.execute("""WITH joined AS (
                SELECT c.pair,c.timestamp,c.observation->>'ratio_band' AS band,
                    o.status,o.result->>'direction' AS direction
                FROM orderflow_price_candidates c
                LEFT JOIN orderflow_price_candidate_results o
                  ON o.event_id=c.event_id AND o.result_kind='outcome'""" + where + """)
                SELECT pair,band,COUNT(*) AS candidate_count,
                    COUNT(*) FILTER (WHERE status='matured') AS matured_count,
                    COUNT(*) FILTER (WHERE status='invalid') AS invalid_count,
                    COUNT(*) FILTER (WHERE direction='up') AS up_count,
                    COUNT(*) FILTER (WHERE direction='down') AS down_count,
                    MIN(timestamp) AS first_timestamp,MAX(timestamp) AS last_timestamp
                FROM joined GROUP BY pair,band ORDER BY pair,band""", params).fetchall()
            profiles = conn.execute("""SELECT c.pair,o.result
                FROM orderflow_price_candidates c
                JOIN orderflow_price_candidate_results o ON o.event_id=c.event_id
                WHERE c.method_version=%s AND o.result_kind='profile' AND o.status='matured'"""
                + (" AND c.pair=%s" if pair is not None else "")
                + " ORDER BY c.timestamp DESC LIMIT %s",
                ([PRICE_SPIKE_METHOD_VERSION, str(pair).upper(), max(0, int(limit_profiles))]
                 if pair is not None else [PRICE_SPIKE_METHOD_VERSION, max(0, int(limit_profiles))])).fetchall()
        bands = ("lt1", "1to2", "2to4", "4to8", "8to20", "ge20")
        # The monitored pairs exist even before the first online candidate.
        # Zero is a known count; probability and return estimates stay unknown.
        pair_names = ([str(pair).upper()] if pair is not None
                      else sorted({"BTC/USDT:USDT", "ETH/USDT:USDT", *(row["pair"] for row in grouped)}))
        by_pair = {}
        for pair_name in pair_names:
            rows = [row for row in grouped if row["pair"] == pair_name]
            matured_total = sum(int(row["matured_count"]) for row in rows)
            any_total = sum(int(row["up_count"]) + int(row["down_count"]) for row in rows)
            base_p_any = any_total / matured_total if matured_total else None
            buckets = {}
            for band in bands:
                row = next((item for item in rows if item["band"] == band), None)
                n = int(row["matured_count"]) if row else 0
                up = int(row["up_count"]) if row else 0
                down = int(row["down_count"]) if row else 0
                buckets[band] = {
                    "candidate_count": int(row["candidate_count"]) if row else 0,
                    "matured_count": n,
                    "invalid_count": int(row["invalid_count"]) if row else 0,
                    "up_count": up, "down_count": down, "any_count": up + down,
                    "p_up": up / n if n else None,
                    "p_down": down / n if n else None,
                    "p_any": (up + down) / n if n else None,
                    "lift": ((up + down) / n / base_p_any) if n and base_p_any else None,
                }
            by_pair[pair_name] = {
                "pair": pair_name, "candidate_count": sum(int(row["candidate_count"]) for row in rows),
                "matured_count": matured_total,
                "invalid_count": sum(int(row["invalid_count"]) for row in rows),
                "base_p_any": base_p_any, "bands": buckets,
                "first_timestamp": min((int(row["first_timestamp"]) for row in rows), default=None),
                "last_timestamp": max((int(row["last_timestamp"]) for row in rows), default=None),
            }
        profile_groups = {}
        for row in profiles:
            result = row["result"] or {}
            direction = result.get("direction")
            if direction not in {"up", "down"}:
                continue
            key = (row["pair"], direction)
            offsets = profile_groups.setdefault(key, {offset: [] for offset in range(-12, 13)})
            for point in result.get("points", []):
                try:
                    offset = int(point["offset"])
                    ratio = _number(point["volume_ratio"], "volume_ratio")
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if offset in offsets:
                    offsets[offset].append(ratio)
        profile = {
            pair_name: {
                direction: [{"offset": offset,
                             "mean_volume_ratio": (sum(values) / len(values) if values else None),
                             "sample_count": len(values)}
                            for offset, values in sorted(profile_groups.get(
                                (pair_name, direction),
                                {offset: [] for offset in range(-12, 13)},
                            ).items())]
                for direction in ("up", "down")
            }
            for pair_name in pair_names
        }
        return {"method_version": PRICE_SPIKE_METHOD_VERSION,
                "event_threshold_pct": 0.015, "forward_bars": 3,
                "ratio_bands": list(bands), "by_pair": by_pair,
                "profile_by_pair": profile,
                "profile_sample_limit": max(0, int(limit_profiles)),
                "sample_scope": "live_online_only",
                "unknown_excluded": True, "invalid_excluded": True}

    def stats(self):
        # 实时写入不清空聚合缓存；避免采集密集时状态轮询重复 COUNT 大表。
        started = time.perf_counter()
        with self._stats_lock:
            if self._stats_cache is None or time.monotonic() - self._stats_cache_at >= 10:
                with self._connection() as conn:
                    counts = {}
                    for name in ("documents", "events", "users", "artifacts", "series_points", "external_records"):
                        value = conn.execute(self._sql.SQL("SELECT COUNT(*) AS n FROM {}").format(self._sql.Identifier(name))).fetchone()["n"]
                        counts[{"series_points": "series_points", "external_records": "trade_records"}.get(name, name)] = value
                    datasets = conn.execute("""SELECT exchange,market,pair,timeframe,candle_type,rows,
                        first_timestamp,last_timestamp FROM candle_datasets ORDER BY exchange,market,pair,timeframe,candle_type""").fetchall()
                    counts["candles"] = sum(row["rows"] for row in datasets)
                    streams = conn.execute("SELECT * FROM stream_stats ORDER BY kind,exchange,market,pair").fetchall()
                    for kind in ("ticks", "orderbooks"):
                        counts[kind] = sum(row["rows"] for row in streams if row["kind"] == kind)
                    database = conn.execute("""SELECT current_database() AS name,
                        COALESCE(SUM(pg_total_relation_size(c.oid)),0)::BIGINT AS size_bytes
                        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                        WHERE n.nspname=%s AND c.relkind='r'""", (self.schema,)).fetchone()
                    version = conn.execute("SELECT payload FROM store_metadata WHERE key='schema_version'").fetchone()
                    # 返回主机和数据库名称，不返回 DSN、用户名、密码或查询参数。
                    location = f"postgresql://{conn.info.host or 'local'}:{conn.info.port}/{conn.info.dbname}"
                    self._stats_cache = {
                        "engine": "postgresql", "database": {"path": location, "name": database["name"],
                        "schema": self.schema, "size_bytes": database["size_bytes"],
                        "schema_version": version["payload"], "journal_mode": "wal"},
                        "counts": counts, "datasets": datasets, "streams": streams,
                    }
                self._stats_cache_at = time.monotonic()
            result = copy.deepcopy(self._stats_cache)
        with self._connection() as conn:
            result["sources"] = conn.execute("""SELECT key,path,kind,row_count,updated_at,error,last_success_at
                FROM sources ORDER BY key""").fetchall()
            sync = conn.execute("SELECT payload FROM store_metadata WHERE key='sync_state'").fetchone()
        result["sync"] = {"last_at": None, "last_success_at": None, "running": False, **(sync["payload"] if sync else {})}
        result["performance"] = {"stats_ms": round((time.perf_counter() - started) * 1000, 3),
            "last_query_ms": self._last_query_ms, "aggregate_cache_seconds": 10,
            "aggregate_age_seconds": round(time.monotonic() - self._stats_cache_at, 3)}
        return _serializable(result)
