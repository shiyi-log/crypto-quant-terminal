"""测试强平模型"""
import pytest
from decimal import Decimal
from core.execution.liquidation_model import (
    LiquidationModel,
    LiquidationCheckpoint,
    Position,
    Candle,
    MaintenanceMarginTier,
)


class TestMaintenanceMarginTier:
    """测试维持保证金档位"""

    def test_get_tier_btc_tier1_low_notional(self):
        """BTC档位1：0-50k名义 → 0.4%维持保证金率，125x杠杆"""
        tier = MaintenanceMarginTier.get_tier('BTCUSDT', Decimal('10000'))

        assert tier.notional_lower == Decimal('0')
        assert tier.notional_upper == Decimal('50000')
        assert tier.mm_rate == Decimal('0.004')  # 0.4%
        assert tier.max_leverage == 125

    def test_get_tier_btc_tier2_medium_notional(self):
        """BTC档位2：50k-250k名义 → 0.5%维持保证金率，100x杠杆"""
        tier = MaintenanceMarginTier.get_tier('BTCUSDT', Decimal('100000'))

        assert tier.notional_lower == Decimal('50000')
        assert tier.notional_upper == Decimal('250000')
        assert tier.mm_rate == Decimal('0.005')  # 0.5%
        assert tier.max_leverage == 100

    def test_get_tier_btc_boundary_exact_50k(self):
        """BTC边界测试：恰好50k应落在第二档位"""
        tier = MaintenanceMarginTier.get_tier('BTCUSDT', Decimal('50000'))

        assert tier.mm_rate == Decimal('0.005')  # 第二档
        assert tier.max_leverage == 100

    def test_get_tier_btc_tier3_high_notional(self):
        """BTC档位3：250k-1M名义 → 1.0%维持保证金率，50x杠杆"""
        tier = MaintenanceMarginTier.get_tier('BTCUSDT', Decimal('500000'))

        assert tier.mm_rate == Decimal('0.01')  # 1.0%
        assert tier.max_leverage == 50

    def test_get_tier_btc_very_high_notional(self):
        """BTC极高名义：10M+ → 2.5%+维持保证金率"""
        tier = MaintenanceMarginTier.get_tier('BTCUSDT', Decimal('5000000'))

        assert tier.mm_rate == Decimal('0.025')  # 2.5%
        assert tier.max_leverage == 20

    def test_get_tier_eth_tier1(self):
        """ETH档位1：0-10k名义 → 0.5%维持保证金率，100x杠杆"""
        tier = MaintenanceMarginTier.get_tier('ETHUSDT', Decimal('5000'))

        assert tier.mm_rate == Decimal('0.005')  # 0.5%
        assert tier.max_leverage == 100

    def test_get_tier_eth_tier2(self):
        """ETH档位2：10k-100k名义 → 0.65%维持保证金率，75x杠杆"""
        tier = MaintenanceMarginTier.get_tier('ETHUSDT', Decimal('50000'))

        assert tier.mm_rate == Decimal('0.0065')  # 0.65%
        assert tier.max_leverage == 75

    def test_get_tier_trx_tier1(self):
        """TRX档位1：0-5k名义 → 0.65%维持保证金率，75x杠杆"""
        tier = MaintenanceMarginTier.get_tier('TRXUSDT', Decimal('2000'))

        assert tier.mm_rate == Decimal('0.0065')  # 0.65%
        assert tier.max_leverage == 75

    def test_get_tier_symbol_with_suffix(self):
        """支持带交易对后缀的symbol格式"""
        tier1 = MaintenanceMarginTier.get_tier('BTC/USDT:USDT', Decimal('10000'))
        tier2 = MaintenanceMarginTier.get_tier('BTCUSDT', Decimal('10000'))

        assert tier1.mm_rate == tier2.mm_rate
        assert tier1.max_leverage == tier2.max_leverage

    def test_get_tier_unknown_symbol(self):
        """未知交易对：应返回保守默认档位"""
        tier = MaintenanceMarginTier.get_tier('UNKNOWN', Decimal('10000'))

        assert tier.mm_rate == Decimal('0.05')  # 5.0% 保守
        assert tier.max_leverage == 10

    def test_get_tier_超出最高档位(self):
        """名义超出档位表最高值：应使用最后一档"""
        tier = MaintenanceMarginTier.get_tier('BTCUSDT', Decimal('1000000000'))  # 1B

        # 应使用最后一档的维持保证金率
        assert tier.mm_rate == Decimal('0.50')  # 50%
        assert tier.max_leverage == 1


