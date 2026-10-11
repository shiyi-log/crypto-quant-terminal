/** Binance Demo only. Decimal values remain strings from exchange reports. */
import { ftClient, unwrap } from './freqtrade/client';
import { requireSession } from './session';

export type DemoDecimal = null | string;
export type DemoRecord = Record<string, unknown>;

export interface DemoCommissionRateQuote {
  symbol: string;
  makerCommissionRate: DemoDecimal;
  takerCommissionRate: DemoDecimal;
}

export interface DemoConnectionCheck {
  connected?: boolean;
  checkedAt?: number;
  readOnly?: boolean;
  errorCode?: string;
  commissionRates?: DemoCommissionRateQuote[];
  commissionRateObservedAt?: null | number;
  commissionRateSource?: 'demo_signed_account_commission_rate_quote_not_fill';
}

export interface DemoEffectiveFeeRate {
  symbol: string;
  liquidity: 'maker' | 'taker' | 'unknown';
  role: 'entry' | 'exit';
  fillCount: number;
  knownCommissionUsdt: DemoDecimal;
  actualReportedQuoteUsdt: DemoDecimal;
  effectiveFeeRate: DemoDecimal;
}

export interface DemoEvidence {
  environment: 'binance_demo';
  endpoint: 'demo-fapi.binance.com';
  snapshotAt: string;
  scope: 'selected_candle_strategy_execution_only';
  questions: {
    fees: {
      state: 'no_actual_fills' | 'actual_fees_partially_known' | 'actual_fees_observed';
      fillCount: number;
      knownFeeFillCount: number;
      unknownFeeFillCount: number;
      rates: DemoEffectiveFeeRate[];
      chargedByAsset: { asset: string; knownCommission: string }[];
    };
    secondsProfit: {
      state: 'seconds_experiment_not_configured';
      secondsExperimentConfigured: false;
      closedRoundTripCount: number;
      knownNetRoundTripCount: number;
      unknownNetRoundTripCount: number;
      closedNetTradingPnlUsdt: DemoDecimal;
      knownNetSubtotalUsdt: string;
      fundingIncluded: false;
      liveProfitabilityVerified: false;
    };
    execution: {
      state: 'no_exchange_orders' | 'demo_execution_observed';
      exchangeAcceptedEntryCount: number;
      terminalAcceptedEntryCount: number;
      zeroFillTerminalAcceptedEntryCount: number;
      acceptedTerminalNonfillRatio: null | number;
      exchangeRejectedEntryCount: number;
      localTerminalUnconfirmedEntryCount: number;
      pendingEntryCount: number;
      unknownEntryCount: number;
      partialTerminalAcceptedEntryCount: number;
      signalPriceDeviation: {
        fillCount: number;
        knownReferenceFillCount: number;
        meanAdverseDeviationBps: DemoDecimal;
        scope: 'demo_fill_vs_live_paper_signal_includes_environment_and_latency';
      };
      liveLaunchAllowed: false;
      launchDecision: 'manual_review_required';
    };
  };
}

export interface DemoStrategyAccount {
  accountId: string;
  strategyId: string;
  ruleHash: string;
  executionHash: string;
  enabled: boolean;
  eligible: boolean;
  blockedReason?: null | string;
  description?: string;
  timeframe?: string;
}

export interface DemoStatus {
  environment: 'binance_demo';
  endpoint: 'demo-fapi.binance.com';
  credentials: {
    configured: boolean;
    decryptionAvailable?: boolean;
    fingerprint?: null | string;
    maskedApiKey?: null | string;
    updatedAt?: null | number | string;
  };
  runner: {
    state: string;
    selectedAccountId?: null | string;
    tradingEnabled?: boolean;
    workerRunning?: boolean;
    accountLocked?: boolean;
    lastError?: null | string;
    lastCheck?: null | DemoRecord;
    [key: string]: unknown;
  };
  accounts: DemoStrategyAccount[];
  metrics: DemoRecord;
  lastCheck?: null | DemoConnectionCheck;
  limitations: string[];
}

