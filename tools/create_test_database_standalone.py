"""
独立测试数据库初始化（无需依赖项目模块）

用途：
- 创建SQLite测试数据库
- 生成合成K线数据
- 用于P0开发和测试
"""
import sqlite3
import json
from datetime import datetime
from decimal import Decimal
import random


def create_test_database(db_path: str = 'test_quant.db'):
    """创建测试数据库表结构"""
    print(f"📦 创建测试数据库: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 删除旧表
    cursor.execute("DROP TABLE IF EXISTS candles")

    # 创建candles表
    cursor.execute("""
        CREATE TABLE candles (
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
    cursor.execute("""
        CREATE INDEX idx_candles_lookup
        ON candles(exchange, market, pair, timeframe, timestamp)
    """)

    conn.commit()
    conn.close()

    print("✅ 数据库表创建完成")


def generate_synthetic_candles(
    symbol: str = 'BTC/USDT:USDT',
    timeframe: str = '15m',
    start_date: str = '2024-09-01',
    num_candles: int = 1000,
    base_price: float = 50000.0,
) -> list[dict]:
    """生成合成K线数据"""
    print(f"🎲 生成 {symbol} {num_candles} 根合成K线...")

    start_ts = int(datetime.strptime(start_date, '%Y-%m-%d').timestamp() * 1000)
    interval_ms = 15 * 60 * 1000  # 15分钟

    candles = []
    current_price = base_price

    for i in range(num_candles):
        timestamp = start_ts + i * interval_ms

        # 随机游走
        price_change_pct = random.gauss(0, 0.005)  # 0.5%标准差
        current_price = current_price * (1 + price_change_pct)
        current_price = max(base_price * 0.5, min(base_price * 1.5, current_price))

        # OHLC
        volatility = current_price * 0.002
        open_price = current_price
        high_price = open_price + random.uniform(0, volatility)
        low_price = open_price - random.uniform(0, volatility)
        close_price = random.uniform(low_price, high_price)

        # Volume
        volume = random.uniform(10, 100)

        # 订单流
        taker_buy_base = volume * random.uniform(0.4, 0.6)
        flow_imbalance = 2.0 * (taker_buy_base / volume) - 1.0

        extras = {
            'closed': True,
            'taker_buy_base': taker_buy_base,
            'flow_imbalance': flow_imbalance,
        }

        candle = {
            'timestamp': timestamp,
            'exchange': 'binance',
            'market': 'futures',
            'pair': symbol,
            'timeframe': timeframe,
            'open': open_price,
            'high': high_price,
            'low': low_price,
            'close': close_price,
            'volume': volume,
            'extras': json.dumps(extras),
        }

        candles.append(candle)
        current_price = close_price

    print(f"✅ 生成完成，价格: {candles[0]['close']:.2f} → {candles[-1]['close']:.2f}")
    return candles


def insert_candles(db_path: str, candles: list[dict]):
    """插入K线到数据库"""
    print(f"💾 插入 {len(candles)} 根K线...")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    for candle in candles:
        cursor.execute("""
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
    conn.close()

    print("✅ 插入完成")


def verify_data(db_path: str):
    """验证测试数据"""
    print("\n🔍 验证测试数据...")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 统计
    cursor.execute("""
        SELECT
            COUNT(*) as total,
            MIN(timestamp) as first_ts,
            MAX(timestamp) as last_ts
        FROM candles
    """)
    row = cursor.fetchone()

    print(f"  总行数: {row[0]}")
    print(f"  时间范围: {datetime.fromtimestamp(row[1]/1000)} → {datetime.fromtimestamp(row[2]/1000)}")

    # 按交易对统计
    cursor.execute("""
        SELECT pair, timeframe, COUNT(*) as count
        FROM candles
        GROUP BY pair, timeframe
    """)

    print("\n📊 数据分布:")
    for row in cursor.fetchall():
        print(f"  {row[0]} {row[1]}: {row[2]} 根")

    # 示例数据
    print("\n📈 前3根K线:")
    cursor.execute("""
        SELECT timestamp, pair, open, high, low, close, volume
        FROM candles
        ORDER BY timestamp
        LIMIT 3
    """)

    for row in cursor.fetchall():
        ts = datetime.fromtimestamp(row[0]/1000).strftime('%Y-%m-%d %H:%M')
        print(f"  {ts} | {row[1]} | O:{row[2]:.2f} H:{row[3]:.2f} L:{row[4]:.2f} C:{row[5]:.2f} V:{row[6]:.2f}")

    conn.close()

    print("\n✅ 验证完成")


def main():
    db_path = 'test_quant.db'

    print("=" * 60)
    print("🚀 测试数据库初始化")
    print("=" * 60)
    print()

    # 1. 创建数据库
    create_test_database(db_path)

    # 2. 生成BTC数据（1000根，约10天）
    btc_candles = generate_synthetic_candles(
        symbol='BTC/USDT:USDT',
        timeframe='15m',
        start_date='2024-09-01',
        num_candles=1000,
        base_price=50000.0,
    )
    insert_candles(db_path, btc_candles)

    # 3. 生成ETH数据
    eth_candles = generate_synthetic_candles(
        symbol='ETH/USDT:USDT',
        timeframe='15m',
        start_date='2024-09-01',
        num_candles=1000,
        base_price=3000.0,
    )
    insert_candles(db_path, eth_candles)

    # 4. 生成TRX数据
    trx_candles = generate_synthetic_candles(
        symbol='TRX/USDT:USDT',
        timeframe='15m',
        start_date='2024-09-01',
        num_candles=1000,
        base_price=0.15,
    )
    insert_candles(db_path, trx_candles)

    # 5. 验证
    verify_data(db_path)

    print("\n" + "=" * 60)
    print("✅ 测试数据库已就绪！")
    print("=" * 60)
    print(f"\n📁 数据库文件: {db_path}")
    print("\n💡 后续步骤:")
    print("  1. 查看数据: sqlite3 test_quant.db 'SELECT COUNT(*) FROM candles;'")
    print("  2. 运行测试: pytest tests/unit/execution/ -v")
    print("  3. 配置.env: QUANT_DATABASE_URL=sqlite:///test_quant.db")
    print()


if __name__ == '__main__':
    main()
