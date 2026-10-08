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
        """COPY 批量暂存，保留同批次最后一个重复键，再统一 UPSERT。"""
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
        query = sql.SQL("""WITH changed AS (
            INSERT INTO {table} ({cols})
            SELECT {cols} FROM (SELECT DISTINCT ON ({keys}) {cols} FROM {stage}
                ORDER BY {keys},_ordinal DESC) AS latest
            ON CONFLICT ({keys}) DO UPDATE SET {updates}
            RETURNING xmax=0 AS inserted
        ) SELECT COUNT(*) AS written,COUNT(*) FILTER (WHERE inserted) AS inserted FROM changed""").format(
            table=table_id, cols=cols, keys=key_cols, stage=stage_id, updates=sql.SQL(",").join(updates))
        return conn.execute(query).fetchone()

    def upsert_candles(self, exchange, market, pair, timeframe, candle_type, rows, source_key=None):
        identity = (exchange, market, pair, timeframe, candle_type)
        with self._connection() as conn:
            # 同一数据集写入串行化，摘要计数与行情事务保持一致。
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("candles:" + _json_text(identity),))
            def values():
                for row in rows:
                    yield (*identity, _timestamp(row["timestamp"]),
                           *(_number(row[name], name) for name in ("open", "high", "low", "close", "volume")),
                           self._json(row.get("extras") or {}), source_key)
            result = self._copy_upsert(conn, "candles",
                ["exchange", "market", "pair", "timeframe", "candle_type", "timestamp",
                 "open", "high", "low", "close", "volume", "extras", "source_key"],
                ["exchange", "market", "pair", "timeframe", "candle_type", "timestamp"], values())
            if result["written"]:
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
