"""
维持保证金档位表验证工具

用途：
- 可视化档位表
- 验证档位查询逻辑
- 生成档位对比报告
"""
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from decimal import Decimal
from core.execution.liquidation_model import MaintenanceMarginTier, BINANCE_USDT_M_TIERS


def visualize_tiers(symbol: str):
    """可视化指定交易对的档位表"""
    print(f"\n{'='*80}")
    print(f"Binance USDT-M维持保证金档位表：{symbol}")
    print(f"{'='*80}")

    symbol_normalized = symbol.replace('/USDT:USDT', 'USDT').replace(':USDT', '')
    tiers = BINANCE_USDT_M_TIERS.get(symbol_normalized, [])

    if not tiers:
        print(f"❌ 未找到{symbol}的档位表")
        return

    print(f"\n{'档位':<6} {'名义范围(USDT)':<30} {'维持保证金率':<15} {'最大杠杆':<10}")
    print("-" * 80)

    for i, (lower, upper, rate, max_lev) in enumerate(tiers, 1):
        notional_range = f"{lower:>12,.0f} ~ {upper:>12,.0f}"
        mm_rate_pct = f"{float(rate)*100:>6.2f}%"
        max_leverage = f"{max_lev:>3}x"

        print(f"T{i:<5} {notional_range:<30} {mm_rate_pct:<15} {max_leverage:<10}")

    print("\n" + "="*80)


def test_tier_lookup_examples():
    """测试典型名义价值的档位查询"""
    print(f"\n{'='*80}")
    print("档位查询测试示例")
    print(f"{'='*80}\n")

    test_cases = [
        ('BTCUSDT', Decimal('10000'), "小额10k"),
        ('BTCUSDT', Decimal('100000'), "中等100k"),
        ('BTCUSDT', Decimal('1000000'), "大额1M"),
        ('BTCUSDT', Decimal('10000000'), "巨额10M"),
        ('ETHUSDT', Decimal('5000'), "ETH小额5k"),
        ('ETHUSDT', Decimal('50000'), "ETH中等50k"),
        ('TRXUSDT', Decimal('2000'), "TRX小额2k"),
    ]

    print(f"{'交易对':<12} {'名义(USDT)':<15} {'场景':<15} {'维持保证金率':<15} {'最大杠杆':<10}")
    print("-" * 80)

    for symbol, notional, scenario in test_cases:
        tier = MaintenanceMarginTier.get_tier(symbol, notional)
        mm_rate_pct = f"{float(tier.mm_rate)*100:.2f}%"
        max_lev = f"{tier.max_leverage}x"

        print(f"{symbol:<12} {notional:>12,.0f}   {scenario:<15} {mm_rate_pct:<15} {max_lev:<10}")

    print("\n" + "="*80)


def calculate_liquidation_price_examples():
    """计算不同档位下的强平价格示例"""
    print(f"\n{'='*80}")
    print("强平价格计算示例（100x杠杆）")
    print(f"{'='*80}\n")

    examples = [
        ('BTCUSDT', Decimal('50000'), Decimal('0.1'), 'long', "BTC多头0.1张"),
        ('BTCUSDT', Decimal('50000'), Decimal('0.1'), 'short', "BTC空头0.1张"),
        ('ETHUSDT', Decimal('3000'), Decimal('1.0'), 'long', "ETH多头1张"),
        ('ETHUSDT', Decimal('3000'), Decimal('1.0'), 'short', "ETH空头1张"),
    ]

    print(f"{'交易对':<10} {'方向':<6} {'入场价':<12} {'数量':<10} {'名义':<12} {'档位率':<10} {'强平价':<12}")
    print("-" * 80)

    for symbol, entry_price, qty, side, scenario in examples:
        notional = entry_price * abs(qty)
        tier = MaintenanceMarginTier.get_tier(symbol, notional)

        if side == 'long':
            liq_price = entry_price * (Decimal('1') - tier.mm_rate)
        else:
            liq_price = entry_price * (Decimal('1') + tier.mm_rate)

        print(f"{symbol:<10} {side:<6} {entry_price:>10,.2f}   {qty:>8.4f}   {notional:>10,.0f}   {float(tier.mm_rate)*100:>5.2f}%   {liq_price:>10,.2f}")

    print("\n说明：")
    print("  - 强平价 = 入场价 × (1 ± 维持保证金率)")
    print("  - 多头：强平价 = 入场价 × (1 - mm_rate)")
    print("  - 空头：强平价 = 入场价 × (1 + mm_rate)")
    print(f"\n{'='*80}")


def verify_tier_coverage():
    """验证档位表覆盖完整性"""
    print(f"\n{'='*80}")
    print("档位表覆盖验证")
    print(f"{'='*80}\n")

    symbols = ['BTCUSDT', 'ETHUSDT', 'TRXUSDT']

    for symbol in symbols:
        tiers = BINANCE_USDT_M_TIERS.get(symbol, [])

        if not tiers:
            print(f"❌ {symbol}: 无档位表")
            continue

        # 检查连续性
        gaps = []
        for i in range(len(tiers) - 1):
            current_upper = tiers[i][1]
            next_lower = tiers[i+1][0]

            if current_upper != next_lower:
                gaps.append((current_upper, next_lower))

        # 检查覆盖范围
        first_lower = tiers[0][0]
        last_upper = tiers[-1][1]

        print(f"✅ {symbol}:")
        print(f"   档位数: {len(tiers)}")
        print(f"   覆盖范围: {first_lower:,.0f} ~ {last_upper:,.0f} USDT")
        print(f"   档位从0开始: {'✅' if first_lower == 0 else '❌'}")
        print(f"   档位连续: {'✅' if len(gaps) == 0 else f'❌ ({len(gaps)}处缺口)'}")

        if gaps:
            for gap_start, gap_end in gaps:
                print(f"     缺口: {gap_start:,.0f} ~ {gap_end:,.0f}")

    print(f"\n{'='*80}")


def main():
    """运行所有验证和可视化"""
    print("\n" + "🔍 Binance USDT-M 维持保证金档位表验证工具")

    # 1. 可视化档位表
    for symbol in ['BTCUSDT', 'ETHUSDT', 'TRXUSDT']:
        visualize_tiers(symbol)

    # 2. 档位查询测试
    test_tier_lookup_examples()

    # 3. 强平价格计算示例
    calculate_liquidation_price_examples()

    # 4. 验证覆盖完整性
    verify_tier_coverage()

    print("\n✅ 验证完成！\n")


if __name__ == '__main__':
    main()
