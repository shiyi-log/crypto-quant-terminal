import { describe, expect, it, vi } from 'vitest';
import { createMemoryHistory, createRouter } from 'vue-router';

vi.mock('#/locales', () => ({ $t: (key: string) => key }));

import { coreRoutes, fallbackNotFoundRoute } from '../routes/core';
import { accessRoutes } from '../routes';
import { overridesPreferences } from '../../preferences';

describe('新版工作区与旧版入口退役', () => {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [...coreRoutes, fallbackNotFoundRoute],
  });

  it('uses the local demo preview as the application home page', () => {
    expect(overridesPreferences.app?.defaultHomePath).toBe(
      '/quant/demo-preview',
    );
  });

  it.each([
    ['trade-desk', 'TradeDesk', 'desk', 'desk'],
    ['acceptance', 'Acceptance', 'governance', 'acceptance'],
    ['factors', 'Factors', 'research', 'factors'],
    ['experiments', 'Experiments', 'research', 'experiments'],
    ['data-quality', 'DataQuality', 'governance', 'data-quality'],
    ['operations', 'Operations', 'governance', 'operations'],
    ['orderflow', 'Orderflow', 'research', 'orderflow'],
    ['price-spikes', 'PriceSpikes', 'research', 'orderflow'],
    ['research', 'Research', 'research', 'research'],
    ['strategy-lab', 'StrategyLab', 'research', 'research'],
    ['binance-demo', 'BinanceDemo', 'governance', 'settings'],
    ['security', 'Security', 'governance', 'settings'],
  ])(
    'routes %s to the local %s/%s preview',
    async (path, suffix, preview, workspace) => {
      const resolved = router.resolve('/quant/' + path);
      expect(resolved.matched).toHaveLength(1);
      expect(resolved.matched.at(-1)?.name).toBe('QuantDemoAlias' + suffix);
      expect(resolved.matched.at(-1)?.redirect).toBeTypeOf('function');
      expect(
        accessRoutes.some((route) => route.path.startsWith('/quant')),
      ).toBe(false);
      await router.push(`/quant/${path}?run_id=historic-7&identifier=frozen-3`);
      expect(router.currentRoute.value.name).toBe('QuantDemoPreview');
      expect(router.currentRoute.value.query).toEqual({
        run_id: 'historic-7',
        identifier: 'frozen-3',
        preview,
        workspace,
      });
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
    expect(resolved.matched).toHaveLength(1);
    expect(resolved.fullPath).toBe(address);
    expect(resolved.matched.at(-1)?.redirect).toBeDefined();
  });

  it('keeps the local demo preview and legacy demo alias routes distinct', () => {
    const demo = router.resolve('/quant/demo-preview');
    const desk = router.resolve('/quant/trade-desk');
    expect(demo.matched).toHaveLength(1);
    expect(demo.matched.at(-1)?.components?.default).toBeDefined();
    expect(demo.matched.some((record) => record.name === 'Root')).toBe(false);
    expect(demo.matched.at(-1)?.name).toBe('QuantDemoPreview');
    expect(desk.matched.at(-1)?.name).toBe('QuantDemoAliasTradeDesk');
    expect(demo.fullPath).toBe('/quant/demo-preview');
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
