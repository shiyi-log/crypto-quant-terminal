/** 所有币种展示共用的优先级；其余币种保留原有相对顺序。 */
const priority: Record<string, number> = { BTC: 0, ETH: 1, TRX: 2 };

export function compareCoins(left: unknown, right: unknown): number {
  const rank = (value: unknown) => {
    const normalized = String(value ?? '').trim().toUpperCase().replace(/\s+/g, '');
    const separated = normalized.split(/[/:_\-]/)[0] ?? '';
    if (priority[separated] !== undefined) return priority[separated];
    // Binance APIs may return compact symbols such as BTCUSDT.
    const compact = normalized.match(/^(BTC|ETH|TRX)(?:USDT|USDC|BUSD|USD|BTC|ETH)$/)?.[1];
    return priority[compact ?? ''] ?? 3;
  };
  return rank(left) - rank(right);
}

export function sortCoins<T>(
  items: readonly T[],
  coin: (item: T) => string = (item) => String(item),
): T[] {
  return [...items].sort((left, right) => compareCoins(coin(left), coin(right)));
}
