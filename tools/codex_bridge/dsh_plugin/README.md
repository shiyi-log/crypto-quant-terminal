# DSH 入站推送插件（codex-bridge）

让 **ChatGPT（Codex Desktop）主动把消息推进 DSH**，并在 DSH 里**自动开一个新会话**。

这是桥接的"入站推送"通道；协议契约见 [`../PROTOCOL.md`](../PROTOCOL.md)。

---

## 1. 它做什么

```
ChatGPT(Codex Desktop)
   │  POST http://127.0.0.1:8898/codex   {"text":"...","title":"...","thread_id":"..."}
   ▼
本插件（跑在 DSH 进程里）
   │  1. 校验 JSON / 字段 / 体积（上限 256 KiB）
   │  2. 生成 id（crypto.randomUUID()）→ 放进内存队列
   │  3. ctx.webhookRuntime.dispatch({kind:'codex-bridge', deliveryId:id, ...})
   ▼
DSH webhookRuntime
   │  按 kind 匹配到本插件注册的规则
   ▼
rule.run(delivery, signal)  ← 从队列取出一条
   │  返回 { workspacePath, title, prompt, agentPreset, permissionPreset }
   ▼
DSH 运行时：创建新的根会话 + 投递 prompt
   ▼
你在 DSH 里看到一个以「ChatGPT：…」命名的新会话，内容是带来源标注的 prompt
```

要点：

- **HTTP 只绑 `127.0.0.1`**，不对局域网暴露。
- HTTP 层在 `dispatch()` 返回后立刻回 `202`，**不等**会话创建完成（fire-and-forget）。
- `/health` 里的 `pending` 是"已入队但规则还没取走"的条数，正常应长期为 0。

## 2. 接口

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| `POST` | `/codex` | `{"text":"必填，非空","title":"可选","thread_id":"可选"}` | `202 {"ok":true,"id":"<uuid>"}` |
| `GET` | `/health` | — | `200 {"ok":true,"pending":N,...}` |

错误码：

| 码 | 含义 |
|---|---|
| `400` | JSON 解析失败 / `text` 缺失或为空 / 字段类型错 |
| `405` | 方法不对（`/codex` 只收 POST，`/health` 只收 GET），响应带 `allow` 头 |
| `413` | 请求体超过 256 KiB |
| `429` | 内存队列已满（默认上限 200 条），稍后重试 |
| `503` | `webhookRuntime` 不可用（profile 没装 webhook 运行时，或规则还没注册好） |
| `404` | 未知路径 |

```bash
# 发一条消息，让 DSH 开新会话
curl -s -X POST http://127.0.0.1:8898/codex \
  -H 'Content-Type: application/json' \
  -d '{"text":"你好，帮我看看昨天的回测为什么失败","title":"回测排查"}'

# 探活
curl -s http://127.0.0.1:8898/health
```

## 3. 安装

```bash
bash /Users/shiyi/DeepSeek/量化/tools/codex_bridge/dsh_plugin/install.sh
```

脚本会：

1. 备份 `~/.dsh/profiles/desktop/cordis.patch.yml`
   → `cordis.patch.yml.bak-codex-bridge-<时间戳>`；
2. **幂等**：已装过就直接跳过；
3. 以**裸 `insert`** 追加一个条目（`id: codex-bridge`，`name:` 是插件的**绝对路径**）；
4. 写入前校验 YAML 合法，不合法就放弃写入、原文件不动。

### ⚠️ 装完必须重启 DSH 应用

profile 只在 DSH **启动时**合成，热改 `cordis.patch.yml` 不会生效。
**不重启就没有 8898 端口，也没有 webhook 规则。**

重启后验证：

```bash
curl -s http://127.0.0.1:8898/health
# 期望：{"ok":true,"pending":0,...,"webhookRuntime":true,"ruleRegistered":true}
```

`webhookRuntime:false` 或 `ruleRegistered:false` 说明该 profile 没有 webhook 运行时
（见下面第 6 节），此时 `POST /codex` 会返回 503。

