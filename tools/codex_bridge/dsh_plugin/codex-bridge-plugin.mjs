/**
 * DSH ⇄ ChatGPT(Codex Desktop) 桥接 —— DSH 入站推送插件
 * ============================================================================
 *
 * 作用：让 ChatGPT(Codex Desktop) 能**主动**把消息推进 DSH，并在 DSH 里变成
 * 一个新的根会话（新会话 + 新 prompt）。
 *
 * 数据流：
 *
 *   ChatGPT ──POST /codex──▶ 本插件 HTTP(127.0.0.1:8898)
 *                              │ 校验 → 入内存队列
 *                              ▼
 *                        ctx.webhookRuntime.dispatch(...)
 *                              │ 按 kind 匹配规则
 *                              ▼
 *                        rule.run(delivery, signal) 取出队列消息
 *                              │ 返回 { workspacePath, title, prompt,
 *                              │        agentPreset, permissionPreset }
 *                              ▼
 *                        DSH 运行时创建新的根会话并投递 prompt
 *
 * 硬性约束（已实测，改动前请先读 tools/codex_bridge/PROTOCOL.md）：
 *
 * 1. 本文件以**绝对路径**被插进 profile 的 cordis.patch.yml，位于 DSH 安装树
 *    之外，因此**只能 import Node 内置模块**。任何 `@deepseek-ai/*` 导入都会
 *    ERR_MODULE_NOT_FOUND，也不能写 TypeScript。
 *
 * 2. 关于 `inject`：**不要**写 `export const inject = { optional: [...] }`。
 *    DSH 随包的 cordis@4.0.4 里 `Inject.resolve()` 只支持两种形态：
 *      - 数组：`['svc']` → 每个元素是**必需**服务；
 *      - 对象：`{ svcName: interceptConfig }` → **键就是服务名**，也全部是**必需**服务。
 *    实测（cordis/lib/index.js）：`Inject.resolve({optional:['webhookRuntime']})`
 *    返回 `{"optional":["webhookRuntime"]}`，即它要求一个**名叫 `optional` 的服务**。
 *    该服务不存在 ⇒ 本插件的 fiber 永远停在 INACTIVE，`apply()` 根本不会被调用。
 *    所以这里声明 `inject = []`（无必需依赖），webhookRuntime 改为用
 *    `ctx.get()` 软查询 + `ctx.inject([...], cb)` 延迟注册，做到真正的"可选注入"
 *    和优雅降级：即使 webhookRuntime 不存在，HTTP 入口照样起来（POST 返回 503）。
 *
 * 3. profile `desktop` 由 Electron 独占，`dsh --profile desktop` 会被拒绝，
 *    因此本插件无法用 CLI 启动验证；只能重启 DSH 应用后生效。
 *    配置合成可用一次性 profile 验证：
 *      dsh --profile <tmp> --patch <patch.yml> --dump-config
 */

import { createServer } from 'node:http'
import { randomUUID } from 'node:crypto'
import { isAbsolute } from 'node:path'

/** Cordis 函数式插件的显示名。 */
export const name = 'codex-bridge'

/**
 * 无必需服务依赖 —— 见文件头注释第 2 条。
 * webhookRuntime / agentPresets / permissionPresets 全部按"可选"处理。
 */
export const inject = []

/** 只允许绑定回环地址，不接受配置覆盖（避免误暴露到 0.0.0.0）。 */
const BIND_HOST = '127.0.0.1'

