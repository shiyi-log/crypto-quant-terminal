import type { App } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import {
  Alert as AntAlert,
  Button as AntButton,
  Tag as AntTag,
} from 'ant-design-vue';
const mocks = vi.hoisted(() => ({
  config: vi.fn(),
  count: vi.fn(),
  start: vi.fn(),
  stop: vi.fn(),
  stopbuy: vi.fn(),
  reload: vi.fn(),
  exit: vi.fn(),
  confirm: vi.fn(),
  message: { info: vi.fn(), warning: vi.fn(), error: vi.fn() },
  ws: vi.fn(),
  off: vi.fn(),
}));
vi.mock('ant-design-vue', async (importOriginal) => {
  const actual = await importOriginal<typeof import('ant-design-vue')>();
  return {
    ...actual,
    Modal: { confirm: mocks.confirm },
    message: mocks.message,
  };
});
vi.mock('#/api/freqtrade', () => ({
  getConfig: mocks.config,
  getCount: mocks.count,
  postStartBot: mocks.start,
  postStopBot: mocks.stop,
  postStopBuy: mocks.stopbuy,
  postReloadConfig: mocks.reload,
  postForceExit: mocks.exit,
}));
vi.mock('#/views/quant/utils/useFtWs', () => ({
  useFtWs: () => ({ connected: { value: false } }),
  onFtWsMessage: mocks.ws,
}));
import BotControls from '../BotControls.vue';
const mounted: { app: App; el: HTMLDivElement }[] = [];
async function open(
  config: unknown = { dry_run: true, state: 'running' },
  count: unknown = { current: 2, max: 10 },
) {
  mocks.config.mockReset().mockResolvedValue(config);
  mocks.count.mockReset().mockResolvedValue(count);
  for (const action of [
    mocks.start,
    mocks.stop,
    mocks.stopbuy,
    mocks.reload,
    mocks.exit,
  ])
    action.mockReset().mockResolvedValue({ result: 'Order created' });
  mocks.confirm.mockReset();
  mocks.message.info.mockReset();
  mocks.message.warning.mockReset();
  mocks.message.error.mockReset();
  mocks.ws.mockReset().mockReturnValue(mocks.off);
  const el = document.createElement('div');
  document.body.append(el);
  const app = createApp({ render: () => h(BotControls) });
  app.component('Button', AntButton);
  app.component('Alert', AntAlert);
  app.component('Tag', AntTag);
  app.mount(el);
  mounted.push({ app, el });
  await vi.waitFor(() => expect(mocks.config).toHaveBeenCalledTimes(1));
  await nextTick();
  return { el };
}
afterEach(() =>
  mounted.splice(0).forEach(({ app, el }) => {
    app.unmount();
    el.remove();
  }),
);
describe('旧引擎控制条', () => {
  it('shows explicit source environment and never writes automatically on mount or receipt', async () => {
    const { el } = await open();
    expect(el.textContent).toContain('旧引擎 · 模拟盘 dry-run');
    expect(mocks.exit).not.toHaveBeenCalled();
    expect(mocks.stop).not.toHaveBeenCalled();
    expect(mocks.start).not.toHaveBeenCalled();
  });
  it('keeps writes disabled with missing environment or malformed count', async () => {
    const { el } = await open({ state: 'running' }, { current: '2', max: 10 });
    expect(el.textContent).toContain('环境未知');
    const exit = Array.from(el.querySelectorAll('button')).find((button) =>
      button.textContent?.includes('全部平仓'),
    )!;
    expect(exit.disabled).toBe(true);
    exit.click();
    await nextTick();
    expect(mocks.confirm).not.toHaveBeenCalled();
  });
  it('confirms explicit market all-exit request and says it awaits fill verification', async () => {
    const { el } = await open(
      { dry_run: false, state: 'running' },
      { current: 2, max: 10 },
    );
    expect(el.textContent).toContain('旧引擎 · 实盘配置');
    const exit = Array.from(el.querySelectorAll('button')).find((button) =>
      button.textContent?.includes('全部平仓'),
    )!;
    exit.click();
    expect(mocks.confirm).toHaveBeenCalledTimes(1);
    const confirmation = mocks.confirm.mock.calls[0]![0];
    expect(confirmation.content).toContain('命令回执不代表交易所已成交');
    await confirmation.onOk();
    expect(mocks.exit).toHaveBeenCalledWith({
      ordertype: 'market',
      tradeid: 'all',
    });
    expect(mocks.message.info.mock.calls[0]?.[0]).toContain(
      '成交待后续查询核验',
    );
  });
  it('does not report a rejected or malformed response as an accepted command', async () => {
    const { el } = await open();
    mocks.exit.mockResolvedValueOnce({
      result: 'rejected',
      error: 'postgres://secret',
    });
    el.querySelector<HTMLButtonElement>('button:last-child')!.click();
    const confirmation = mocks.confirm.mock.calls[0]![0];
    await confirmation.onOk();
    expect(mocks.exit).toHaveBeenCalledTimes(1);
    expect(mocks.message.info).not.toHaveBeenCalled();
    expect(mocks.message.warning.mock.calls[0]?.[0]).toContain('结果未知');
    expect(JSON.stringify(mocks.message.warning.mock.calls)).not.toContain(
      'secret',
    );
  });
  it('allows a confirmed stop command when the count endpoint is malformed', async () => {
    const { el } = await open(
      { dry_run: true, state: 'running' },
      { current: '2', max: 10 },
    );
    expect(
      el.querySelector<HTMLButtonElement>('button[data-command=forceexit]')
        ?.disabled,
    ).toBe(true);
    const stop = el.querySelector<HTMLButtonElement>(
      'button[data-command=stop]',
    )!;
    expect(stop.disabled).toBe(false);
    stop.click();
    const confirmation = mocks.confirm.mock.calls[0]![0];
    await confirmation.onOk();
    expect(mocks.stop).toHaveBeenCalledTimes(1);
  });
});
