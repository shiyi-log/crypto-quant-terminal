"""
交易成本模型

职责：
1. 计算Maker/Taker手续费
2. 估算滑点成本
3. 计算资金费率成本
4. 提供三种成本场景（base/taker_base/stress）
"""

from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


# Binance USDT-M期货费率（2024-10-22）
BINANCE_FEES = {
    'maker': Decimal('0.0002'),   # 0.02% - 挂单成交
    'taker': Decimal('0.0004'),   # 0.04% - 吃单成交
}

# ── Maker 成交与逆向选择：盘口实测（2026-10-22）──
# 来源：tools/measure_maker_adverse_real.py，quant.orderbooks 的 5 秒桶
# 判定口径：挂单价 P = mid×(1+δ)，成交 = 后续桶 max(bid) ≥ P —— 这是【乐观上界】，
# 未计排队；真实成交率只会更低。
#
#   pair            δ=0.5bp  δ=1.0bp  δ=2.0bp  δ=5.0bp
#   BTC/USDT:USDT     65.1%    57.7%    43.3%    12.6%
#   ETH/USDT:USDT     78.8%    72.4%    59.9%    26.6%
#
# 1bp 档取两币较保守值（BTC 57.7%），因为成本模型必须偏保守才不会被前向结果推翻。
MEASURED_MAKER_FILLABILITY = {
    '1bp': Decimal('0.58'),
    '0.5bp': Decimal('0.65'),
    '2bp': Decimal('0.43'),
    '5bp': Decimal('0.13'),
}
MEASURED_MAKER_FILLABILITY_SOURCE = (
    "orderbook 5s buckets, max(bid)>=limit price, optimistic upper bound "
    "(queue position not modeled); sample 2026-10-22 BTC/ETH 7-day window"
)

# 典型滑点估计（基点）
#
# 说明：这里的 slip 指【跨越价差/冲击】成本，不含手续费。
# - taker 必须跨价差：BTC 盘口实测 1 tick = 0.5 USDT ≈ 0.6bp（price 81739.5），
#   ETH 同量级；取 1bp 作为小额单基准，2bp 作为压力。
# - maker 不跨价差（挂单），其代价是【逆向选择】而非滑点：实测 +5m 逆向为
#   -3.2 ~ -4.5bp 且方向为负（对挂单有利），因此单列 0bp 滑点并在
#   `_calculate_maker_base` 里用成交率折算降级成本，不重复计一次滑点。
TYPICAL_SLIPPAGE = {
    'maker_base': Decimal('0'),         # 粘单不跨价差
    'taker_base': Decimal('0.0001'),    # 1bp - 跨价差（盘口实测 1 tick ≈ 0.6bp）
    'stress': Decimal('0.0002'),        # 2bp - 压力场景
}

# 资金费率估计（每8小时，年化）
FUNDING_RATE = {
    'avg_8h': Decimal('0.00005'),        # 0.005% / 8h = 0.164% 年化
    'worst_8h': Decimal('0.00010'),      # 0.01% / 8h = 0.328% 年化（压力）
}


@dataclass
class CostBreakdown:
    """成本分解"""

    entry_fee: Decimal           # 开仓手续费（USDT）
    exit_fee: Decimal            # 平仓手续费（USDT）
    entry_slippage: Decimal      # 开仓滑点（USDT）
    exit_slippage: Decimal       # 平仓滑点（USDT）
    funding_cost: Decimal        # 资金费用（USDT）

    @property
    def total_fee(self) -> Decimal:
        """总手续费"""
        return self.entry_fee + self.exit_fee

    @property
    def total_slippage(self) -> Decimal:
        """总滑点"""
        return self.entry_slippage + self.exit_slippage

    @property
    def total_cost(self) -> Decimal:
        """总成本"""
        return self.total_fee + self.total_slippage + self.funding_cost

    def as_dict(self) -> dict:
        """转换为字典（用于日志）"""
        return {
            'entry_fee_usdt': float(self.entry_fee),
            'exit_fee_usdt': float(self.exit_fee),
            'entry_slippage_usdt': float(self.entry_slippage),
            'exit_slippage_usdt': float(self.exit_slippage),
            'funding_cost_usdt': float(self.funding_cost),
            'total_fee_usdt': float(self.total_fee),
            'total_slippage_usdt': float(self.total_slippage),
            'total_cost_usdt': float(self.total_cost),
        }