/** 默认配置。任何一项都能被 cordis.patch.yml 里的 `config:` 覆盖。 */
const DEFAULTS = Object.freeze({
  /** HTTP 监听端口（仅 127.0.0.1）。 */
  port: 8898,
  /** 新会话的工作目录，必须是绝对路径。 */
  workspacePath: '/Users/shiyi/DeepSeek/量化',
  /** 规则 kind，必须与 dispatch 的 delivery.kind 完全一致。 */
  kind: 'codex-bridge',
  /** delivery.source，会记进 DSH 会话的消息来源。 */
  source: 'codex-desktop',
  /** 请求体字节上限（256 KiB）。 */
  maxBodyBytes: 256 * 1024,
  /** 内存队列长度上限，超出返回 429。 */
  queueLimit: 200,
  /** 队列条目存活上限（毫秒），超时由清理定时器丢弃。 */
  queueTtlMs: 10 * 60 * 1000,
  /** 新会话标题前缀。 */
  titlePrefix: 'ChatGPT',
  /** 用来解析"默认 agent preset"的候选，解析失败时按顺序回退。 */
  agentPreset: null,
  agentPresetFallback: 'standard',
  /** 权限预设，必须真实存在；解析失败时按顺序回退。 */
  permissionPreset: 'danger-full-access',
  permissionPresetFallback: 'read-only',
  /** prompt 末尾的"回传"提示。 */
  replyHint: '请处理这条消息，并把结论回传到桥接（见 tools/codex_bridge/README.md）。',
})

// --------------------------------------------------------------------------
// 小工具：配置消毒（不 import Schemastery，只能手写，且绝不抛异常）
// --------------------------------------------------------------------------

/** 取一个非空字符串，否则回退。 */
function pickString(value, fallback) {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : fallback
}

/** 取一个整数并夹到 [min, max]，非法则回退。 */
function pickInt(value, fallback, min, max) {
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isSafeInteger(n)) return fallback
  if (n < min || n > max) return fallback
  return n
}

/** 取一个绝对路径字符串，否则回退。 */
function pickAbsolutePath(value, fallback) {
  const s = pickString(value, null)
  if (s === null) return fallback
  if (!isAbsolute(s)) return fallback
  return s
}

/**
 * 把入口 `config` 消毒成一份完整配置。
 * 任何非法值都静默回退到默认值，绝不抛异常（否则会让整个插件装载失败）。
 * @returns {{config: object, warnings: string[]}}
 */
function normalizeConfig(raw) {
  const warnings = []
  const input = raw !== null && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
  if (raw !== undefined && raw !== null && (typeof raw !== 'object' || Array.isArray(raw))) {
    warnings.push('config 不是对象，已全部使用默认值')
  }

  const workspacePath = pickAbsolutePath(input.workspacePath, DEFAULTS.workspacePath)
  if (input.workspacePath !== undefined && workspacePath !== input.workspacePath) {
    warnings.push(`workspacePath 不是合法的绝对路径，已回退为 ${workspacePath}`)
  }

  const config = {
    port: pickInt(input.port, DEFAULTS.port, 1, 65535),
    workspacePath,
    kind: pickString(input.kind, DEFAULTS.kind),
    source: pickString(input.source, DEFAULTS.source),
    maxBodyBytes: pickInt(input.maxBodyBytes, DEFAULTS.maxBodyBytes, 1, 16 * 1024 * 1024),
    queueLimit: pickInt(input.queueLimit, DEFAULTS.queueLimit, 1, 100000),
    queueTtlMs: pickInt(input.queueTtlMs, DEFAULTS.queueTtlMs, 1000, 24 * 60 * 60 * 1000),
    titlePrefix: pickString(input.titlePrefix, DEFAULTS.titlePrefix),
    agentPreset: pickString(input.agentPreset, DEFAULTS.agentPreset),
    agentPresetFallback: pickString(input.agentPresetFallback, DEFAULTS.agentPresetFallback),
    permissionPreset: pickString(input.permissionPreset, DEFAULTS.permissionPreset),
    permissionPresetFallback: pickString(input.permissionPresetFallback, DEFAULTS.permissionPresetFallback),
    replyHint: pickString(input.replyHint, DEFAULTS.replyHint),
  }

  if (input.port !== undefined && config.port !== input.port) {
    warnings.push(`port 非法，已回退为 ${config.port}`)
  }
  return { config, warnings }
}

// --------------------------------------------------------------------------
// 小工具：日志与"可选服务"软查询
// --------------------------------------------------------------------------

