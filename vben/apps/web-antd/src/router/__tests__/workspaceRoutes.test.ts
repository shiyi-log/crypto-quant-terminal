import { describe, expect, it, vi } from 'vitest';
import { createMemoryHistory, createRouter } from 'vue-router';

vi.mock('#/locales', () => ({ $t: (key: string) => key }));

import { coreRoutes, fallbackNotFoundRoute } from '../routes/core';
import quantRoutes from '../routes/modules/quant';

describe('新版工作区与旧版入口退役', () => {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [...coreRoutes, ...quantRoutes, fallbackNotFoundRoute],
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
    'orderflow',
    'price-spikes',
    'research',
    'strategy-lab',
    'binance-demo',
    'security',
    'trade-desk',
    'acceptance',
  ])('preserves exact historical %s query identity', (path) => {
    const address = '/quant/' + path + '?run_id=historic-7&identifier=frozen-3';
    const resolved = router.resolve(address);
    expect(resolved.matched).toHaveLength(2);
    expect(resolved.fullPath).toBe(address);
    expect(resolved.matched.at(-1)?.redirect).toBeUndefined();
  });

  it.each([
    'overview',
    'positions',
    'market',
    'data',
    'backtest',
    'detail',
    'tasks',
    'live',
    'strategy-focus',
    'research-ledger',
    'iteration',
    'ops',
    'pairlist',
    'logs',
  ])('旧版 %s 入口不再解析为可挂载业务页面', (path) => {
    const resolved = router.resolve('/quant/' + path + '?run_id=old-7');
    expect(resolved.name).toBe('FallbackNotFound');
    expect(resolved.matched.some((record) => record.name === 'Quant')).toBe(
      false,
    );
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
