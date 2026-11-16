import type {
  FtBackgroundJob,
  FtBacktestHistoryEntry,
  FtBacktestRequest,
  FtBacktestStatus,
  FtDownloadDataRequest,
  FtLookaheadRequest,
  FtRecursiveRequest,
} from '#/api/freqtrade';

import { reactive } from 'vue';

export type JobKind = 'download' | 'lookahead' | 'recursive';
export type CommandKind = 'backtest' | JobKind;
export const UNKNOWN_RETRY_ACK = '接受重复执行风险';
export interface UnknownCommandAcknowledgement {
  reviewed: boolean;
  phrase: string;
}
type UnknownCall = (...args: any[]) => Promise<any>;
export interface TaskApi {
  getBacktestStatus: UnknownCall;
  getBacktestHistory: UnknownCall;
  getWebStrategies: UnknownCall;
  getWebAvailablePairs: UnknownCall;
  getWhitelist: UnknownCall;
  postBacktest: UnknownCall;
  abortBacktest: UnknownCall;
  resetBacktest: UnknownCall;
  postDownloadData: UnknownCall;
  postLookaheadAnalysis: UnknownCall;
  postRecursiveAnalysis: UnknownCall;
  getBackgroundJob: UnknownCall;
  getLookaheadResult: UnknownCall;
  getRecursiveResult: UnknownCall;
  setBacktestNotes: UnknownCall;
}
function object(value: unknown): value is Record<string, any> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}
function text(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}
export function parseStatus(value: unknown): FtBacktestStatus {
  if (
    !object(value) ||
    typeof value.running !== 'boolean' ||
    !text(value.status) ||
    typeof value.progress !== 'number' ||
    !Number.isFinite(value.progress)
  )
    throw new Error('回测状态合同不完整');
  return value as FtBacktestStatus;
}
export function parseHistory(value: unknown): FtBacktestHistoryEntry[] {
  if (
    !Array.isArray(value) ||
    value.some(
      (row) =>
        !object(row) ||
        !text(row.filename) ||
        !text(row.strategy) ||
        !text(row.run_id) ||
        typeof row.backtest_start_time !== 'number' ||
        !Number.isFinite(row.backtest_start_time),
    )
  )
    throw new Error('历史产物合同不完整');
  return value;
}
export function parseJob(value: unknown, expected: string): FtBackgroundJob {
  if (
    !object(value) ||
    value.job_id !== expected ||
    typeof value.running !== 'boolean' ||
    !text(value.status)
  )
    throw new Error('任务身份或状态合同不完整');
  return value as FtBackgroundJob;
}
export function parseAnalysisResult(
  value: unknown,
  kind: 'lookahead' | 'recursive',
): any | null {
  if (
    !object(value) ||
    typeof value.status !== 'string' ||
    typeof value.running !== 'boolean'
  )
    return null;
  if (value.running || value.status !== 'ended') return null;
  const result = value.result;
  if (!object(result)) return null;
  if (
    kind === 'lookahead' &&
    !('has_bias' in result || 'total_signals' in result)
  )
    return null;
  if (kind === 'recursive' && Object.keys(result).length === 0) return null;
  return result;
}
function strings(value: unknown, key: string): string[] {
  if (
    !object(value) ||
    !Array.isArray(value[key]) ||
    value[key].some((item: unknown) => !text(item))
  )
    throw new Error('清单合同不完整');
  return [...new Set<string>(value[key])];
}
const terminal = (job: FtBackgroundJob) =>
  job.running === false && ['error', 'failed', 'success'].includes(job.status);
export const metricText = (v: unknown, ratio = false) =>
  typeof v === 'number' && Number.isFinite(v)
    ? ratio
      ? (v * 100).toFixed(2) + '%'
      : String(v)
    : '未知';

