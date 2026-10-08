#!/usr/bin/env python3
"""只读采集现有研究文件和交易快照，增量同步到 PostgreSQL。"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import sqlite3
import time
from typing import Any, Iterator
import zipfile


# 配置文件和凭据始终留在原来的私有配置位置。
PRIVATE_PARTS = {"private", "secrets", ".git", ".venv", "__pycache__"}
PRIVATE_NAMES = re.compile(r"config|secret|credential|password|smoke", re.IGNORECASE)
SENSITIVE_KEYS = re.compile(
    r"^(?:api[_-]?key|api[_-]?secret|secret|password|passwd|token|"
    r"jwt[_-]?secret[_-]?key|ws[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"private[_-]?key|client[_-]?secret|authorization)$", re.IGNORECASE
)
SQLITE_TABLES = (
    "trades", "orders", "wallet_history", "pairlocks", "locks",
    "KeyValueStore", "trade_custom_data", "custom_data",
)
TRUSTED_PICKLES = {
    "funding_binance.pkl": ("binance", "funding_rate_archive", None),
    "funding_okx.pkl": ("okx", "funding_rate_archive", None),
    "dvol_btc.pkl": ("deribit", "dvol", "BTC"),
    "dvol_eth.pkl": ("deribit", "dvol", "ETH"),
}
BATCH_SIZE = 5000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_payload(value: Any) -> Any:
    """清理不可序列化的数值，并递归剔除非用户表中的凭据字段。"""
    if isinstance(value, dict):
        return {str(k): clean_payload(v) for k, v in value.items()
                if not SENSITIVE_KEYS.match(str(k))}
    if isinstance(value, (list, tuple)):
        return [clean_payload(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return {"encoding": "base64", "value": base64.b64encode(value).decode("ascii")}
    if hasattr(value, "item"):
        return clean_payload(value.item())
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value


def public_source(path: Path, base: Path) -> bool:
    """拒绝符号链接和配置路径，避免扫描越界或导入敏感配置。"""
    try:
        relative = path.relative_to(base)
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return (not path.is_symlink()
            and not any(part.lower() in PRIVATE_PARTS for part in relative.parts)
            and not PRIVATE_NAMES.search(relative.as_posix()))


def fingerprint(path: Path, sqlite: bool = False) -> tuple[int, int]:
    # WAL 的变化也属于交易库变化；不能只查看主 SQLite 文件。
    paths = [path, Path(str(path) + "-wal")] if sqlite else [path]
    stats = [p.stat() for p in paths if p.exists()]
    return max(s.st_mtime_ns for s in stats), sum(s.st_size for s in stats)


@contextmanager
def exclusive_sync(root: Path) -> Iterator[None]:
    """同一项目只允许一个同步进程，退出或崩溃后操作系统自动释放锁。"""
    path = root / "database" / "runtime" / "data_sync.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("已有数据同步进程运行") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def candle_identity(path: Path, data_dir: Path) -> tuple[str, str, str, str, str]:
    """从现有文件名提取交易所、市场、交易对、周期和数据类型。"""
    rel = path.relative_to(data_dir)
    if rel.parts[0] == "orderflow":
        match = re.fullmatch(r"(.+)_([1-9]\d*[smhdwM])_(orderflow|oi)", path.stem)
        if not match:
            raise ValueError("不支持的订单流文件名")
        coin, timeframe, kind = match.groups()
        return "binance", "futures", f"{coin}/USDT:USDT", timeframe, kind
    match = re.fullmatch(r"(.+)-([1-9]\d*[smhdwM])(?:-(futures|mark|index|funding_rate|merged))?", path.stem)
    if not match:
        raise ValueError("不支持的行情文件名")
    symbol, timeframe, kind = match.groups()
    exchange = rel.parts[0]
    market = "futures" if "futures" in rel.parts or kind in {
        "futures", "mark", "index", "funding_rate", "merged"
    } else "spot"
    pieces = symbol.split("_")
    if len(pieces) == 1 and exchange == "merged":
        pair = f"{symbol}/USDT:USDT"
    elif len(pieces) in (2, 3):
        pair = f"{pieces[0]}/{pieces[1]}"
        if market == "futures":
            pair += ":" + (pieces[2] if len(pieces) == 3 else pieces[1])
    else:
        raise ValueError("不支持的交易对文件名")
    return exchange, market, pair, timeframe, kind or "spot"


def timestamp_ms(values: Any) -> Any:
    """把有时区、无时区和数字时间统一到 UTC 毫秒。"""
    import pandas as pd

    series = pd.Series(values)
    if pd.api.types.is_numeric_dtype(series):
        valid = pd.to_numeric(series, errors="coerce").dropna().abs()
        scale = valid.median() if len(valid) else 0
        unit = "ns" if scale >= 1e17 else "us" if scale >= 1e14 else "ms" if scale >= 1e11 else "s"
        dates = pd.to_datetime(series, unit=unit, utc=True, errors="coerce")
    else:
        dates = pd.to_datetime(series, utc=True, errors="coerce")
    # 先检查 NaT；不能把 NaT 的 int64 哨兵当作有效历史行情。
    result = pd.Series(dates.astype("datetime64[ns, UTC]").astype("int64") // 1_000_000, index=series.index)
    return result.where(dates.notna())


def normalized_frame(frame: Any, funding: bool = False) -> tuple[Any, int]:
    import numpy as np
    import pandas as pd

    if "date" not in frame.columns:
        raise ValueError("行情缺少 date")
    columns = ["open", "high", "low", "close", "volume"]
    if any(col not in frame.columns for col in columns):
        raise ValueError("行情缺少 OHLCV")
    source_count = len(frame)
    data = frame.copy().reset_index(drop=True)
    data["timestamp"] = timestamp_ms(data["date"])
    for col in columns:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    valid = data["timestamp"].notna() & (data["timestamp"] >= 0)
    valid &= np.isfinite(data[columns]).all(axis=1)
    valid &= data["volume"] >= 0
    if not funding:
        valid &= (data[columns[:4]] > 0).all(axis=1)
    data = data[valid].drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    data["timestamp"] = data["timestamp"].astype("int64")
    return data, source_count - len(data)


def sqlite_snapshot(path: Path, timeout: float = 3.0) -> dict[str, list[dict[str, Any]]]:
    """短只读事务取得一致快照，先释放 SQLite 再写 PostgreSQL。"""
    deadline = time.monotonic() + timeout
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)
    connection.row_factory = sqlite3.Row
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {}
        for table in SQLITE_TABLES:
            if table in names:
                # 表名只来自固定白名单，不能由文件内容插入 SQL。
                result[table] = [clean_payload(dict(row)) for row in connection.execute(f'SELECT * FROM "{table}"')]
        return result
    finally:
        connection.rollback()
        connection.close()


class DataSynchronizer:
    def __init__(self, store: Any, root: Path | str, batch_size: int = BATCH_SIZE):
        self.store = store
        self.root = Path(root).resolve()
        self.user_data = self.root / "bot" / "user_data"
        self.batch_size = max(1, batch_size)
        self.summary: dict[str, Any] = {}
        self._last_fast_service = time.monotonic()

    def _key(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _unchanged(self, key: str, stamp: tuple[int, int], force: bool) -> bool:
        state = self.store.source_state(key)
        return (not force and state is not None and not state.get("error")
                and (state.get("mtime_ns"), state.get("size")) == stamp)

    def _run_source(self, path: Path, kind: str, callback: Any, force: bool,
                    sqlite: bool = False) -> None:
        key = self._key(path)
        try:
            stamp = fingerprint(path, sqlite=sqlite)
            if self._unchanged(key, stamp, force):
                self.summary["skipped"] += 1
                return
            rows = callback(path, key)
            if not sqlite and fingerprint(path) != stamp:
                raise RuntimeError("源文件在同步期间变化，将于下一轮重试")
            self.store.record_source(key, key, kind, stamp[0], stamp[1], rows)
            self.summary["files"] += 1
            self.summary["rows"] += rows
        except Exception as exc:
            # 错误消息可能含数据库 DSN 或原始凭据，状态和日志只存异常类别。
            self.summary["errors"] += 1
            if path.exists():
                stamp = fingerprint(path, sqlite=sqlite)
                self.store.record_source(key, key, kind, stamp[0], stamp[1], 0,
                                         error=type(exc).__name__)
            print(f"[sync] {key}: {type(exc).__name__}", flush=True)

    def _document(self, path: Path, key: str) -> int:
        document_key = path.relative_to(self.user_data).as_posix()
        with path.open(encoding="utf-8") as handle:
            if path.suffix.lower() == ".jsonl":
                # 整个文件解析成功才替换事件；半条尾行留到下一轮重试。
                records = [clean_payload(json.loads(line)) for line in handle if line.strip()]
                return self.store.replace_events(document_key, records, source_path=key)
            payload = clean_payload(json.load(handle))
        self.store.put_document(document_key, payload, source_path=key)
        return 1

    def _users(self, path: Path, key: str) -> int:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        # 用户迁移由数据库层保证只插入缺失用户；原始密码哈希不会重算。
        migrated = self.store.migrate_users(path)
        return len(payload.get("users", {})) if migrated else 0

    def _sqlite(self, path: Path, key: str) -> int:
        snapshot = sqlite_snapshot(path)
        return self.store.replace_external_snapshot(key, snapshot)

    def _batches(self, rows: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
        for start in range(0, len(rows), self.batch_size):
            yield rows[start:start + self.batch_size]

    def _yield_fast(self) -> None:
        # 初次历史导入也每五秒服务一次进度和交易快照，训练进程无需等待。
        if time.monotonic() - self._last_fast_service >= 5:
            self._fast_sources(False)
            self._last_fast_service = time.monotonic()
            self.store.set_sync_state({**self.summary, "last_at": utc_now()})

    def _feather(self, path: Path, key: str) -> int:
        import pandas as pd

        exchange, market, pair, timeframe, kind = candle_identity(path, self.user_data / "data")
        frame = pd.read_feather(path)
        total = 0
        if kind == "funding_rate":
            if "date" not in frame.columns:
                raise ValueError("资金费率缺少日期")
            column = "funding_rate" if "funding_rate" in frame.columns else "close"
            if column not in frame.columns:
                raise ValueError("资金费率缺少数值")
            data = frame.copy().reset_index(drop=True)
            data["timestamp"] = timestamp_ms(data["date"])
            data["value"] = pd.to_numeric(data[column], errors="coerce")
            valid = (data["timestamp"].notna() & (data["timestamp"] >= 0)
                     & data["value"].map(math.isfinite))
            data = data[valid].drop_duplicates("timestamp", keep="last").sort_values("timestamp")
            self.summary["rejected_rows"] += len(frame) - len(data)
            rows = [{"timestamp": int(row.timestamp), "value": float(row.value),
                     "extras": {"market": market, "timeframe": timeframe}}
                    for row in data.itertuples()]
            for batch in self._batches(rows):
                total += self.store.upsert_series(exchange, "funding_rate", pair, batch, source_key=key)
                self._yield_fast()
            return total
        if kind == "oi":
            if "date" not in frame.columns:
                raise ValueError("持仓量缺少日期")
            frame = frame.copy().reset_index(drop=True)
            frame["timestamp"] = timestamp_ms(frame["date"])
            for column, metric in (("oi", "open_interest"), ("oi_val", "open_interest_value")):
                if column not in frame.columns:
                    continue
                values = pd.to_numeric(frame[column], errors="coerce")
                valid = frame["timestamp"].notna() & values.map(lambda x: math.isfinite(x) and x >= 0)
                data = frame[valid].copy()
                data["value"] = values[valid]
                data = data.drop_duplicates("timestamp", keep="last")
                rows = [{"timestamp": int(row.timestamp), "value": float(row.value),
                         "extras": {"market": market, "timeframe": timeframe}}
                        for row in data.itertuples()]
                for batch in self._batches(rows):
                    total += self.store.upsert_series(exchange, metric, pair, batch, source_key=key)
                    self._yield_fast()
            return total
        frame, rejected = normalized_frame(frame, funding=kind == "funding_rate")
        self.summary["rejected_rows"] += rejected
        columns = {"date", "timestamp", "open", "high", "low", "close", "volume"}
        for start in range(0, len(frame), self.batch_size):
            rows = []
            for row in frame.iloc[start:start + self.batch_size].to_dict(orient="records"):
                rows.append({"timestamp": int(row["timestamp"]),
                             **{col: float(row[col]) for col in columns - {"date", "timestamp"}},
                             "extras": clean_payload({col: value for col, value in row.items() if col not in columns})})
            total += self.store.upsert_candles(exchange, market, pair, timeframe, kind, rows, source_key=key)
            self._yield_fast()
        return total

    def _pickle(self, path: Path, key: str) -> int:
        import pandas as pd

        # pickle 可执行代码，只读取项目固定目录内四个已知数据源文件。
        if path.parent != self.user_data / "newsrc" or path.name not in TRUSTED_PICKLES or path.is_symlink():
            raise ValueError("拒绝非受信 pickle")
        source, metric, asset = TRUSTED_PICKLES[path.name]
        data = pd.read_pickle(path)
        if isinstance(data, pd.Series):
            data = data.to_frame(asset or data.name or "value")
        if not isinstance(data, pd.DataFrame):
            raise ValueError("pickle 不是表格或时间序列")
        times = timestamp_ms(data.index).to_numpy()
        total = 0
        for name in data.columns:
            values = pd.to_numeric(data[name], errors="coerce").to_numpy()
            unique = {}
            for stamp, value in zip(times, values):
                if pd.notna(stamp) and stamp >= 0 and math.isfinite(value):
                    unique[int(stamp)] = float(value)
            series_asset = asset or f"{name}/USDT:USDT"
            rows = [{"timestamp": stamp, "value": value,
                    "extras": {"market": "futures" if metric.startswith("funding_rate") else "index",
                               "origin": "newsrc"}}
                    for stamp, value in sorted(unique.items())]
            for batch in self._batches(rows):
                total += self.store.upsert_series(source, metric, series_asset, batch, source_key=key)
                self._yield_fast()
        return total

    def _artifact(self, path: Path, key: str) -> int:
        stamp = path.stat()
        metadata = {"suffix": path.suffix.lower()}
        kind = "model" if "models" in path.relative_to(self.user_data).parts else "cache"
        if path.suffix.lower() == ".npy":
            import numpy as np

            # 内存映射只读取数组头信息，禁止把大型训练张量复制进数据库。
            array = np.load(path, mmap_mode="r", allow_pickle=False)
            metadata.update(shape=list(array.shape), dtype=str(array.dtype))
            del array
        elif path.suffix.lower() == ".parquet":
            import pyarrow.parquet as parquet

            info = parquet.read_metadata(path)
            metadata.update(rows=info.num_rows, row_groups=info.num_row_groups,
                            columns=info.schema.names)
        self.store.save_artifact(key, key, stamp.st_size, stamp.st_mtime_ns, kind, metadata)
        return 1

    def _prediction(self, path: Path, key: str) -> int:
        import pandas as pd

        frame = pd.read_feather(path)
        return self._tabular_records(frame, path, key, "prediction")

    def _csv(self, path: Path, key: str) -> int:
        import pandas as pd

        frame = pd.read_csv(path)
        return self._tabular_records(frame, path, key, "csv")

    def _tabular_records(self, frame: Any, path: Path, key: str, kind: str) -> int:
        # pandas 负责日期 ISO 化和 NaN 转 null，保留模型原来的预测字段。
        records = clean_payload(json.loads(frame.to_json(orient="records", date_format="iso", double_precision=15)))
        document_key = path.relative_to(self.user_data).as_posix()
        count = self.store.replace_events(document_key, records, source_path=key)
        stamp = path.stat()
        self.store.save_artifact(key, key, stamp.st_size, stamp.st_mtime_ns, kind,
                                 {"rows": len(frame), "columns": list(frame.columns)})
        return count

    def _archive(self, path: Path, key: str) -> int:
        documents = []
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                relative = PurePosixPath(member.filename)
                # 全部在内存中读取，不解压；拒绝目录穿越和异常大型成员。
                if (relative.is_absolute() or ".." in relative.parts or "\\" in member.filename
                        or member.is_dir() or relative.suffix.lower() != ".json"
                        or PRIVATE_NAMES.search(relative.as_posix())
                        or any(part.lower() in PRIVATE_PARTS for part in relative.parts)):
                    continue
                if member.file_size > 128 * 1024 * 1024:
                    raise ValueError("回测 JSON 成员超过大小上限")
                payload = clean_payload(json.loads(archive.read(member)))
                document_key = path.relative_to(self.user_data).as_posix() + "/" + relative.as_posix()
                documents.append((document_key, payload))
        # 解析全部成功再提交各文档，损坏 ZIP 不会覆盖已有回测结果。
        for document_key, payload in documents:
            self.store.put_document(document_key, payload, source_path=key)
        self._artifact(path, key)
        return len(documents)

    def _fast_sources(self, force: bool) -> None:
        for path in sorted(self.user_data.rglob("*")):
            if path.suffix.lower() in {".json", ".jsonl"} and path.is_file() and public_source(path, self.user_data):
                self._run_source(path, "document", self._document, force)
        users = self.root / "auth" / "users.json"
        if users.is_file() and not users.is_symlink():
            self._run_source(users, "users", self._users, force)
        for path in sorted((self.root / "bot").glob("*.sqlite")):
            if public_source(path, self.root / "bot"):
                self._run_source(path, "sqlite", self._sqlite, force, sqlite=True)

    def run(self, fast: bool = False, force: bool = False) -> dict[str, Any]:
        self.summary = {"status": "running", "running": True, "mode": "fast" if fast else "full",
                        "started_at": utc_now(), "pid": os.getpid(), "files": 0,
                        "rows": 0, "errors": 0, "skipped": 0, "rejected_rows": 0}
        self.store.set_sync_state(self.summary)
        self._fast_sources(force)
        if not fast:
            for path in sorted((self.user_data / "data").rglob("*.feather")):
                if public_source(path, self.user_data):
                    self._run_source(path, "feather", self._feather, force)
            for name in TRUSTED_PICKLES:
                path = self.user_data / "newsrc" / name
                if path.is_file() and public_source(path, self.user_data):
                    self._run_source(path, "series", self._pickle, force)
            for path in sorted(self.user_data.rglob("*.csv")):
                if path.is_file() and public_source(path, self.user_data):
                    self._run_source(path, "csv", self._csv, force)
            for folder in ("models", "freqaimodels", "cache", "backtest_results", "hyperopt_results"):
                for path in sorted((self.user_data / folder).rglob("*")):
                    if (path.is_file() and path.suffix.lower() not in {".json", ".jsonl", ".csv"}
                            and public_source(path, self.user_data)):
                        if folder == "models" and path.suffix.lower() == ".feather":
                            self._run_source(path, "prediction", self._prediction, force)
                        elif folder == "backtest_results" and path.suffix.lower() == ".zip":
                            self._run_source(path, "archive", self._archive, force)
                        else:
                            self._run_source(path, "artifact", self._artifact, force)
                        self._yield_fast()
        finished_at = utc_now()
        self.summary.update(status="error" if self.summary["errors"] else "completed",
                            running=False, finished_at=finished_at, last_at=finished_at)
        if not self.summary["errors"]:
            self.summary["last_success_at"] = finished_at
        self.store.set_sync_state(self.summary)
        return dict(self.summary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读同步项目文件至 PostgreSQL")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="执行一轮后退出（默认）")
    mode.add_argument("--daemon", action="store_true", help="持续增量同步")
    parser.add_argument("--fast", action="store_true", help="仅同步 JSON、用户和交易快照")
    parser.add_argument("--force", action="store_true", help="忽略成功文件指纹，重新同步")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--interval", type=float, default=5.0, help="快速同步间隔秒数")
    parser.add_argument("--scan-interval", type=float, default=60.0, help="行情和缓存扫描间隔秒数")
    args = parser.parse_args(argv)
    from data_store import DataStore

    running = True

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal running
        running = False

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    try:
        with exclusive_sync(args.root):
            store = DataStore(root=args.root)
            store.initialize()
            synchronizer = DataSynchronizer(store, args.root)
            last_full = float("-inf")
            while running:
                started = time.monotonic()
                fast = args.fast or started - last_full < max(5.0, args.scan_interval)
                result = synchronizer.run(fast=fast, force=args.force)
                if not fast:
                    last_full = time.monotonic()
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if not args.daemon:
                    return 1 if result["errors"] else 0
                # 短等待允许信号及时结束；不会阻塞模型和其它服务进程。
                deadline = started + max(1.0, args.interval)
                while running and time.monotonic() < deadline:
                    time.sleep(min(0.25, deadline - time.monotonic()))
    except Exception as exc:
        print(f"[sync] 无法启动: {type(exc).__name__}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
