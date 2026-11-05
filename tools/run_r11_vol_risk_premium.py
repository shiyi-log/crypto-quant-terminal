#!/usr/bin/env python3
"""R11/T023 BTC/ETH volatility risk-premium synthetic simulation.

This runner only downloads public market observations and computes a historical
synthetic PnL ledger. It never calls a trading endpoint or places an order.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import random
import subprocess
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist, mean, pstdev, stdev
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "research_multi_strategy/R11_VOL_RISK_PREMIUM_T023_20261024.md"
START_MS = 1_609_459_200_000  # 2021-01-01T00:00:00Z requested window
DVOL_START_MS = 1_640_995_200_000  # 2022-01-01T00:00:00Z effective DVOL coverage
END_MS = 1_790_812_800_000  # 2026-10-01T00:00:00Z, exclusive
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
HOLDING_DAYS = 30
HOLDING_HOURS = HOLDING_DAYS * 24
VAR_NOTIONAL = 2500.0
OPTION_NOTIONAL = 2500.0
OPTION_BID_ASK = 0.002
HEDGE_FEE_BPS = 8.0
HEDGE_FEE_RATE = HEDGE_FEE_BPS / 10_000.0
RISK_FREE_RATE = 0.0
BOOTSTRAP_ITERATIONS = 20_000
BOOTSTRAP_SEED = 20261024
BLOCK_DAYS = 30
FAMILY_K = 4
ALPHA = 0.05
DELTA_ECON_BPS = 12.0
OPTION_SPREAD_GRID = (0.002, 0.005, 0.01, 0.02)
HEDGE_FEE_GRID_BPS = (8.0, 16.0)
ASSETS = ("BTC", "ETH")
RUN_ID = "r11-vol-risk-premium-20261024-v1"
UTC = timezone.utc


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_object(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def git_value(args: list[str]) -> str:
    try:
        return subprocess.check_output(args, cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def normalize_timestamp(raw: int | str) -> int:
    value = int(raw)
    if value >= 10**18:
        value //= 1_000_000
    elif value >= 10**15:
        value //= 1_000
    if not 10**11 <= value < 10**14:
        raise ValueError(f"invalid timestamp {raw!r}")
    return value


def finite_positive(value: Any, field: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and positive: {value!r}")
    return result


def _download(url: str, path: Path, requests: list[dict[str, Any]], *, user_agent: str = "r11-research/1.0") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        payload = path.read_bytes()
        cached = True
    else:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=90) as response:
            payload = response.read()
        path.write_bytes(payload)
        cached = False
    requests.append({"url": url, "cache": display_path(path), "sha256": sha256_bytes(payload), "bytes": len(payload), "cached": cached})
    return payload


def _deribit_url(currency: str, start_ms: int, end_ms: int, resolution: int) -> str:
    params = urllib.parse.urlencode({"currency": currency, "start_timestamp": start_ms,
                                     "end_timestamp": end_ms, "resolution": resolution})
    return f"https://www.deribit.com/api/v2/public/get_volatility_index_data?{params}"


def load_deribit_dvol(currency: str, resolution: int, cache: Path, requests: list[dict[str, Any]],
                      dvol_requests: list[dict[str, Any]]) -> dict[int, float]:
    """Fetch Deribit DVOL in <=900-point windows, retaining close values."""
    step = resolution * 1000
    rows: dict[int, float] = {}
    cursor = DVOL_START_MS
    page = 0
    while cursor < END_MS:
        end = min(cursor + step * 899, END_MS - step)
        url = _deribit_url(currency, cursor, end, resolution)
        cache_path = cache / "deribit" / currency / f"dvol-{resolution}-from-{DVOL_START_MS}-{page:04d}.json"
        try:
            payload = _download(url, cache_path, requests)
        except Exception as exc:
            dvol_requests.append({"url": url, "cache": display_path(cache_path), "status": "missing", "error": str(exc)})
            raise RuntimeError(f"Deribit DVOL request failed for {currency} {resolution}: {exc}") from exc
        dvol_requests.append(requests[-1] | {"currency": currency, "resolution": resolution})
        try:
            decoded = json.loads(payload)
            data = decoded["result"]["data"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"malformed Deribit response for {currency} {resolution}") from exc
        if not isinstance(data, list):
            raise ValueError(f"Deribit data is not a list for {currency} {resolution}")
        max_seen = None
        for item in data:
            if not isinstance(item, (list, tuple)) or len(item) < 5:
                raise ValueError(f"malformed DVOL row: {item!r}")
            ts = normalize_timestamp(item[0])
            if not START_MS <= ts < END_MS:
                continue
            close = finite_positive(item[4], "dvol close")
            old = rows.get(ts)
            if old is not None and old != close:
                raise ValueError(f"conflicting DVOL duplicate {currency} {resolution} {ts}")
            rows[ts] = close
            max_seen = ts if max_seen is None else max(max_seen, ts)
        if max_seen is None:
            cursor = end + step
        else:
            cursor = max_seen + step
        page += 1
    expected = set(range(DVOL_START_MS, END_MS, step))
    missing = sorted(expected - set(rows))
    if missing:
        raise RuntimeError(f"Deribit DVOL {currency} resolution={resolution} missing {len(missing)} timestamps; first={missing[:3]}")
    return dict(sorted(rows.items()))


def month_values() -> list[str]:
    result: list[str] = []
    current = datetime.fromtimestamp(START_MS / 1000, tz=UTC)
    end = datetime.fromtimestamp(END_MS / 1000, tz=UTC)
    year, month = current.year, current.month
    while (year, month) < (end.year, end.month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return result


def load_binance_hourly(asset: str, cache: Path, requests: list[dict[str, Any]]) -> dict[int, float]:
    rows: dict[int, float] = {}
    for month in month_values():
        symbol = f"{asset}USDT"
        url = f"https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/1h/{symbol}-1h-{month}.zip"
        path = cache / "binance" / "futures" / symbol / f"{symbol}-1h-{month}.zip"
        try:
            payload = _download(url, path, requests)
        except Exception as exc:
            raise RuntimeError(f"Binance 1h download failed for {asset} {month}: {exc}") from exc
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                files = [name for name in archive.namelist() if not name.endswith("/")]
                if len(files) != 1:
                    raise ValueError(f"expected one CSV in {path.name}, got {files}")
                with archive.open(files[0]) as handle:
                    reader = csv.reader(io.TextIOWrapper(handle, encoding="utf-8"))
                    for values in reader:
                        if not values or not values[0].strip().lstrip("-").isdigit() or len(values) < 5:
                            continue
                        ts = normalize_timestamp(values[0])
                        if not START_MS <= ts < END_MS:
                            continue
                        close = finite_positive(values[4], "Binance close")
                        old = rows.get(ts)
                        if old is not None and old != close:
                            raise ValueError(f"conflicting Binance duplicate {asset} {ts}")
                        rows[ts] = close
        except (zipfile.BadZipFile, OSError, UnicodeError) as exc:
            raise ValueError(f"malformed Binance archive {path}") from exc
    expected = set(range(START_MS, END_MS, HOUR_MS))
    missing = sorted(expected - set(rows))
    if missing:
        raise RuntimeError(f"Binance 1h {asset} missing {len(missing)} timestamps; first={missing[:3]}")
    return dict(sorted(rows.items()))


def fixture_data() -> tuple[dict[str, dict[int, float]], dict[str, dict[int, float]], dict[str, dict[int, float]]]:
    """Small deterministic fixture for tests and offline smoke runs."""
    prices: dict[str, dict[int, float]] = {}
    dvol_1h: dict[str, dict[int, float]] = {}
    dvol_1d: dict[str, dict[int, float]] = {}
    fixture_end = DVOL_START_MS + 90 * DAY_MS
    for asset, base in (("BTC", 30_000.0), ("ETH", 2_000.0)):
        p, dvh, dvd = {}, {}, {}
        for i, ts in enumerate(range(START_MS, fixture_end, HOUR_MS)):
            price = base * math.exp(0.00002 * i + 0.012 * math.sin(i / 37.0))
            vol = 55.0 + 7.0 * math.sin(i / 113.0) + (3.0 if asset == "ETH" else 0.0)
            p[ts] = price
            dvh[ts] = vol
            if ts % DAY_MS == 0:
                dvd[ts] = vol
        prices[asset], dvol_1h[asset], dvol_1d[asset] = p, dvh, dvd
    return prices, dvol_1h, dvol_1d


def norm_cdf(x: float) -> float:
    return NormalDist().cdf(x)


def bs_call_put_delta(spot: float, strike: float, sigma: float, maturity: float, rate: float = RISK_FREE_RATE) -> tuple[float, float, float]:
    spot = finite_positive(spot, "spot")
    strike = finite_positive(strike, "strike")
    sigma = finite_positive(sigma, "sigma")
    if maturity <= 0:
        call = max(spot - strike, 0.0)
        put = max(strike - spot, 0.0)
        delta = 1.0 if spot > strike else (-1.0 if spot < strike else 0.0)
        return call, put, delta
    sqrt_t = math.sqrt(maturity)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * maturity) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    discount = math.exp(-rate * maturity)
    call = spot * norm_cdf(d1) - strike * discount * norm_cdf(d2)
    put = strike * discount * norm_cdf(-d2) - spot * norm_cdf(-d1)
    long_straddle_delta = 2.0 * norm_cdf(d1) - 1.0
    return call, put, long_straddle_delta


def bs_put_delta(spot: float, strike: float, sigma: float, maturity: float, rate: float = RISK_FREE_RATE) -> float:
    if maturity <= 0:
        return -1.0 if spot < strike else 0.0
    sqrt_t = math.sqrt(maturity)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * maturity) / (sigma * sqrt_t)
    return norm_cdf(d1) - 1.0


def _daily_entries(start: int = DVOL_START_MS, end: int = END_MS, frequency: str = "daily") -> list[int]:
    if frequency not in {"daily", "weekly"}:
        raise ValueError("frequency must be daily or weekly")
    result = []
    for ts in range(start, end, DAY_MS):
        if frequency == "weekly" and datetime.fromtimestamp(ts / 1000, tz=UTC).weekday() != 0:
            continue
        result.append(ts)
    return result


def realized_vol(prices: dict[int, float], entry: int, expiry: int) -> float | None:
    previous = prices.get(entry)
    if previous is None:
        return None
    values: list[float] = []
    for ts in range(entry + HOUR_MS, expiry + HOUR_MS, HOUR_MS):
        current = prices.get(ts)
        if current is None:
            return None
        values.append(math.log(current / previous))
        previous = current
    if len(values) < 2:
        return None
    return stdev(values) * math.sqrt(365 * 24)


def simulate_variance(asset: str, prices: dict[int, float], dvol_entry: dict[int, float], frequency: str) -> list[dict[str, Any]]:
    """Use the 1h DVOL close visible at the exact UTC entry timestamp.

    Daily DVOL closes are intentionally not accepted here: a daily bar keyed
    at 00:00 contains information from the end of that UTC day and would
    introduce lookahead into a 00:00 entry.
    """
    rows: list[dict[str, Any]] = []
    for entry in _daily_entries(frequency=frequency):
        expiry = entry + HOLDING_DAYS * DAY_MS
        if expiry >= END_MS:
            continue
        iv_value = dvol_entry.get(entry)
        if iv_value is None or prices.get(entry) is None:
            continue
        rv = realized_vol(prices, entry, expiry)
        if rv is None:
            continue
        sigma_iv = iv_value / 100.0
        gross = VAR_NOTIONAL * (sigma_iv * sigma_iv - rv * rv) * (HOLDING_DAYS / 365.0)
        option_cost = VAR_NOTIONAL * sigma_iv * sigma_iv * (HOLDING_DAYS / 365.0) * OPTION_BID_ASK
        rows.append({
            "method": "variance_swap_proxy", "asset": asset, "entry_ts": entry, "expiry_ts": expiry,
            "entry_date": datetime.fromtimestamp(entry / 1000, tz=UTC).date().isoformat(),
            "iv": sigma_iv, "rv": rv, "premium_received": None, "payoff": None,
            "hedge_pnl": 0.0, "option_cost": option_cost, "hedge_fees": 0.0,
            "gross_pnl": gross, "net_pnl": gross - option_cost,
            "frequency": frequency, "entry_iv_source": "deribit_dvol_1h_close_at_entry",
            "entry_dvol_ts": entry,
        })
    return rows


def simulate_straddle(asset: str, prices: dict[int, float], dvol_entry: dict[int, float], frequency: str,
                      *, tail_protection: bool = False) -> list[dict[str, Any]]:
    """Use the 1h DVOL close visible at the exact UTC entry timestamp."""
    rows: list[dict[str, Any]] = []
    method = "tail_protected_straddle" if tail_protection else "short_atm_straddle"
    for entry in _daily_entries(frequency=frequency):
        expiry = entry + HOLDING_DAYS * DAY_MS
        if expiry >= END_MS:
            continue
        spot = prices.get(entry)
        dvol = dvol_entry.get(entry)
        if spot is None or dvol is None:
            continue
        sigma = finite_positive(dvol / 100.0, "entry IV")
        qty = OPTION_NOTIONAL / spot
        tail_strike = spot * 0.80
        call, put, _ = bs_call_put_delta(spot, spot, sigma, HOLDING_DAYS / 365.0)
        protective_put = 0.0
        if tail_protection:
            _, protective_put, _ = bs_call_put_delta(spot, tail_strike, sigma, HOLDING_DAYS / 365.0)
        premium = (call + put - protective_put) * qty
        option_cost = abs(premium) * OPTION_BID_ASK
        hedge_qty = 0.0
        hedge_fees = 0.0
        hedge_turnover = 0.0
        hedge_pnl = 0.0
        previous_spot = spot
        for day in range(0, HOLDING_DAYS + 1):
            ts = entry + day * DAY_MS
            current = prices.get(ts)
            if current is None:
                hedge_qty = None  # type: ignore[assignment]
                break
            if day > 0:
                hedge_pnl += hedge_qty * (current - previous_spot)
            remaining = max(HOLDING_DAYS - day, 0) / 365.0
            if remaining > 0:
                _, _, long_delta = bs_call_put_delta(current, spot, sigma, remaining)
                target = long_delta * qty
                if tail_protection:
                    target -= bs_put_delta(current, tail_strike, sigma, remaining) * qty
            else:
                target = 0.0
            trade = target - hedge_qty
            hedge_turnover += abs(trade) * current
            hedge_fees += abs(trade) * current * HEDGE_FEE_RATE
            hedge_qty = target
            previous_spot = current
        if hedge_qty is None:
            continue
        terminal = prices[expiry]
        payoff = qty * abs(terminal - spot)
        protective_payoff = qty * max(tail_strike - terminal, 0.0) if tail_protection else 0.0
        payoff -= protective_payoff
        gross = premium - payoff + hedge_pnl
        rows.append({
            "method": method, "asset": asset, "entry_ts": entry, "expiry_ts": expiry,
            "entry_date": datetime.fromtimestamp(entry / 1000, tz=UTC).date().isoformat(),
            "iv": sigma, "rv": None, "quantity": qty, "strike": spot,
            "tail_strike": tail_strike if tail_protection else None,
            "premium_received": premium, "payoff": payoff, "protective_payoff": protective_payoff,
            "hedge_pnl": hedge_pnl, "hedge_turnover": hedge_turnover,
            "option_cost": option_cost, "hedge_fees": hedge_fees,
            "gross_pnl": gross, "net_pnl": gross - option_cost - hedge_fees,
            "frequency": frequency, "entry_iv_source": "deribit_dvol_1h_close_at_entry",
            "entry_dvol_ts": entry,
        })
    return rows


def _terciles(values: list[float]) -> tuple[float, float]:
    ordered = sorted(values)
    return ordered[max(0, int((len(ordered) - 1) / 3))], ordered[min(len(ordered) - 1, int(2 * (len(ordered) - 1) / 3))]


def add_regimes(rows: list[dict[str, Any]]) -> None:
    by_asset: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_asset[row["asset"]].append(float(row["iv"]))
    cuts = {asset: _terciles(values) for asset, values in by_asset.items() if values}
    for row in rows:
        low, high = cuts[row["asset"]]
        row["dvol_regime"] = "low" if row["iv"] <= low else ("high" if row["iv"] >= high else "mid")
        row["year"] = str(datetime.fromtimestamp(row["entry_ts"] / 1000, tz=UTC).year)


def cluster_stats(rows: list[dict[str, Any]], value_key: str = "net_pnl", *, denominator: float = VAR_NOTIONAL,
                  family_k: int = FAMILY_K, seed_offset: int = 0, block_days: int = BLOCK_DAYS) -> dict[str, Any]:
    known = [row for row in rows if row.get(value_key) is not None]
    values = [float(row[value_key]) for row in known]
    n = len(values)
    n_unique = len({int(row["entry_ts"]) for row in known})
    result: dict[str, Any] = {"n": n, "n_unique": n_unique, "value_key": value_key,
                              "denominator_usdt": denominator, "block_days": block_days,
                              "status": "not_estimable"}
    if not values:
        result["reason"] = "no_known_values"
        return result
    observed = mean(values)
    naive_se = stdev(values) / math.sqrt(n) if n > 1 else None
    blocks: dict[int, list[float]] = defaultdict(list)
    for row in known:
        block = int(row["entry_ts"]) // (block_days * DAY_MS)
        blocks[block].append(float(row[value_key]))
    block_values = list(blocks.values())
    block_sums = [sum(block) for block in block_values]
    block_counts = [len(block) for block in block_values]
    block_means = [s / c for s, c in zip(block_sums, block_counts)]
    cluster_se = stdev(block_means) / math.sqrt(len(block_means)) if len(block_means) > 1 else None
    rng = random.Random(BOOTSTRAP_SEED + seed_offset)
    bootstrap: list[float] = []
    null_bootstrap: list[float] = []
    if block_values:
        for _ in range(BOOTSTRAP_ITERATIONS):
            selected = [rng.randrange(len(block_values)) for _ in block_values]
            total_count = sum(block_counts[i] for i in selected)
            sampled_sum = sum(block_sums[i] for i in selected)
            bootstrap_mean = sampled_sum / total_count
            bootstrap.append(bootstrap_mean)
            null_sum = sum(block_sums[i] - observed * block_counts[i] for i in selected)
            null_bootstrap.append(null_sum / total_count)
        bootstrap.sort()
        null_bootstrap.sort()
    alpha_family = ALPHA / family_k
    z = NormalDist().inv_cdf(1 - alpha_family / 2)
    mean_bps = observed / denominator * 10_000.0
    naive_bps = None if naive_se is None else naive_se / denominator * 10_000.0
    cluster_bps = None if cluster_se is None else cluster_se / denominator * 10_000.0
    if bootstrap:
        ci = [bootstrap[int(0.025 * len(bootstrap))], bootstrap[min(len(bootstrap) - 1, int(0.975 * len(bootstrap)))]]
        p = min(1.0, 2.0 * min(sum(v <= 0 for v in bootstrap), sum(v >= 0 for v in bootstrap)) / len(bootstrap))
    else:
        ci = [None, None]
        p = None
    mde_bps = None if cluster_bps is None else (z + 0.84) * cluster_bps
    design_effect = None if naive_se in (None, 0) or cluster_se is None else (cluster_se / naive_se) ** 2
    status = "estimable" if n_unique >= 400 and mde_bps is not None and mde_bps <= DELTA_ECON_BPS else "not_estimable"
    result.update({
        "mean_usdt": observed, "mean_bps": mean_bps, "naive_se_usdt": naive_se,
        "naive_se_bps": naive_bps, "cluster_se_usdt": cluster_se, "cluster_se_bps": cluster_bps,
        "design_effect": design_effect, "ci95_usdt": ci, "ci95_bps": [None if x is None else x / denominator * 10_000 for x in ci],
        "p": p, "mde_bps": mde_bps, "family_alpha": alpha_family,
        "bootstrap_iterations": BOOTSTRAP_ITERATIONS, "bootstrap_block_days": block_days,
        "block_count": len(block_values), "effective_independent_n": (n / design_effect if design_effect else len(block_values)),
        "status": status,
        "reason": None if status == "estimable" else ("n_unique<400" if n_unique < 400 else "mde>delta_econ"),
    })
    return result


def summary_by(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row[key])].append(row)
    for label, group in sorted(groups.items()):
        out[label] = {
            "n": len(group), "n_unique": len({r["entry_ts"] for r in group}),
            "gross_usdt": sum(r["gross_pnl"] for r in group),
            "net_usdt": sum(r["net_pnl"] for r in group),
            "mean_gross_bps": mean([r["gross_pnl"] / VAR_NOTIONAL * 10_000 for r in group]),
            "mean_net_bps": mean([r["net_pnl"] / VAR_NOTIONAL * 10_000 for r in group]),
            "option_cost_usdt": sum((r.get("option_cost") or 0.0) for r in group),
            "hedge_fees_usdt": sum((r.get("hedge_fees") or 0.0) for r in group),
        }
    return out


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def tail_metrics(rows: list[dict[str, Any]], value_key: str = "net_pnl") -> dict[str, Any]:
    values = [float(row[value_key]) for row in rows if row.get(value_key) is not None]
    if not values:
        return {"n": 0, "worst_single_usdt": None, "q05_usdt": None, "q01_usdt": None,
                "cvar95_usdt": None, "cvar99_usdt": None, "max_drawdown_usdt": None,
                "max_drawdown_bps": None, "max_consecutive_losing_months": None}
    q05 = _quantile(values, 0.05)
    q01 = _quantile(values, 0.01)
    cvar95 = mean([v for v in values if v <= q05]) if q05 is not None else None
    cvar99 = mean([v for v in values if v <= q01]) if q01 is not None else None
    ordered = sorted(rows, key=lambda row: int(row["entry_ts"]))
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in ordered:
        cumulative += float(row[value_key])
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    monthly: dict[str, float] = defaultdict(float)
    for row in ordered:
        month = datetime.fromtimestamp(int(row["entry_ts"]) / 1000, tz=UTC).strftime("%Y-%m")
        monthly[month] += float(row[value_key])
    longest = current = 0
    for month in sorted(monthly):
        if monthly[month] < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return {
        "n": len(values), "worst_single_usdt": min(values), "worst_single_bps": min(values) / VAR_NOTIONAL * 10_000,
        "q05_usdt": q05, "q05_bps": None if q05 is None else q05 / VAR_NOTIONAL * 10_000,
        "q01_usdt": q01, "q01_bps": None if q01 is None else q01 / VAR_NOTIONAL * 10_000,
        "cvar95_usdt": cvar95, "cvar95_bps": None if cvar95 is None else cvar95 / VAR_NOTIONAL * 10_000,
        "cvar99_usdt": cvar99, "cvar99_bps": None if cvar99 is None else cvar99 / VAR_NOTIONAL * 10_000,
        "max_drawdown_usdt": max_drawdown, "max_drawdown_bps": max_drawdown / VAR_NOTIONAL * 10_000,
        "max_consecutive_losing_months": longest,
    }


def crisis_summary(rows: list[dict[str, Any]], start_date: str, end_date: str) -> dict[str, Any]:
    selected = [row for row in rows if start_date <= row["entry_date"] <= end_date]
    values = [float(row["net_pnl"]) for row in selected]
    return {"start": start_date, "end": end_date, "n": len(values),
            "net_usdt": sum(values) if values else None,
            "mean_net_bps": (mean(values) / VAR_NOTIONAL * 10_000) if values else None,
            "tail": tail_metrics(selected)}


def sensitivity_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for asset in ASSETS:
        base = [row for row in rows if row["asset"] == asset and row["method"] == "short_atm_straddle"]
        combos: dict[str, Any] = {}
        for spread in OPTION_SPREAD_GRID:
            for fee_bps in HEDGE_FEE_GRID_BPS:
                adjusted = []
                for row in base:
                    item = dict(row)
                    item["net_pnl"] = row["gross_pnl"] - abs(row["premium_received"] or 0.0) * spread - (row.get("hedge_turnover", 0.0) * fee_bps / 10_000.0)
                    adjusted.append(item)
                key = f"spread_{spread:.3f}_fee_{int(fee_bps)}bp"
                combos[key] = {"option_spread": spread, "hedge_fee_bps": fee_bps,
                               "net": cluster_stats(adjusted, "net_pnl", seed_offset=int(spread * 10000) + int(fee_bps), block_days=BLOCK_DAYS),
                               "tail": tail_metrics(adjusted)}
        out[asset] = combos
    return out


def variance_sensitivity_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for asset in ASSETS:
        base = [row for row in rows if row["asset"] == asset and row["method"] == "variance_swap_proxy"]
        combos: dict[str, Any] = {}
        for spread in OPTION_SPREAD_GRID:
            adjusted = []
            for row in base:
                item = dict(row)
                modeled_cost = VAR_NOTIONAL * (row["iv"] ** 2) * (HOLDING_DAYS / 365.0) * spread
                item["net_pnl"] = row["gross_pnl"] - modeled_cost
                adjusted.append(item)
            combos[f"spread_{spread:.3f}"] = {
                "option_spread": spread,
                "net": cluster_stats(adjusted, "net_pnl", seed_offset=int(spread * 10000) + 700, block_days=BLOCK_DAYS),
                "tail": tail_metrics(adjusted),
            }
        out[asset] = combos
    return out


def build_report(all_rows: list[dict[str, Any]], coverage: dict[str, Any], *, mode: str, frequency: str,
                 spec_hash: str, code_hash: str, request_count: int) -> dict[str, Any]:
    add_regimes(all_rows)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in all_rows:
        groups[(row["asset"], row["method"])].append(row)
    statistics: dict[str, Any] = {}
    for (asset, method), rows in sorted(groups.items()):
        key = f"{asset}:{method}"
        block_stats = {}
        for block_days in (30, 60):
            block_stats[f"gross_{block_days}d"] = cluster_stats(rows, "gross_pnl", seed_offset=len(statistics) * 31 + block_days, block_days=block_days)
            block_stats[f"net_{block_days}d"] = cluster_stats(rows, "net_pnl", seed_offset=len(statistics) * 31 + block_days + 1, block_days=block_days)
        statistics[key] = {
            "gross": block_stats["gross_30d"], "net": block_stats["net_30d"],
            "block_stats": block_stats,
            "annual": summary_by(rows, "year"),
            "dvol_regime": summary_by(rows, "dvol_regime"),
            "crisis_windows": {
                "LUNA": crisis_summary(rows, "2022-05-01", "2022-06-30"),
                "FTX": crisis_summary(rows, "2022-11-01", "2022-12-31"),
            },
            "tail": tail_metrics(rows),
            "costs": {
                "option_cost_usdt": sum((r.get("option_cost") or 0.0) for r in rows),
                "hedge_fees_usdt": sum((r.get("hedge_fees") or 0.0) for r in rows),
            },
        }
    primary_straddle = [row for row in all_rows if row["method"] == "short_atm_straddle"]
    return {
        "task": "T023", "run_id": RUN_ID, "strategy_id": "r11_vol_risk_premium_v1",
        "execution_evidence": "unknown", "conclusion": "synthetic_evidence_only",
        "data_quality": {
            "lookahead_fix_applied": True,
            "entry_iv_source": "deribit_dvol_1h_close_at_entry",
            "daily_dvol_close_used_for_entry": False,
            "pre_fix_results": "invalidated",
        },
        "data_mode": mode, "entry_frequency": frequency, "spec_sha256": spec_hash,
        "code_sha256": code_hash, "request_count": request_count,
        "entry_iv_policy": {
            "source": "Deribit DVOL 1h close at exact UTC entry timestamp",
            "resolution_seconds": 3600,
            "daily_close_used_for_entry": False,
            "lookahead_fix": "T023/T024 historical results before this fix are invalid and must not be reused",
        },
        "data_window": {"start_ms": START_MS, "effective_position_start_ms": DVOL_START_MS,
                         "end_exclusive_ms": END_MS, "start": "2021-01-01T00:00:00Z",
                         "effective_position_start": "2022-01-01T00:00:00Z", "end_exclusive": "2026-10-01T00:00:00Z",
                         "pre_dvol_policy": "unknown_excluded"},
        "coverage": coverage, "config": {
            "holding_days": HOLDING_DAYS, "variance_notional_usdt": VAR_NOTIONAL,
            "option_notional_usdt": OPTION_NOTIONAL, "option_bid_ask_rate": OPTION_BID_ASK,
            "option_spread_grid": OPTION_SPREAD_GRID, "perp_hedge_fee_bps_per_trade": HEDGE_FEE_BPS,
            "hedge_fee_grid_bps": HEDGE_FEE_GRID_BPS, "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
            "bootstrap_block_days": [30, 60], "family_tests": FAMILY_K, "delta_econ_bps": DELTA_ECON_BPS,
            "risk_free_rate": RISK_FREE_RATE,
        },
        "statistics": statistics,
        "cost_sensitivity": sensitivity_rows(primary_straddle),
        "variance_cost_sensitivity": variance_sensitivity_rows(all_rows),
    }


def data_coverage(prices: dict[str, dict[int, float]], dvol_1h: dict[str, dict[int, float]], dvol_1d: dict[str, dict[int, float]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for asset in ASSETS:
        def cov(rows: dict[int, float], step: int) -> dict[str, Any]:
            keys = sorted(rows)
            return {"rows": len(keys), "first_timestamp": keys[0] if keys else None,
                    "last_timestamp": keys[-1] if keys else None, "expected_step_ms": step}
        out[asset] = {"binance_1h": cov(prices[asset], HOUR_MS), "dvol_1h": cov(dvol_1h[asset], HOUR_MS),
                      "dvol_1d": cov(dvol_1d[asset], DAY_MS)}
    return out


def run(output: Path, *, fixture: bool = False, frequency: str = "daily") -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    raw_cache = output / "raw_cache"
    requests: list[dict[str, Any]] = []
    dvol_requests: list[dict[str, Any]] = []
    if fixture:
        prices, dvol_1h, dvol_1d = fixture_data()
        mode = "fixture"
    else:
        prices, dvol_1h, dvol_1d = {}, {}, {}
        for asset in ASSETS:
            print(f"loading Deribit {asset} DVOL 1h/1d", flush=True)
            dvol_1h[asset] = load_deribit_dvol(asset, 3600, raw_cache, requests, dvol_requests)
            dvol_1d[asset] = load_deribit_dvol(asset, 86400, raw_cache, requests, dvol_requests)
            print(f"loading Binance {asset} 1h", flush=True)
            prices[asset] = load_binance_hourly(asset, raw_cache, requests)
        mode = "network"
    all_rows: list[dict[str, Any]] = []
    for asset in ASSETS:
        all_rows.extend(simulate_variance(asset, prices[asset], dvol_1h[asset], frequency))
        all_rows.extend(simulate_straddle(asset, prices[asset], dvol_1h[asset], frequency))
        all_rows.extend(simulate_straddle(asset, prices[asset], dvol_1h[asset], frequency, tail_protection=True))
    spec_hash = sha256_file(SPEC_PATH)
    code_hash = sha256_file(Path(__file__))
    report = build_report(all_rows, data_coverage(prices, dvol_1h, dvol_1d), mode=mode,
                          frequency=frequency, spec_hash=spec_hash, code_hash=code_hash,
                          request_count=len(requests))
    if frequency == "daily":
        weekly_rows: list[dict[str, Any]] = []
        for asset in ASSETS:
            weekly_rows.extend(simulate_variance(asset, prices[asset], dvol_1h[asset], "weekly"))
            weekly_rows.extend(simulate_straddle(asset, prices[asset], dvol_1h[asset], "weekly"))
            weekly_rows.extend(simulate_straddle(asset, prices[asset], dvol_1h[asset], "weekly", tail_protection=True))
        weekly_report = build_report(weekly_rows, data_coverage(prices, dvol_1h, dvol_1d), mode=mode,
                                     frequency="weekly", spec_hash=spec_hash, code_hash=code_hash,
                                     request_count=len(requests))
        report["weekly_sensitivity"] = {"entry_frequency": "weekly_monday_00utc",
                                         "statistics": weekly_report["statistics"]}
    report["dvol_requests"] = {"count": len(dvol_requests), "path": "dvol_requests.jsonl"}
    write_jsonl(output / "positions.jsonl", all_rows)
    write_jsonl(output / "dvol_requests.jsonl", dvol_requests)
    write_jsonl(output / "data_requests.jsonl", requests)
    (output / "REPORT.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    md_lines = [
        "# R11 BTC/ETH volatility risk premium (T023)", "",
        f"- run_id: `{RUN_ID}`", f"- execution_evidence: `{report['execution_evidence']}`",
        f"- data_mode: `{mode}`; entry_frequency: `{frequency}`",
        f"- spec_sha256: `{spec_hash}`", f"- code_sha256: `{code_hash}`", "",
        "This is a historical synthetic simulation. Entry IV now uses the Deribit DVOL 1h close at the exact UTC entry timestamp; the daily DVOL close is never used for entry because it contains end-of-day information. The pre-fix T023/T024 results are invalidated. No historical option execution or option order-book evidence is available.", "",
        "| asset | method | n | n_unique | gross bp | net bp | net CI95 bp | p | MDE bp | status |",
        "|---|---|---:|---:|---:|---:|---|---:|---:|---|",
    ]
    for key, stats in report["statistics"].items():
        asset, method = key.split(":", 1)
        net = stats["net"]
        md_lines.append(f"| {asset} | {method} | {net['n']} | {net['n_unique']} | {stats['gross']['mean_bps']:.3f} | {net['mean_bps']:.3f} | [{net['ci95_bps'][0]:.3f}, {net['ci95_bps'][1]:.3f}] | {net['p']:.6g} | {net['mde_bps']:.3f} | {net['status']} |" if net["ci95_bps"][0] is not None else f"| {asset} | {method} | {net['n']} | {net['n_unique']} | {stats['gross'].get('mean_bps')} | {net.get('mean_bps')} | unknown | unknown | unknown | {net['status']} |")
    md_lines += ["", "## Tail metrics (net PnL)", "", "| asset | method | worst bp | q05 bp | q01 bp | CVaR95 bp | CVaR99 bp | max DD bp | losing months |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for key, stats in report["statistics"].items():
        asset, method = key.split(":", 1)
        tail = stats["tail"]
        md_lines.append(f"| {asset} | {method} | {tail['worst_single_bps']:.3f} | {tail['q05_bps']:.3f} | {tail['q01_bps']:.3f} | {tail['cvar95_bps']:.3f} | {tail['cvar99_bps']:.3f} | {tail['max_drawdown_bps']:.3f} | {tail['max_consecutive_losing_months']} |")
    md_lines += ["", "## Stress windows", "", "LUNA = 2022-05-01..2022-06-30 UTC; FTX = 2022-11-01..2022-12-31 UTC. Full per-method values are in REPORT.json.", ""]
    md_lines += ["| asset | method | window | n | mean net bp | net USDT |", "|---|---|---|---:|---:|---:|"]
    for key, stats in report["statistics"].items():
        asset, method = key.split(":", 1)
        for window, item in stats["crisis_windows"].items():
            md_lines.append(f"| {asset} | {method} | {window} | {item['n']} | {item['mean_net_bps'] if item['mean_net_bps'] is not None else 'unknown'} | {item['net_usdt'] if item['net_usdt'] is not None else 'unknown'} |")
    md_lines += ["", "## Cost sensitivity: short ATM straddle", "", "| asset | option spread | hedge fee | net bp | CI95 bp | MDE bp | status |", "|---|---:|---:|---:|---|---:|---|"]
    for asset, combos in report["cost_sensitivity"].items():
        for item in combos.values():
            net = item["net"]
            md_lines.append(f"| {asset} | {item['option_spread']:.3%} | {item['hedge_fee_bps']:.0f} | {net['mean_bps']:.3f} | [{net['ci95_bps'][0]:.3f}, {net['ci95_bps'][1]:.3f}] | {net['mde_bps']:.3f} | {net['status']} |")
    md_lines += ["", "## Cost sensitivity: variance-swap proxy", "", "| asset | option spread | net bp | CI95 bp | MDE bp | status |", "|---|---:|---:|---|---:|---|"]
    for asset, combos in report["variance_cost_sensitivity"].items():
        for item in combos.values():
            net = item["net"]
            md_lines.append(f"| {asset} | {item['option_spread']:.3%} | {net['mean_bps']:.3f} | [{net['ci95_bps'][0]:.3f}, {net['ci95_bps'][1]:.3f}] | {net['mde_bps']:.3f} | {net['status']} |")
    if report.get("weekly_sensitivity"):
        md_lines += ["", "## Weekly Monday entry sensitivity", "", "| asset | method | n | net bp | CI95 bp | p | MDE bp | status |", "|---|---|---:|---:|---|---:|---:|---|"]
        for key, stats in report["weekly_sensitivity"]["statistics"].items():
            asset, method = key.split(":", 1)
            net = stats["net"]
            if net["ci95_bps"][0] is None:
                md_lines.append(f"| {asset} | {method} | {net['n']} | unknown | unknown | unknown | unknown | {net['status']} |")
            else:
                md_lines.append(f"| {asset} | {method} | {net['n']} | {net['mean_bps']:.3f} | [{net['ci95_bps'][0]:.3f}, {net['ci95_bps'][1]:.3f}] | {net['p']:.6g} | {net['mde_bps']:.3f} | {net['status']} |")
    md_lines += ["", "## Cost assumptions", "", "- Option premium bid-ask grid: 0.2%, 0.5%, 1.0%, 2.0% of modeled premium.", "- Perpetual delta-hedge fee grid: 8 bp and 16 bp per hedge trade, including initial hedge and terminal unwind.", "- Variance proxy cost: same option-spread grid applied to modeled implied-variance premium.", "- MDE above 12 bp is reported as not_estimable and is not described as significant profitability.", "", "## Data coverage", "", "```json", json.dumps(report["coverage"], ensure_ascii=False, indent=2), "```", ""]
    (output / "REPORT.md").write_text("\n".join(md_lines))
    reproduce = output / "reproduce.sh"
    reproduce.write_text("#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")/../..\"\npython3 tools/run_r11_vol_risk_premium.py --output artifacts/r11_vol_risk_premium_20261024\n")
    reproduce.chmod(0o755)
    files = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files[display_path(path)] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    manifest = {
        "task": "T023", "run_id": RUN_ID, "strategy_id": "r11_vol_risk_premium_v1",
        "execution_evidence": "unknown", "spec_sha256": spec_hash, "code_sha256": code_hash,
        "git_commit": git_value(["git", "rev-parse", "HEAD"]),
        "git_dirty": bool(git_value(["git", "status", "--porcelain"])),
        "generated_at_utc": datetime.now(UTC).isoformat(), "data_mode": mode,
        "files": files, "data_requests": len(requests), "dvol_requests": len(dvol_requests),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/r11_vol_risk_premium_20261024")
    parser.add_argument("--fixture", action="store_true", help="use deterministic local fixture instead of public downloads")
    parser.add_argument("--entry-frequency", choices=("daily", "weekly"), default="daily")
    args = parser.parse_args()
    report = run(args.output, fixture=args.fixture, frequency=args.entry_frequency)
    for key, stats in report["statistics"].items():
        print(f"{key}: n={stats['net']['n']} n_unique={stats['net']['n_unique']} net_bp={stats['net'].get('mean_bps')} status={stats['net']['status']}")


if __name__ == "__main__":
    main()
