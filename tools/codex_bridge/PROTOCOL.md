> 当前直接发送要求优先于本文历史队列契约：跨助手消息使用 `desktop_link.py` 的 native IPC / Harness steer，MCP 发送工具也使用直接通道，禁止 fallback 队列。本文只描述保留的独立 CLI／队列方案。见 [DESKTOP_LINK.md](DESKTOP_LINK.md)。

# DSH ⇄ ChatGPT(Codex Desktop) 双向桥接 — 协议契约（冻结版 v1）

> ## ⚠️ 通道优先级（重要）
>
> **本次双向联动请优先使用 `desktop_link.py`**（见第 7 节）——它复用双方已开的桌面会话，
> **实时投递**，无需重启、无需插件、无需轮询。
>
> - `bridge.py` + `codex_client.py`（`codex exec` 队列方案）是**独立 CLI 会话**方案：
>   会新开/续聊一个 CLI 线程，**不必为本次联动启动它**。
> - `dsh_plugin/` 需要**重启 DSH** 才生效，且 desktop profile 是否有 `webhookRuntime` 未验证。
> - `mcp_server.py` 的 `send_to_dsh` **只写队列文件，不会即刻投递到运行中的会话**；
>   且 headless `codex exec` 下 MCP 工具调用会被审批策略拒绝。**不要宣称它是实时通道。**

本文件是**唯一**的共享接口契约。所有实现必须逐字遵守；任何改动先改本文件，再改代码。

## 0. 事实基线（已实测）

- `/Applications/ChatGPT.app` 的 `CFBundleName` 是 `ChatGPT`，但它**实际是 Codex 桌面版**
  （`Contents/MacOS/ChatGPT`，`~/.codex`，`Resources/codex-cli`）。
- CLI：`/opt/homebrew/bin/codex`（`codex-cli 0.162.0-alpha.2`）。
- 模型走本机代理 `http://127.0.0.1:15721/v1`，模型 `gpt-6.1-sol`，已登录可用。
- 出站已实测通过：
  - 新建线程：`codex exec --json -o <file> "<prompt>"` → stdout 首行含
    `{"type":"thread.started","thread_id":"<uuid>"}`
  - 续聊同一线程（保留上下文）：
    `codex exec --json resume <uuid> "<prompt>"`
  - 最终回答：JSONL 中 `{"type":"item.completed","item":{"type":"agent_message","text":...}}`，
    或直接读 `-o` 指定的文件。
- 在 `/Users/shiyi/DeepSeek/量化`（`config.toml` 中 trust_level = "trusted"）下运行才能持久化会话。
- DSH 侧：profile `desktop` 由 Electron 独占，`dsh --profile desktop` **不可用**；
  但在 profile 的 `cordis.patch.yml` 里用**绝对路径**插入本地插件是可行的
  （实测 `name: /abs/path/plugin.mjs` 会被规范化成 `file:///abs/path/plugin.mjs` 并出现在 `--dump-config`）。
- DSH 入站注入的官方机制是 `ctx.webhookRuntime`：
  `rule.run(delivery, signal)` 返回 `null` 或
  `{ workspacePath, title, prompt, agentPreset, permissionPreset, model? }`，
  运行时据此**创建一个新的根会话**并投递 prompt。

## 1. 根目录

所有桥接状态放在：

```
${CODEX_BRIDGE_HOME:-$HOME/.dsh-codex-bridge}/
├── queue/                     # 队列文件（JSONL，追加写）
│   ├── to_chatgpt.jsonl       # DSH → ChatGPT 出站
│   └── to_dsh.jsonl           # ChatGPT → DSH 入站
├── thread.json                # 线程状态（原子替换写）
├── ledger.jsonl               # 双向流水（只追加，永不修改）
├── daemon.pid                 # 守护进程 pid
├── daemon.log                 # 守护进程日志
└── state/cursor.json          # 各消费者的已读游标（原子替换写）
```

