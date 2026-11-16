import { reactive } from 'vue';
export type LegacyCommand =
  | 'start'
  | 'stop'
  | 'stopbuy'
  | 'reload'
  | 'forceexit';
type Transport = {
  config: () => Promise<unknown>;
  count: () => Promise<unknown>;
  command: (command: LegacyCommand) => Promise<unknown>;
};
function finite(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (typeof value === 'string' && !/^[0-9]+(?:\.[0-9]+)?$/.test(value.trim()))
    return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
function actualConfig(value: unknown): value is Record<string, any> {
  return (
    (value !== null &&
      typeof value === 'object' &&
      !Array.isArray(value) &&
      (value as any).dry_run === true) ||
    (value !== null &&
      typeof value === 'object' &&
      !Array.isArray(value) &&
      (value as any).dry_run === false)
  );
}
function safeNotice(value: unknown, fallback: string): string {
  const status =
    value && typeof value === 'object' ? (value as any).status : '';
  const result =
    value && typeof value === 'object' ? (value as any).result : '';
  if (
    typeof status === 'string' &&
    /^(?:error|failed|rejected|denied)$/i.test(status)
  )
    return fallback;
  if (
    typeof result === 'string' &&
    /^(?:error|failed|rejected|denied)$/i.test(result)
  )
    return fallback;
  return status === 'accepted' ||
    status === 'ok' ||
    (typeof result === 'string' && result.length > 0)
    ? '收到命令回执，状态待后续查询核验。'
    : fallback;
}
export function createLegacyControls(transport: Transport) {
  let disposed = false;
  const state = reactive({
    environment: '运行环境未知',
    state: '',
    current: 0,
    max: null as number | null,
    configKnown: false,
    countKnown: false,
    busy: '' as LegacyCommand | '',
    notice: '',
    async refresh() {
      if (disposed) return;
      const [cfg, count] = await Promise.allSettled([
        transport.config(),
        transport.count(),
      ]);
      if (disposed) return;
      if (cfg.status === 'fulfilled' && actualConfig(cfg.value)) {
        state.configKnown = true;
        state.environment = cfg.value.dry_run ? '模拟盘 dry-run' : '实盘配置';
        state.state =
          typeof cfg.value.state === 'string' ? cfg.value.state : '';
      } else {
        state.configKnown = false;
      }
      if (
        count.status === 'fulfilled' &&
        count.value &&
        typeof count.value === 'object'
      ) {
        const current = finite((count.value as any).current);
        const max = finite((count.value as any).max);
        if (current !== null && max !== null) {
          state.countKnown = true;
          state.current = current;
          state.max = max;
        } else state.countKnown = false;
      } else state.countKnown = false;
    },
    canCommand(command: LegacyCommand) {
      if (!state.configKnown || disposed) return false;
      if (command === 'forceexit') return state.countKnown && state.current > 0;
      if (command === 'start')
        return ['stopped', 'paused'].includes(state.state);
      if (command === 'reload') return true;
      return state.state === 'running';
    },
    async execute(command: LegacyCommand) {
      if (!state.canCommand(command) || state.busy) return;
      state.busy = command;
      try {
        const result = await transport.command(command);
        if (!disposed)
          state.notice = safeNotice(
            result,
            '命令已提交，结果未知；请刷新旧引擎状态核验。',
          );
      } catch {
        if (!disposed) state.notice = '命令结果未知，请刷新旧引擎状态核验。';
      } finally {
        if (!disposed) state.busy = '';
      }
    },
    dispose() {
      disposed = true;
      state.busy = '';
    },
  });
  return state;
}
