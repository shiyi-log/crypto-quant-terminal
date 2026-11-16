import { describe, expect, it, vi } from 'vitest';

import {
  createPairPolicy,
  parseBlacklist,
  parseLocks,
  lockTime,
  parseWhitelist,
  splitPairs,
} from '../pairPolicy';

const black = (pairs = ['ETH/USDT:USDT']) => ({
  blacklist: pairs,
  blacklist_expanded: pairs,
  length: pairs.length,
  method: ['StaticPairList'],
  errors: {},
});
const lock = {
  id: 7,
  pair: 'BTC/USDT:USDT',
  active: true,
  side: 'long',
  lock_time: '2026-11-16 01:00:00',
  lock_timestamp: 1794790800000,
  lock_end_time: '2026-11-16 03:00:00',
  lock_end_timestamp: 1794798000000,
  reason: 'CooldownPeriod',
};
function api() {
  return {
    whitelist: vi.fn(async (): Promise<unknown> => ({
      whitelist: ['BTC/USDT:USDT'],
    })),
    blacklist: vi.fn(async (): Promise<unknown> => black()),
    locks: vi.fn(async (): Promise<unknown> => ({
      locks: [lock],
      lock_count: 1,
    })),
    add: vi.fn(async (_pairs: string[]): Promise<unknown> => black()),
    remove: vi.fn(async (_pairs: string[]): Promise<unknown> => black([])),
    unlock: vi.fn(async (_id: number): Promise<unknown> => ({
      locks: [],
      lock_count: 0,
    })),
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('pair policy response contracts', () => {
  it('accepts the actual nullable LockModel reason without dropping the whole lock source', () => {
    const facts = parseLocks({
      locks: [{ ...lock, reason: null }],
      lock_count: 1,
    });
    expect(facts.rows).toHaveLength(1);
    expect(facts.rows[0]?.reason).toBeNull();
  });
  it('splits whitespace without damaging pair names and reads naive engine times as UTC', () => {
    expect(splitPairs('SOL/USDT:USDT BTC/USDT:USDT\nETH/USDT:USDT')).toEqual([
      'BTC/USDT:USDT',
      'ETH/USDT:USDT',
      'SOL/USDT:USDT',
    ]);
    expect(lockTime('2026-11-16 01:00:00')).toBe(
      lockTime(Date.UTC(2026, 10, 16, 1)),
    );
  });

  it('accepts real empty responses while rejecting missing or malformed lists', () => {
    expect(parseWhitelist({ whitelist: [] })).toEqual([]);
    expect(() => parseWhitelist({})).toThrow();
    expect(() => parseWhitelist({ whitelist: ['BTC', null] })).toThrow();
    expect(() =>
      parseBlacklist({ ...black(), blacklist_expanded: {} }),
    ).toThrow();
    expect(() =>
      parseLocks({ locks: [{ ...lock, active: 'false' }], lock_count: 1 }),
    ).toThrow();
    expect(() =>
      parseLocks({ locks: [{ ...lock, id: -1 }], lock_count: 1 }),
    ).toThrow();
  });
  it('keeps wildcard expansion and per-pair errors separate from the source entries', () => {
    expect(
      parseBlacklist({
        ...black(['.*UP/USDT']),
        blacklist_expanded: ['BTCUP/USDT'],
        errors: { INVALID: { error_msg: 'Pair is unknown' } },
      }),
    ).toEqual({
      pairs: ['.*UP/USDT'],
      expanded: ['BTCUP/USDT'],
      methods: ['StaticPairList'],
      errors: [['INVALID', 'Pair is unknown']],
    });
    expect(splitPairs('ETH/USDT:USDT， BTC/USDT:USDT;ETH/USDT:USDT')).toEqual([
      'BTC/USDT:USDT',
      'ETH/USDT:USDT',
    ]);
  });
});

describe('pair policy independent evidence and command lifecycle', () => {
  it('requires the actual Locks response contract before calling an unlock receipt received', async () => {
    const transport = api();
    const policy = createPairPolicy(transport);
    await policy.refresh();
    transport.unlock.mockResolvedValueOnce({
      status: 'error',
      message: 'rejected',
    });
    expect(await policy.unlock(7)).toBe(false);
    expect(policy.state.command?.status).toBe('unknown');
    transport.unlock.mockResolvedValueOnce({ locks: [], lock_count: 0 });
    expect(await policy.unlock(7)).toBe(true);
    expect(policy.state.command?.status).toBe('received');
    expect(transport.unlock).toHaveBeenCalledTimes(2);
  });
  it('does not write against a snapshot older than sixty seconds', async () => {
    const transport = api();
    let time = 1000;
    const policy = createPairPolicy(transport, () => time);
    await policy.refresh();
    time = 61_001;
    expect(await policy.unlock(7)).toBe(false);
    expect(transport.unlock).not.toHaveBeenCalled();
    expect(policy.state.command?.status).toBe('blocked');
  });

  it('retains successful facts independently after a later source failure', async () => {
    const transport = api();
    let time = 10;
    const policy = createPairPolicy(transport, () => time);
    await policy.refresh();
    transport.whitelist.mockResolvedValueOnce({ whitelist: null });
    transport.blacklist.mockResolvedValueOnce(black([]));
    time = 20;
    await policy.refresh();
    expect(policy.state.whitelist.data).toEqual(['BTC/USDT:USDT']);
    expect(policy.state.whitelist.at).toBe(10);
    expect(policy.state.whitelist.error).toBeTruthy();
    expect(policy.state.blacklist.data?.pairs).toEqual([]);
    expect(policy.state.blacklist.at).toBe(20);
    expect(policy.state.blacklist.error).toBeNull();
  });
  it('discards older responses when a newer refresh has already completed', async () => {
    const transport = api();
    const old = deferred<unknown>();
    transport.whitelist.mockReturnValueOnce(old.promise);
    const policy = createPairPolicy(transport);
    const pending = policy.refresh();
    transport.whitelist.mockResolvedValueOnce({ whitelist: ['TRX/USDT:USDT'] });
    await policy.refresh();
    old.resolve({ whitelist: ['BTC/USDT:USDT'] });
    await pending;
    expect(policy.state.whitelist.data).toEqual(['TRX/USDT:USDT']);
  });
  it('does not apply late reads after the workspace has been disposed', async () => {
    const transport = api();
    const response = deferred<unknown>();
    transport.whitelist.mockReturnValueOnce(response.promise);
    const policy = createPairPolicy(transport);
    const pending = policy.refresh();
    policy.dispose();
    response.resolve({ whitelist: ['BTC/USDT:USDT'] });
    await pending;
    expect(policy.state.whitelist.data).toBeNull();
  });
  it('blocks unknown or stale restrictions and duplicate writes without issuing commands', async () => {
    const transport = api();
    const policy = createPairPolicy(transport);
    expect(await policy.add(['TRX/USDT:USDT'])).toBe(false);
    expect(transport.add).not.toHaveBeenCalled();
    await policy.refresh();
    const pending = deferred<unknown>();
    transport.add.mockReturnValueOnce(pending.promise);
    const first = policy.add(['TRX/USDT:USDT']);
    expect(await policy.add(['BTC/USDT:USDT'])).toBe(false);
    expect(transport.add).toHaveBeenCalledTimes(1);
    pending.resolve(black(['TRX/USDT:USDT']));
    await first;
    transport.blacklist.mockRejectedValueOnce(new Error('offline'));
    await policy.refresh();
    expect(await policy.remove(['ETH/USDT:USDT'])).toBe(false);
    expect(transport.remove).not.toHaveBeenCalled();
  });
  it('treats the write response as a receipt and refreshes the actual restriction facts', async () => {
    const transport = api();
    const policy = createPairPolicy(transport);
    await policy.refresh();
    transport.add.mockResolvedValueOnce(black(['TRX/USDT:USDT']));
    transport.blacklist.mockResolvedValueOnce(
      black(['ETH/USDT:USDT', 'TRX/USDT:USDT']),
    );
    expect(await policy.add(['ETH/USDT:USDT', 'TRX/USDT:USDT'])).toBe(true);
    expect(transport.add).toHaveBeenCalledWith(['TRX/USDT:USDT']);
    expect(policy.state.blacklist.data?.pairs).toEqual([
      'ETH/USDT:USDT',
      'TRX/USDT:USDT',
    ]);
    expect(policy.state.command?.status).toBe('received');
  });
  it('preserves per-pair rejection evidence and refuses inactive lock commands', async () => {
    const transport = api();
    const policy = createPairPolicy(transport);
    await policy.refresh();
    transport.add.mockResolvedValueOnce({
      ...black(),
      errors: { TRX: { error_msg: 'Unknown pair' } },
    });
    expect(await policy.add(['TRX'])).toBe(false);
    expect(policy.state.command?.errors).toEqual([['TRX', 'Unknown pair']]);
    expect(await policy.unlock(9)).toBe(false);
    expect(transport.unlock).not.toHaveBeenCalled();
    expect(await policy.unlock(7)).toBe(true);
    expect(transport.unlock).toHaveBeenCalledWith(7);
  });
  it('marks an uncertain write as unknown and never retries it automatically', async () => {
    const transport = api();
    const policy = createPairPolicy(transport);
    await policy.refresh();
    transport.remove.mockRejectedValueOnce(new Error('network timeout'));
    expect(await policy.remove(['ETH/USDT:USDT'])).toBe(false);
    expect(policy.state.command?.status).toBe('unknown');
    await policy.refresh();
    expect(transport.remove).toHaveBeenCalledTimes(1);
  });
});
