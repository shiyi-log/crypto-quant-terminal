/** 严格读取展示合同；进程健康不产生账户风险、批准或执行资格。 */
export type HealthState = 'ok' | 'warning' | 'failed' | 'unknown';
export interface HealthComponent {
  key: string;
  label: string;
  state: HealthState;
  updated_at: string | null;
  last_success_at: string | null;
  age_seconds: number | null;
  error_codes: string[];
  metrics: Record<string, boolean | number | null>;
}
export interface HealthEvidence {
  generated_at: string;
  ok: boolean;
  components: HealthComponent[];
}
export interface ServiceEvidence {
  generatedAt: string;
  running: number;
  total: number;
  services: Array<{
    key: string;
    name: string;
    running: boolean;
    pid: string | null;
    port: number | null;
    desiredState: string | null;
    healthReason: string | null;
  }>;
}
export interface EventEvidence {
  eventId: string;
  timestamp: string;
  eventType: string;
  researchId: string | null;
  hash: string | null;
  reason: string | null;
}
export interface EventSnapshot {
  events: EventEvidence[];
  total: number;
}
type RecordValue = Record<string, unknown>;
function record(value: unknown): RecordValue {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error('invalid_object');
  return value as RecordValue;
}
function text(value: unknown): string {
  if (typeof value !== 'string' || !value.trim())
    throw new Error('invalid_text');
  return value;
}
function date(value: unknown): string {
  const result = text(value);
  const match =
    /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(?:Z|[+-](\d{2}):?(\d{2}))?$/.exec(
      result,
    );
  if (!match) throw new Error('invalid_date');
  const [year, month, day, hour, minute, second] = match
    .slice(1, 7)
    .map(Number);
  const calendar = new Date(Date.UTC(year!, month! - 1, day!));
  if (
    calendar.getUTCFullYear() !== year ||
    calendar.getUTCMonth() + 1 !== month ||
    calendar.getUTCDate() !== day ||
    hour! > 23 ||
    minute! > 59 ||
    second! > 59 ||
    (match[7] !== undefined && Number(match[7]) > 23) ||
    (match[8] !== undefined && Number(match[8]) > 59) ||
    !Number.isFinite(Date.parse(result))
  )
    throw new Error('invalid_date');
  return result;
}
function nullableDate(value: unknown): string | null {
  return value === null ? null : date(value);
}
function count(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0)
    throw new Error('invalid_count');
  return value;
}
function unique(keys: string[]) {
  if (new Set(keys).size !== keys.length) throw new Error('duplicate_identity');
}

export function parseHealth(value: unknown): HealthEvidence {
  const source = record(value);
  if (typeof source.ok !== 'boolean' || !Array.isArray(source.components))
    throw new Error('invalid_health');
  const components = source.components.map((item): HealthComponent => {
    const row = record(item);
    const metrics = record(row.metrics);
    if (
      typeof row.state !== 'string' ||
      !['ok', 'warning', 'failed', 'unknown'].includes(row.state) ||
      !(
        row.age_seconds === null ||
        (typeof row.age_seconds === 'number' &&
          Number.isFinite(row.age_seconds))
      ) ||
      !Array.isArray(row.error_codes) ||
      !row.error_codes.every((code) => typeof code === 'string') ||
      !Object.values(metrics).every(
        (metric) =>
          metric === null ||
          typeof metric === 'boolean' ||
          (typeof metric === 'number' && Number.isFinite(metric)),
      )
    )
      throw new Error('invalid_component');
    return {
      key: text(row.key),
      label: text(row.label),
      state: row.state as HealthState,
      updated_at: nullableDate(row.updated_at),
      last_success_at: nullableDate(row.last_success_at),
      age_seconds: row.age_seconds,
      error_codes: [...row.error_codes],
      metrics: { ...metrics } as HealthComponent['metrics'],
    };
  });
  unique(components.map((row) => row.key));
  return {
    generated_at: date(source.generated_at),
    ok:
      source.ok &&
      components.length > 0 &&
      components.every((row) => row.state === 'ok'),
    components,
  };
}