export function createTaskQueue(api: TaskApi) {
  const storageKey = 'quant-research-task-snapshots-v1';
  const initial = () => {
    const fallback: {
      jobs: Record<JobKind, FtBackgroundJob | null>;
      results: Partial<Record<JobKind, { jobId: string; data: any }>>;
    } = {
      jobs: { download: null, lookahead: null, recursive: null },
      results: {},
    };
    try {
      const value = JSON.parse(sessionStorage.getItem(storageKey) ?? '{}');
      if (!object(value)) return fallback;
      for (const kind of ['download', 'lookahead', 'recursive'] as JobKind[]) {
        if (value.jobs?.[kind] != null) {
          const job = value.jobs[kind];
          if (
            object(job) &&
            text(job.job_id) &&
            typeof job.running === 'boolean' &&
            text(job.status)
          )
            fallback.jobs[kind] = job as unknown as FtBackgroundJob;
        }
        const result = value.results?.[kind];
        if (
          object(result) &&
          text(result.jobId) &&
          parseAnalysisResult(
            { status: 'ended', running: false, result: result.data },
            kind as 'lookahead' | 'recursive',
          )
        )
          fallback.results[kind as 'lookahead' | 'recursive'] = result as {
            jobId: string;
            data: any;
          };
      }
    } catch {
      /* Malformed snapshots are unknown, never commands to replay. */
    }
    return fallback;
  };
  const restored = initial();
  const loadUnknown = () => {
    try {
      const value = JSON.parse(
        sessionStorage.getItem('quant-research-command-unknown-v1') ?? '{}',
      );
      return Object.fromEntries(
        (['backtest', 'download', 'lookahead', 'recursive'] as const).map(
          (key) => [key, value[key] === true],
        ),
      );
    } catch {
      return {
        backtest: true,
        download: true,
        lookahead: true,
        recursive: true,
      };
    }
  };
  const persistUnknown = () => {
    try {
      sessionStorage.setItem(
        'quant-research-command-unknown-v1',
        JSON.stringify(state.commandsUnknown),
      );
    } catch {
      /* UI state remains fail-closed for this session. */
    }
  };
  const state = reactive({
    backtest: null as FtBacktestStatus | null,
    history: [] as FtBacktestHistoryEntry[],
    historyKnown: false,
    strategies: [] as string[],
    pairs: [] as string[],
    whitelist: [] as string[],
    errors: {} as Record<string, string>,
    loading: false,
    busy: {} as Record<string, boolean>,
    commandsUnknown: loadUnknown() as Record<CommandKind, boolean>,
    outcome: '',
    observed: {} as Record<string, string>,
    jobs: restored.jobs as Record<JobKind, FtBackgroundJob | null>,
    results: restored.results as Partial<
      Record<JobKind, { jobId: string; data: any }>
    >,
  });
  const revisions: Record<string, number> = {};
  let disposed = false;
  let polling = false;
  let stopRequested = false;
  const next = (key: string) => (revisions[key] = (revisions[key] ?? 0) + 1);
  const current = (key: string, revision: number) =>
    !disposed && revisions[key] === revision;
  const mark = (key: string) => {
    state.errors[key] = '';
    state.observed[key] = new Date().toLocaleString('zh-CN', { hour12: false });
  };
  const persistSnapshots = () => {
    try {
      sessionStorage.setItem(
        storageKey,
        JSON.stringify({ jobs: state.jobs, results: state.results }),
      );
    } catch {
      /* Live state is still usable until this page closes. */
    }
  };
  async function read(
    key: string,
    call: () => Promise<any>,
    parse: (v: unknown) => any,
    assign: (v: any) => void,
  ) {
    const rev = next(key);
    try {
      const value = parse(await call());
      if (current(key, rev)) {
        assign(value);
        mark(key);
      }
    } catch {
      if (current(key, rev))
        state.errors[key] =
          '读取失败或合同不完整；保留上次成功快照，尚无快照的来源保持未知。';
    }
  }
  async function refresh() {
    if (disposed) return;
    const rev = next('refresh');
    state.loading = true;
    await Promise.all([
      ...(state.busy.backtest
        ? []
        : [
            read('backtest', api.getBacktestStatus, parseStatus, (value) => {
              state.backtest = value;
              if (!value.running && value.status !== 'stopping')
                stopRequested = false;
            }),
          ]),
      read('history', api.getBacktestHistory, parseHistory, (value) => {
        state.history = value;
        state.historyKnown = true;
      }),
      read(
        'strategies',
        api.getWebStrategies,
        (v) => strings(v, 'strategies'),
        (v) => (state.strategies = v),
      ),
      read(
        'pairs',
        api.getWebAvailablePairs,
        (v) => strings(v, 'pairs'),
        (v) => (state.pairs = v),
      ),
      read(
        'whitelist',
        api.getWhitelist,
        (v) => strings(v, 'whitelist'),
        (v) => (state.whitelist = v),
      ),
    ]);
    if (current('refresh', rev)) {
      for (const kind of ['download', 'lookahead', 'recursive'] as JobKind[]) {
        if (state.jobs[kind] && !state.busy[kind]) state.errors[kind] = '';
      }
      state.loading = false;
    }
  }
  async function backtestCommand(
    kind: 'abort' | 'reset' | 'start',
    payload?: FtBacktestRequest,
  ) {
    if (disposed || state.busy.backtest || state.commandsUnknown.backtest)
      return;
    state.busy.backtest = true;
    state.outcome = '';
    const rev = next('backtest');
    try {
      const response = await (kind === 'start'
        ? api.postBacktest(payload)
        : kind === 'abort'
          ? api.abortBacktest()
          : api.resetBacktest());
      const value = parseStatus(response);
      if (!current('backtest', rev)) return;
      state.backtest = value;
      mark('backtest');
      state.commandsUnknown.backtest = false;
      persistUnknown();
      if (kind === 'abort') {
        stopRequested = true;
        state.outcome = '已请求中止，等待后续状态确认。';
      } else if (kind === 'reset')
        state.outcome =
          value.status === 'reset' && !value.running
            ? '研究实例内存结果已清空；磁盘历史产物保留。'
            : '研究实例未清空结果，任务可能仍在运行。';
      else state.outcome = '回测提交已响应；执行结果与研究结论需后续核对。';
    } catch {
      if (current('backtest', rev)) {
        state.commandsUnknown.backtest = true;
        persistUnknown();
        state.errors.backtest = '命令结果未知；请先刷新事实，勿重复提交。';
        state.outcome = '';
      }
    } finally {
      if (!disposed) state.busy.backtest = false;
    }
  }
  async function startJob(
    kind: JobKind,
    payload: FtDownloadDataRequest | FtLookaheadRequest | FtRecursiveRequest,
  ) {
    if (disposed || state.busy[kind] || state.commandsUnknown[kind]) return;
    state.busy[kind] = true;
    state.outcome = '';
    const rev = next(kind);
    try {
      const call =
        kind === 'download'
          ? api.postDownloadData
          : kind === 'lookahead'
            ? api.postLookaheadAnalysis
            : api.postRecursiveAnalysis;
      const response = await call(payload);
      if (!object(response) || !text(response.job_id) || !text(response.status))
        throw new Error('bad acknowledgment');
      if (current(kind, rev)) {
        state.jobs[kind] = {
          job_id: response.job_id,
          running: false,
          status: 'pending',
        };
        persistSnapshots();
        mark(kind);
        state.outcome = '任务提交已响应；等待精确任务状态确认。';
      }
    } catch {
      if (current(kind, rev)) {
        state.commandsUnknown[kind] = true;
        persistUnknown();
        state.errors[kind] =
          '命令结果未知；保留之前结果及其来源，请先核对任务。';
        state.outcome = '';
      }
    } finally {
      if (!disposed) state.busy[kind] = false;
    }
  }
  async function poll() {
    if (disposed || polling) return;
    polling = true;
    try {
      const work: Promise<void>[] = [];
      if (
        !state.busy.backtest &&
        (state.backtest?.running ||
          stopRequested ||
          state.backtest?.status === 'stopping')
      )
        work.push(
          read('backtest', api.getBacktestStatus, parseStatus, (value) => {
            state.backtest = value;
            if (!value.running && value.status !== 'stopping')
              stopRequested = false;
          }),
        );
      for (const kind of ['download', 'lookahead', 'recursive'] as JobKind[]) {
        const job = state.jobs[kind];
        if (
          !job ||
          state.busy[kind] ||
          state.errors[kind] ||
          (terminal(job) &&
            (['error', 'failed'].includes(job.status) ||
              kind === 'download' ||
              state.results[kind]?.jobId === job.job_id))
        )
          continue;
        const id = job.job_id;
        const rev = revisions[kind];
        work.push(
          (async () => {
            try {
              const value = parseJob(await api.getBackgroundJob(id), id);
              if (!current(kind, rev!) || state.jobs[kind]?.job_id !== id)
                return;
              state.jobs[kind] = value;
              persistSnapshots();
              mark(kind);
              if (!terminal(value)) return;
              if (value.status !== 'success' || value.error) {
                state.errors[kind] =
                  '任务执行失败；保留先前成功分析，失败原因请核对原始任务。';
                return;
              }
              if (kind !== 'download') {
                const response = await (kind === 'lookahead'
                  ? api.getLookaheadResult(id)
                  : api.getRecursiveResult(id));
                const result = parseAnalysisResult(response, kind);
                if (!result) throw new Error('invalid result');
                if (current(kind, rev!) && state.jobs[kind]?.job_id === id) {
                  state.results[kind] = { jobId: id, data: result };
                  persistSnapshots();
                }
              }
            } catch {
              if (current(kind, rev!))
                state.errors[kind] =
                  '任务身份或结果无法确认；保留最后事实，手动刷新后核对。';
            }
          })(),
        );
      }
      await Promise.all(work);
    } finally {
      polling = false;
    }
  }
  async function saveNotes(row: FtBacktestHistoryEntry, notes: string) {
    if (disposed || state.busy.notes) return;
    state.busy.notes = true;
    state.outcome = '';
    const target = { filename: row.filename, strategy: row.strategy };
    const rev = next('history');
    try {
      const value = parseHistory(
        await api.setBacktestNotes(target.filename, target.strategy, notes),
      );
      if (current('history', rev)) {
        state.history = value;
        state.historyKnown = true;
        mark('history');
        state.outcome = '备注已保存';
      }
    } catch {
      if (current('history', rev))
        state.errors.history =
          '备注命令结果未知；保留上次历史，请重新读取后核对。';
    } finally {
      if (!disposed) state.busy.notes = false;
    }
  }
  async function confirmUnknown(
    kind: CommandKind,
    acknowledgement: UnknownCommandAcknowledgement,
  ) {
    if (
      !acknowledgement.reviewed ||
      acknowledgement.phrase !== UNKNOWN_RETRY_ACK ||
      !state.commandsUnknown[kind] ||
      state.busy[kind] ||
      disposed
    )
      return;
    state.commandsUnknown[kind] = false;
    persistUnknown();
    state.errors[kind] = '操作者确认原请求尚无对应后台任务；可以重新提交一次。';
  }
  function needsPoll() {
    return (
      !disposed &&
      (!!state.backtest?.running ||
        stopRequested ||
        state.backtest?.status === 'stopping' ||
        Object.entries(state.jobs).some(
          ([kind, job]) =>
            job &&
            !state.errors[kind] &&
            (!terminal(job) ||
              (job.status === 'success' &&
                kind !== 'download' &&
                state.results[kind as JobKind]?.jobId !== job.job_id)),
        ))
    );
  }
  return {
    state,
    refresh,
    poll,
    needsPoll,
    startBacktest: (p: FtBacktestRequest) => backtestCommand('start', { ...p }),
    abort: () => backtestCommand('abort'),
    reset: () => backtestCommand('reset'),
    startJob,
    saveNotes,
    confirmUnknown,
    dispose: () => {
      disposed = true;
    },
  };
}
