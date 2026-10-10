/**
 * Freqtrade REST API 封装
 * 所有接口路径对应 /api/v1/*
 *
 * 统一用 unwrap() 取响应体 —— 见 client.ts 中的说明。
 */
import {
  errText,
  FT_SERVER_ORIGIN,
  FT_TOKEN_KEY,
  ftClient,
  unwrap,
} from './client';

export * from './client';

/* ══════════ 类型定义 ══════════ */

export interface FtConfig {
  bot_name?: string;
  dry_run?: boolean;
  exchange?: string;
  margin_mode?: string;
  max_open_trades?: number;
  stake_amount?: number;
  stake_currency?: string;
  state?: string;
  strategy?: string;
  trading_mode?: string;
  version?: string;
  [key: string]: any;
}

export interface FtProfit {
  closed_trade_count?: number;
  losing_trades?: number;
  profit_all_coin?: number;
  profit_all_percent?: number;
  profit_closed_coin?: number;
  profit_closed_percent?: number;
  trade_count?: number;
  winning_trades?: number;
  winrate?: number;
  [key: string]: any;
}

export interface FtBalance {
  currencies?: Array<{
    balance?: number;
    currency?: string;
    est_stake?: number;
    free?: number;
    used?: number;
  }>;
  starting_capital?: number;
  symbol?: string;
  total?: number;
  value?: number;
  [key: string]: any;
}

export interface FtTrade {
  amount?: number;
  close_date?: string;
  close_rate?: number;
  enter_tag?: string;
  is_open?: boolean;
  is_short?: boolean;
  leverage?: number;
  open_date?: string;
  open_rate?: number;
  pair?: string;
  profit_abs?: number;
  profit_pct?: number;
  profit_ratio?: number;
  stake_amount?: number;
  trade_id?: number;
  [key: string]: any;
}

export interface FtCandles {
  all_columns?: string[];
  annotations?: any[];
  columns: string[];
  data: any[][];
  pair: string;
  timeframe: string;
}

/* ══════════ 接口 ══════════ */

export async function getConfig(): Promise<FtConfig> {
  return unwrap<FtConfig>(await ftClient.get('/v1/show_config'));
}

export async function getProfit(): Promise<FtProfit> {
  return unwrap<FtProfit>(await ftClient.get('/v1/profit'));
}

export async function getBalance(): Promise<FtBalance> {
  return unwrap<FtBalance>(await ftClient.get('/v1/balance'));
}

export async function getOpenTrades(): Promise<FtTrade[]> {
  return unwrap<FtTrade[]>(await ftClient.get('/v1/status'));
}

export async function getTrades(limit = 30): Promise<any> {
  return unwrap(await ftClient.get(`/v1/trades?limit=${limit}`));
}

/** 运行时白名单（show_config 不返回，需单独取） */
export async function getWhitelist(): Promise<{ whitelist: string[] }> {
  return unwrap(await ftClient.get('/v1/whitelist'));
}

export async function getLogs(limit = 200): Promise<any> {
  return unwrap(await ftClient.get(`/v1/logs?limit=${limit}`));
}

export async function getPairCandles(
  pair: string,
  timeframe: string,
  limit = 200,
): Promise<FtCandles> {
  return unwrap<FtCandles>(
    await ftClient.get(
      `/v1/pair_candles?pair=${encodeURIComponent(pair)}&timeframe=${timeframe}&limit=${limit}`,
    ),
  );
}

export interface FtLocalOhlcv {
  columns: string[];
  data: any[][];
  last_candle?: string;
  length?: number;
  pair: string;
  source?: string;
  timeframe: string;
}

/**
 * 实时 K 线（认证服务代理 Binance 公共行情接口）。
 *
 * 为什么不直接用本地 feather：
 *   它是「下载时点」的快照，机器人不重启就不更新，图表会停在旧数据上
 *   （实测本地数据停在两天前）。实时源与 /locals/ohlcv 返回同构，
 *   失败会抛错，由调用方回落到本地数据。
 */
export async function getLiveKlines(
  pair: string,
  timeframe: string,
  limit = 200,
  fresh = false,
): Promise<FtLocalOhlcv> {
  return unwrap<FtLocalOhlcv>(
    await ftClient.get(
      `/locals/klines?pair=${encodeURIComponent(pair)}&timeframe=${timeframe}&limit=${limit}${fresh ? '&fresh=1' : ''}`,
    ),
  );
}