/**
 * 取一个可选服务，取不到返回 undefined，永不抛异常。
 * `ctx.get(name)` 是 cordis 提供的"不声明 inject 也能读服务"的官方通道
 * （DSH 自己的 dsh-base patch 里也用 `!!js "!ctx.get('profileContext')"`）。
 * 默认 strict=true，即只返回当前处于 active 状态的实现。
 */
function getService(ctx, serviceName) {
  try {
    if (ctx !== null && typeof ctx.get === 'function') {
      const impl = ctx.get(serviceName)
      if (impl) return impl
    }
  } catch {
    /* 服务不存在/未激活：按缺失处理 */
  }
  // 兜底：少数场景下 ctx 上直接挂了服务属性（正常情况下会因未声明 inject 而抛错）。
  try {
    const direct = ctx?.[serviceName]
    if (direct) return direct
  } catch {
    /* 同上，忽略 */
  }
  return undefined
}

/**
 * 造一个只做"记录"的 logger，供 ctx.logger 不可用时兜底。
 * 不写文件、不 import 任何东西，避免产生额外句柄。
 */
function fallbackLogger() {
  const noop = () => {}
  return { info: noop, warn: noop, error: noop, debug: noop, trace: noop, success: noop }
}

/** 构造带前缀的日志器，永不抛异常。 */
function makeLogger(ctx) {
  let real
  try {
    real = ctx?.logger
  } catch {
    real = undefined
  }
  const sink = real && typeof real === 'object' ? real : fallbackLogger()
  const emit = (level, message) => {
    try {
      const fn = sink[level]
      if (typeof fn === 'function') fn.call(sink, `codex-bridge: ${message}`)
    } catch {
      /* 日志失败绝不影响主流程 */
    }
  }
  return {
    info: (m) => emit('info', m),
    warn: (m) => emit('warn', m),
    error: (m) => emit('error', m),
    debug: (m) => emit('debug', m),
  }
}

// --------------------------------------------------------------------------
// 预设解析：agentPreset / permissionPreset 必须是**真实存在**的 id
// --------------------------------------------------------------------------

/**
 * 解析权限预设名。真实预设有 read-only / workspace-write / danger-full-access。
 * `ctx.permissionPresets.resolve(name)` 是**同步**的，未知名字会抛。
 * @returns {string} 一个当前实例确实认识的名字
 */
function resolvePermissionPreset(ctx, config, logger) {
  const service = getService(ctx, 'permissionPresets')
  const candidates = [
    config.permissionPreset,
    config.permissionPresetFallback,
    'danger-full-access',
    'workspace-write',
    'read-only',
  ]

  if (service && typeof service.resolve === 'function') {
    for (const candidate of candidates) {
      if (!candidate) continue
      try {
        service.resolve(candidate)
        if (candidate !== config.permissionPreset) {
          logger.warn(`permissionPreset "${config.permissionPreset}" 不可用，改用 "${candidate}"`)
        }
        return candidate
      } catch {
        /* 换下一个候选 */
      }
    }
    // 全都不认识：退到服务自己声明的默认预设（如果它暴露了）。
    try {
      const fallback = service.defaultPreset
      if (typeof fallback === 'string' && fallback !== '') {
        logger.warn(`permissionPreset 候选全部不可用，改用服务默认 "${fallback}"`)
        return fallback
      }
    } catch {
      /* 忽略 */
    }
  } else {
    logger.warn('ctx.permissionPresets 不可用，permissionPreset 不做校验')
  }
  return config.permissionPreset
}

/**
 * 解析 agent preset id。`ctx.agentPresets.resolve(id)` 是**异步**的且来自远端注册表，
 * 未知名会抛。传 undefined 表示"取当前默认预设"。
 * 已知真实 id：standard / ptc / minimal / cordis（见 dsh-agent-preset-registry）。
 * @returns {Promise<string>} 一个当前实例确实认识的 id
 */