class TestLiquidationModel:
    """测试强平模型核心逻辑"""

    @pytest.fixture
    def model(self):
        return LiquidationModel()

    def test_long_position_liquidation_by_mark_price(self, model):
        """长仓通过标记价触发强平"""
        position = Position(
            symbol='BTCUSDT',
            side='long',
            entry_price=Decimal('50000'),
            qty=Decimal('0.1'),  # 5000 USDT名义
            entry_notional=Decimal('5000'),
            opened_timestamp=1000000,
            leverage=Decimal('100'),
        )

        # 强平价必须含杠杆项：liq = entry × (1 - 1/leverage + mmr)
        #   = 50000 × (1 - 0.01 + 0.004) = 49700
        # 旧公式漏掉 1/leverage，会得到 49800——在 100× 下把强平线提前 0.2%，
        # 系统性高估强平频率与损失。
        bar = Candle(
            timestamp=1001000,
            mark_open=Decimal('50100'),
            mark_high=Decimal('50100'),
            mark_low=Decimal('49650'),  # ← 穿过强平价 49700
            mark_close=Decimal('49700'),
            trade_open=Decimal('50100'),
            trade_high=Decimal('50100'),
            trade_low=Decimal('49900'),  # 交易价未穿过
            trade_close=Decimal('50000'),
        )

        check = model.check_position(position, bar, maintenance_margin_rate=Decimal('0.004'))

        assert check.is_liquidated_by_mark is True
        assert check.is_liquidated_by_trade is False
        assert check.liquidation_price == Decimal('49700')
        # realized_loss应为保证金（5000/100=50 USDT）
        assert check.realized_loss is not None

    def test_short_position_liquidation_by_trade_price(self, model):
        """空仓通过交易价触发强平"""
        position = Position(
            symbol='ETHUSDT',
            side='short',
            entry_price=Decimal('3000'),
            qty=Decimal('-1'),  # 3000 USDT名义
            entry_notional=Decimal('3000'),
            opened_timestamp=1000000,
            leverage=Decimal('100'),
        )

        # 空头强平价含杠杆项：liq = entry × (1 + 1/leverage - mmr)
        #   = 3000 × (1 + 0.01 - 0.004) = 3018
        bar = Candle(
            timestamp=1001000,
            mark_open=Decimal('2990'),
            mark_high=Decimal('3005'),  # 标记价未穿过 3018
            mark_low=Decimal('2980'),
            mark_close=Decimal('3000'),
            trade_open=Decimal('2995'),
            trade_high=Decimal('3020'),  # ← 交易价穿过
            trade_low=Decimal('2985'),
            trade_close=Decimal('3010'),
        )

        check = model.check_position(position, bar, maintenance_margin_rate=Decimal('0.004'))

        assert check.is_liquidated_by_trade is True
        assert check.liquidation_price == Decimal('3018')
        # 不利滑点：空头买入，应该 >= 3018
        assert check.liquidation_fill_price >= Decimal('3018')

    def test_no_liquidation_within_maintenance_margin(self, model):
        """在维持保证金范围内不触发强平"""
        position = Position(
            symbol='BTCUSDT',
            side='long',
            entry_price=Decimal('50000'),
            qty=Decimal('0.1'),
            entry_notional=Decimal('5000'),
            opened_timestamp=1000000,
            leverage=Decimal('100'),
        )

        # 标记价和交易价都未穿过49800
        bar = Candle(
            timestamp=1001000,
            mark_open=Decimal('50000'),
            mark_high=Decimal('50200'),
            mark_low=Decimal('49900'),  # > 49800
            mark_close=Decimal('50100'),
            trade_open=Decimal('50000'),
            trade_high=Decimal('50200'),
            trade_low=Decimal('49900'),
            trade_close=Decimal('50100'),
        )

        check = model.check_position(position, bar, maintenance_margin_rate=Decimal('0.004'))

        assert check.is_liquidated_by_mark is False
        assert check.is_liquidated_by_trade is False
        assert not check  # __bool__ should return False

    def test_stress_slippage_scenarios(self, model):
        """压力滑点场景"""
        base_price = Decimal('50000')

        # 基准滑点（1 tick）
        slipped_base_long = model.apply_stress_slippage(base_price, 'long', 'base')
        assert slipped_base_long == Decimal('49999.99')  # -0.01

        slipped_base_short = model.apply_stress_slippage(base_price, 'short', 'base')
        assert slipped_base_short == Decimal('50000.01')  # +0.01

        # 压力滑点（5 tick）
        slipped_stress_long = model.apply_stress_slippage(base_price, 'long', 'stress')
        assert slipped_stress_long == Decimal('49999.95')  # -0.05

        slipped_stress_short = model.apply_stress_slippage(base_price, 'short', 'stress')
        assert slipped_stress_short == Decimal('50000.05')  # +0.05


class TestLiquidationIntegration:
    """集成测试（与strategy_simulation.py）"""

    def test_account_equity_after_liquidation(self):
        """TODO: 强平后账户权益更新"""
        # 需要完整的Account类实现
        pass

    def test_historical_replay_liquidations(self):
        """TODO: 回放历史强平事件"""
        # 使用风险预检P99数据构建场景
        pass

    def test_liquidation_logging(self):
        """TODO: 强平事件日志记录"""
        # 验证每次强平都被记录
        pass


# 运行测试：
# pytest tests/unit/execution/test_liquidation_model.py -v
