import { describe, expect, it } from 'vitest';
import type { InventoryDataset } from '../dataQuality';

import {
  createDataInventoryReader,
  parseDataInventory,
  selectDatasets,
} from '../dataQuality';

function response() {
  return {
    engine: 'postgresql',
    database: { name: 'quant_terminal', schema_version: 'v2' },
    counts: { candles: 105122, ticks: 0 },
    datasets: [
      {
        exchange: 'binance',
        market: 'futures',
        pair: 'BTC/USDT:USDT',
        timeframe: '5m',
        candle_type: 'futures',
        rows: 105121,
        first_timestamp: '2025-01-01T00:00:00+00:00',
        last_timestamp: '2026-01-01T00:00:00+00:00',
      },
      {
        exchange: 'binance',
        market: 'futures',
        pair: 'ETH/USDT:USDT',
        timeframe: '1m',
        candle_type: 'futures',
        rows: 1,
        first_timestamp: 1000,
        last_timestamp: 1000,
      },
    ],
    sources: [
      {
        key: 'source-1',
        kind: 'feather',
        row_count: 105121,
        updated_at: '2026-01-01T00:01:00+00:00',
        error: null,
      },
    ],
    sync: {
      running: true,
      last_success_at: '2026-01-01T00:01:00+00:00',
      errors: [],
    },
    collector: {
      state: 'running',
      last_error: 'websocket interrupted',
      disk_alert_level: 'warning',
    },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('data inventory evidence', () => {
  it('does not turn a year of endpoints or record counts into quality approval', () => {
    const result = parseDataInventory(response());
    expect(result.datasets[0]?.rows).toBe(105121);
    expect(result.datasets[0]?.firstTimestamp).toBe(
      '2025-01-01T00:00:00+00:00',
    );
    expect(
      result.qualityChecks.map((check: { state: string }) => check.state),
    ).toEqual(['unavailable', 'unavailable', 'unavailable', 'unavailable']);
    expect(result.researchEligible).toBe(false);
    expect(result.collectorError).toBe('websocket interrupted');
    expect(result.syncState).toBe('running');
  });

  it('distinguishes verified empty arrays from missing or malformed collections', () => {
    expect(
      parseDataInventory({ engine: 'postgresql', datasets: [], sources: [] })
        .datasets,
    ).toEqual([]);
    for (const change of [
      { datasets: null },
      { datasets: {} },
      { sources: undefined },
      { sources: {} },
    ]) {
      expect(() => parseDataInventory({ ...response(), ...change })).toThrow(
        '数据清单合同不完整',
      );
    }
  });

  it('keeps unknown counts and missing timestamps unknown rather than zero', () => {
    const source = response();
    const result = parseDataInventory({
      ...source,
      counts: {},
      collector: null,
      datasets: [
        { ...source.datasets[0], first_timestamp: null, last_timestamp: null },
      ],
    });
    expect(result.counts.candles).toBeNull();
    expect(result.counts.ticks).toBeNull();
    expect(result.datasets[0]?.firstTimestamp).toBeNull();
    expect(result.collectorState).toBe('unknown');
  });

  it('rejects invalid row counts, reversed endpoints and duplicate dataset identities', () => {
    const source = response();
    for (const change of [
      { rows: -1 },
      { rows: true },
      { rows: Number.MAX_SAFE_INTEGER + 1 },
      { pair: '' },
      { first_timestamp: 2000, last_timestamp: 1000 },
    ]) {
      expect(() =>
        parseDataInventory({
          ...source,
          datasets: [{ ...source.datasets[0], ...change }],
        }),
      ).toThrow();
    }
    expect(() =>
      parseDataInventory({
        ...source,
        datasets: [source.datasets[0], source.datasets[0]],
      }),
    ).toThrow();
  });

  it('preserves backend numeric sync error counts without inventing error details', () => {
    const result = parseDataInventory({
      ...response(),
      sync: { running: false, errors: 2 },
    });
    expect(result.syncErrors).toEqual(['同步报告 2 项错误，逐项详情未提供']);
    expect(() =>
      parseDataInventory({ ...response(), sync: { errors: -1 } }),
    ).toThrow();
  });

  it('rejects impossible dates and malformed optional versions instead of disguising them as unknown', () => {
    const source = response();
    for (const last_timestamp of [
      '2026-02-30T00:00:00Z',
      '1',
      8_640_000_000_000_001,
    ]) {
      expect(() =>
        parseDataInventory({
          ...source,
          datasets: [{ ...source.datasets[0], last_timestamp }],
        }),
      ).toThrow();
    }
    expect(() =>
      parseDataInventory({ ...source, database: { schema_version: {} } }),
    ).toThrow();
    expect(() =>
      parseDataInventory({
        ...source,
        sources: [source.sources[0], source.sources[0]],
      }),
    ).toThrow();
  });

  it('filters exact pair and frequency without merging markets or inventing missing rows', () => {
    const data = parseDataInventory(response());
    expect(
      selectDatasets(data.datasets, 'BTC/USDT:USDT', '5m').map(
        (item: InventoryDataset) => item.pair,
      ),
    ).toEqual(['BTC/USDT:USDT']);
    expect(selectDatasets(data.datasets, 'TRX/USDT:USDT', '5m')).toEqual([]);
    expect(
      selectDatasets(data.datasets, '', '1m').map(
        (item: InventoryDataset) => item.pair,
      ),
    ).toEqual(['ETH/USDT:USDT']);
  });
});

describe('data inventory refresh lifecycle', () => {
  it('retains a successful snapshot when a later request or response contract fails', async () => {
    let value: unknown = response();
    const reader = createDataInventoryReader(async () => value);
    await reader.refresh();
    const first = reader.state.snapshot;
    value = { datasets: 'damaged' };
    await reader.refresh();
    expect(reader.state.snapshot).toBe(first);
    expect(reader.state.status).toBe('stale');
    expect(reader.state.error).toContain('数据清单合同不完整');
    expect(reader.state.loading).toBe(false);
  });

  it('reports first-read failure as unknown and preserves a confirmed empty result', async () => {
    const reader = createDataInventoryReader(async () => {
      throw new Error('offline');
    });
    await reader.refresh();
    expect(reader.state.status).toBe('error');
    expect(reader.state.snapshot).toBeNull();
    const empty = createDataInventoryReader(async () => ({
      engine: 'postgresql',
      datasets: [],
      sources: [],
    }));
    await empty.refresh();
    expect(empty.state.status).toBe('ready');
    expect(empty.state.snapshot?.datasets).toEqual([]);
  });

  it('aborts an old refresh and ignores its later completion or error', async () => {
    const first = deferred<unknown>();
    const signals: AbortSignal[] = [];
    let calls = 0;
    const reader = createDataInventoryReader((signal) => {
      signals.push(signal);
      return calls++ === 0
        ? first.promise
        : Promise.resolve({ engine: 'postgresql', datasets: [], sources: [] });
    });
    const oldRead = reader.refresh();
    await reader.refresh();
    first.resolve(response());
    await oldRead;
    expect(signals[0]?.aborted).toBe(true);
    expect(reader.state.snapshot?.datasets).toEqual([]);
    expect(reader.state.status).toBe('ready');
  });

  it('does not publish an in-flight snapshot after the page is left', async () => {
    const pending = deferred<unknown>();
    const reader = createDataInventoryReader(() => pending.promise);
    const task = reader.refresh();
    reader.dispose();
    pending.resolve(response());
    await task;
    expect(reader.state.snapshot).toBeNull();
    expect(reader.state.loading).toBe(false);
  });
});
