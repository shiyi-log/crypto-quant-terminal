"""Mature immutable order-flow surge observations against archived candles.

The online detector records a fact at the close of a candle.  This module is
the delayed, read-only part of that pipeline: it waits until the required
future 5m candles are closed, calculates the forward return and excursion
metrics, and appends a maturity event.  It never mutates the original
observation payload.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import threading
import time
from typing import Any, Iterable, Mapping, Sequence

from price_spike import mature_price_spike_observations


FUTURE_TIMEFRAME = "5m"
FUTURE_INTERVAL_MS = 5 * 60 * 1000
DEFAULT_HORIZONS = ("5m", "30m", "1h", "4h")
HORIZON_CANDLES = {"5m": 1, "30m": 6, "1h": 12, "4h": 48}
MATURITY_METHOD_VERSION = "orderflow-volume-surge-maturity-v1"
LOG = logging.getLogger("orderflow_maturity")


def _json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), default=_json_default,
                      allow_nan=False)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _finite_number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{name} must be a finite number")
    return result


def _timestamp(value: Any, name: str = "timestamp") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    try:
        exact = float(value) == result
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if result < 0 or not exact:
        raise ValueError(f"{name} must be a non-negative integer")
    return result


def horizon_candles(horizon: str) -> int:
    """Return the number of 5m candles required for a named horizon."""
    try:
        return HORIZON_CANDLES[str(horizon)]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unsupported maturity horizon: {horizon!r}") from exc


def _nested_observation(observation: Mapping[str, Any]) -> Mapping[str, Any]:
    """Find the detector payload when a database row wraps it several times."""
    for key in ("observation", "payload"):
        value = observation.get(key)
        if isinstance(value, Mapping):
            nested = value.get("observation") if isinstance(value.get("observation"), Mapping) else value
            if isinstance(nested, Mapping):
                return nested
    return observation


def _observation_value(observation: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = observation.get(key)
    if value is not None:
        return value
    nested = _nested_observation(observation)
    return nested.get(key, default)


def _candle_closed(candle: Mapping[str, Any]) -> bool:
    if "closed" in candle:
        if candle.get("closed") is not True:
            return False
    extras = candle.get("extras")
    if isinstance(extras, Mapping) and "closed" in extras:
        if extras.get("closed") is not True:
            return False
    # REST/imported archives may predate the marker or contain the current
    # open candle.  Its entire 5m interval must have elapsed before use.
    try:
        return (_timestamp(candle.get("timestamp")) + FUTURE_INTERVAL_MS
                <= int(time.time() * 1000))
    except ValueError:
        return False


def _candle_projection(candle: Mapping[str, Any]) -> dict[str, Any]:
    """Keep market facts in the fingerprint and omit volatile receive times."""
    projection: dict[str, Any] = {}
    for key in ("timestamp", "open", "high", "low", "close", "volume",
                "quote_volume", "num_trades", "taker_buy_base",
                "taker_buy_quote", "flow_imbal"):
        if key in candle:
            projection[key] = candle[key]
    extras = candle.get("extras")
    if isinstance(extras, Mapping):
        stable = {
            key: extras[key] for key in (
                "closed", "quote_volume", "num_trades", "taker_buy_base",
                "taker_buy_quote", "flow_imbal",
            ) if key in extras
        }
        if stable:
            projection["extras"] = stable
    return projection


def candle_fingerprint(candles: Sequence[Mapping[str, Any]]) -> str:
    """Return a deterministic fingerprint for the future candle window."""
    return _fingerprint([_candle_projection(candle) for candle in candles])


def _entry_close(observation: Mapping[str, Any]) -> float:
    for key in ("entry_close", "close", "entry_price"):
        value = _observation_value(observation, key)
        if value is not None:
            return _finite_number(value, key, positive=True)
    raise ValueError("surge observation must contain the entry close")


def _future_window(observation: Mapping[str, Any], candles: Iterable[Mapping[str, Any]], count: int) -> list[dict[str, Any]] | None:
    timestamp = _timestamp(_observation_value(observation, "timestamp"))
    normalized: list[dict[str, Any]] = []
    for candle in candles:
        if not isinstance(candle, Mapping):
            continue
        try:
            candle_timestamp = _timestamp(candle.get("timestamp"))
        except ValueError:
            continue
        if candle_timestamp <= timestamp or not _candle_closed(candle):
            continue
        row = dict(candle)
        row["timestamp"] = candle_timestamp
        normalized.append(row)
    normalized.sort(key=lambda row: row["timestamp"])
    expected = timestamp + FUTURE_INTERVAL_MS
    result: list[dict[str, Any]] = []
    for row in normalized:
        if row["timestamp"] < expected:
            continue
        if row["timestamp"] != expected:
            # A missing candle makes the horizon unknown, even if a later
            # candle is available in the archive.
            return None
        try:
            open_price = _finite_number(row.get("open"), "future open", positive=True)
            close = _finite_number(row.get("close"), "future close", positive=True)
            high = _finite_number(row.get("high"), "future high", positive=True)
            low = _finite_number(row.get("low"), "future low", positive=True)
        except ValueError:
            return None
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            return None
        result.append(row)
        expected += FUTURE_INTERVAL_MS
        if len(result) == count:
            return result
    return None


def calculate_maturity(
    observation: Mapping[str, Any],
    candles: Iterable[Mapping[str, Any]],
    horizon: str,
    *,
    method_version: str = MATURITY_METHOD_VERSION,
) -> dict[str, Any] | None:
    """Calculate one maturity result, or ``None`` when its window is unknown.

    ``candles`` may contain the observation candle and rows in either order.
    A complete window must contain every consecutive closed 5m candle after
    the observation; this prevents a sparse archive from looking mature.
    """
    if not isinstance(observation, Mapping):
        raise ValueError("observation must be a mapping")
    horizon = str(horizon)
    count = horizon_candles(horizon)
    entry_timestamp = _timestamp(_observation_value(observation, "timestamp"))
    entry_close = _entry_close(observation)
    window = _future_window(observation, candles, count)
    if window is None:
        return None
    closes = [_finite_number(row["close"], "future close", positive=True) for row in window]
    highs = [_finite_number(row["high"], "future high", positive=True) for row in window]
    lows = [_finite_number(row["low"], "future low", positive=True) for row in window]
    result = {
        "observation_id": str(_observation_value(observation, "event_id", _observation_value(observation, "id", ""))),
        "pair": str(_observation_value(observation, "pair", "")),
        "horizon": horizon,
        "entry_timestamp": entry_timestamp,
        "entry_price": entry_close,
        "maturity_timestamp": window[-1]["timestamp"],
        "future_close": closes[-1],
        "return": closes[-1] / entry_close - 1.0,
        "mfe": max(highs) / entry_close - 1.0,
        "mae": min(lows) / entry_close - 1.0,
        "candle_fingerprint": candle_fingerprint(window),
        "method_version": str(method_version),
        "status": "matured",
    }
    return result


# Names used by callers that prefer the terminology in the research notes.
compute_maturity = calculate_maturity
calculate_surge_maturity = calculate_maturity


def _maturity_event_id(observation_id: str, horizon: str, fingerprint: str,
                       method_version: str) -> str:
    return "orderflow-maturity:" + _fingerprint({
        "observation_id": observation_id,
        "horizon": horizon,
        "candle_fingerprint": fingerprint,
        "method_version": method_version,
    })


def _append_event(store: Any, event: dict[str, Any], result: dict[str, Any]) -> Any:
    """Use the append-only API exposed by either current datastore variant."""
    append_result = getattr(store, "append_orderflow_surge_result", None)
    if callable(append_result):
        return append_result(event["observation_id"], result,
                            revision_id=event["event_id"],
                            method_version=event["method_version"],
                            data_fingerprint=event["data_fingerprint"],
                            event_type=event["event_type"],
                            market=event["market"], pair=event["pair"],
                            timeframe=event["timeframe"],
                            timestamp=event["timestamp"],
                            observed_at=event.get("observed_at"),
                            observation=event["observation"])
    append_revision = getattr(store, "append_orderflow_surge", None)
    if callable(append_revision):
        revision = copy.deepcopy(event)
        revision["event_id"] = event["observation_id"]
        revision["revision_id"] = event["event_id"]
        return append_revision(revision)
    append = getattr(store, "append_surge_event", None)
    if callable(append):
        return append(event)
    raise TypeError("store does not expose an order-flow append API")


def _known_revision_ids(store: Any, row: Mapping[str, Any], observation_id: str) -> set[str]:
    """Avoid rewriting unchanged results when the store exposes its history."""
    revisions = row.get("revisions")
    rows = list(revisions) if isinstance(revisions, (list, tuple)) else []
    method = getattr(store, "list_surge_events", None)
    if callable(method):
        rows.extend(method(observation_id=observation_id, limit=None) or [])
    known: set[str] = set()
    for revision in rows:
        if not isinstance(revision, Mapping):
            continue
        identifier = revision.get("revision_id") or revision.get("event_id")
        if identifier is not None:
            known.add(str(identifier))
    return known


def _set_status(store: Any, observation_id: str, status: str) -> None:
    for name in ("project_surge_status", "update_surge_observation_status", "set_surge_status"):
        method = getattr(store, name, None)
        if callable(method):
            method(observation_id, status)
            return


def _list_observations(store: Any, limit: int | None) -> list[dict[str, Any]]:
    reader = getattr(store, "read_orderflow_surges", None)
    if callable(reader):
        # The compatibility list API defaults to futures.  The canonical
        # reader can revisit pending observations from both market archives.
        rows = reader(market=None, timeframe=FUTURE_TIMEFRAME, status=None,
                      limit=limit, include_revisions=False)
        return [dict(row) for row in (rows or []) if isinstance(row, Mapping)]
    method = getattr(store, "list_surge_observations", None)
    if not callable(method):
        raise TypeError("store does not expose list_surge_observations")
    try:
        rows = method(status=None, limit=limit)
    except TypeError:
        rows = method(limit=limit)
    return [dict(row) for row in (rows or []) if isinstance(row, Mapping)]


def mature_surge_observations(
    store: Any,
    *,
    exchange: str = "binance",
    candle_type: str | None = None,
    horizons: Sequence[str] = DEFAULT_HORIZONS,
    limit: int | None = 100,
    method_version: str = MATURITY_METHOD_VERSION,
) -> list[dict[str, Any]]:
    """Mature archived observations and append any newly discovered events.

    Re-running this function is idempotent for unchanged candles.  If a closed
    future candle is revised, its changed fingerprint gives the new event a
    different ID while preserving the previous event.
    """
    selected = tuple(dict.fromkeys(str(value) for value in horizons))
    for horizon in selected:
        horizon_candles(horizon)
    observations = _list_observations(store, limit)
    appended: list[dict[str, Any]] = []
    for row in observations:
        observation_id = _observation_value(row, "event_id", _observation_value(row, "id"))
        if observation_id is None:
            continue
        observation_id = str(observation_id)
        known_revisions = _known_revision_ids(store, row, observation_id)
        timestamp_value = _observation_value(row, "timestamp")
        pair = _observation_value(row, "pair")
        if timestamp_value is None or not pair:
            continue
        market = str(_observation_value(row, "market", "futures"))
        candle_market = candle_type or str(_observation_value(row, "candle_type", market))
        try:
            timestamp = _timestamp(timestamp_value)
        except ValueError:
            continue
        max_horizon_ms = max((horizon_candles(h) for h in selected), default=0) * FUTURE_INTERVAL_MS
        if not selected:
            continue
        candle_args = (exchange, market, str(pair), FUTURE_TIMEFRAME)
        candle_kwargs = {
            "candle_type": candle_market,
            "limit": max(200, max_horizon_ms // FUTURE_INTERVAL_MS + 2),
            "start": timestamp,
            "end": timestamp + max_horizon_ms,
        }
        try:
            candles = store.get_candles(*candle_args, **candle_kwargs)
        except TypeError:
            # Small test doubles and older stores may expose only the original
            # four positional arguments; their default candle type is futures.
            candles = store.get_candles(*candle_args, limit=candle_kwargs["limit"],
                                        start=timestamp,
                                        end=timestamp + max_horizon_ms)
        matured_count = 0
        for horizon in selected:
            result = calculate_maturity(row, candles, horizon, method_version=method_version)
            if result is None:
                continue
            matured_count += 1
            fingerprint = result["candle_fingerprint"]
            event_id = _maturity_event_id(observation_id, horizon, fingerprint, method_version)
            if event_id in known_revisions:
                continue
            payload = {
                "observation_id": observation_id,
                "pair": str(pair),
                "market": market,
                "horizon": horizon,
                "entry_timestamp": result["entry_timestamp"],
                "entry_price": result["entry_price"],
                "maturity_timestamp": result["maturity_timestamp"],
                "return": result["return"],
                "mae": result["mae"],
                "mfe": result["mfe"],
                "future_close": result["future_close"],
                "candle_fingerprint": fingerprint,
                "method_version": method_version,
                "status": "matured",
            }
            # The revision store uses the original detector metadata to bind a
            # maturity row to its immutable observation.  The maturity
            # algorithm version and future-window fingerprint remain in the
            # result, where they can change without rewriting that identity.
            observation_method = str(_observation_value(row, "method_version", method_version))
            observation_fingerprint = _observation_value(row, "data_fingerprint") or fingerprint
            event = {
                "event_id": event_id,
                "revision_id": event_id,
                "observation_id": observation_id,
                "event_type": "orderflow_volume_surge_matured",
                "revision_kind": "maturity",
                "status": "matured",
                "method_version": observation_method,
                "data_fingerprint": str(observation_fingerprint),
                "market": market,
                "pair": str(pair),
                "timeframe": str(_observation_value(row, "timeframe", "5m")),
                "timestamp": timestamp,
                "observed_at": _observation_value(row, "observed_at"),
                "observation": copy.deepcopy(dict(_nested_observation(row))),
                "result": result,
                "payload": payload,
            }
            stored = _append_event(store, event, result)
            # ``append_surge_event`` returns False for an exact retry.  The
            # revision-oriented APIs return an ID for both insert and retry,
            # so their idempotency remains a storage concern.
            if stored is not False:
                appended.append(event)
        if selected and matured_count == len(selected):
            _set_status(store, observation_id, "matured")
    return appended


class OrderflowMaturityWorker:
    """Run archived-candle maturity scans on one background thread.

    The collector owns start/close.  Store creation and database work happen in
    this thread, never in the market receive loop.  Every scan revisits all
    observations so old pending windows and revised future candles are not
    starved by a newest-only page.
    """

    def __init__(self, store_factory: Any, *, interval: float = 30.0,
                 horizons: Sequence[str] = DEFAULT_HORIZONS,
                 exchange: str = "binance", candle_type: str | None = None):
        self.interval = float(interval)
        if not math.isfinite(self.interval) or self.interval <= 0:
            raise ValueError("maturity interval must be positive and finite")
        if not callable(store_factory):
            raise TypeError("store_factory must be callable")
        self._store_factory = store_factory
        self._horizons = tuple(horizons)
        for horizon in self._horizons:
            horizon_candles(horizon)
        self._exchange, self._candle_type = exchange, candle_type
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._notified = False
        self._scans = 0
        self._appended = 0
        self._price_appended = 0
        self._failures = 0
        self._last_error: str | None = None
        self._last_scan_at: float | None = None

    def start(self) -> None:
        with self._condition:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run,
                                            name="orderflow-maturity", daemon=True)
            self._thread.start()

    def notify(self) -> None:
        """Request a scan after a committed candle batch without blocking."""
        with self._condition:
            if not self._closed:
                self._notified = True
                self._condition.notify_all()

    @property
    def status(self) -> dict[str, Any]:
        with self._condition:
            return {"running": self._thread is not None and self._thread.is_alive(),
                    "closed": self._closed, "scans": self._scans,
                    "appended": self._appended, "price_appended": self._price_appended,
                    "failures": self._failures,
                    "last_error": self._last_error, "last_scan_at": self._last_scan_at}

    def close(self, timeout: float = 5.0) -> bool:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)))
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        store = None
        try:
            while True:
                with self._condition:
                    if self._closed:
                        return
                    self._notified = False
                try:
                    if store is None:
                        store = self._store_factory()
                    appended = mature_surge_observations(
                        store, exchange=self._exchange, candle_type=self._candle_type,
                        horizons=self._horizons, limit=None)
                    if callable(getattr(type(store), "read_pending_price_candidates", None)):
                        price_appended = mature_price_spike_observations(
                            store, exchange=self._exchange,
                            candle_type=self._candle_type or "futures", limit=None)
                    else:
                        price_appended = []
                except Exception as error:
                    with self._condition:
                        self._failures += 1
                        self._last_error = type(error).__name__
                    # Database exceptions may contain connection credentials.
                    LOG.warning("order-flow maturity scan failed (%s)", type(error).__name__)
                else:
                    with self._condition:
                        self._scans += 1
                        self._appended += len(appended)
                        self._price_appended += len(price_appended)
                        self._last_error = None
                        self._last_scan_at = time.time()
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._notified,
                                             timeout=self.interval)
        finally:
            closer = getattr(store, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as error:
                    with self._condition:
                        self._failures += 1
                        self._last_error = type(error).__name__
                    LOG.warning("order-flow maturity store close failed (%s)",
                                type(error).__name__)


mature_orderflow_surges = mature_surge_observations


__all__ = [
    "DEFAULT_HORIZONS", "FUTURE_INTERVAL_MS", "HORIZON_CANDLES",
    "MATURITY_METHOD_VERSION", "candle_fingerprint", "horizon_candles",
    "calculate_maturity", "compute_maturity", "calculate_surge_maturity",
    "mature_surge_observations", "mature_orderflow_surges",
    "OrderflowMaturityWorker",
]
