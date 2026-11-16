import { reactive } from 'vue';

export type Fact = Record<string, any>;
export interface OverviewFacts {
  config: Fact | null;
  profit: Fact | null;
  balance: Fact | null;
  positions: Fact[] | null;
  trades: Fact | null;
  ops: Fact | null;
  iteration: Fact | null;
  progress: Fact | null;
  researchProgress: Fact | null;
  jobs: Fact[] | null;
  services: Fact | null;
  whitelist: Fact | null;
}
export type OverviewSource = keyof OverviewFacts;
export type OverviewRequests = Record<OverviewSource, () => Promise<unknown>>;
export const sourceNames: Record<OverviewSource, string> = {
  config: '引擎配置',
  profit: '盈亏统计',
  balance: '账户余额',
  positions: '当前持仓',
  trades: '最近成交',
  ops: '因子监控',
  iteration: '旧模型参数',
  progress: '回测进度',
  researchProgress: '研究进度',
  jobs: '后台任务',
  services: '程序状态',
  whitelist: '选币名单',
};
export function finiteNumber(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (
    typeof value === 'string' &&
    !/^[+-]?(?:[0-9]+(?:[.][0-9]*)?|[.][0-9]+)(?:e[+-]?[0-9]+)?$/i.test(
      value.trim(),
    )
  )
    return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}
export function sumKnown(rows: Fact[], key: string): number | null {
  const values = rows.map((row) => finiteNumber(row[key]));
  return values.some((value) => value === null)
    ? null
    : (values as number[]).reduce((sum, value) => sum + value, 0);
}
export function environmentLabel(config: unknown): string {
  if (!isRecord(config) || typeof config.dry_run !== 'boolean')
    return '运行环境未知';
  return config.dry_run ? '模拟盘 dry-run' : '实盘配置';
}
function isRecord(value: unknown): value is Fact {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function rows(value: unknown): value is Fact[] {
  return Array.isArray(value) && value.every(isRecord);
}
function optionalNumbers(value: Fact, keys: string[]): boolean {
  return keys.every(
    (key) =>
      value[key] === undefined ||
      value[key] === null ||
      finiteNumber(value[key]) !== null,
  );
}
function validTrades(value: unknown): value is Fact[] {
  if (!rows(value)) return false;
  const seen = new Set<string>();
  return value.every((trade) => {
    const id = finiteNumber(trade.trade_id);
    if (
      id === null ||
      !Number.isSafeInteger(id) ||
      id < 0 ||
      seen.has(String(id)) ||
      typeof trade.pair !== 'string' ||
      !trade.pair.trim()
    )
      return false;
    seen.add(String(id));
    return optionalNumbers(trade, [
      'profit_abs',
      'profit_pct',
      'profit_ratio',
      'stake_amount',
      'open_rate',
      'current_rate',
    ]);
  });
}
function validate(key: OverviewSource, value: unknown): boolean {
  if (key === 'positions') return validTrades(value);
  if (key === 'jobs') return rows(value);
  if (key === 'progress' || key === 'researchProgress')
    return (
      value === null ||
      (isRecord(value) &&
        optionalNumbers(value, ['percent']) &&
        (value.status === undefined || typeof value.status === 'string'))
    );
  if (!isRecord(value) || Object.keys(value).length === 0) return false;
  if (key === 'trades')
    return (
      validTrades(value.trades) && optionalNumbers(value, ['total_trades'])
    );
  if (key === 'config')
    return (
      (value.dry_run === undefined || typeof value.dry_run === 'boolean') &&
      ['strategy', 'state', 'timeframe', 'trading_mode'].every(
        (field) =>
          value[field] === undefined || typeof value[field] === 'string',
      )
    );
  if (key === 'profit')
    return optionalNumbers(value, [
      'profit_all_coin',
      'profit_all_percent',
      'profit_closed_coin',
      'profit_closed_percent',
      'winning_trades',
      'losing_trades',
      'closed_trade_count',
      'winrate',
    ]);
  if (key === 'balance')
    return (
      optionalNumbers(value, ['total', 'starting_capital']) &&
      (value.currencies === undefined || rows(value.currencies))
    );
  if (key === 'ops')
    return (
      rows(value.factors) &&
      value.factors.every(
        (factor) =>
          (factor.factor === undefined || typeof factor.factor === 'string') &&
          optionalNumbers(factor, ['ic_base', 'ic_recent']) &&
          (factor.alert === undefined || typeof factor.alert === 'boolean'),
      ) &&
      (value.retrain === undefined || isRecord(value.retrain))
    );
  if (key === 'services') return rows(value.services);
  if (key === 'whitelist')
    return (
      Array.isArray(value.whitelist) &&
      value.whitelist.every((pair) => typeof pair === 'string' && pair.trim())
    );
  return true;
}
export function createOverviewState(requests: OverviewRequests) {
  let disposed = false;
  let generation = 0;
  let inFlight: Promise<void> | null = null;
  let refreshQueued = false;
  const facts = reactive<OverviewFacts>({
    config: null,
    profit: null,
    balance: null,
    positions: null,
    trades: null,
    ops: null,
    iteration: null,
    progress: null,
    researchProgress: null,
    jobs: null,
    services: null,
    whitelist: null,
  });
  const state = reactive({
    facts,
    errors: {} as Partial<Record<OverviewSource, string>>,
    updated: {} as Partial<Record<OverviewSource, string>>,
    loading: false,
    refresh(): Promise<void> {
      if (disposed) return Promise.resolve();
      if (inFlight) {
        refreshQueued = true;
        return inFlight.then(() => {
          if (refreshQueued && !disposed) {
            refreshQueued = false;
            return state.refresh();
          }
        });
      }
      const run = async () => {
        const request = ++generation;
        state.loading = true;
        await Promise.all(
          (Object.keys(sourceNames) as OverviewSource[]).map(async (key) => {
            try {
              const value = await requests[key]();
              if (disposed || request !== generation) return;
              if (!validate(key, value)) throw new Error('invalid snapshot');
              // 每一来源独立提交，失败不得抹掉另一个来源或上次成功事实。
              (facts as Record<string, unknown>)[key] = value;
              state.updated[key] = new Date().toISOString();
              delete state.errors[key];
            } catch {
              if (!disposed && request === generation)
                state.errors[key] =
                  sourceNames[key] + '读取失败，保留上次成功快照。';
            }
          }),
        );
        if (request === generation) state.loading = false;
      };
      inFlight = run();
      return inFlight.finally(() => {
        inFlight = null;
      });
    },
    dispose() {
      disposed = true;
      generation++;
      state.loading = false;
    },
  });
  return state;
}
export type OverviewState = ReturnType<typeof createOverviewState>;
