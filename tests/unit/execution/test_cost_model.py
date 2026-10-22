"""测试交易成本模型"""
import pytest
from decimal import Decimal
from core.execution.cost_model import (
    CostModel,
    CostBreakdown,
    calculate_cost,
    BINANCE_FEES,
)


class TestCostModel:
    """测试成本模型"""

    @pytest.fixture
    def model(self):
        return CostModel(maker_fillability=Decimal('0.70'))

    def test_maker_base_scenario(self, model):
        """Maker基准场景：70%成交率"""
        cost = model.calculate_round_trip_cost(
            entry_notional=Decimal('10000'),
            exit_notional=Decimal('10000'),
            scenario='maker_base',
            holding_hours=Decimal('24'),  # 24小时
        )

        # 手续费 = 10000 × (0.02% × 0.7 + 0.04% × 0.3) × 2
        expected_fee = Decimal('10000') * (
            BINANCE_FEES['maker'] * Decimal('0.70') +
            BINANCE_FEES['taker'] * Decimal('0.30')
        ) * Decimal('2')

        assert abs(cost.total_fee - expected_fee) < Decimal('0.01')
        assert cost.total_slippage > Decimal('0')  # 应有滑点
        assert cost.funding_cost > Decimal('0')    # 应有资金费用
        assert cost.total_cost > cost.total_fee    # 总成本 > 手续费

    def test_taker_base_scenario(self, model):
        """全Taker场景"""
        cost = model.calculate_round_trip_cost(
            entry_notional=Decimal('10000'),
            exit_notional=Decimal('10000'),
            scenario='taker_base',
            holding_hours=Decimal('24'),
        )

        # 手续费 = 10000 × 0.04% × 2 = 8 USDT
        expected_fee = Decimal('10000') * BINANCE_FEES['taker'] * Decimal('2')

        assert cost.total_fee == expected_fee
        assert cost.total_slippage > Decimal('0')
        assert cost.funding_cost > Decimal('0')

    def test_stress_scenario_higher_cost(self, model):
        """压力场景：成本应高于基准场景"""
        base_cost = model.calculate_round_trip_cost(
            entry_notional=Decimal('10000'),
            exit_notional=Decimal('10000'),
            scenario='taker_base',
            holding_hours=Decimal('24'),
        )

        stress_cost = model.calculate_round_trip_cost(
            entry_notional=Decimal('10000'),
            exit_notional=Decimal('10000'),
            scenario='stress',
            holding_hours=Decimal('24'),
        )

        # 压力场景成本应更高（2×滑点 + worst资金费率）
        assert stress_cost.total_slippage > base_cost.total_slippage
        assert stress_cost.funding_cost > base_cost.funding_cost
        assert stress_cost.total_cost > base_cost.total_cost

    def test_funding_cost_scales_with_time(self, model):
        """资金费用应与持仓时间成比例"""
        cost_24h = model.calculate_round_trip_cost(
            entry_notional=Decimal('10000'),
            exit_notional=Decimal('10000'),
            scenario='maker_base',
            holding_hours=Decimal('24'),
        )

        cost_48h = model.calculate_round_trip_cost(
            entry_notional=Decimal('10000'),
            exit_notional=Decimal('10000'),
            scenario='maker_base',
            holding_hours=Decimal('48'),
        )

        # 48小时的资金费用应约为24小时的2倍
        ratio = cost_48h.funding_cost / cost_24h.funding_cost
        assert abs(ratio - Decimal('2')) < Decimal('0.01')

    def test_zero_holding_time_no_funding(self, model):
        """0持仓时间：无资金费用"""
        cost = model.calculate_round_trip_cost(
            entry_notional=Decimal('10000'),
            exit_notional=Decimal('10000'),
            scenario='maker_base',
            holding_hours=Decimal('0'),
        )

        assert cost.funding_cost == Decimal('0')
        assert cost.total_cost == cost.total_fee + cost.total_slippage

    def test_cost_breakdown_properties(self):
        """测试CostBreakdown属性计算"""
        breakdown = CostBreakdown(
            entry_fee=Decimal('2'),
            exit_fee=Decimal('3'),
            entry_slippage=Decimal('1'),
            exit_slippage=Decimal('1.5'),
            funding_cost=Decimal('0.5'),
        )

        assert breakdown.total_fee == Decimal('5')
        assert breakdown.total_slippage == Decimal('2.5')
        assert breakdown.total_cost == Decimal('8')  # 5 + 2.5 + 0.5

    def test_cost_as_dict(self):
        """测试成本分解转字典"""
        breakdown = CostBreakdown(
            entry_fee=Decimal('2'),
            exit_fee=Decimal('3'),
            entry_slippage=Decimal('1'),
            exit_slippage=Decimal('1.5'),
            funding_cost=Decimal('0.5'),
        )

        d = breakdown.as_dict()

        assert 'entry_fee_usdt' in d
        assert 'total_cost_usdt' in d
        assert d['total_fee_usdt'] == 5.0
        assert d['total_cost_usdt'] == 8.0

    def test_convenience_function(self):
        """测试便捷函数"""
        cost = calculate_cost(
            Decimal('10000'),
            Decimal('10000'),
            'taker_base',
            Decimal('24'),
        )

        assert isinstance(cost, CostBreakdown)
        assert cost.total_cost > Decimal('0')


class TestCostScenarios:
    """测试不同成本场景的对比"""

    def test_scenario_comparison_10k_position(self):
        """10k名义，24h持仓：三种场景对比"""
        model = CostModel(maker_fillability=Decimal('0.70'))

        scenarios = ['maker_base', 'taker_base', 'stress']
        costs = {}

        for scenario in scenarios:
            costs[scenario] = model.calculate_round_trip_cost(
                entry_notional=Decimal('10000'),
                exit_notional=Decimal('10000'),
                scenario=scenario,
                holding_hours=Decimal('24'),
            )

        # maker_base应最便宜
        assert costs['maker_base'].total_cost < costs['taker_base'].total_cost

        # stress应最贵
        assert costs['stress'].total_cost > costs['taker_base'].total_cost
        assert costs['stress'].total_cost > costs['maker_base'].total_cost

    def test_large_position_cost_scaling(self):
        """大仓位（100k）成本应按比例增长"""
        model = CostModel()

        cost_10k = model.calculate_round_trip_cost(
            Decimal('10000'), Decimal('10000'), 'taker_base', Decimal('24')
        )

        cost_100k = model.calculate_round_trip_cost(
            Decimal('100000'), Decimal('100000'), 'taker_base', Decimal('24')
        )

        # 100k成本应约为10k的10倍
        ratio = cost_100k.total_cost / cost_10k.total_cost
        assert abs(ratio - Decimal('10')) < Decimal('0.1')


# 运行测试：
# pytest tests/unit/execution/test_cost_model.py -v