async function resolveAgentPreset(ctx, config, logger) {
  const service = getService(ctx, 'agentPresets')
  if (service && typeof service.resolve === 'function') {
    const attempts = [config.agentPreset, undefined, config.agentPresetFallback, 'standard']
    for (const attempt of attempts) {
      try {
        const resolved = await service.resolve(attempt)
        const id = resolved && typeof resolved.id === 'string' && resolved.id !== '' ? resolved.id : null
        if (id) {
          if (attempt !== config.agentPreset) {
            logger.warn(
              `agentPreset "${config.agentPreset ?? '(默认)'}" 解析失败，改用 "${id}"`,
            )
          }
          return id
        }
      } catch {
        /* 换下一个候选 */
      }
    }
    logger.warn('agentPresets.resolve() 全部候选失败，使用静态回退值')
  } else {
    logger.warn('ctx.agentPresets 不可用，agentPreset 使用静态回退值')
  }
  return config.agentPreset ?? config.agentPresetFallback
}

// --------------------------------------------------------------------------
// prompt 构造
// --------------------------------------------------------------------------

/** 标题必须非空且别太夸张。 */
function deriveTitle(item, config) {
  const firstLine = String(item.text)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line !== '')
  const clipped = firstLine === undefined
    ? `消息 ${item.id.slice(0, 8)}`
    : firstLine.length > 60
      ? `${firstLine.slice(0, 60)}…`
      : firstLine
  return `${config.titlePrefix}：${clipped}`
}

/** 模型实际看到的正文：明确标注来源。 */
function buildPrompt(item, config) {
  const lines = ['【来自 ChatGPT(Codex Desktop) 的消息】', '']
  if (item.threadId) lines.push(`（Codex 线程：${item.threadId}）`, '')
  lines.push(item.text, '', config.replyHint)
  return lines.join('\n')
}

// --------------------------------------------------------------------------
// HTTP 层小工具
// --------------------------------------------------------------------------

/** 发送一个 JSON 响应，只发一次。 */
function sendJson(response, status, payload, extraHeaders) {
  if (response.headersSent || response.writableEnded) return
  const body = Buffer.from(JSON.stringify(payload), 'utf8')
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': String(body.byteLength),
    'cache-control': 'no-store',
    ...(extraHeaders ?? {}),
  })
  response.end(body)
}

/**
 * 以**有界**方式读取请求体。
 * @returns {Promise<{ok: true, text: string} | {ok: false, status: number, error: string}>}
 */
function readBoundedBody(request, maxBodyBytes) {
  return new Promise((resolvePromise) => {
    const declared = request.headers['content-length']
    if (declared !== undefined) {
      if (!/^(0|[1-9]\d*)$/.test(declared)) {
        request.resume()
        resolvePromise({ ok: false, status: 400, error: 'Content-Length 非法' })
        return
      }
      if (Number(declared) > maxBodyBytes) {
        request.resume()
        resolvePromise({ ok: false, status: 413, error: '请求体过大' })
        return
      }
    }

    const chunks = []
    let size = 0
    let settled = false
    const finish = (result) => {
      if (settled) return
      settled = true
      request.removeListener('data', onData)
      request.removeListener('end', onEnd)
      request.removeListener('error', onError)
      request.removeListener('aborted', onAborted)
      resolvePromise(result)
    }
    function onData(chunk) {
      size += chunk.byteLength
      if (size > maxBodyBytes) {
        request.resume()
        finish({ ok: false, status: 413, error: '请求体过大' })
        return
      }
      chunks.push(chunk)
    }
    function onEnd() {
      try {
        const text = Buffer.concat(chunks, size).toString('utf8')
        finish({ ok: true, text })
      } catch {
        finish({ ok: false, status: 400, error: '请求体不是合法 UTF-8' })
      }
    }
    function onError() {
      finish({ ok: false, status: 400, error: '读取请求体失败' })
    }
    function onAborted() {
      finish({ ok: false, status: 400, error: '请求被中断' })
    }

    request.on('data', onData)
    request.on('end', onEnd)
    request.on('error', onError)
    request.on('aborted', onAborted)
  })
}

