import {
  cancelOpenOrder,
  getBalance,
  getConfig,
  getOpenTrades,
  getTrade,
  getTradeCustomData,
  getTrades,
  postForceExit,
  reloadTrade,
} from '#/api/freqtrade';
import { createPositionsState } from './positionsState';

/** Bind UI state to the real legacy Freqtrade API without translating its facts. */
export function createLegacyPositionsState() {
  return createPositionsState({
    config: getConfig,
    balance: getBalance,
    positions: getOpenTrades,
    history: () => getTrades(30),
    detail: getTrade,
    custom: getTradeCustomData,
    exit: postForceExit,
    cancel: cancelOpenOrder,
    reload: reloadTrade,
  });
}
