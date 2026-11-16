import { describe, expect, it, vi } from 'vitest';
import { createMemoryHistory, createRouter } from 'vue-router';

vi.mock('#/locales', () => ({ $t: (key: string) => key }));

import { coreRoutes } from '../routes/core';
import quantRoutes from '../routes/modules/quant';

describe('workspace and historical route identity', () => {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [...coreRoutes, ...quantRoutes],
  });

  it.each(['factors', 'experiments', 'data-quality', 'operations'])(
    'opens the new %s workspace with a real component',
    (path) => {
      const resolved = router.resolve('/quant/' + path);
      expect(resolved.matched).toHaveLength(2);
      expect(resolved.matched.at(-1)?.components?.default).toBeTypeOf(
        'function',
      );
      expect(resolved.matched.at(-1)?.redirect).toBeUndefined();
    },
  );

  it.each([
    'overview',
    'positions',
    'market',
    'data',
    'orderflow',
    'price-spikes',
    'backtest',
    'detail',
    'tasks',
    'live',
    'research',
    'strategy-lab',
    'strategy-focus',
    'binance-demo',
    'research-ledger',
    'iteration',
    'ops',
    'pairlist',
    'security',
    'logs',
    'trade-desk',
    'acceptance',
  ])('preserves exact historical %s query identity', (path) => {
    const address = '/quant/' + path + '?run_id=historic-7&identifier=frozen-3';
    const resolved = router.resolve(address);
    expect(resolved.matched).toHaveLength(2);
    expect(resolved.fullPath).toBe(address);
    expect(resolved.matched.at(-1)?.redirect).toBeUndefined();
  });

  it.each(['forbidden', 'internal-error', 'offline', 'coming-soon'])(
    'makes the %s system page explicitly reachable',
    (page) => {
      const resolved = router.resolve('/system/' + page);
      expect(resolved.matched).toHaveLength(1);
      expect(resolved.matched[0]?.components?.default).toBeTypeOf('function');
      expect(resolved.meta.hideInMenu).toBe(true);
    },
  );
});
