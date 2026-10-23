"""Legacy diagnostic risk checker.

This module is retained for historical, scalar-position diagnostics.  It is
not the authoritative account-risk gate for executable research accounts:
``MaintenanceMarginTier`` is a dated static table and ``max_leverage`` is a
single scalar.  New frozen executions must use
``core.execution.account_risk_preflight`` together with
``core.execution.tier_snapshot`` and a verified account API snapshot.

综合风险检查器

职责：
1. 集成5个子模型（强平/成本/滑点/资金费率/仓位规模）
2. 执行三种场景检查（base/taker_base/stress）
3. 生成结构化报告
4. 判定PASS/FAIL
"""

from __future__ import annotations

# Explicit marker for callers and audits.  Keeping this module importable
# preserves legacy reports without allowing it to be mistaken for the frozen
# account-snapshot preflight path.
LEGACY_DIAGNOSTIC_ONLY = True

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Dict, Optional, Literal
from enum import Enum

# 导入子模型
from core.execution.liquidation_model import (
    LiquidationModel,
    MaintenanceMarginTier,
)
from core.execution.cost_model import (
    CostModel,
    CostBreakdown,
    CostScenario,
)
from core.execution.slippage_estimator import (
    SlippageEstimator,
    SlippageEstimate,
)
from core.execution.funding_rate_model import (
    FundingRateModel,
    FundingCostEstimate,
    FundingScenario,
)


class CheckStatus(Enum):
    """检查状态"""
    PASS = 'PASS'
    WARN = 'WARN'
    FAIL = 'FAIL'


@dataclass
class PositionSpec:
    """持仓规格"""

    symbol: str
    side: str  # 'long' / 'short'
    entry_price: Decimal
    qty: Decimal
    leverage: Decimal
    holding_hours: Decimal  # 预估持仓时长

    @property
    def entry_notional(self) -> Decimal:
        """开仓名义价值"""
        return self.entry_price * self.qty

    @property
    def margin(self) -> Decimal:
        """保证金"""
        return self.entry_notional / self.leverage


@dataclass
class LiquidationCheck:
    """强平检查结果"""

    liquidation_price: Decimal
    distance_to_liq_pct: Decimal  # 距离强平价的百分比
    mm_rate: Decimal               # 维持保证金率
    status: CheckStatus
    message: str


@dataclass
class CostCheck:
    """成本检查结果"""

    cost_breakdown: CostBreakdown
    cost_to_margin_ratio: Decimal  # 成本/保证金比率
    status: CheckStatus
    message: str


@dataclass
class SlippageCheck:
    """滑点检查结果"""

    slippage_estimate: SlippageEstimate
    slippage_to_margin_ratio: Decimal
    status: CheckStatus
    message: str


@dataclass
class FundingCheck:
    """资金费率检查结果"""

    funding_estimate: FundingCostEstimate
    funding_to_margin_ratio: Decimal
    status: CheckStatus
    message: str


@dataclass
class PositionSizeCheck:
    """仓位规模检查结果"""

    notional: Decimal
    leverage: Decimal
    margin: Decimal
    max_notional_per_symbol: Decimal
    status: CheckStatus
    message: str


