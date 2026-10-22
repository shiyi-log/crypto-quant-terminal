"""
Binance USDT-M 强平模型

职责：
1. 计算维持保证金档位
2. 逐K线检查强平触发（标记价vs交易价双路径）
3. 模拟强平成交价（破产价+滑点）
4. 记录强平事件

参考：
- https://www.binance.com/en/support/faq/detail/360033525271
- https://www.binance.com/en/support/faq/b3c689c1f50a44cabb3a84e663b81d93
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


# Binance USDT-M 维持保证金档位表
# 来源：https://www.binance.com/en/futures/trading-parameters/perpetual/leverage-margin
# 更新：2024-10-22
# 注意：实际档位会定期调整，生产环境应从API动态获取

BINANCE_USDT_M_TIERS = {
    'BTCUSDT': [
        # (名义下限, 名义上限, 维持保证金率, 最大杠杆)
        (Decimal('0'), Decimal('50000'), Decimal('0.004'), 125),      # 0-50k: 0.4%, 125x
        (Decimal('50000'), Decimal('250000'), Decimal('0.005'), 100),  # 50k-250k: 0.5%, 100x
        (Decimal('250000'), Decimal('1000000'), Decimal('0.01'), 50),  # 250k-1M: 1.0%, 50x
        (Decimal('1000000'), Decimal('10000000'), Decimal('0.025'), 20), # 1M-10M: 2.5%, 20x
        (Decimal('10000000'), Decimal('20000000'), Decimal('0.05'), 10), # 10M-20M: 5.0%, 10x
        (Decimal('20000000'), Decimal('50000000'), Decimal('0.10'), 5),  # 20M-50M: 10.0%, 5x
        (Decimal('50000000'), Decimal('100000000'), Decimal('0.125'), 4), # 50M-100M: 12.5%, 4x
        (Decimal('100000000'), Decimal('200000000'), Decimal('0.15'), 3), # 100M-200M: 15.0%, 3x
        (Decimal('200000000'), Decimal('300000000'), Decimal('0.25'), 2), # 200M-300M: 25.0%, 2x
        (Decimal('300000000'), Decimal('500000000'), Decimal('0.50'), 1), # 300M-500M: 50.0%, 1x
    ],
    'ETHUSDT': [
        (Decimal('0'), Decimal('10000'), Decimal('0.005'), 100),
        (Decimal('10000'), Decimal('100000'), Decimal('0.0065'), 75),
        (Decimal('100000'), Decimal('500000'), Decimal('0.01'), 50),
        (Decimal('500000'), Decimal('1000000'), Decimal('0.025'), 20),
        (Decimal('1000000'), Decimal('2000000'), Decimal('0.05'), 10),
        (Decimal('2000000'), Decimal('5000000'), Decimal('0.10'), 5),
        (Decimal('5000000'), Decimal('10000000'), Decimal('0.125'), 4),
        (Decimal('10000000'), Decimal('20000000'), Decimal('0.15'), 3),
        (Decimal('20000000'), Decimal('50000000'), Decimal('0.25'), 2),
        (Decimal('50000000'), Decimal('100000000'), Decimal('0.50'), 1),
    ],
    'TRXUSDT': [
        (Decimal('0'), Decimal('5000'), Decimal('0.0065'), 75),
        (Decimal('5000'), Decimal('25000'), Decimal('0.01'), 50),
        (Decimal('25000'), Decimal('100000'), Decimal('0.02'), 25),
        (Decimal('100000'), Decimal('250000'), Decimal('0.05'), 10),
        (Decimal('250000'), Decimal('1000000'), Decimal('0.10'), 5),
        (Decimal('1000000'), Decimal('2000000'), Decimal('0.125'), 4),
        (Decimal('2000000'), Decimal('5000000'), Decimal('0.15'), 3),
        (Decimal('5000000'), Decimal('10000000'), Decimal('0.25'), 2),
        (Decimal('10000000'), Decimal('30000000'), Decimal('0.50'), 1),
    ],
}


@dataclass
class MaintenanceMarginTier:
    """Binance USDT-M 维持保证金档位"""

    notional_lower: Decimal  # 名义下限（USDT）
    notional_upper: Decimal  # 名义上限（USDT）
    mm_rate: Decimal         # 维持保证金率（小数）
    max_leverage: int        # 最大杠杆倍数

    @classmethod
    def get_tier(cls, symbol: str, notional: Decimal) -> MaintenanceMarginTier:
        """
        查询档位

        参数：
            symbol: 交易对，例如 'BTCUSDT'
            notional: 名义价值（USDT）

        返回：
            对应档位

        示例：
            >>> tier = MaintenanceMarginTier.get_tier('BTCUSDT', Decimal('100000'))
            >>> tier.mm_rate
            Decimal('0.005')  # 0.5%
            >>> tier.max_leverage
            100
        """
        # 标准化symbol（去除交易对后缀）
        symbol_normalized = symbol.replace('/USDT:USDT', 'USDT').replace(':USDT', '')

        # 查询档位表
        tiers = BINANCE_USDT_M_TIERS.get(symbol_normalized)

        if not tiers:
            # 未知交易对，使用保守默认档位
            return cls(
                notional_lower=Decimal('0'),
                notional_upper=Decimal('inf'),
                mm_rate=Decimal('0.05'),  # 5.0% 保守
                max_leverage=10,
            )

        # 二分查找档位
        for lower, upper, rate, max_lev in tiers:
            if lower <= notional < upper:
                return cls(
                    notional_lower=lower,
                    notional_upper=upper,
                    mm_rate=rate,
                    max_leverage=max_lev,
                )

        # 超出最高档位，使用最后一档
        last_tier = tiers[-1]
        return cls(
            notional_lower=last_tier[0],
            notional_upper=Decimal('inf'),
            mm_rate=last_tier[2],
            max_leverage=last_tier[3],
        )


@dataclass
class Position:
    """持仓信息（供测试和回测使用）"""

    symbol: str
    side: str  # 'long' 或 'short'
    entry_price: Decimal
    qty: Decimal  # 正数=多头，负数=空头
    entry_notional: Decimal
    opened_timestamp: int
    leverage: Decimal = Decimal('100')


@dataclass
class Candle:
    """K线数据（包含标记价和交易价）"""

    timestamp: int

    # 标记价OHLC（用于浮盈计算和强平触发检查）
    mark_open: Decimal
    mark_high: Decimal
    mark_low: Decimal
    mark_close: Decimal

    # 交易价OHLC（用于实际成交价）
    trade_open: Decimal
    trade_high: Decimal
    trade_low: Decimal
    trade_close: Decimal

    volume: Optional[Decimal] = None


@dataclass
class LiquidationCheckpoint:
    """强平检查点（单根K线的结果）"""

    timestamp: int
    symbol: str
    side: str

    # 持仓信息
    entry_price: Decimal
    entry_notional: Decimal
    qty: Decimal

    # K线价格
    mark_price: Decimal  # 标记价（收盘）
    trade_price_high: Decimal  # 交易价高点
    trade_price_low: Decimal   # 交易价低点

    # 触发标志
    is_liquidated_by_mark: bool  # 标记价触发
    is_liquidated_by_trade: bool  # 交易价触发

    # 价格
    liquidation_price: Optional[Decimal] = None  # 理论强平价
    liquidation_fill_price: Optional[Decimal] = None  # 实际成交价（含滑点）

    # 损失
    realized_loss: Optional[Decimal] = None  # 已实现损失（USDT）

    def __bool__(self) -> bool:
        """是否触发强平"""
        return self.is_liquidated_by_mark or self.is_liquidated_by_trade


class LiquidationModel:
    """
    强平模型主类

    使用方式：
        model = LiquidationModel()

        for bar in candles:
            if position:
                check = model.check_position(position, bar, mm_rate=Decimal('0.004'))
                if check:
                    # 处理强平
                    account.apply_liquidation(check)
                    position = None
    """

    def check_position(
        self,
        position: Position,
        bar: Candle,
        maintenance_margin_rate: Decimal,
    ) -> LiquidationCheckpoint:
        """
        检查单根K线的强平触发

        逻辑：
        1. 计算理论强平价
        2. 检查标记价是否穿越（浮盈触发）
        3. 检查交易价是否穿越（实际成交）
        4. 应用滑点计算最终成交价

        参数：
            position: 当前持仓
            bar: K线数据（需包含mark和trade OHLC）
            maintenance_margin_rate: 维持保证金率

        返回：
            LiquidationCheckpoint: 强平检查结果

        TODO: 实现完整强平检查逻辑
        """
        # 计算强平价
        #
        # ⚠️ 必须包含杠杆项。Binance 逐仓强平价（名义为 entry×qty）满足
        #     (liq - entry)×qty×direction + margin - liq×qty×mmr = 0
        # 其中 margin = entry×qty/leverage，解得：
        #     long : liq = entry × (1 - 1/leverage + mmr)
        #     short: liq = entry × (1 + 1/leverage - mmr)
        # 旧实现漏掉 1/leverage，在 100× 下把强平线放在 entry×(1-0.4%)≈0.996，
        # 而真实应在 entry×0.994；漏项会让强平提前约 0.6% 触发，系统性高估强平损失。
        leverage = position.leverage if position.leverage and position.leverage > 0 else Decimal('1')
        inverse_leverage = Decimal('1') / leverage
        if position.side == 'long':
            liq_price = position.entry_price * (
                Decimal('1') - inverse_leverage + maintenance_margin_rate)
        else:  # short
            liq_price = position.entry_price * (
                Decimal('1') + inverse_leverage - maintenance_margin_rate)

        # 检查标记价触发
        is_liquidated_by_mark = False
        if position.side == 'long' and bar.mark_low <= liq_price:
            is_liquidated_by_mark = True
        elif position.side == 'short' and bar.mark_high >= liq_price:
            is_liquidated_by_mark = True

        # 检查交易价触发
        is_liquidated_by_trade = False
        liquidation_fill_price = None

        if position.side == 'long' and bar.trade_low <= liq_price:
            is_liquidated_by_trade = True
            # 应用不利滑点（基准：1 tick）
            liquidation_fill_price = self.apply_stress_slippage(
                liq_price,
                position.side,
                scenario='base'
            )
        elif position.side == 'short' and bar.trade_high >= liq_price:
            is_liquidated_by_trade = True
            liquidation_fill_price = self.apply_stress_slippage(
                liq_price,
                position.side,
                scenario='base'
            )

        # 计算损失
        realized_loss = None
        if is_liquidated_by_mark or is_liquidated_by_trade:
            # TODO: 更精确的损失计算（考虑杠杆、保证金、浮盈）
            # 简化实现：假设全部保证金损失
            realized_loss = position.entry_notional / position.leverage

        return LiquidationCheckpoint(
            timestamp=bar.timestamp,
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            entry_notional=position.entry_notional,
            qty=position.qty,
            mark_price=bar.mark_close,
            trade_price_high=bar.trade_high,
            trade_price_low=bar.trade_low,
            is_liquidated_by_mark=is_liquidated_by_mark,
            is_liquidated_by_trade=is_liquidated_by_trade,
            liquidation_price=liq_price,
            liquidation_fill_price=liquidation_fill_price,
            realized_loss=realized_loss,
        )

    def apply_stress_slippage(
        self,
        base_price: Decimal,
        side: str,
        scenario: str = 'base',
    ) -> Decimal:
        """
        添加不利滑点

        参数：
            base_price: 理论强平价
            side: 'long' 或 'short'
            scenario: 'base' (1 tick) 或 'stress' (5 tick)

        返回：
            实际成交价

        TODO: 实现精确的tick大小（不同币种不同）
        """
        # BTC/ETH 假设 tick = 0.01 USDT
        tick_size = Decimal('0.01')

        if scenario == 'base':
            ticks = Decimal('1')
        elif scenario == 'stress':
            ticks = Decimal('5')
        else:
            ticks = Decimal('1')

        # 不利方向滑点
        if side == 'long':
            # 多头强平=卖出，滑点向下
            return base_price - tick_size * ticks
        else:
            # 空头强平=买入，滑点向上
            return base_price + tick_size * ticks


# TODO: 实现与strategy_simulation.py的集成
# TODO: 实现与demo_execution_service.py的集成
# TODO: 添加日志记录（每次强平事件）
