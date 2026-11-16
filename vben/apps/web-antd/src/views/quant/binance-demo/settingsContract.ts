import type { DemoRecord, DemoRows, DemoStatus } from '#/api/binance-demo';

import { demoError } from './display';

const invalid = () =>
  new Error('Demo 设置响应不完整，保留已知事实并停止新增风险操作');

function object(value: unknown): DemoRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw invalid();
  return value as DemoRecord;
}
function text(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) throw invalid();
  return value;
}
function boolean(value: unknown) {
  if (typeof value !== 'boolean') throw invalid();
  return value;
}
function environment(value: DemoRecord) {
  if (
    value.environment !== 'binance_demo' ||
    ('endpoint' in value && value.endpoint !== 'demo-fapi.binance.com')
  )
    throw invalid();
}

/** 严格读取原始列表，不把缺失 items 或坏行悄悄规范化为空账本。 */
export function parseDemoSettingsRows<T extends DemoRecord = DemoRecord>(
  value: unknown,
): DemoRows<T> {
  const raw = object(value);
  environment(raw);
  if (!Array.isArray(raw.items)) throw invalid();
  const items = raw.items.map((row) => object(row) as T);
  const limit = raw.limit ?? 100;
  if (typeof limit !== 'number' || !Number.isSafeInteger(limit) || limit < 0)
    throw invalid();
  return { environment: 'binance_demo', items, limit };
}

export function parseDemoSettingsStatus(value: unknown): DemoStatus {
  const raw = object(value);
  environment(raw);
  if (raw.endpoint !== 'demo-fapi.binance.com') throw invalid();
  const credentials = object(raw.credentials),
    runner = object(raw.runner);
  boolean(credentials.configured);
  if (credentials.decryptionAvailable !== undefined)
    boolean(credentials.decryptionAvailable);
  for (const field of ['fingerprint', 'maskedApiKey'])
    if (credentials[field] != null) text(credentials[field]);
  text(runner.state);
  for (const field of [
    'tradingEnabled',
    'workerRunning',
    'accountLocked',
    'switchPersistenceUnresolved',
    'sharedRegistryPersistenceUnresolved',
    'sharedRecoveryIncomplete',
    'status_snapshot_stale',
  ])
    if (runner[field] !== undefined) boolean(runner[field]);
  for (const field of ['selectedAccountId', 'virtualAccountKey'])
    if (runner[field] != null) text(runner[field]);
  for (const field of ['switchSupported', 'parallelSupported'])
    if (raw[field] !== undefined) boolean(raw[field]);
  if (
    !Array.isArray(raw.accounts) ||
    !Array.isArray(raw.limitations) ||
    raw.limitations.some((row) => typeof row !== 'string')
  )
    throw invalid();
  const keys = new Set<string>();
  for (const item of raw.accounts) {
    const row = object(item),
      key = text(row.accountId);
    if (keys.has(key)) throw invalid();
    keys.add(key);
    for (const field of ['strategyId', 'ruleHash', 'executionHash'])
      text(row[field]);
    boolean(row.enabled);
    boolean(row.eligible);
  }
  object(raw.metrics);
  return raw as unknown as DemoStatus;
}

function identity(value: DemoStatus) {
  return JSON.stringify([
    value.environment,
    value.endpoint,
    value.credentials.fingerprint ?? null,
    value.runner.selectedAccountId ?? null,
    value.runner.virtualAccountKey ?? null,
    value.accounts
      .map((row) => [row.accountId, row.ruleHash, row.executionHash])
      .sort((a, b) => String(a[0]).localeCompare(String(b[0]))),
  ]);
}

/** 命令/轮询切换身份后，旧响应不能被挂到新的会话标签。 */
export function assertSameDemoIdentity(before: DemoStatus, after: DemoStatus) {
  if (identity(before) !== identity(after))
    throw new Error('Demo 账户或会话已变化，等待重新读取');
}

export function retainedDemoSlice<T>(
  old: T,
  result: PromiseSettledResult<T>,
  before: string,
  after: string,
  empty: T,
): T {
  return result.status === 'fulfilled'
    ? result.value
    : before === after
      ? old
      : empty;
}

/** 一次用户动作只允许一项命令在途，未知结果不会被自动重发。 */
export function createDemoCommandGate() {
  let active = false,
    disposed = false;
  return {
    async run(work: () => Promise<unknown>) {
      if (active || disposed) return false;
      active = true;
      try {
        await work();
        return true;
      } finally {
        active = false;
      }
    },
    busy: () => active,
    dispose() {
      disposed = true;
    },
  };
}

/** 仅展示固定拒绝码的中文映射，未知上游文本可能带有凭据或内部地址。 */
export function demoSettingsError(reason: unknown) {
  const raw = reason as {
    message?: unknown;
    response?: {
      data?: { detail?: unknown; error?: unknown; message?: unknown };
    };
  };
  const body = raw?.response?.data;
  const code = body?.detail ?? body?.error ?? body?.message ?? raw?.message;
  if (
    typeof code === 'string' &&
    /^(?:demo_|shared_|paper_account_)[a-z0-9_]+$/.test(code)
  ) {
    const label = demoError({ message: code });
    if (label !== code) return label;
  }
  return 'Demo 请求未能确认，请刷新核对状态或查看脱敏运行日志';
}