/**
 * 本地行情（读服务端已下载的 feather 数据）。
 *
 * 为什么不直接用 Freqtrade 的 /pair_candles：
 *   它只返回「策略自身周期」的数据。机器人跑 5m 策略时，
 *   请求 1h/4h 会返回空数组，图表就是空白。
 *   本地接口不受机器人周期限制，任意已下载周期都能出图。
 */
export async function getLocalOhlcv(
  pair: string,
  timeframe: string,
  limit = 200,
): Promise<FtLocalOhlcv> {
  return unwrap<FtLocalOhlcv>(
    await ftClient.get(
      `/locals/ohlcv?pair=${encodeURIComponent(pair)}&timeframe=${timeframe}&limit=${limit}`,
    ),
  );
}

/**
 * 读取回测/训练任务进度。
 * 由 run_backtest_task.py 写入 Freqtrade 静态目录，经认证服务代理提供，
 * 因此必须带上会话令牌。
 *
 * 去重：同一份 run_progress.json 被总览页、TaskProgress 组件、回测页
 * 三处以 3~5 秒的间隔各自轮询，会重复打同一个请求。
 * 这里做「并发合并 + 2.5 秒短缓存」，三个调用方共用一次真实请求。
 * TTL 小于最小的 3 秒轮询间隔，因此调用方不会拿到过期数据。
 */
let runProgressCache: { at: number; value: any | null } | null = null;
let runProgressInflight: Promise<any | null> | null = null;
const RUN_PROGRESS_TTL_MS = 2500;

async function fetchRunProgress(): Promise<any | null> {
  try {
    const token = localStorage.getItem(FT_TOKEN_KEY) ?? '';
    const res = await fetch(
      `${FT_SERVER_ORIGIN}/run_progress.json?t=${Date.now()}`,
      {
        cache: 'no-store',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      },
    );
    if (!res.ok) {
      return null;
    }
    return await res.json();
  } catch {
    return null;
  }
}

export async function getRunProgress(): Promise<any | null> {
  if (
    runProgressCache &&
    Date.now() - runProgressCache.at < RUN_PROGRESS_TTL_MS
  ) {
    return runProgressCache.value;
  }
  if (runProgressInflight) {
    return runProgressInflight;
  }
  runProgressInflight = fetchRunProgress().then((value) => {
    runProgressCache = { at: Date.now(), value };
    runProgressInflight = null;
    return value;
  });
  return runProgressInflight;
}

/**
 * 模型自动迭代任务进度（bot/auto_research.py 写入 research_progress.json）
 * 与 run_progress.json 同构，另附 trial 账本统计。
 */
export async function getResearchProgress(): Promise<any | null> {
  try {
    return unwrap(await ftClient.get('/locals/research_progress'));
  } catch {
    return null;
  }
}

/**
 * 整个程序的运行状况（供「总览」页展示）
 * 与 start.sh status 同源：端口探活 + 进程名匹配 + 各服务状态文件
 */
export async function getServices(): Promise<any | null> {
  try {
    return unwrap(await ftClient.get('/locals/services'));
  } catch {
    return null;
  }
}

/**
 * 可用的回测明细列表（每个模型一次回测）
 */
export async function getBacktestList(): Promise<{
  items: Array<{ identifier: string; strategy?: string; summary: any }>;
}> {
  return unwrap(await ftClient.get('/locals/backtest_list'));
}

/**
 * 单次回测的完整交易明细（含每笔交易的决策依据）
 */
export async function getBacktestDetail(identifier: string): Promise<{
  by_exit: Array<{ reason: string; trades: number; profit_abs: number }>;
  by_pair: Array<{
    pair: string;
    trades: number;
    wins: number;
    profit_abs: number;
  }>;
  equity: [string, number][];
  identifier: string;
  strategy: string;
  summary: Record<string, any>;
  thresholds: { entry: number; exit: number };
  trades: Array<Record<string, any>>;
}> {
  return unwrap(
    await ftClient.get(
      `/locals/backtest_detail?identifier=${encodeURIComponent(identifier)}`,
    ),
  );
}

/**
 * 策略研究汇总（模型对比 + 路线结论 + Carry 回测）
 */
export async function getResearch(): Promise<{
  carry: any;
  generated_at: string;
  models: any[];
  routes: any[];
}> {
  return unwrap(await ftClient.get('/locals/research'));
}

/**
 * 策略研究数据的刷新状态。
 *
 * build_research.py 原先是纯手工脚本、无人调度，页面会长期停在旧数据上；
 * 现在由 bot/refresh_research.py 每 6 小时重建，这里供页面显示数据新旧与
 * 最近一次刷新的成败（失败时保留上一份 summary）。
 */
