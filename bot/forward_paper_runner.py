#!/usr/bin/env python3
"""Incremental, append-only forward paper replay.

The runner consumes cumulative candle snapshots.  Each snapshot is replayed
through :func:`event_backtest.run_v2`; only events that have not appeared in
the append-only ledgers are written.  Keeping the replay deterministic makes
the process restartable without persisting a mutable trading engine state,
while the candle-prefix fingerprints prevent a provider from silently
rewriting history.

This module is deliberately separate from the live trading path.  It models
fees through ``run_v2``; slippage and funding remain unknown.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import event_backtest as engine  # noqa: E402
import paper_dryrun as paper  # noqa: E402


SCHEMA_VERSION = "forward-paper-v2"
LEDGER_FILES = ("manifest.jsonl", "decisions.jsonl", "fills.jsonl")


def _plain(value: Any) -> Any:
    """Convert pandas/numpy values into JSON-safe deterministic values."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        else:
            stamp = stamp.tz_convert("UTC")
        return stamp.isoformat()
    if isinstance(value, np.ndarray):
        return _plain(value.tolist())
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if np.isfinite(number) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def sha256(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _utc_iso(value: Any) -> str:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return stamp.isoformat()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row at {path}:{number} is not an object")
            rows.append(value)
    return rows


def _append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    payload = "".join(canonical_json(row) + "\n" for row in rows)
    if not payload:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return payload.count("\n")


def _write_json_atomic(path: Path, value: Any) -> None:
    payload = (canonical_json(value) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _runtime() -> dict[str, str]:
    return {"python": platform.python_version(), "numpy": np.__version__,
            "pandas": pd.__version__}


def _normalize_snapshot(data: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Normalize available frames while preserving empty symbols as missing.

    ``paper.normalize_data`` intentionally rejects an all-empty input.  A
    forward collector, however, must record an empty/partial observation as
    ``data_ready=false`` rather than turn it into an exception.
    """
    if not isinstance(data, dict):
        raise TypeError("data must be a mapping of symbol to DataFrame")
    missing = []
    available: dict[str, pd.DataFrame] = {}
    for symbol, frame in data.items():
        symbol = str(symbol)
        if not isinstance(frame, pd.DataFrame):
            raise TypeError(f"{symbol}: snapshot value must be a DataFrame")
        if not {"open", "close"}.issubset(frame.columns):
            raise ValueError(f"{symbol}: open and close columns are required")
        if frame.empty:
            missing.append(symbol)
        else:
            available[symbol] = frame
    if not available:
        return {}, sorted(set(missing) | set(str(k) for k in data))
    normalized = paper.normalize_data(available)
    return normalized, sorted(set(missing))


def _prefix_fingerprints(data: dict[str, pd.DataFrame], through: pd.Timestamp) -> dict[str, str]:
    return {
        symbol: paper.data_fingerprint({symbol: frame.loc[frame.index <= through]})
        for symbol, frame in sorted(data.items())
    }


def _latest_common_candle(data: dict[str, pd.DataFrame]) -> pd.Timestamp | None:
    if not data:
        return None
    return min(frame.index.max() for frame in data.values())


def _latest_observed_candle(data: dict[str, pd.DataFrame]) -> pd.Timestamp | None:
    if not data:
        return None
    return max(frame.index.max() for frame in data.values())


def _same_timestamp_rows(data: dict[str, pd.DataFrame]) -> bool:
    if not data:
        return False
    frames = list(data.values())
    reference = frames[0].index
    # A matching latest timestamp is insufficient: an interior gap would
    # otherwise allow a later snapshot to trade on an incomplete candidate
    # pool.  Require the complete candle timeline to be identical.
    if not all(frame.index.equals(reference) for frame in frames[1:]):
        return False
    # Equal indexes alone still allow every symbol to omit the same candle.
    # A cumulative forward snapshot must have a regular timeline; otherwise
    # the replay would silently jump over an unseen execution interval.
    if len(reference) < 2:
        return True
    deltas = reference[1:] - reference[:-1]
    return bool((deltas > pd.Timedelta(0)).all() and
                (deltas == deltas[0]).all())


def _finite_ohlc_rows(data: dict[str, pd.DataFrame]) -> bool:
    """Return whether every available candle has finite prices.

    ``normalize_data`` deliberately preserves NaNs so the input fingerprint
    can expose them.  They are still unavailable for execution and must make
    the cumulative snapshot fail closed instead of being treated as a valid
    ready candle.
    """
    return bool(data) and all(
        np.isfinite(frame[["open", "close"]].to_numpy(dtype="float64")).all()
        for frame in data.values()
    )


class ForwardPaperRunner:
    """One immutable rule set with append-only forward paper ledgers.

    ``update`` expects a cumulative snapshot containing all candles since the
    first observation.  Replaying from that snapshot is deterministic and
    avoids a separate mutable position database.  The checkpoint is merely a
    restart cursor and prefix-integrity record; the JSONL ledgers are the
    source of truth.
    """

    def __init__(self, output_dir: str | os.PathLike[str],
                 variants: list[dict[str, Any]], *, top_n: int = 8,
                 max_open: int = 10, exposure: float = 0.30,
                 cost_one: float = 0.0005, wallet: float = 10_000.0,
                 clock: Any = None, ledger_store: Any = None):
        if not isinstance(top_n, int) or top_n < 1:
            raise ValueError("top_n must be a positive integer")
        if not isinstance(max_open, int) or max_open < 1:
            raise ValueError("max_open must be a positive integer")
        if not math.isfinite(exposure) or exposure <= 0 or exposure > 1:
            raise ValueError("exposure must be in (0, 1]")
        if not math.isfinite(cost_one) or cost_one < 0:
            raise ValueError("cost_one must be non-negative")
        if not math.isfinite(wallet) or wallet <= 0:
            raise ValueError("wallet must be positive")
        self.output_dir = Path(output_dir)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # JSONL remains the durable outbox.  A database store is optional so
        # an unavailable database can never stop paper observation; failed
        # batches are retried from the outbox on the next update/restart.
        self.ledger_store = ledger_store
        self._ledger_sync_error: str | None = None
        self._ledger_sync_count = 0
        self.variants = paper.freeze_variants(variants)
        self.variant_by_id = {item["variant_id"]: item for item in self.variants}
        self.rules = {"top_n": top_n, "max_open": max_open,
                      "exposure": float(exposure), "cost_one": float(cost_one),
                      "wallet": float(wallet)}
        self.engine_hash = paper.engine_fingerprint()
        identity = {"schema_version": SCHEMA_VERSION, "rules": self.rules,
                    "variants": self.variants, "engine_sha256": self.engine_hash}
        self.run_id = sha256(canonical_json(identity))[:24]
        self.manifest_path = self.output_dir / "manifest.jsonl"
        self.decision_path = self.output_dir / "decisions.jsonl"
        self.fill_path = self.output_dir / "fills.jsonl"
        self.checkpoint_path = self.output_dir / "checkpoint.json"
        self._ensure_manifest()
        self._refresh_ledgers()
        self._sync_database_outbox()

    def _manifest_identity(self) -> dict[str, Any]:
        return {
            "event_type": "manifest",
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "engine": "event_backtest.run_v2",
            "engine_sha256": self.engine_hash,
            "variants": self.variants,
            "rules": self.rules,
            "runtime": _runtime(),
            "cost_completeness": {"fee": "modeled", "slippage": "unknown",
                                   "funding": "unknown"},
            "interpretation": "incremental forward paper replay; not live trading",
        }

    def _ensure_manifest(self) -> None:
        rows = _load_jsonl(self.manifest_path)
        identity = self._manifest_identity()
        if rows:
            if rows[0].get("event_type") != "manifest":
                raise ValueError("manifest ledger starts with a non-manifest event")
            existing = dict(rows[0])
            if existing.get("schema_version") == "forward-paper-v1":
                raise ValueError(
                    "forward-paper-v1 ledgers cannot be resumed with v2 because "
                    "event identities now include lifecycle links; keep the v1 "
                    "ledger intact and choose a new output directory"
                )
            # Runtime versions are informational and may differ after restart;
            # all executable identity fields must remain immutable.
            existing.pop("runtime", None)
            expected = dict(identity)
            expected.pop("runtime", None)
            if existing != expected:
                raise ValueError("immutable manifest conflict")
            for path in (self.decision_path, self.fill_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(exist_ok=True)
            return
        _append_jsonl(self.manifest_path, [identity])
        for path in (self.decision_path, self.fill_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)

    def _refresh_ledgers(self) -> None:
        self._manifest_rows = _load_jsonl(self.manifest_path)
        self._decision_rows = _load_jsonl(self.decision_path)
        self._fill_rows = _load_jsonl(self.fill_path)
        self._decision_ids = {row.get("event_id") for row in self._decision_rows}
        self._fill_ids = {row.get("event_id") for row in self._fill_rows}
        self._decision_keys = {self._logical_key(row): row.get("event_id")
                               for row in self._decision_rows}
        self._fill_keys = {self._logical_key(row): row.get("event_id")
                           for row in self._fill_rows}
        self._decision_by_event_id = {
            row.get("event_id"): row for row in self._decision_rows
        }
        self._fill_by_logical_key = {
            self._logical_key(row): row for row in self._fill_rows
        }
        self._open_positions = self._rebuild_open_positions(self._fill_rows)
        if self.checkpoint_path.exists():
            try:
                self._checkpoint = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("invalid checkpoint JSON") from exc
        else:
            self._checkpoint = {}

    @classmethod
    def _rebuild_open_positions(cls, rows: Iterable[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
        """Rebuild the one-position-per-coin index from append-only fills.

        The engine never holds two positions for one coin in a variant.  A
        deterministic index lets a later exit fill retain the same lifecycle
        ID after a process restart, while tolerating older ledgers that do not
        yet contain the new IDs.
        """
        open_positions: dict[tuple[str, str, str], dict[str, Any]] = {}
        ordered = sorted(rows, key=lambda row: (
            row.get("filled_at_utc", ""),
            row.get("event_id", ""),
        ))
        for row in ordered:
            variant = str(row.get("variant_id", ""))
            coin = str(row.get("coin", ""))
            side = str(row.get("side", ""))
            key = (variant, coin, side)
            if row.get("action") == "entry":
                position_id = row.get("position_id") or sha256(canonical_json({
                    "run_id": row.get("run_id"), "variant_id": variant,
                    "kind": "paper_position", "coin": coin, "side": side,
                    "filled_at_utc": row.get("filled_at_utc"),
                }))
                open_positions[key] = {
                    "position_id": position_id,
                    "paper_trade_id": row.get("paper_trade_id") or position_id,
                    "entry_px": row.get("price"),
                    "quantity": row.get("quantity"),
                    "stake": row.get("stake"),
                    "fee_in": row.get("fee", 0.0),
                    "entry_date": row.get("filled_at_utc"),
                    "side": side,
                    "coin": coin,
                    "pending_exit": False,
                }
            elif row.get("action") == "exit":
                open_positions.pop(key, None)
        return open_positions

    def _engine_state_at(self, variant_id: str, boundary: str | None,
                         data: dict[str, pd.DataFrame]) -> tuple[float, dict[str, dict[str, Any]]]:
        """Reconstruct cash and open positions at a replay boundary.

        Fills are the durable execution record.  Rebuilding from them keeps a
        restart or a data-gap recovery from silently starting a fresh wallet
        and losing an existing lifecycle.
        """
        boundary_stamp = pd.Timestamp(boundary) if boundary is not None else None
        if boundary_stamp is not None and boundary_stamp.tzinfo is not None:
            boundary_stamp = boundary_stamp.tz_convert("UTC")
        cash = float(self.rules["wallet"])
        positions: dict[str, dict[str, Any]] = {}
        coin_to_pair = {
            str(symbol).split("/")[0]: str(symbol) for symbol in data
        }
        rows = [row for row in self._fill_rows
                if str(row.get("variant_id", "")) == str(variant_id)]
        rows.sort(key=lambda row: (str(row.get("filled_at_utc", "")),
                                   0 if row.get("action") == "exit" else 1,
                                   str(row.get("event_id", ""))))
        for row in rows:
            filled = pd.Timestamp(row.get("filled_at_utc"))
            if filled.tzinfo is not None:
                filled = filled.tz_convert("UTC")
            if boundary_stamp is not None and filled > boundary_stamp:
                continue
            coin = str(row.get("coin", ""))
            pair = coin_to_pair.get(coin)
            if pair is None:
                continue
            action = row.get("action")
            side = str(row.get("side", "long"))
            if action == "entry":
                stake = float(row.get("stake", 0.0))
                fee = float(row.get("fee", 0.0))
                cash -= stake + fee
                positions[pair] = {
                    "entry_px": float(row["price"]),
                    "quantity": float(row["quantity"]),
                    "stake": stake,
                    "fee_in": fee,
                    "entry_date": row.get("filled_at_utc"),
                    "side": side,
                    "pending_exit": False,
                }
            elif action == "exit":
                position = positions.pop(pair, None)
                if position is None:
                    raise ValueError(
                        "cannot reconstruct paper state: exit has no entry "
                        f"for {variant_id} {pair}"
                    )
                price = float(row["price"])
                quantity = float(row["quantity"])
                signed_quantity = quantity if position["side"] == "long" else -quantity
                value = position["stake"] + signed_quantity * (
                    price - position["entry_px"]
                )
                cash += value - float(row.get("fee", 0.0))

        # A missing execution price makes an exit pending.  The decision
        # ledger records that intent even though no fill exists yet.
        pending: set[str] = set()
        for row in sorted(self._decision_rows,
                          key=lambda item: (str(item.get("execution_at_utc", "")),
                                            str(item.get("event_id", "")))):
            if str(row.get("variant_id", "")) != str(variant_id):
                continue
            execution = pd.Timestamp(row.get("execution_at_utc"))
            if execution.tzinfo is not None:
                execution = execution.tz_convert("UTC")
            if boundary_stamp is not None and execution > boundary_stamp:
                continue
            for item in row.get("exits", []):
                if item.get("status") != "pending":
                    continue
                pair = coin_to_pair.get(str(item.get("coin", "")))
                if pair in positions:
                    pending.add(pair)
        for pair in pending:
            positions[pair]["pending_exit"] = True
        return cash, positions

    def _database_ledger_state(self) -> dict[str, Any]:
        return {
            "enabled": self.ledger_store is not None,
            "synced_event_count": self._ledger_sync_count,
            "sync_error": self._ledger_sync_error,
        }

    def _database_events(self) -> list[dict[str, Any]]:
        """Build the stable database envelope from the append-only outbox."""
        if self.ledger_store is None:
            return []

        events: list[dict[str, Any]] = []
        manifest = self._manifest_rows[0] if self._manifest_rows else None
        if manifest is not None:
            events.append({
                **dict(manifest),
                "event_type": "manifest",
                "event_id": f"manifest:{self.run_id}",
                "run_id": self.run_id,
            })
        events.extend(dict(row) for row in self._manifest_rows[1:]
                       if row.get("event_type") == "snapshot")
        events.extend(dict(row) for row in self._decision_rows)
        events.extend(dict(row) for row in self._fill_rows)
        # checkpoint.json is mutable state, so each persisted version gets a
        # stable event ID derived from its content.  This makes a failed DB
        # write safely replayable without treating a retry as a new state.
        if self._checkpoint:
            checkpoint = dict(self._checkpoint)
            snapshot_id = next(
                (row.get("event_id") for row in reversed(self._manifest_rows)
                 if row.get("event_type") == "snapshot"),
                "initial",
            )
            checkpoint_payload = {
                "event_type": "checkpoint",
                "event_id": f"checkpoint:{self.run_id}:{sha256(canonical_json(checkpoint))}",
                "run_id": self.run_id,
                "snapshot_event_id": snapshot_id,
                "checkpoint": checkpoint,
            }
            events.append(checkpoint_payload)
        return events

    def _sync_database_outbox(self) -> bool:
        """Best-effort replay of JSONL outbox events into the research ledger.

        The runner's observable result is the local outbox.  Database errors
        are retained in state and deliberately swallowed; the next call (or
        a new process) retries every event idempotently.
        """
        if self.ledger_store is None:
            return False
        events = self._database_events()
        if not events:
            return True
        try:
            # Import lazily to keep the paper runner usable in environments
            # that do not install psycopg or configure the production store.
            import research_ledger

            research_ledger.append_forward_paper_events(
                self.ledger_store, self.run_id, events
            )
            self._ledger_sync_error = None
            self._ledger_sync_count = len(events)
            return True
        except Exception as exc:  # noqa: BLE001 - DB must not block paper run
            self._ledger_sync_error = f"{type(exc).__name__}: {exc}"
            return False

    @staticmethod
    def _logical_key(row: dict[str, Any]) -> str:
        event_type = row.get("event_type")
        if event_type == "decision":
            return canonical_json([row.get("variant_id"), event_type,
                                   row.get("execution_at_utc"), row.get("candle_utc")])
        if event_type == "fill":
            return canonical_json([row.get("variant_id"), event_type,
                                   row.get("action"), row.get("coin"),
                                   row.get("filled_at_utc")])
        return canonical_json([row.get("event_id")])

    def _assert_prefix_stable(self, data: dict[str, pd.DataFrame]) -> None:
        frozen = self._checkpoint.get("frozen_prefixes", {})
        for symbol, item in frozen.items():
            frame = data.get(symbol)
            if frame is None:
                continue
            through = pd.Timestamp(item["through_utc"])
            if through.tzinfo is not None:
                through = through.tz_convert("UTC").tz_localize(None)
            actual = paper.data_fingerprint({symbol: frame.loc[frame.index <= through]})
            if actual != item["fingerprint"]:
                raise ValueError(f"immutable candle prefix conflict: {symbol}")

        # Checkpoints written before frozen_prefixes used one shared boundary.
        previous_through = self._checkpoint.get("candle_through_utc")
        previous_fingerprints = self._checkpoint.get("prefix_fingerprints", {})
        if not previous_through or not previous_fingerprints:
            return
        through = pd.Timestamp(previous_through)
        if through.tzinfo is not None:
            through = through.tz_convert("UTC").tz_localize(None)
        for symbol, expected in previous_fingerprints.items():
            frame = data.get(symbol)
            if frame is None:
                continue  # partial snapshots are recorded as not ready
            actual = paper.data_fingerprint({symbol: frame.loc[frame.index <= through]})
            if actual != expected:
                raise ValueError(f"immutable candle prefix conflict: {symbol}")

    @staticmethod
    def _frozen_prefixes(data: dict[str, pd.DataFrame],
                         previous: dict[str, dict[str, str]] | None = None
                         ) -> dict[str, dict[str, str]]:
        """Capture or extend the immutable observed prefix for each symbol."""
        result = dict(previous or {})
        for symbol, frame in data.items():
            if frame.empty:
                continue
            through = frame.index.max()
            old = result.get(symbol)
            if old is not None:
                old_through = pd.Timestamp(old["through_utc"])
                if old_through.tzinfo is not None:
                    old_through = old_through.tz_convert("UTC").tz_localize(None)
                if through < old_through:
                    continue
            prefix = frame.loc[frame.index <= through]
            result[symbol] = {
                "through_utc": _utc_iso(through),
                "fingerprint": paper.data_fingerprint({symbol: prefix}),
            }
        return result

    @staticmethod
    def _next_candle_after(data: dict[str, pd.DataFrame], boundary: str | None
                           ) -> pd.Timestamp | None:
        """Return the boundary candle as signal history for the next execution.

        ``run_v2`` skips index zero by design.  Starting it at the boundary
        therefore makes the following candle the first actionable execution;
        starting one candle later would skip that execution entirely.
        """
        if boundary is None or not data:
            return None
        indexes = [frame.index for frame in data.values() if not frame.empty]
        if not indexes or len(indexes[0]) < 2:
            return None
        deltas = indexes[0][1:] - indexes[0][:-1]
        if not len(deltas) or not (deltas == deltas[0]).all():
            return None
        stamp = pd.Timestamp(boundary)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("UTC").tz_localize(None)
        index = indexes[0]
        prior = index[index <= stamp]
        if len(prior) == 0:
            return None
        signal = prior[-1]
        if not bool((index > signal).any()):
            return None
        return signal

    def _snapshot_event(self, *, data: dict[str, pd.DataFrame], normalized: dict[str, pd.DataFrame],
                        missing: list[str], data_ready: bool, through: pd.Timestamp | None,
                        data_fp: str | None, new_decisions: int = 0,
                        new_fills: int = 0,
                        recovery_of: str | None = None,
                        observed_at_utc: str | None = None,
                        replay_from_utc: str | None = None,
                        blocked_until_utc: str | None = None,
                        replay_completed: bool = False) -> dict[str, Any]:
        through_value = _utc_iso(through) if through is not None else None
        snapshot_id = sha256(canonical_json({"run_id": self.run_id,
                                             "data_fingerprint": data_fp,
                                             "candle_through_utc": through_value,
                                             "data_ready": data_ready,
                                             "missing": sorted(missing),
                                             "replay_completed": replay_completed,
                                             "observed_at_utc": observed_at_utc,
                                             "recovery_of": recovery_of}))
        return {
            "event_type": "snapshot",
            "event_id": snapshot_id,
            "run_id": self.run_id,
            "data_ready": bool(data_ready),
            "replay_completed": bool(replay_completed),
            "data_fingerprint": data_fp,
            "candle_through_utc": through_value,
            "symbols": sorted(normalized),
            "missing": sorted(missing),
            "rows": {symbol: int(len(frame)) for symbol, frame in sorted(normalized.items())},
            "decided_at_utc": (_utc_iso(through + timedelta(minutes=5))
                               if through is not None else None),
            "observed_at_utc": observed_at_utc,
            "replay_from_utc": replay_from_utc,
            "blocked_until_utc": blocked_until_utc,
            "new_decision_count": int(new_decisions),
            "new_fill_count": int(new_fills),
            "recovery_of": recovery_of,
        }

    def _append_events(self, raw_events: list[dict[str, Any]], data_fp: str) -> tuple[int, int]:
        decisions: list[dict[str, Any]] = []
        fills: list[dict[str, Any]] = []
        priority = {"decision": 0, "fill": 1}
        action_priority = {"exit": 0, "entry": 1}
        raw_events.sort(key=lambda event: (
            event.get("execution_at_utc", event.get("filled_at_utc", "")),
            priority.get(event.get("event_type"), 9),
            action_priority.get(event.get("action"), 9), event.get("coin", "")))
        decision_refs: dict[tuple[str, str, str], str] = {}
        for row in self._decision_rows:
            key = (str(row.get("variant_id", "")),
                   str(row.get("execution_at_utc", "")),
                   str(row.get("candle_utc", "")))
            if row.get("event_id"):
                decision_refs[key] = str(row["event_id"])
        for event in raw_events:
            event = dict(event)
            event_type = event.get("event_type")
            if event_type not in {"decision", "fill"}:
                continue
            if event_type == "decision":
                event["candidates"] = [
                    {key: value for key, value in candidate.items() if key != "actual_fill"}
                    for candidate in event.get("candidates", [])
                ]
                event["exits"] = [
                    {key: value for key, value in item.items() if key != "actual_fill"}
                    for item in event.get("exits", [])
                ]
            variant_id = event.pop("_variant_id", None)
            if variant_id is None:
                raise ValueError("engine event is missing variant id")
            variant = next(item for item in self.variants if item["variant_id"] == variant_id)
            body = {"run_id": self.run_id, "variant_id": variant_id,
                    "rule_hash": variant["rule_hash"], "data_fingerprint": data_fp,
                    **event}
            if event_type == "decision":
                decision_key = (variant_id, str(body.get("execution_at_utc", "")),
                                str(body.get("candle_utc", "")))
            else:
                decision_key = (variant_id, str(body.get("filled_at_utc", "")),
                                str(body.get("candle_utc", "")))
                # A fill must point to an observed decision event.  Do not
                # manufacture a hash for a missing parent: a synthetic ID
                # would look linked to consumers while having no ledger row.
                body["decision_event_id"] = decision_refs.get(decision_key)
                lifecycle_key = (variant_id, str(body.get("coin", "")),
                                 str(body.get("side", "")))
                logical = self._logical_key(body)
                existing = self._fill_by_logical_key.get(logical)
                if existing is not None:
                    body["position_id"] = existing.get("position_id")
                    body["paper_trade_id"] = existing.get("paper_trade_id")
                elif body.get("action") == "entry":
                    position_id = sha256(canonical_json({
                        "run_id": self.run_id, "variant_id": variant_id,
                        "kind": "paper_position", "coin": body.get("coin"),
                        "side": body.get("side"),
                        "filled_at_utc": body.get("filled_at_utc"),
                    }))
                    body["position_id"] = position_id
                    body["paper_trade_id"] = position_id
                else:
                    prior = self._open_positions.get(lifecycle_key)
                    if prior is None:
                        raise ValueError(
                            "exit fill has no open paper position: "
                            f"{variant_id} {body.get('coin')} {body.get('side')}"
                        )
                    body["position_id"] = prior["position_id"]
                    body["paper_trade_id"] = prior["paper_trade_id"]
            event_id = sha256(canonical_json({key: value for key, value in body.items()
                                              if key not in {"data_fingerprint", "ordinal"}}))
            body["event_id"] = event_id
            if event_type == "decision":
                # The decision's lifecycle reference is its own immutable
                # event ID; fills use this exact value as their parent link.
                body["decision_event_id"] = event_id
            logical = self._logical_key(body)
            ids = self._decision_ids if event_type == "decision" else self._fill_ids
            keys = self._decision_keys if event_type == "decision" else self._fill_keys
            if logical in keys and keys[logical] != event_id:
                raise ValueError(f"immutable event conflict: {variant_id} {event_type}")
            if event_id in ids:
                continue
            keys[logical] = event_id
            ids.add(event_id)
            if event_type == "decision":
                decision_refs[decision_key] = event_id
                self._decision_by_event_id[event_id] = body
            else:
                lifecycle_key = (variant_id, str(body.get("coin", "")),
                                 str(body.get("side", "")))
                if body.get("action") == "entry":
                    self._open_positions[lifecycle_key] = {
                        "position_id": body["position_id"],
                        "paper_trade_id": body["paper_trade_id"],
                        "entry_px": body.get("price"),
                        "quantity": body.get("quantity"),
                        "stake": body.get("stake"),
                        "fee_in": body.get("fee", 0.0),
                        "entry_date": body.get("filled_at_utc"),
                        "side": body.get("side"),
                        "coin": body.get("coin"),
                        "pending_exit": False,
                    }
                else:
                    self._open_positions.pop(lifecycle_key, None)
                self._fill_by_logical_key[logical] = body
            (decisions if event_type == "decision" else fills).append(body)
        _append_jsonl(self.decision_path, decisions)
        _append_jsonl(self.fill_path, fills)
        self._decision_rows.extend(decisions)
        self._fill_rows.extend(fills)
        return len(decisions), len(fills)

    def _deferred_variant_summaries(self, replay_boundary: str | None,
                                    reason: str) -> dict[str, Any]:
        """Return a shape-stable summary when no replay was executable.

        A ready snapshot can still be the final candle in the observation, so
        there is no safe execution row yet.  Preserve the last successful
        values where available, but always emit the fields used by the API and
        UI; consumers must not infer that a missing key means zero trades.
        """
        previous = self._checkpoint.get("variant_summaries", {})
        summaries: dict[str, Any] = {}
        for variant in self.variants:
            variant_id = variant["variant_id"]
            old = dict(previous.get(variant_id, {}))
            exits = [row for row in self._fill_rows
                     if row.get("variant_id") == variant_id
                     and row.get("action") == "exit"]
            realized = (float(sum(float(row.get("profit_abs", 0.0))
                                  for row in exits)) if exits else None)
            open_count = sum(1 for key in self._open_positions
                             if key[0] == variant_id)
            diagnostics = dict(old.get("diagnostics", {}))
            diagnostics["replay_deferred"] = reason
            summaries[variant_id] = {
                "closed_trade_count": int(len(exits)),
                "realized_profit_after_fee_before_unknown_costs": realized,
                "unrealized_pnl_before_unknown_costs": old.get(
                    "unrealized_pnl_before_unknown_costs"
                ),
                "slippage": old.get("slippage", "unknown"),
                "funding": old.get("funding", "unknown"),
                "net_profit": old.get("net_profit"),
                "ending_equity_marked": old.get("ending_equity_marked"),
                "open_position_count": int(open_count),
                "strategy_usable": False,
                "summary_scope_start_utc": replay_boundary,
                "diagnostics": diagnostics,
            }
        return summaries

    @staticmethod
    def _after_replay_watermark(event: dict[str, Any], watermark: str | None) -> bool:
        """Keep only events observed after an incomplete snapshot watermark.

        A later complete snapshot may contain older candles, but those candles
        were not actionable when the incomplete snapshot was recorded.  The
        replay engine still needs its full indicator history, so filtering is
        done at the event boundary after running from the watermark.
        """
        if watermark is None:
            return True
        execution = event.get("execution_at_utc", event.get("filled_at_utc"))
        if execution is None:
            return False
        execution_stamp = pd.Timestamp(execution)
        watermark_stamp = pd.Timestamp(watermark)
        # Ledger timestamps are serialized as UTC-aware ISO strings while
        # engine indexes may be timezone-naive. Compare on one UTC-naive axis.
        if execution_stamp.tzinfo is not None:
            execution_stamp = execution_stamp.tz_convert("UTC").tz_localize(None)
        if watermark_stamp.tzinfo is not None:
            watermark_stamp = watermark_stamp.tz_convert("UTC").tz_localize(None)
        return execution_stamp > watermark_stamp

    def update(self, data: dict[str, pd.DataFrame]) -> dict[str, Any]:
        """Record one cumulative candle snapshot and return its audit summary."""
        observed_at = pd.Timestamp(self.clock())
        if observed_at.tzinfo is None:
            observed_at = observed_at.tz_localize("UTC")
        else:
            observed_at = observed_at.tz_convert("UTC")
        observed_at_value = observed_at.isoformat()
        normalized, empty_symbols = _normalize_snapshot(data)
        self._assert_prefix_stable(normalized)
        incoming_symbols = {str(symbol) for symbol in data}
        expected_symbols = set(self._checkpoint.get("symbols", []))
        if not expected_symbols and incoming_symbols:
            # Lock the full declared universe on the first observation, even
            # when one or more symbols are empty.  Otherwise a partial first
            # snapshot would make the next complete snapshot look like an
            # illegal universe expansion.
            expected_symbols = incoming_symbols
        elif expected_symbols:
            # Compare the declared incoming universe, including empty frames.
            # Checking only normalized symbols lets a newly introduced empty
            # symbol slip through until it later receives candles.
            added_symbols = incoming_symbols - expected_symbols
            if added_symbols:
                raise ValueError(
                    "immutable symbol universe conflict: "
                    + ",".join(sorted(added_symbols))
                )
        missing = sorted(expected_symbols - set(normalized))
        missing.extend(symbol for symbol in empty_symbols if symbol not in missing)
        through = _latest_common_candle(normalized)
        latest_aligned = _same_timestamp_rows(normalized)
        finite_prices = _finite_ohlc_rows(normalized)
        data_ready = bool(normalized and not missing and latest_aligned and finite_prices)
        data_fp = paper.data_fingerprint(normalized) if normalized else None
        previous_fp = self._checkpoint.get("data_fingerprint")
        previous_through = self._checkpoint.get("candle_through_utc")
        latest_snapshot = next(
            (row for row in reversed(self._manifest_rows)
             if row.get("event_type") == "snapshot"), None
        )
        incoming_through = _utc_iso(through) if through is not None else None
        previous_observed = self._checkpoint.get("last_observed_at_utc")
        cadence = None
        if normalized and _same_timestamp_rows(normalized):
            sample_index = next(iter(normalized.values())).index
            if len(sample_index) > 1:
                cadence = sample_index[1] - sample_index[0]
        if cadence is None and self._checkpoint.get("cadence_seconds"):
            cadence = pd.Timedelta(seconds=float(self._checkpoint["cadence_seconds"]))
        missed_observation_gap = False
        if previous_observed and cadence is not None:
            elapsed = observed_at - pd.Timestamp(previous_observed)
            missed_observation_gap = elapsed > cadence * 1.5
        # Idempotence is relative to the latest recorded snapshot as well as
        # the last good checkpoint.  A partial observation may have followed
        # a good checkpoint; replaying that same good snapshot must append a
        # recovery record instead of returning the stale data_ready=false row.
        if (latest_snapshot is not None
                and latest_snapshot.get("data_fingerprint") == data_fp
                and latest_snapshot.get("candle_through_utc") == incoming_through
                and bool(latest_snapshot.get("data_ready")) == data_ready
                and not missed_observation_gap):
            # Same immutable snapshot: no ledger or checkpoint writes. This is
            # deliberately byte-for-byte idempotent; a new candle or an
            # explicit partial observation advances the observation watermark.
            # Retrying the outbox here is side-effect free for the files and
            # lets a recovered database catch up without requiring restart.
            self._sync_database_outbox()
            return self.summary(data_fingerprint=data_fp, data_ready=data_ready,
                                variants=self._checkpoint.get("variant_summaries", {}))

        observed_through = _latest_observed_candle(normalized)
        observed_value = _utc_iso(observed_through) if observed_through is not None else None
        forward_from = self._checkpoint.get("forward_from_utc")
        if forward_from is None and observed_value is not None:
            # The first observation establishes the live paper boundary. Its
            # earlier candles are indicator warmup only and never create trades.
            forward_from = observed_value
        replay_boundary = self._checkpoint.get("replay_from_utc") or forward_from
        if missed_observation_gap and observed_value is not None:
            # Do not convert downtime into a burst of historical paper orders.
            # Reset the replay segment at the newest observed candle; the next
            # fresh update can proceed from there.
            replay_boundary = observed_value
        if not data_ready and observed_value is not None:
            old_boundary = replay_boundary
            if (old_boundary is None
                    or pd.Timestamp(observed_value) > pd.Timestamp(old_boundary)):
                replay_boundary = observed_value
        replay_start = self._next_candle_after(normalized, replay_boundary)

        new_decisions = new_fills = 0
        replay_completed = False
        summaries: dict[str, Any] = {}
        if data_ready and replay_start is not None:
            for variant in self.variants:
                raw_events: list[dict[str, Any]] = []
                initial_cash, initial_positions = self._engine_state_at(
                    variant["variant_id"], replay_boundary, normalized
                )
                trades, equity, _, diag = engine.run_v2(
                    normalized, top_n=self.rules["top_n"], max_open=self.rules["max_open"],
                    exposure=self.rules["exposure"], cost_one=self.rules["cost_one"],
                    wallet=self.rules["wallet"], initial_cash=initial_cash,
                    initial_positions=initial_positions,
                    chan_entry=variant["chan_entry"], chan_exit=variant["chan_exit"],
                    start=replay_start,
                    event_sink=raw_events)
                # The boundary candle is signal history, not a new actionable
                # observation.  Only append decisions/fills strictly after it.
                raw_events = [event for event in raw_events
                              if self._after_replay_watermark(event, replay_boundary)]
                for event in raw_events:
                    event["_variant_id"] = variant["variant_id"]
                added_decisions, added_fills = self._append_events(raw_events, data_fp)
                new_decisions += added_decisions
                new_fills += added_fills
                cumulative_exits = [row for row in self._fill_rows
                                    if row.get("variant_id") == variant["variant_id"]
                                    and row.get("action") == "exit"]
                realized = (float(sum(float(row.get("profit_abs", 0.0))
                                      for row in cumulative_exits))
                            if cumulative_exits else None)
                open_count = sum(1 for key in self._open_positions
                                 if key[0] == variant["variant_id"])
                summaries[variant["variant_id"]] = {
                    "closed_trade_count": int(len(cumulative_exits)),
                    "realized_profit_after_fee_before_unknown_costs": realized,
                    "unrealized_pnl_before_unknown_costs": self._unrealized_pnl(
                        normalized, diag
                    ),
                    "slippage": "unknown", "funding": "unknown", "net_profit": None,
                    "ending_equity_marked": float(equity.iloc[-1]) if not equity.empty else None,
                    "open_position_count": int(open_count),
                    "strategy_usable": True,
                    "summary_scope_start_utc": replay_boundary,
                    "diagnostics": diag,
                }
            replay_completed = True
        elif data_ready:
            # A frozen boundary without enough timestamps to identify the next
            # candle cannot safely fall back to an unbounded historical replay.
            # Keep the last successful cumulative summary visible while making
            # the deferred state explicit.  A ready snapshot alone is not an
            # executable replay.
            summaries = self._deferred_variant_summaries(
                replay_boundary, "next_candle_unavailable"
            )

        recovery_of = None
        if (latest_snapshot is not None
                and bool(latest_snapshot.get("data_ready")) != data_ready):
            recovery_of = latest_snapshot.get("event_id")
        snapshot = self._snapshot_event(data=data, normalized=normalized, missing=missing,
                                        data_ready=data_ready, through=through,
                                        data_fp=data_fp, new_decisions=new_decisions,
                                        new_fills=new_fills, recovery_of=recovery_of,
                                        observed_at_utc=observed_at_value,
                                        replay_from_utc=replay_boundary,
                                        replay_completed=replay_completed,
                                        blocked_until_utc=(replay_boundary if not data_ready
                                                           else self._checkpoint.get(
                                                               "blocked_until_utc")))
        snapshot_id = snapshot["event_id"]
        if snapshot_id not in {row.get("event_id") for row in self._manifest_rows}:
            _append_jsonl(self.manifest_path, [snapshot])
            self._manifest_rows.append(snapshot)
        if data_ready:
            prefix = _prefix_fingerprints(normalized, through)
            checkpoint = {
                "schema_version": SCHEMA_VERSION, "run_id": self.run_id,
                "symbols": sorted(normalized), "data_fingerprint": data_fp,
                "candle_through_utc": _utc_iso(through), "prefix_fingerprints": prefix,
                "data_ready": True,
                "replay_completed": bool(replay_completed),
                "last_observed_at_utc": observed_at_value,
                "cadence_seconds": (cadence.total_seconds() if cadence is not None
                                    else self._checkpoint.get("cadence_seconds")),
                "forward_from_utc": forward_from,
                "replay_from_utc": replay_boundary,
                "frozen_prefixes": self._frozen_prefixes(
                    normalized, self._checkpoint.get("frozen_prefixes")
                ),
                "decision_count": len(self._decision_rows), "fill_count": len(self._fill_rows),
                "variant_summaries": summaries,
                "database_ledger": self._database_ledger_state(),
            }
            # Keep the irreversible boundary created by an earlier incomplete
            # snapshot. A recovery checkpoint must not make those historical
            # candles actionable again after restart.
            if self._checkpoint.get("blocked_until_utc") is not None:
                checkpoint["blocked_until_utc"] = self._checkpoint["blocked_until_utc"]
            checkpoint["history_gap_count"] = self._checkpoint.get("history_gap_count", 0)
            if missed_observation_gap:
                checkpoint["history_gap_count"] += 1
                checkpoint["continuity_lost"] = True
            _write_json_atomic(self.checkpoint_path, checkpoint)
            self._checkpoint = checkpoint
        elif expected_symbols:
            # Preserve the last known-good prefix checkpoint while recording
            # the universe seen in an incomplete observation.  This keeps
            # prefix tamper detection active across temporary data outages.
            checkpoint = dict(self._checkpoint)
            checkpoint.update({
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "symbols": sorted(expected_symbols),
                "forward_from_utc": forward_from,
                "data_ready": False,
                "replay_completed": False,
                "last_observed_at_utc": observed_at_value,
            })
            if cadence is not None:
                checkpoint["cadence_seconds"] = cadence.total_seconds()
            if replay_boundary is not None:
                checkpoint["blocked_until_utc"] = replay_boundary
                checkpoint["replay_from_utc"] = replay_boundary
            checkpoint["frozen_prefixes"] = self._frozen_prefixes(
                normalized, checkpoint.get("frozen_prefixes")
            )
            if observed_value is not None:
                checkpoint["history_gap_count"] = int(checkpoint.get("history_gap_count", 0)) + 1
            _write_json_atomic(self.checkpoint_path, checkpoint)
            self._checkpoint = checkpoint
        self._sync_database_outbox()
        if self._checkpoint:
            self._checkpoint["database_ledger"] = self._database_ledger_state()
            _write_json_atomic(self.checkpoint_path, self._checkpoint)
        return self.summary(data_fingerprint=data_fp, data_ready=data_ready,
                            new_decisions=new_decisions, new_fills=new_fills,
                            variants=summaries, replay_completed=replay_completed)

    # ``process`` is the name used by the first forward-runner integration.
    # Keep it as a deliberate alias so callers can migrate without creating a
    # second execution path.
    def process(self, data: dict[str, pd.DataFrame]) -> dict[str, Any]:
        return self.update(data)

    @property
    def state(self) -> dict[str, Any]:
        """Return a read-only-style snapshot of restart state and ledger counts."""
        return {
            **dict(self._checkpoint),
            "run_id": self.run_id,
            "decision_count": len(self._decision_rows),
            "fill_count": len(self._fill_rows),
        }

    def summary(self, *, data_fingerprint: str | None = None,
                data_ready: bool | None = None, new_decisions: int = 0,
                new_fills: int = 0, variants: dict[str, Any] | None = None,
                replay_completed: bool | None = None) -> dict[str, Any]:
        latest = next(
            (row for row in reversed(self._manifest_rows)
             if row.get("event_type") == "snapshot"),
            None,
        )
        variant_payload = variants
        if variant_payload is None or (not variant_payload and not data_ready):
            variant_payload = self._checkpoint.get("variant_summaries", {})
        resolved_ready = data_ready
        if resolved_ready is None:
            resolved_ready = (latest or {}).get(
                "data_ready", self._checkpoint.get("data_ready", False)
            )
        resolved_through = ((latest or {}).get("candle_through_utc")
                            if latest is not None
                            else self._checkpoint.get("candle_through_utc"))
        resolved_fingerprint = ((latest or {}).get("data_fingerprint")
                                if latest is not None
                                else self._checkpoint.get("data_fingerprint"))
        resolved_replay = replay_completed
        if resolved_replay is None:
            resolved_replay = (latest or {}).get(
                "replay_completed", self._checkpoint.get("replay_completed", False)
            )
        if variants is None or (not variants and not data_ready):
            variant_payload = self._checkpoint.get("variant_summaries", {})
        variant_payload = dict(variant_payload or {})
        if not resolved_ready or not resolved_replay:
            variant_payload = {
                key: {**value, "strategy_usable": False}
                for key, value in variant_payload.items()
            }
        return {
            "schema_version": SCHEMA_VERSION, "run_id": self.run_id,
            "data_fingerprint": data_fingerprint if data_fingerprint is not None
            else resolved_fingerprint,
            "data_ready": bool(resolved_ready),
            "replay_completed": bool(resolved_replay),
            "candle_through_utc": resolved_through,
            "decision_count": len(self._decision_rows), "fill_count": len(self._fill_rows),
            "new_decision_count": new_decisions, "new_fill_count": new_fills,
            "variants": variant_payload or {},
            "strategy_usable": bool(resolved_ready and resolved_replay),
            "strategy_unusable_reason": (None if resolved_ready and resolved_replay else
                "data_not_ready_or_universe_incomplete" if not resolved_ready else
                "replay_deferred"),
            "database_ledger": self._database_ledger_state(),
            "cost_completeness": {"fee": "modeled", "slippage": "unknown", "funding": "unknown"},
        }

    @staticmethod
    def _unrealized_pnl(data: dict[str, pd.DataFrame], diag: dict[str, Any]) -> float | None:
        """Mark open paper positions using the latest known close.

        Funding and slippage remain unknown and are deliberately excluded from
        this field.  A missing mark yields ``None`` instead of silently
        treating an unpriced position as zero.
        """
        positions = diag.get("open_positions", [])
        if not positions:
            return 0.0
        by_coin = {str(symbol).split("/")[0]: frame for symbol, frame in data.items()}
        total = 0.0
        for position in positions:
            coin = str(position.get("pair", "")).split("/")[0]
            frame = by_coin.get(coin)
            if frame is None:
                return None
            closes = frame["close"].dropna()
            if closes.empty or not math.isfinite(float(position.get("open_rate", math.nan))):
                return None
            latest = float(closes.iloc[-1])
            open_rate = float(position["open_rate"])
            if open_rate <= 0 or not math.isfinite(latest) or latest <= 0:
                return None
            sign = -1.0 if bool(position.get("is_short")) else 1.0
            total += float(position.get("stake", 0.0)) * sign * (latest / open_rate - 1.0)
        return float(total)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="incremental forward paper replay")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--coins", default="")
    args = parser.parse_args(argv)
    data = paper.load_feather_1h(args.data_dir,
                                 [x.strip() for x in args.coins.split(",") if x.strip()] or None)
    # Keep database setup lazy and best-effort.  The local JSONL outbox remains
    # usable when PostgreSQL or its optional dependencies are unavailable.
    from data_store import DataStore

    project_root = Path(__file__).resolve().parents[1]
    store = DataStore(root=project_root)
    try:
        runner = ForwardPaperRunner(args.output, paper.DEFAULT_VARIANTS,
                                    ledger_store=store)
        print(json.dumps(runner.update(data), ensure_ascii=False, indent=2,
                         allow_nan=False))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