CostScenario = Literal['maker_base', 'taker_base', 'stress']


class CostModel:
    """
    交易成本模型

    使用方式：
        model = CostModel()

        # 计算Maker成本（基准场景）
        cost = model.calculate_round_trip_cost(
            entry_notional=Decimal('10000'),
            exit_notional=Decimal('10100'),
            scenario='maker_base',
            holding_hours=Decimal('24'),  # 持仓24小时
        )

        print(f"总成本: {cost.total_cost} USDT")
        print(f"手续费: {cost.total_fee} USDT")
        print(f"滑点: {cost.total_slippage} USDT")
    """

    def __init__(
        self,
        maker_fillability: Decimal | None = None,
        *,
        quote_distance: str = '1bp',
    ):
        """初始化成本模型。

        参数:
            maker_fillability: Maker 订单成交概率（0-1）。默认取盘口实测的
                ``MEASURED_MAKER_FILLABILITY['1bp']``（BTC 57.7%、ETH 72.4%，
                保守取 0.58）。该实测是「桶内 max(bid) ≥ 挂单价」的**乐观上界**，
                真实成交率只会更低，因此默认值不应调高。
            quote_distance: 挂单距中间价的档位（'0.5bp'/'1bp'/'2bp'/'5bp'），
                仅在未显式给 maker_fillability 时生效。
        """
        if maker_fillability is None:
            if quote_distance not in MEASURED_MAKER_FILLABILITY:
                raise ValueError(
                    f"未知挂单档位: {quote_distance}，"
                    f"可选 {sorted(MEASURED_MAKER_FILLABILITY)}"
                )
            maker_fillability = MEASURED_MAKER_FILLABILITY[quote_distance]
        if not Decimal(0) <= Decimal(str(maker_fillability)) <= Decimal(1):
            raise ValueError("maker_fillability 必须在 [0,1] 之间")
        self.maker_fillability = Decimal(str(maker_fillability))
        self.fillability_source = MEASURED_MAKER_FILLABILITY_SOURCE

    def calculate_round_trip_cost(
        self,
        entry_notional: Decimal,
        exit_notional: Decimal,
        scenario: CostScenario = 'maker_base',
        holding_hours: Decimal = Decimal('24'),
    ) -> CostBreakdown:
        """
        计算完整往返成本

        参数：
            entry_notional: 开仓名义价值（USDT）
            exit_notional: 平仓名义价值（USDT）
            scenario: 成本场景
                - 'maker_base': Maker优先（基准）
                - 'taker_base': 全部Taker
                - 'stress': 压力场景（2×滑点，worst资金费）
            holding_hours: 持仓时长（小时）

        返回：
            CostBreakdown: 成本分解
        """
        if scenario == 'maker_base':
            return self._calculate_maker_base(
                entry_notional, exit_notional, holding_hours
            )
        elif scenario == 'taker_base':
            return self._calculate_taker_base(
                entry_notional, exit_notional, holding_hours
            )
        elif scenario == 'stress':
            return self._calculate_stress(
                entry_notional, exit_notional, holding_hours
            )
        else:
            raise ValueError(f"未知成本场景: {scenario}")

    def _calculate_maker_base(
        self,
        entry_notional: Decimal,
        exit_notional: Decimal,
        holding_hours: Decimal,
    ) -> CostBreakdown:
        """
        Maker基准场景

        假设：
        - 开仓：Maker成交（fillability%）或降级为Taker
        - 平仓：同上
        - 滑点：1bp
        - 资金费率：平均值
        """
        maker_fee = BINANCE_FEES['maker']
        taker_fee = BINANCE_FEES['taker']

        # 开仓手续费：按成交率加权
        entry_fee = (
            entry_notional * maker_fee * self.maker_fillability +
            entry_notional * taker_fee * (Decimal('1') - self.maker_fillability)
        )

        # 平仓手续费：同样加权
        exit_fee = (
            exit_notional * maker_fee * self.maker_fillability +
            exit_notional * taker_fee * (Decimal('1') - self.maker_fillability)
        )

        # 滑点（1bp）
        slippage_bp = TYPICAL_SLIPPAGE['maker_base']
        entry_slippage = entry_notional * slippage_bp
        exit_slippage = exit_notional * slippage_bp

        # 资金费用
        funding_cost = self._calculate_funding_cost(
            (entry_notional + exit_notional) / Decimal('2'),  # 平均名义
            holding_hours,
            FUNDING_RATE['avg_8h'],
        )

        return CostBreakdown(
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            entry_slippage=entry_slippage,
            exit_slippage=exit_slippage,
            funding_cost=funding_cost,
        )

    def _calculate_taker_base(
        self,
        entry_notional: Decimal,
        exit_notional: Decimal,
        holding_hours: Decimal,
    ) -> CostBreakdown:
        """
        全Taker场景

        假设：
        - 开仓/平仓：全部Taker成交
        - 滑点：1bp
        - 资金费率：平均值
        """
        taker_fee = BINANCE_FEES['taker']

        entry_fee = entry_notional * taker_fee
        exit_fee = exit_notional * taker_fee

        slippage_bp = TYPICAL_SLIPPAGE['taker_base']
        entry_slippage = entry_notional * slippage_bp
        exit_slippage = exit_notional * slippage_bp

        funding_cost = self._calculate_funding_cost(
            (entry_notional + exit_notional) / Decimal('2'),
            holding_hours,
            FUNDING_RATE['avg_8h'],
        )

        return CostBreakdown(
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            entry_slippage=entry_slippage,
            exit_slippage=exit_slippage,
            funding_cost=funding_cost,
        )

    def _calculate_stress(
        self,
        entry_notional: Decimal,
        exit_notional: Decimal,
        holding_hours: Decimal,
    ) -> CostBreakdown:
        """
        压力场景

        假设：
        - 开仓/平仓：全部Taker
        - 滑点：2bp（2×基准）
        - 资金费率：最差值
        """
        taker_fee = BINANCE_FEES['taker']

        entry_fee = entry_notional * taker_fee
        exit_fee = exit_notional * taker_fee

        slippage_bp = TYPICAL_SLIPPAGE['stress']
        entry_slippage = entry_notional * slippage_bp
        exit_slippage = exit_notional * slippage_bp

        funding_cost = self._calculate_funding_cost(
            (entry_notional + exit_notional) / Decimal('2'),
            holding_hours,
            FUNDING_RATE['worst_8h'],
        )

        return CostBreakdown(
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            entry_slippage=entry_slippage,
            exit_slippage=exit_slippage,
            funding_cost=funding_cost,
        )

    def _calculate_funding_cost(
        self,
        avg_notional: Decimal,
        holding_hours: Decimal,
        rate_per_8h: Decimal,
    ) -> Decimal:
        """
        计算资金费用

        参数：
            avg_notional: 平均名义（USDT）
            holding_hours: 持仓时长（小时）
            rate_per_8h: 每8小时资金费率

        返回：
            资金费用（USDT）

        公式：
            num_periods = holding_hours / 8
            funding_cost = avg_notional × rate_per_8h × num_periods
        """
        num_periods = holding_hours / Decimal('8')
        return avg_notional * rate_per_8h * num_periods


# 便捷函数
def calculate_cost(
    entry_notional: Decimal,
    exit_notional: Decimal,
    scenario: CostScenario = 'maker_base',
    holding_hours: Decimal = Decimal('24'),
) -> CostBreakdown:
    """
    便捷函数：计算交易成本

    示例：
        >>> cost = calculate_cost(Decimal('10000'), Decimal('10100'), 'maker_base', Decimal('24'))
        >>> print(f"总成本: {cost.total_cost} USDT")
    """
    model = CostModel()
    return model.calculate_round_trip_cost(
        entry_notional, exit_notional, scenario, holding_hours
    )
