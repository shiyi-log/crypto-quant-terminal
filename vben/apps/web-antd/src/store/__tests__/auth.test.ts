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
    value: { fullPath: '/quant/strategy-lab', path: '/quant/strategy-lab' },
  },
  logoutApi: vi.fn(),
  resetAllStores: vi.fn(),
  routerReplace: vi.fn(),
  userStore: { setUserInfo: vi.fn() },
}));

vi.mock('vue-router', () => ({
  useRouter: () => ({
    currentRoute: mocks.currentRoute,
    replace: mocks.routerReplace,
  }),
}));
vi.mock('@vben/stores', () => ({
  resetAllStores: mocks.resetAllStores,
  useAccessStore: () => mocks.accessStore,
  useUserStore: () => mocks.userStore,
}));
vi.mock('@vben/preferences', () => ({
  preferences: { app: { defaultHomePath: '/quant/overview' } },
}));
vi.mock('ant-design-vue', () => ({ notification: { success: vi.fn() } }));
vi.mock('#/api', () => ({
  getAccessCodesApi: vi.fn(),
  getUserInfoApi: vi.fn(),
  loginApi: vi.fn(),
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
});
