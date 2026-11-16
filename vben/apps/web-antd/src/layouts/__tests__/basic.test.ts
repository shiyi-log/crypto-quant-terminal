import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createApp,
  defineComponent,
  h,
  nextTick,
  onMounted,
  onUnmounted,
} from 'vue';
import { createMemoryHistory, createRouter } from 'vue-router';

const effects = vi.hoisted(() => ({ pollerStarts: 0, pollerStops: 0 }));

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
// Mock at the external polling boundary: the real shell decides whether it is mounted.
vi.mock('#/views/quant/components/BotControls.vue', () => ({
  default: defineComponent({
    setup() {
      onMounted(() => {
        effects.pollerStarts += 1;
      });
      onUnmounted(() => {
        effects.pollerStops += 1;
      });
      return () => h('button', '旧引擎写命令');
    },
  }),
}));

import Basic from '../basic.vue';

beforeEach(() => {
  effects.pollerStarts = 0;
  effects.pollerStops = 0;
});

async function run(
  path: string,
  action: (
    router: ReturnType<typeof createRouter>,
    element: HTMLDivElement,
  ) => Promise<void>,
) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: ['trade-desk', 'acceptance', 'overview', 'ops', 'binance-demo'].map(
      (view) => ({
        path: '/quant/' + view,
        name: (
          {
            'trade-desk': 'QuantTradeDesk',
            acceptance: 'QuantAcceptance',
            overview: 'QuantOverview',
            ops: 'QuantOps',
            'binance-demo': 'QuantBinanceDemo',
          } as Record<string, string>
        )[view],
        component: { template: '<div />' },
      }),
    ),
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
  it.each(['trade-desk', 'acceptance', 'binance-demo', 'ops'])(
    '%s never mounts the legacy command poller',
    async (view) => {
      await run('/quant/' + view, async (_, element) => {
        await nextTick();
        expect(effects.pollerStarts).toBe(0);
        expect(element.textContent).not.toContain('旧引擎写命令');
        expect(element.textContent).not.toContain('web-login-identity');
      });
    },
  );

  it('stops the legacy poller before entering a Demo account view', async () => {
    await run('/quant/overview', async (router, element) => {
      await nextTick();
      expect(effects.pollerStarts).toBe(1);
      expect(element.textContent).toContain('旧交易引擎操作');
      await router.push('/quant/trade-desk');
      await nextTick();
      expect(effects.pollerStops).toBe(1);
      expect(element.textContent).not.toContain('旧引擎写命令');
      expect(element.querySelector('[role="status"]')?.textContent).toContain(
        'Binance Demo',
      );
    });
  });
});
