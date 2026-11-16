import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';
import type { Component } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

const boundary = vi.hoisted(() => ({ login: vi.fn(), reset: vi.fn() }));
vi.mock('#/store', () => ({
  useAuthStore: () => ({ authLogin: boundary.login, loginLoading: false }),
}));
vi.mock('#/api/core/auth', () => ({ resetPasswordApi: boundary.reset }));
import AuthLayout from '../../../../layouts/auth.vue';
import Login from '../login.vue';
import CodeLogin from '../code-login.vue';
import QrcodeLogin from '../qrcode-login.vue';
import ForgetPassword from '../forget-password.vue';
import Register from '../register.vue';

const cleanups: (() => void)[] = [];
afterEach(() => {
  cleanups.splice(0).forEach((cleanup) => cleanup());
});
beforeEach(() => {
  sessionStorage.clear();
  vi.clearAllMocks();
  boundary.login.mockResolvedValue(undefined);
  boundary.reset.mockResolvedValue({
    generated: true,
    password: 'one-time-local-secret',
    username: 'admin',
  });
});

async function mount(component: Component, path = '/auth/login') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/auth/login', component: Login },
      { path: '/auth/unsupported', component },
      { path: '/quant/trade-desk', component: { template: '<p>desk</p>' } },
    ],
  });
  await router.push(path);
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(component);
  app.use(router).mount(element);
  cleanups.push(() => {
    app.unmount();
    element.remove();
  });
  await flush();
  return { element, router, app };
}
async function flush() {
  await Promise.resolve();
  await nextTick();
  await Promise.resolve();
  await nextTick();
}
function fill(element: Element, selector: string, value: string) {
  const input = element.querySelector<HTMLInputElement>(selector);
  expect(input).not.toBeNull();
  input!.value = value;
  input!.dispatchEvent(new Event('input', { bubbles: true }));
}
function submit(element: Element, selector = 'form') {
  const form = element.querySelector(selector);
  expect(form).not.toBeNull();
  form!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
}
function button(element: Element, text: string) {
  const result = [...element.querySelectorAll('button')].find((item) =>
    item.textContent?.includes(text),
  );
  expect(result).toBeDefined();
  return result!;
}

describe('real workstation authentication', () => {
  it('submits the typed credentials and preserves the original target for the existing store and router', async () => {
    sessionStorage.setItem('ft_manual_logout', '1');
    const { element, router } = await mount(
      Login,
      '/auth/login?redirect=%2Fquant%2Facceptance',
    );
    fill(element, '[autocomplete=username]', 'operator');
    fill(element, '[autocomplete=current-password]', 'unchanged-input');
    submit(element);
    await flush();
    expect(boundary.login).toHaveBeenCalledExactlyOnceWith({
      username: 'operator',
      password: 'unchanged-input',
    });
    expect(router.currentRoute.value.query.redirect).toBe('/quant/acceptance');
  });

  it('shows credential failure and clears the failed password without claiming success', async () => {
    sessionStorage.setItem('ft_manual_logout', '1');
    boundary.login.mockRejectedValueOnce(new Error('用户名或密码错误'));
    const { element } = await mount(Login);
    fill(element, '[autocomplete=username]', 'operator');
    fill(element, '[autocomplete=current-password]', 'bad-password');
    submit(element);
    await flush();
    expect(element.querySelector('[role=alert]')?.textContent).toContain(
      '用户名或密码错误',
    );
    expect(
      element.querySelector<HTMLInputElement>(
        '[autocomplete=current-password]',
      )!.value,
    ).toBe('');
    expect(element.textContent).not.toContain('登录成功');
  });

  it.each(['ft_manual_logout', 'ft_session_expired'])(
    'respects %s and requires an explicit local login',
    async (flag) => {
      sessionStorage.setItem(flag, '1');
      const { element } = await mount(Login);
      expect(boundary.login).not.toHaveBeenCalled();
      if (flag === 'ft_session_expired')
        expect(element.textContent).toContain('登录已失效');
      button(element, '本机免密登录').click();
      await flush();
      expect(boundary.login).toHaveBeenCalledExactlyOnceWith({ auto: true });
    },
  );

  it('tries local automatic login once and keeps the real form if it is unavailable', async () => {
    boundary.login.mockRejectedValueOnce(new Error('unavailable'));
    const { element } = await mount(Login);
    expect(boundary.login).toHaveBeenCalledExactlyOnceWith({ auto: true });
    expect(
      element.querySelector('[autocomplete=current-password]'),
    ).not.toBeNull();
    expect(element.textContent).not.toContain('登录成功');
  });

  it('does not reset until explicitly submitted, then removes the generated secret on return', async () => {
    sessionStorage.setItem('ft_manual_logout', '1');
    const { element } = await mount(Login);
    button(element, '本机恢复密码').click();
    await flush();
    expect(boundary.reset).not.toHaveBeenCalled();
    fill(element, '[name=recovery-username]', 'operator');
    boundary.reset.mockResolvedValueOnce({
      generated: true,
      password: 'one-time-local-secret',
      username: 'operator',
    });
    submit(element, '[aria-label=本机恢复密码]');
    await flush();
    expect(boundary.reset).toHaveBeenCalledExactlyOnceWith('operator');
    expect(element.textContent).toContain('one-time-local-secret');
    button(element, '返回登录').click();
    await flush();
    expect(element.textContent).not.toContain('one-time-local-secret');
  });

  it('opens local recovery from a supported explicit link without issuing a request', async () => {
    const { element } = await mount(
      Login,
      '/auth/login?recovery=local&redirect=%2Fquant%2Facceptance',
    );
    expect(element.querySelector('[aria-label=本机恢复密码]')).not.toBeNull();
    expect(boundary.login).not.toHaveBeenCalled();
    expect(boundary.reset).not.toHaveBeenCalled();
  });
});

