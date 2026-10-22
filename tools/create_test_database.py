"""
测试数据库初始化和测试数据生成

用途：
- 创建独立的测试数据库（SQLite）
- 生成合成K线数据用于开发和测试
- 验证强平模型、成本模型等核心功能
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
import random

# 设置测试环境
os.environ['QUANT_DATABASE_URL'] = 'sqlite:///test_quant.db'

from data_store import DataStore


def create_test_database():
    """创建测试数据库表结构"""
    print("📦 创建测试数据库...")

    store = DataStore()

    with store._connection() as conn:
        # 创建candles表（简化版）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                exchange TEXT NOT NULL,
                market TEXT NOT NULL,
                pair TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                extras TEXT,
                UNIQUE(exchange, market, pair, timeframe, timestamp)
            )
        """)

        # 创建索引
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_candles_lookup
            ON candles(exchange, market, pair, timeframe, timestamp)
        """)

        conn.commit()

    print("✅ 数据库表创建完成")


def generate_synthetic_candles(
    symbol: str = 'BTC/USDT:USDT',
    timeframe: str = '15m',
    start_date: str = '2024-09-01',
    num_candles: int = 1000,
) -> list[dict]:
    """
    生成合成K线数据

    特点：
    - 随机游走价格（带趋势和波动）
    - 包含closed标记
    - 包含订单流数据（taker_buy_base）
    - 用于测试强平触发场景
    """
    print(f"🎲 生成 {num_candles} 根合成K线...")

    start_ts = int(datetime.strptime(start_date, '%Y-%m-%d').timestamp() * 1000)
    interval_ms = 15 * 60 * 1000  # 15分钟

    candles = []
    current_price = Decimal('50000')  # BTC起始价

    for i in range(num_candles):
        timestamp = start_ts + i * interval_ms

        # 随机游走 + 小趋势
        price_change_pct = Decimal(str(random.gauss(0, 0.005)))  # 均值0，标准差0.5%
        current_price = current_price * (Decimal('1') + price_change_pct)
        current_price = max(Decimal('10000'), min(Decimal('100000'), current_price))

        # OHLC生成
        volatility = current_price * Decimal('0.002')  # 0.2%波动
        open_price = current_price
        high_price = open_price + Decimal(str(random.uniform(0, float(volatility))))
        low_price = open_price - Decimal(str(random.uniform(0, float(volatility))))
        close_price = Decimal(str(random.uniform(float(low_price), float(high_price))))

        # Volume
        volume = Decimal(str(random.uniform(10, 100)))

        # 订单流（taker_buy_base，约50%均衡）
        taker_buy_base = volume * Decimal(str(random.uniform(0.4, 0.6)))
        flow_imbalance = 2.0 * float(taker_buy_base / volume) - 1.0

        candle = {
            'timestamp': timestamp,
            'exchange': 'binance',
            'market': 'futures',
            'pair': symbol,
            'timeframe': timeframe,
            'open': float(open_price),
            'high': float(high_price),
            'low': float(low_price),
            'close': float(close_price),
            'volume': float(volume),
            'extras': f'{{"closed": true, "taker_buy_base": {float(taker_buy_base)}, "flow_imbalance": {flow_imbalance:.4f}}}'
        }

        candles.append(candle)
        current_price = close_price

    print(f"✅ 生成完成，价格范围: {candles[0]['close']:.2f} → {candles[-1]['close']:.2f}")
    return candles


def insert_test_candles(candles: list[dict]):
    """插入测试K线到数据库"""
    print(f"💾 插入 {len(candles)} 根K线到数据库...")

    store = DataStore()

    with store._connection() as conn:
        for candle in candles:
            conn.execute("""
                INSERT OR REPLACE INTO candles
                (timestamp, exchange, market, pair, timeframe, open, high, low, close, volume, extras)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candle['timestamp'],
                candle['exchange'],
                candle['market'],
                candle['pair'],
                candle['timeframe'],
                candle['open'],
                candle['high'],
                candle['low'],
                candle['close'],
                candle['volume'],
                candle['extras'],
            ))

        conn.commit()

    print("✅ 数据插入完成")


def verify_test_data():
    """验证测试数据"""
    print("\n🔍 验证测试数据...")

    store = DataStore()

    with store._connection() as conn:
        # 统计
        result = conn.execute("""
            SELECT
                COUNT(*) as total,
                MIN(timestamp) as first_ts,
                MAX(timestamp) as last_ts
            FROM candles
        """).fetchone()

        print(f"  总行数: {result[0]}")
        print(f"  时间范围: {datetime.fromtimestamp(result[1]/1000)} → {datetime.fromtimestamp(result[2]/1000)}")

        # 检查closed覆盖率
        result2 = conn.execute("""
            SELECT COUNT(*)
            FROM candles
            WHERE extras LIKE '%"closed": true%'
        """).fetchone()

        print(f"  closed覆盖率: {100*result2[0]/result[0]:.1f}%")

        # 示例数据
        print("\n📊 前3根K线示例:")
        samples = conn.execute("""
            SELECT timestamp, pair, open, high, low, close, volume
            FROM candles
            ORDER BY timestamp
            LIMIT 3
        """).fetchall()

        for row in samples:
            ts = datetime.fromtimestamp(row[0]/1000).strftime('%Y-%m-%d %H:%M')
            print(f"  {ts} | {row[1]} | O:{row[2]:.2f} H:{row[3]:.2f} L:{row[4]:.2f} C:{row[5]:.2f} V:{row[6]:.2f}")

    print("\n✅ 测试数据验证完成")


def main():
    """主函数：创建测试数据库并生成数据"""
    print("=" * 60)
    print("🚀 测试数据库初始化")
    print("=" * 60)

    # 1. 创建数据库
    create_test_database()

    # 2. 生成BTC 15m数据（1000根，约10天）
    btc_candles = generate_synthetic_candles(
        symbol='BTC/USDT:USDT',
        timeframe='15m',
        start_date='2024-09-01',
        num_candles=1000,
    )
    insert_test_candles(btc_candles)

    # 3. 生成ETH 15m数据（1000根）
    eth_candles = generate_synthetic_candles(
        symbol='ETH/USDT:USDT',
        timeframe='15m',
        start_date='2024-09-01',
        num_candles=1000,
    )
    # 调整ETH价格范围
    for candle in eth_candles:
        candle['open'] = candle['open'] * 0.06  # ETH约BTC的6%
        candle['high'] = candle['high'] * 0.06
        candle['low'] = candle['low'] * 0.06
        candle['close'] = candle['close'] * 0.06

    insert_test_candles(eth_candles)

    # 4. 验证
    verify_test_data()

    print("\n" + "=" * 60)
    print("✅ 测试数据库已就绪！")
    print("=" * 60)
    print("\n💡 后续步骤:")
    print("  1. 运行强平模型测试: pytest tests/unit/execution/test_liquidation_model.py -v")
    print("  2. 运行策略回测: uv run python -m strategy_simulation --test")
    print("  3. 检查数据: sqlite3 test_quant.db 'SELECT COUNT(*) FROM candles;'")
    print()


if __name__ == '__main__':
    main()
