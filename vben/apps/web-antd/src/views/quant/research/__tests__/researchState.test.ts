import { describe, expect, it } from 'vitest';

import {
  metricText,
  parseResearchSnapshot,
  pickResearchCandidate,
  researchLevel,
  researchTime,
} from '../researchState';

const snapshot = () => ({
  generated_at: '2026-11-16 10:00:00',
  routes: [
    {
      name: 'Carry',
      level: 'good',
      evidence: '历史输入',
      annual: '3%',
      verdict: '推荐',
    },
  ],
  models: [{ identifier: 'model-1', label: '模型一', profit_pct: null }],
  carry: { cash: null, sweep: [], liquidation: [], buffers: [], schemes: [] },
});

describe('historical research evidence projection', () => {
  it('preserves a real empty collection separately from missing contract', () => {
    const empty = { ...snapshot(), routes: [], models: [] };
    expect(parseResearchSnapshot(empty).routes).toEqual([]);
    for (const value of [
      null,
      {},
      { ...snapshot(), routes: null },
      { ...snapshot(), models: {} },
    ])
      expect(() => parseResearchSnapshot(value)).toThrow();
  });
  it('rejects duplicate or malformed identities before replacing known evidence', () => {
    for (const value of [
      { ...snapshot(), routes: [{ name: '' }] },
      { ...snapshot(), routes: [snapshot().routes[0], snapshot().routes[0]] },
      { ...snapshot(), models: [{ identifier: 'x' }, { identifier: 'x' }] },
      { ...snapshot(), carry: { sweep: {} } },
      { ...snapshot(), carry: {} },
    ])
      expect(() => parseResearchSnapshot(value)).toThrow();
  });
  it('returns an isolated snapshot without filling absent cost or identity proof', () => {
    const original = snapshot();
    const parsed = parseResearchSnapshot(original);
    original.routes[0]!.evidence = '后来变化';
    expect(parsed.routes[0]!.evidence).toBe('历史输入');
    expect(parsed.cost_completeness).toBeUndefined();
    expect(parsed.strategy_hash).toBeUndefined();
  });
  it('does not promote inherited object keys or a historical good verdict into approval', () => {
    expect(researchLevel('good')).toBe('历史推荐');
    expect(researchLevel('constructor')).toBe('结论未知');
    expect(researchLevel('bad')).toBe('历史证伪');
  });
  it('keeps missing or invalid metrics unknown and prints the actual loss sign', () => {
    for (const value of [null, undefined, '', ' ', false, Infinity, 'NaN'])
      expect(metricText(value)).toBe('未知');
    expect(metricText(-2.25, 2, true)).toBe('-2.25');
    expect(metricText(2.25, 2, true)).toBe('+2.25');
    expect(metricText(0, 2, true)).toBe('0.00');
  });
  it('interprets the legacy generated timestamp in Beijing and rejects invalid or zoneless ISO dates', () => {
    expect(researchTime('2026-11-16 10:00:00')).toBe(Date.UTC(2026, 10, 16, 2));
    expect(researchTime('2026-11-16T02:00:00Z')).toBe(
      Date.UTC(2026, 10, 16, 2),
    );
    for (const value of [
      '2026-02-30 10:00:00',
      '2026-11-16T10:00:00',
      '',
      null,
    ])
      expect(researchTime(value)).toBeNull();
  });
  it('does not substitute another candidate when an exact history identity is missing', () => {
    const rows = snapshot().routes;
    expect(pickResearchCandidate(rows, 'Carry')?.name).toBe('Carry');
    expect(pickResearchCandidate(rows, 'missing')).toBeNull();
    expect(pickResearchCandidate(rows, '')?.name).toBe('Carry');
  });
});
