import type { IterationRun } from '#/api/freqtrade';

import { fullOrderTime } from '../utils/orderDisplayId';

interface StatusView {
  label: string;
  color: string;
}

/** 执行完成不代表研究通过，两列始终分别展示。 */
export function executionView(status: string | null | undefined): StatusView {
  const values = {
    running: { label: '运行中', color: 'processing' },
    succeeded: { label: '执行成功', color: 'green' },
    failed: { label: '执行失败', color: 'red' },
    cancelled: { label: '已取消', color: 'default' },
    paused: { label: '已暂停', color: 'default' },
    disabled: { label: '自动迭代已停用', color: 'default' },
    busy: { label: '等待其他任务', color: 'processing' },
    skipped: { label: '已跳过', color: 'default' },
  } as Record<string, StatusView>;
  return Object.hasOwn(values, status ?? '')
    ? values[status ?? '']!
    : { label: '状态未知', color: 'default' };
}

export function researchView(verdict: string | null | undefined): StatusView {
  const values = {
    passed: { label: '研究通过', color: 'green' },
    rejected: { label: '研究拒绝', color: 'orange' },
    not_estimable: { label: '不可评估', color: 'default' },
  } as Record<string, StatusView>;
  return Object.hasOwn(values, verdict ?? '')
    ? values[verdict ?? '']!
    : { label: '尚无裁定', color: 'default' };
}

export function formatRunMetric(
  value: unknown,
  suffix = '',
  digits = 2,
): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${value.toFixed(digits)}${suffix}`
    : '未知';
}

/** gate.values 使用后端规范字段，并保留它显式给出的未知值。 */
export function runMetrics(run: IterationRun | null): Record<string, unknown> {
  return { ...run?.entry?.metrics, ...run?.metrics, ...run?.gate?.values };
}

export function formatRunTime(value: unknown): string {
  const time = fullOrderTime(value);
  return time === '—' ? '未记录' : time;
}

export function validRunId(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value) &&
    !value.includes('..')
  );
}
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function optionalRecord(value: unknown) {
  return value == null || record(value);
}
function isRun(value: unknown): value is IterationRun {
  if (!record(value) || !validRunId(value.run_id)) return false;
  for (const field of [
    'execution_status',
    'research_verdict',
    'status',
    'strategy',
    'identifier',
    'model',
    'timerange',
    'version_id',
    'error_code',
    'result_source',
  ]) {
    if (value[field] != null && typeof value[field] !== 'string') return false;
  }
  for (const field of ['metrics', 'gate', 'progress', 'evidence', 'entry'])
    if (!optionalRecord(value[field])) return false;
  const gate = value.gate;
  if (
    record(gate) &&
    ((gate.reasons != null &&
      (!Array.isArray(gate.reasons) ||
        !gate.reasons.every((item) => typeof item === 'string'))) ||
      !optionalRecord(gate.values))
  )
    return false;
  const progress = value.progress;
  if (
    record(progress) &&
    progress.percent != null &&
    (typeof progress.percent !== 'number' ||
      !Number.isFinite(progress.percent) ||
      progress.percent < 0 ||
      progress.percent > 100)
  )
    return false;
  return true;
}

export interface RunSnapshot {
  items: IterationRun[];
  total: number;
  hasSnapshot: boolean;
  loading: boolean;
  error: string;
  request: number;
}

export function createRunSnapshot(): RunSnapshot {
  return {
    items: [],
    total: 0,
    hasSnapshot: false,
    loading: false,
    error: '',
    request: 0,
  };
}

/** 只有完整合法的响应才替换现有快照；网络错误与真实空列表分开。 */
export async function refreshRunSnapshot(
  state: RunSnapshot,
  loader: () => Promise<unknown>,
): Promise<void> {
  if (state.loading) return;
  state.loading = true;
  const request = ++state.request;
  try {
    const value = (await loader()) as {
      items?: unknown;
      total?: unknown;
    } | null;
    if (request !== state.request) return;
    if (
      !value ||
      !Array.isArray(value.items) ||
      !value.items.every(isRun) ||
      typeof value.total !== 'number' ||
      !Number.isInteger(value.total) ||
      value.total < value.items.length ||
      new Set(value.items.map((item) => item.run_id)).size !==
        value.items.length
    )
      throw new Error('invalid_snapshot');
    state.items = value.items;
    state.total = value.total;
    state.hasSnapshot = true;
    state.error = '';
  } catch {
    if (request === state.request) state.error = '运行记录暂时无法读取，请重试';
  } finally {
    if (request === state.request) state.loading = false;
  }
}

export interface RunDetail {
  selectedRunId: string;
  item: IterationRun | null;
  loading: boolean;
  error: string;
  request: number;
}

export function createRunDetail(): RunDetail {
  return {
    selectedRunId: '',
    item: null,
    loading: false,
    error: '',
    request: 0,
  };
}

/** 选择另一轮时清空旧详情；迟到响应不能串到新 run_id。 */
export async function refreshRunDetail(
  state: RunDetail,
  runId: string,
  loader: (id: string) => Promise<unknown>,
): Promise<void> {
  const request = ++state.request;
  if (!validRunId(runId)) {
    state.selectedRunId = '';
    state.item = null;
    state.error = '运行参数无效';
    state.loading = false;
    return;
  }
  if (state.selectedRunId !== runId) state.item = null;
  state.selectedRunId = runId;
  state.loading = true;
  state.error = '';
  try {
    const value = await loader(runId);
    if (request !== state.request) return;
    if (!isRun(value) || value.run_id !== runId)
      throw new Error('run_mismatch');
    state.item = value;
  } catch {
    if (request === state.request) state.error = '该轮证据暂时无法读取，请重试';
  } finally {
    if (request === state.request) state.loading = false;
  }
}
