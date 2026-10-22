"""
滑点估算器

职责：
1. 基于订单簿深度估算实际滑点
2. 提供保守估计和压力估计
3. 与历史成交数据结合校准

依赖：
- 实时订单簿深度数据
- 历史成交量曲线
- 市场冲击模型
"""

from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Tuple


@dataclass
class OrderbookSnapshot:
    """订单簿快照"""

    timestamp: int
    symbol: str
    bids: List[Tuple[Decimal, Decimal]]  # [(price, qty), ...]
    asks: List[Tuple[Decimal, Decimal]]  # [(price, qty), ...]

    @property
    def mid_price(self) -> Decimal:
        """中间价"""
        best_bid = self.bids[0][0] if self.bids else Decimal('0')
        best_ask = self.asks[0][0] if self.asks else Decimal('0')
        return (best_bid + best_ask) / Decimal('2')

    @property
    def spread(self) -> Decimal:
        """买卖价差"""
        if not self.bids or not self.asks:
            return Decimal('0')
        return self.asks[0][0] - self.bids[0][0]

    @property
    def spread_bps(self) -> Decimal:
        """价差（基点）"""
        if self.mid_price == 0:
            return Decimal('0')
        return self.spread / self.mid_price * Decimal('10000')


@dataclass
class SlippageEstimate:
    """滑点估算结果"""

    estimated_fill_price: Decimal       # 预估成交价
    estimated_slippage_bps: Decimal     # 预估滑点（基点）
    estimated_slippage_usdt: Decimal    # 预估滑点（USDT）
    confidence: str                      # 置信度（low/medium/high）
    method: str                          # 估算方法

    def as_dict(self) -> dict:
        return {
            'fill_price': float(self.estimated_fill_price),
            'slippage_bps': float(self.estimated_slippage_bps),
            'slippage_usdt': float(self.estimated_slippage_usdt),
            'confidence': self.confidence,
            'method': self.method,
        }


class SlippageEstimator:
    """
    滑点估算器

    使用方式：
        estimator = SlippageEstimator()

        # 方法1：基于订单簿
        snapshot = OrderbookSnapshot(...)
        estimate = estimator.estimate_from_orderbook(
            snapshot,
            side='buy',
            qty=Decimal('1.5'),  # BTC
        )

        # 方法2：保守估计（无实时数据）
        estimate = estimator.estimate_conservative(
            symbol='BTCUSDT',
            side='buy',
            notional=Decimal('50000'),
        )
    """

    def __init__(self):
        """初始化滑点估算器"""
        # 历史滑点统计（待实测数据填充）
        self.historical_slippage = {
            'BTCUSDT': {
                'maker': Decimal('0.0001'),  # 1bp
                'taker': Decimal('0.0001'),  # 1bp
            },
            'ETHUSDT': {
                'maker': Decimal('0.0001'),
                'taker': Decimal('0.0001'),
            },
        }

    def estimate_from_orderbook(
        self,
        snapshot: OrderbookSnapshot,
        side: str,
        qty: Decimal,
    ) -> SlippageEstimate:
        """
        基于订单簿深度估算滑点

        参数：
            snapshot: 订单簿快照
            side: 'buy' 或 'sell'
            qty: 目标数量（BTC/ETH等）

        返回：
            SlippageEstimate: 滑点估算

        逻辑：
            1. 遍历订单簿，累计吃单至qty
            2. 计算加权平均成交价
            3. 与中间价对比得出滑点
        """
        if side == 'buy':
            levels = snapshot.asks
        else:
            levels = snapshot.bids

        if not levels:
            return self._fallback_estimate(snapshot.symbol, side, qty)

        # 累计吃单
        remaining_qty = qty
        total_cost = Decimal('0')
        filled_qty = Decimal('0')

        for price, level_qty in levels:
            if remaining_qty <= 0:
                break

            fill_qty = min(remaining_qty, level_qty)
            total_cost += price * fill_qty
            filled_qty += fill_qty
            remaining_qty -= fill_qty

        if filled_qty == 0:
            return self._fallback_estimate(snapshot.symbol, side, qty)

        # 加权平均成交价
        avg_fill_price = total_cost / filled_qty

        # 计算滑点
        mid = snapshot.mid_price
        if side == 'buy':
            slippage_abs = avg_fill_price - mid
        else:
            slippage_abs = mid - avg_fill_price

        slippage_bps = (slippage_abs / mid) * Decimal('10000')
        slippage_usdt = slippage_abs * qty

        # 判断置信度
        if remaining_qty > 0:
            confidence = 'low'   # 订单簿深度不足
        elif filled_qty == qty:
            confidence = 'high'  # 完全吃单
        else:
            confidence = 'medium'

        return SlippageEstimate(
            estimated_fill_price=avg_fill_price,
            estimated_slippage_bps=slippage_bps,
            estimated_slippage_usdt=slippage_usdt,
            confidence=confidence,
            method='orderbook',
        )

    def estimate_conservative(
        self,
        symbol: str,
        side: str,
        notional: Decimal,
    ) -> SlippageEstimate:
        """
        保守估算（无实时订单簿）

        参数：
            symbol: 交易对
            side: 'buy' 或 'sell'
            notional: 名义价值（USDT）

        返回：
            SlippageEstimate: 保守滑点估算

        逻辑：
            使用历史Taker滑点作为基准
        """
        # 获取历史滑点
        if symbol in self.historical_slippage:
            base_slippage_bps = self.historical_slippage[symbol]['taker']
        else:
            # 默认1bp
            base_slippage_bps = Decimal('0.0001')

        # 保守估计：按名义计算USDT
        slippage_usdt = notional * base_slippage_bps

        # 假设中间价未知，用占位价
        estimated_fill_price = Decimal('0')  # 需要实际价格才能计算

        return SlippageEstimate(
            estimated_fill_price=estimated_fill_price,
            estimated_slippage_bps=base_slippage_bps,
            estimated_slippage_usdt=slippage_usdt,
            confidence='low',
            method='conservative',
        )

    def estimate_stress(
        self,
        symbol: str,
        side: str,
        notional: Decimal,
    ) -> SlippageEstimate:
        """
        压力估算（2×保守值）

        用于风险预检的极端场景
        """
        base_estimate = self.estimate_conservative(symbol, side, notional)

        return SlippageEstimate(
            estimated_fill_price=base_estimate.estimated_fill_price,
            estimated_slippage_bps=base_estimate.estimated_slippage_bps * Decimal('2'),
            estimated_slippage_usdt=base_estimate.estimated_slippage_usdt * Decimal('2'),
            confidence='low',
            method='stress',
        )

    def _fallback_estimate(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
    ) -> SlippageEstimate:
        """回退估算（订单簿不可用时）"""
        # 使用历史滑点
        if symbol in self.historical_slippage:
            slippage_bps = self.historical_slippage[symbol]['taker']
        else:
            slippage_bps = Decimal('0.0001')

        return SlippageEstimate(
            estimated_fill_price=Decimal('0'),
            estimated_slippage_bps=slippage_bps,
            estimated_slippage_usdt=Decimal('0'),  # 无法计算
            confidence='low',
            method='fallback',
        )


# 便捷函数
def estimate_slippage_conservative(
    symbol: str,
    side: str,
    notional: Decimal,
) -> SlippageEstimate:
    """
    便捷函数：保守滑点估算

    示例：
        >>> est = estimate_slippage_conservative('BTCUSDT', 'buy', Decimal('10000'))
        >>> print(f"预估滑点: {est.estimated_slippage_usdt} USDT")
    """
    estimator = SlippageEstimator()
    return estimator.estimate_conservative(symbol, side, notional)