/**
 * 校验 POST /codex 的 JSON 负载。
 * @returns {{ok: true, value: {text: string, title?: string, threadId?: string}} | {ok: false, error: string}}
 */
function validatePayload(parsed) {
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, error: '请求体必须是 JSON 对象' }
  }
  const text = parsed.text
  if (typeof text !== 'string' || text.trim() === '') {
    return { ok: false, error: '字段 "text" 必须是非空字符串' }
  }
  const value = { text }
  if (parsed.title !== undefined && parsed.title !== null) {
    if (typeof parsed.title !== 'string') return { ok: false, error: '字段 "title" 必须是字符串' }
    if (parsed.title.trim() !== '') value.title = parsed.title.trim()
  }
  if (parsed.thread_id !== undefined && parsed.thread_id !== null) {
    if (typeof parsed.thread_id !== 'string') return { ok: false, error: '字段 "thread_id" 必须是字符串' }
    if (parsed.thread_id.trim() !== '') value.threadId = parsed.thread_id.trim()
  }
  return { ok: true, value }
}

// --------------------------------------------------------------------------
// 插件主体
// --------------------------------------------------------------------------

/**
 * Cordis 插件入口。
 * @param {object} ctx 插件上下文
 * @param {object} [rawConfig] cordis.patch.yml 里 `config:` 的原始值
 * @returns {() => void} 幂等的 disposer：关 HTTP、注销规则、清定时器
 */
