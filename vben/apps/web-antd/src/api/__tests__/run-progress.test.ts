import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getRunProgress } from '../freqtrade';
import { FT_SESSION_EXPIRED_KEY, FT_TOKEN_KEY } from '../session';

const mocks = vi.hoisted(() => ({ expireSession: vi.fn(), fetch: vi.fn() }));
vi.mock('@vben/hooks', () => ({
  useAppConfig: () => ({ apiURL: 'http://127.0.0.1:8890/api' }),
}));
vi.mock('#/store', () => ({
  useAuthStore: () => ({ expireSession: mocks.expireSession }),
}));

let clock = Date.UTC(2026, 9, 12);

describe('authenticated run-progress polling', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    mocks.expireSession.mockResolvedValue(undefined);
    vi.stubGlobal('fetch', mocks.fetch);
    vi.useFakeTimers();
    clock += 3000;
    vi.setSystemTime(clock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('redirects on 401 instead of treating an expired session as absent progress', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'expired-token');
    mocks.fetch.mockResolvedValue({ ok: false, status: 401 });

    await expect(getRunProgress()).resolves.toBeNull();

    expect(mocks.expireSession).toHaveBeenCalledOnce();
    expect(localStorage.getItem(FT_TOKEN_KEY)).toBeNull();
    expect(sessionStorage.getItem(FT_SESSION_EXPIRED_KEY)).toBe('1');
  });

  it('keeps normal missing-progress errors on the page', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'valid-token');
    mocks.fetch.mockResolvedValue({ ok: false, status: 404 });

    await expect(getRunProgress()).resolves.toBeNull();

    expect(localStorage.getItem(FT_TOKEN_KEY)).toBe('valid-token');
    expect(mocks.expireSession).not.toHaveBeenCalled();
  });

  it('does not clear a fresh login when the previous progress poll returns 401', async () => {
    localStorage.setItem(FT_TOKEN_KEY, 'old-token');
    mocks.fetch.mockImplementation(async (_url, options) => {
      expect(options.headers.Authorization).toBe('Bearer old-token');
      localStorage.setItem(FT_TOKEN_KEY, 'new-token');
      return { ok: false, status: 401 };
    });

    await expect(getRunProgress()).resolves.toBeNull();

    expect(localStorage.getItem(FT_TOKEN_KEY)).toBe('new-token');
    expect(mocks.expireSession).not.toHaveBeenCalled();
  });
});