@dataclass
class RiskPreflightReport:
    """风险预检报告"""

    position: PositionSpec
    scenario: str  # 'maker_base' / 'taker_base' / 'stress'

    # 子检查
    liquidation_check: LiquidationCheck
    cost_check: CostCheck
    slippage_check: SlippageCheck
    funding_check: FundingCheck
    position_size_check: PositionSizeCheck

    # 总体判定
    overall_status: CheckStatus
    overall_message: str

    # 元数据
    check_timestamp: int
    checker_version: str = '1.0.0'

    def as_dict(self) -> Dict:
        """转换为字典（用于JSON序列化）"""
        return {
            'position': {
                'symbol': self.position.symbol,
                'side': self.position.side,
                'entry_price': float(self.position.entry_price),
                'qty': float(self.position.qty),
                'leverage': float(self.position.leverage),
                'entry_notional': float(self.position.entry_notional),
                'margin': float(self.position.margin),
                'holding_hours': float(self.position.holding_hours),
            },
            'scenario': self.scenario,
            'checks': {
                'liquidation': {
                    'status': self.liquidation_check.status.value,
                    'liquidation_price': float(self.liquidation_check.liquidation_price),
                    'distance_pct': float(self.liquidation_check.distance_to_liq_pct),
                    'mm_rate': float(self.liquidation_check.mm_rate),
                    'message': self.liquidation_check.message,
                },
                'cost': {
                    'status': self.cost_check.status.value,
                    'total_cost': float(self.cost_check.cost_breakdown.total_cost),
                    'cost_to_margin_ratio': float(self.cost_check.cost_to_margin_ratio),
                    'breakdown': self.cost_check.cost_breakdown.as_dict(),
                    'message': self.cost_check.message,
                },
                'slippage': {
                    'status': self.slippage_check.status.value,
                    'slippage_usdt': float(self.slippage_check.slippage_estimate.estimated_slippage_usdt),
                    'slippage_to_margin_ratio': float(self.slippage_check.slippage_to_margin_ratio),
                    'message': self.slippage_check.message,
                },
                'funding': {
                    'status': self.funding_check.status.value,
                    'total_cost': float(self.funding_check.funding_estimate.total_cost),
                    'funding_to_margin_ratio': float(self.funding_check.funding_to_margin_ratio),
                    'message': self.funding_check.message,
                },
                'position_size': {
                    'status': self.position_size_check.status.value,
                    'notional': float(self.position_size_check.notional),
                    'leverage': float(self.position_size_check.leverage),
                    'message': self.position_size_check.message,
                },
            },
            'overall': {
                'status': self.overall_status.value,
                'message': self.overall_message,
            },
            'meta': {
                'check_timestamp': self.check_timestamp,
                'checker_version': self.checker_version,
            },
        }