describe('recovery identity and local secret lifecycle', () => {
  it('keeps the explicitly entered username while opening local recovery', async () => {
    sessionStorage.setItem('ft_manual_logout', '1');
    const { element, router } = await mount(Login);
    fill(element, '[autocomplete=username]', 'operator');
    button(element, '本机恢复密码').click();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.query.recovery).toBe('local'),
    );
    expect(
      element.querySelector<HTMLInputElement>('[name=recovery-username]')
        ?.value,
    ).toBe('operator');
    boundary.reset.mockResolvedValueOnce({
      generated: true,
      username: 'operator',
      password: 'operator-local-secret',
    });
    submit(element);
    await flush();
    expect(boundary.reset).toHaveBeenCalledExactlyOnceWith('operator');
    expect(element.textContent).toContain('operator-local-secret');
  });
  it.each([
    {
      username: 'other-operator',
      generated: true,
      password: 'untrusted-result',
    },
    { username: 'admin', generated: false, password: 'untrusted-result' },
    { username: 'admin', generated: true, password: '' },
    { username: 'admin', generated: true, password: 'x'.repeat(257) },
    {
      username: 'admin',
      generated: true,
      password: 'invalid' + String.fromCharCode(10) + 'secret',
    },
  ])(
    'refuses an incomplete or mismatched recovery receipt %#',
    async (result) => {
      boundary.reset.mockResolvedValueOnce(result);
      const { element } = await mount(Login, '/auth/login?recovery=local');
      submit(element);
      await flush();
      expect(element.textContent).not.toContain('新密码已生成');
      expect(element.querySelector('output')).toBeNull();
      expect(element.querySelector('[role=alert]')?.textContent).toContain(
        '无法确认',
      );
    },
  );

  it.each([
    { response: { data: { detail: 'password=never-render-me' } } },
    new Error('postgres://operator:never-render-me@example.invalid/db'),
  ])(
    'uses a safe recovery error without rendering arbitrary diagnostics %#',
    async (failure) => {
      boundary.reset.mockRejectedValueOnce(failure);
      const { element } = await mount(Login, '/auth/login?recovery=local');
      submit(element);
      await flush();
      expect(element.textContent).not.toContain('never-render-me');
      expect(element.querySelector('[role=alert]')?.textContent).toContain(
        '恢复未完成',
      );
    },
  );

  it('clears a recovery result when the same login route leaves recovery mode', async () => {
    const { element, router } = await mount(
      AuthLayout,
      '/auth/login?recovery=local',
    );
    submit(element);
    await flush();
    expect(element.textContent).toContain('one-time-local-secret');
    await router.push('/auth/login');
    await flush();
    expect(element.textContent).not.toContain('one-time-local-secret');
    expect(
      element.querySelector('[autocomplete=current-password]'),
    ).not.toBeNull();
  });

  it('removes the recovery query when returning and retains the original target', async () => {
    const { element, router } = await mount(
      Login,
      '/auth/login?recovery=local&redirect=%2Fquant%2Facceptance',
    );
    button(element, '返回登录').click();
    await flush();
    await vi.waitFor(() =>
      expect(router.currentRoute.value.query.recovery).toBeUndefined(),
    );
    expect(router.currentRoute.value.query.redirect).toBe('/quant/acceptance');
  });

  it('clears an in-flight password immediately on unmount', async () => {
    sessionStorage.setItem('ft_manual_logout', '1');
    let resolveLogin!: () => void;
    boundary.login.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          resolveLogin = resolve;
        }),
    );
    const { element, app } = await mount(Login);
    fill(element, '[autocomplete=current-password]', 'pending-local-secret');
    submit(element);
    await flush();
    const state = (
      app._instance as unknown as { setupState: Record<string, unknown> }
    ).setupState;
    expect(state.password).toBe('pending-local-secret');
    app.unmount();
    expect(state.password).toBe('');
    resolveLogin();
    await flush();
    expect(state.password).toBe('');
  });

  it('does not retain a late recovery secret after unmount', async () => {
    let resolveReset!: (value: unknown) => void;
    boundary.reset.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveReset = resolve;
        }),
    );
    const { element, app } = await mount(Login, '/auth/login?recovery=local');
    submit(element);
    await flush();
    const state = (
      app._instance as unknown as { setupState: Record<string, unknown> }
    ).setupState;
    app.unmount();
    resolveReset({
      generated: true,
      username: 'admin',
      password: 'late-local-secret',
    });
    await flush();
    expect(state.resetResult).toBeNull();
  });

  it('does not publish a recovery receipt after its route scope has changed', async () => {
    let resolveReset!: (value: unknown) => void;
    boundary.reset.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveReset = resolve;
        }),
    );
    const { element, router } = await mount(
      AuthLayout,
      '/auth/login?recovery=local',
    );
    submit(element);
    await flush();
    await router.push('/auth/login');
    await flush();
    resolveReset({
      generated: true,
      username: 'admin',
      password: 'late-route-secret',
    });
    await flush();
    expect(element.textContent).not.toContain('late-route-secret');
    expect(element.querySelector('output')).toBeNull();
  });
});

