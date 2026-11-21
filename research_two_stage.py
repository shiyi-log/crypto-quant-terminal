"""研究第二阶段的离线冻结协议；本模块不执行留出集或授予交易资格。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from research_protocol import FrozenResearchEvidence

LOGGER = logging.getLogger(__name__)
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9_.:/@-]{1,200}\Z")
IDENTITY_KEYS = {"code_hash", "data_hash", "cost_hash", "risk_hash", "strategy_hash"}
INFERENCE_KEYS = {
    "protocol_version", "family_sha256", "inference_sha256", "identities",
    "selection_policy_sha256", "holdout_policy_sha256", "seed",
}
ATTEMPT_KEYS = {"config_id", "attempt_id", "producer_id", "inference_sha256", "stage"}


@dataclass(frozen=True, slots=True)
class FrozenValidationPlan:
    """以规范 JSON 保存不可变的验证计划快照。"""

    canonical_json: str
    sha256: str

    def to_dict(self) -> dict:
        return json.loads(self.canonical_json)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _freeze(value: dict) -> FrozenValidationPlan:
    text = _canonical(value)
    return FrozenValidationPlan(text, hashlib.sha256(text.encode("utf-8")).hexdigest())


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError(f"{field} 必须为小写 SHA-256")
    return value


def _id(value: object, field: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError(f"{field} 格式无效")
    return value


def _policy_alpha(value: object) -> Fraction:
    if not isinstance(value, str):
        raise ValueError("显著性阈值必须使用精确十进制字符串")
    try:
        decimal = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("显著性阈值无效") from None
    if not decimal.is_finite() or not 0 < decimal <= Decimal("0.05"):
        raise ValueError("显著性阈值必须位于 (0, 0.05]")
    return Fraction(decimal)


def _validated_policy(value: object, *, version: str) -> dict:
    if not isinstance(value, dict) or value.get("version") != version:
        raise ValueError(f"策略版本必须为 {version}")
    if version == "pair-select-v1":
        if set(value) != {"version", "alpha"}:
            raise ValueError("候选筛选策略字段无效")
        _policy_alpha(value["alpha"])
    elif version == "holdout-pair-bootstrap-v1":
        if (set(value) != {"version", "replicates"}
                or type(value["replicates"]) is not int
                or value["replicates"] != 99_999):
            raise ValueError("留出策略必须明确使用固定 99999 次抽样")
    return json.loads(_canonical(value))


def freeze_validation_plan(
    family: FrozenResearchEvidence,
    inference: dict,
    *,
    attempt_mappings: list,
    selection_policy: dict,
    holdout_policy: dict,
) -> FrozenValidationPlan:
    """冻结完整配置尝试网格；未认证来源、顺序和留出消费均保持关闭。"""
    started = time.perf_counter()
    if type(family) is not FrozenResearchEvidence:
        raise ValueError("必须提供冻结实验族")
    if hashlib.sha256(family.canonical_json.encode("utf-8")).hexdigest() != family.sha256:
        raise ValueError("实验族哈希不匹配")
    frozen_family = family.to_dict()
    if frozen_family.get("protocol_version") != "research-protocol-v1":
        raise ValueError("实验族协议版本不受支持")
    family_hash = _sha(family.sha256, "实验族哈希")
    if not isinstance(inference, dict) or set(inference) != INFERENCE_KEYS:
        raise ValueError("推断证据字段无效")
    if (inference["protocol_version"] != "validation-block-bootstrap-v2"
            or _sha(inference["family_sha256"], "推断实验族哈希") != family_hash
            or type(inference["seed"]) is not int
            or not 0 <= inference["seed"] < 2**32):
        raise ValueError("推断证据身份或版本不匹配")
    inference_hash = _sha(inference["inference_sha256"], "推断证据哈希")
    _sha(inference["selection_policy_sha256"], "筛选策略哈希")
    _sha(inference["holdout_policy_sha256"], "留出策略哈希")
    identities = inference["identities"]
    if not isinstance(identities, dict) or set(identities) != IDENTITY_KEYS:
        raise ValueError("推断证据必须绑定五类冻结身份")
    normalized_identities = {key: _sha(identities[key], key) for key in sorted(IDENTITY_KEYS)}
    if normalized_identities != frozen_family.get("identities"):
        raise ValueError("推断证据与实验族身份不一致")

    configs = frozen_family.get("configurations")
    if (not isinstance(configs, list) or not configs
            or len(configs) != frozen_family.get("configuration_count")):
        raise ValueError("实验族配置清单不完整")
    expected_ids = {_id(row.get("config_id"), "配置身份") for row in configs}
    if len(expected_ids) != len(configs):
        raise ValueError("实验族配置身份重复")
    if not isinstance(attempt_mappings, list) or len(attempt_mappings) != len(expected_ids):
        raise ValueError("每个验证配置都必须具有且仅具有一个尝试映射")

    seen_configs: set[str] = set()
    seen_attempts: set[str] = set()
    attempts = []
    for row in attempt_mappings:
        if not isinstance(row, dict) or set(row) not in (ATTEMPT_KEYS, ATTEMPT_KEYS | {"family_sha256"}):
            raise ValueError("尝试映射字段无效")
        config_id = _id(row["config_id"], "配置身份")
        attempt_id = _id(row["attempt_id"], "尝试身份")
        producer_id = _id(row["producer_id"], "worker 身份")
        if (config_id not in expected_ids or config_id in seen_configs or attempt_id in seen_attempts
                or row["stage"] != "validation"
                or _sha(row["inference_sha256"], "尝试推断哈希") != inference_hash):
            raise ValueError("验证尝试与实验族、推断或阶段未正确绑定")
        if "family_sha256" in row and _sha(row["family_sha256"], "尝试实验族哈希") != family_hash:
            raise ValueError("尝试实验族哈希不匹配")
        seen_configs.add(config_id)
        seen_attempts.add(attempt_id)
        attempts.append({
            "config_id": config_id,
            "attempt_id": attempt_id,
            "producer_id": producer_id,
            "inference_sha256": inference_hash,
            "stage": "validation",
        })
    if seen_configs != expected_ids:
        raise ValueError("验证尝试网格未覆盖全部配置")

    selection = _validated_policy(selection_policy, version="pair-select-v1")
    holdout = _validated_policy(holdout_policy, version="holdout-pair-bootstrap-v1")
    value = {
        "protocol_version": "research-two-stage-v2",
        "family_sha256": family_hash,
        "inference_sha256": inference_hash,
        "identities": normalized_identities,
        "validation": {
            "configuration_count": len(expected_ids),
            "attempts": sorted(attempts, key=lambda item: item["config_id"]),
            "registration_order_verified": False,
            "selection_policy": selection,
        },
        "holdout": {
            "policy": holdout,
            "calculator_available": False,
            "execution_ticket": False,
        },
        "acceptance_eligible": False,
        "production_authorized": False,
    }
    plan = _freeze(value)
    elapsed_ms = (time.perf_counter() - started) * 1000
    LOGGER.info(
        "研究验证计划已冻结 阶段=validation 配置数=%s 尝试数=%s 耗时_ms=%.3f "
        "顺序认证=false 留出计算器=不可用 执行票据=false 验收资格=false 生产授权=false 摘要=%s",
        len(expected_ids), len(attempts), elapsed_ms, plan.sha256,
    )
    return plan


def _fraction(value: object, field: str) -> Fraction:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} 必须为精确数值")
    if isinstance(value, Fraction):
        result = value
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{field} 必须为有限数值")
        result = Fraction(value)
    elif isinstance(value, (str, int)):
        try:
            result = Fraction(Decimal(value))
        except (InvalidOperation, ValueError, ZeroDivisionError):
            raise ValueError(f"{field} 数值无效") from None
    else:
        raise ValueError(f"{field} 类型无效")
    return result


def select_validation_pair(results: list, policy: dict) -> dict | None:
    """按精确门槛筛选基础/压力成本组合，并使用稳定键处理并列。"""
    started = time.perf_counter()
    alpha = _policy_alpha(_validated_policy(policy, version="pair-select-v1")["alpha"])
    if not isinstance(results, list):
        raise ValueError("验证结果必须为列表")
    eligible = []
    required = {
        "config_id", "stress_config_id", "adjusted_p", "lower_bound",
        "stress_adjusted_p", "stress_lower_bound", "execution_gates_passed",
    }
    rejected: Counter[str] = Counter()
    for row in results:
        if not isinstance(row, dict) or not required <= set(row):
            raise ValueError("验证结果字段不完整")
        base = _id(row["config_id"], "基础配置身份")
        stress = _id(row["stress_config_id"], "压力配置身份")
        base_p = _fraction(row["adjusted_p"], "基础校正概率")
        base_lower = _fraction(row["lower_bound"], "基础收益下界")
        stress_p = _fraction(row["stress_adjusted_p"], "压力校正概率")
        stress_lower = _fraction(row["stress_lower_bound"], "压力收益下界")
        reason_codes = []
        if max(base_p, stress_p) > alpha:
            reason_codes.append("adjusted_probability_exceeds_alpha")
        if min(base_lower, stress_lower) <= 0:
            reason_codes.append("non_positive_lower_bound")
        if row["execution_gates_passed"] is not True:
            reason_codes.append("execution_gate_not_passed_or_unknown")
        if reason_codes:
            # 仅记录固定原因码计数，不序列化调用方结果或附加字段。
            rejected.update(reason_codes)
            continue
        eligible.append((
            -min(base_lower, stress_lower), max(base_p, stress_p), base, stress, row,
        ))
    if not eligible:
        LOGGER.info(
            "研究候选筛选完成 阶段=validation 输入数=%s 合格数=0 "
            "拒绝原因码统计=%s 耗时_ms=%.3f 结果=未选中 执行票据=false",
            len(results), json.dumps(dict(sorted(rejected.items())), ensure_ascii=False,
                                     separators=(",", ":")),
            (time.perf_counter() - started) * 1000,
        )
        return None
    selected = min(eligible, key=lambda item: item[:4])[4]
    LOGGER.info(
        "研究候选筛选完成 阶段=validation 输入数=%s 合格数=%s "
        "拒绝原因码统计=%s 耗时_ms=%.3f 结果=已按精确下界和稳定身份排序 执行票据=false",
        len(results), len(eligible),
        json.dumps(dict(sorted(rejected.items())), ensure_ascii=False,
                   separators=(",", ":")),
        (time.perf_counter() - started) * 1000,
    )
    # 统计值保留 Fraction 精度，并断开调用方传入对象的可变引用。
    return deepcopy(selected)


def claim_holdout_v2(plan: FrozenValidationPlan, *, selection: dict) -> dict:
    """留出计算器及一次性数据库消费未接通前，明确拒绝发放执行票据。"""
    started = time.perf_counter()
    if type(plan) is not FrozenValidationPlan:
        raise ValueError("必须提供冻结验证计划")
    if hashlib.sha256(plan.canonical_json.encode("utf-8")).hexdigest() != plan.sha256:
        raise ValueError("验证计划哈希不匹配")
    if not isinstance(selection, dict):
        raise ValueError("候选选择事实必须为对象")
    LOGGER.info(
        "研究留出请求已拒绝 阶段=final_holdout 原因码=holdout_calculator_unavailable "
        "耗时_ms=%.3f 计算器=不可用 持久消费=未接线 执行票据=false",
        (time.perf_counter() - started) * 1000,
    )
    return {
        "execution_ticket": False,
        "status": "not_estimable",
        "reason_codes": ["holdout_calculator_unavailable"],
    }
