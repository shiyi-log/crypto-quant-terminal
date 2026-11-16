import { reactive } from 'vue';

export type Trade = Record<string, any> & {
  trade_id: number | string;
  pair: string;
};
export type CommandKind = 'market' | 'limit' | 'partial' | 'cancel' | 'reload';
export interface PositionsTransport {
  config: () => Promise<unknown>;
  balance: () => Promise<unknown>;
  positions: () => Promise<unknown>;
  history: () => Promise<unknown>;
  detail: (id: string) => Promise<unknown>;
  custom: (id: string) => Promise<unknown>;
  exit: (payload: {
    tradeid: string;
    ordertype: 'limit' | 'market';
    price?: number;
    amount?: number;
  }) => Promise<unknown>;
  cancel: (id: string) => Promise<unknown>;
  reload: (id: string) => Promise<unknown>;
}
export function numberValue(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (
    typeof value === 'string' &&
    !/^[+-]?(?:[0-9]+(?:[.][0-9]*)?|[.][0-9]+)(?:e[+-]?[0-9]+)?$/i.test(
      value.trim(),
    )
  )
    return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
export function formatNumber(value: unknown, digits = 2): string {
  const number = numberValue(value);
  return number === null
    ? '—'
    : number.toLocaleString('en-US', {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      });
}
export function signedNumber(value: unknown, digits = 2): string {
  const number = numberValue(value);
  return number === null
    ? '—'
    : (number > 0 ? '+' : '') + formatNumber(number, digits);
}
export function profitPercent(trade: Record<string, any>): number | null {
  const ratio = numberValue(trade.profit_ratio ?? trade.close_profit);
  return ratio === null ? numberValue(trade.profit_pct) : ratio * 100;
}
function object(value: unknown): value is Record<string, any> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}
function idValue(value: unknown): string | null {
  return (typeof value === 'string' || typeof value === 'number') &&
    /^[1-9][0-9]*$/.test(String(value))
    ? String(value)
    : null;
}
function tradeValue(value: unknown): value is Trade {
  return (
    object(value) &&
    idValue(value.trade_id) !== null &&
    typeof value.pair === 'string' &&
    value.pair.length > 0 &&
    value.pair.length < 100 &&
    (value.is_open === undefined || typeof value.is_open === 'boolean') &&
    (value.is_short === undefined || typeof value.is_short === 'boolean')
  );
}
function tradesValue(value: unknown): value is Trade[] {
  return (
    Array.isArray(value) &&
    value.every(tradeValue) &&
    new Set(value.map((row) => String(row.trade_id))).size === value.length
  );
}
const labels = {
  config: '引擎环境',
  balance: '账户余额',
  positions: '当前持仓',
  history: '最近已平仓记录',
};
export function createPositionsState(api: PositionsTransport) {
  const data = reactive({
    config: null as Record<string, any> | null,
    balance: null as Record<string, any> | null,
    positions: null as Trade[] | null,
    history: null as Trade[] | null,
    total: null as number | null,
    environment: 'unknown' as 'dry' | 'live' | 'unknown',
    errors: {} as Record<string, string>,
    readAt: {} as Record<string, string>,
    loading: false,
    connected: false,
    selectedId: null as string | null,
    detailOpen: false,
    view: 'open' as 'open' | 'closed',
    pair: '',
    query: '',
    detail: null as Trade | null,
    detailError: '',
    detailLoading: false,
    custom: '',
    customError: '',
    customLoading: false,
    command: null as {
      kind: CommandKind;
      trade: Trade;
      environment: 'dry' | 'live';
    } | null,
    commandValue: '',
    commandError: '',
    busy: false,
    receipt: '',
    confirmationOpen: false,
    commandFormOpen: false,
  });
  let alive = true;
  let refreshId = 0;
  let selectionId = 0;
  let customId = 0;
  async function refresh() {
    if (!alive || data.loading) return;
    const generation = ++refreshId;
    data.loading = true;
    const errors: Record<string, string> = {};
    const read = async (
      key: keyof typeof labels,
      request: () => Promise<unknown>,
      apply: (value: unknown) => boolean,
    ) => {
      try {
        const value = await request();
        if (!alive || generation !== refreshId) return;
        if (!apply(value)) throw new Error('invalid response shape');
        data.readAt[key] = new Date().toISOString();
      } catch {
        errors[key] =
          labels[key] + '读取失败或响应格式无效，保留上次成功快照。';
      }
    };
    await Promise.all([
      read('config', api.config, (value) => {
        if (!object(value) || typeof value.dry_run !== 'boolean') {
          data.environment = 'unknown';
          return false;
        }
        data.config = value;
        data.environment = value.dry_run ? 'dry' : 'live';
        return true;
      }),
      read('balance', api.balance, (value) => {
        if (
          !object(value) ||
          !Array.isArray(value.currencies) ||
          !value.currencies.every(
            (row: unknown) => object(row) && typeof row.currency === 'string',
          )
        )
          throw new Error('invalid balance');
        data.balance = value;
        return true;
      }),
      read('positions', api.positions, (value) => {
        if (!tradesValue(value) || value.some((row) => row.is_open === false))
          throw new Error('invalid positions');
        data.positions = value.map((row) => ({ ...row, is_open: true }));
        return true;
      }),
      read('history', api.history, (value) => {
        if (
          !object(value) ||
          !tradesValue(value.trades) ||
          value.trades.some((row: Trade) => row.is_open === true) ||
          !Number.isSafeInteger(value.total_trades) ||
          value.total_trades < value.trades.length
        )
          throw new Error('invalid history');
        data.history = value.trades.map((row: Trade) => ({
          ...row,
          is_open: false,
        }));
        data.total = value.total_trades;
        return true;
      }),
    ]);
    if (alive && generation === refreshId) {
      data.errors = errors;
      data.loading = false;
      if (data.selectedId && !data.detail && selectedSnapshot())
        void select(data.selectedId);
    }
  }
  function selectedSnapshot(): Trade | null {
    return (
      [...(data.positions ?? []), ...(data.history ?? [])].find(
        (row) => String(row.trade_id) === data.selectedId,
      ) ?? null
    );
  }
  async function select(target: unknown) {
    if (!alive) return;
    if (data.command) cancelCommand();
    const id = idValue(target);
    const current = ++selectionId;
    customId++;
    const same = id !== null && data.selectedId === id;
    data.selectedId = id;
    data.custom = '';
    data.customError = '';
    data.customLoading = false;
    data.detailError = '';
    if (!same) {
      data.detail = null;
      data.receipt = '';
    }
    if (!id) {
      data.detailLoading = false;
      if (target !== null && target !== undefined)
        data.detailError = '交易编号无效，请使用单个原始交易 ID。';
      return;
    }
    data.detailOpen = false;
    data.detailLoading = true;
    try {
      const snapshot = selectedSnapshot();
      if (!snapshot) throw new Error('Trade is not in the selected ledger');
      const value = await api.detail(id);
      if (!alive || current !== selectionId) return;
      if (
        !tradeValue(value) ||
        String(value.trade_id) !== id ||
        snapshot.pair !== value.pair ||
        (value.orders !== undefined &&
          (!Array.isArray(value.orders) || !value.orders.every(object)))
      )
        throw new Error('invalid detail');
      data.detail = value;
    } catch {
      if (alive && current === selectionId)
        data.detailError =
          '所选交易详情未能读取，保留该交易的已有快照；不会改选其他交易。';
    } finally {
      if (alive && current === selectionId) data.detailLoading = false;
    }
  }
  async function loadCustom() {
    const id = data.selectedId;
    if (!alive || !id) return;
    const scope = selectionId;
    const current = ++customId;
    data.customLoading = true;
    data.customError = '';
    try {
      const value = await api.custom(id);
      if (!alive || scope !== selectionId || current !== customId) return;
      const raw =
        object(value) && 'custom_data' in value ? value.custom_data : value;
      data.custom =
        raw == null ||
        (object(raw) && Object.keys(raw).length === 0) ||
        (Array.isArray(raw) && raw.length === 0)
          ? '暂无自定义数据（策略未写入）'
          : typeof raw === 'string'
            ? raw
            : JSON.stringify(raw, null, 2);
    } catch {
      if (alive && scope === selectionId && current === customId)
        data.customError =
          '自定义数据未能读取。可能尚未写入，请重试或核对策略。';
    } finally {
      if (alive && scope === selectionId && current === customId)
        data.customLoading = false;
    }
  }
  function requestCommand(kind: CommandKind, target: unknown): boolean {
    const id = idValue(target);
    const row = data.positions?.find((item) => String(item.trade_id) === id);
    if (
      !alive ||
      data.busy ||
      data.command ||
      !['market', 'limit', 'partial', 'cancel', 'reload'].includes(kind) ||
      !row ||
      data.environment === 'unknown' ||
      data.errors.config ||
      data.errors.positions ||
      data.loading
    )
      return false;
    data.command = { kind, trade: { ...row }, environment: data.environment };
    data.commandError = '';
    data.commandValue =
      kind === 'partial'
        ? String(numberValue(row.amount) ?? '')
        : kind === 'limit'
          ? String(
              numberValue(row.current_rate) ?? numberValue(row.open_rate) ?? '',
            )
          : '';
    return true;
  }
  function cancelCommand() {
    if (!data.busy) {
      data.command = null;
      data.commandError = '';
      data.commandValue = '';
      data.confirmationOpen = false;
      data.commandFormOpen = false;
    }
  }
  async function confirmCommand() {
    const command = data.command;
    if (!alive || !command || data.busy) return;
    const id = String(command.trade.trade_id);
    if (
      data.loading ||
      data.errors.config ||
      data.errors.positions ||
      data.environment !== command.environment ||
      !data.positions?.some(
        (row) => String(row.trade_id) === id && row.pair === command.trade.pair,
      )
    ) {
      data.commandError = '环境或持仓快照已变化，请取消后刷新确认。';
      return;
    }
    const value = numberValue(data.commandValue);
    if (
      (command.kind === 'limit' || command.kind === 'partial') &&
      (value === null || value <= 0)
    ) {
      data.commandError = '请输入大于 0 的有效数值。';
      return;
    }
    if (
      command.kind === 'partial' &&
      (numberValue(command.trade.amount) === null ||
        value! >= numberValue(command.trade.amount)!)
    ) {
      data.commandError = '部分退出数量必须小于已知持仓数量。';
      return;
    }
    data.busy = true;
    data.commandError = '';
    data.receipt = '';
    try {
      let response: unknown;
      if (command.kind === 'cancel') response = await api.cancel(id);
      else if (command.kind === 'reload') response = await api.reload(id);
      else
        response = await api.exit({
          tradeid: id,
          ordertype: command.kind === 'limit' ? 'limit' : 'market',
          ...(command.kind === 'limit'
            ? { price: value! }
            : command.kind === 'partial'
              ? { amount: value! }
              : {}),
        });
      if (!alive) return;
      const accepted =
        object(response) &&
        !response.error &&
        !response.detail &&
        response.status !== 'error' &&
        response.status !== 'failed' &&
        (command.kind === 'reload'
          ? (tradeValue(response) && String(response.trade_id) === id) ||
            response.status === 'success'
          : response.status === 'success' ||
            response.status === 'Created exit order' ||
            (typeof response.result === 'string' &&
              response.result.length > 0));
      data.receipt = accepted
        ? '交易 ' +
          id +
          ' 收到指令回执；实际委托、撤单或成交状态请刷新交易详情核对。'
        : '交易 ' +
          id +
          ' 指令结果未知；请核对交易详情后决定下一步，系统不会自动重发。';
    } catch {
      if (alive)
        data.receipt =
          '交易 ' +
          id +
          ' 指令结果未知；请核对交易详情后决定下一步，系统不会自动重发。';
    } finally {
      if (alive) {
        data.busy = false;
        data.command = null;
        data.commandValue = '';
        data.confirmationOpen = false;
        data.commandFormOpen = false;
        await refresh();
      }
    }
  }
  function openConfirmation() {
    if (data.command && !data.busy) data.confirmationOpen = true;
  }
  function closeConfirmation() {
    if (!data.busy) data.confirmationOpen = false;
  }
  function dispose() {
    alive = false;
    refreshId++;
    selectionId++;
    customId++;
  }
  function closeDetail() {
    data.detailOpen = false;
  }
  function openDetail() {
    if (data.detail) data.detailOpen = true;
  }
  return {
    data,
    refresh,
    select,
    loadCustom,
    selectedSnapshot,
    requestCommand,
    cancelCommand,
    confirmCommand,
    openConfirmation,
    closeConfirmation,
    openDetail,
    closeDetail,
    dispose,
  };
}
export type PositionsState = ReturnType<typeof createPositionsState>;