describe('retired authentication methods', () => {
  it.each([
    ['code', CodeLogin],
    ['QR', QrcodeLogin],
    ['email', ForgetPassword],
    ['register', Register],
  ] as const)(
    '%s provides real login links instead of an inert form or synthetic code',
    async (_, component) => {
      const { element } = await mount(
        component,
        '/auth/unsupported?redirect=%2Fquant%2Facceptance',
      );
      expect(element.textContent).toContain('当前不支持');
      expect(element.querySelector('form, input, canvas, img')).toBeNull();
      const links = [...element.querySelectorAll<HTMLAnchorElement>('a')];
      expect(
        links.some(
          (link) =>
            link.href.includes('/auth/login') &&
            link.href.includes('redirect='),
        ),
      ).toBe(true);
      expect(links.some((link) => link.href.includes('recovery=local'))).toBe(
        true,
      );
      expect(boundary.login).not.toHaveBeenCalled();
      expect(boundary.reset).not.toHaveBeenCalled();
    },
  );
});

describe('workstation access layout', () => {
  it('renders the real selected authentication route with a clear account boundary', async () => {
    sessionStorage.setItem('ft_manual_logout', '1');
    const { element } = await mount(AuthLayout);
    expect(element.querySelector('[aria-label=用户名密码登录]')).not.toBeNull();
    expect(element.textContent).toContain('账户风险');
    expect(element.textContent).toContain('登录身份不代表交易账户');
  });
});
