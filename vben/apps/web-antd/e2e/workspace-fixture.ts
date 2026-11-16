import type { Page } from '@playwright/test';

import { installDemoFixture } from './demo-fixture';

export const API_PATHS = {
  factors: '/api/locals/ops',
  data: '/api/locals/database/status',
  runs: '/api/locals/iterations',
  health: '/api/locals/runtime-health',
  services: '/api/locals/services',
  events: '/api/locals/research-ledger-events',
};

export const FACTOR_NAME = 'synthetic_momentum_7';
export const SYNTHETIC_DATA_SOURCE = '合成测试采集源';
export const SYNTHETIC_SERVICE = '合成测试采集服务';
export const RUN_A = 'synthetic-run-a';
export const RUN_B = 'synthetic-run-b';

export function runFacts(runId: string) {
  return {
    run_id: runId,
    execution_status: 'succeeded',
    research_verdict: 'rejected',
    strategy: '合成测试研究策略',
    error_code: `synthetic-proof-${runId}`,
    model: 'synthetic-model',
    identifier: `${runId}-artifact`,
    started_at: '2026-11-16T01:00:00Z',
    completed_at: '2026-11-16T01:01:00Z',
    metrics: { profit_pct: '-1.234567890123456789', total_trades: 17 },
    evidence: {
      code_sha256: 'a'.repeat(64),
      dataset_sha256: 'b'.repeat(64),
      rule_hash: 'c'.repeat(64),
    },
    gate: { reasons: ['合成测试：成本证据未齐，研究拒绝'] },
  };
}

/** 只提供已存在的只读合同；名称显式标注合成，不建立任何研究或交易批准。 */
export async function installWorkspaceFixture(page: Page) {
  const state = await installDemoFixture(page);
  const generated = new Date().toISOString();
  state.reads.set(API_PATHS.factors, {
    generated_at: generated,
    factors: [
      {
        factor: FACTOR_NAME,
        group: '动量',
        ic_base: '0.1200',
        ic_recent: '-0.0400',
        t_recent: '-2.3000',
        drift: '-0.1600',
        alert: true,
        weak: false,
        flip: true,
        decay: true,
        status: '符号反转',
      },
      {
        factor: 'synthetic_volume_ratio',
        group: '成交量',
        ic_base: null,
        ic_recent: '0',
        t_recent: null,
        drift: null,
        alert: null,
        weak: null,
        flip: null,
        decay: null,
        status: null,
      },
    ],
  });
  state.reads.set(API_PATHS.data, {
    engine: 'postgresql',
    database: {
      name: 'synthetic-browser-data',
      schema_version: 'synthetic-v1',
    },
    counts: { candles: 100_003, ticks: 200_007, orderbooks: 31 },
    datasets: [
      {
        exchange: 'binance',
        market: 'futures',
        pair: 'BTC/USDT:USDT',
        timeframe: '1m',
        candle_type: 'futures',
        rows: 100_003,
        first_timestamp: 1_700_000_000_000,
        last_timestamp: 1_730_000_000_000,
      },
      {
        exchange: 'binance',
        market: 'futures',
        pair: 'ETH/USDT:USDT',
        timeframe: '1h',
        candle_type: 'futures',
        rows: 1007,
        first_timestamp: 1_700_000_000_000,
        last_timestamp: 1_730_000_000_000,
      },
    ],
    sources: [
      {
        key: SYNTHETIC_DATA_SOURCE,
        kind: 'synthetic_fixture',
        row_count: 100_003,
        updated_at: generated,
      },
    ],
    collector: {
      state: 'running',
      last_error: null,
      disk_alert_level: 'unknown',
    },
    sync: { running: false, last_success_at: generated, errors: [] },
  });
  state.reads.set(API_PATHS.runs, {
    items: [runFacts(RUN_A), runFacts(RUN_B)],
    total: 19,
    generated_at: generated,
  });
  state.reads.set(`${API_PATHS.runs}/${RUN_A}`, runFacts(RUN_A));
  state.reads.set(`${API_PATHS.runs}/${RUN_B}`, runFacts(RUN_B));
  state.reads.set(API_PATHS.health, {
    generated_at: generated,
    ok: true,
    components: [
      {
        key: 'synthetic_collection',
        label: '合成测试行情采集',
        state: 'ok',
        updated_at: generated,
        last_success_at: generated,
        age_seconds: 1,
        error_codes: [],
        metrics: { pending: 0 },
      },
    ],
  });
  state.reads.set(API_PATHS.services, {
    generated_at: generated,
    running: 1,
    total: 1,
    services: [
      {
        key: 'synthetic_collector',
        name: SYNTHETIC_SERVICE,
        running: true,
        pid: null,
        port: null,
        desired_state: 'running',
        health_reason: '合成测试进程事实，不提供账户执行资格',
      },
    ],
  });
  state.reads.set(API_PATHS.events, (url: URL) => {
    const research = url.searchParams.get('research_id') || RUN_A;
    return {
      total: 1,
      events: [
        {
          event_id: `synthetic-event-${research}`,
          timestamp: generated,
          event_type: 'synthetic_research_observed',
          research_id: research,
          payload: {
            sha256: 'd'.repeat(64),
            reason: '合成测试事件，不构成批准',
            api_key: 'synthetic-private-payload-must-not-render',
          },
        },
      ],
    };
  });
  return state;
}
