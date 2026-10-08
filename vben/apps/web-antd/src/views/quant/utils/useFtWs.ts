/**
 * Freqtrade WebSocket 实时推送（共享单连接）
 *
 * 为什么需要：页面原本全靠 HTTP 轮询，持仓最快也要 6 秒才刷新一次，
 * 急跌时这个延迟是实打实的风险。Freqtrade 的 WS 会在成交/撤单/白名单变化时
 * 主动推送，收到后立刻刷新即可。
 *
 * 设计要点：
 *  - **全局单连接**：模块级 socket + 引用计数，多个页面同时用也只有一个连接；
 *    所有页面在最后一个使用者卸载时才断开。
 *  - **失败静默回退**：拿不到 ws 地址（认证服务未开放 / 未登录）或连接失败时，
 *    只是不推送，各页原有的轮询逻辑照旧，功能不受影响。
 *  - **自动重连**：非主动关闭时 5 秒后重连，避免服务重启后再也收不到推送。
 */
import { onMounted, onUnmounted, ref } from 'vue';

import { getWsToken } from '#/api/freqtrade';

export interface FtWsMessage {
  data: any;
  type: string;
}

type FtWsHandler = (msg: FtWsMessage) => void;

/** 订阅的消息类型（后端 RPCMessageType 的取值） */
const SUBSCRIBE_TYPES = [
  'whitelist',
  'entry_fill',
  'entry_cancel',
  'exit_fill',
  'exit_cancel',
  'new_candle',
  'protection_trigger',
  'protection_trigger_global',
];

const handlers = new Set<FtWsHandler>();

/** 连接状态，供 UI 显示「实时」指示灯 */
const connected = ref(false);

let socket: WebSocket | null = null;
let refCount = 0;
let reconnectTimer: any = null;
let closedByUs = false;

async function open() {
  if (socket) {
    return;
  }
  closedByUs = false;
  let info: Awaited<ReturnType<typeof getWsToken>> | null = null;
  try {
    info = await getWsToken();
  } catch {
    info = null;
  }
  // 等待期间组件可能已全部卸载，或已建立其它连接
  if (closedByUs || refCount <= 0 || socket) {
    return;
  }
  if (!info?.available || !info.ws_url) {
    // 取不到地址（未登录 / 认证服务未开放）：30 秒后再试一次，
    // 这样服务恢复后无需刷新页面也能自动转为实时推送。
    scheduleReconnect(30_000);
    return;
  }
  const ws = new WebSocket(info.ws_url);
  socket = ws;

  ws.onopen = () => {
    connected.value = true;
    ws.send(JSON.stringify({ data: [...SUBSCRIBE_TYPES], type: 'subscribe' }));
  };

  ws.onmessage = (ev: MessageEvent) => {
    let msg: FtWsMessage | null = null;
    try {
      msg = JSON.parse(ev.data as string);
    } catch {
      return;
    }
    if (!msg?.type) {
      return;
    }
    // 单个订阅者抛错不能影响其它订阅者
    handlers.forEach((fn) => {
      try {
        fn(msg as FtWsMessage);
      } catch {
        /* 忽略订阅者内部错误 */
      }
    });
  };

  ws.onclose = () => {
    connected.value = false;
    socket = null;
    if (!closedByUs && refCount > 0) {
      scheduleReconnect();
    }
  };

  ws.onerror = () => {
    connected.value = false;
  };
}

function scheduleReconnect(delay = 5000) {
  if (reconnectTimer) {
    return;
  }
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void open();
  }, delay);
}

function close() {
  closedByUs = true;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (socket) {
    socket.close();
    socket = null;
  }
  connected.value = false;
}

/**
 * 订阅推送。返回取消订阅函数 —— 组件里务必在 onUnmounted 调用，
 * 否则组件卸载后 handler 仍留在集合里，会造成内存泄漏与无谓刷新。
 */
export function onFtWsMessage(fn: FtWsHandler): () => void {
  handlers.add(fn);
  return () => {
    handlers.delete(fn);
  };
}

/** 在组件中使用：建立/复用连接，组件全部卸载后自动断开 */
export function useFtWs() {
  onMounted(() => {
    refCount += 1;
    if (refCount === 1) {
      void open();
    }
  });
  onUnmounted(() => {
    refCount -= 1;
    if (refCount <= 0) {
      refCount = 0;
      close();
    }
  });
  return { connected };
}
