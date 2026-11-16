export type RegistryStatus = 'champion' | 'challenger' | 'archived';
export type RegistryVersion = Record<string, any> & {
  id: string;
  layer: string;
  status: RegistryStatus;
  metrics: Record<string, number | string | null>;
  gate: null | {
    passed: boolean;
    reasons: string[];
    criteria?: string;
    status?: string;
  };
  metrics_validity?: string;
};
export type VersionRegistrySnapshot = {
  updated: string | null;
  running: Record<string, unknown> | null;
  trials: Record<string, any>;
  layers: Record<
    string,
    {
      champion: RegistryVersion | null;
      challengers: RegistryVersion[];
      archived: RegistryVersion[];
    }
  >;
  rows: RegistryVersion[];
};
type RegistryApi = {
  load: () => Promise<unknown>;
  promote: (
    id: string,
    note: string,
    confirmUnpassed: boolean,
  ) => Promise<unknown>;
};
type ConfirmTarget = {
  id: string;
  layer: string;
  label: string;
  gate: RegistryVersion['gate'];
  signature: string;
};
type RegistryState = {
  snapshot: VersionRegistrySnapshot | null;
  loading: boolean;
  submitting: boolean;
  error: string;
  commandError: string;
  note: string;
  confirmUnpassed: boolean;
  confirmTarget: ConfirmTarget | null;
  selectedId: string;
  outcome: string;
};

const record = (value: unknown): value is Record<string, any> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value);
const nullableText = (value: unknown, field: string): string | null => {
  if (value === null) return null;
  if (typeof value !== 'string')
    throw new Error('版本注册表字段 ' + field + ' 格式不完整');
  return value;
};
function parseVersion(
  value: unknown,
  layer: string,
  expected: RegistryStatus,
): RegistryVersion {
  if (!record(value) || typeof value.id !== 'string' || !value.id.trim()) {
    throw new Error('版本注册表包含无效版本身份');
  }
  if (value.layer !== layer || value.status !== expected)
    throw new Error('版本注册表分组与版本状态不一致');
  if (value.metrics !== undefined && !record(value.metrics))
    throw new Error('版本指标格式不完整');
  const metrics = (value.metrics ?? {}) as Record<string, unknown>;
  for (const [key, metric] of Object.entries(metrics)) {
    if (
      metric !== null &&
      typeof metric !== 'string' &&
      (typeof metric !== 'number' || !Number.isFinite(metric))
    ) {
      throw new Error('版本指标 ' + key + ' 格式不完整');
    }
  }
  let gate: RegistryVersion['gate'] = null;
  if (value.gate !== undefined && value.gate !== null) {
    if (!record(value.gate) || typeof value.gate.passed !== 'boolean')
      throw new Error('版本门禁状态格式不完整');
    if (
      value.gate.reasons !== undefined &&
      (!Array.isArray(value.gate.reasons) ||
        value.gate.reasons.some(
          (reason: unknown) => typeof reason !== 'string',
        ))
    ) {
      throw new Error('版本门禁理由格式不完整');
    }
    if (
      value.gate.criteria !== undefined &&
      typeof value.gate.criteria !== 'string'
    )
      throw new Error('版本门禁判据格式不完整');
    if (
      value.gate.status !== undefined &&
      typeof value.gate.status !== 'string'
    )
      throw new Error('版本门禁状态格式不完整');
    gate = {
      ...value.gate,
      passed: value.gate.passed,
      reasons: value.gate.reasons ?? [],
    };
  }
  if (
    value.metrics_validity !== undefined &&
    typeof value.metrics_validity !== 'string'
  )
    throw new Error('指标有效性格式不完整');
  return {
    ...value,
    id: value.id,
    layer,
    status: expected,
    metrics: metrics as RegistryVersion['metrics'],
    gate,
  };
}

export function parseVersionRegistry(value: unknown): VersionRegistrySnapshot {
  if (!record(value) || !record(value.layers))
    throw new Error('版本注册表格式不完整');
  const updated = nullableText(value.updated, 'updated');
  const running = value.running == null ? null : value.running;
  if (running !== null && !record(running))
    throw new Error('迭代运行状态格式不完整');
  if (!record(value.trials)) throw new Error('迭代统计格式不完整');
  const layers: VersionRegistrySnapshot['layers'] = {};
  const rows: RegistryVersion[] = [];
  const ids = new Set<string>();
  for (const [layer, raw] of Object.entries(value.layers)) {
    if (!record(raw)) throw new Error('版本分层格式不完整');
    const champion =
      raw.champion === null
        ? null
        : parseVersion(raw.champion, layer, 'champion');
    if (!Array.isArray(raw.challengers) || !Array.isArray(raw.archived))
      throw new Error('版本候选或归档列表格式不完整');
    const challengers = raw.challengers.map((item: unknown) =>
      parseVersion(item, layer, 'challenger'),
    );
    const archived = raw.archived.map((item: unknown) =>
      parseVersion(item, layer, 'archived'),
    );
    for (const item of [champion, ...challengers, ...archived]) {
      if (!item) continue;
      if (ids.has(item.id)) throw new Error('版本注册表包含重复版本');
      ids.add(item.id);
      rows.push(item);
    }
    layers[layer] = { champion, challengers, archived };
  }
  return {
    updated,
    running: running as VersionRegistrySnapshot['running'],
    trials: value.trials,
    layers,
    rows,
  };
}

