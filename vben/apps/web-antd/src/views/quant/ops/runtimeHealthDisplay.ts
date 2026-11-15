import type { RuntimeHealth, RuntimeHealthComponent } from '#/api/runtime-health';

export function healthStateView(state: string) {
  return ({
    ok: { label: '正常', color: 'green' },
    warning: { label: '需关注', color: 'orange' },
    failed: { label: '异常', color: 'red' },
    unknown: { label: '未知', color: 'default' },
  } as Record<string, { label: string; color: string }>)[state]
    ?? { label: '未知', color: 'default' };
}

export function formatHealthMetric(value: unknown): string {
  if (typeof value === 'boolean') return value ? '是' : '否';
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : '未知';
}

function validDate(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && Number.isFinite(Date.parse(value)));
}

function validComponent(value: unknown): value is RuntimeHealthComponent {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const row = value as RuntimeHealthComponent;
  return typeof row.key === 'string' && !!row.key.trim()
    && typeof row.label === 'string' && !!row.label.trim()
    && ['ok', 'warning', 'failed', 'unknown'].includes(row.state)
    && validDate(row.updated_at) && validDate(row.last_success_at)
    && (row.age_seconds === null || (typeof row.age_seconds === 'number'
      && Number.isFinite(row.age_seconds)))
    && Array.isArray(row.error_codes) && row.error_codes.every((code) => typeof code === 'string')
    && !!row.metrics && typeof row.metrics === 'object' && !Array.isArray(row.metrics)
    && Object.values(row.metrics).every((metric) => metric === null || typeof metric === 'boolean'
      || (typeof metric === 'number' && Number.isFinite(metric)));
}

export interface HealthSnapshot {
  value: RuntimeHealth | null;
  hasSnapshot: boolean;
  loading: boolean;
  error: string;
}

export function createHealthSnapshot(): HealthSnapshot {
  return { value: null, hasSnapshot: false, loading: false, error: '' };
}

/** 各管道必须完整返回；请求失败时保留最近成功快照并标明读取失败。 */
export async function refreshHealthSnapshot(
  state: HealthSnapshot, loader: () => Promise<unknown>,
): Promise<void> {
  if (state.loading) return;
  state.loading = true;
  try {
    const value = await loader() as RuntimeHealth | null;
    if (!value || typeof value.ok !== 'boolean' || !value.generated_at
      || !validDate(value.generated_at) || !Array.isArray(value.components)
      || !value.components.every(validComponent)) throw new Error('invalid_snapshot');
    const keys = new Set(value.components.map((row) => row.key));
    if (keys.size !== value.components.length
      || !['collection', 'sync', 'archive', 'integrity', 'storage'].every((key) => keys.has(key))) {
      throw new Error('incomplete_snapshot');
    }
    state.value = { ...value, ok: value.ok && value.components.every((row) => row.state === 'ok') };
    state.hasSnapshot = true;
    state.error = '';
  } catch {
    state.error = '运行健康状态暂时无法读取，请重试';
  } finally {
    state.loading = false;
  }
}
