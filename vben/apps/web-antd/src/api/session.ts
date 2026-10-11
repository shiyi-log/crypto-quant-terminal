/** Local auth sessions cannot be refreshed; an expired session needs a login. */
export const FT_TOKEN_KEY = 'ft_access_token';
export const FT_SESSION_EXPIRED_KEY = 'ft_session_expired';

let expirationPromise: null | Promise<void> = null;

export function clearExpiredSession() {
  localStorage.removeItem(FT_TOKEN_KEY);
  sessionStorage.setItem(FT_SESSION_EXPIRED_KEY, '1');
}

/** Deduplicate polling failures and ignore a late 401 from a previous login. */
export function handleSessionExpired(expectedToken?: string): Promise<void> {
  if (
    expectedToken !== undefined &&
    (localStorage.getItem(FT_TOKEN_KEY) ?? '') !== expectedToken
  ) {
    return Promise.resolve();
  }
  if (expirationPromise) return expirationPromise;

  clearExpiredSession();
  expirationPromise = (async () => {
    // Avoid importing the auth store while its API dependencies initialize.
    const { useAuthStore } = await import('#/store');
    if (!localStorage.getItem(FT_TOKEN_KEY)) {
      await useAuthStore().expireSession();
    }
  })().finally(() => {
    expirationPromise = null;
  });
  return expirationPromise;
}

export function requireSession(message: string) {
  const token = localStorage.getItem(FT_TOKEN_KEY) ?? '';
  if (!token.trim()) {
    void handleSessionExpired(token);
    throw new Error(message);
  }
}
