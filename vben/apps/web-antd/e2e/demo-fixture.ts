import type { Page, Route } from '@playwright/test';
import { existsSync, statSync } from 'node:fs';
import { resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

export const SYNTHETIC_ACCOUNT = '合成测试 · strategy-a';
export const SYNTHETIC_SESSION = 'synthetic-session-a';
const token = 'synthetic-browser-token-no-real-account';
const demoPath = '/api/locals/binance-demo';
const distRoot = fileURLToPath(new URL('../dist/', import.meta.url));
type PendingRead = { arrived: () => void; released: Promise<void> };
type ReadBody = unknown | ((url: URL) => unknown);

/** 全部账户数据是合成事实；任意遗漏的 API 或外部请求均阻断，绝不代理真实账户。 */
export async function installDemoFixture(page: Page) {
  if (!existsSync(resolve(distRoot, 'index.html')))
    throw new Error('请先构建 web-antd；测试需要真实 dist/index.html');
  const origin = new URL(
    process.env.QUANT_E2E_BASE_URL || 'http://quant-e2e.invalid',
  ).origin;
  let pendingAccount: PendingRead | undefined;
  const pendingReads = new Map<string, PendingRead>();
  const state = {
    fail: false,
    unknown: false,
    session: SYNTHETIC_SESSION,
    account: SYNTHETIC_ACCOUNT,
    equity: '998.12345678901234567890',
    statusCalls: 0,
    mutations: [] as string[],
    unexpected: [] as string[],
    requestedSessions: [] as string[],
    readRequests: [] as string[],
    authRequests: [] as string[],
    pageErrors: [] as string[],
    reads: new Map<string, ReadBody>(),
    readFailures: new Set<string>(),
    holdNextRead(path: string) {
      let arrived!: () => void;
      let release!: () => void;
      const waiting = new Promise<void>((resolve) => {
        arrived = resolve;
      });
      const released = new Promise<void>((resolve) => {
        release = resolve;
      });
      pendingReads.set(path, { arrived, released });
      return { waiting, release };
    },
    holdNextAccount() {
      let arrived!: () => void;
      let release!: () => void;
      const waiting = new Promise<void>((resolve) => {
        arrived = resolve;
      });
      const released = new Promise<void>((resolve) => {
        release = resolve;
      });
      pendingAccount = { arrived, released };
      return { waiting, release };
    },
  };
  page.on('pageerror', (error) => state.pageErrors.push(error.message));
  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, json: body });
  await page.routeWebSocket('**/*', (socket) => {
    state.unexpected.push(`WebSocket ${new URL(socket.url()).pathname}`);
    socket.close();
  });
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (url.origin !== origin) {
      // Iconify 在生产壳层以 fetch 请求图标 JSON；它们仍被阻断，但不属于业务 API。
      const iconAsset =
        request.method() === 'GET' &&
        [
          'https://api.iconify.design',
          'https://api.unisvg.com',
          'https://api.simplesvg.com',
        ].includes(url.origin) &&
        /^\/[a-z0-9-]+\.json$/.test(path);
      if (
        !iconAsset &&
        (path.startsWith('/api/') ||
          path.startsWith('/auth/') ||
          request.resourceType() === 'xhr' ||
          request.resourceType() === 'fetch')
      )
        state.unexpected.push(
          `foreign-origin ${request.method()} ${url.origin}${path}`,
        );
      return route.abort('blockedbyclient');
    }
    if (path.startsWith('/auth/'))
      state.authRequests.push(`${request.method()} ${path}`);
    if (path === '/auth/auto-login' || path === '/auth/login') {
      if (request.method() !== 'POST') {
        state.unexpected.push(`${request.method()} ${path}`);
        return json(route, { detail: '合成登录方法错误' }, 405);
      }
      return json(route, {
        access_token: token,
        username: 'synthetic-test-user',
      });
    }
    if (path === '/auth/me' && request.method() === 'GET') {
      if (request.headers().authorization !== `Bearer ${token}`) {
        state.unexpected.push('missing-synthetic-auth /auth/me');
        return json(route, { detail: '合成会话缺失' }, 401);
      }
      return json(route, { username: 'synthetic-test-user' });
    }
    if (request.method() !== 'GET') {
      state.mutations.push(`${request.method()} ${path}`);
      return json(route, { detail: '只读浏览器测试拒绝执行命令' }, 405);
    }
    if (path.startsWith('/api/') || path.startsWith('/auth/')) {
      state.readRequests.push(`${path}${url.search}`);
      if (state.reads.has(path)) {
        if (request.headers().authorization !== `Bearer ${token}`) {
          state.unexpected.push(`missing-synthetic-auth ${path}`);
          return json(route, { detail: '合成会话缺失' }, 401);
        }
        if (state.readFailures.has(path))
          return json(route, { detail: '合成读取失败' }, 503);
        const value = state.reads.get(path);
        const body = typeof value === 'function' ? value(url) : value;
        const pending = pendingReads.get(path);
        pendingReads.delete(path);
        if (pending) {
          pending.arrived();
          await pending.released;
        }
        return json(route, body);
      }
      if (path === '/api/v1/show_config')
        return json(route, {
          state: 'stopped',
          dry_run: true,
          strategy: 'CI 合成夹具',
          trading_mode: 'futures',
          exchange: 'binance',
          max_open_trades: 2,
        });
      if (path === '/api/v1/count') return json(route, { current: 0, max: 2 });
      if (path === '/api/ws-token')
        return json(route, { available: false, ws_url: '' });
      if (path !== demoPath && !path.startsWith(`${demoPath}/`)) {
        state.unexpected.push(path);
        return json(route, { detail: '未定义的合成 API' }, 501);
      }
      if (request.headers().authorization !== `Bearer ${token}`) {
        state.unexpected.push('missing-synthetic-auth');
        return json(route, { detail: '合成会话缺失' }, 401);
      }
      if (state.fail) return json(route, { detail: '合成读取失败' }, 503);
      const now = Date.now();
      const common = {
        environment: 'binance_demo',
        endpoint: 'demo-fapi.binance.com',
      };
      if (path === demoPath) {
        state.statusCalls += 1;
        return json(route, {
          ...common,
          credentials: { configured: false },
          runner: {
            state: 'paused',
            selectedAccountId: state.account,
            virtualAccountKey: state.session,
            tradingEnabled: false,
            last_cycle_ms: now,
            risk_policy: {
              entries_mode: 'blocked_evidence_incomplete',
              evidence_complete: false,
              daily_pnl_usdt: state.unknown ? null : '-2.34567890',
              stage_drawdown_usdt: state.unknown ? null : '2.34567890',
              stage_drawdown_limit_usdt: '150',
            },
            actual_symbol_config: state.unknown
              ? []
              : [{ leverage: 10, margin_type: 'ISOLATED' }],
          },
          accounts: [],
          metrics: {},
          limitations: ['合成测试数据，不构成真实账户或验收证据'],
        });
      }
      if (path.endsWith('/account')) {
        const body = {
          ...common,
          observedAt: now,
          account: { totalMarginBalance: state.unknown ? null : state.equity },
          items: [],
          limit: 100,
        };
        const pending = pendingAccount;
        pendingAccount = undefined;
        if (pending) {
          pending.arrived();
          await pending.released;
        }
        return json(route, body);
      }
      if (path.endsWith('/positions'))
        return json(route, {
          ...common,
          items: [
            {
              symbol: 'BTCUSDT',
              positionAmt: '0.002',
              entryPrice: '60000.10',
              markPrice: '59999.9',
              unRealizedProfit: '-0.0004',
            },
          ],
          limit: 100,
        });
      if (
        path.endsWith('/orders') ||
        path.endsWith('/fills') ||
        path.endsWith('/evidence')
      ) {
        state.requestedSessions.push(url.searchParams.get('session') || '');
      }
      if (path.endsWith('/orders'))
        return json(route, {
          ...common,
          items: [
            {
              symbol: 'BTCUSDT',
              clientOrderId: 'synthetic-order-a',
              side: 'BUY',
              type: 'LIMIT',
              status: 'unknown',
              origQty: '0.002',
              executedQty: '0',
              price: '60000.10',
            },
          ],
          limit: 100,
        });
      if (path.endsWith('/fills'))
        return json(route, {
          ...common,
          items: [
            {
              symbol: 'BTCUSDT',
              id: 'synthetic-fill-a',
              side: 'BUY',
              price: '60000.10',
              qty: '0.002',
              commission: null,
              commissionAsset: null,
              time: now,
            },
          ],
          limit: 100,
        });
      if (path.endsWith('/evidence'))
        return json(route, {
          ...common,
          snapshotAt: new Date(now).toISOString(),
          scope: 'selected_candle_strategy_execution_only',
          questions: {
            fees: {
              state: 'actual_fees_partially_known',
              fillCount: 1,
              knownFeeFillCount: 0,
              unknownFeeFillCount: 1,
              rates: [],
              chargedByAsset: [],
            },
            secondsProfit: {
              state: 'seconds_experiment_not_configured',
              secondsExperimentConfigured: false,
              closedNetTradingPnlUsdt: null,
              closedRoundTripCount: 0,
              knownNetRoundTripCount: 0,
              unknownNetRoundTripCount: 0,
              knownNetSubtotalUsdt: '0',
              fundingIncluded: false,
              liveProfitabilityVerified: false,
            },
            execution: {
              state: 'no_exchange_orders',
              exchangeAcceptedEntryCount: 0,
              terminalAcceptedEntryCount: 0,
              zeroFillTerminalAcceptedEntryCount: 0,
              acceptedTerminalNonfillRatio: null,
              exchangeRejectedEntryCount: 0,
              localTerminalUnconfirmedEntryCount: 0,
              pendingEntryCount: 0,
              unknownEntryCount: 1,
              partialTerminalAcceptedEntryCount: 0,
              signalPriceDeviation: {
                fillCount: 1,
                knownReferenceFillCount: 0,
                meanAdverseDeviationBps: null,
                scope:
                  'demo_fill_vs_live_paper_signal_includes_environment_and_latency',
              },
              liveLaunchAllowed: false,
              launchDecision: 'manual_review_required',
            },
          },
        });
      state.unexpected.push(path);
      return json(route, { detail: '未定义的合成 Demo API' }, 501);
    }
    // 直接读取真实生产构建文件；既有本机服务器可能仍提供旧版本，绝不转发遗漏接口。
    if (
      request.resourceType() === 'xhr' ||
      request.resourceType() === 'fetch'
    ) {
      state.unexpected.push(`${request.method()} ${path}`);
      return json(route, { detail: '未定义的同源读取' }, 501);
    }
    let pathname: string;
    try {
      pathname = decodeURIComponent(path);
    } catch {
      return route.abort('blockedbyclient');
    }
    const file = resolve(
      distRoot,
      `.${pathname === '/' ? '/index.html' : pathname}`,
    );
    if (
      !file.startsWith(distRoot.replace(/[/\\]$/, '') + sep) ||
      !existsSync(file) ||
      !statSync(file).isFile()
    ) {
      state.unexpected.push(`missing-dist ${path}`);
      return json(route, { detail: '构建文件不存在' }, 404);
    }
    return route.fulfill({ path: file });
  });
  return state;
}