export async function getResearchStatus(): Promise<{
  attempt_at?: string;
  duration_s?: number;
  error?: string;
  ok?: boolean;
  refresh_running?: boolean;
  summary_generated_at?: string;
}> {
  return unwrap(await ftClient.get('/locals/research_status'));
}

/**
 * 实盘运维状态（策略信号 / 波动率中枢 / 因子健康度 / 重估信号）
 */
/** 自动迭代巡检状态（C1~C5） */
/** 深度学习迭代状态 */
export async function getMlStatus(): Promise<any> {
  return unwrap(await ftClient.get('/locals/ml'));
}

export async function getAutoIterate(): Promise<any> {
  return unwrap(await ftClient.get('/locals/auto_iterate'));
}

export async function getOps(): Promise<any> {
  return unwrap(await ftClient.get('/locals/ops'));
}

/**
 * 模型迭代结果（走查四变量对比：池规模/信号选择/仓位分配/参数稳定性）
 */
export async function getIteration(): Promise<any> {
  return unwrap(await ftClient.get('/locals/iteration'));
}

/**
 * 模型版本注册表 —— 正在使用的版本（champion）vs 最新迭代的版本（challenger）
 * 数据来源: bot/model_registry.py → bot/user_data/model_versions.json
 */
export async function getModelVersions(): Promise<any> {
  return unwrap(await ftClient.get('/locals/model_versions'));
}

/**
 * 人工上线闸门：把某个版本设为「正在使用」。
 * 理由必填（写入版本历史）；未通过判据的版本需 confirmUnpassed=true 才能通过。
 * 注意：只改版本注册表，不会自动改实盘 config。
 */
export async function promoteModelVersion(
  id: string,
  note: string,
  confirmUnpassed = false,
): Promise<any> {
  return unwrap(
    await ftClient.post('/locals/model_versions/promote', {
      id,
      note,
      confirm_unpassed: confirmUnpassed,
    }),
  );
}

/* ══════════ 回测标识符中文映射 ══════════
 * identifier（如 universe15-trf-1h-7d）是模型目录名，同时也写进 FreqAI
 * 的 config，属于「机器名」不能改；这里只做展示层的翻译。
 * 按「-」分段整段匹配，匹配不上的片段原样保留，新组合不会显示成空白。
 */
const IDENT_TOKENS: Record<string, string> = {
  'btc-eth': 'BTC/ETH',
  '1h': '1小时',
  '4h': '4小时',
  '5m': '5分钟',
  '7d': '7天预测',
  '30d': '30天预测',
  funding: '资金费率',
  lgb: 'LightGBM',
  mlp: 'PyTorch MLP',
  trf: 'Transformer',
  universe15: '15币种池',
  universe60: '60币种池',
  xgb: 'XGBoost',
};

export function modelLabel(identifier?: string): string {
  if (!identifier) return '—';
  // 长片段优先：btc-eth 要吃掉 btc、eth 两段，不能只匹配单段
  const keys = Object.keys(IDENT_TOKENS).sort(
    (a, b) => b.split('-').length - a.split('-').length || b.length - a.length,
  );
  const parts = identifier.split('-');
  const out: string[] = [];
  let i = 0;
  while (i < parts.length) {
    let hit = false;
    for (const key of keys) {
      const kp = key.split('-');
      if (
        kp.length <= parts.length - i &&
        kp.every((p, j) => p === parts[i + j])
      ) {
        out.push(IDENT_TOKENS[key]!);
        i += kp.length;
        hit = true;
        break;
      }
    }
    if (!hit) {
      out.push(parts[i]!);
      i += 1;
    }
  }
  return out.join(' · ');
}

/* ══════════ 退出原因中文映射 ══════════ */
export const EXIT_LABELS: Record<string, string> = {
  ai_exit_long: 'AI 平多',
  ai_exit_short: 'AI 平空',
  ai_long: 'AI 开多',
  ai_short: 'AI 开空',
  exit_signal: '退出信号',
  force_exit: '强制平仓(回测结束)',
  roi: '目标收益(ROI)',
  stop_loss: '止损',
  trailing_stop_loss: '追踪止损',
};

/* ══════════════════════════════════════════════════════════════
 * 交易控制
 *
 * 这些端点挂在 Freqtrade 的 `api_trading` 路由上，依赖 is_trading_mode，
 * 因此在 `freqtrade trade` 运行中的实盘 bot 上【直接可用】。
 *
 * 对照：回测 / 下载数据 / 分析类端点挂在 is_webserver_mode 的路由下，
 * 在实盘 bot 上会返回 503 "Bot is not in the correct state."
 * （见 freqtrade/rpc/api_server/webserver.py:218-278 与 deps.py:68）
 * ══════════════════════════════════════════════════════════════ */