### 可配置项

`config:` 写在 `cordis.patch.yml` 的条目里，任何一项都能省（省掉用默认值）。
脚本支持用环境变量改写入的初值：

| 环境变量 | 对应配置 | 默认 |
|---|---|---|
| `BRIDGE_PORT` | `port` | `8898` |
| `BRIDGE_WORKSPACE` | `workspacePath` | `/Users/shiyi/DeepSeek/量化` |
| `BRIDGE_PERMISSION_PRESET` | `permissionPreset` | `danger-full-access` |
| `DSH_PROFILE_DIR` | —— | `~/.dsh/profiles/desktop` |

完整配置项（含 `kind` / `source` / `maxBodyBytes` / `queueLimit` / `queueTtlMs` /
`titlePrefix` / `agentPreset` / `replyHint`）见
[`codex-bridge-plugin.mjs`](./codex-bridge-plugin.mjs) 顶部的 `DEFAULTS`。

> 本插件**不导入** `@deepseek-ai/schemastery`（那是被禁止的跨树导入），
> 所以不做 Schemastery 校验，改为手写消毒：**任何非法配置项都静默回退到默认值，
> 绝不抛异常**（抛异常会让整个插件装载失败）。

## 4. 卸载

```bash
bash /Users/shiyi/DeepSeek/量化/tools/codex_bridge/dsh_plugin/uninstall.sh
```

- 只删 `# >>> codex-bridge >>>` … `# <<< codex-bridge <<<` 之间的块（以块内
  `id: codex-bridge` 定位），其余内容逐字保留；
- 删前先备份 `cordis.patch.yml.bak-codex-bridge-uninstall-<时间戳>`；
- 删后校验 YAML 仍合法，不合法就放弃写入；
- 同样**需要重启 DSH 应用**才生效；重启后 8898 端口不再监听。

## 5. "可选注入"为什么不能写 `inject = { optional: [...] }`

任务书建议写：

```js
export const inject = { optional: ['webhookRuntime'] }   // ❌ 在 DSH 上不成立
```

**在 DSH 随包的 cordis@4.0.4 上这是错的，会让插件永远不加载。**
`Inject.resolve()`（`cordis/lib/index.js`）只有两种语义：

- 数组 `['svc']` → 每个元素都是**必需**服务；
- 对象 `{ svcName: interceptConfig }` → **键本身就是服务名**，且仍然是**必需**服务。

实测证据：

```js
Inject.resolve(['webhookRuntime'])            // => {"webhookRuntime":null}
Inject.resolve({ optional: ['webhookRuntime'] })  // => {"optional":["webhookRuntime"]}
```

第二种结果里依赖集合的键是 **`optional`**，即插件在等一个**名叫 `optional` 的服务**。
该服务不存在 ⇒ fiber 永远停在 `INACTIVE` ⇒ `apply()` 根本不会被调用。

所以本插件写 `export const inject = []`，并用 cordis 的正确写法实现"可选注入"：

```js
ctx.inject(['webhookRuntime'], (scope) => {
  const dispose = scope.webhookRuntime.register(rule)   // 服务到位才注册
  return () => dispose()                                // 服务消失/插件卸载自动注销
})
```

再加上 `ctx.get('webhookRuntime')` 软查询（DSH 自己的 `dsh-base` 补丁层也用
`!!js "!ctx.get('profileContext')"`），于是：

- `webhookRuntime` 不在 → HTTP 照样起来，`POST /codex` 返回 **503**，日志有说明；
- `webhookRuntime` 在 → 自动注册规则，`POST /codex` 返回 202 并开新会话。

### 同理：patch 条目必须是"裸 insert"

实测（`dsh --profile <tmp> --patch <p> --dump-config`）：

| 写法 | 结果 |
|---|---|
| `- insert:` + 子条目 | ✅ 插件出现在合成配置里 |
| `- id: codex-bridge` + `insert:` | ❌ `patch insert: entry "codex-bridge" not found` |

