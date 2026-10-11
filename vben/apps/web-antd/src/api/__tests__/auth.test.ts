import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getMeApi, loginApi, refreshTokenApi } from '../core/auth';
import { getUserInfoApi } from '../core/user';
import { FT_SESSION_EXPIRED_KEY, FT_TOKEN_KEY } from '../session';

const mocks = vi.hoisted(() => ({ expireSession: vi.fn(), fetch: vi.fn() }));

vi.mock('#/api/freqtrade', () => ({
  FT_API_URL: 'http://127.0.0.1:8890/api',
  FT_SERVER_ORIGIN: 'http://127.0.0.1:8890',
  FT_TOKEN_KEY: 'ft_access_token',
  ftLogout: vi.fn(),
}));
vi.mock('#/store', () => ({
  useAuthStore: () => ({ expireSession: mocks.expireSession }),
}));

function response(status: number, body: unknown) {
  return { json: async () => body, ok: status === 200, status, statusText: '' };
}

describe('local authentication errors', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    mocks.expireSession.mockResolvedValue(undefined);
    vi.stubGlobal('fetch', mocks.fetch);
  });
  afterEach(() => vi.unstubAllGlobals());

  it('propagates /auth/me 401 instead of inventing an authenticated admin user', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'expired-token');
    mocks.fetch.mockResolvedValue(response(401, { detail: '登录已过期' }));

    await expect(getUserInfoApi()).rejects.toMatchObject({
      response: { status: 401 },
    });

    expect(mocks.expireSession).toHaveBeenCalledOnce();
    expect(localStorage.getItem(FT_TOKEN_KEY)).toBeNull();
  });

  it('keeps credential errors on the login form without another expiry redirect', async () => {
    mocks.fetch.mockResolvedValue(
      response(401, { detail: '用户名或密码错误' }),
    );

    await expect(
      loginApi({ username: 'admin', password: 'wrong' }),
    ).rejects.toThrow('用户名或密码错误');

    expect(mocks.expireSession).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(FT_SESSION_EXPIRED_KEY)).toBeNull();
    expect(mocks.fetch).toHaveBeenCalledOnce();
  });

  it('ignores an old /auth/me 401 that arrives after a newer login', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'old-token');
    mocks.fetch.mockImplementation(async () => {
      localStorage.setItem(FT_TOKEN_KEY, 'new-token');
      return response(401, { detail: '旧会话过期' });
    });

    await expect(getMeApi()).rejects.toMatchObject({
      response: { status: 401 },
    });

    expect(localStorage.getItem(FT_TOKEN_KEY)).toBe('new-token');
    expect(mocks.expireSession).not.toHaveBeenCalled();
  });

  it('lets an explicit successful login clear both automatic-login gates', async () => {
    sessionStorage.setItem(FT_SESSION_EXPIRED_KEY, '1');
    sessionStorage.setItem('ft_manual_logout', '1');
    mocks.fetch.mockResolvedValue(
      response(200, { access_token: 'new-token', username: 'admin' }),
    );

    await expect(loginApi({ auto: true })).resolves.toMatchObject({
      accessToken: 'new-token',
    });

    expect(localStorage.getItem(FT_TOKEN_KEY)).toBe('new-token');
    expect(sessionStorage.getItem(FT_SESSION_EXPIRED_KEY)).toBeNull();
    expect(sessionStorage.getItem('ft_manual_logout')).toBeNull();
  });

  it('never returns an expired token as a pretend refresh result', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'expired-token');
    await expect(refreshTokenApi()).rejects.toThrow('请重新登录');
    expect(mocks.fetch).not.toHaveBeenCalled();
  });
});
