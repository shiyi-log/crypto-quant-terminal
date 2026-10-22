/**
 * 研究账本事件 —— 走本地认证服务（auth_service.py 8890）。
 *
 * 与策略实验室一致：本地服务返回裸 JSON，没有 {code,data} 信封，
 * 因此用 ftClient + unwrap 显式取响应体，而不是 requestClient。
 */
import { ftClient, unwrap } from '#/api/freqtrade/client';
import { requireSession } from '#/api/session';

export interface ResearchLedgerEvent {
  event_id: string;
  timestamp: string;
  event_type: string;
  research_id: null | string;
  gate_type?: null | string;
  payload: Record<string, unknown>;
}

export interface ResearchLedgerEventsResponse {
  events: ResearchLedgerEvent[];
  total: number;
}

export interface ResearchLedgerQuery {
  research_id?: string;
  event_type?: string;
  limit?: number;
}

/** 拉取研究账本事件时间线（按时间倒序）。 */
export async function getResearchLedgerEvents(
  query: ResearchLedgerQuery = {},
): Promise<ResearchLedgerEventsResponse> {
  requireSession('请先登录后再查看研究账本');
  const params: Record<string, string | number> = {};
  if (query.research_id) params.research_id = query.research_id;
  if (query.event_type) params.event_type = query.event_type;
  if (query.limit) params.limit = query.limit;

  const body = unwrap<ResearchLedgerEventsResponse>(
    await ftClient.get('/locals/research-ledger-events', { params }),
  );
  return {
    events: Array.isArray(body?.events) ? body.events : [],
    total: typeof body?.total === 'number' ? body.total : 0,
  };
}
