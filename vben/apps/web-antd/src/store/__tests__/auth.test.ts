import { LOGIN_PATH } from '@vben/constants';

import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { FT_SESSION_EXPIRED_KEY, FT_TOKEN_KEY } from '../../api/session';
import { useAuthStore } from '../auth';

const mocks = vi.hoisted(() => ({
  accessStore: {
    setAccessCodes: vi.fn(),
    setAccessToken: vi.fn(),
    setLoginExpired: vi.fn(),
  },
  currentRoute: {
    value: {} as {
      fullPath: string;
      path: string;
      query?: Record<string, string>;
    },
  },
  getAccessCodesApi: vi.fn(),
  getUserInfoApi: vi.fn(),
  loginApi: vi.fn(),
  logoutApi: vi.fn(),
  resetAllStores: vi.fn(),
  routerPush: vi.fn(),
  routerReplace: vi.fn(),
  userStore: { setUserInfo: vi.fn() },
}));

vi.mock('vue-router', () => ({
  useRouter: () => ({
    currentRoute: mocks.currentRoute,
    push: mocks.routerPush,
    replace: mocks.routerReplace,
  }),
}));
vi.mock('@vben/stores', () => ({
  resetAllStores: mocks.resetAllStores,
  useAccessStore: () => mocks.accessStore,
  useUserStore: () => mocks.userStore,
}));
vi.mock('@vben/preferences', () => ({
  preferences: { app: { defaultHomePath: '/quant/demo-preview' } },
}));
vi.mock('ant-design-vue', () => ({ notification: { success: vi.fn() } }));
vi.mock('#/api', () => ({
  getAccessCodesApi: mocks.getAccessCodesApi,
  getUserInfoApi: mocks.getUserInfoApi,
  loginApi: mocks.loginApi,
  logoutApi: mocks.logoutApi,
}));
vi.mock('#/locales', () => ({ $t: (key: string) => key }));

describe('expired session navigation', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    mocks.currentRoute.value = {
      fullPath: '/quant/strategy-lab?strategy=s23',
      path: '/quant/strategy-lab',
      query: {},
    };
  });

  it('clears both token stores and redirects without calling the unavailable logout API', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'expired-token');
    mocks.logoutApi.mockImplementation(() => new Promise(() => {}));

    await useAuthStore().expireSession();

    expect(localStorage.getItem(FT_TOKEN_KEY)).toBeNull();
    expect(mocks.accessStore.setAccessToken).toHaveBeenCalledWith(null);
    expect(mocks.resetAllStores).toHaveBeenCalledOnce();
    expect(mocks.logoutApi).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(FT_SESSION_EXPIRED_KEY)).toBe('1');
    expect(mocks.routerReplace).toHaveBeenCalledWith({
      path: LOGIN_PATH,
      query: {
        redirect: encodeURIComponent('/quant/strategy-lab?strategy=s23'),
      },
    });
  });

  it('does not nest login redirects when already on the login page', async () => {
    mocks.currentRoute.value = {
      fullPath: `${LOGIN_PATH}?redirect=%2Fquant`,
      path: LOGIN_PATH,
    };
    await useAuthStore().expireSession();

    expect(mocks.routerReplace).toHaveBeenCalledWith({
      path: LOGIN_PATH,
      query: {},
    });
  });

  it('returns to the requested internal page after login instead of the demo home', async () => {
    mocks.currentRoute.value = {
      fullPath: `${LOGIN_PATH}?redirect=%2Fquant%2Ftrade-desk%3Fsymbol%3DBTCUSDT`,
      path: LOGIN_PATH,
      query: { redirect: '%2Fquant%2Ftrade-desk%3Fsymbol%3DBTCUSDT' },
    };
    mocks.loginApi.mockResolvedValue({ accessToken: 'synthetic-token' });
    mocks.getUserInfoApi.mockResolvedValue({
      homePath: '/quant/demo-preview',
    });
    mocks.getAccessCodesApi.mockResolvedValue([]);

    await useAuthStore().authLogin({ username: 'operator', password: 'secret' });

    expect(mocks.routerPush).toHaveBeenCalledExactlyOnceWith(
      '/quant/trade-desk?symbol=BTCUSDT',
    );
  });

  it('ignores an external login redirect and uses the local demo home', async () => {
    mocks.currentRoute.value = {
      fullPath: `${LOGIN_PATH}?redirect=https%3A%2F%2Fattacker.example`,
      path: LOGIN_PATH,
      query: { redirect: 'https://attacker.example' },
    };
    mocks.loginApi.mockResolvedValue({ accessToken: 'synthetic-token' });
    mocks.getUserInfoApi.mockResolvedValue({
      homePath: '/quant/demo-preview',
    });
    mocks.getAccessCodesApi.mockResolvedValue([]);

    await useAuthStore().authLogin({ username: 'operator', password: 'secret' });

    expect(mocks.routerPush).toHaveBeenCalledExactlyOnceWith(
      '/quant/demo-preview',
    );
  });
});
