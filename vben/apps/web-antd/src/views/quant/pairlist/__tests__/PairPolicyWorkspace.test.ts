import type { App } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick } from 'vue';
import PairPolicyWorkspace from '../PairPolicyWorkspace.vue';
import { createPairPolicy } from '../pairPolicy';

const instances: { app: App; element: HTMLDivElement }[] = [];
const black = (pairs = ['ETH/USDT:USDT']) => ({
  blacklist: pairs,
  blacklist_expanded: ['ETH/USDT:USDT'],
  errors: {},
  length: pairs.length,
  method: ['StaticPairList'],
});
function open() {
  const transport = {
    whitelist: vi.fn(async () => ({ whitelist: ['BTC/USDT:USDT'] })),
    blacklist: vi.fn(async () => black()),
    locks: vi.fn(async () => ({
      lock_count: 1,
      locks: [
        {
          id: 7,
          pair: 'BTC/USDT:USDT',
          side: 'long',
          active: true,
          reason: 'CooldownPeriod',
          lock_end_timestamp: Date.now() + 60_000,
        },
      ],
    })),
    add: vi.fn(async (_pairs: string[]) => black()),
    remove: vi.fn(async (_pairs: string[]) => black([])),
    unlock: vi.fn(async (_id: number) => ({ locks: [], lock_count: 0 })),
  };
  const policy = createPairPolicy(transport);
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp({ render: () => h(PairPolicyWorkspace, { policy }) });
  app.mount(element);
  instances.push({ app, element });
  return {
    element,
    policy,
    transport,
    get<T extends HTMLElement = HTMLElement>(selector: string): T {
      const result = element.querySelector<T>(selector);
      if (!result) throw new Error('Expected element ' + selector);
      return result;
    },
  };
}
afterEach(() =>
  instances.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  }),
);

describe('旧引擎名单工作台', () => {
  it('while a command is pending the modal keeps keyboard focus instead of tabbing into background', async () => {
    const view = open();
    await view.policy.refresh();
    await nextTick();
    let finish!: (value: { locks: never[]; lock_count: number }) => void;
    view.transport.unlock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    view.get('button[data-view=locks]').click();
    await nextTick();
    view.get('button[data-lock="7"]').click();
    await nextTick();
    view.get('[role=alertdialog] button[data-action=confirm]').click();
    await nextTick();
    const dialog = view.get('[role=alertdialog]');
    const event = new KeyboardEvent('keydown', {
      key: 'Tab',
      bubbles: true,
      cancelable: true,
    });
    dialog.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(dialog);
    finish({ locks: [], lock_count: 0 });
    await vi.waitFor(() =>
      expect(view.element.querySelector('[role=alertdialog]')).toBeNull(),
    );
  });
  it('returns focus to the invoking action after cancelling confirmation with Escape', async () => {
    const view = open();
    await view.policy.refresh();
    await nextTick();
    view.get('button[data-view=locks]').click();
    await nextTick();
    const trigger = view.get('button[data-lock="7"]');
    trigger.focus();
    trigger.click();
    await nextTick();
    const cancel = view.get('[role=alertdialog] button[data-action=cancel]');
    expect(document.activeElement).toBe(cancel);
    cancel.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    await nextTick();
    await nextTick();
    expect(view.element.querySelector('[role=alertdialog]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(view.transport.unlock).not.toHaveBeenCalled();
  });

  it('keeps the account boundary explicit and reads without issuing writes', async () => {
    const view = open();
    await view.policy.refresh();
    await nextTick();
    expect(view.element.textContent).toContain('旧交易引擎');
    expect(view.element.textContent).toContain('实际模拟 / 实盘模式未知');
    expect(view.element.textContent).toContain(
      '不会解除 Binance Demo 的账户冷却或风险锁',
    );
    expect(view.element.textContent).toContain('BTC/USDT:USDT');
    expect(view.transport.add).not.toHaveBeenCalled();
    expect(view.transport.remove).not.toHaveBeenCalled();
    expect(view.transport.unlock).not.toHaveBeenCalled();
  });
  it('requires an explicit confirmation for adding a new restriction', async () => {
    const view = open();
    await view.policy.refresh();
    await nextTick();
    view.get('button[data-view=blacklist]').click();
    await nextTick();
    const input = view.get<HTMLInputElement>(
      'input[aria-label="新增黑名单交易对"]',
    );
    input.value = 'TRX/USDT:USDT';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await nextTick();
    view.get('button[data-action=add]').click();
    await nextTick();
    expect(view.transport.add).not.toHaveBeenCalled();
    expect(view.get('[role=alertdialog]').textContent).toContain(
      'TRX/USDT:USDT',
    );
    view.get('[role=alertdialog] button[data-action=cancel]').click();
    await nextTick();
    expect(view.transport.add).not.toHaveBeenCalled();
    view.get('button[data-action=add]').click();
    await nextTick();
    view.get('[role=alertdialog] button[data-action=confirm]').click();
    await vi.waitFor(() =>
      expect(view.transport.add).toHaveBeenCalledWith(['TRX/USDT:USDT']),
    );
    await vi.waitFor(() =>
      expect(view.element.querySelector('[role=alertdialog]')).toBeNull(),
    );
    expect(view.element.textContent).toContain('已收到命令回执');
  });
  it('selects exact blacklist rows for bulk removal and preserves expansion details', async () => {
    const view = open();
    await view.policy.refresh();
    await nextTick();
    view.get('button[data-view=blacklist]').click();
    await nextTick();
    const checkbox = view.get<HTMLInputElement>(
      'input[type=checkbox][value="ETH/USDT:USDT"]',
    );
    checkbox.checked = true;
    checkbox.dispatchEvent(new Event('change', { bubbles: true }));
    await nextTick();
    view.get('button[data-action=remove]').click();
    await nextTick();
    view.get('[role=alertdialog] button[data-action=confirm]').click();
    await vi.waitFor(() =>
      expect(view.transport.remove).toHaveBeenCalledWith(['ETH/USDT:USDT']),
    );
    expect(view.element.textContent).toContain('展开后的实际生效名单');
  });
  it('confirms the selected legacy lock and never describes it as an account risk reset', async () => {
    const view = open();
    await view.policy.refresh();
    await nextTick();
    view.get('button[data-view=locks]').click();
    await nextTick();
    view.get('button[data-lock="7"]').click();
    await nextTick();
    expect(view.get('[role=alertdialog]').textContent).toContain(
      'BTC/USDT:USDT',
    );
    expect(view.transport.unlock).not.toHaveBeenCalled();
    view.get('[role=alertdialog] button[data-action=confirm]').click();
    await vi.waitFor(() =>
      expect(view.transport.unlock).toHaveBeenCalledWith(7),
    );
  });
  it('shows unknown data separately from a successfully read empty list', async () => {
    const view = open();
    expect(view.element.textContent).toContain('尚未读取');
    view.transport.whitelist.mockResolvedValueOnce({ whitelist: [] });
    await view.policy.refresh();
    await nextTick();
    expect(view.element.textContent).toContain('白名单为空');
    view.transport.whitelist.mockRejectedValueOnce(new Error('offline'));
    await view.policy.refresh();
    await nextTick();
    expect(view.element.textContent).toContain('保留上次');
  });
});
