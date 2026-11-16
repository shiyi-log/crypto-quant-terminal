/** 旧研究运行的只读事实边界；执行成功、研究通过与上线批准分别处理。 */
export interface ExperimentRun {
  run_id: string;
  execution_status?: string;
  research_verdict?: string;
  strategy?: null | string;
  model?: null | string;
  identifier?: string;
  version_id?: string;
  timerange?: null | string;
  updated_at?: string;
  completed_at?: string;
  finished_at?: string;
  started_at?: string;
  timestamp?: string;
  error_code?: null | string;
  result_source?: null | string;
  metrics?: Record<string, unknown>;
  evidence?: null | Record<string, unknown>;
  gate?: { reasons?: string[]; values?: Record<string, unknown> };
  progress?: { percent?: null | number; phase?: string };
  entry?: {
    evidence?: Record<string, unknown>;
    metrics?: Record<string, unknown>;
  };
}
export interface RunList {
  items: ExperimentRun[];
  total: number;
  generated_at: null | string;
}
export interface ExperimentState {
  snapshot: null | RunList;
  detail: null | ExperimentRun;
  selectedRunId: string;
  listLoading: boolean;
  detailLoading: boolean;
  listError: string;
  detailError: string;
  listRequest: number;
  detailRequest: number;
  listController: null | AbortController;
  detailController: null | AbortController;
  disposed: boolean;
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function identity(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,179}$/.test(value)
  );
}
function optionalText(
  row: Record<string, unknown>,
  key: string,
  nullable = false,
) {
  if (
    !(key in row) ||
    row[key] === undefined ||
    (nullable && row[key] === null)
  )
    return;
  if (typeof row[key] !== 'string' || (row[key] as string).length > 4096)
    throw new Error('运行记录合同不完整');
}

export function parseRun(value: unknown): ExperimentRun {
  if (!object(value) || !identity(value.run_id))
    throw new Error('运行身份合同不完整');
  for (const key of [
    'execution_status',
    'research_verdict',
    'identifier',
    'version_id',
    'updated_at',
    'completed_at',
    'finished_at',
    'started_at',
    'timestamp',
  ])
    optionalText(value, key);
  for (const key of ['strategy', 'model', 'timerange'])
    optionalText(value, key, true);
  for (const key of ['error_code', 'result_source'])
    optionalText(value, key, true);
  for (const key of ['metrics', 'gate', 'progress', 'entry']) {
    if (key in value && value[key] !== undefined && !object(value[key]))
      throw new Error('运行记录合同不完整');
  }
  if (
    'evidence' in value &&
    value.evidence !== undefined &&
    value.evidence !== null &&
    !object(value.evidence)
  )
    throw new Error('证据合同不完整');
  const gate = value.gate;
  if (object(gate)) {
    if (gate.values !== undefined && !object(gate.values))
      throw new Error('判据合同不完整');
    if (
      gate.reasons !== undefined &&
      (!Array.isArray(gate.reasons) ||
        !gate.reasons.every((item) => typeof item === 'string'))
    )
      throw new Error('判据原因合同不完整');
  }
  const entry = value.entry;
  if (object(entry)) {
    for (const key of ['metrics', 'evidence'])
      if (entry[key] !== undefined && !object(entry[key]))
        throw new Error('运行产物合同不完整');
  }
  const progress = value.progress;
  if (object(progress)) {
    if (
      progress.percent !== undefined &&
      progress.percent !== null &&
      (typeof progress.percent !== 'number' ||
        !Number.isFinite(progress.percent) ||
        progress.percent < 0 ||
        progress.percent > 100)
    )
      throw new Error('进度合同不完整');
    optionalText(progress, 'phase');
  }
  return structuredClone(value) as unknown as ExperimentRun;
}

export function parseRunList(value: unknown): RunList {
  if (
    !object(value) ||
    !Array.isArray(value.items) ||
    typeof value.total !== 'number' ||
    !Number.isSafeInteger(value.total) ||
    value.total < value.items.length
  )
    throw new Error('列表合同不完整');
  const items = value.items.map(parseRun);
  if (new Set(items.map((item) => item.run_id)).size !== items.length)
    throw new Error('列表身份重复');
  optionalText(value, 'generated_at', true);
  return {
    items,
    total: value.total,
    generated_at:
      typeof value.generated_at === 'string' ? value.generated_at : null,
  };
}

export function createExperimentState(): ExperimentState {
  return {
    snapshot: null,
    detail: null,
    selectedRunId: '',
    listLoading: false,
    detailLoading: false,
    listError: '',
    detailError: '',
    listRequest: 0,
    detailRequest: 0,
    listController: null,
    detailController: null,
    disposed: false,
  };
}