export function apply(ctx, rawConfig) {
  const logger = makeLogger(ctx)
  const { config, warnings } = normalizeConfig(rawConfig)
  for (const warning of warnings) logger.warn(warning)

  /** 待处理消息队列（FIFO）。 */
  const queue = []
  /** 已丢弃计数，只用于 /health 观测。 */
  let dropped = 0
  /** 幂等清理标志。 */
  let disposed = false
  /** HTTP 服务器句柄。 */
  let server = null
  /** 定时器句柄。 */
  let sweepTimer = null
  /** 队列清理定时器的 unref 保护。 */
  let listening = false
  /** 缓存解析后的 agent preset。 */
  let cachedAgentPreset = null
  /** 惰性解析中的 promise，避免并发重复解析。 */
  let agentPresetPromise = null
  /** 已注册的 webhook 规则 disposer（来自 webhookRuntime.register）。 */
  let ruleDisposer = null
  /** 子 fiber 句柄（来自 ctx.inject）。 */
  let ruleFiber = null

  // ---------------------------------------------------------------- 预设

  /** 首次调用时解析并缓存 agent preset；失败也返回可用值。 */
  async function agentPreset() {
    if (cachedAgentPreset !== null) return cachedAgentPreset
    agentPresetPromise ??= resolveAgentPreset(ctx, config, logger)
      .catch(() => config.agentPreset ?? config.agentPresetFallback)
      .then((id) => {
        cachedAgentPreset = id
        agentPresetPromise = null
        return id
      })
    return agentPresetPromise
  }

  // ---------------------------------------------------------------- 队列

  /** 丢弃过期的队列条目，返回丢弃条数。 */
  function sweep(now = Date.now()) {
    let removed = 0
    while (queue.length > 0 && now - queue[0].receivedAt > config.queueTtlMs) {
      queue.shift()
      removed += 1
    }
    if (removed > 0) {
      dropped += removed
      logger.warn(`队列中 ${removed} 条消息超时（TTL ${config.queueTtlMs}ms）被丢弃`)
    }
    return removed
  }

  /**
   * 按 deliveryId 精确取出一条；找不到则取最旧的一条（FIFO 兜底）。
   * @returns {object | undefined}
   */
  function takeItem(deliveryId) {
    sweep()
    if (typeof deliveryId === 'string' && deliveryId !== '') {
      const index = queue.findIndex((item) => item.id === deliveryId)
      if (index >= 0) return queue.splice(index, 1)[0]
    }
    return queue.shift()
  }

  // ---------------------------------------------------------------- 规则

  /**
   * rule.run 返回的 Session 请求对象。
   * 运行时校验（dsh-webhook/lib/index.js resolveRequest）会检查：
   *   workspacePath 绝对；title/prompt/agentPreset/permissionPreset 为非空字符串。
   * 这里先自查一遍，不合法就返回 null 并告警，绝不让运行时抛 TypeError。
   */
  function buildRequest(item, agentPresetId, permissionPresetName) {
    if (!isAbsolute(config.workspacePath)) {
      logger.error(`workspacePath 不是绝对路径（${config.workspacePath}），本条消息不处理`)
      return null
    }
    const prompt = buildPrompt(item, config)
    const title = item.title ? `${config.titlePrefix}：${item.title}` : deriveTitle(item, config)
    if (prompt.trim() === '' || title.trim() === '') {
      logger.error('构造出的 title/prompt 为空，本条消息不处理')
      return null
    }
    return {
      workspacePath: config.workspacePath,
      title,
      prompt,
      agentPreset: agentPresetId,
      permissionPreset: permissionPresetName,
      // 不传 model：让 DSH 用 profile 的 agent-default-model 选择。
    }
  }

  /** 规则定义：kind 必须与 dispatch 的 delivery.kind 完全一致。 */
  const rule = {
    id: 'codex-bridge',
    kind: config.kind,
    /**
     * @param {object} delivery 已冻结的 provider delivery
     * @param {AbortSignal} signal 注册期生命周期信号
     * @returns {Promise<object | null>} Session 请求对象或 null（本条不处理）
     */
    async run(delivery, signal) {
      try {
        if (delivery === null || typeof delivery !== 'object') return null
        if (delivery.kind !== config.kind) return null
        if (signal && signal.aborted) return null
        const item = takeItem(delivery.deliveryId)
        if (item === undefined) {
          logger.warn(`规则被触发但队列为空（deliveryId=${String(delivery.deliveryId)}）`)
          return null
        }
        const agentPresetId = await agentPreset()
        const permissionPresetName = resolvePermissionPreset(ctx, config, logger)
        const request = buildRequest(item, agentPresetId, permissionPresetName)
        if (request === null) return null
        logger.info(`投递消息 ${item.id}（${Buffer.byteLength(item.text, 'utf8')} 字节）→ 新 DSH 会话`)
        return request
      } catch (error) {
        logger.error(`规则执行失败：${error instanceof Error ? error.message : String(error)}`)
        return null
      }
    },
  }

  // ---------------------------------------------------------------- 服务可用性

  /** webhookRuntime 当前是否可用（决定 POST 是 202 还是 503）。 */
  function webhookRuntimeOrNull() {
    return getService(ctx, 'webhookRuntime') ?? null
  }

  // ---------------------------------------------------------------- HTTP

  /** POST /codex 的处理逻辑。 */
  async function handleCodex(request, response) {
    const runtime = webhookRuntimeOrNull()
    if (runtime === null) {
      // 不消费 body 会让 keep-alive 连接脏掉，先排空再回。
      request.resume()
      sendJson(response, 503, {
        ok: false,
        error: 'webhookRuntime 不可用，DSH 会话无法创建',
        hint: '该 profile 未装载 @deepseek-ai/dsh-webhook',
      })
      return
    }

    // 运行时在、但规则还没注册好（装载竞态，或注册时抛错）。
    // 此时 dispatch 会静默丢弃消息，所以宁可返回 503 让调用方重试，也不要假装 202。
    if (ruleDisposer === null) {
      request.resume()
      sendJson(response, 503, {
        ok: false,
        error: 'codex-bridge 规则尚未注册，消息未接收',
        hint: '服务刚启动时的短暂窗口，或规则注册失败；稍后重试，并检查 DSH 日志里的 codex-bridge 告警',
      })
      return
    }

    const read = await readBoundedBody(request, config.maxBodyBytes)
    if (!read.ok) {
      sendJson(response, read.status, { ok: false, error: read.error })
      return
    }

    let parsed
    try {
      parsed = JSON.parse(read.text)
    } catch {
      sendJson(response, 400, { ok: false, error: 'JSON 解析失败' })
      return
    }

    const validated = validatePayload(parsed)
    if (!validated.ok) {
      sendJson(response, 400, { ok: false, error: validated.error })
      return
    }

    sweep()
    if (queue.length >= config.queueLimit) {
      sendJson(response, 429, {
        ok: false,
        error: `队列已满（上限 ${config.queueLimit}），请稍后重试`,
      })
      return
    }

    const item = {
      id: randomUUID(),
      text: validated.value.text,
      title: validated.value.title,
      threadId: validated.value.threadId,
      receivedAt: Date.now(),
    }
    queue.push(item)

    // 让 webhookRuntime 按 kind 找到上面这条规则，进而创建 DSH 会话。
    try {
      runtime.dispatch({
        kind: config.kind,
        source: config.source,
        deliveryId: item.id,
        receivedAt: item.receivedAt,
        event: {
          text: item.text,
          ...(item.title === undefined ? {} : { title: item.title }),
          ...(item.threadId === undefined ? {} : { thread_id: item.threadId }),
        },
      })
    } catch (error) {
      const index = queue.findIndex((queued) => queued.id === item.id)
      if (index >= 0) queue.splice(index, 1)
      logger.warn(`dispatch 失败：${error instanceof Error ? error.message : String(error)}`)
      sendJson(response, 503, { ok: false, error: 'webhookRuntime 不可用，派发失败' })
      return
    }

    logger.info(`已接收 ${item.id}，待处理 ${queue.length} 条`)
    sendJson(response, 202, { ok: true, id: item.id })
  }

  /** GET /health。 */
  function handleHealth(response) {
    sweep()
    sendJson(response, 200, {
      ok: true,
      pending: queue.length,
      dropped,
      listening,
      kind: config.kind,
      port: config.port,
      workspacePath: config.workspacePath,
      webhookRuntime: webhookRuntimeOrNull() !== null,
      ruleRegistered: ruleDisposer !== null,
    })
  }

  /** 总路由。 */
  function route(request, response) {
    let pathname
    try {
      pathname = new URL(request.url ?? '/', `http://${BIND_HOST}`).pathname
    } catch {
      sendJson(response, 400, { ok: false, error: '非法 URL' })
      return
    }

    if (pathname === '/codex') {
      if (request.method !== 'POST') {
        request.resume()
        sendJson(response, 405, { ok: false, error: '只接受 POST' }, { allow: 'POST' })
        return
      }
      void handleCodex(request, response).catch((error) => {
        logger.error(`处理 /codex 失败：${error instanceof Error ? error.message : String(error)}`)
        sendJson(response, 500, { ok: false, error: '内部错误' })
      })
      return
    }

    if (pathname === '/health') {
      if (request.method !== 'GET') {
        request.resume()
        sendJson(response, 405, { ok: false, error: '只接受 GET' }, { allow: 'GET' })
        return
      }
      handleHealth(response)
      return
    }

    request.resume()
    sendJson(response, 404, { ok: false, error: '未知路径', paths: ['POST /codex', 'GET /health'] })
  }

  function startServer() {
    const created = createServer((request, response) => {
      try {
        route(request, response)
      } catch (error) {
        logger.error(`请求处理异常：${error instanceof Error ? error.message : String(error)}`)
        sendJson(response, 500, { ok: false, error: '内部错误' })
      }
    })

    created.on('clientError', (_error, socket) => {
      try {
        if (socket.writable) socket.end('HTTP/1.1 400 Bad Request\r\n\r\n')
      } catch {
        /* 忽略 */
      }
    })

    created.on('error', (error) => {
      // 端口占用等：记录但不抛，避免整个插件装载失败。
      logger.error(`HTTP 服务器错误（${BIND_HOST}:${config.port}）：${error?.message ?? error}`)
    })

    created.listen(config.port, BIND_HOST, () => {
      listening = true
      logger.info(`HTTP 入口已监听 http://${BIND_HOST}:${config.port}（POST /codex, GET /health）`)
    })

    return created
  }

  // ---------------------------------------------------------------- 启动

  server = startServer()
  logger.info(
    `已装载：kind=${config.kind} workspace=${config.workspacePath} permission=${config.permissionPreset}`,
  )

  // 队列清理定时器：unref() 保证它不会拖着进程不退出。
  sweepTimer = setInterval(() => sweep(), Math.max(1000, Math.min(60_000, Math.floor(config.queueTtlMs / 4))))
  if (typeof sweepTimer.unref === 'function') sweepTimer.unref()

  // 延迟注册规则：webhookRuntime 到位时才注册，消失时自动注销。
  // 这是 cordis@4.0.4 下"可选注入"的正确写法。
  try {
    ruleFiber = ctx.inject(['webhookRuntime'], (scope) => {
      const runtime = scope.webhookRuntime
      if (!runtime || typeof runtime.register !== 'function') {
        logger.warn('webhookRuntime 已就绪但没有 register()，入站推送不可用')
        return
      }
      try {
        ruleDisposer = runtime.register(rule)
        logger.info(`已向 webhookRuntime 注册规则 "${rule.id}"（kind=${rule.kind}）`)
      } catch (error) {
        logger.error(`注册规则失败：${error instanceof Error ? error.message : String(error)}`)
        return
      }
      return () => {
        const dispose = ruleDisposer
        ruleDisposer = null
        if (typeof dispose === 'function') {
          try {
            const result = dispose()
            if (result && typeof result.then === 'function') result.catch(() => {})
          } catch {
            /* 忽略 */
          }
        }
      }
    })
    // fiber 是 thenable：装载失败只记日志，绝不让它变成 unhandled rejection。
    if (ruleFiber && typeof ruleFiber.then === 'function') {
      ruleFiber.then(undefined, (error) => {
        logger.warn(`webhookRuntime 规则装载失败：${error instanceof Error ? error.message : String(error)}`)
      })
    }
  } catch (error) {
    logger.warn(`无法注册 webhookRuntime 规则（服务缺失）：${error instanceof Error ? error.message : String(error)}`)
  }

  // 预热 agent preset 解析（异步，失败不影响启动）。
  void agentPreset().catch(() => {})

  // ---------------------------------------------------------------- 清理

  /**
   * 幂等清理：关 HTTP、注销规则、清定时器。
   * 刻意写成同步函数，保证在任何 cordis 卸载路径下都能跑完。
   */
  function dispose() {
    if (disposed) return
    disposed = true

    if (sweepTimer !== null) {
      try {
        clearInterval(sweepTimer)
      } catch {
        /* 忽略 */
      }
      sweepTimer = null
    }

    if (ruleDisposer !== null) {
      const pending = ruleDisposer
      ruleDisposer = null
      try {
        const result = pending()
        if (result && typeof result.then === 'function') result.catch(() => {})
      } catch {
        /* 忽略 */
      }
    }

    // 子 fiber 会随宿主一起卸载；显式再断一次以防先清理插件后清理 fiber。
    if (ruleFiber !== null) {
      try {
        ruleFiber.dispose?.()
      } catch {
        /* 忽略 */
      }
      ruleFiber = null
    }

    queue.length = 0

    if (server !== null) {
      const closing = server
      server = null
      listening = false
      try {
        closing.close(() => {})
      } catch {
        /* 忽略 */
      }
      // 断开 keep-alive 连接，否则 close() 会一直等下去、端口也不释放。
      try {
        closing.closeIdleConnections?.()
      } catch {
        /* 忽略 */
      }
      try {
        closing.closeAllConnections?.()
      } catch {
        /* 忽略 */
      }
    }

    logger.info('已卸载：HTTP 已关闭、规则已注销、定时器已清理')
  }

  return dispose
}

// 刻意**不**提供 default 导出：DSH loader 的 unwrapExports() 会做
// `exports = exports.default ?? exports`，一旦有 default 就只看 default 对象。
// 官方插件（如 @deepseek-ai/dsh-webhook-github）也只导出 { name, inject, apply }。