class RiskPreflight:
    """
    风险预检主类

    使用方式：
        checker = RiskPreflight()

        position = PositionSpec(
            symbol='BTCUSDT',
            side='long',
            entry_price=Decimal('50000'),
            qty=Decimal('0.5'),
            leverage=Decimal('10'),
            holding_hours=Decimal('24'),
        )

        # 执行三种场景检查
        reports = checker.run_all_scenarios(position)

        for report in reports:
            print(f"{report.scenario}: {report.overall_status.value}")
            if report.overall_status == CheckStatus.FAIL:
                print(f"失败原因: {report.overall_message}")
    """

    def __init__(
        self,
        # 风险阈值
        min_distance_to_liq_pct: Decimal = Decimal('20'),  # 最小距离强平价20%
        max_cost_to_margin_ratio: Decimal = Decimal('0.10'),  # 成本不超过保证金10%
        max_slippage_to_margin_ratio: Decimal = Decimal('0.05'),  # 滑点不超过5%
        max_funding_to_margin_ratio: Decimal = Decimal('0.05'),  # 资金费不超过5%
        max_notional_per_symbol: Decimal = Decimal('500000'),  # 单币种最大名义50万
        max_leverage: Decimal = Decimal('20'),  # 最大杠杆20x
    ):
        """
        初始化风险预检器

        参数：
            min_distance_to_liq_pct: 最小距离强平价（百分比）
            max_cost_to_margin_ratio: 最大成本/保证金比率
            max_slippage_to_margin_ratio: 最大滑点/保证金比率
            max_funding_to_margin_ratio: 最大资金费/保证金比率
            max_notional_per_symbol: 单币种最大名义价值
            max_leverage: 最大杠杆倍数
        """
        self.min_distance_to_liq_pct = min_distance_to_liq_pct
        self.max_cost_to_margin_ratio = max_cost_to_margin_ratio
        self.max_slippage_to_margin_ratio = max_slippage_to_margin_ratio
        self.max_funding_to_margin_ratio = max_funding_to_margin_ratio
        self.max_notional_per_symbol = max_notional_per_symbol
        self.max_leverage = max_leverage

        # 初始化子模型
        self.liquidation_model = LiquidationModel()
        self.cost_model = CostModel()
        self.slippage_estimator = SlippageEstimator()
        self.funding_model = FundingRateModel()

    def run_all_scenarios(
        self,
        position: PositionSpec,
        timestamp: Optional[int] = None,
    ) -> List[RiskPreflightReport]:
        """
        执行三种场景检查

        参数：
            position: 持仓规格
            timestamp: 检查时间戳（默认：当前时间）

        返回：
            三份报告（maker_base, taker_base, stress）
        """
        if timestamp is None:
            import time
            timestamp = int(time.time())

        scenarios = [
            ('maker_base', 'avg'),
            ('taker_base', 'avg'),
            ('stress', 'worst'),
        ]

        reports = []
        for cost_scenario, funding_scenario in scenarios:
            report = self.check_position(
                position,
                cost_scenario=cost_scenario,
                funding_scenario=funding_scenario,
                timestamp=timestamp,
            )
            reports.append(report)

        return reports

    def check_position(
        self,
        position: PositionSpec,
        cost_scenario: str = 'maker_base',
        funding_scenario: str = 'avg',
        timestamp: Optional[int] = None,
    ) -> RiskPreflightReport:
        """
        检查单个持仓（单场景）

        参数：
            position: 持仓规格
            cost_scenario: 成本场景
            funding_scenario: 资金费场景
            timestamp: 时间戳

        返回：
            RiskPreflightReport: 风险报告
        """
        if timestamp is None:
            import time
            timestamp = int(time.time())

        # 1. 强平检查
        liq_check = self._check_liquidation(position)

        # 2. 成本检查
        cost_check = self._check_cost(position, cost_scenario)

        # 3. 滑点检查
        slip_check = self._check_slippage(position)

        # 4. 资金费率检查
        fund_check = self._check_funding(position, funding_scenario)

        # 5. 仓位规模检查
        size_check = self._check_position_size(position)

        # 总体判定
        overall_status, overall_message = self._determine_overall_status([
            liq_check, cost_check, slip_check, fund_check, size_check
        ])

        return RiskPreflightReport(
            position=position,
            scenario=cost_scenario,
            liquidation_check=liq_check,
            cost_check=cost_check,
            slippage_check=slip_check,
            funding_check=fund_check,
            position_size_check=size_check,
            overall_status=overall_status,
            overall_message=overall_message,
            check_timestamp=timestamp,
        )

    def _check_liquidation(self, pos: PositionSpec) -> LiquidationCheck:
        """强平检查"""
        # 获取维持保证金率
        tier = MaintenanceMarginTier.get_tier(pos.symbol, pos.entry_notional)
        mm_rate = tier.mm_rate

        # 计算强平价
        # 公式：liq_price = entry_price × (1 ± (1/leverage - mm_rate))
        # 对于多头：liq_price = entry × (1 - (1/lev - mm))
        # 对于空头：liq_price = entry × (1 + (1/lev - mm))
        margin_ratio = Decimal('1') / pos.leverage

        if pos.side == 'long':
            # 多头：价格下跌到触发强平
            price_drop_ratio = margin_ratio - mm_rate
            liq_price = pos.entry_price * (Decimal('1') - price_drop_ratio)
            distance_pct = ((pos.entry_price - liq_price) / pos.entry_price) * Decimal('100')
        else:  # short
            # 空头：价格上涨到触发强平
            price_rise_ratio = margin_ratio - mm_rate
            liq_price = pos.entry_price * (Decimal('1') + price_rise_ratio)
            distance_pct = ((liq_price - pos.entry_price) / pos.entry_price) * Decimal('100')

        # 判定
        if distance_pct >= self.min_distance_to_liq_pct:
            status = CheckStatus.PASS
            message = f"距离强平价{distance_pct:.1f}%，符合最小要求{self.min_distance_to_liq_pct}%"
        else:
            status = CheckStatus.FAIL
            message = f"距离强平价仅{distance_pct:.1f}%，低于最小要求{self.min_distance_to_liq_pct}%"

        return LiquidationCheck(
            liquidation_price=liq_price,
            distance_to_liq_pct=distance_pct,
            mm_rate=mm_rate,
            status=status,
            message=message,
        )

    def _check_cost(self, pos: PositionSpec, scenario: str) -> CostCheck:
        """成本检查"""
        cost_breakdown = self.cost_model.calculate_round_trip_cost(
            entry_notional=pos.entry_notional,
            exit_notional=pos.entry_notional,  # 假设无盈亏
            scenario=scenario,
            holding_hours=pos.holding_hours,
        )

        ratio = cost_breakdown.total_cost / pos.margin

        if ratio <= self.max_cost_to_margin_ratio:
            status = CheckStatus.PASS
            message = f"总成本{cost_breakdown.total_cost:.2f} USDT，占保证金{ratio*100:.1f}%"
        else:
            status = CheckStatus.FAIL
            message = f"总成本过高：{ratio*100:.1f}%，超过上限{self.max_cost_to_margin_ratio*100:.1f}%"

        return CostCheck(
            cost_breakdown=cost_breakdown,
            cost_to_margin_ratio=ratio,
            status=status,
            message=message,
        )

    def _check_slippage(self, pos: PositionSpec) -> SlippageCheck:
        """滑点检查"""
        slippage_est = self.slippage_estimator.estimate_conservative(
            pos.symbol, pos.side, pos.entry_notional
        )

        ratio = slippage_est.estimated_slippage_usdt / pos.margin

        if ratio <= self.max_slippage_to_margin_ratio:
            status = CheckStatus.PASS
            message = f"预估滑点{slippage_est.estimated_slippage_usdt:.2f} USDT，占保证金{ratio*100:.1f}%"
        else:
            status = CheckStatus.FAIL
            message = f"预估滑点过高：{ratio*100:.1f}%，超过上限{self.max_slippage_to_margin_ratio*100:.1f}%"

        return SlippageCheck(
            slippage_estimate=slippage_est,
            slippage_to_margin_ratio=ratio,
            status=status,
            message=message,
        )

    def _check_funding(self, pos: PositionSpec, scenario: str) -> FundingCheck:
        """资金费率检查"""
        funding_est = self.funding_model.estimate_funding_cost(
            pos.symbol,
            pos.entry_notional,
            pos.holding_hours,
            scenario=scenario,
        )

        ratio = funding_est.total_cost / pos.margin

        if ratio <= self.max_funding_to_margin_ratio:
            status = CheckStatus.PASS
            message = f"资金费用{funding_est.total_cost:.2f} USDT，占保证金{ratio*100:.1f}%"
        else:
            status = CheckStatus.WARN
            message = f"资金费用较高：{ratio*100:.1f}%，接近上限{self.max_funding_to_margin_ratio*100:.1f}%"

        return FundingCheck(
            funding_estimate=funding_est,
            funding_to_margin_ratio=ratio,
            status=status,
            message=message,
        )

    def _check_position_size(self, pos: PositionSpec) -> PositionSizeCheck:
        """仓位规模检查"""
        status_list = []

        # 检查名义价值
        if pos.entry_notional > self.max_notional_per_symbol:
            status_list.append((CheckStatus.FAIL, f"名义价值{pos.entry_notional}超过上限{self.max_notional_per_symbol}"))

        # 检查杠杆
        if pos.leverage > self.max_leverage:
            status_list.append((CheckStatus.FAIL, f"杠杆{pos.leverage}x超过上限{self.max_leverage}x"))

        if not status_list:
            return PositionSizeCheck(
                notional=pos.entry_notional,
                leverage=pos.leverage,
                margin=pos.margin,
                max_notional_per_symbol=self.max_notional_per_symbol,
                status=CheckStatus.PASS,
                message="仓位规模符合要求",
            )
        else:
            # 取最严重的状态
            worst_status = max(s[0] for s in status_list)
            messages = [s[1] for s in status_list]
            return PositionSizeCheck(
                notional=pos.entry_notional,
                leverage=pos.leverage,
                margin=pos.margin,
                max_notional_per_symbol=self.max_notional_per_symbol,
                status=worst_status,
                message='; '.join(messages),
            )

    def _determine_overall_status(
        self,
        checks: List,
    ) -> tuple[CheckStatus, str]:
        """判定总体状态"""
        statuses = [c.status for c in checks]

        if CheckStatus.FAIL in statuses:
            failed = [c for c in checks if c.status == CheckStatus.FAIL]
            messages = [c.message for c in failed]
            return CheckStatus.FAIL, f"检查失败: {'; '.join(messages)}"
        elif CheckStatus.WARN in statuses:
            warned = [c for c in checks if c.status == CheckStatus.WARN]
            messages = [c.message for c in warned]
            return CheckStatus.WARN, f"警告: {'; '.join(messages)}"
        else:
            return CheckStatus.PASS, "所有检查通过"


# 便捷函数
def preflight_check(position: PositionSpec) -> List[RiskPreflightReport]:
    """
    便捷函数：执行风险预检

    示例：
        >>> pos = PositionSpec(
        ...     symbol='BTCUSDT',
        ...     side='long',
        ...     entry_price=Decimal('50000'),
        ...     qty=Decimal('0.5'),
        ...     leverage=Decimal('10'),
        ...     holding_hours=Decimal('24'),
        ... )
        >>> reports = preflight_check(pos)
        >>> for r in reports:
        ...     print(f"{r.scenario}: {r.overall_status.value}")
    """
    checker = RiskPreflight()
    return checker.run_all_scenarios(position)
