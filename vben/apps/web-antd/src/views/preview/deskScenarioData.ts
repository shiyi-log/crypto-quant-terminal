/**
 * 交易台的全部场景摘要共用这一份本地固定数据，避免风险栏与页面细节互相矛盾。
 * 这些值仅供前端演示，不代表账户事实或授权状态。
 */
export type DeskScenario =
  | 'normal'
  | 'flat'
  | 'unknown'
  | 'stale'
  | 'read-failure'
  | 'partial-fill'
  | 'cooldown'
  | 'hard-failure';

export const DEMO_DESK_SCENARIOS = {
  normal: {
    label: '样本正常 · 固定快照',
    reason: 'demo_snapshot_observed',
    account: '仅演示',
    pnl: '-2.35 USDT',
    budget: '15.65 USDT',
    riskUsed: '0.42% / 2.00%',
    drawdown: '2.35 / 150 USDT',
    entry: '演示状态：允许状态样本',
    position: '0.002 BTC 固定样本',
    protection: '待确认样本',
  },
  flat: {
    label: '空仓 · 无活动委托',
    reason: 'account_flat_observed',
    account: '空仓样本',
    pnl: '未评估',
    budget: '未知',
    riskUsed: '未知',
    drawdown: '未评估',
    entry: '空仓样本：无持仓委托',
    position: '当前没有持仓或活动委托',
    protection: '无持仓保护单',
  },
  unknown: {
    label: '账户快照未知',
    reason: 'account_snapshot_unknown',
    account: '状态未知',
    pnl: '未知',
    budget: '未知',
    riskUsed: '未知',
    drawdown: '未知',
    entry: '状态未知：禁止新增风险',
    position: 'BTCUSDT · 上次样本保留',
    protection: '保护状态未知',
  },
  stale: {
    label: '快照已过期 · 保留最后样本',
    reason: 'account_snapshot_stale',
    account: '快照过期',
    pnl: '未知',
    budget: '未知',
    riskUsed: '未知',
    drawdown: '未知',
    entry: '快照过期：禁止新增风险',
    position: 'BTCUSDT · 上次样本保留',
    protection: '保护状态未知',
  },
  'read-failure': {
    label: '读取失败：保留上次样本',
    reason: 'account_snapshot_read_failed',
    account: '读取失败',
    pnl: '未知',
    budget: '未知',
    riskUsed: '未知',
    drawdown: '未知',
    entry: '读取失败：禁止新增风险',
    position: 'BTCUSDT · 上次样本保留',
    protection: '保护状态未知',
  },
  'partial-fill': {
    label: '部分成交：保护确认中',
    reason: 'partial_fill_protection_pending',
    account: '部分成交',
    pnl: '未知',
    budget: '未知',
    riskUsed: '未知',
    drawdown: '未知',
    entry: '保护确认前禁止新增风险',
    position: '0.001 BTC 已成交 · 剩余 0.001 BTC 未成交',
    protection: '确认中 · 尚未确认有效保护',
  },
  cooldown: {
    label: '冷却中 · 43 分钟',
    reason: 'daily_net_loss_cooldown',
    account: '冷却中 · 43 分钟',
    pnl: '-20.00 USDT',
    budget: '0.00 USDT',
    riskUsed: '未知',
    drawdown: '20.00 / 150 USDT',
    entry: '禁止新增风险 · 剩余 43 分钟',
    position: 'BTCUSDT · 持仓样本保留',
    protection: '保护状态未知',
  },
  'hard-failure': {
    label: '本轮硬失败 · 已终止',
    reason: 'daily_net_loss_hard_limit_exceeded',
    account: '本轮硬失败',
    pnl: '-50.01 USDT',
    budget: '0.00 USDT',
    riskUsed: '未知',
    drawdown: '50.01 / 150 USDT',
    entry: '本轮已终止 · 禁止新增风险',
    position: 'BTCUSDT · 仅展示最后样本',
    protection: '需人工核验保护事实',
  },
} as const satisfies Record<DeskScenario, {
  label: string;
  reason: string;
  account: string;
  pnl: string;
  budget: string;
  riskUsed: string;
  drawdown: string;
  entry: string;
  position: string;
  protection: string;
}>;
