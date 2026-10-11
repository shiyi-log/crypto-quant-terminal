import type {
  StrategyLabAccount,
  StrategyLabDetail,
  StrategyLabExecution,
} from '#/api/strategy-lab';

import { parseFreqtradeTime } from '../utils/orderDisplayId';

export type LabPopulation = 'factorial' | 'fnew' | 'microstructure' | 'controls' | 'archived';
export type LabRiskFilter = 'all' | 'R50' | 'R200' | 'R1000';
export type LabStopFilter = 'all' | '1' | '1.5' | '2.5';
export type LabBookGroupFilter = 'all' | 'control' | 'filter';

const factorialSuffix = /_(?:B15m|Cgen2|Dgen3)_(R50|R200|R1000)_k(1\.0|1\.5|2\.5)_100x_maker$/u;
const fnewSuffix = /_Fnew_(R50|R200|R1000)_k(1\.0|1\.5|2\.5)_100x_(?:maker|taker)$/u;
const microstructureSuffix = /_(?:control|filter)_(R50|R200|R1000)_k(1\.0|1\.5|2\.5)_100x_maker$/u;
const legacySuffix = /_(?:100x(?:_maker)?|v2|[A-Z]_?(?:gen\d+(?:_multitf)?|carry|5m|15m|1h|1d)?_?(?:100x_)?(?:maker|taker))$/u;

function numberOf(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (String(value).trim() === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function canonicalLabStrategy(item: StrategyLabAccount | string): string {
  if (typeof item === 'string') {
    return item.replace(factorialSuffix, '').replace(fnewSuffix, '')
      .replace(microstructureSuffix, '').replace(legacySuffix, '');
  }
  return item.variant_of ?? item.definition?.variant_of ??
    item.definition?.canonical_strategy_id ??
    item.definition?.fnew_factors?.strategy ??
    item.definition?.factorial_factors?.strategy ??
    canonicalLabStrategy(item.strategy_id);
}

function riskPopulation(item: StrategyLabAccount): LabPopulation | null {
  const definition = item.definition;
  const population = item.sample_population ?? definition?.sample_population;
  if (definition?.microstructure_experiment_version ||
      population === 'orderbook-confirmation-forward-v1' ||
      microstructureSuffix.test(item.strategy_id)) return 'microstructure';
  if (definition?.fnew_design_version || population === 'fnew-20261012-v1' ||
      fnewSuffix.test(item.strategy_id)) return 'fnew';
  return item.definition?.factorial_design_version || factorialSuffix.test(item.strategy_id)
    ? 'factorial' : null;
}

export function labAccountPopulation(item: StrategyLabAccount): LabPopulation {
  if (item.status === 'archived' || item.lifecycle === 'archived') return 'archived';
  return riskPopulation(item) ?? 'controls';
}

export function labPopulationHasRiskFactors(population: LabPopulation): boolean {
  return ['factorial', 'fnew', 'microstructure'].includes(population);
}

export function labBookGroup(item: StrategyLabAccount): LabBookGroupFilter {
  const group = item.definition?.microstructure_group ??
    item.definition?.factorial_factors?.book_group ??
    item.strategy_id.match(/_(control|filter)_R(?:50|200|1000)_k/u)?.[1];
  return group === 'control' || group === 'filter' ? group : 'all';
}

export function labAccountFactors(item?: StrategyLabAccount): {
  riskBudget: string;
  stopWidth: number | null;
} {
  const definition = item?.definition;
  const suffix = item?.strategy_id.match(factorialSuffix) ??
    item?.strategy_id.match(fnewSuffix) ?? item?.strategy_id.match(microstructureSuffix);
  const factors = definition?.fnew_factors ?? definition?.factorial_factors;
  const actualRisk = numberOf(item?.execution?.max_loss_per_trade_usdt ?? definition?.max_loss_per_trade_usdt);
  const riskBudget = factors?.risk_budget ?? suffix?.[1] ??
    (actualRisk === null || !item || !riskPopulation(item) ? '' : `R${actualRisk}`);
  const params = definition?.params_frozen ?? definition?.params;
  return {
    riskBudget,
    stopWidth: numberOf(factors?.stop_width_k ?? item?.execution?.risk_stop_atr_mult ??
      params?.risk_stop_atr_mult ?? suffix?.[2]),
  };
}

export function filterLabAccounts(
  items: StrategyLabAccount[],
  population: LabPopulation,
  risk: LabRiskFilter,
  stop: LabStopFilter,
  bookGroup: LabBookGroupFilter = 'all',
): StrategyLabAccount[] {
  return items.filter((item) => {
    if (labAccountPopulation(item) !== population) return false;
    if (!labPopulationHasRiskFactors(population)) return true;
    if (population === 'microstructure' && bookGroup !== 'all' &&
        labBookGroup(item) !== bookGroup) return false;
    const factors = labAccountFactors(item);
    return (risk === 'all' || factors.riskBudget === risk) &&
      (stop === 'all' || factors.stopWidth === Number(stop));
  });
}

export function selectLabAccount(
  items: StrategyLabAccount[],
  currentId: string,
  focused: boolean,
): string {
  if (items.some((item) => item.strategy_id === currentId)) return currentId;
  const preferred = focused
    ? ['s03_tsmom_vol_target', 's05_bollinger_fade']
    : ['s11_tsmom_bollinger_pullback', 's03_tsmom_vol_target', 's05_bollinger_fade'];
  for (const strategy of preferred) {
    const match = items.find((item) => canonicalLabStrategy(item) === strategy);
    if (match) return match.strategy_id;
  }
  return items[0]?.strategy_id ?? '';
}

/** Detail from a previous account must never appear under a new selection. */
export function selectedLabDetail(
  value: StrategyLabDetail | null,
  selectedId: string,
): StrategyLabDetail | null {
  return selectedId && value?.account.strategy_id === selectedId ? value : null;
}

/** Warmup tracks closed input candles even before a forward execution begins. */
export function labWarmupCandleTime(item: StrategyLabAccount): number | null {
  return parseFreqtradeTime(item.warmup?.last_closed_at)?.getTime() ?? null;
}

/** The overview model describes legacy accounts, not new risk populations. */
export function selectedLabExecution(
  item: StrategyLabAccount | undefined,
  globalModel: StrategyLabExecution,
): StrategyLabExecution {
  if (item?.execution) return item.execution;
  if (!item || !riskPopulation(item)) return globalModel;
  const definition = item?.definition;
  const params = definition?.params_frozen ?? definition?.params;
  return {
    fee_rate_per_side: definition?.fee_rate_per_side,
    leverage: definition?.position_budget?.leverage,
    max_loss_per_trade_usdt: definition?.max_loss_per_trade_usdt,
    risk_stop_atr_mult: numberOf(params?.risk_stop_atr_mult),
    risk_sizing_mode: definition?.risk_sizing_mode,
    risk_budget_scope: definition?.risk_budget_scope,
    execution_enabled: definition?.execution_enabled,
    blocked_execution_reason: definition?.blocked_execution_reason,
    execution_universe: definition?.execution_universe,
  };
}