带 `id` 的补丁语义是"**指向已存在的条目**再改它"，而根条目里并没有 `codex-bridge`。
所以 install.sh 用裸 `insert`（DSH 的 `dsh-base` 补丁层也是这么写的）。

## 6. 前置条件：profile 里得有 webhook 运行时

本插件只负责"HTTP 入口 + 规则"，真正创建会话的是 DSH 的
`@deepseek-ai/dsh-webhook`（服务名 `webhookRuntime`）。

DESKTOP profile 的 bundle 列表是
`dsh-base / dsh-web-app / dsh-experimental-agent-team-profile / dsh-experimental-auto-review /
dsh-experimental-schedule-bundle / dsh-experimental-voice-input-bundle`，
这些 bundle 的补丁层里**没有** webhook 条目（已 grep 确认）。

> ⚠️ **这一点我没能端到端验证**：desktop profile 由 Electron 独占，
> `dsh --profile desktop` 会被拒绝（`profile "desktop" is managed exclusively by the
> Electron application`），所以无法用 CLI dump 出 desktop 的真实合成配置。

**重启后请先 `curl /health` 看 `webhookRuntime` 字段**：

- `true` → 一切就绪，`POST /codex` 能开新会话；
- `false` → 需要往 profile 里加 webhook 运行时，即在 `cordis.patch.yml` 里再追加：

  ```yaml
  - insert:
      - id: webhook-runtime
        name: '@deepseek-ai/dsh-webhook'
  ```

  然后再重启 DSH。此时 HTTP 入口已经可用（会返回 503 + 明确原因），
  加上运行时后即可 202。

## 7. 排障

| 现象 | 原因 / 处理 |
|---|---|
| `curl: (7) Failed to connect` | 没重启 DSH；或端口被 `port` 配置改了；看 DSH 启动日志 |
| `POST /codex` 返回 503 | `webhookRuntime` 不可用 → 见第 6 节 |
| `/health` 里 `pending` 一直涨 | 规则没注册（`ruleRegistered:false`），消息只进队列没被消费；TTL 到期会自动清 |
| 429 | 队列满，说明上游发得比消费快；调大 `queueLimit` 或查 503 原因 |
| DSH 启动失败 | `cordis.patch.yml` 被写坏。用 `.bak-codex-bridge-<时间戳>` 还原后重启 |
| 装了但日志无 `codex-bridge:` 字样 | 插件没被装载：确认 patch 用的是裸 `insert`（见第 5 节） |

查看插件日志：DSH 应用日志里搜 `codex-bridge:` 前缀。

## 8. 文件

| 文件 | 作用 |
|---|---|
| `codex-bridge-plugin.mjs` | 插件本体。**只 import Node 内置模块**，不含密钥 |
| `install.sh` | 安装（备份 + 幂等 + YAML 校验 + 提示重启） |
| `uninstall.sh` | 卸载（备份 + 精确删块 + YAML 校验 + 提示重启） |
| `README.md` | 本文件 |

## 9. 验证命令（开发者）

```bash
cd /Users/shiyi/DeepSeek/量化/tools/codex_bridge

# 1) 语法
node --check dsh_plugin/codex-bridge-plugin.mjs

# 2) 配置合成（用一次性 profile，不碰运行中的应用）
D="/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
"$D" --profile bpoc --patch <(printf -- "- insert:\n    - id: codex-bridge\n      name: '%s'\n" \
      "$PWD/dsh_plugin/codex-bridge-plugin.mjs") --dump-config | grep -A2 codex-bridge

# 3) install/uninstall 干跑（在临时目录造 mock profile，不碰真实配置）
export DSH_PROFILE_DIR="$(mktemp -d)"
mkdir -p "$DSH_PROFILE_DIR/profiles/desktop"
# ... 造好 cordis.patch.yml 后分别跑 install.sh（两次，验幂等）与 uninstall.sh
```

`install.sh` / `uninstall.sh` **绝不要**在验证时指向真实的
`~/.dsh/profiles/desktop`，除非确实要安装。