export interface DemoOrder extends DemoRecord {
  symbol?: string;
  orderId?: number | string;
  clientOrderId?: string;
  side?: string;
  type?: string;
  status?: string;
  price?: DemoDecimal;
  origQty?: DemoDecimal;
  executedQty?: DemoDecimal;
  avgPrice?: DemoDecimal;
  cumQuote?: DemoDecimal;
  time?: number;
  updateTime?: number;
}

export interface DemoFill extends DemoRecord {
  id?: number | string;
  orderId?: number | string;
  symbol?: string;
  side?: string;
  price?: DemoDecimal;
  qty?: DemoDecimal;
  quoteQty?: DemoDecimal;
  commission?: DemoDecimal;
  commissionAsset?: null | string;
  realizedPnl?: DemoDecimal;
  maker?: boolean | null;
  time?: number;
}

export interface DemoRows<T extends DemoRecord = DemoRecord> {
  environment: 'binance_demo';
  items: T[];
  limit: number;
}

const basePath = '/locals/binance-demo';

function body(value: unknown): DemoRecord {
  const result = unwrap<unknown>(value);
  if (!result || typeof result !== 'object' || Array.isArray(result)) {
    throw new Error('Demo 服务响应格式异常');
  }
  return result as DemoRecord;
}

function assertDemo(value: DemoRecord) {
  if (value.environment !== 'binance_demo') {
    throw new Error('返回的账户环境不是币安 Demo，已停止展示');
  }
  if ('endpoint' in value && value.endpoint !== 'demo-fapi.binance.com') {
    throw new Error('Demo 服务端点不匹配，已停止操作');
  }
}

function authenticated() {
  requireSession('请先登录后配置币安 Demo');
}

export async function getBinanceDemoStatus(signal?: AbortSignal): Promise<DemoStatus> {
  authenticated();
  const result = body(await ftClient.get(basePath, { signal }));
  assertDemo(result);
  return result as unknown as DemoStatus;
}

export async function getBinanceDemoEvidence(signal?: AbortSignal): Promise<DemoEvidence> {
  authenticated();
  const result = body(await ftClient.get(`${basePath}/evidence`, { signal }));
  assertDemo(result);
  if (result.scope !== 'selected_candle_strategy_execution_only') {
    throw new Error('Demo 证据范围不匹配，已停止展示');
  }
  return result as unknown as DemoEvidence;
}

async function mutate(path: string, payload: DemoRecord): Promise<DemoRecord> {
  authenticated();
  const result = body(await ftClient.post(`${basePath}/${path}`, payload));
  if ('environment' in result) assertDemo(result);
  return result;
}

export function saveBinanceDemoCredentials(apiKey: string, secret: string) {
  // Credentials only travel in this POST body; never place them in a URL or store.
  return mutate('credentials', { apiKey, secret });
}

export function checkBinanceDemoConnection() {
  return mutate('check', {});
}

export function startBinanceDemo(accountId: string) {
  return mutate('start', { accountId });
}

export function stopBinanceDemo() {
  return mutate('stop', {});
}

async function rows<T extends DemoRecord>(path: string, signal?: AbortSignal): Promise<DemoRows<T>> {
  authenticated();
  const result = body(await ftClient.get(`${basePath}/${path}`, {
    ...(path === 'orders' || path === 'fills' || path === 'comparisons' ? { params: { limit: 100 } } : {}), signal,
  }));
  assertDemo(result);
  return {
    environment: 'binance_demo',
    items: Array.isArray(result.items)
      ? result.items.filter((item): item is T => Boolean(item) && typeof item === 'object' && !Array.isArray(item))
      : [],
    limit: typeof result.limit === 'number' ? result.limit : 100,
  };
}

export const getBinanceDemoOrders = (signal?: AbortSignal) => rows<DemoOrder>('orders', signal);
export const getBinanceDemoFills = (signal?: AbortSignal) => rows<DemoFill>('fills', signal);
export const getBinanceDemoPositions = (signal?: AbortSignal) => rows('positions', signal);
export const getBinanceDemoAccount = (signal?: AbortSignal) => rows('account', signal);
export const getBinanceDemoComparisons = (signal?: AbortSignal) => rows('comparisons', signal);
