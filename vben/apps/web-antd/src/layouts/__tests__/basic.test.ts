import { describe, expect, it, vi } from 'vitest';
import { createApp, defineComponent, h, nextTick } from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

vi.mock('@vben/common-ui', () => ({
  AuthenticationLoginExpiredModal: { template: '<div />' },
}));
vi.mock('@vben/hooks', () => ({
  useWatermark: () => ({ destroyWatermark() {}, updateWatermark() {} }),
}));
vi.mock('@vben/layouts', () => ({
  BasicLayout: defineComponent({
    setup(_, { slots }) {
      return () => h('main', slots['content-top']?.());
    },
  }),
  LockScreen: { template: '<div />' },
  UserDropdown: { template: '<div />' },
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
    routes: [
      'trade-desk',
      'acceptance',
      'factors',
      'operations',
      'binance-demo',
    ].map((view) => ({
      path: '/quant/' + view,
      name: (
        {
          'trade-desk': 'QuantTradeDesk',
          acceptance: 'QuantAcceptance',
          factors: 'QuantFactors',
          operations: 'QuantOperations',
          'binance-demo': 'QuantBinanceDemo',
        } as Record<string, string>
      )[view],
      component: { template: '<div />' },
    })),
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

describe('application shell external command boundary', () => {
  it.each(['trade-desk', 'acceptance', 'binance-demo', 'operations'])(
    '%s never mounts the legacy command poller',
    async (view) => {
      await run('/quant/' + view, async (_, element) => {
        await nextTick();
        expect(element.textContent).not.toContain('旧引擎写命令');
        expect(element.textContent).not.toContain('web-login-identity');
      });
    },
  );

  it('切换新版工作区始终没有旧交易控制栏', async () => {
    await run('/quant/factors', async (router, element) => {
      await nextTick();
      expect(element.textContent).toContain('因子实验室');
      expect(element.querySelector('.quant-shell-legacy')).toBeNull();
      await router.push('/quant/trade-desk');
      await nextTick();
      expect(element.querySelector('.quant-shell-legacy')).toBeNull();
      expect(element.textContent).not.toContain('旧引擎写命令');
      expect(element.querySelector('[role="status"]')?.textContent).toContain(
        'Binance Demo',
      );
    });
  });
});