export interface FtCount {
  current?: number;
  max?: number;
  total_stake?: number;
  [key: string]: any;
}

/** 当前开仓数与上限（用于控制条实时显示） */
export async function getCount(): Promise<FtCount> {
  return unwrap<FtCount>(await ftClient.get('/v1/count'));
}

/** 启动机器人主循环 */
export async function postStartBot(): Promise<any> {
  return unwrap(await ftClient.post('/v1/start'));
}

/** 停止机器人（同时停止处理未平仓交易） */
export async function postStopBot(): Promise<any> {
  return unwrap(await ftClient.post('/v1/stop'));
}

/** 暂停买入（StopBuy）：继续管理已有持仓，但不开新仓、不加仓 */
export async function postStopBuy(): Promise<any> {
  return unwrap(await ftClient.post('/v1/stopbuy'));
}

/** 重载配置（含策略），会把运行期临时改动一并重置 */
export async function postReloadConfig(): Promise<any> {
  return unwrap(await ftClient.post('/v1/reload_config'));
}

export interface ForceExitParams {
  /** 部分平仓的数量，单位为基础币种（仅 amount < 持仓量 时才生效） */
  amount?: number;
  /** 限价单价格（ordertype='limit' 时必填；市价单不需要） */
  ordertype?: 'limit' | 'market';
  /** 限价单价格（与 ordertype='limit' 搭配使用） */
  price?: number;
  /** 交易 ID；传 'all' 表示对全部未平仓交易执行平仓 */
  tradeid: number | string;
}

/** 强制平仓：单笔（可市价/限价/部分数量）或全部（tradeid='all'） */
export async function postForceExit(params: ForceExitParams): Promise<any> {
  return unwrap(await ftClient.post('/v1/forceexit', params));
}

export interface ForceEnterParams {
  entry_tag?: string;
  leverage?: number;
  ordertype?: 'limit' | 'market';
  pair: string;
  price?: number;
  side?: 'long' | 'short';
  stakeamount?: number;
}

/** 手动开仓（或对已有持仓加仓） */
export async function postForceEnter(params: ForceEnterParams): Promise<any> {
  return unwrap(await ftClient.post('/v1/forceenter', params));
}

/** 删除交易记录（仅从数据库移除，不平仓） */
export async function deleteTrade(tradeId: number | string): Promise<any> {
  return unwrap(await ftClient.delete(`/v1/trades/${tradeId}`));
}

/** 撤销该交易在交易所的挂单 */
export async function cancelOpenOrder(tradeId: number | string): Promise<any> {
  return unwrap(await ftClient.delete(`/v1/trades/${tradeId}/open-order`));
}

/** 从交易所重新拉取该交易的状态并同步到本地 */
export async function reloadTrade(tradeId: number | string): Promise<any> {
  return unwrap(await ftClient.post(`/v1/trades/${tradeId}/reload`));
}

/** 单笔交易的自定义数据（策略写入的 custom_data） */
export async function getTradeCustomData(
  tradeId: number | string,
): Promise<any> {
  return unwrap(await ftClient.get(`/v1/trades/${tradeId}/custom-data`));
}

/** 单笔交易详情（含订单列表） */
export async function getTrade(tradeId: number | string): Promise<any> {
  return unwrap(await ftClient.get(`/v1/trade/${tradeId}`));
}

/* ══════════ 黑名单与 Pair Lock（同属 api_trading，实盘可用）══════════ */

export interface FtBlacklist {
  blacklist: string[];
  blacklist_expanded: string[];
  /**
   * 注意：错误值**不是**字符串，而是 `{ error_msg: string }` 对象。
   * 实测 DELETE /v1/blacklist?pairs_to_delete=FAKE/USDT 返回
   * `{"errors":{"FAKE/USDT":{"error_msg":"Pair FAKE/USDT is not in the current blacklist."}}}`
   * 因此渲染前必须取 error_msg，直接拼字符串会得到 [object Object]。
   */
  errors: Record<string, any>;
  length: number;
  method?: string[];
}

export async function getBlacklist(): Promise<FtBlacklist> {
  return unwrap<FtBlacklist>(await ftClient.get('/v1/blacklist'));
}

/** 加入黑名单（立即生效，直到重载配置） */
export async function addBlacklist(pairs: string[]): Promise<FtBlacklist> {
  return unwrap<FtBlacklist>(
    await ftClient.post('/v1/blacklist', { blacklist: pairs }),
  );
}

