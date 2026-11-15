import { describe, expect, it } from 'vitest';

import {
  createHealthSnapshot,
  formatHealthMetric,
  healthStateView,
  refreshHealthSnapshot,
} from '../runtimeHealthDisplay';

function payload(state = 'ok') {
  return {
    generated_at: '2026-11-15T18:00:00Z',
    ok: state === 'ok',
    components: ['collection', 'sync', 'archive', 'integrity', 'storage'].map((key) => ({
      key, label: key, state,
      updated_at: '2026-11-15T18:00:00Z', last_success_at: null,
      age_seconds: 0, error_codes: [] as string[], metrics: { pending: 0 },
    })),
  };
}

describe('runtime health display', () => {
  it('keeps failed and unknown components visibly distinct from healthy', () => {
    expect(healthStateView('failed').color).toBe('red');
    expect(healthStateView('unknown').color).toBe('default');
    expect(healthStateView('unrecognized').label).toBe('未知');
    expect(healthStateView('ok').color).toBe('green');
  });

  it('preserves zero metrics while missing and nonfinite values remain unknown', () => {
    expect(formatHealthMetric(0)).toBe('0');
    expect(formatHealthMetric(false)).toBe('否');
    expect(formatHealthMetric(null)).toBe('未知');
    expect(formatHealthMetric(Number.NaN)).toBe('未知');
    expect(formatHealthMetric(Number.POSITIVE_INFINITY)).toBe('未知');
  });

  it('retains the last complete snapshot on a failed refresh', async () => {
    const snapshot = createHealthSnapshot();
    await refreshHealthSnapshot(snapshot, async () => payload('warning'));
    await refreshHealthSnapshot(snapshot, async () => { throw new Error('private-path-secret'); });
    expect(snapshot.value?.components[0]?.state).toBe('warning');
    expect(snapshot.hasSnapshot).toBe(true);
    expect(snapshot.error).toBeTruthy();
    expect(snapshot.error).not.toContain('private-path-secret');
    expect(snapshot.loading).toBe(false);
  });

  it.each([
    null, { ok: true, components: [] },
    { ...payload(), components: [{ key: 'collection', state: 'ok' }] },
    { ...payload(), ok: 'true' },
    { ...payload(), components: [payload().components[0], payload().components[0]] },
    { ...payload(), components: payload().components.slice(0, 4) },
    { ...payload(), components: payload().components.map((row) => ({ ...row, error_codes: 'failed' })) },
    { ...payload(), components: payload().components.map((row) => ({ ...row, age_seconds: Number.NaN })) },
  ])('rejects incomplete or ambiguous snapshots without showing healthy', async (value) => {
    const snapshot = createHealthSnapshot();
    await refreshHealthSnapshot(snapshot, async () => value);
    expect(snapshot.hasSnapshot).toBe(false);
    expect(snapshot.value).toBeNull();
    expect(snapshot.error).toBeTruthy();
  });

  it('rejects a global healthy claim when a component reports failure', async () => {
    const snapshot = createHealthSnapshot();
    await refreshHealthSnapshot(snapshot, async () => ({ ...payload('failed'), ok: true }));
    expect(snapshot.value?.ok).toBe(false);
    expect(snapshot.value?.components[0]?.state).toBe('failed');
  });

  it('keeps a clock-skew failure visible when the backend reports negative age', async () => {
    const snapshot = createHealthSnapshot();
    const value = payload('failed');
    value.components[0]!.age_seconds = -120;
    value.components[0]!.error_codes = ['status_clock_skew'];
    await refreshHealthSnapshot(snapshot, async () => value);
    expect(snapshot.error).toBe('');
    expect(snapshot.value?.components[0]?.error_codes).toEqual(['status_clock_skew']);
    expect(snapshot.value?.ok).toBe(false);
  });

  it('prevents an overlapping refresh from replacing a newer response', async () => {
    const snapshot = createHealthSnapshot();
    let finish!: (value: unknown) => void;
    const first = refreshHealthSnapshot(snapshot, () => new Promise((resolve) => { finish = resolve; }));
    let secondCalled = false;
    await refreshHealthSnapshot(snapshot, async () => { secondCalled = true; return payload('failed'); });
    finish(payload());
    await first;
    expect(secondCalled).toBe(false);
    expect(snapshot.value?.components[0]?.state).toBe('ok');
  });
});