export async function refreshRuns(
  state: ExperimentState,
  loader: (signal: AbortSignal) => Promise<unknown>,
) {
  if (state.disposed) return;
  state.listController?.abort();
  const request = ++state.listRequest;
  const controller = new AbortController();
  state.listController = controller;
  state.listLoading = true;
  try {
    const raw = await loader(controller.signal);
    if (
      controller.signal.aborted ||
      request !== state.listRequest ||
      state.disposed
    )
      return;
    state.snapshot = parseRunList(raw);
    state.listError = '';
  } catch (error) {
    if (
      !controller.signal.aborted &&
      request === state.listRequest &&
      !state.disposed
    ) {
      const contract = error instanceof Error && error.message.includes('合同');
      state.listError = `${contract ? '运行记录合同不完整' : '运行记录读取失败'}${state.snapshot ? '，保留上次成功数据' : '，尚无可用快照'}；请重试`;
    }
  } finally {
    if (request === state.listRequest && !state.disposed)
      state.listLoading = false;
  }
}

export async function selectRun(
  state: ExperimentState,
  runId: string,
  loader: (id: string, signal: AbortSignal) => Promise<unknown>,
) {
  if (state.disposed) return;
  state.detailController?.abort();
  const request = ++state.detailRequest;
  if (state.selectedRunId !== runId) state.detail = null;
  state.selectedRunId = runId;
  state.detailError = '';
  state.detailLoading = true;
  const controller = new AbortController();
  state.detailController = controller;
  try {
    if (!identity(runId)) throw new Error('运行身份无效');
    const raw = await loader(runId, controller.signal);
    if (
      controller.signal.aborted ||
      request !== state.detailRequest ||
      state.disposed
    )
      return;
    const item = parseRun(raw);
    if (item.run_id !== runId) throw new Error('返回运行身份不匹配');
    state.detail = item;
  } catch (error) {
    if (
      !controller.signal.aborted &&
      request === state.detailRequest &&
      !state.disposed
    ) {
      const identityError =
        error instanceof Error && error.message.includes('身份');
      state.detailError = `${identityError ? '该轮身份无法核对' : '该轮证据读取失败'}${state.detail ? '，保留该轮上次成功数据' : '，未取得该轮证据'}；请重试`;
    }
  } finally {
    if (request === state.detailRequest && !state.disposed)
      state.detailLoading = false;
  }
}

export function cancelExperiments(state: ExperimentState) {
  state.disposed = true;
  state.listController?.abort();
  state.detailController?.abort();
  state.listRequest += 1;
  state.detailRequest += 1;
}

export function executionLabel(value: unknown): string {
  const labels: Record<string, string> = {
    running: '运行中',
    succeeded: '执行成功',
    failed: '执行失败',
    cancelled: '已取消',
    paused: '已暂停',
    disabled: '自动迭代已停用',
    busy: '等待其他任务',
    skipped: '已跳过',
  };
  return Object.hasOwn(labels, String(value))
    ? labels[String(value)]!
    : '执行状态未知';
}
export function verdictLabel(value: unknown): string {
  const labels: Record<string, string> = {
    passed: '研究通过（旧判据）',
    rejected: '研究拒绝',
    not_estimable: '不可评估',
  };
  return Object.hasOwn(labels, String(value))
    ? labels[String(value)]!
    : '尚无研究裁定';
}
export function metricText(value: unknown): string {
  if (
    typeof value === 'string' &&
    value.length <= 128 &&
    Number.isFinite(Number(value)) &&
    /^[-+]?(?:[0-9]+(?:[.][0-9]*)?|[.][0-9]+)(?:[eE][-+]?[0-9]+)?$/.test(value)
  )
    return value;
  return typeof value === 'number' && Number.isFinite(value)
    ? String(value)
    : '未知';
}
export function artifactLink(run: ExperimentRun): null | string {
  return run.identifier?.trim()
    ? `/quant/detail?identifier=${encodeURIComponent(run.identifier)}`
    : null;
}
export function identityFacts(
  run: ExperimentRun,
): Array<{ label: string; value: string }> {
  const evidence = { ...run.entry?.evidence, ...run.evidence };
  return [
    ['代码指纹', ['code_sha256', 'code_hash']],
    ['数据指纹', ['dataset_sha256', 'data_hash', 'input_fingerprint']],
    ['策略指纹', ['strategy_hash', 'rule_hash']],
    ['执行指纹', ['execution_hash']],
    ['成本指纹', ['cost_model_hash', 'cost_hash']],
    ['风险指纹', ['risk_policy_hash', 'risk_hash']],
  ].map(([label, keys]) => {
    const value = (keys as string[])
      .map((key) => evidence[key])
      .find((item) => typeof item === 'string' && item.trim());
    return {
      label: label as string,
      value: typeof value === 'string' ? value : '未知',
    };
  });
}
