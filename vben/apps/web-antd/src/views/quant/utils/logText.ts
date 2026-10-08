/**
 * Freqtrade 日志中文化
 *
 * Freqtrade 自身只输出英文日志，且不支持语言包。
 * 这里在前端做一层模式翻译：保留原始技术信息（PID、路径、异常栈），
 * 把常见的英文描述转成中文，便于阅读。
 *
 * 未匹配到的日志会原样返回，不会丢信息。
 */

export const LEVEL_CN: Record<string, string> = {
  DEBUG: '调试',
  ERROR: '错误',
  INFO: '信息',
  WARNING: '警告',
  CRITICAL: '严重',
};

export const STATE_CN: Record<string, string> = {
  RUNNING: '运行中',
  STOPPED: '已停止',
  PAUSED: '已暂停',
  RELOAD_CONFIG: '重载配置',
  running: '运行中',
  stopped: '已停止',
  paused: '已暂停',
};

const RULES: Array<[RegExp, string | ((...m: string[]) => string)]> = [
  // ── 心跳 / 状态 ──
  [
    /^Bot heartbeat\.\s*PID=(\d+),\s*version='([^']*)',\s*state='([^']*)'/,
    (_m, pid, ver, st) =>
      `机器人心跳 · 进程 ${pid} · 版本 ${ver} · 状态 ${STATE_CN[st] ?? st}`,
  ],
  [/^Changing state to:\s*(\w+)/, (_m, s) => `切换运行状态：${STATE_CN[s] ?? s}`],
  [
    /^Sending rpc message:\s*\{'type':\s*(\w+),\s*'status':\s*'?([^'}']*)'?\}?/,
    (_m, type, st) => {
      const t: Record<string, string> = {
        status: '状态',
        warning: '警告',
        startup: '启动',
        exception: '异常',
      };
      return `${t[type] ?? type}推送：${STATE_CN[st] ?? st}`;
    },
  ],

  // ── 启动流程 ──
  [/^Starting worker\s*(.*)$/, (_m, v) => `启动工作进程 ${v}`.trim()],
  [/^Starting freqtrade in (\w+) mode/, (_m, m) => `以「${m}」模式启动 Freqtrade`],
  [/^Using config:\s*(.+)$/, (_m, p) => `加载配置：${p}`],
  [/^Using user-data directory:\s*(.+)$/, (_m, p) => `用户目录：${p}`],
  [/^Using data directory:\s*(.+)$/, (_m, p) => `数据目录：${p}`],
  [/^Using DB:\s*(.+)$/, (_m, p) => `使用数据库：${p}`],
  [/^Logfile configured$/, () => `日志文件已就绪`],
  [/^Dry run is enabled/, () => `干跑模式已启用（只模拟，不动真钱）`],
  [/^Instance is running with dry_run enabled/, () => `实例以干跑模式运行`],
  [/^Runmode set to (\w+)/, (_m, m) => `运行模式：${m}`],

  // ── 交易所 / 策略 ──
  [/^Checking exchange\.\.\./, () => `正在检查交易所…`],
  [
    /^Exchange "(\w+)" is officially supported/,
    (_m, e) => `交易所 ${e} 属官方支持列表`,
  ],
  [/^Using Exchange "(\w+)"/, (_m, e) => `使用交易所：${e}`],
  [
    /Using resolved exchange class '(\w+)'/,
    (_m, e) => `已解析交易所类：${e}`,
  ],
  [
    /Using resolved strategy (\w+) from '([^']+)'/,
    (_m, s, p) => `已加载策略 ${s}（${p}）`,
  ],
  [/^Using resolved pairlist (\w+)/, (_m, p) => `使用币对列表：${p}`],
  [/^Using CCXT ([\d.]+)/, (_m, v) => `CCXT 版本：${v}`],
  [/^Time offset to (\w+) is (-?\d+)ms/, (_m, e, ms) => `与 ${e} 时间偏差：${ms} 毫秒`],
  [
    /Applying additional ccxt config:\s*(.+)$/,
    (_m, c) => `应用 ccxt 附加配置：${c}`,
  ],

  // ── 币对列表 ──
  [/^Starting initial pairlist refresh$/, () => `开始首次刷新币对列表`],
  [/^Initial Pairlist refresh took ([\d.]+)s/, (_m, s) => `首次刷新完成，耗时 ${s} 秒`],
  [
    /^Whitelist with (\d+) pairs:\s*(.+)$/,
    (_m, n, p) => `白名单 ${n} 个币对：${p}`,
  ],

  // ── 下单 / 交易 ──
  [/^Found no parameter file\./, () => `未找到超参文件（使用默认值）`],
  [/^Strategy Parameter\(default\):\s*(\w+)\s*=\s*(.+)$/, (_m, k, v) => `策略参数默认值 ${k} = ${v}`],
  [/^No params for (\w+) found, using default values\./, (_m, s) => `${s} 无自定义参数，使用默认值`],
  [/^No protection Handlers defined\./, () => `未定义保护规则`],

  // ── FreqAI ──
  [/^Training (\d+) timeranges/, (_m, n) => `开始训练：共 ${n} 个时间窗口`],
  [
    /^Training (\S+),\s*(\d+)\/(\d+) pairs from (.+?) to (.+?),\s*(\d+)\/(\d+) trains/,
    (_m, pair, i, n, from, to, k, total) =>
      `训练 ${pair}（第 ${i}/${n} 个币对）· 数据 ${from} → ${to} · 第 ${k}/${total} 轮`,
  ],
  [
    /^-+ Starting training (\S+) -+$/,
    (_m, p) => `── 开始训练 ${p} ──`,
  ],
  [
    /^-+ Done training (\S+) \(([\d.]+) secs\) -+$/,
    (_m, p, s) => `── ${p} 训练完成（${s} 秒）──`,
  ],
  [/^-+ Training on data from (.+?) to (.+?) -+$/, (_m, a, b) => `训练数据区间：${a} → ${b}`],
  [/^Training model on (\d+) features/, (_m, n) => `特征数量：${n}`],
  [/^Training model on (\d+) data points/, (_m, n) => `训练样本：${n} 个`],
  [
    /^(\S+): dropped (\d+) training points due to NaNs/,
    (_m, p, n) => `${p}：因缺失值丢弃 ${n} 条训练样本`,
  ],
  [/^Saving metadata to disk\./, () => `保存模型元数据`],
  [/^Could not find model at (.+)$/, (_m, p) => `未找到已有模型，将重新训练：${p}`],
  [/^Could not find existing datadrawer/, () => `无历史数据记录，从头开始`],
  [/^Could not find existing historic_predictions/, () => `无历史预测记录，从头开始`],
  [
    /^Set fresh train queue from whitelist\. Queue:\s*(.+)$/,
    (_m, q) => `训练队列：${q}`,
  ],
  [/^DI tossed (\d+) predictions for being too far from training data\./, (_m, n) => `相异度过滤：剔除 ${n} 条分布外预测`],

  // ── 回测 ──
  [/^Using fee ([\d.]+)% - worst case fee from exchange/, (_m, f) => `手续费按最坏情况计：${f}%`],
  [
    /^Loading data from (.+?) up to (.+?) \((\d+) days\)\./,
    (_m, a, b, d) => `加载数据 ${a} → ${b}（${d} 天）`,
  ],
  [/^Dataload complete\. Calculating indicators/, () => `数据加载完成，计算指标中`],
  [/^Running backtesting for Strategy (\w+)/, (_m, s) => `开始回测策略 ${s}`],
  [/^Backtest result caching disabled/, () => `回测结果缓存已禁用`],
  [/^Backtesting ([\d.]+)/, (_m, v) => `回测版本 ${v}`],
  [/^Parameter --timerange detected:\s*(.+)$/, (_m, t) => `回测区间：${t}`],
  [/^Filter trades by timerange:\s*(.+)$/, (_m, t) => `按区间过滤成交：${t}`],

  // ── 补充：启动/运行期常见消息 ──
  [/^Wallets synced\.$/, () => `钱包已同步`],
  [/^Enabling rpc\.api_server$/, () => `启用 API 服务`],
  [/^Enabling colorized output\.$/, () => `启用彩色日志输出`],
  [/^Verbosity set to (\d+)/, (_m, v) => `日志详细级别设为 ${v}`],
  [/^Starting HTTP Server at (.+)$/, (_m, a) => `启动 HTTP 服务：${a}`],
  [/^Starting Local Rest Server\.$/, () => `启动本地 REST 服务`],
  [/^Started server process \[(\d+)\]/, (_m, n) => `服务进程已启动 [${n}]`],
  [/^Waiting for application startup\.$/, () => `等待应用启动…`],
  [/^Application startup complete\.$/, () => `应用启动完成`],
  [/^Uvicorn running on (.+?)\s*\(Press CTRL\+C to quit\)/, (_m, u) => `Uvicorn 已监听 ${u}`],
  [/^Parameter --(\S+) detected:\s*(.*)$/, (_m, k, v) => `检测到参数 --${k}${v ? '：' + v : ''}`],
  [/^Parameter --(\S+) detected\.\.\.$/, (_m, k) => `检测到参数 --${k}`],
  [/^Validating configuration \.\.\.$/, () => `校验配置…`],
  [/^Found no parameter file\.$/, () => `未找到超参文件（使用默认值）`],
  [/^Using pairlist from configuration\.$/, () => `使用配置中的币对列表`],
  [/^No params for (\w+) found, using default values\.$/, (_m, s) => `${s} 无自定义参数，用默认值`],
  [/^\s*$/, () => ''],

  // ── 异常（保留原文，只翻译标题）──
  [
    /^Exception in ASGI application/,
    () => `ASGI 应用异常（多为客户端提前断开，通常无害）`,
  ],
  [/^Traceback \(most recent call last\):/, () => `调用栈（最近一次调用在最后）：`],
  [/^During handling of the above exception, another exception occurred:/, () => `处理上述异常时又抛出新的异常：`],
  [/^The above exception was the direct cause of the following exception:/, () => `上述异常直接导致了以下异常：`],
];

/** 把一行 Freqtrade 日志翻成中文；匹配不到则原样返回 */
export function translateLog(msg: string): string {
  if (!msg) return '';
  for (const [re, rep] of RULES) {
    const m = msg.match(re);
    if (m) {
      return typeof rep === 'string' ? rep : rep(...m);
    }
  }
  return msg;
}

/** 日志级别中文 */
export function levelCn(lv: string): string {
  return LEVEL_CN[lv] ?? lv;
}