/**
 * 从黑名单移除。
 * 后端参数是重复的 query key（pairs_to_delete=A&pairs_to_delete=B），
 * 因此手工拼查询串，避免 axios 数组序列化成 `[]` 形式导致匹配不上。
 */
export async function deleteBlacklist(pairs: string[]): Promise<FtBlacklist> {
  const qs = pairs
    .map((p) => `pairs_to_delete=${encodeURIComponent(p)}`)
    .join('&');
  return unwrap<FtBlacklist>(await ftClient.delete(`/v1/blacklist?${qs}`));
}

export interface FtLock {
  active: boolean;
  id: number;
  lock_end_time: string;
  lock_end_timestamp: number;
  lock_time: string;
  lock_timestamp: number;
  pair: string;
  reason?: string;
  side: string;
}

export async function getLocks(): Promise<{
  lock_count: number;
  locks: FtLock[];
}> {
  return unwrap(await ftClient.get('/v1/locks'));
}

/** 解除一笔保护锁（protections 触发的冷却期） */
export async function deleteLock(lockId: number | string): Promise<any> {
  return unwrap(await ftClient.delete(`/v1/locks/${lockId}`));
}

/* ══════════ 实盘统计（同属 api_trading，实盘可用）══════════
 * 注意：页面上原有的资金曲线全部来自【回测产物】，与实盘无关。
 * 这一组接口才是实盘真实口径。
 */

export interface FtDailyRecord {
  abs_profit: number;
  date: string;
  fiat_value: number;
  rel_profit: number;
  starting_balance: number;
  trade_count: number;
}

export interface FtDaily {
  data: FtDailyRecord[];
  fiat_display_currency: string;
  stake_currency: string;
}

/** 按日/周/月的盈亏分解 */
export async function getDaily(timescale = 30): Promise<FtDaily> {
  return unwrap<FtDaily>(
    await ftClient.get(`/v1/daily?timescale=${timescale}`),
  );
}

export async function getWeekly(timescale = 30): Promise<FtDaily> {
  return unwrap<FtDaily>(
    await ftClient.get(`/v1/weekly?timescale=${timescale}`),
  );
}

export async function getMonthly(timescale = 30): Promise<FtDaily> {
  return unwrap<FtDaily>(
    await ftClient.get(`/v1/monthly?timescale=${timescale}`),
  );
}

export interface FtStats {
  durations: Record<string, null | number>;
  exit_reasons: Record<string, { draws: number; losses: number; wins: number }>;
}

/** 出场原因统计与持仓时长 */
export async function getStats(): Promise<FtStats> {
  return unwrap<FtStats>(await ftClient.get('/v1/stats'));
}

export interface FtStatRow {
  count: number;
  enter_tag?: string;
  exit_reason?: string;
  mix_tag?: string;
  pair?: string;
  profit?: number;
  profit_abs: number;
  profit_pct: number;
  profit_ratio: number;
}

/** 分币对绩效 */
export async function getPerformance(): Promise<FtStatRow[]> {
  return unwrap<FtStatRow[]>(await ftClient.get('/v1/performance'));
}

/** 按入场标签归因 */
export async function getEntryStats(): Promise<FtStatRow[]> {
  return unwrap<FtStatRow[]>(await ftClient.get('/v1/entries'));
}

/** 按出场原因归因 */
export async function getExitStats(): Promise<FtStatRow[]> {
  return unwrap<FtStatRow[]>(await ftClient.get('/v1/exits'));
}

/** 按入场×出场组合标签归因 */
export async function getMixTagStats(): Promise<FtStatRow[]> {
  return unwrap<FtStatRow[]>(await ftClient.get('/v1/mix_tags'));
}

export interface FtWalletHistory {
  capture_start_ts: number;
  columns: string[];
  data: any[][];
  length: number;
}

/** 实盘钱包余额历史（净值曲线的真实来源） */
export async function getHistoricBalance(): Promise<FtWalletHistory> {
  return unwrap<FtWalletHistory>(await ftClient.get('/v1/historic_balance'));
}

/* ══════════ 研究任务（webserver 模式端点）══════════
 * 下面这些端点依赖 is_webserver_mode：在 `freqtrade trade` 的实盘 bot 上
 * 会返回 503。需要另起一个 `freqtrade webserver` 实例（见 start.sh 的 web 子命令），
 * 并把 FT_API_URL 指向它，或由认证服务按前缀分流。
 * 在 503 时前端应显示明确提示，而不是报「未知错误」。
 */

