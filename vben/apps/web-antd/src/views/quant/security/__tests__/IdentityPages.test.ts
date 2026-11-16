import type { App } from 'vue';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

const api = vi.hoisted(() => ({
  change: vi.fn(),
  reset: vi.fn(),
  logout: vi.fn(),
  user: {
    userInfo: {
      username: 'operator',
      realName: '值班员',
      roles: ['admin'],
      desc: '运行维护',
    },
  },
}));
vi.mock('#/api/core/auth', () => ({
  changePasswordApi: api.change,
  resetPasswordApi: api.reset,
  logoutApi: api.logout,
}));
vi.mock('@vben/stores', () => ({ useUserStore: () => api.user }));

import Security from '../index.vue';
import Profile from '../../../_core/profile/index.vue';

const cleanups: { app: App; element: HTMLDivElement }[] = [];
afterEach(() => {
  cleanups.splice(0).forEach(({ app, element }) => {
    app.unmount();
    element.remove();
  });
});
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  api.change.mockResolvedValue({ message: 'ok' });
  api.reset.mockResolvedValue({
    generated: true,
    password: 'one-use-secret',
    username: 'operator',
  });
});
async function flush() {
  await Promise.resolve();
  await nextTick();
  await Promise.resolve();
  await nextTick();
}
async function mount(component: typeof Security | typeof Profile) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/quant/trade-desk', component: { template: '<div />' } },
      { path: '/profile', component: { template: '<div />' } },
      { path: '/quant/security', component: Security },
    ],
  });
  await router.push('/quant/security');
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(component);
  app.use(router).mount(element);
  cleanups.push({ app, element });
  await flush();
  return element;
}
function fill(element: Element, name: string, value: string) {
  const input = element.querySelector<HTMLInputElement>(`[name=${name}]`);
  expect(input).not.toBeNull();
  input!.value = value;
  input!.dispatchEvent(new Event('input', { bubbles: true }));
}
function submit(element: Element) {
  element
    .querySelector('form')!
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
}
function button(element: Element, text: string) {
  const target = [
    ...element.querySelectorAll<HTMLButtonElement>('button'),
  ].find((item) => item.textContent?.includes(text));
  expect(target).toBeDefined();
  return target!;
}

describe('identity workspaces', () => {
  it('shows the current login identity without issuing a password command', async () => {
    const element = await mount(Security);
    expect(element.textContent).toContain('operator');
    expect(element.textContent).toContain('登录身份');
    expect(element.textContent).toContain('交易账户');
    expect(api.change).not.toHaveBeenCalled();
    expect(api.reset).not.toHaveBeenCalled();
  });

  it('validates the real form before changing and clears sensitive fields on success', async () => {
    const element = await mount(Security);
    fill(element, 'oldPassword', 'current-value');
    fill(element, 'newPassword', 'next-value');
    fill(element, 'confirm', 'different');
    submit(element);
    await flush();
    expect(api.change).not.toHaveBeenCalled();
    expect(element.querySelector('[role=alert]')?.textContent).toContain(
      '不一致',
    );
    fill(element, 'confirm', 'next-value');
    submit(element);
    await flush();
    expect(api.change).toHaveBeenCalledExactlyOnceWith(
      'current-value',
      'next-value',
    );
    expect(element.textContent).toContain('密码已修改');
    expect(element.querySelector('input[type=password]')).toBeNull();
  });

  it('keeps a failed change distinct from success and omits raw upstream secrets', async () => {
    api.change.mockRejectedValue(new Error('current-value private-token'));
    const element = await mount(Security);
    fill(element, 'oldPassword', 'current-value');
    fill(element, 'newPassword', 'next-value');
    fill(element, 'confirm', 'next-value');
    submit(element);
    await flush();
    expect(element.querySelector('[role=alert]')?.textContent).toContain(
      '未收到',
    );
    expect(element.textContent).not.toContain('private-token');
    expect(element.textContent).not.toContain('密码已修改');
  });

  it('requires local-reset acknowledgement and clears its revealed result explicitly', async () => {
    localStorage.setItem('ft_access_token', 'old-session');
    const element = await mount(Security);
    const reset = button(element, '生成随机新密码');
    expect(reset.disabled).toBe(true);
    const acknowledgement = element.querySelector<HTMLInputElement>(
      '[name=confirmRecovery]',
    )!;
    expect(acknowledgement).not.toBeNull();
    acknowledgement.checked = true;
    acknowledgement.dispatchEvent(new Event('change', { bubbles: true }));
    await flush();
    button(element, '生成随机新密码').click();
    await flush();
    expect(api.reset).toHaveBeenCalledExactlyOnceWith('operator');
    expect(localStorage.getItem('ft_access_token')).toBeNull();
    expect(sessionStorage.getItem('ft_manual_logout')).toBe('1');
    const secret = element.querySelector<HTMLInputElement>(
      '[aria-label=本次恢复密码]',
    );
    expect(secret?.type).toBe('password');
    expect(secret?.value).toBe('one-use-secret');
    button(element, '显示本次密码').click();
    await flush();
    expect(
      element.querySelector<HTMLInputElement>('[aria-label=本次恢复密码]')
        ?.type,
    ).toBe('text');
    button(element, '清除本次密码').click();
    await flush();
    expect(element.querySelector('[aria-label=本次恢复密码]')).toBeNull();
    expect(element.textContent).not.toContain('one-use-secret');
  });

  it('keeps profile tabs functional and unsupported security features honest', async () => {
    const element = await mount(Profile);
    expect(element.textContent).toContain('operator');
    button(element, '安全状态').click();
    await flush();
    expect(element.textContent).toContain('MFA');
    expect(element.textContent).toContain('暂不提供');
    expect(element.querySelector('[role=switch]')).toBeNull();
    button(element, '修改密码').click();
    await flush();
    fill(element, 'oldPassword', 'current-value');
    fill(element, 'newPassword', 'next-value');
    fill(element, 'confirm', 'next-value');
    submit(element);
    await flush();
    expect(api.change).toHaveBeenCalledExactlyOnceWith(
      'current-value',
      'next-value',
    );
    expect(api.reset).not.toHaveBeenCalled();
  });
});