export function parseServices(value: unknown): ServiceEvidence {
  const source = record(value);
  if (!Array.isArray(source.services)) throw new Error('invalid_services');
  const total = count(source.total);
  const running = count(source.running);
  const services = source.services.map((item) => {
    const row = record(item);
    if (
      typeof row.running !== 'boolean' ||
      !(
        row.pid === null ||
        typeof row.pid === 'string' ||
        (typeof row.pid === 'number' && Number.isSafeInteger(row.pid))
      ) ||
      !(
        row.port === null ||
        (typeof row.port === 'number' &&
          Number.isInteger(row.port) &&
          row.port > 0 &&
          row.port <= 65535)
      )
    )
      throw new Error('invalid_service');
    return {
      key: text(row.key),
      name: text(row.name),
      running: row.running,
      pid: row.pid === null ? null : String(row.pid),
      port: row.port,
      desiredState:
        typeof row.desired_state === 'string' ? row.desired_state : null,
      healthReason:
        typeof row.health_reason === 'string' ? row.health_reason : null,
    };
  });
  unique(services.map((row) => row.key));
  if (
    total !== services.length ||
    running !== services.filter((row) => row.running).length
  )
    throw new Error('inconsistent_services');
  return { generatedAt: date(source.generated_at), running, total, services };
}

export function parseEvents(value: unknown): EventSnapshot {
  const source = record(value);
  if (!Array.isArray(source.events)) throw new Error('invalid_events');
  const total = count(source.total);
  const events = source.events.map((item): EventEvidence => {
    const row = record(item);
    const payload = record(row.payload);
    if (
      !(
        row.research_id === null ||
        (typeof row.research_id === 'string' && row.research_id.trim())
      )
    )
      throw new Error('invalid_research_identity');
    // 只显示明确字段，避免把原始 payload、凭据或上游异常作为日志全文展开。
    const hash = [
      payload.sha256,
      payload.content_hash,
      payload.binding_hash,
      payload.hash,
    ].find(
      (entry) => typeof entry === 'string' && /^[a-f0-9]{64}$/i.test(entry),
    );
    const reason = typeof payload.reason === 'string' ? payload.reason : null;
    return {
      eventId: text(row.event_id),
      timestamp: date(row.timestamp),
      eventType: text(row.event_type),
      researchId: row.research_id,
      hash: typeof hash === 'string' ? hash : null,
      reason,
    };
  });
  unique(events.map((row) => row.eventId));
  if (total < events.length) throw new Error('inconsistent_events');
  return { events, total };
}

export interface ReadState<T> {
  value: T | null;
  valueKey: string | null;
  requestKey: string;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string;
  generation: number;
  controller: AbortController | null;
}
export function createReadState<T>(): ReadState<T> {
  return {
    value: null,
    valueKey: null,
    requestKey: '',
    status: 'idle',
    error: '',
    generation: 0,
    controller: null,
  };
}
export function cancelReadState<T>(state: ReadState<T>) {
  state.generation++;
  state.controller?.abort();
  state.status = state.value ? 'ready' : 'idle';
}
export async function refreshReadState<T>(
  state: ReadState<T>,
  key: string,
  loader: (signal: AbortSignal) => Promise<unknown>,
  parse: (value: unknown) => T,
): Promise<void> {
  state.controller?.abort();
  const controller = new AbortController();
  const generation = ++state.generation;
  state.controller = controller;
  state.requestKey = key;
  state.status = 'loading';
  state.error = '';
  try {
    const result = parse(await loader(controller.signal));
    if (generation !== state.generation || controller.signal.aborted) return;
    state.value = result;
    state.valueKey = key;
    state.status = 'ready';
  } catch {
    if (generation !== state.generation || controller.signal.aborted) return;
    state.error = state.value
      ? '读取失败，保留上次成功事实'
      : '读取失败，尚无可用事实';
    state.status = 'error';
  }
}