**写入纪律（强制）**
- JSONL 队列与流水：只允许 `O_APPEND` 追加，一行一个 JSON 对象，UTF-8，`\n` 结尾。
- 单行必须 < 64 KiB，超长截断并把 `truncated: true` 置位。
- `thread.json` / `cursor.json`：写临时文件 + `os.replace()` 原子替换。
- 任何一方不得重写、压缩、删除队列文件；只有 `bridge.py compact` 可以归档。
- 多进程同时追加同一队列文件是允许的（`O_APPEND` + 单次 `write` 保证行不撕裂）。
  `to_chatgpt.jsonl` 的写入者：`bridge.py`（send/HTTP /send）与 MCP 工具。
  `to_dsh.jsonl` 的写入者：守护进程（ChatGPT 的回答）与 MCP 工具（`send_to_dsh`）。
- **消费者只能有一个**：游标文件由 `read_messages(advance=True)` 独占推进。
  `to_chatgpt` 的消费者是守护进程；`to_dsh` 的消费者是 DSH 侧。
  其他角色只能用 `peek_new()`（不推进游标）。

## 2. 消息对象

```json
{
  "id": "m-<uuid4hex>",
  "ts": "2026-10-09T16:30:00.123456+08:00",
  "direction": "to_chatgpt",
  "source": "dsh",
  "text": "消息正文（必填，非空）",
  "thread_id": "01a11fc2-58b2-7de3-ac27-1c271b5135e8",
  "kind": "message",
  "meta": {},
  "truncated": false
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | `m-` + uuid4 hex；全局唯一，用于去重与回执 |
| `ts` | 是 | RFC3339 带本地时区偏移 |
| `direction` | 是 | `to_chatgpt` 或 `to_dsh` |
| `source` | 是 | `dsh` / `chatgpt` / `mcp` / `daemon` |
| `text` | 是 | 非空字符串 |
| `thread_id` | 否 | Codex 线程 uuid；出站已有线程时必填 |
| `kind` | 是 | `message`（默认）/ `reply` / `system` |
| `meta` | 否 | 任意 JSON 对象；`meta.error` 表示投递失败 |
| `truncated` | 否 | 正文是否被截断 |

## 3. `thread.json`

```json
{
  "thread_id": "01a11fc2-58b2-7de3-ac27-1c271b5135e8",
  "created_at": "2026-10-09T16:30:00+08:00",
  "updated_at": "2026-10-09T16:35:00+08:00",
  "turn_count": 3,
  "last_outbound_id": "m-ab12...",
  "last_inbound_id": "m-cd34...",
  "last_error": null,
  "model": "gpt-6.1-sol",
  "cwd": "/Users/shiyi/DeepSeek/量化"
}
```

- 不存在或 `thread_id` 为空 ⇒ 下一条出站走**新建线程**，随后写回此文件。
- 已存在 ⇒ 下一条出站走 `codex exec resume <thread_id>`。
- `turn_count` 每成功投递一次 +1。

## 4. 游标 `state/cursor.json`

```json
{ "to_dsh": {"offset": 0, "consumed_ids": []}, "to_chatgpt": {"offset": 0} }
```

- `offset` = 已读取的**字节数**（消费者从该偏移读新内容）。
- `consumed_ids` 最多保留最近 500 个 id，用于幂等去重。

## 5. 公开 API（Python 模块与 CLI）

```python
# bridge_core.py
home() -> Path
append_message(direction: str, text: str, *, source: str, kind: str = "message",
               thread_id: str | None = None, meta: dict | None = None) -> dict
read_messages(direction: str, *, advance: bool = True, limit: int = 200) -> list[dict]
peek_new(direction: str) -> list[dict]          # 不推进游标
load_thread() -> dict
save_thread(**fields) -> dict
ledger(entry: dict) -> None
log(msg: str) -> None
```

CLI（`bridge.py`）：

```
bridge.py send "<text>"            # 追加到 to_chatgpt 队列（不做网络调用）
bridge.py poll [--direction to_dsh] [--json]   # 打印新入站消息
bridge.py thread                   # 打印 thread.json
bridge.py state                    # 打印游标与队列长度
bridge.py serve [--port 8899]      # 前台守护进程
bridge.py flush                    # 立即消费一次出站队列
```

守护进程 HTTP（仅 127.0.0.1）：

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/send` | `{"text": "...", "wait": false}` | `202 {"id": "...", "queued": true}` |
| POST | `/inbound` | `{"text": "...", "source": "mcp"}` | `202 {"id": "..."}` |
| GET | `/health` | — | `200 {"ok": true, "thread_id": ..., "pending_out": N, "pending_in": N}` |
| GET | `/thread` | — | `200 thread.json` |

