import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ftClient } from '../freqtrade/client';
import { requestClient } from '../request';
import {
  FT_SESSION_EXPIRED_KEY,
  FT_TOKEN_KEY,
  handleSessionExpired,
  requireSession,
} from '../session';

const mocks = vi.hoisted(() => ({
  accessToken: undefined as null | string | undefined,
  expireSession: vi.fn(),
}));

vi.mock('@vben/hooks', () => ({
  useAppConfig: () => ({ apiURL: 'http://127.0.0.1:8890/api' }),
}));
vi.mock('#/store', () => ({
  useAuthStore: () => ({ expireSession: mocks.expireSession }),
}));
vi.mock('@vben/stores', () => ({
  useAccessStore: () => ({
    accessToken:
      mocks.accessToken === undefined
        ? localStorage.getItem('ft_access_token')
        : mocks.accessToken,
  }),
}));
vi.mock('@vben/preferences', () => ({
  preferences: {
    app: {
      enableRefreshToken: true,
      locale: 'zh-CN',
      loginExpiredMode: 'modal',
    },
  },
}));
vi.mock('@vben/locales', () => ({ $t: (key: string) => key }));
vi.mock('ant-design-vue', () => ({ message: { error: vi.fn() } }));

describe('local session expiry', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    vi.clearAllMocks();
    mocks.accessToken = undefined;
    mocks.expireSession.mockResolvedValue(undefined);
  });

  it('clears the FT token before waiting for the login-page transition', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'expired-token');
    let finish!: () => void;
    mocks.expireSession.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    const transition = handleSessionExpired('expired-token');

    expect(localStorage.getItem(FT_TOKEN_KEY)).toBeNull();
    expect(sessionStorage.getItem(FT_SESSION_EXPIRED_KEY)).toBe('1');
    await vi.waitFor(() => expect(mocks.expireSession).toHaveBeenCalledOnce());
    finish();
    await transition;
  });

  it('only transitions once for concurrent polling failures', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'expired-token');

    await Promise.all([
      handleSessionExpired('expired-token'),
      handleSessionExpired('expired-token'),
      handleSessionExpired('expired-token'),
    ]);

    expect(mocks.expireSession).toHaveBeenCalledOnce();
  });

  it('does not clear a new login when an old request returns 401', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'new-token');
    await handleSessionExpired('old-token');

    expect(localStorage.getItem(FT_TOKEN_KEY)).toBe('new-token');
    expect(sessionStorage.getItem(FT_SESSION_EXPIRED_KEY)).toBeNull();
    expect(mocks.expireSession).not.toHaveBeenCalled();
  });

  it('redirects a missing-token page instead of leaving it in a polling error state', async () => {
    expect(() => requireSession('请先登录')).toThrow('请先登录');
    await vi.waitFor(() => expect(mocks.expireSession).toHaveBeenCalledOnce());
    expect(sessionStorage.getItem(FT_SESSION_EXPIRED_KEY)).toBe('1');
  });
});

describe('quant request 401 handling', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    vi.clearAllMocks();
    mocks.accessToken = undefined;
    mocks.expireSession.mockResolvedValue(undefined);
  });

  it('handles an actual ftClient 401 without retrying the rejected token', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'expired-token');
    const adapter = vi.fn(async (config) => {
      throw { config, response: { status: 401 } };
    });
    ftClient.instance.defaults.adapter = adapter;

    await expect(ftClient.get('/locals/strategy-lab')).rejects.toMatchObject({
      response: { status: 401 },
    });

    expect(adapter).toHaveBeenCalledOnce();
    expect(mocks.expireSession).toHaveBeenCalledOnce();
    expect(localStorage.getItem(FT_TOKEN_KEY)).toBeNull();
  });

  it('does not redirect for ordinary API errors', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'valid-token');
    ftClient.instance.defaults.adapter = async (config) => {
      throw { config, response: { status: 500 } };
    };

    await expect(ftClient.get('/locals/strategy-lab')).rejects.toMatchObject({
      response: { status: 500 },
    });

    expect(localStorage.getItem(FT_TOKEN_KEY)).toBe('valid-token');
    expect(mocks.expireSession).not.toHaveBeenCalled();
  });

  it('uses the original request token when a delayed 401 arrives after re-login', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'old-token');
    ftClient.instance.defaults.adapter = async (config) => {
      expect(config.headers.Authorization).toBe('Bearer old-token');
      localStorage.setItem(FT_TOKEN_KEY, 'new-token');
      throw { config, response: { status: 401 } };
    };

    await expect(ftClient.get('/locals/strategy-lab')).rejects.toMatchObject({
      response: { status: 401 },
    });

    expect(localStorage.getItem(FT_TOKEN_KEY)).toBe('new-token');
    expect(mocks.expireSession).not.toHaveBeenCalled();
  });

  it('expires the common client session even with cached modal and refresh preferences', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'expired-token');
    const adapter = vi.fn(async (config) => {
      throw { config, response: { status: 401 } };
    });
    requestClient.instance.defaults.adapter = adapter;

    await expect(requestClient.get('/private')).rejects.toMatchObject({
      response: { status: 401 },
    });

    expect(adapter).toHaveBeenCalledOnce();
    expect(mocks.expireSession).toHaveBeenCalledOnce();
    expect(localStorage.getItem(FT_TOKEN_KEY)).toBeNull();
  });

  it.each(['ft-token', ''])(
    'expires a rejected Vben token even when FT token is %s',
    async (ftToken) => {
      mocks.accessToken = 'vben-token';
      if (ftToken) localStorage.setItem(FT_TOKEN_KEY, ftToken);
      requestClient.instance.defaults.adapter = async (config) => {
        expect(config.headers.Authorization).toBe('Bearer vben-token');
        throw { config, response: { status: 401 } };
      };

      await expect(requestClient.get('/private')).rejects.toMatchObject({
        response: { status: 401 },
      });

      expect(mocks.expireSession).toHaveBeenCalledOnce();
      expect(localStorage.getItem(FT_TOKEN_KEY)).toBeNull();
      expect(sessionStorage.getItem(FT_SESSION_EXPIRED_KEY)).toBe('1');
    },
  );

  it('keeps a new Vben login when an older common-client 401 arrives', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'ft-token');
    mocks.accessToken = 'old-vben-token';
    requestClient.instance.defaults.adapter = async (config) => {
      mocks.accessToken = 'new-vben-token';
      throw { config, response: { status: 401 } };
    };

    await expect(requestClient.get('/private')).rejects.toMatchObject({
      response: { status: 401 },
    });

    expect(localStorage.getItem(FT_TOKEN_KEY)).toBe('ft-token');
    expect(mocks.expireSession).not.toHaveBeenCalled();
  });

  it('keeps a new FT login when an older common-client 401 arrives', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'old-ft-token');
    mocks.accessToken = 'vben-token';
    requestClient.instance.defaults.adapter = async (config) => {
      localStorage.setItem(FT_TOKEN_KEY, 'new-ft-token');
      throw { config, response: { status: 401 } };
    };

    await expect(requestClient.get('/private')).rejects.toMatchObject({
      response: { status: 401 },
    });

    expect(localStorage.getItem(FT_TOKEN_KEY)).toBe('new-ft-token');
    expect(mocks.expireSession).not.toHaveBeenCalled();
  });
});