/** 判断一个错误是否为「需要 webserver 模式」 */
export function isWebserverOnlyError(error: any): boolean {
  const msg = errText(error);
  return /webserver mode|not in the correct state|503/i.test(msg);
}

/* ══════════════════════════════════════════════════════════════
 * 研究任务（走 webserver 实例，前端路径前缀 /web/v1/...）
 *
 * 链路：浏览器 → 认证服务 8890 的 /api/web/ → freqtrade webserver 8891
 * 认证服务把前缀去掉后转发，并代持 8891 的令牌 ——
 * 因此这里和实盘接口用的是同一个 ftClient，只是路径多了 /web 前缀。
 * ══════════════════════════════════════════════════════════════ */

/** webserver 实例上可用的策略列表 */
export async function getWebStrategies(): Promise<{ strategies: string[] }> {
  return unwrap(await ftClient.get('/web/v1/strategies'));
}

export interface FtBacktestRequest {
  /** 回测缓存：none / day / week / month */
  backtest_cache?: string;
  dry_run_wallet?: number;
  /** 必须显式提供，后端无默认值 */
  enable_protections?: boolean;
  freqaimodel?: string;
  max_open_trades?: number | string;
  stake_amount?: number | string;
  strategy: string;
  timeframe?: string;
  timeframe_detail?: string;
  timerange?: string;
}

export interface FtBacktestStatus {
  backtest_result?: any;
  progress: number;
  running: boolean;
  status: string;
  status_msg: string;
  step: string;
  trade_count?: null | number;
}

/** 发起回测（异步；用 getBacktestStatus() 轮询进度） */
export async function postBacktest(
  payload: FtBacktestRequest,
): Promise<FtBacktestStatus> {
  return unwrap(
    await ftClient.post('/web/v1/backtest', {
      enable_protections: false,
      ...payload,
    }),
  );
}

/** 回测进度 / 结果 */
export async function getBacktestStatus(): Promise<FtBacktestStatus> {
  return unwrap(await ftClient.get('/web/v1/backtest'));
}

/** 中止正在跑的回测 */
export async function abortBacktest(): Promise<FtBacktestStatus> {
  return unwrap(await ftClient.get('/web/v1/backtest/abort'));
}

/** 清空内存中的回测结果 */
export async function resetBacktest(): Promise<FtBacktestStatus> {
  return unwrap(await ftClient.delete('/web/v1/backtest'));
}

export interface FtBacktestHistoryEntry {
  backtest_end_ts?: null | number;
  backtest_start_time: number;
  backtest_start_ts?: null | number;
  filename: string;
  notes?: null | string;
  run_id: string;
  strategy: string;
  timeframe?: null | string;
  timeframe_detail?: null | string;
}

/** 历史回测列表（磁盘上的 backtest_results/*.json） */
export async function getBacktestHistory(): Promise<FtBacktestHistoryEntry[]> {
  return unwrap(await ftClient.get('/web/v1/backtest/history'));
}

/** 删除一份历史回测结果 */
export async function deleteBacktestHistory(
  filename: string,
): Promise<FtBacktestHistoryEntry[]> {
  return unwrap(
    await ftClient.delete(
      `/web/v1/backtest/history/${encodeURIComponent(filename)}`,
    ),
  );
}

/**
 * 给历史回测写备注。
 *
 * ⚠️ 两个坑（都已实测踩到）：
 *  1. 请求体**必须带 `strategy`**，Freqtrade 的 BacktestMetadataUpdate 把它列为必填，
 *     只发 notes 会得到 422 `Field required`。
 *  2. RequestClient 没有封装 PATCH，这里用它的 axios 实例发一次。
 */
export async function setBacktestNotes(
  filename: string,
  strategy: string,
  notes: string,
): Promise<FtBacktestHistoryEntry[]> {
  const raw = await ftClient.instance.patch(
    `/web/v1/backtest/history/${encodeURIComponent(filename)}`,
    { notes, strategy },
  );
  return unwrap(raw);
}

export interface FtDownloadDataRequest {
  /** 与 timerange 二选一 */
  candle_types?: string[];
  days?: number;
  download_trades?: boolean;
  erase?: boolean;
  pairs: string[];
  prepend_data?: boolean;
  timeframes?: string[];
  timerange?: string;
  trading_mode?: string;
}

export interface FtJobStarted {
  job_id: string;
  status: string;
}

/** 发起数据下载（异步；用 getBackgroundJob(job_id) 轮询） */
export async function postDownloadData(
  payload: FtDownloadDataRequest,
): Promise<FtJobStarted> {
  return unwrap(await ftClient.post('/web/v1/download_data', payload));
}

