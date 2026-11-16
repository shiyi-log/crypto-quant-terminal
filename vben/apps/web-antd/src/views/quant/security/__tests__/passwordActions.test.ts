import { describe, expect, it, vi } from 'vitest';

import { createPasswordActions } from '../passwordActions';

function setup(username = 'operator') {
  const change = vi.fn().mockResolvedValue({ message: 'ok' });
  const reset = vi.fn().mockResolvedValue({
    generated: true,
    password: 'temporary-secret',
    username,
  });
  const changed = vi.fn();
  const copy = vi.fn().mockResolvedValue(undefined);
  const actions = createPasswordActions({
    username: () => username,
    change,
    reset,
    changed,
    copy,
  });
  Object.assign(actions.state.form, {
    oldPassword: 'old-password',
    newPassword: 'new-password',
    confirm: 'new-password',
  });
  return { actions, change, reset, changed, copy };
}

describe('password commands', () => {
  it.each([
    { oldPassword: '' },
    { newPassword: 'short', confirm: 'short' },
    { newPassword: 'old-password', confirm: 'old-password' },
    { confirm: 'different' },
  ])('rejects invalid input before a write: %o', async (fields) => {
    const { actions, change, changed } = setup();
    Object.assign(actions.state.form, fields);
    await actions.changePassword();
    expect(change).not.toHaveBeenCalled();
    expect(changed).not.toHaveBeenCalled();
    expect(actions.state.error).not.toBe('');
  });

  it('preserves password bytes and clears fields after a confirmed change', async () => {
    const { actions, change, changed } = setup();
    Object.assign(actions.state.form, {
      oldPassword: ' old-password ',
      newPassword: ' new-password ',
      confirm: ' new-password ',
    });
    await actions.changePassword();
    expect(change).toHaveBeenCalledExactlyOnceWith(
      ' old-password ',
      ' new-password ',
    );
    expect(actions.state.form).toEqual({
      oldPassword: '',
      newPassword: '',
      confirm: '',
    });
    expect(actions.state.changed).toBe(true);
    expect(changed).toHaveBeenCalledTimes(1);
  });

  it('does not reflect a sensitive upstream error or claim success', async () => {
    const { actions, change, changed } = setup();
    change.mockRejectedValue(new Error('signed=secret&password=old-password'));
    await actions.changePassword();
    expect(actions.state.error).not.toContain('secret');
    expect(actions.state.error).not.toContain('old-password');
    expect(actions.state.error).not.toBe('');
    expect(actions.state.changed).toBe(false);
    expect(changed).not.toHaveBeenCalled();
  });

  it('serializes reset and change across duplicate clicks', async () => {
    const { actions, change, reset } = setup();
    let resolve!: (value: { message: string }) => void;
    change.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const running = actions.changePassword();
    await actions.changePassword();
    await actions.resetPassword();
    expect(change).toHaveBeenCalledTimes(1);
    expect(reset).not.toHaveBeenCalled();
    resolve({ message: 'ok' });
    await running;
  });

  it('refuses a reset when the current identity is unknown', async () => {
    const { actions, reset } = setup('');
    await actions.resetPassword();
    expect(reset).not.toHaveBeenCalled();
    expect(actions.state.error).not.toBe('');
  });

  it('keeps the generated result ephemeral and copies only on a user action', async () => {
    const { actions, reset, copy } = setup();
    expect(reset).not.toHaveBeenCalled();
    await actions.resetPassword();
    expect(reset).toHaveBeenCalledExactlyOnceWith('operator');
    expect(actions.state.generatedPassword).toBe('temporary-secret');
    expect(copy).not.toHaveBeenCalled();
    await actions.copyGeneratedPassword();
    expect(copy).toHaveBeenCalledExactlyOnceWith('temporary-secret');
    expect(actions.state.copied).toBe(true);
    actions.clearGeneratedPassword();
    expect(actions.state.generatedPassword).toBe('');
    expect(actions.state.copied).toBe(false);
    await actions.copyGeneratedPassword();
    expect(copy).toHaveBeenCalledTimes(1);
  });

  it.each([
    { generated: true, password: 'temporary-secret', username: 'other' },
    { generated: false, password: 'temporary-secret', username: 'operator' },
    { generated: true, password: '', username: 'operator' },
  ])('rejects an unbound reset result: %o', async (body) => {
    const { actions, reset } = setup();
    reset.mockResolvedValue(body);
    await actions.resetPassword();
    expect(actions.state.generatedPassword).toBe('');
    expect(actions.state.error).not.toBe('');
  });

  it('reports local restriction without raw error details', async () => {
    const { actions, reset } = setup();
    reset.mockRejectedValue({
      response: { status: 403 },
      message: 'private-password',
    });
    await actions.resetPassword();
    expect(actions.state.error).toContain('本机');
    expect(actions.state.error).not.toContain('private-password');
  });

  it('discards late results after leaving and clears entered secrets', async () => {
    const { actions, reset, changed } = setup();
    let resolve!: (value: {
      generated: boolean;
      password: string;
      username: string;
    }) => void;
    reset.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const running = actions.resetPassword();
    actions.dispose();
    resolve({ generated: true, password: 'late-secret', username: 'operator' });
    await running;
    expect(actions.state.generatedPassword).toBe('');
    expect(actions.state.form).toEqual({
      oldPassword: '',
      newPassword: '',
      confirm: '',
    });
    expect(changed).not.toHaveBeenCalled();
  });

  it('does not attach a late recovery result to a new login identity', async () => {
    let username = 'operator';
    let resolve!: (value: unknown) => void;
    const reset = vi.fn(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const actions = createPasswordActions({
      username: () => username,
      change: vi.fn(),
      reset,
      changed: vi.fn(),
      copy: vi.fn(),
    });
    const running = actions.resetPassword();
    username = 'other';
    resolve({
      generated: true,
      password: 'private-old-result',
      username: 'operator',
    });
    await running;
    expect(actions.state.generatedPassword).toBe('');
    expect(actions.state.resetCompleted).toBe(false);
  });

  it('discards a previous identity failure after the account changes', async () => {
    let username = 'operator';
    let reject!: (reason: unknown) => void;
    const reset = vi.fn(
      () =>
        new Promise((_done, fail) => {
          reject = fail;
        }),
    );
    const actions = createPasswordActions({
      username: () => username,
      change: vi.fn(),
      reset,
      changed: vi.fn(),
      copy: vi.fn(),
    });
    const running = actions.resetPassword();
    username = 'other';
    reject({ response: { status: 403 } });
    await running;
    expect(actions.state.error).toBe('');
  });
});
