"""完整验证实验族的纯离线近似时间块推断；不认证来源或授予交易资格。"""
from __future__ import annotations

import hashlib
import heapq
import json
import logging
import re
import struct
from fractions import Fraction

from research_protocol import DAY_MS, FrozenResearchEvidence, _family

VERSION = "validation-block-bootstrap-v1"
FULL_VERSION = "validation-block-bootstrap-v2"
FULL_ENGINE = "exact-signed54-counts-v1"
LOGGER = logging.getLogger(__name__)
REPLICATES = 99999
MAX_FAMILY_FOLDS = 128
MAX_DATASET_DAYS = 3660
SCALE = 10**24
SHA = re.compile(r"[a-f0-9]{64}\Z")
REFERENCE = re.compile(r"[A-Za-z0-9_.:@/-]{1,200}\Z")
MONEY = re.compile(r"-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,24})?\Z")
FIXED = {
    "stage": "validation", "metric": "fixed_capital_equity_delta_usdt",
    "block_anchor": "fold_start", "scheme": "nonoverlapping_stratified",
    "capital_policy": "fixed_no_compounding_no_transfers",
    "boundary_policy": "flat_before_and_after_fold_costs_included",
    "method": "time_block_bootstrap", "replicates": REPLICATES,
    "rng": "sha256-counter-rejection-v1", "money_scale": 24,
    "null": "aggregate_mean_le_zero", "tail": "greater_equal",
    "confidence_bound": "bonferroni_basic_rank",
    "min_tail_count": 20, "min_blocks_per_fold": 30,
    "degeneracy": "any_config_fold_constant_blocks_reject_family",
}
LIMITS = {"max_input_rows": 2_000_000, "max_index_draws": 30_000_000,
          "max_weighted_additions": 60_000_000, "max_distribution_cells": 2_000_000}
SPEC_KEYS = set(FIXED) | set(LIMITS) | {
    "initial_capital", "period_ms", "block_ms", "evaluation_as_of_ms",
    "seed", "multiple_testing", "alpha"}
FULL_LIMITS = {"max_input_rows": 2_000_000, "max_index_draws": 30_000_000,
               "max_blocks": 300, "max_native_macs": 10_799_892_000,
               "max_reconstruction_cells": 35_999_640, "max_heap_cells": 5000,
               "max_workspace_bytes": 64_000_000}
FULL_FIXED = {"engine": FULL_ENGINE, "limb_bits": 54, "max_limbs": 3}
FULL_SPEC_KEYS = (SPEC_KEYS - {"max_weighted_additions", "max_distribution_cells"}
                  | set(FULL_LIMITS) | set(FULL_FIXED) | {"batch_size"})
META_KEYS = {"protocol_version", "family_sha256", "family_size", "identities",
             "folds", "inference_registration_verified"}
SERIES_KEYS = {"family_sha256", "inference_sha256", "config_id", "attempt_id",
               "producer_id", "identities", "stage", "state", "execution_sha256",
               "cost_sha256", "equity_sha256", "rows"}
ROW_KEYS = {"fold_id", "start_ms", "end_ms", "available_ms", "complete", "net_pnl"}


class StatisticsError(ValueError):
    """只输出固定错误码，不回传输入内容或上游异常。"""


def _fail(code):
    raise StatisticsError(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _freeze(value):
    text = _json(value)
    return FrozenResearchEvidence(text, hashlib.sha256(text.encode("utf-8")).hexdigest())


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        _fail("identity_invalid")
    return value


def _stamp(value):
    if type(value) is not int or not 0 <= value < 2**63:
        _fail("timestamp_invalid")
    return value


def _reference(value):
    if not isinstance(value, str) or not REFERENCE.fullmatch(value):
        _fail("reference_invalid")
    return value


def _money(value):
    if not isinstance(value, str) or not MONEY.fullmatch(value):
        _fail("money_invalid")
    negative = value.startswith("-")
    whole, _, fraction = value.lstrip("-").partition(".")
    result = int(whole) * SCALE + int(fraction.ljust(24, "0") or "0")
    if negative and result == 0:
        _fail("money_negative_zero")
    return -result if negative else result


def _money_text(value):
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), SCALE)
    suffix = str(fraction).zfill(24).rstrip("0")
    return f"{sign}{whole}" + (f".{suffix}" if suffix else "")