export interface FtBackgroundJob {
  error?: null | string;
  job_category?: string;
  job_id: string;
  progress?: null | number;
  progress_tasks?: null | Record<string, { progress?: number; total?: number }>;
  running: boolean;
  status: string;
}

/** 后台任务状态（下载数据 / 前瞻分析 / 递归分析 共用，建议 1 秒轮询） */
export async function getBackgroundJob(
  jobId: string,
): Promise<FtBackgroundJob> {
  return unwrap(await ftClient.get(`/web/v1/background/${jobId}`));
}

/** 列出当前所有后台任务 */
export async function getBackgroundJobs(): Promise<FtBackgroundJob[]> {
  return unwrap(await ftClient.get('/web/v1/background'));
}

export interface FtLookaheadRequest {
  lookahead_allow_limit_orders?: boolean;
  minimum_trade_amount?: number;
  strategy: string;
  targeted_trade_amount?: number;
  timeframe?: string;
  timerange?: string;
}

/** 发起前视偏差分析（异步） */
export async function postLookaheadAnalysis(
  payload: FtLookaheadRequest,
): Promise<FtJobStarted> {
  return unwrap(await ftClient.post('/web/v1/lookahead_analysis', payload));
}

/** 前视偏差分析结果 */
export async function getLookaheadResult(jobId: string): Promise<any> {
  return unwrap(await ftClient.get(`/web/v1/lookahead_analysis/${jobId}`));
}

export interface FtRecursiveRequest {
  startup_candle?: number[];
  strategy: string;
  timeframe?: string;
  timerange?: string;
}

/** 发起递归分析（寻找合适的 startup_candle_count，异步） */
export async function postRecursiveAnalysis(
  payload: FtRecursiveRequest,
): Promise<FtJobStarted> {
  return unwrap(await ftClient.post('/web/v1/recursive_analysis', payload));
}

/** 递归分析结果 */
export async function getRecursiveResult(jobId: string): Promise<any> {
  return unwrap(await ftClient.get(`/web/v1/recursive_analysis/${jobId}`));
}

/** webserver 实例能看到的全部交易对（用于下载数据的选币） */
export async function getWebAvailablePairs(): Promise<{
  length: number;
  pairs: string[];
}> {
  return unwrap(await ftClient.get('/web/v1/available_pairs'));
}

/* ══════════ WebSocket 实时推送 ══════════ */

export interface FtWsInfo {
  available: boolean;
  ws_url: string;
}

/**
 * 取 WebSocket 订阅地址（含 ws_token）。
 *
 * 认证服务是 BaseHTTPRequestHandler，无法代理 WS 升级，因此由它确认登录态后
 * 下发地址，浏览器直连实盘 bot 的 /api/v1/message/ws。
 * 取不到地址时调用方应静默回退到轮询。
 */
export async function getWsToken(): Promise<FtWsInfo> {
  return unwrap<FtWsInfo>(await ftClient.get('/ws-token'));
}

export interface ResearchLedgerVersion {
  id: string;
  round?: number | null;
  layer?: string;
  config?: Record<string, unknown> | string | null;
  direction?: string;
  changes?: Array<
    | string
    | {
        t?: string | number | null;
        event?: string | null;
        note?: string | null;
      }
  >;
  registry_status?: string;
  deployment_status: 'not_deployed' | 'unverified' | 'verified';
  evaluation_status: 'invalidated' | 'unverified' | 'forward_observation';
  reported_metrics?: {
    ic?: number | null;
    t?: number | null;
    q?: number | null;
  };
  effect?: {
    actual_orders?: number | null;
    closed_trades?: number | null;
    realized_profit_abs?: number | null;
  };
  evidence?: string[];
  limitations?: string[];
}

export interface ResearchLedger {
  generated_at?: string | null;
  last_synced_at?: string | null;
  source_id?: string | null;
  summary: {
    version_count: number;
    deployed_ml_count: number;
    actual_trade_count: number;
    actual_order_count: number;
    closed_trade_count: number;
    realized_profit_abs: number | null;
    unattributed_trade_count: number;
  };
  versions: ResearchLedgerVersion[];
  comparison?: {
    baseline_id?: string | null;
    candidate_id?: string | null;
    config_changes?: Array<
      | string
      | {
          key: string;
          before?: unknown;
          after?: unknown;
        }
    >;
    note?: string;
  } | null;
  actual_trades: Array<{
    trade_id: number | string;
    pair: string;
    side: string;
    leverage?: number | null;
    stake_amount?: number | null;
    open_date?: string | null;
    close_date?: string | null;
    is_open?: boolean | null;
    realized_profit_abs: number | null;
    model_id: string | null;
    attribution_status: string;
  }>;
  actual_orders: Array<{
    order_id: number | string;
    trade_id?: number | string | null;
    pair: string;
    side: string;
    status: string;
    amount?: number | null;
    filled?: number | null;
    price?: number | null;
    order_date?: string | null;
    model_id: string | null;
  }>;
  lessons: Array<{
    id: string;
    title: string;
    status: string;
    reason: string;
    evidence: string[];
    do_not_repeat: string | boolean;
  }>;
  directions: Array<{
    id: string;
    title: string;
    status: string;
    hypothesis: string;
    next_check: string;
  }>;
  errors: string[];
}

