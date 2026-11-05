#!/usr/bin/env python3
"""Read archived and local market data with explicit schemas and provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.archive_retention import (  # noqa: E402
    ARCHIVE_ROOT,
    TABLE_SPECS,
    PostgresArchiveSource,
    verify_manifest,
)


def _date_ms(value, end=False):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _primary_key(name):
    return TABLE_SPECS[name].primary_key


def _deduplicate_by_primary_key(frame: pd.DataFrame, primary_key, source_name: str):
    key_columns = list(primary_key)
    duplicate_mask = frame.duplicated(subset=key_columns, keep=False)
    duplicate_count = int(frame.duplicated(subset=key_columns, keep="first").sum())
    if duplicate_count:
        duplicate_rows = frame.loc[duplicate_mask]
        for key, group in duplicate_rows.groupby(key_columns, sort=False, dropna=False):
            normalized_rows = [
                tuple(_normalize_value(value) for value in row)
                for row in group.itertuples(index=False, name=None)
            ]
            if any(row != normalized_rows[0] for row in normalized_rows[1:]):
                raise ValueError(
                    f"conflicting {source_name} rows for primary key {key!r}"
                )
    result = frame.drop_duplicates(subset=key_columns, keep="first").copy()
    result.attrs["duplicate_rows_removed"] = duplicate_count
    return result, duplicate_count


def union_tables(table_name: str, local: pd.DataFrame, archived: pd.DataFrame) -> pd.DataFrame:
    """Merge local and archived rows, rejecting conflicting versions of a key."""
    combined = pd.concat([local, archived], ignore_index=True)
    key = list(_primary_key(table_name))
    result, duplicate_count = _deduplicate_by_primary_key(combined, key, "union")
    result = result.sort_values(by=["timestamp", *key], kind="mergesort").reset_index(drop=True)
    result.attrs["duplicate_rows_removed"] = duplicate_count
    return result


def _normalize_value(value):
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            scalar = value.item()
        except (ValueError, AttributeError):
            scalar = value
        if scalar is not value:
            return _normalize_value(scalar)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, (datetime, pd.Timestamp)):
        timestamp = value
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return {"timestamp": timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds")}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"decimal": format(value, "f")}
    if isinstance(value, float):
        return None if math.isnan(value) else {"float64": value.hex()}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    return value


def _canonical_dataset_sha256(frame: pd.DataFrame, primary_key) -> str:
    key_columns = list(primary_key)
    ordered = frame.sort_values(key_columns, kind="mergesort")
    header = {
        "columns": list(ordered.columns),
        "dtypes": [str(dtype) for dtype in ordered.dtypes],
    }
    digest = hashlib.sha256()
    digest.update(json.dumps(header, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")
    for row in ordered.itertuples(index=False, name=None):
        canonical_row = [_normalize_value(value) for value in row]
        digest.update(json.dumps(canonical_row, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def equivalence_report(archived: pd.DataFrame, local: pd.DataFrame, primary_key) -> dict:
    key_columns = list(primary_key)
    raw_archive_rows, raw_local_rows = len(archived), len(local)
    archived, archive_duplicates = _deduplicate_by_primary_key(archived, key_columns, "archive")
    local, local_duplicates = _deduplicate_by_primary_key(local, key_columns, "local")
    columns_match = list(archived.columns) == list(local.columns)
    dtype_match = archived.dtypes.astype(str).to_dict() == local.dtypes.astype(str).to_dict()
    archived_sorted = archived.sort_values(key_columns, kind="mergesort").reset_index(drop=True)
    local_sorted = local.sort_values(key_columns, kind="mergesort").reset_index(drop=True)
    row_count_match = len(archived_sorted) == len(local_sorted)
    sample_size = 0
    differences = 0
    archive_by_key = {}
    local_by_key = {}
    if columns_match:
        for frame, index in ((archived_sorted, archive_by_key), (local_sorted, local_by_key)):
            for row in frame.to_dict(orient="records"):
                key = tuple(_normalize_value(row[column]) for column in key_columns)
                index[key] = row
        common_keys = set(archive_by_key).intersection(local_by_key)
        sample_size = min(1000, len(common_keys))
        sample_keys = sorted(
            common_keys,
            key=lambda key: hashlib.sha256(json.dumps(
                key, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).digest(),
        )[:sample_size]
        for key in sample_keys:
            for column in archived.columns:
                if _normalize_value(archive_by_key[key][column]) != _normalize_value(local_by_key[key][column]):
                    differences += 1
    archive_sha256 = _canonical_dataset_sha256(archived_sorted, key_columns)
    local_sha256 = _canonical_dataset_sha256(local_sorted, key_columns)
    return {
        "archive_rows": len(archived_sorted), "local_rows": len(local_sorted),
        "archive_duplicate_rows_removed": archive_duplicates,
        "local_duplicate_rows_removed": local_duplicates,
        "archive_input_rows": raw_archive_rows, "local_input_rows": raw_local_rows,
        "row_count_match": row_count_match,
        "columns_match": columns_match, "dtype_match": dtype_match,
        "sample_size": sample_size, "sample_differences": differences,
        "archive_canonical_sha256": archive_sha256,
        "local_canonical_sha256": local_sha256,
        "canonical_sha256_match": archive_sha256 == local_sha256,
        "archive_min_ts": int(archived["timestamp"].min()) if len(archived) else None,
        "archive_max_ts": int(archived["timestamp"].max()) if len(archived) else None,
        "local_min_ts": int(local["timestamp"].min()) if len(local) else None,
        "local_max_ts": int(local["timestamp"].max()) if len(local) else None,
    }


def resample_1m_close_to_5m(frame: pd.DataFrame) -> pd.DataFrame:
    rows = frame.loc[frame["timeframe"] == "1m"].copy()
    if rows.empty:
        return pd.DataFrame(columns=["exchange", "market", "pair", "candle_type", "timestamp", "close"])
    rows["timestamp"] = pd.to_datetime(rows["timestamp"], unit="ms", utc=True)
    rows = rows.sort_values("timestamp", kind="mergesort")
    rows["timestamp"] = rows["timestamp"].dt.floor("5min")
    keys = ["exchange", "market", "pair", "candle_type", "timestamp"]
    result = rows.groupby(keys, as_index=False, sort=True).tail(1)[keys + ["close"]]
    result["timestamp"] = result["timestamp"].map(lambda value: int(value.timestamp() * 1000))
    return result.sort_values(keys[:-1] + ["timestamp"], kind="mergesort").reset_index(drop=True)


def compare_resampled_feature(archived_1m: pd.DataFrame, local_1m: pd.DataFrame) -> dict:
    archive_feature = resample_1m_close_to_5m(archived_1m)
    local_feature = resample_1m_close_to_5m(local_1m)
    key = ["exchange", "market", "pair", "candle_type", "timestamp"]
    report = equivalence_report(archive_feature, local_feature, key)
    report["feature"] = "1m close resampled to UTC 5m last close"
    return report


def _read_archive(table_name, start_ms, end_ms, archive_root):
    spec = TABLE_SPECS[table_name]
    root = Path(archive_root) / table_name
    frames = []
    checksums = []
    manifests = []
    if root.exists():
        for manifest_path in sorted(root.glob("date=*/part-*.parquet.manifest.json")):
            data_path = Path(str(manifest_path)[:-len(".manifest.json")])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest["max_ts"] < start_ms or manifest["min_ts"] >= end_ms:
                continue
            verify_manifest(data_path, manifest)
            table = pq.ParquetFile(data_path).read()
            if not table.schema.equals(spec.schema, check_metadata=False):
                raise TypeError(f"archive schema mismatch: {data_path}")
            frame = table.to_pandas()
            frame = frame[(frame.timestamp >= start_ms) & (frame.timestamp < end_ms)]
            frames.append(frame)
            checksums.append({"path": str(data_path), "sha256": manifest["sha256"],
                              "bytes": int(manifest["bytes"]), "verified": True})
            manifests.append(manifest_path)
    empty = pa.Table.from_batches([], schema=spec.schema).to_pandas()
    result = pd.concat(frames, ignore_index=True) if frames else empty
    result, duplicate_count = _deduplicate_by_primary_key(
        result, spec.primary_key, "archive"
    )
    result = result.sort_values(
        by=["timestamp", *spec.primary_key], kind="mergesort"
    ).reset_index(drop=True)
    result.attrs["archive_duplicate_rows_removed"] = duplicate_count
    return result, checksums, manifests


def _read_local(source, table_name, start_ms, end_ms):
    spec = TABLE_SPECS[table_name]
    rows = source.iter_rows(spec, start_ms, end_ms)
    from tools.archive_retention import build_arrow_table
    batch_frames = []
    buffer = []
    for row in rows:
        buffer.append(row)
        if len(buffer) >= 50_000:
            batch_frames.append(build_arrow_table(table_name, buffer).to_pandas())
            buffer = []
    if buffer:
        batch_frames.append(build_arrow_table(table_name, buffer).to_pandas())
    return pd.concat(batch_frames, ignore_index=True) if batch_frames else pa.Table.from_batches(
        [], schema=spec.schema).to_pandas()


def load_dataset(table_name, start_ms, end_ms, archive_root=ARCHIVE_ROOT,
                 source_mode="union", database_source=None) -> tuple[pd.DataFrame, dict]:
    if start_ms >= end_ms:
        raise ValueError("start must be earlier than end")
    archived, checksums, manifests = _read_archive(table_name, start_ms, end_ms, archive_root)
    local = pd.DataFrame(columns=archived.columns)
    if source_mode in {"union", "local"}:
        if database_source is None:
            raise ValueError("database_source is required for union or local reads")
        local = _read_local(database_source, table_name, start_ms, end_ms)
    if source_mode == "archive":
        result = archived
    elif source_mode == "local":
        result = local
    elif source_mode == "union":
        result = union_tables(table_name, local, archived)
    else:
        raise ValueError("source_mode must be archive, local, or union")
    report = {
        "table": table_name, "source_mode": source_mode,
        "archive_rows": len(archived), "local_rows": len(local), "output_rows": len(result),
        "archive_duplicate_rows_removed": archived.attrs.get("archive_duplicate_rows_removed", 0),
        "archive_checksums": checksums,
        "archive_bytes": sum(item["bytes"] for item in checksums),
        "archive_files": [str(path) for path in manifests],
        "missing_dates": _missing_dates(result, start_ms, end_ms),
        "min_ts": int(result.timestamp.min()) if len(result) else None,
        "max_ts": int(result.timestamp.max()) if len(result) else None,
        "duplicate_rows_removed": (len(local) + len(archived) - len(result)
                                    if source_mode == "union" else 0),
    }
    return result, report


def _missing_dates(frame, start_ms, end_ms):
    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
    end = datetime.fromtimestamp(max(start_ms, end_ms - 1) / 1000, timezone.utc).date()
    present = set()
    for value in frame.get("timestamp", []):
        present.add(datetime.fromtimestamp(int(value) / 1000, timezone.utc).date())
    missing = []
    current = start
    while current <= end:
        if current not in present:
            missing.append(current.isoformat())
        from datetime import timedelta
        current += timedelta(days=1)
    return missing


def _load_dsn():
    from runtime_config import load_environment
    load_environment()
    dsn = os.environ.get("QUANT_DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("QUANT_DATABASE_URL is not configured")
    return dsn


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", choices=sorted(TABLE_SPECS), required=True)
    parser.add_argument("--start", required=True, help="inclusive UTC date/time")
    parser.add_argument("--end", required=True, help="exclusive UTC date/time")
    parser.add_argument("--source", choices=("union", "archive", "local"), default="union")
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--dsn-schema", default="quant")
    parser.add_argument("--compare-local", action="store_true",
                        help="额外读取同一时间区间的本地 DB 并生成等价性报告")
    args = parser.parse_args(argv)
    start_ms, end_ms = _date_ms(args.start), _date_ms(args.end)
    database_source = None
    if args.source in {"union", "local"} or args.compare_local:
        database_source = PostgresArchiveSource(_load_dsn(), args.dsn_schema)
    try:
        started = datetime.now(timezone.utc)
        frame, report = load_dataset(args.table, start_ms, end_ms, args.archive_root,
                                     args.source, database_source if args.source in {"union", "local"} else None)
        report["read_seconds"] = (datetime.now(timezone.utc) - started).total_seconds()
        report["rows_per_second"] = (len(frame) / report["read_seconds"]
                                     if report["read_seconds"] else None)
        if args.compare_local:
            archive_started = datetime.now(timezone.utc)
            archive_frame, archive_report = load_dataset(
                args.table, start_ms, end_ms, args.archive_root, "archive")
            archive_report["read_seconds"] = (datetime.now(timezone.utc) - archive_started).total_seconds()
            archive_report["rows_per_second"] = (
                len(archive_frame) / archive_report["read_seconds"]
                if archive_report["read_seconds"] else None
            )
            local_started = datetime.now(timezone.utc)
            local_frame, local_report = load_dataset(
                args.table, start_ms, end_ms, args.archive_root, "local", database_source)
            local_report["read_seconds"] = (datetime.now(timezone.utc) - local_started).total_seconds()
            local_report["rows_per_second"] = (
                len(local_frame) / local_report["read_seconds"]
                if local_report["read_seconds"] else None
            )
            report["equivalence"] = equivalence_report(
                archive_frame, local_frame, TABLE_SPECS[args.table].primary_key)
            report["equivalence"]["archive_sha256"] = [
                item["sha256"] for item in archive_report["archive_checksums"]]
            report["equivalence"]["archive_read_seconds"] = archive_report["read_seconds"]
            report["equivalence"]["local_read_seconds"] = local_report["read_seconds"]
            report["equivalence"]["archive_rows_per_second"] = archive_report["rows_per_second"]
            report["equivalence"]["local_rows_per_second"] = local_report["rows_per_second"]
            report["equivalence"]["archive_bytes"] = archive_report["archive_bytes"]
            if args.table == "candles":
                report["equivalence"]["resampled_feature"] = compare_resampled_feature(
                    archive_frame, local_frame)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            table = pa.Table.from_pandas(frame, schema=TABLE_SPECS[args.table].schema,
                                         preserve_index=False, safe=True)
            pq.write_table(table, args.out, compression="zstd", compression_level=6, version="2.6")
            report["output_path"] = str(args.out)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    finally:
        if database_source is not None:
            database_source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
