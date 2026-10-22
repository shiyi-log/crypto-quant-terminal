"""
资金费率模型

职责：
1. 存储历史资金费率数据
2. 预测未来资金费用
3. 计算持仓成本
4. 提供三种场景（平均/最坏/极端）

数据来源：
- Binance Funding Rate History API
- 每8小时结算一次（00:00, 08:00, 16:00 UTC）
"""

from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Literal


# 典型资金费率（每8小时）
TYPICAL_FUNDING_RATES = {
    'BTCUSDT': {
        'avg': Decimal('0.00005'),    # 0.005% / 8h = 0.164% 年化
        'std': Decimal('0.00003'),    # 标准差
        'worst': Decimal('0.00010'),  # 最差值（99分位）
        'extreme': Decimal('0.00020'), # 极端值（压力测试）
    },
    'ETHUSDT': {
        'avg': Decimal('0.00006'),
        'std': Decimal('0.00004'),
        'worst': Decimal('0.00012'),
        'extreme': Decimal('0.00025'),
    },
    'TRXUSDT': {
        'avg': Decimal('0.00008'),  # 小币种资金费率通常更高
        'std': Decimal('0.00005'),
        'worst': Decimal('0.00015'),
        'extreme': Decimal('0.00030'),
    },
}


@dataclass
class FundingRateData:
    """单次资金费率记录"""

    timestamp: int          # 结算时间戳
    symbol: str             # 交易对
    funding_rate: Decimal   # 资金费率


@dataclass
class FundingCostEstimate:
    """资金费用估算"""

    symbol: str
    holding_hours: Decimal
    avg_notional: Decimal         # 平均名义价值（USDT）
    num_settlements: Decimal      # 结算次数
    rate_per_8h: Decimal          # 每8h费率
    total_cost: Decimal           # 总费用（USDT）
    scenario: str                 # 场景

    def as_dict(self) -> dict:
        return {
            'symbol': self.symbol,
            'holding_hours': float(self.holding_hours),
            'avg_notional': float(self.avg_notional),
            'num_settlements': float(self.num_settlements),
            'rate_per_8h': float(self.rate_per_8h),
            'total_cost': float(self.total_cost),
            'scenario': self.scenario,
        }


FundingScenario = Literal['avg', 'worst', 'extreme']


class FundingRateModel:
    """
    资金费率模型

    使用方式：
        model = FundingRateModel()

        # 估算24小时持仓的资金费用
        estimate = model.estimate_funding_cost(
            symbol='BTCUSDT',
            avg_notional=Decimal('50000'),
            holding_hours=Decimal('24'),
            scenario='avg',
        )

        print(f"预估资金费用: {estimate.total_cost} USDT")

        # 加载历史数据（可选）
        model.add_historical_rate(
            timestamp=1697990400,
            symbol='BTCUSDT',
            funding_rate=Decimal('0.00004'),
        )
    """

    def __init__(self):
        """初始化资金费率模型"""
        # 历史资金费率存储
        self.historical_rates: dict[str, List[FundingRateData]] = {
            'BTCUSDT': [],
            'ETHUSDT': [],
            'TRXUSDT': [],
        }

    def estimate_funding_cost(
        self,
        symbol: str,
        avg_notional: Decimal,
        holding_hours: Decimal,
        scenario: FundingScenario = 'avg',
    ) -> FundingCostEstimate:
        """
        估算资金费用

        参数：
            symbol: 交易对
            avg_notional: 平均名义价值（USDT）
            holding_hours: 持仓时长（小时）
            scenario: 费率场景
                - 'avg': 平均费率（历史均值）
                - 'worst': 最差费率（99分位）
                - 'extreme': 极端费率（压力测试）

        返回：
            FundingCostEstimate: 费用估算

        公式：
            num_settlements = holding_hours / 8
            total_cost = avg_notional × rate_per_8h × num_settlements
        """
        # 获取费率
        if symbol in TYPICAL_FUNDING_RATES:
            rates = TYPICAL_FUNDING_RATES[symbol]
        else:
            # 默认使用BTC费率
            rates = TYPICAL_FUNDING_RATES['BTCUSDT']

        if scenario == 'avg':
            rate = rates['avg']
        elif scenario == 'worst':
            rate = rates['worst']
        elif scenario == 'extreme':
            rate = rates['extreme']
        else:
            raise ValueError(f"未知费率场景: {scenario}")

        # 计算结算次数
        num_settlements = holding_hours / Decimal('8')

        # 计算总费用
        total_cost = avg_notional * rate * num_settlements

        return FundingCostEstimate(
            symbol=symbol,
            holding_hours=holding_hours,
            avg_notional=avg_notional,
            num_settlements=num_settlements,
            rate_per_8h=rate,
            total_cost=total_cost,
            scenario=scenario,
        )

    def add_historical_rate(
        self,
        timestamp: int,
        symbol: str,
        funding_rate: Decimal,
    ) -> None:
        """
        添加历史资金费率数据

        参数：
            timestamp: 结算时间戳
            symbol: 交易对
            funding_rate: 资金费率
        """
        if symbol not in self.historical_rates:
            self.historical_rates[symbol] = []

        self.historical_rates[symbol].append(
            FundingRateData(
                timestamp=timestamp,
                symbol=symbol,
                funding_rate=funding_rate,
            )
        )

    def get_recent_avg_rate(
        self,
        symbol: str,
        periods: int = 30,
    ) -> Decimal:
        """
        获取最近N期的平均费率

        参数：
            symbol: 交易对
            periods: 期数（每期8小时）

        返回：
            平均费率（Decimal）

        逻辑：
            若历史数据不足，返回典型值
        """
        if symbol not in self.historical_rates:
            return TYPICAL_FUNDING_RATES.get(symbol, {}).get('avg', Decimal('0.00005'))

        rates = self.historical_rates[symbol]
        if len(rates) == 0:
            return TYPICAL_FUNDING_RATES.get(symbol, {}).get('avg', Decimal('0.00005'))

        # 取最近N期
        recent = rates[-periods:]

        total = sum(r.funding_rate for r in recent)
        return total / Decimal(len(recent))

    def annualize_rate(self, rate_per_8h: Decimal) -> Decimal:
        """
        将8小时费率年化

        参数：
            rate_per_8h: 每8小时费率

        返回：
            年化费率（Decimal）

        公式：
            年化 = rate_per_8h × (365 × 24 / 8) = rate_per_8h × 1095
        """
        return rate_per_8h * Decimal('1095')


# 便捷函数
def estimate_funding_cost(
    symbol: str,
    avg_notional: Decimal,
    holding_hours: Decimal,
    scenario: FundingScenario = 'avg',
) -> FundingCostEstimate:
    """
    便捷函数：估算资金费用

    示例：
        >>> est = estimate_funding_cost('BTCUSDT', Decimal('50000'), Decimal('24'), 'avg')
        >>> print(f"24h资金费用: {est.total_cost} USDT")
    """
    model = FundingRateModel()
    return model.estimate_funding_cost(symbol, avg_notional, holding_hours, scenario)


def annualize(rate_per_8h: Decimal) -> Decimal:
    """
    便捷函数：年化费率

    示例：
        >>> annual = annualize(Decimal('0.00005'))  # 0.005%
        >>> print(f"年化费率: {annual * 100:.2f}%")
        年化费率: 5.48%
    """
    return rate_per_8h * Decimal('1095')