function signature(version: RegistryVersion) {
  return JSON.stringify(version);
}
function invalidated(version: RegistryVersion) {
  return (
    version.metrics_validity === 'invalidated' ||
    version.gate?.status === 'invalidated'
  );
}
export function createVersionRegistry(api: RegistryApi) {
  const state = reactive<RegistryState>({
    snapshot: null,
    loading: false,
    submitting: false,
    error: '',
    commandError: '',
    note: '',
    confirmUnpassed: false,
    confirmTarget: null,
    selectedId: '',
    outcome: '',
  });
  let disposed = false;
  let request = 0;
  let submitting = false;
  const refresh = async () => {
    const current = ++request;
    state.loading = true;
    state.error = '';
    try {
      const snapshot = parseVersionRegistry(await api.load());
      if (!disposed && current === request) state.snapshot = snapshot;
    } catch {
      if (!disposed && current === request)
        state.error = '版本注册表读取失败，已保留上次成功快照；请重试。';
    } finally {
      if (!disposed && current === request) state.loading = false;
    }
  };
  const select = (id: string) => {
    if (state.snapshot?.rows.some((row) => row.id === id))
      state.selectedId = id;
  };
  const openConfirmation = (id: string) => {
    const version = state.snapshot?.rows.find((row) => row.id === id);
    state.selectedId = version?.id ?? state.selectedId;
    state.commandError = '';
    state.outcome = '';
    state.note = '';
    state.confirmUnpassed = false;
    if (!version || version.status !== 'challenger' || invalidated(version)) {
      state.confirmTarget = null;
      state.commandError =
        version && invalidated(version)
          ? '该版本口径已作废，不能登记。'
          : '只能对有效的候选版本进行人工登记。';
      return;
    }
    state.confirmTarget = {
      id: version.id,
      layer: version.layer,
      label: typeof version.label === 'string' ? version.label : '',
      gate: version.gate,
      signature: signature(version),
    };
  };
  const submit = async () => {
    const target = state.confirmTarget;
    if (!target || submitting || disposed) return;
    const version = state.snapshot?.rows.find((row) => row.id === target.id);
    if (
      !version ||
      signature(version) !== target.signature ||
      version.status !== 'challenger' ||
      invalidated(version)
    ) {
      state.commandError = '版本数据自确认后已变化，请刷新并重新确认。';
      return;
    }
    const note = state.note.trim();
    if (note.length < 4) {
      state.commandError = '请填写至少 4 个字的登记理由。';
      return;
    }
    if (version.gate?.passed !== true && !state.confirmUnpassed) {
      state.commandError = '该版本未通过冻结判据，请明确勾选确认后再提交。';
      return;
    }
    submitting = true;
    state.submitting = true;
    state.commandError = '';
    state.outcome = '';
    try {
      const receipt = await api.promote(
        target.id,
        note,
        version.gate?.passed !== true && state.confirmUnpassed,
      );
      if (disposed) return;
      if (
        !record(receipt) ||
        receipt.status !== 'ok' ||
        receipt.id !== target.id
      ) {
        state.commandError =
          '登记请求已发送，但回执身份无法核对，结果未知；请刷新注册表核验。';
        return;
      }
      state.outcome =
        '已登记 ' +
        target.id +
        '。此操作仅更新版本注册表，不改变实际部署配置或订单。';
      state.confirmTarget = null;
      state.note = '';
      state.confirmUnpassed = false;
      await refresh();
    } catch {
      if (!disposed)
        state.commandError =
          '登记请求失败或结果未知；请刷新注册表核验后再操作。';
    } finally {
      submitting = false;
      if (!disposed) state.submitting = false;
    }
  };
  const dispose = () => {
    disposed = true;
    request += 1;
  };
  return { state, refresh, select, openConfirmation, submit, dispose };
}
import { reactive } from 'vue';
