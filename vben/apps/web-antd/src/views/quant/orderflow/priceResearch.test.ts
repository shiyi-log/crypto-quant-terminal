import { describe, expect, it } from 'vitest';

import {
  createPriceReader,
  outcomeLabel,
  parsePriceSnapshot,
  profileSegments,
  selectPriceEvent,
} from './priceResearch';

export const pair = 'BTC/USDT:USDT';
export function response(items: unknown[] = []) {
  return {
    items,
    market: 'futures',
    timeframe: '5m',
    direction: 'any',
    sample_scope: 'live_online_only',
    method_version: 'orderflow-price-spike-close3-v4',
    limitations: ['仅在线样本'],
    warmup: {},
    stats: {
      method_version: 'orderflow-price-spike-close3-v4',
      event_threshold_pct: 0.015,
      forward_bars: 3,
      ratio_bands: ['lt1', '1to2', '2to4', '4to8', '8to20', 'ge20'],
      sample_scope: 'live_online_only',
      unknown_excluded: true,
      invalid_excluded: true,
      by_pair: {
        [pair]: {
          candidate_count: 0,
          matured_count: 0,
          base_p_any: null,
          bands: {},
        },
      },
      profile_by_pair: { [pair]: { up: [], down: [] } },
    },
  };
}
export function candidate(id = 'event-1', changes = {}) {
  return {
    event_id: id,
    pair,
    timestamp: 1000,
    status: 'unknown',
    observation: { volume_ratio: 4, ratio_band: '4to8' },
    outcome: null,
    profile: null,
    profile_status: null,
    ...changes,
  };
}
const deferred = () => {
  let resolve!: (value: unknown) => void;
  const promise = new Promise<unknown>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

describe('price research evidence contract', () => {
  it('keeps successful empty rows and null probabilities distinct from missing contracts', () => {
    const snapshot = parsePriceSnapshot(response(), { pair, direction: 'any' });
    expect(snapshot.items).toEqual([]);
    expect(snapshot.stats.by_pair[pair]?.base_p_any).toBeNull();
    expect(() =>
      parsePriceSnapshot(
        { ...response(), items: undefined },
        { pair, direction: 'any' },
      ),
    ).toThrow();
    expect(() =>
      parsePriceSnapshot(
        { ...response(), stats: {} },
        { pair, direction: 'any' },
      ),
    ).toThrow();
  });
  it('rejects duplicate identities, wrong market scope and malformed event facts', () => {
    for (const raw of [
      response([candidate(), candidate()]),
      response([candidate('wrong', { pair: 'ETH/USDT:USDT' })]),
      response([candidate('bad', { status: 'success' })]),
      response([candidate('bad', { timestamp: true })]),
      { ...response(), direction: 'up' },
      { ...response(), sample_scope: 'backtest' },
    ])
      expect(() =>
        parsePriceSnapshot(raw, { pair, direction: 'any' }),
      ).toThrow();
  });
  it('does not label a matured non-event as a downward price burst', () => {
    expect(
      outcomeLabel(
        candidate('none', {
          status: 'matured',
          outcome: { direction: 'none', return_pct: 0.001 },
        }),
      ),
    ).toBe('未达到阈值');
    expect(
      outcomeLabel(
        candidate('up', {
          status: 'matured',
          outcome: { direction: 'up', return_pct: 0.02 },
        }),
      ),
    ).toBe('上涨 +');
    expect(outcomeLabel(candidate())).toBe('待成熟');
  });
  it('rejects a response whose individual event contradicts the requested direction', () => {
    const raw = response([
      candidate('down', {
        status: 'matured',
        outcome: { direction: 'down', return_pct: -0.02 },
      }),
    ]);
    raw.direction = 'up';
    expect(() => parsePriceSnapshot(raw, { pair, direction: 'up' })).toThrow();
  });
  it('opens only the requested event and reports missing without substitution', () => {
    const rows = [candidate('first'), candidate('second')];
    expect(selectPriceEvent(rows, 'second').selected?.event_id).toBe('second');
    expect(selectPriceEvent(rows, 'absent')).toEqual({
      selected: null,
      missing: true,
    });
    expect(selectPriceEvent(rows, '')).toEqual({
      selected: null,
      missing: false,
    });
  });
  it('splits profiles at missing observations and never invents a zero sample', () => {
    expect(
      profileSegments([
        { offset: -12, mean_volume_ratio: 2, sample_count: 1 },
        { offset: -11, mean_volume_ratio: null, sample_count: 0 },
        { offset: -10, mean_volume_ratio: 3, sample_count: 1 },
        { offset: -9, mean_volume_ratio: 4, sample_count: 1 },
      ]),
    ).toEqual([
      [{ offset: -12, value: 2 }],
      [
        { offset: -10, value: 3 },
        { offset: -9, value: 4 },
      ],
    ]);
  });
  it('refuses a numeric-looking boolean and a probability outside the actual contract', () => {
    const booleanProfile = response();
    booleanProfile.stats.profile_by_pair[pair]!.up = [
      { offset: 0, mean_volume_ratio: true, sample_count: 1 },
    ] as never[];
    expect(() =>
      parsePriceSnapshot(booleanProfile, { pair, direction: 'any' }),
    ).toThrow();
    const invalidProbability = response();
    invalidProbability.stats.by_pair[pair]!.base_p_any = 1.5 as never;
    expect(() =>
      parsePriceSnapshot(invalidProbability, { pair, direction: 'any' }),
    ).toThrow();
  });
  it('cancels older reads and ignores late success after another read or unmount', async () => {
    const reader = createPriceReader();
    const old = deferred();
    let oldSignal!: AbortSignal;
    const pending = reader.load(
      { pair, direction: 'any' },
      (signal: AbortSignal) => {
        oldSignal = signal;
        return old.promise;
      },
    );
    await reader.load({ pair, direction: 'any' }, async () =>
      response([candidate('current')]),
    );
    expect(oldSignal.aborted).toBe(true);
    old.resolve(response([candidate('late')]));
    await pending;
    expect(reader.state.snapshot?.items[0]?.event_id).toBe('current');
    const afterUnmount = deferred();
    const cancelled = reader.load(
      { pair, direction: 'any' },
      () => afterUnmount.promise,
    );
    reader.cancel();
    afterUnmount.resolve(response([candidate('unmounted')]));
    await cancelled;
    expect(reader.state.snapshot?.items[0]?.event_id).toBe('current');
  });
  it('retains successful facts with original query when a new scope fails', async () => {
    const reader = createPriceReader();
    await reader.load({ pair, direction: 'any' }, async () =>
      response([candidate()]),
    );
    await reader.load(
      { pair: 'ETH/USDT:USDT', direction: 'down' },
      async () => {
        throw new Error('offline');
      },
    );
    expect(reader.state.snapshot?.query).toEqual({ pair, direction: 'any' });
    expect(reader.state.snapshot?.items[0]?.event_id).toBe('event-1');
    expect(reader.state.error).toContain('读取失败');
  });
});
