/**
 * 强平事件 —— 走本地认证服务（auth_service.py 8890）。
 *
 * 后端返回裸 JSON（无 {code,data} 信封），所以用 ftClient + unwrap。
 * 数据来源是 strategy_simulation.py 在强平发生时写入的
 * `liquidation_model_details` 事件；没有强平就是空列表，不得用示例数据填充。
 */
import { ftClient, unwrap } from '#/api/freqtrade/client';
import { requireSession } from '#/api/session';

export interface LiquidationEvent {
  pair: null | string;
  /** 触发提示价（多头取当根最低价，空头取最高价） */
  trigger_price?: null | number;
  /** 策略账户 */
  strategy_id?: null | string;
  /** 持仓方向 */
  side?: null | string;
  /** 该档名义价值对应的维持保证金率 */
  maintenance_margin_rate?: null | number;
  /** 理论强平价 */
  liquidation_price_theory?: null | number;
  /** 标记价是否触发 */
  triggered_by_mark?: boolean;
  /** 成交价是否触发 */
  triggered_by_trade?: boolean;
  /** 含不利滑点的成交价 */
  fill_price_with_slippage?: null | number;
  /** 该仓估计损失（USDT） */
  realized_loss_estimate?: null | number;
  /** 事件时间（毫秒时间戳） */
  candle_ms?: number;
  timestamp?: string;
}

export interface LiquidationEventsResponse {
  events: LiquidationEvent[];
  count: number;
}

/** 拉取强平事件列表（按时间倒序）。 */
export async function getLiquidationEvents(params: {
  strategy_id?: string;
  limit?: number;
} = {}): Promise<LiquidationEventsResponse> {
  requireSession('请先登录后再查看强平事件');
  const query: Record<string, string | number> = {};
  if (params.strategy_id) query.strategy_id = params.strategy_id;
  if (params.limit) query.limit = params.limit;

  const body = unwrap<LiquidationEventsResponse>(
    await ftClient.get('/locals/liquidation-events', { params: query }),
  );
  return {
    events: Array.isArray(body?.events) ? body.events : [],
    count: typeof body?.count === 'number' ? body.count : 0,
  };
}
