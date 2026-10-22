"""测试资金费率模型"""
import pytest
from decimal import Decimal
from core.execution.funding_rate_model import (
    FundingRateModel,
    FundingRateData,
    FundingCostEstimate,
    estimate_funding_cost,
    annualize,
    TYPICAL_FUNDING_RATES,
)


class TestFundingRateModel:
    """测试资金费率模型"""

    @pytest.fixture
    def model(self):
        return FundingRateModel()

    def test_estimate_avg_scenario(self, model):
        """平均场景：24h持仓"""
        estimate = model.estimate_funding_cost(
            symbol='BTCUSDT',
            avg_notional=Decimal('50000'),
            holding_hours=Decimal('24'),
            scenario='avg',
        )

        # 24h = 3次结算（0h, 8h, 16h）
        assert estimate.num_settlements == Decimal('3')

        # 费用 = 50000 × 0.00005 × 3 = 7.5 USDT
        expected_cost = Decimal('50000') * TYPICAL_FUNDING_RATES['BTCUSDT']['avg'] * Decimal('3')
        assert estimate.total_cost == expected_cost
        assert estimate.scenario == 'avg'

    def test_estimate_worst_scenario(self, model):
        """最差场景：应高于平均"""
        avg_est = model.estimate_funding_cost(
            'BTCUSDT', Decimal('50000'), Decimal('24'), 'avg'
        )

        worst_est = model.estimate_funding_cost(
            'BTCUSDT', Decimal('50000'), Decimal('24'), 'worst'
        )

        assert worst_est.total_cost > avg_est.total_cost
        assert worst_est.scenario == 'worst'

    def test_estimate_extreme_scenario(self, model):
        """极端场景：应最高"""
        worst_est = model.estimate_funding_cost(
            'BTCUSDT', Decimal('50000'), Decimal('24'), 'worst'
        )

        extreme_est = model.estimate_funding_cost(
            'BTCUSDT', Decimal('50000'), Decimal('24'), 'extreme'
        )

        assert extreme_est.total_cost > worst_est.total_cost
        assert extreme_est.scenario == 'extreme'

    def test_funding_cost_scales_with_time(self, model):
        """费用应与持仓时间成比例"""
        cost_24h = model.estimate_funding_cost(
            'BTCUSDT', Decimal('50000'), Decimal('24'), 'avg'
        )

        cost_48h = model.estimate_funding_cost(
            'BTCUSDT', Decimal('50000'), Decimal('48'), 'avg'
        )

        # 48h费用应为24h的2倍
        ratio = cost_48h.total_cost / cost_24h.total_cost
        assert abs(ratio - Decimal('2')) < Decimal('0.01')

    def test_funding_cost_scales_with_notional(self, model):
        """费用应与名义价值成比例"""
        cost_50k = model.estimate_funding_cost(
            'BTCUSDT', Decimal('50000'), Decimal('24'), 'avg'
        )

        cost_100k = model.estimate_funding_cost(
            'BTCUSDT', Decimal('100000'), Decimal('24'), 'avg'
        )

        # 100k费用应为50k的2倍
        ratio = cost_100k.total_cost / cost_50k.total_cost
        assert abs(ratio - Decimal('2')) < Decimal('0.01')

    def test_zero_holding_time(self, model):
        """0持仓时间：无费用"""
        estimate = model.estimate_funding_cost(
            'BTCUSDT', Decimal('50000'), Decimal('0'), 'avg'
        )

        assert estimate.num_settlements == Decimal('0')
        assert estimate.total_cost == Decimal('0')

    def test_add_historical_rate(self, model):
        """测试添加历史数据"""
        model.add_historical_rate(
            timestamp=1697990400,
            symbol='BTCUSDT',
            funding_rate=Decimal('0.00004'),
        )

        assert len(model.historical_rates['BTCUSDT']) == 1
        assert model.historical_rates['BTCUSDT'][0].funding_rate == Decimal('0.00004')

    def test_get_recent_avg_rate_no_data(self, model):
        """无历史数据：返回典型值"""
        avg_rate = model.get_recent_avg_rate('BTCUSDT')

        assert avg_rate == TYPICAL_FUNDING_RATES['BTCUSDT']['avg']

    def test_get_recent_avg_rate_with_data(self, model):
        """有历史数据：计算平均值"""
        # 添加3条历史数据
        model.add_historical_rate(1000, 'BTCUSDT', Decimal('0.00004'))
        model.add_historical_rate(2000, 'BTCUSDT', Decimal('0.00006'))
        model.add_historical_rate(3000, 'BTCUSDT', Decimal('0.00005'))

        avg_rate = model.get_recent_avg_rate('BTCUSDT', periods=3)

        expected = (Decimal('0.00004') + Decimal('0.00006') + Decimal('0.00005')) / Decimal('3')
        assert avg_rate == expected

    def test_annualize_rate(self, model):
        """测试年化计算"""
        rate_8h = Decimal('0.00005')  # 0.005% / 8h

        annual = model.annualize_rate(rate_8h)

        # 年化 = 0.00005 × 1095 = 0.05475 = 5.475%
        expected = Decimal('0.00005') * Decimal('1095')
        assert annual == expected

    def test_estimate_as_dict(self):
        """测试结果转字典"""
        estimate = FundingCostEstimate(
            symbol='BTCUSDT',
            holding_hours=Decimal('24'),
            avg_notional=Decimal('50000'),
            num_settlements=Decimal('3'),
            rate_per_8h=Decimal('0.00005'),
            total_cost=Decimal('7.5'),
            scenario='avg',
        )

        d = estimate.as_dict()

        assert d['symbol'] == 'BTCUSDT'
        assert d['holding_hours'] == 24.0
        assert d['total_cost'] == 7.5
        assert d['scenario'] == 'avg'


