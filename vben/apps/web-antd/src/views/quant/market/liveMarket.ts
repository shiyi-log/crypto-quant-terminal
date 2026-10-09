import { FT_TOKEN_KEY, ftClient, unwrap } from '#/api/freqtrade/client';

export interface MarketSubscription {
  pair: string;
  timeframe: string;
}

export interface LiveCandle {
  close: number;
  closed: boolean;
  high: number;
  low: number;
  open: number;
  timestamp: number;
  volume: number;
}

export interface LiveKline extends MarketSubscription {
  candle: LiveCandle;
  exchange_event_timestamp: number;
  received_timestamp: number;
  sent_timestamp: number;
  type: 'kline';
}

type ConnectionState = 'connected' | 'connecting' | 'reconnecting';
export interface LiveMarketState {
  source: ConnectionState;
  transport: ConnectionState;
}

export interface LiveMarketSubscriber {
  onKline: (message: LiveKline) => void;
  onReconnect: () => void;
  onState: (state: LiveMarketState) => void;
  subscription: MarketSubscription;
}

// All charts share one connection; subscriptions are always a complete snapshot.
const subscribers = new Set<LiveMarketSubscriber>();
const sourceStates = new Map<string, ConnectionState>();
let socket: null | WebSocket = null;
let connecting = false;
let ready = false;
let previouslyReady = false;
let transport: ConnectionState = 'connecting';
let epoch = 0;
let retry = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
let handshakeTimer: ReturnType<typeof setTimeout> | undefined;
let subscriptionTimer: ReturnType<typeof setTimeout> | undefined;

const marketOf = (pair: string) => (pair.includes(':') ? 'futures' : 'spot');
const validState = (value: unknown): value is ConnectionState =>
  value === 'connected' || value === 'connecting' || value === 'reconnecting';

function notifyState() {
  for (const subscriber of subscribers) {
    subscriber.onState({
      source: sourceStates.get(marketOf(subscriber.subscription.pair)) ?? 'connecting',
      transport,
    });
  }
}

function sendSubscriptions() {
  if (!ready || socket?.readyState !== WebSocket.OPEN) return;
  const unique = new Map<string, MarketSubscription>();
  for (const { subscription } of subscribers) {
    unique.set(`${subscription.pair}|${subscription.timeframe}`, subscription);
  }
  socket.send(JSON.stringify({ type: 'subscribe', subscriptions: [...unique.values()] }));
}

function scheduleSubscriptions() {
  clearTimeout(subscriptionTimer);
  subscriptionTimer = setTimeout(sendSubscriptions, 0);
}

function scheduleReconnect() {
  if (!subscribers.size || reconnectTimer) return;
  transport = 'reconnecting';
  notifyState();
  const delay = Math.min(15_000, 1000 * 2 ** Math.min(retry++, 4));
  reconnectTimer = setTimeout(() => {
    reconnectTimer = undefined;
    void connect();
  }, delay);
}

function isKline(message: any): message is LiveKline {
  const candle = message?.candle;
  return (
    message?.type === 'kline' &&
    typeof message.pair === 'string' &&
    typeof message.timeframe === 'string' &&
    candle &&
    ['timestamp', 'open', 'high', 'low', 'close', 'volume'].every(
      (key) => typeof candle[key] === 'number' && Number.isFinite(candle[key]),
    ) &&
    candle.timestamp > 0 &&
    typeof candle.closed === 'boolean' &&
    ['exchange_event_timestamp', 'received_timestamp', 'sent_timestamp'].every(
      (key) => typeof message[key] === 'number' && Number.isFinite(message[key]) && message[key] > 0,
    )
  );
}

async function connect() {
  if (!subscribers.size || socket || connecting) return;
  const connectionEpoch = epoch;
  connecting = true;
  transport = previouslyReady ? 'reconnecting' : 'connecting';
  sourceStates.clear();
  notifyState();
  try {
    const info = unwrap<{ url: string }>(await ftClient.get('/locals/market-stream-info'));
    if (connectionEpoch !== epoch || !subscribers.size) return;
    const url = new URL(info.url, window.location.href);
    if (!['ws:', 'wss:'].includes(url.protocol)) throw new Error('Invalid market stream URL');
    const ws = new WebSocket(url.href);
    socket = ws;
    handshakeTimer = setTimeout(() => ws.close(), 10_000);
    ws.onopen = () => {
      if (socket !== ws) return;
      // Authentication goes in the first message, never in the URL.
      let token = '';
      try {
        token = localStorage.getItem(FT_TOKEN_KEY) ?? '';
      } catch {
        ws.close();
        return;
      }
      ws.send(JSON.stringify({ type: 'auth', token }));
    };
    ws.onmessage = (event) => {
      if (socket !== ws) return;
      let message: any;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message.type === 'ready') {
        const reconnected = previouslyReady;
        ready = previouslyReady = true;
        retry = 0;
        clearTimeout(handshakeTimer);
        transport = 'connected';
        sendSubscriptions();
        notifyState();
        if (reconnected) {
          for (const subscriber of subscribers) subscriber.onReconnect();
        }
      } else if (message.type === 'status' && validState(message.state)) {
        const oldState = sourceStates.get(message.market);
        sourceStates.set(message.market, message.state);
        notifyState();
        if (message.state === 'connected' && oldState === 'reconnecting') {
          for (const subscriber of subscribers) {
            if (marketOf(subscriber.subscription.pair) === message.market) subscriber.onReconnect();
          }
        }
      } else if (isKline(message)) {
        for (const subscriber of subscribers) {
          const { subscription } = subscriber;
          if (subscription.pair === message.pair && subscription.timeframe === message.timeframe) {
            subscriber.onKline(message);
          }
        }
      }
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null;
      ready = false;
      sourceStates.clear();
      clearTimeout(handshakeTimer);
      scheduleReconnect();
    };
    ws.onerror = () => {
      if (socket === ws) ws.close();
    };
  } catch {
    if (connectionEpoch === epoch) scheduleReconnect();
  } finally {
    if (connectionEpoch === epoch) connecting = false;
  }
}

export function subscribeLiveMarket(subscriber: LiveMarketSubscriber): () => void {
  subscribers.add(subscriber);
  notifyState();
  scheduleSubscriptions();
  void connect();
  return () => {
    subscribers.delete(subscriber);
    if (subscribers.size) {
      scheduleSubscriptions();
      return;
    }
    // Invalidate an in-flight discovery request when the last chart leaves.
    epoch += 1;
    connecting = ready = previouslyReady = false;
    retry = 0;
    clearTimeout(reconnectTimer);
    clearTimeout(handshakeTimer);
    clearTimeout(subscriptionTimer);
    reconnectTimer = handshakeTimer = subscriptionTimer = undefined;
    const oldSocket = socket;
    socket = null;
    oldSocket?.close();
    sourceStates.clear();
    transport = 'connecting';
  };
}
