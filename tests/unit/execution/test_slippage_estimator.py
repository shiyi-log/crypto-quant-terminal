"""测试滑点估算器"""
import pytest
from decimal import Decimal
from core.execution.slippage_estimator import (
    SlippageEstimator,
    OrderbookSnapshot,
    SlippageEstimate,
    estimate_slippage_conservative,
)


class TestOrderbookSnapshot:
    """测试订单簿快照"""

    def test_mid_price_calculation(self):
        """测试中间价计算"""
        snapshot = OrderbookSnapshot(
            timestamp=1000,
            symbol='BTCUSDT',
            bids=[(Decimal('50000'), Decimal('1.0'))],
            asks=[(Decimal('50002'), Decimal('1.0'))],
        )

        assert snapshot.mid_price == Decimal('50001')
        assert snapshot.spread == Decimal('2')
        # 2/50001 × 10000 ≈ 0.4bp（允许精度误差）
        assert abs(snapshot.spread_bps - Decimal('0.4')) < Decimal('0.01')

    def test_empty_orderbook(self):
        """空订单簿"""
        snapshot = OrderbookSnapshot(
            timestamp=1000,
            symbol='BTCUSDT',
            bids=[],
            asks=[],
        )

        assert snapshot.mid_price == Decimal('0')
        assert snapshot.spread == Decimal('0')


class TestSlippageEstimator:
    """测试滑点估算器"""

    @pytest.fixture
    def estimator(self):
        return SlippageEstimator()

    @pytest.fixture
    def sample_orderbook(self):
        """模拟订单簿：BTC = 50000 USDT"""
        return OrderbookSnapshot(
            timestamp=1000,
            symbol='BTCUSDT',
            bids=[
                (Decimal('49999'), Decimal('1.0')),
                (Decimal('49998'), Decimal('2.0')),
                (Decimal('49997'), Decimal('3.0')),
            ],
            asks=[
                (Decimal('50001'), Decimal('1.0')),
                (Decimal('50002'), Decimal('2.0')),
                (Decimal('50003'), Decimal('3.0')),
            ],
        )

    def test_estimate_from_orderbook_buy_small(self, estimator, sample_orderbook):
        """买入小额（0.5 BTC）：全部吃第一档"""
        estimate = estimator.estimate_from_orderbook(
            sample_orderbook,
            side='buy',
            qty=Decimal('0.5'),
        )

        # 吃第一档：50001
        assert estimate.estimated_fill_price == Decimal('50001')
        assert estimate.confidence == 'high'
        assert estimate.method == 'orderbook'

        # 滑点 = (50001 - 50000) / 50000 × 10000 = 0.2bp
        mid = sample_orderbook.mid_price
        expected_slippage_bps = (Decimal('50001') - mid) / mid * Decimal('10000')
        assert abs(estimate.estimated_slippage_bps - expected_slippage_bps) < Decimal('0.01')

    def test_estimate_from_orderbook_buy_multi_level(self, estimator, sample_orderbook):
        """买入中等（1.5 BTC）：吃两档"""
        estimate = estimator.estimate_from_orderbook(
            sample_orderbook,
            side='buy',
            qty=Decimal('1.5'),
        )

        # 吃1.0@50001 + 0.5@50002
        # 加权平均 = (1.0×50001 + 0.5×50002) / 1.5 = 50001.333...
        expected_fill = (Decimal('1.0') * Decimal('50001') +
                        Decimal('0.5') * Decimal('50002')) / Decimal('1.5')

        assert abs(estimate.estimated_fill_price - expected_fill) < Decimal('0.01')
        assert estimate.confidence == 'high'

    def test_estimate_from_orderbook_sell(self, estimator, sample_orderbook):
        """卖出：吃买单"""
        estimate = estimator.estimate_from_orderbook(
            sample_orderbook,
            side='sell',
            qty=Decimal('0.5'),
        )

        # 吃第一档买单：49999
        assert estimate.estimated_fill_price == Decimal('49999')
        assert estimate.confidence == 'high'

    def test_estimate_from_orderbook_insufficient_depth(self, estimator, sample_orderbook):
        """订单簿深度不足"""
        estimate = estimator.estimate_from_orderbook(
            sample_orderbook,
            side='buy',
            qty=Decimal('10.0'),  # 远超订单簿深度（1+2+3=6）
        )

        # 置信度应为低
        assert estimate.confidence == 'low'

    def test_conservative_estimate(self, estimator):
        """保守估算"""
        estimate = estimator.estimate_conservative(
            symbol='BTCUSDT',
            side='buy',
            notional=Decimal('10000'),
        )

        # 应使用历史滑点（1bp）
        assert estimate.estimated_slippage_bps == Decimal('0.0001')
        assert estimate.estimated_slippage_usdt == Decimal('10000') * Decimal('0.0001')
        assert estimate.confidence == 'low'
        assert estimate.method == 'conservative'

    def test_stress_estimate(self, estimator):
        """压力估算：2×保守值"""
        conservative = estimator.estimate_conservative(
            'BTCUSDT', 'buy', Decimal('10000')
        )

        stress = estimator.estimate_stress(
            'BTCUSDT', 'buy', Decimal('10000')
        )

        # 压力滑点应为保守值的2倍
        assert stress.estimated_slippage_bps == conservative.estimated_slippage_bps * Decimal('2')
        assert stress.estimated_slippage_usdt == conservative.estimated_slippage_usdt * Decimal('2')
        assert stress.method == 'stress'

    def test_slippage_estimate_as_dict(self):
        """测试结果转字典"""
        estimate = SlippageEstimate(
            estimated_fill_price=Decimal('50001'),
            estimated_slippage_bps=Decimal('0.2'),
            estimated_slippage_usdt=Decimal('1.5'),
            confidence='high',
            method='orderbook',
        )

        d = estimate.as_dict()

        assert 'fill_price' in d
        assert 'slippage_bps' in d
        assert d['confidence'] == 'high'
        assert d['method'] == 'orderbook'

    def test_convenience_function(self):
        """测试便捷函数"""
        est = estimate_slippage_conservative('BTCUSDT', 'buy', Decimal('10000'))

        assert isinstance(est, SlippageEstimate)
        assert est.estimated_slippage_usdt > Decimal('0')


class TestSlippageScenarios:
    """测试不同滑点场景"""

    def test_btc_vs_eth_slippage(self):
        """BTC vs ETH：历史滑点对比"""
        estimator = SlippageEstimator()

        btc_est = estimator.estimate_conservative('BTCUSDT', 'buy', Decimal('10000'))
        eth_est = estimator.estimate_conservative('ETHUSDT', 'buy', Decimal('10000'))

        # 两者应有相同的基准滑点（当前实现）
        assert btc_est.estimated_slippage_bps == eth_est.estimated_slippage_bps

    def test_large_order_slippage_scaling(self):
        """大订单滑点应线性增长（保守估计）"""
        estimator = SlippageEstimator()

        est_10k = estimator.estimate_conservative('BTCUSDT', 'buy', Decimal('10000'))
        est_100k = estimator.estimate_conservative('BTCUSDT', 'buy', Decimal('100000'))

        # 100k滑点应约为10k的10倍
        ratio = est_100k.estimated_slippage_usdt / est_10k.estimated_slippage_usdt
        assert abs(ratio - Decimal('10')) < Decimal('0.01')


# 运行测试：
# pytest tests/unit/execution/test_slippage_estimator.py -v