class TestFundingScenarios:
    """测试不同费率场景"""

    def test_btc_eth_trx_comparison(self):
        """对比BTC/ETH/TRX资金费率"""
        model = FundingRateModel()

        btc = model.estimate_funding_cost('BTCUSDT', Decimal('50000'), Decimal('24'), 'avg')
        eth = model.estimate_funding_cost('ETHUSDT', Decimal('50000'), Decimal('24'), 'avg')
        trx = model.estimate_funding_cost('TRXUSDT', Decimal('50000'), Decimal('24'), 'avg')

        # TRX（小币种）费率应最高
        assert trx.rate_per_8h > eth.rate_per_8h
        assert trx.rate_per_8h > btc.rate_per_8h

    def test_long_holding_period(self):
        """长持仓（30天）费用估算"""
        model = FundingRateModel()

        estimate = model.estimate_funding_cost(
            'BTCUSDT',
            Decimal('50000'),
            Decimal('720'),  # 30天 = 720小时
            'avg',
        )

        # 30天 = 90次结算
        assert estimate.num_settlements == Decimal('90')
        assert estimate.total_cost > Decimal('0')


class TestConvenienceFunctions:
    """测试便捷函数"""

    def test_estimate_funding_cost_function(self):
        """测试estimate_funding_cost便捷函数"""
        est = estimate_funding_cost('BTCUSDT', Decimal('50000'), Decimal('24'), 'avg')

        assert isinstance(est, FundingCostEstimate)
        assert est.total_cost > Decimal('0')

    def test_annualize_function(self):
        """测试annualize便捷函数"""
        annual = annualize(Decimal('0.00005'))

        expected = Decimal('0.00005') * Decimal('1095')
        assert annual == expected

        # 验证年化百分比
        annual_percent = annual * Decimal('100')
        assert abs(annual_percent - Decimal('5.475')) < Decimal('0.001')


# 运行测试：
# pytest tests/unit/execution/test_funding_rate_model.py -v
