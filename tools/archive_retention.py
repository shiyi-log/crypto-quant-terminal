#!/usr/bin/env python3
"""Copy old market data to training-friendly Parquet files; never delete DB rows."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


ARCHIVE_VERSION = 1
ARCHIVE_ROOT = Path("/Volumes/yidong/quant_archive")
RETENTION_DAYS = 7
BATCH_ROWS = 50_000
ESTIMATE_SAMPLE_ROWS = 10_000
UTC = timezone.utc


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    schema: pa.Schema
    predicate: str = "TRUE"


TABLE_SPECS = {
    "ticks": TableSpec(
        "ticks",
        ("exchange", "market", "pair", "trade_id", "timestamp", "price", "quantity",
         "buyer_maker", "extras", "received_at"),
        ("exchange", "market", "pair", "trade_id"),
        pa.schema([
            ("exchange", pa.string()), ("market", pa.string()), ("pair", pa.string()),
            ("trade_id", pa.string()), ("timestamp", pa.int64()),
            ("price", pa.decimal128(38, 18)), ("quantity", pa.decimal128(38, 18)),
            ("buyer_maker", pa.bool_()), ("extras", pa.string()),
            ("received_at", pa.timestamp("us", tz="UTC")),
        ]),
    ),
    "orderbooks": TableSpec(
        "orderbooks",
        ("exchange", "market", "pair", "timestamp", "last_update_id", "bids", "asks",
         "extras", "received_at"),
        ("exchange", "market", "pair", "timestamp", "last_update_id"),
        pa.schema([
            ("exchange", pa.string()), ("market", pa.string()), ("pair", pa.string()),
            ("timestamp", pa.int64()), ("last_update_id", pa.string()),
            ("bids", pa.string()), ("asks", pa.string()), ("extras", pa.string()),
            ("received_at", pa.timestamp("us", tz="UTC")),
        ]),
    ),
    "candles": TableSpec(
        "candles",
        ("exchange", "market", "pair", "timeframe", "candle_type", "timestamp", "open",
         "high", "low", "close", "volume", "extras", "source_key", "updated_at"),
        ("exchange", "market", "pair", "timeframe", "candle_type", "timestamp"),
        pa.schema([
            ("exchange", pa.string()), ("market", pa.string()), ("pair", pa.string()),
            ("timeframe", pa.string()), ("candle_type", pa.string()), ("timestamp", pa.int64()),
            ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()),
            ("close", pa.float64()), ("volume", pa.float64()), ("extras", pa.string()),
            pa.field("source_key", pa.string(), nullable=True),
            pa.field("updated_at", pa.timestamp("us", tz="UTC"), nullable=False),
        ]),
        "timeframe IN ('1m', '5m')",
    ),
}

JSON_COLUMNS = {
    "ticks": {"extras"},
    "orderbooks": {"bids", "asks", "extras"},
    "candles": {"extras"},
}


def _canonical_json(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":"))


def _utc_datetime(value):
    if value is None or isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result is not None and result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC) if result is not None else None


def build_arrow_table(table_name: str, rows) -> pa.Table:
    spec = TABLE_SPECS[table_name]
    columns = [[] for _ in spec.columns]
    for row in rows:
        if len(row) != len(spec.columns):
            raise ValueError(f"{table_name} row has {len(row)} fields; expected {len(spec.columns)}")
        for index, (column, field, raw) in enumerate(zip(spec.columns, spec.schema, row)):
            if raw is None:
                value = None
            elif column in JSON_COLUMNS[table_name]:
                value = _canonical_json(raw)
            elif pa.types.is_timestamp(field.type):
                value = _utc_datetime(raw)
            elif pa.types.is_integer(field.type):
                value = int(raw)
            elif pa.types.is_floating(field.type):
                value = float(raw)
            elif pa.types.is_decimal(field.type):
                value = Decimal(str(raw))
            elif pa.types.is_boolean(field.type):
                value = str(raw).lower() in {"t", "true", "1"}
            else:
                value = str(raw)
            columns[index].append(value)
    arrays = [pa.array(values, type=field.type) for values, field in zip(columns, spec.schema)]
    return pa.Table.from_arrays(arrays, schema=spec.schema)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parquet_size(table: pa.Table) -> int:
    output = pa.BufferOutputStream()
    pq.write_table(table, output, compression="zstd", compression_level=6, version="2.6")
    return output.getvalue().size


def _timestamp_bounds(table_name: str, table: pa.Table) -> tuple[int, int]:
    values = table.column("timestamp").to_pylist()
    return min(values), max(values)


def write_parquet_part(table_name: str, partition_date: date, rows, archive_root,
                       source_db_version: str, source_database: str = "",
                       expected_row_count: int | None = None) -> dict:
    spec = TABLE_SPECS[table_name]
    root = Path(archive_root)
    directory = root / table_name / f"date={partition_date.isoformat()}"
    directory.mkdir(parents=True, exist_ok=True)
    iterator = iter(rows)
    first = next(iterator, None)
    if first is None:
        raise ValueError("cannot write an empty archive part")

    fd, temp_name = tempfile.mkstemp(prefix=".part-", suffix=".parquet.tmp", dir=directory)
    os.close(fd)
    temp_path = Path(temp_name)
    writer = None
    row_count = 0
    min_ts = None
    max_ts = None
    try:
        for chunk in _chunks(itertools.chain((first,), iterator), BATCH_ROWS):
            table = build_arrow_table(table_name, chunk)
            if writer is None:
                writer = pq.ParquetWriter(temp_path, spec.schema, compression="zstd",
                                          compression_level=6, version="2.6")
            writer.write_table(table)
            chunk_min, chunk_max = _timestamp_bounds(table_name, table)
            min_ts = chunk_min if min_ts is None else min(min_ts, chunk_min)
            max_ts = chunk_max if max_ts is None else max(max_ts, chunk_max)
            row_count += table.num_rows
        writer.close()
        writer = None
        if expected_row_count is not None and row_count != int(expected_row_count):
            raise RuntimeError(
                f"row count mismatch while exporting {table_name}/{partition_date}: "
                f"expected {expected_row_count}, got {row_count}"
            )
        digest = _sha256(temp_path)
        filename = f"part-{min_ts}-{max_ts}-{digest[:16]}.parquet"
        data_path = directory / filename
        manifest_path = directory / f"{filename}.manifest.json"
        if not data_path.exists():
            os.replace(temp_path, data_path)
        else:
            temp_path.unlink()
        manifest = {
            "archive_version": ARCHIVE_VERSION,
            "source_table": table_name,
            "partition_date": partition_date.isoformat(),
            "row_count": row_count,
            "min_ts": min_ts,
            "max_ts": max_ts,
            "columns": [{"name": field.name, "type": str(field.type),
                         "nullable": field.nullable} for field in spec.schema],
            "sha256": digest,
            "bytes": data_path.stat().st_size,
            "compression": "zstd-6",
            "source_database": source_database,
            "source_db_version": source_db_version,
        }
        fd, temp_manifest_name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
            os.replace(temp_manifest_name, manifest_path)
        finally:
            if os.path.exists(temp_manifest_name):
                os.unlink(temp_manifest_name)
        verify_manifest(data_path)
        return {"data_path": str(data_path), "manifest_path": str(manifest_path),
                "row_count": row_count, "min_ts": min_ts, "max_ts": max_ts,
                "bytes": data_path.stat().st_size, "sha256": digest}
    except Exception:
        if writer is not None:
            writer.close()
        if temp_path.exists():
            temp_path.unlink()
        raise


def _chunks(values, size):
    chunk = []
    for value in values:
        chunk.append(value)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def verify_manifest(data_path, manifest=None) -> bool:
    data_path = Path(data_path)
    if str(data_path).endswith(".manifest.json"):
        manifest_path = data_path
        data_path = Path(str(data_path)[:-len(".manifest.json")])
        if manifest is None:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest_path = Path(str(data_path) + ".manifest.json")
    if manifest is None:
        if not manifest_path.is_file():
            raise ValueError(f"manifest missing: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if _sha256(data_path) != manifest.get("sha256"):
        raise ValueError(f"checksum mismatch: {data_path}")
    if data_path.stat().st_size != int(manifest.get("bytes", -1)):
        raise ValueError(f"byte count mismatch: {data_path}")
    parquet = pq.ParquetFile(data_path)
    if parquet.metadata.num_rows != int(manifest.get("row_count", -1)):
        raise ValueError(f"row count mismatch: {data_path}")
    table_name = manifest.get("source_table")
    if table_name not in TABLE_SPECS:
        raise ValueError("manifest contains an unsupported source table")
    spec = TABLE_SPECS[table_name]
    if not parquet.schema_arrow.equals(spec.schema, check_metadata=False):
        raise ValueError(f"schema mismatch: {data_path}")
    expected_columns = [{"name": field.name, "type": str(field.type),
                         "nullable": field.nullable} for field in spec.schema]
    if manifest.get("columns") != expected_columns:
        raise ValueError(f"manifest schema mismatch: {data_path}")
    table = pq.read_table(data_path, columns=["timestamp"])
    timestamps = table.column("timestamp").to_pylist()
    if (min(timestamps), max(timestamps)) != (manifest.get("min_ts"), manifest.get("max_ts")):
        raise ValueError(f"timestamp range mismatch: {data_path}")
    return True


def check_archive_volume(archive_root=ARCHIVE_ROOT) -> dict:
    archive_root = Path(archive_root)
    mount = Path("/Volumes/yidong")
    is_configured_volume = archive_root == ARCHIVE_ROOT or mount in archive_root.parents
    if is_configured_volume and not os.path.ismount(mount):
        raise RuntimeError(f"archive volume is not mounted: {mount}")
    existing = archive_root
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.is_dir() or not os.access(existing, os.W_OK | os.X_OK):
        raise RuntimeError(f"archive destination is unavailable or not writable: {existing}")
    usage = shutil.disk_usage(existing)
    return {"mount": str(mount if is_configured_volume else existing),
            "available_bytes": usage.free, "archive_root": str(archive_root)}


class PostgresArchiveSource:
    """Client-side COPY reader; PostgreSQL never writes to the external volume."""

    def __init__(self, dsn: str, schema: str = "quant"):
        import psycopg
        from psycopg import sql

        self.sql = sql
        self.conn = psycopg.connect(dsn, options="-c default_transaction_read_only=on")
        self.conn.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
        self.conn.commit()
        self.server_version = self.conn.execute("SHOW server_version").fetchone()[0]
        self.database = self.conn.execute("SELECT current_database()").fetchone()[0]
        self.conn.commit()

    def close(self):
        self.conn.close()

    @contextmanager
    def snapshot(self):
        """Keep row counts, size estimates, and COPY output on one read-only MVCC view."""
        with self.conn.transaction():
            self.conn.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            yield

    def _table_name(self, spec):
        return self.sql.Identifier(spec.name)

    def partitions(self, spec: TableSpec, cutoff_ms: int) -> list[dict]:
        query = self.sql.SQL("""
            SELECT (to_timestamp(timestamp / 1000.0) AT TIME ZONE 'UTC')::date AS partition_date,
                   COUNT(*) AS row_count, MIN(timestamp) AS min_ts, MAX(timestamp) AS max_ts,
                   COALESCE(SUM(pg_column_size(t)), 0) AS tuple_bytes
              FROM {} AS t
             WHERE timestamp < %s AND ({})
             GROUP BY partition_date ORDER BY partition_date
        """).format(self._table_name(spec), self.sql.SQL(spec.predicate))
        rows = self.conn.execute(query, (cutoff_ms,)).fetchall()
        return [{"date": row[0], "row_count": row[1], "min_ts": row[2], "max_ts": row[3],
                 "tuple_bytes": row[4]} for row in rows]

    def iter_rows(self, spec: TableSpec, start_ms: int, end_ms: int, limit: int | None = None):
        selected = self.sql.SQL(", ").join(
            self.sql.SQL("{}::text").format(self.sql.Identifier(column))
            for column in spec.columns
        )
        order = self.sql.SQL(", ").join(self.sql.Identifier(column) for column in
                                         ("timestamp",) + spec.primary_key)
        limit_clause = self.sql.SQL(" LIMIT %s") if limit is not None else self.sql.SQL("")
        params = (start_ms, end_ms) + ((int(limit),) if limit is not None else ())
        query = self.sql.SQL("""
            COPY (SELECT {} FROM {} AS t
                   WHERE timestamp >= %s AND timestamp < %s AND ({})
                   ORDER BY {}{}) TO STDOUT
        """).format(selected, self._table_name(spec), self.sql.SQL(spec.predicate), order,
                    limit_clause)
        with self.conn.cursor().copy(query, params) as copy:
            copy.set_types(["text"] * len(spec.columns))
            yield from copy.rows()


def _date_window(partition_date: date) -> tuple[int, int]:
    start = datetime.combine(partition_date, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _estimate_partition(source, spec, part, start_ms, cutoff_ms):
    row_count = int(part["row_count"])
    if row_count == 0:
        return 0, "exact"
    sample_limit = min(row_count, ESTIMATE_SAMPLE_ROWS)
    sample = list(source.iter_rows(spec, start_ms, cutoff_ms, limit=sample_limit))
    table = build_arrow_table(spec.name, sample)
    measured = _parquet_size(table)
    if row_count <= ESTIMATE_SAMPLE_ROWS:
        return measured, "exact-in-memory"
    return round(measured * row_count / len(sample)), f"sample-{len(sample)}-rows"


def run_archive(source, archive_root=ARCHIVE_ROOT, cutoff_ms=None,
                table_names=None, dry_run=True) -> dict:
    if cutoff_ms is None:
        cutoff_ms = int((datetime.now(UTC) - timedelta(days=RETENTION_DAYS)).timestamp() * 1000)
    volume = check_archive_volume(archive_root)
    selected = table_names or list(TABLE_SPECS)
    results = {"archive_only": True, "dry_run": dry_run, "cutoff_ms": cutoff_ms,
               "volume": volume, "tables": {}}
    snapshot_factory = getattr(source, "snapshot", None)
    snapshot = snapshot_factory() if snapshot_factory is not None else nullcontext()
    with snapshot:
        return _run_archive_snapshot(
            source, archive_root, cutoff_ms, selected, dry_run, results
        )


def _run_archive_snapshot(source, archive_root, cutoff_ms, selected, dry_run, results):
    for table_name in selected:
        spec = TABLE_SPECS[table_name]
        table_result = {"rows": 0, "tuple_bytes": 0, "estimated_parquet_bytes": 0,
                        "estimate_method": "exact-in-memory", "partitions": [], "parts": []}
        for part in source.partitions(spec, cutoff_ms):
            start_ms, day_end_ms = _date_window(part["date"])
            end_ms = min(day_end_ms, cutoff_ms)
            estimate, method = _estimate_partition(source, spec, part, start_ms, end_ms)
            table_result["rows"] += int(part["row_count"])
            table_result["tuple_bytes"] += int(part["tuple_bytes"])
            table_result["estimated_parquet_bytes"] += estimate
            if method != "exact-in-memory":
                table_result["estimate_method"] = method
            partition_result = {
                "date": part["date"].isoformat(), "rows": int(part["row_count"]),
                "tuple_bytes": int(part["tuple_bytes"]),
                "estimated_parquet_bytes": estimate, "estimate_method": method,
                "min_ts": int(part["min_ts"]), "max_ts": int(part["max_ts"]),
            }
            table_result["partitions"].append(partition_result)
            if not dry_run:
                part_result = write_parquet_part(
                    table_name, part["date"], source.iter_rows(spec, start_ms, end_ms),
                    archive_root, source_db_version=source.server_version,
                    source_database=getattr(source, "database", ""),
                    expected_row_count=part["row_count"],
                )
                if part_result["row_count"] != int(part["row_count"]):
                    raise RuntimeError(f"row count mismatch before considering archive part complete: {table_name}/{part['date']}")
                table_result["parts"].append({**part_result, "date": part["date"].isoformat()})
        table_result["rows_archived"] = 0 if dry_run else sum(p["row_count"] for p in table_result["parts"])
        results["tables"][table_name] = table_result
    results["estimated_total_bytes"] = sum(item["estimated_parquet_bytes"]
                                            for item in results["tables"].values())
    results["source_delete_count"] = 0
    return results


def restore_manifest(conn, manifest_path, target_schema: str, persist=True) -> dict:
    """Restore one verified part. persist=False uses and rolls back a temporary schema."""
    from psycopg import sql
    from psycopg.types.json import Jsonb

    manifest_path = Path(manifest_path)
    data_path = Path(str(manifest_path)[:-len(".manifest.json")])
    verify_manifest(data_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    spec = TABLE_SPECS[manifest["source_table"]]
    table = pq.ParquetFile(data_path).read()
    ddl_types = {
        "string": "TEXT", "int64": "BIGINT", "double": "DOUBLE PRECISION",
        "bool": "BOOLEAN", "decimal128(38, 18)": "NUMERIC(38,18)",
        "timestamp[us, tz=UTC]": "TIMESTAMPTZ",
    }
    columns_ddl = ", ".join(
        f'"{field.name}" {"JSONB" if field.name in JSON_COLUMNS[spec.name] else ddl_types[str(field.type)]}'
        + (" NOT NULL" if not field.nullable else "")
        for field in spec.schema
    )
    primary_key_ddl = ", ".join(f'"{column}"' for column in spec.primary_key)
    columns = sql.SQL(", ").join(sql.Identifier(field.name) for field in spec.schema)
    stage_name = f"archive_restore_stage_{os.urandom(8).hex()}"

    class _RollbackRestore(Exception):
        def __init__(self, report):
            self.report = report

    try:
        with conn.transaction():
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(target_schema)))
            conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({}, PRIMARY KEY ({}))").format(
                sql.Identifier(target_schema), sql.Identifier(spec.name),
                sql.SQL(columns_ddl), sql.SQL(primary_key_ddl)))
            conn.execute(sql.SQL("CREATE TEMP TABLE {} (LIKE {}.{} INCLUDING ALL) ON COMMIT DROP").format(
                sql.Identifier(stage_name), sql.Identifier(target_schema), sql.Identifier(spec.name)))
            copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(stage_name), columns)
            with conn.cursor().copy(copy_sql) as copy:
                for row in table.to_pylist():
                    values = []
                    for name in spec.columns:
                        value = row[name]
                        if name in JSON_COLUMNS[spec.name] and value is not None:
                            value = Jsonb(json.loads(value))
                        values.append(value)
                    copy.write_row(values)
            join_keys = sql.SQL(" AND ").join(
                sql.SQL("target.{} = stage.{}").format(sql.Identifier(key), sql.Identifier(key))
                for key in spec.primary_key
            )
            row_target = sql.SQL(", ").join(
                sql.SQL("target.{}").format(sql.Identifier(column)) for column in spec.columns)
            row_stage = sql.SQL(", ").join(
                sql.SQL("stage.{}").format(sql.Identifier(column)) for column in spec.columns)
            conflicts = conn.execute(sql.SQL("""
                SELECT COUNT(*) FROM {}.{} AS target
                JOIN {} AS stage ON {}
                WHERE ROW({}) IS DISTINCT FROM ROW({})
            """).format(sql.Identifier(target_schema), sql.Identifier(spec.name),
                         sql.Identifier(stage_name), join_keys, row_target, row_stage)).fetchone()[0]
            if conflicts:
                raise ValueError(f"restore primary-key conflicts contain {conflicts} differing rows")
            insert_sql = sql.SQL("""
                INSERT INTO {}.{} ({}) SELECT {} FROM {}
                ON CONFLICT ({}) DO NOTHING
            """).format(sql.Identifier(target_schema), sql.Identifier(spec.name), columns,
                         columns, sql.Identifier(stage_name), sql.SQL(primary_key_ddl))
            insert_cursor = conn.execute(insert_sql)
            inserted_count = insert_cursor.rowcount
            restored_count = conn.execute(sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                sql.Identifier(target_schema), sql.Identifier(spec.name))).fetchone()[0]
            report = {"target_schema": target_schema, "table": spec.name,
                      "rows_restored": inserted_count, "part_rows": len(table),
                      "target_rows": restored_count, "persisted": persist}
            if not persist:
                raise _RollbackRestore(report)
    except _RollbackRestore as rollback:
        return rollback.report
    return {"target_schema": target_schema, "table": spec.name,
            "rows_restored": inserted_count, "part_rows": len(table),
            "target_rows": restored_count,
            "persisted": persist}


def _load_dsn():
    from runtime_config import load_environment
    load_environment()
    dsn = os.environ.get("QUANT_DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("QUANT_DATABASE_URL is not configured")
    return dsn


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只读统计（默认）")
    mode.add_argument("--archive", action="store_true", help="归档 Parquet，不删除本地数据")
    mode.add_argument("--restore", metavar="MANIFEST", help="校验并回灌一个 manifest 指定的数据分片")
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--schema", default="quant", help="PostgreSQL 源 schema")
    parser.add_argument("--target-schema", help="restore 目标 schema；必须显式指定")
    parser.add_argument("--table", action="append", choices=sorted(TABLE_SPECS), dest="tables")
    parser.add_argument("--cutoff", help="UTC ISO 时间；默认当前时间减 7 天")
    args = parser.parse_args(argv)
    if args.restore:
        if not args.target_schema:
            parser.error("--restore requires --target-schema")
        dsn = _load_dsn()
        import psycopg
        with psycopg.connect(dsn) as conn:
            result = restore_manifest(conn, args.restore, args.target_schema, persist=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    cutoff_ms = None
    if args.cutoff:
        cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        cutoff_ms = int(cutoff.astimezone(UTC).timestamp() * 1000)
    source = PostgresArchiveSource(_load_dsn(), args.schema)
    try:
        report = run_archive(source, args.archive_root, cutoff_ms=cutoff_ms,
                             table_names=args.tables, dry_run=not args.archive)
    finally:
        source.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