def _fraction(value):
    return {"numerator": value.numerator, "denominator": value.denominator}


def _checked_family(family):
    if (type(family) is not FrozenResearchEvidence
            or type(family.canonical_json) is not str
            or len(family.canonical_json) > 1_000_000
            or type(family.sha256) is not str or not SHA.fullmatch(family.sha256)):
        _fail("family_invalid")
    try:
        value = json.loads(family.canonical_json)
        if not isinstance(value, dict):
            _fail("family_invalid")
        dataset = value.get("dataset")
        folds = value.get("folds")
        if not isinstance(dataset, dict) or not isinstance(folds, list) or len(folds) > MAX_FAMILY_FOLDS:
            _fail("family_invalid")
        start, end = _stamp(dataset.get("start_ms")), _stamp(dataset.get("end_ms"))
        length = end - start
        derived_folds = max(0, (length - 240 * DAY_MS) // (30 * DAY_MS) + 1)
        if not 365 * DAY_MS <= length <= MAX_DATASET_DAYS * DAY_MS or derived_folds > MAX_FAMILY_FOLDS:
            _fail("family_invalid")
        return _family(family)
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        _fail("family_invalid")


def freeze_validation_inference(family, spec):
    """冻结全部数值政策；尚无服务端登记证明，固定保留未认证标记。"""
    frozen = _checked_family(family)
    if not isinstance(spec, dict) or set(spec) not in (SPEC_KEYS, FULL_SPEC_KEYS):
        _fail("policy_fields_invalid")
    full = set(spec) == FULL_SPEC_KEYS
    if spec["method"] != "time_block_bootstrap":
        _fail("unsupported_method")
    for key, expected in FIXED.items():
        if type(spec[key]) is not type(expected) or spec[key] != expected:
            _fail("policy_value_invalid")
    for key, expected in (FULL_FIXED.items() if full else ()):
        if type(spec[key]) is not type(expected) or spec[key] != expected:
            _fail("policy_value_invalid")
    if full and (type(spec["batch_size"]) is not int or not 1 <= spec["batch_size"] <= 1024):
        _fail("policy_value_invalid")
    for key, maximum in (FULL_LIMITS if full else LIMITS).items():
        if type(spec[key]) is not int or not 0 < spec[key] <= maximum:
            _fail("policy_limit_invalid")
    _sha(spec["seed"])
    if _money(spec["initial_capital"]) <= 0:
        _fail("capital_invalid")
    period, block = _stamp(spec["period_ms"]), _stamp(spec["block_ms"])
    comparison = frozen["comparison"]
    if (not period or block < frozen["label_span_ms"] or block % period
            or block != comparison["block_ms"]
            or comparison["method"] != spec["method"]
            or spec["multiple_testing"] != comparison["multiple_testing"]
            or spec["alpha"] != comparison["alpha"]):
        _fail("comparison_mismatch")
    # 概率也只接受精确普通十进制字符串，不能以浮点中转。
    alpha = Fraction(_money(spec["alpha"]), SCALE)
    if not 0 < alpha <= Fraction(1, 20):
        _fail("alpha_invalid")
    if _stamp(spec["evaluation_as_of_ms"]) < frozen["dataset"]["end_ms"]:
        _fail("evaluation_as_of_invalid")
    if not frozen["folds"]:
        _fail("folds_missing")
    for fold in frozen["folds"]:
        length = fold["validation_end_ms"] - fold["validation_start_ms"]
        if length % block or length // block < spec["min_blocks_per_fold"]:
            _fail("fold_blocks_invalid")
    size = frozen["configuration_count"]
    rows = sum((fold["validation_end_ms"] - fold["validation_start_ms"]) // period
               for fold in frozen["folds"]) * size
    if rows > LIMITS["max_input_rows"]:
        _fail("policy_grid_resource_limit")
    if (alpha * (REPLICATES + 1) / size).__floor__() < spec["min_tail_count"]:
        _fail("monte_carlo_resolution_insufficient")
    return _freeze({**spec, "initial_capital": _money_text(_money(spec["initial_capital"])),
                    "protocol_version": FULL_VERSION if full else VERSION, "family_sha256": family.sha256,
                    "family_size": size, "identities": frozen["identities"],
                    "folds": frozen["folds"], "inference_registration_verified": False})


def _checked_inference(family, inference):
    if (type(inference) is not FrozenResearchEvidence
            or type(inference.canonical_json) is not str
            or type(inference.sha256) is not str or not SHA.fullmatch(inference.sha256)):
        _fail("inference_invalid")
    try:
        if (len(inference.canonical_json) > 100_000
                or hashlib.sha256(inference.canonical_json.encode("utf-8")).hexdigest() != inference.sha256):
            _fail("inference_invalid")
        value = json.loads(inference.canonical_json)
        if not isinstance(value, dict):
            _fail("inference_invalid")
        keys = FULL_SPEC_KEYS if value.get("protocol_version") == FULL_VERSION else SPEC_KEYS
        if set(value) != keys | META_KEYS:
            _fail("inference_invalid")
        expected = freeze_validation_inference(family, {key: value[key] for key in keys})
    except (ValueError, KeyError, TypeError, RecursionError, UnicodeError):
        _fail("inference_invalid")
    if expected != inference:
        _fail("inference_invalid")
    return value


def _validated_inputs(family, inference, series):
    frozen, policy = _checked_family(family), _checked_inference(family, inference)
    configurations = frozen["configurations"]
    if not isinstance(series, list) or len(series) != len(configurations):
        _fail("family_incomplete")
    if any(not isinstance(item, dict) or set(item) != SERIES_KEYS for item in series):
        _fail("series_fields_invalid")
    ids = [item["config_id"] for item in series]
    if (any(not isinstance(value, str) for value in ids) or len(set(ids)) != len(ids)
            or set(ids) != {item["config_id"] for item in configurations}):
        _fail("family_configuration_mismatch")
    by_id = {item["config_id"]: item for item in series}
    # 在读取/分配行结构前预检全部规模，不能按剩余配置降低B。
    rows_expected = sum((fold["validation_end_ms"] - fold["validation_start_ms"])
                        // policy["period_ms"] for fold in policy["folds"])
    block_counts = [(fold["validation_end_ms"] - fold["validation_start_ms"])
                    // policy["block_ms"] for fold in policy["folds"]]
    index_draws = REPLICATES * sum(block_counts)
    full = policy["protocol_version"] == FULL_VERSION
    if (rows_expected * len(series) > policy["max_input_rows"]
            or index_draws > policy["max_index_draws"]
            or (full and sum(block_counts) > policy["max_blocks"])
            or (not full and (index_draws * len(series) > policy["max_weighted_additions"]
                              or REPLICATES * len(series) > policy["max_distribution_cells"]))):
        _fail("resource_limit_exceeded")
    block_periods = policy["block_ms"] // policy["period_ms"]
    canonical, blocks, attempts = [], [], set()
    for config in configurations:
        item = by_id[config["config_id"]]
        if (item["family_sha256"] != family.sha256
                or item["inference_sha256"] != inference.sha256
                or item["identities"] != frozen["identities"]
                or item["stage"] != "validation" or item["state"] != "completed"):
            _fail("series_identity_or_state_invalid")
        attempt = _reference(item["attempt_id"])
        if attempt in attempts:
            _fail("attempt_duplicate")
        attempts.add(attempt)
        _reference(item["producer_id"])
        for key in ("execution_sha256", "cost_sha256", "equity_sha256"):
            _sha(item[key])
        rows = item["rows"]
        if not isinstance(rows, list) or len(rows) != rows_expected:
            _fail("series_grid_incomplete")
        cursor, fold_blocks, normalized = 0, [], []
        for ordinal, fold in enumerate(policy["folds"]):
            values = []
            for timestamp in range(fold["validation_start_ms"], fold["validation_end_ms"],
                                   policy["period_ms"]):
                row = rows[cursor]
                cursor += 1
                if not isinstance(row, dict) or set(row) != ROW_KEYS:
                    _fail("period_fields_invalid")
                end = timestamp + policy["period_ms"]
                if (type(row["fold_id"]) is not int or row["fold_id"] != ordinal
                        or _stamp(row["start_ms"]) != timestamp or _stamp(row["end_ms"]) != end
                        or row["complete"] is not True
                        or not end <= _stamp(row["available_ms"]) <= policy["evaluation_as_of_ms"]):
                    _fail("period_grid_or_availability_invalid")
                amount = _money(row["net_pnl"])
                values.append(amount)
                normalized.append({**row, "net_pnl": _money_text(amount)})
            sums = tuple(sum(values[index:index + block_periods])
                         for index in range(0, len(values), block_periods))
            if len(set(sums)) == 1:
                _fail("variance_degenerate")
            fold_blocks.append(sums)
        blocks.append(tuple(fold_blocks))
        canonical.append({**item, "identities": dict(frozen["identities"]), "rows": normalized})
    prepared = {"frozen": frozen, "policy": policy, "series": canonical, "blocks": blocks,
            "block_counts": block_counts, "period_count": rows_expected,
            "index_draws": index_draws, "input_sha256": _freeze(canonical).sha256}
    if full:
        prepared["compute_resources"] = _full_resources(blocks, block_counts, policy)
    return prepared


def _full_resources(blocks, counts, policy):
    """NumPy分配之前证明每个int64中间和安全，并冻结各类操作预算。"""
    size, columns, batch = len(blocks), sum(counts), policy["batch_size"]
    peak = max(abs(value) for config in blocks for fold in config for value in fold)
    limbs = max(1, (peak.bit_length() + 53) // 54)
    if columns * ((1 << 54) - 1) > (1 << 63) - 1 or limbs > policy["max_limbs"]:
        _fail("integer_overflow_bound")
    rank = (Fraction(_money(policy["alpha"]), SCALE) * (REPLICATES + 1) / size).__floor__()
    # 矩阵字节精确；Python批结果/堆按每单元128bytes保守计量，不包含调用方输入。
    matrix_bytes = 8 * (batch * columns + limbs * columns * size + limbs * batch * size)
    workspace = matrix_bytes + 128 * (batch * size + size * rank) + 1_048_576
    usage = {"limbs": limbs, "native_macs": REPLICATES * columns * size * limbs,
             "reconstruction_cells": REPLICATES * size * limbs, "heap_cells": size * rank,
             "matrix_bytes": matrix_bytes, "workspace_bytes": workspace}
    for key in ("native_macs", "reconstruction_cells", "heap_cells", "workspace_bytes"):
        if usage[key] > policy[f"max_{key}"]:
            _fail("resource_limit_exceeded")
    return usage


def _resampled_delta(blocks, draws):
    return sum(sum(fold[index] for index in indexes) - sum(fold)
               for fold, indexes in zip(blocks, draws, strict=True))


def _tail_summary(total, distribution, alpha):
    size = len(distribution)
    rank_count = (alpha * (size + 1)).__floor__()
    if not size or not 1 <= rank_count <= size:
        _fail("monte_carlo_resolution_insufficient")
    count = sum(value >= total for value in distribution)
    rank = size - rank_count + 1
    return {"raw_p": Fraction(count + 1, size + 1),
            "lower_total": total - sorted(distribution)[rank - 1],
            "tail_count": count, "tail_rank": rank}


def _adjust(probabilities, method):
    size = len(probabilities)
    if method == "bonferroni":
        return {key: min(Fraction(1), value * size) for key, value in probabilities.items()}
    if method != "holm":
        _fail("unsupported_correction")
    result, previous = {}, Fraction(0)
    for rank, (key, value) in enumerate(sorted(probabilities.items(), key=lambda pair: (pair[1], pair[0]))):
        previous = min(Fraction(1), max(previous, value * (size - rank)))
        result[key] = previous
    return result


def _draw_index(domain, replicate, fold, draw, count):
    limit = (2**256 // count) * count
    prefix = domain + struct.pack(">QII", replicate, fold, draw)
    for retry in range(64):
        digest = hashlib.sha256(prefix + struct.pack(">I", retry)).digest()
        value = int.from_bytes(digest, "big")
        if value < limit:
            return value % count, retry
    _fail("rng_rejection_limit")


def _bootstrap(blocks, counts, domain):
    totals = [sum(map(sum, item)) for item in blocks]
    distributions = [[] for _ in blocks]
    fingerprint, rejected = hashlib.sha256(), 0
    for replicate in range(REPLICATES):
        selected = []
        for fold, count in enumerate(counts):
            indexes = []
            for draw in range(count):
                index, retry = _draw_index(domain, replicate, fold, draw, count)
                fingerprint.update(struct.pack(">I", index))
                rejected += retry
                indexes.append(index)
            selected.append(indexes)
        for index, item in enumerate(blocks):
            distributions[index].append(_resampled_delta(item, selected))
    return totals, distributions, fingerprint.hexdigest(), rejected


def _signed54_digits(blocks, max_limbs):
    """将有符号块和拆成54位数位；返回 [limb,block,config] 的int64数组。"""
    try:
        import numpy as np
    except ImportError:
        _fail("numpy_unavailable")
    config_values = [tuple(value for fold in config for value in fold) for config in blocks]
    columns = len(config_values[0]) if config_values else 0
    if any(len(values) != columns for values in config_values):
        _fail("block_shape_invalid")
    peak = max((abs(value) for values in config_values for value in values), default=0)
    limbs = max(1, (peak.bit_length() + 53) // 54)
    if limbs > max_limbs:
        _fail("integer_overflow_bound")
    mask = (1 << 54) - 1
    result = np.empty((limbs, columns, len(config_values)), dtype=np.int64)
    for limb in range(limbs):
        shift = limb * 54
        for column in range(columns):
            for config, values in enumerate(config_values):
                value = values[column]
                digit = (abs(value) >> shift) & mask
                result[limb, column, config] = digit if value >= 0 else -digit
    return result


def _signed54_product(counts, digits):
    """用int64矩阵乘法计算signed54数位；调用者已完成中间界证明。"""
    try:
        import numpy as np
    except ImportError:
        _fail("numpy_unavailable")
    if type(counts) is np.ndarray:
        if counts.dtype != np.dtype(np.int64) or counts.ndim != 2:
            _fail("block_counts_invalid")
        matrix = counts
    else:
        if (not isinstance(counts, (list, tuple))
                or any(not isinstance(row, (list, tuple))
                       or any(type(value) is not int or not 0 <= value <= 300 for value in row)
                       for row in counts)):
            _fail("block_counts_invalid")
        matrix = np.asarray(counts, dtype=np.int64)
    if type(digits) is not np.ndarray or digits.dtype != np.dtype(np.int64):
        _fail("block_digits_invalid")
    layers = digits
    if matrix.ndim != 2 or layers.ndim != 3 or layers.shape[1] != matrix.shape[1]:
        _fail("block_shape_invalid")
    # 先逐元素限制再求行和；不能让 NumPy int64 sum 回绕后参与安全证明。
    if matrix.size and (int(matrix.min()) < 0 or int(matrix.max()) > 300):
        _fail("block_counts_invalid")
    row_sums = [sum(int(value) for value in row) for row in matrix]
    if layers.size and (layers.min() < -((1 << 54) - 1) or layers.max() > (1 << 54) - 1):
        _fail("block_digits_invalid")
    if row_sums and min(row_sums) <= 0:
        _fail("block_counts_invalid")
    if row_sums and max(row_sums) * ((1 << 54) - 1) > (1 << 63) - 1:
        _fail("integer_overflow_bound")
    if row_sums and max(row_sums) > 300:
        _fail("block_counts_invalid")
    result = np.empty((layers.shape[0], matrix.shape[0], layers.shape[2]), dtype=np.int64)
    for limb in range(layers.shape[0]):
        np.matmul(matrix, layers[limb], out=result[limb])
    return result


def _stream_tail(values, total, rank, heap):
    """流式维护最大rank个误差和含并列的尾计数，返回尾计数。"""
    if type(rank) is not int or rank <= 0:
        _fail("monte_carlo_resolution_insufficient")
    tail = 0
    for value in values:
        if value >= total:
            tail += 1
        if len(heap) < rank:
            heapq.heappush(heap, value)
        elif value > heap[0]:
            heapq.heapreplace(heap, value)
    return tail


def _count_batch(domain, start, stop, counts, fingerprint):
    """按原索引顺序生成一批共享计数；调用者维护全局摘要顺序。"""
    try:
        import numpy as np
    except ImportError:
        _fail("numpy_unavailable")
    if type(start) is not int or type(stop) is not int or not 0 <= start <= stop:
        _fail("batch_invalid")
    if not isinstance(counts, list) or not counts or any(type(value) is not int or value <= 0 for value in counts):
        _fail("block_counts_invalid")
    columns = sum(counts)
    matrix = np.zeros((stop - start, columns), dtype=np.int64)
    rejected = 0
    for row, replicate in enumerate(range(start, stop)):
        offset = 0
        for fold, count in enumerate(counts):
            for draw in range(count):
                index, retry = _draw_index(domain, replicate, fold, draw, count)
                fingerprint.update(struct.pack(">I", index))
                rejected += retry
                matrix[row, offset + index] += 1
            offset += count
    return matrix, rejected


def _bootstrap_counts(blocks, counts, domain, resources, policy):
    """v2共享计数内核；SHA索引顺序与v1逐索引路径完全相同。"""
    if resources["workspace_bytes"] > policy["max_workspace_bytes"]:
        _fail("resource_limit_exceeded")
    batch = policy["batch_size"]
    configs = len(blocks)
    totals = [sum(map(sum, item)) for item in blocks]
    rank = (Fraction(_money(policy["alpha"]), SCALE) * (REPLICATES + 1) / configs).__floor__()
    heaps, tails = [[] for _ in blocks], [0] * configs
    digits = _signed54_digits(blocks, policy["max_limbs"])
    fingerprint, rejected = hashlib.sha256(), 0
    for start in range(0, REPLICATES, batch):
        stop = min(REPLICATES, start + batch)
        matrix, rejected_batch = _count_batch(domain, start, stop, counts, fingerprint)
        rejected += rejected_batch
        products = _signed54_product(matrix, digits)
        for row in range(stop - start):
            for config in range(configs):
                value = sum(int(products[limb, row, config]) << (54 * limb)
                            for limb in range(products.shape[0])) - totals[config]
                if value >= totals[config]:
                    tails[config] += 1
                heap = heaps[config]
                if len(heap) < rank:
                    heapq.heappush(heap, value)
                elif value > heap[0]:
                    heapq.heapreplace(heap, value)
        del matrix, products
    return totals, heaps, tails, fingerprint.hexdigest(), rejected


def _diagnostic(family, inference, code):
    return _freeze({
        "protocol_version": VERSION, "calculation_status": "not_estimable",
        "status": "not_estimable", "reason_codes": [code], "results": [],
        "family_sha256": family.sha256 if type(family) is FrozenResearchEvidence
        and type(family.sha256) is str and SHA.fullmatch(family.sha256) else None,
        "inference_sha256": inference.sha256 if type(inference) is FrozenResearchEvidence
        and type(inference.sha256) is str and SHA.fullmatch(inference.sha256) else None,
        "source_authenticated": False, "inference_registration_verified": False,
        "acceptance_eligible": False, "production_authorized": False})


def calculate_validation_family(family, inference, series):
    """实际复算完整验证族；任一配置无效时不输出幸存配置或领取留出票。"""
    try:
        prepared = _validated_inputs(family, inference, series)
        spec = prepared["policy"]
        full = spec["protocol_version"] == FULL_VERSION
        LOGGER.info("研究统计输入校验完成 B=%d M=%d index_draws=%d",
                    REPLICATES, spec["family_size"], prepared["index_draws"])
        domain = (b"sha256-counter-rejection-v1\0" + bytes.fromhex(family.sha256)
                  + bytes.fromhex(inference.sha256) + bytes.fromhex(spec["seed"]))
        alpha = Fraction(_money(spec["alpha"]), SCALE)
        if full:
            totals, heaps, tails, digest, rejected = _bootstrap_counts(
                prepared["blocks"], prepared["block_counts"], domain,
                prepared["compute_resources"], spec)
            rank = (alpha * (REPLICATES + 1) / spec["family_size"]).__floor__()
            summaries = [{"raw_p": Fraction(count + 1, REPLICATES + 1),
                          "lower_total": total - heap[0], "tail_count": count,
                          "tail_rank": REPLICATES - rank + 1}
                         for total, heap, count in zip(totals, heaps, tails, strict=True)]
        else:
            totals, distributions, digest, rejected = _bootstrap(
                prepared["blocks"], prepared["block_counts"], domain)
            summaries = [_tail_summary(total, distribution, alpha / spec["family_size"])
                         for total, distribution in zip(totals, distributions, strict=True)]
        probabilities = {item["config_id"]: summary["raw_p"]
                         for item, summary in zip(prepared["series"], summaries, strict=True)}
        adjusted = _adjust(probabilities, spec["multiple_testing"])
        results = []
        for item, total, summary in zip(prepared["series"], totals, summaries, strict=True):
            results.append({"config_id": item["config_id"], "attempt_id": item["attempt_id"],
                            "net_profit": _money_text(total),
                            "mean_net_profit_usdt": _fraction(Fraction(total, SCALE * prepared["period_count"])),
                            "raw_p": _fraction(summary["raw_p"]),
                            "adjusted_p": _fraction(adjusted[item["config_id"]]),
                            "net_profit_lower_bound": _money_text(summary["lower_total"]),
                            "tail_count": summary["tail_count"], "tail_rank": summary["tail_rank"]})
        evidence = _freeze({"protocol_version": spec["protocol_version"], "calculation_status": "computed",
                        "status": "not_estimable", "reason_codes": ["source_untrusted", "inference_registration_unverified"],
                        "family_sha256": family.sha256, "inference_sha256": inference.sha256,
                        "input_sha256": prepared["input_sha256"], "family_size": spec["family_size"],
                        "input_manifest": [{key: item[key] for key in SERIES_KEYS - {"rows"}}
                                           | {"series_sha256": _freeze(item).sha256}
                                           for item in prepared["series"]],
                        "identities": spec["identities"], "stage": "validation", "folds": spec["folds"],
                        "period_count": prepared["period_count"], "period_ms": spec["period_ms"],
                        "block_ms": spec["block_ms"], "block_counts": prepared["block_counts"],
                        "method": spec["method"], "approximation": "weak_dependence_stationarity_required",
                        "multiple_testing": spec["multiple_testing"], "confidence_bound": spec["confidence_bound"],
                        "alpha": spec["alpha"], "replicates": REPLICATES, "seed": spec["seed"], "rng": spec["rng"],
                        "index_draws": prepared["index_draws"], "rejected_draws": rejected, "resample_digest": digest,
                        **({"compute_engine": FULL_ENGINE, "compute_resources": prepared["compute_resources"],
                            "batch_size": spec["batch_size"]} if full else {}),
                        "money_scale": 24, "results": results, "source_authenticated": False,
                        "inference_registration_verified": False, "acceptance_eligible": False,
                        "production_authorized": False})
        LOGGER.info("研究统计计算完成；来源未认证，登记未核验，不授予交易资格")
        return evidence
    except StatisticsError as error:
        LOGGER.info("研究统计不可估计 reason=%s", str(error))
        return _diagnostic(family, inference, str(error))
