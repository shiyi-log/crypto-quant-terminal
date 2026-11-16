import type {
  DemoAccountSnapshot,
  DemoEvidence,
  DemoRecord,
  DemoSession,
  DemoStrategySession,
} from '#/api/binance-demo';

import { ftClient, unwrap } from '#/api/freqtrade/client';
import { requireSession } from '#/api/session';

import { parseDemoSettingsRows } from './settingsContract';

const basePath = '/locals/binance-demo';

/** 设置工作区直接验证原始响应；共享兼容 wrapper 不参与完整性判断。 */
async function read(
  path: string,
  signal?: AbortSignal,
  session?: string,
  view?: 'audit' | 'signals',
) {
  requireSession('请先登录后配置币安 Demo');
  const params = {
    ...(['orders', 'fills', 'comparisons', 'sessions', 'strategies'].includes(
      path,
    )
      ? { limit: 100 }
      : {}),
    ...(session ? { session } : {}),
    ...(view ? { view } : {}),
  };
  return unwrap<unknown>(
    await ftClient.get(`${basePath}/${path}`, {
      signal,
      ...(Object.keys(params).length ? { params } : {}),
    }),
  );
}

export const readDemoOrders = async (signal?: AbortSignal, session?: string) =>
  parseDemoSettingsRows(await read('orders', signal, session));
export const readDemoFills = async (signal?: AbortSignal, session?: string) =>
  parseDemoSettingsRows(await read('fills', signal, session));
export const readDemoPositions = async (signal?: AbortSignal) =>
  parseDemoSettingsRows(await read('positions', signal));
export const readDemoComparisons = async (
  signal?: AbortSignal,
  session?: string,
  view?: 'audit' | 'signals',
) => parseDemoSettingsRows(await read('comparisons', signal, session, view));
function validateSessions(items: DemoRecord[]) {
  const keys = new Set<string>();
  for (const row of items) {
    if (
      typeof row.virtualAccountKey !== 'string' ||
      !row.virtualAccountKey ||
      typeof row.strategyId !== 'string' ||
      !row.strategyId ||
      keys.has(row.virtualAccountKey)
    )
      throw new Error('Demo 会话响应不完整');
    keys.add(row.virtualAccountKey);
  }
}
export async function readDemoSessions(signal?: AbortSignal) {
  const result = parseDemoSettingsRows<DemoSession>(
    await read('sessions', signal),
  );
  validateSessions(result.items);
  if (result.items.some((row) => typeof row.active !== 'boolean'))
    throw new Error('Demo 会话响应不完整');
  return result;
}
export async function readDemoStrategies(signal?: AbortSignal) {
  const result = parseDemoSettingsRows<DemoStrategySession>(
    await read('strategies', signal),
  );
  validateSessions(result.items);
  for (const row of result.items) {
    if (
      typeof row.primary !== 'boolean' ||
      typeof row.entriesEnabled !== 'boolean' ||
      (row.member !== undefined && typeof row.member !== 'boolean') ||
      !row.metrics ||
      typeof row.metrics !== 'object' ||
      Array.isArray(row.metrics) ||
      !Array.isArray(row.roundTrips) ||
      !row.ownedPositions ||
      typeof row.ownedPositions !== 'object'
    )
      throw new Error('Demo 策略响应不完整');
  }
  return result;
}
export async function readDemoAccount(
  signal?: AbortSignal,
): Promise<DemoAccountSnapshot> {
  const raw = await read('account', signal);
  const rows = parseDemoSettingsRows(raw);
  const body = raw as DemoRecord;
  if (
    !body.account ||
    typeof body.account !== 'object' ||
    Array.isArray(body.account)
  )
    throw new Error('Demo 账户响应不完整');
  if (
    body.observedAt != null &&
    (typeof body.observedAt !== 'number' ||
      !Number.isSafeInteger(body.observedAt) ||
      body.observedAt < 0)
  )
    throw new Error('Demo 账户响应不完整');
  return {
    ...rows,
    account: body.account as DemoRecord,
    observedAt: body.observedAt as null | number,
  };
}
export async function readDemoEvidence(
  signal?: AbortSignal,
  session?: string,
): Promise<DemoEvidence> {
  const raw = await read('evidence', signal, session);
  if (!raw || typeof raw !== 'object' || Array.isArray(raw))
    throw new Error('Demo 证据响应不完整');
  const body = raw as DemoRecord;
  const questions = body.questions as DemoRecord | undefined;
  if (
    body.environment !== 'binance_demo' ||
    body.endpoint !== 'demo-fapi.binance.com' ||
    body.scope !== 'selected_candle_strategy_execution_only' ||
    !questions ||
    typeof questions !== 'object' ||
    Array.isArray(questions) ||
    !['fees', 'secondsProfit', 'execution'].every(
      (key) =>
        questions[key] &&
        typeof questions[key] === 'object' &&
        !Array.isArray(questions[key]),
    )
  )
    throw new Error('Demo 证据响应不完整');
  return raw as DemoEvidence;
}
