import { describe, expect, it, vi } from 'vitest';
import { createApp, defineComponent, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

vi.mock('@vben/common-ui', () => ({
  AuthenticationLoginExpiredModal: {
    template: '<div data-testid="login-expired-modal"><slot /></div>',
  },
}));
vi.mock('@vben/hooks', () => ({
  useWatermark: () => ({ destroyWatermark() {}, updateWatermark() {} }),
}));
vi.mock('@vben/layouts', () => ({
  BasicLayout: defineComponent({
    setup(_, { slots }) {
      return () =>
        h('main', [
          slots['user-dropdown']?.(),
          slots.extra?.(),
          slots['lock-screen']?.(),
        ]);
    },
  }),
  LockScreen: { template: '<div data-testid="lock-screen" />' },
  UserDropdown: { template: '<div data-testid="user-dropdown" />' },
}));
vi.mock('@vben/preferences', () => ({
  preferences: {
    app: { defaultAvatar: '', watermark: false, watermarkContent: '' },
  },
  usePreferences: () => ({ isDark: false }),
}));
vi.mock('@vben/stores', () => ({
  useAccessStore: () => ({ loginExpired: false }),
  useUserStore: () => ({ userInfo: { username: 'web-login-identity' } }),
}));
vi.mock('ant-design-vue', () => ({
  Card: defineComponent({
    setup(_, { slots }) {
      return () => h('section', slots.default?.());
    },
  }),
}));
vi.mock('#/store', () => ({ useAuthStore: () => ({ logout() {} }) }));
vi.mock('#/locales', () => ({ $t: (key: string) => key }));
vi.mock('#/views/_core/authentication/login.vue', () => ({
  default: { template: '<div />' },
}));
import Basic from '../basic.vue';

async function run(
  path: string,
  action: (
    router: ReturnType<typeof createRouter>,
    element: HTMLDivElement,
  ) => Promise<void>,
) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }],
  });
  await router.push(path);
  await router.isReady();
  const element = document.createElement('div');
  document.body.append(element);
  const app = createApp(Basic);
  app.use(router).mount(element);
  try {
    await action(router, element);
  } finally {
    app.unmount();
    element.remove();
  }
}

describe('通用应用布局', () => {
  it('旧量化深链接不会挂载旧量化页头或工作区主题', async () => {
    await run('/quant/trade-desk', async (_, element) => {
      await nextTick();
      expect(element.textContent).not.toContain('旧引擎写命令');
      expect(element.querySelector('.quant-workspace-shell')).toBeNull();
      expect(element.querySelector('.quant-shell-legacy')).toBeNull();
      expect(
        element.querySelector('[data-testid="user-dropdown"]'),
      ).not.toBeNull();
      expect(
        element.querySelector('[data-testid="login-expired-modal"]'),
      ).not.toBeNull();
      expect(
        element.querySelector('[data-testid="lock-screen"]'),
      ).not.toBeNull();
    });
  });
});
