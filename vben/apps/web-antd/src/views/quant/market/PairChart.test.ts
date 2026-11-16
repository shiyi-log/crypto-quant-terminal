import type { App } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, reactive } from 'vue';
const api = vi.hoisted(() => ({
  live: vi.fn(),
  local: vi.fn(),
  signals: vi.fn(),
  render: vi.fn(),
  subscriber: null as any,
}));
vi.mock('#/api/freqtrade', () => ({
  getLiveKlines: api.live,
  getLocalOhlcv: api.local,
  getPairCandles: api.signals,
}));
vi.mock('@vben/plugins/echarts', () => ({
  EchartsUI: { template: '<div />' },
  useEcharts: () => ({
    getChartInstance: () => null,
    renderEcharts: api.render,
    updateData: api.render,
  }),
}));
vi.mock('./liveMarket', () => ({
  subscribeLiveMarket: (subscriber: unknown) => {
    api.subscriber = subscriber;
    return () => {};
  },
}));
import PairChart from './PairChart.vue';
const pair = 'BTC/USDT:USDT';
function snapshot(requestPair = pair, timeframe = '1h', close = 101) {
  return {
    pair: requestPair,
    timeframe,
    columns: ['date', 'open', 'high', 'low', 'close', 'volume'],
    data: [['2026-11-16T00:00:00Z', 100, 103, 99, close, 8]],
  };
}
const mounted: { app: App; element: HTMLDivElement }[] = [];
async function open() {
  const props = reactive({ pair, timeframe: '1h' });
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(PairChart, props) });
  app.mount(element);
  mounted.push({ app, element });
  return { props, element };
}
beforeEach(() => {
  localStorage.clear();
  api.live
    .mockReset()
    .mockImplementation(async (p: string, tf: string) => snapshot(p, tf));
  api.local
    .mockReset()
    .mockImplementation(async (p: string, tf: string) => snapshot(p, tf));
  api.signals.mockReset().mockImplementation(async (p: string, tf: string) => ({
    pair: p,
    timeframe: tf,
    columns: ['date'],
    data: [],
  }));
  api.render.mockReset();
  api.subscriber = null;
});
afterEach(() =>
  mounted.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  }),
);
describe('legacy market candle evidence', () => {
  it('labels local fallback as historical even when one public live delta arrives', async () => {
    api.live.mockRejectedValue(new Error('offline'));
    const view = await open();
    await vi.waitFor(() => expect(api.render).toHaveBeenCalled());
    expect(
      view.element.querySelector('[data-testid=candle-source]')?.textContent,
    ).toContain('本地历史回退');
    api.subscriber.onKline({
      type: 'kline',
      pair,
      timeframe: '1h',
      candle: {
        timestamp: Date.parse('2026-11-16T01:00:00Z'),
        open: 101,
        close: 102,
        high: 103,
        low: 100,
        volume: 3,
        closed: false,
      },
      exchange_event_timestamp: Date.now(),
      received_timestamp: Date.now(),
      sent_timestamp: Date.now(),
    });
    expect(
      view.element.querySelector('[data-testid=candle-source]')?.textContent,
    ).toContain('本地历史回退');
  });
  it('rejects a malformed live candle without changing verified chart facts', async () => {
    await open();
    await vi.waitFor(() => expect(api.render).toHaveBeenCalled());
    api.subscriber.onKline({
      type: 'kline',
      pair,
      timeframe: '1h',
      candle: {
        timestamp: Date.parse('2026-11-16T00:00:00Z'),
        open: 100,
        close: -10,
        high: 103,
        low: 99,
        volume: 3,
        closed: false,
      },
      exchange_event_timestamp: Date.now(),
      received_timestamp: Date.now(),
      sent_timestamp: Date.now(),
    });
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(
      api.render.mock.calls.some(
        ([options]) => options.series?.[0]?.data?.[0]?.[1] === -10,
      ),
    ).toBe(false);
  });
  it('ignores an older websocket candle outside the loaded window without rolling back the last candle time', async () => {
    const view = await open();
    await vi.waitFor(() => expect(api.render).toHaveBeenCalled());
    const before = view.element.querySelector(
      '[data-testid=candle-scope]',
    )?.textContent;
    api.subscriber.onKline({
      type: 'kline',
      pair,
      timeframe: '1h',
      candle: {
        timestamp: Date.parse('2026-11-15T23:00:00Z'),
        open: 90,
        close: 95,
        high: 98,
        low: 88,
        volume: 3,
        closed: true,
      },
      exchange_event_timestamp: Date.now(),
      received_timestamp: Date.now(),
      sent_timestamp: Date.now(),
    });
    expect(
      view.element.querySelector('[data-testid=candle-scope]')?.textContent,
    ).toBe(before);
    expect(
      view.element.querySelector('[data-testid=candle-source]')?.textContent,
    ).toContain('公共行情代理快照');
    expect(
      view.element.querySelector('[data-testid=candle-source]')?.textContent,
    ).not.toContain('实时增量');
  });
  it('rejects wrong-identity and malformed prices instead of drawing another pair or zero', async () => {
    api.live.mockResolvedValue(snapshot('ETH/USDT:USDT'));
    const bad = snapshot();
    bad.data[0]![4] = null as any;
    api.local.mockResolvedValue(bad);
    const view = await open();
    await vi.waitFor(() =>
      expect(view.element.textContent).toContain('行情暂不可用'),
    );
    expect(api.render).not.toHaveBeenCalled();
  });
  it('retains original chart facts and marks failed refresh as old data', async () => {
    const view = await open();
    await vi.waitFor(() => expect(api.render).toHaveBeenCalled());
    api.live.mockRejectedValue(
      new Error('postgres://synthetic:secret@example/test'),
    );
    api.local.mockRejectedValue(new Error('offline'));
    const refresh = [
      ...view.element.querySelectorAll<HTMLButtonElement>('button'),
    ].find((item) => item.textContent?.includes('刷新图表'));
    expect(refresh).toBeDefined();
    refresh!.click();
    await vi.waitFor(() =>
      expect(view.element.textContent).toContain('旧快照'),
    );
    expect(view.element.textContent).not.toContain('secret');
    expect(
      view.element.querySelector('[data-testid=candle-source]')?.textContent,
    ).toContain('公共行情代理');
    expect(
      view.element.querySelector('[data-testid=candle-scope]')?.textContent,
    ).toContain('BTC/USDT:USDT');
  });
  it('ignores late scope responses and their sources after the chart switches timeframe', async () => {
    let resolveOld: (value: unknown) => void = () => {};
    api.live.mockImplementation((p: string, tf: string) =>
      tf === '1h'
        ? new Promise((resolve) => {
            resolveOld = resolve;
          })
        : Promise.resolve(snapshot(p, tf, 102)),
    );
    const view = await open();
    await vi.waitFor(() => expect(api.live).toHaveBeenCalled());
    view.props.timeframe = '4h';
    await vi.waitFor(() =>
      expect(
        view.element.querySelector('[data-testid=candle-scope]')?.textContent,
      ).toContain('4h'),
    );
    await vi.waitFor(() =>
      expect(
        api.render.mock.calls.some(
          ([options]) => options.series?.[0]?.data?.[0]?.[1] === 102,
        ),
      ).toBe(true),
    );
    resolveOld(snapshot(pair, '1h', 999));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(
      view.element.querySelector('[data-testid=candle-scope]')?.textContent,
    ).toContain('4h');
    expect(
      api.render.mock.calls.some(
        ([options]) => options.series?.[0]?.data?.[0]?.[1] === 999,
      ),
    ).toBe(false);
  });
});
