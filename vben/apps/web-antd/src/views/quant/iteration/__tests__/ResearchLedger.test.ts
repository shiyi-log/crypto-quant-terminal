import type { App } from 'vue';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';

const api = vi.hoisted(() => ({ ledger: vi.fn(), forward: vi.fn() }));
vi.mock('#/api/freqtrade', () => ({
  getResearchLedger: api.ledger,
  getForwardPaper: api.forward,
}));

import ResearchLedger from '../ResearchLedger.vue';

const mounts: { app: App; element: HTMLDivElement }[] = [];
const ledger = (source = 'known-ledger') => ({
  generated_at: '2026-11-16T06:00:00Z',
  last_synced_at: '2026-11-16T05:59:00Z',
  source_id: source,
  summary: {
    version_count: 0,
    deployed_ml_count: 0,
    actual_trade_count: 0,
    actual_order_count: 0,
    closed_trade_count: 0,
    realized_profit_abs: null,
    unattributed_trade_count: 0,
  },
  versions: [],
  actual_trades: [],
  actual_orders: [],
  lessons: [],
  directions: [],
  errors: [] as string[],
});
const forward = (runId = 'known-paper') => ({
  run_id: runId,
  manifest: { variants: [] },
  summary: {
    data_ready: false,
    replay_completed: false,
    decision_count: 0,
    fill_count: 0,
    entry_count: 0,
    close_count: 0,
    closed_trade_count: null,
    closed_pnl: null,
    slippage: 'unknown',
    funding: 'unknown',
    database_ledger: {
      enabled: true,
      synced_event_count: 0,
      sync_error: null as string | null,
    },
  },
  counts: { decisions: 0, fills: 0, closes: 0 },
  decisions: [],
  fills: [],
  closes: [],
  limit: 30,
});
async function flush() {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
  await nextTick();
}
async function mount() {
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(ResearchLedger);
  app.mount(element);
  mounts.push({ app, element });
  await flush();
  return { app, element };
}
function refresh(element: Element) {
  const button = [...element.querySelectorAll('button')].find(
    (item) => item.textContent?.replaceAll(' ', '') === '手动刷新',
  );
  if (!button) throw new Error('refresh unavailable');
  button.click();
}
beforeEach(() => {
  vi.resetAllMocks();
  api.ledger.mockResolvedValue(ledger());
  api.forward.mockResolvedValue(forward());
});
afterEach(() => {
  mounts.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  });
});

describe('研究证据账安全读取与独立快照', () => {
  it('首次读取只查询，成功显示两个实际来源与未知成本', async () => {
    const { element } = await mount();
    expect(api.ledger).toHaveBeenCalledTimes(1);
    expect(api.forward).toHaveBeenCalledWith(undefined, 30);
    expect(element.textContent).toContain('known-ledger');
    expect(element.textContent).toContain('known-paper');
    expect(element.textContent).toContain('滑点和资金费仍未知');
    expect(
      [...element.querySelectorAll('button')].some((button) =>
        /删除|上线|设为优选/.test(button.textContent ?? ''),
      ),
    ).toBe(false);
  });

  it('账本请求失败保留原账本，纸面来源独立更新且错误不显示私密文本', async () => {
    const { element } = await mount();
    api.ledger.mockRejectedValueOnce(
      new Error('postgresql://operator:synthetic-secret@private-db/trades'),
    );
    api.forward.mockResolvedValueOnce(forward('paper-next'));
    refresh(element);
    await flush();
    expect(element.textContent).toContain('known-ledger');
    expect(element.textContent).toContain('paper-next');
    expect(element.textContent).toContain('保留');
    expect(element.textContent).not.toContain('synthetic-secret');
    expect(element.textContent).not.toContain('private-db');
  });

  it('纸面失败保留原运行，账本来源独立更新且不暴露内部错误', async () => {
    const { element } = await mount();
    api.forward.mockRejectedValueOnce(
      new Error('Bearer synthetic-token /Users/private/archive'),
    );
    api.ledger.mockResolvedValueOnce(ledger('ledger-next'));
    refresh(element);
    await flush();
    expect(element.textContent).toContain('ledger-next');
    expect(element.textContent).toContain('known-paper');
    expect(element.textContent).toContain('保留上次快照');
    expect(element.textContent).not.toContain('synthetic-token');
    expect(element.textContent).not.toContain('/Users/private/archive');
  });

  it('首轮请求失败显示不可用，不把失败当作真实空数据', async () => {
    api.ledger.mockRejectedValueOnce(new Error('internal secret'));
    api.forward.mockRejectedValueOnce(new Error('internal secret'));
    const { element } = await mount();
    expect(element.textContent).toContain('读取失败不代表账本没有数据');
    expect(element.textContent).toContain('读取失败不代表没有运行记录');
    expect(element.textContent).not.toContain('暂无证据账数据');
    expect(element.textContent).not.toContain('internal secret');
  });

  it('合法payload内的诊断只报告状态和数量，不透传连接串', async () => {
    const l = ledger();
    l.errors = ['postgresql://operator:synthetic-secret@private-db/trades'];
    const f = forward();
    f.summary.database_ledger.sync_error =
      'postgresql://operator:synthetic-secret@private-db/trades';
    api.ledger.mockResolvedValueOnce(l);
    api.forward.mockResolvedValueOnce(f);
    const { element } = await mount();
    expect(element.textContent).toContain('账本存在数据错误');
    expect(element.textContent).toContain('数据库账本同步待重试');
    expect(element.textContent).not.toContain('synthetic-secret');
    expect(element.textContent).not.toContain('private-db');
  });

  it('损坏刷新保留两份成功快照，不将格式错误转成空账本', async () => {
    const { element } = await mount();
    api.ledger.mockResolvedValueOnce({ summary: {}, versions: null });
    api.forward.mockResolvedValueOnce({
      summary: {},
      manifest: {},
      decisions: null,
    });
    refresh(element);
    await flush();
    expect(element.textContent).toContain('known-ledger');
    expect(element.textContent).toContain('known-paper');
    expect(element.textContent).toContain('响应不完整');
  });

  it('卸载后迟到的成功与失败都不发布，且不遗留自动刷新', async () => {
    const setIntervalSpy = vi.spyOn(globalThis, 'setInterval');
    const clearIntervalSpy = vi.spyOn(globalThis, 'clearInterval');
    let resolveLedger!: (value: ReturnType<typeof ledger>) => void;
    let rejectForward!: (error: Error) => void;
    api.ledger.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveLedger = resolve;
        }),
    );
    api.forward.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectForward = reject;
        }),
    );
    const { app, element } = await mount();
    const refreshTimer = setIntervalSpy.mock.results.at(-1)?.value;
    expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 30_000);
    app.unmount();
    expect(clearIntervalSpy).toHaveBeenCalledWith(refreshTimer);
    mounts.splice(
      mounts.findIndex((item) => item.app === app),
      1,
    );
    resolveLedger(ledger('late-ledger'));
    rejectForward(new Error('late-secret'));
    await flush();
    expect(element.textContent).toBe('');
    expect(api.ledger).toHaveBeenCalledTimes(1);
    expect(api.forward).toHaveBeenCalledTimes(1);
    element.remove();
  });
});