export async function getResearchLedger(): Promise<ResearchLedger> {
  return unwrap<ResearchLedger>(await ftClient.get('/locals/research-ledger'));
}

export interface ForwardPaperVariantSummary {
  closed_trade_count?: number;
  realized_profit_after_fee_before_unknown_costs?: number | null;
  unrealized_pnl_before_unknown_costs?: number | null;
  ending_equity_marked?: number | null;
  open_position_count?: number;
  strategy_usable?: boolean;
  database_ledger?: {
    enabled?: boolean;
    synced_event_count?: number;
    sync_error?: string | null;
  };
  slippage?: 'unknown' | string;
  funding?: 'unknown' | string;
  net_profit?: number | null;
  diagnostics?: Record<string, unknown>;
  summary_scope_start_utc?: string | null;
  last_successful_replay?: boolean;
  [key: string]: unknown;
}

export interface ForwardPaperEvent {
  action?: string;
  candle_utc?: string | null;
  candidates?: Array<Record<string, unknown>>;
  coin?: string;
  decided_at_utc?: string | null;
  decision_reason?: string | null;
  event_id?: string;
  event_type?: string;
  execution_at_utc?: string | null;
  exits?: Array<Record<string, unknown>>;
  fee?: number | null;
  filled_at_utc?: string | null;
  price?: number | null;
  profit_abs?: number | null;
  quantity?: number | null;
  reason?: string | null;
  rule_hash?: string;
  side?: string;
  variant_id?: string;
  [key: string]: unknown;
}

export interface ForwardPaperResponse {
  run_id: string;
  manifest: {
    variants?: Array<{
      variant_id: string;
      rule_hash?: string;
      hypothesis?: string;
      chan_entry?: number;
      chan_exit?: number;
      [key: string]: unknown;
    }>;
    [key: string]: any;
  };
  latest_snapshot?: {
    data_ready?: boolean;
    data_fingerprint?: string | null;
    candle_through_utc?: string | null;
    missing?: string[];
    [key: string]: unknown;
  } | null;
  checkpoint?: {
    candle_through_utc?: string | null;
    data_fingerprint?: string | null;
    variant_summaries?: Record<string, ForwardPaperVariantSummary>;
    [key: string]: unknown;
  } | null;
  variants?: Record<string, ForwardPaperVariantSummary>;
  last_successful_replay?: {
    data_ready?: boolean;
    replay_completed?: boolean;
    candle_through_utc?: string | null;
    data_fingerprint?: string | null;
    variants?: Record<string, ForwardPaperVariantSummary>;
  } | null;
  summary: {
    data_ready: boolean;
    data_fingerprint?: string | null;
    candle_through_utc?: string | null;
    decision_count: number;
    fill_count: number;
    entry_count: number;
    close_count: number;
    closed_trade_count: number | null;
    closed_pnl: number | null;
    cross_variant_closed_trade_count?: number;
    cross_variant_closed_pnl?: number;
    unrealized_pnl?: number | null;
    slippage: string;
    funding: string;
    net_pnl?: number | null;
    database_ledger?: {
      enabled?: boolean;
      synced_event_count?: number;
      sync_error?: string | null;
    };
    cost_completeness?: Record<string, string>;
  };
  counts: { decisions: number; fills: number; closes: number };
  decisions: ForwardPaperEvent[];
  fills: ForwardPaperEvent[];
  closes: ForwardPaperEvent[];
  limit: number;
}

/** 当前认证用户可读的前向纸面运行摘要（默认取最近一次运行）。 */
export async function getForwardPaper(
  runId?: string,
  limit = 30,
): Promise<ForwardPaperResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (runId) params.set('run_id', runId);
  return unwrap<ForwardPaperResponse>(
    await ftClient.get(`/locals/forward-paper?${params.toString()}`),
  );
}