守护进程负责：消费 `to_chatgpt` → 调 codex → 把回答写进 `to_dsh`（`kind: "reply"`）。

## 6. 已实测修正（重要，勿按错误写法实现）

### 6.1 DSH 插件的 `inject` 写法

- ❌ `export const inject = { optional: ['webhookRuntime'] }` —— cordis@4.0.4 的对象形态里
  **键就是服务名**，且全部视为**必需**。这样写会让插件去等一个名叫 `optional` 的服务，
  fiber 永远 INACTIVE，`apply()` 根本不被调用。
  （实测：`Inject.resolve(['webhookRuntime']) => {"webhookRuntime":null}`；
  `Inject.resolve({optional:['webhookRuntime']}) => {"optional":["webhookRuntime"]}`）
- ✅ 正确写法：`export const inject = []`，然后在 `apply` 内
  `ctx.inject(['webhookRuntime'], scope => scope.webhookRuntime.register(rule))`，
  并用 `ctx.get('webhookRuntime')` 做软查询以优雅降级。

### 6.2 `cordis.patch.yml` 的插入写法

- ✅ 裸 `insert:`（DSH 自己的 `dsh-base` 补丁层也是这样）
- ❌ `- id: codex-bridge` + `insert:` → `patch insert: entry "codex-bridge" not found`

### 6.3 Codex 侧 MCP 工具调用需要审批

实测 `codex exec` 调 MCP 工具时返回：

```
MCP tool call requires approval, but approval policy is never
```

即 **headless `codex exec` 下 MCP 工具调用会被审批策略挡掉**（`approval: never`）。
在桌面 GUI 里模型会弹出审批请求，用户点允许即可。
因此：**MCP 是本机自动化通道的备选**，桌面真人对话场景可用；
真正的实时双向通道是 `desktop_link.py`（见第 7 节）。

## 7. 实时桌面通道（首选，已实测双向打通）

`desktop_link.py` 复用**双方已存在的桌面会话**，不新开模型会话、不改全局配置、不重启应用：

| 方向 | 机制 |
|---|---|
| DSH → ChatGPT | `codex queue --thread <id> --message <text>`（bundled CLI，投递给运行中的桌面会话）|
| ChatGPT → DSH | Harness 原生 `POST /api/session/prompt`，`mode: "steer"`，复用桌面登录态 cookie |

```bash
P=/Users/shiyi/DeepSeek/量化/.venv/bin/python3
L=/Users/shiyi/DeepSeek/量化/tools/codex_bridge/desktop_link.py
$P $L status                                   # 核对绑定
$P $L send --to codex   --id <唯一id> --text "…"
$P $L send --to deepseek --id <唯一id> --text "…"
$P $L history                                  # 投递流水（accepted ≠ 已回复）
```

绑定落在 `~/.dsh-codex-bridge/desktop.json`（**不存 cookie**，每次只读复用登录态）。

**已实测证据**：
- `status` → `ready: true`，识别出本会话 `session-1da3c96f-…`（标题「ChatGPT 双向消息联动」）
- Codex → DSH：流水 `codex-desktop-link-ready-20261009`，`status: accepted`
- DSH → Codex：流水 `dsh-native-ack-20261009`，`accepted: true`，`queued: true`

## 8. 文件所有权（避免写冲突）

| 文件 | 唯一作者 |
|---|---|
| `bridge_core.py` | Lead |
| `bridge.py` | Lead |
| `codex_client.py`, `tests/test_codex_client.py` | T1 |
| `dsh_plugin/codex-bridge-plugin.mjs` | T2 |
| `mcp_server.py`, `tests/test_mcp_server.py` | T3 |
| `PROTOCOL.md`, `README.md` | Lead |
