# Codex 与 DeepSeek 当前桌面会话直接联动

`desktop_link.py` 复用双方现有会话与模型设置，直接投递消息：

- Codex → DeepSeek：Harness 原生 `session/prompt`，固定 `mode=steer`。
- DeepSeek → Codex：`direct_codex.py` 连接当前桌面的 `~/.codex/ipc/ipc.sock`，经已有 owner 直接调用活动会话的 `turn/steer`；原生明确会话空闲时调用 `turn/start`。

两个方向都不使用消息队列。不会调用 `codex queue`、新开助手、另开 app-server 或 `thread/resume`。长工具调用期间，助手可能要到下一安全边界才处理消息。

## 发消息

在项目根目录执行：

```bash
python3 tools/codex_bridge/desktop_link.py status
python3 tools/codex_bridge/desktop_link.py send --to deepseek --text '请检查这项实现，并把结论发回来。'
python3 tools/codex_bridge/desktop_link.py send --to codex --text '检查完成，这是我的结论。'
python3 tools/codex_bridge/desktop_link.py history
```

`status` 只读检查两端绑定，显示 `delivery_mode: direct`。长文本可从 stdin 输入，避免 shell 解释正文：

```bash
python3 tools/codex_bridge/desktop_link.py send --to deepseek < message.txt
```

`--reply-to <消息ID>` 记录回复关联；普通消息是否需要回复由接收助手判断，避免自动无限互相回复。`--mode` 仅接受 `steer`；Python 调用传入 `queue` 也会在访问应用之前被拒绝。

`accepted` 表示接收端确认直接受理；不代表对方已读或已回复。Codex 回执还包含 `method`、`turn_id` 与 `transport: desktop-native-ipc`。两个桌面应用需正常运行，目标 Codex 会话需有可用的桌面 owner；不可用时明确失败，不回退到队列。

## MCP 入口

`mcp_server.py` 的 `send_to_dsh` 与 `send_to_codex` 使用相同直接通道；绑定缺失会返回工具错误，不写队列。`get_my_messages`、`read_dsh_replies` 仅只读旧方案的存量队列，不能代表当前桌面收信状态。

常驻 MCP 进程不会自动热加载 Python 文件。已加载旧版的连接需要重新连接该 MCP server 才能使用新实现；本次没有重启桌面应用或常驻 MCP。当前立即互发使用 `desktop_link.py`。

## 绑定与本机登录态

```bash
python3 tools/codex_bridge/desktop_link.py sessions
python3 tools/codex_bridge/desktop_link.py bind \
  --codex-thread <Codex会话UUID> \
  --dsh-session <DeepSeek会话ID>
```

绑定只更新消息目标，使用前先核对 `status`。Harness 地址变化时在 `bind` 指定 `--dsh-url http://127.0.0.1:<端口>`。Codex 的 `--codex-binary` 保留为旧绑定兼容字段；直接投递不启动 CLI。

绑定保存在 `~/.dsh-codex-bridge/desktop.json`，投递流水保存在同目录 `desktop.sqlite`，权限均为 `600`。流水只记录尝试和回执，不是待处理队列。配置不保存 Cookie；每次请求只读复用 DeepSeek 桌面的本机登录态，只发给匹配端口的 `127.0.0.1` Harness，不经过代理或 HTTP 重定向。Codex IPC 必须是当前用户的本机 Unix socket。

## 多项目命名 link

`desktop.json` 仍是唯一的默认绑定，语义不变：正在运行的应用、watchdog 和已加载的 MCP 进程都继续用它。需要多项目并存时，把额外的配对写进同目录 `links.json`：

```bash
python3 tools/codex_bridge/desktop_link.py bind --link wdhash \
  --codex-thread <Codex会话UUID> --dsh-session <DeepSeek会话ID> --note '说明'
python3 tools/codex_bridge/desktop_link.py links
python3 tools/codex_bridge/desktop_link.py status --link wdhash
python3 tools/codex_bridge/desktop_link.py send --to codex --link wdhash --text '...'
```

解析顺序：显式 `--link` → （仅发往 Codex 时）调用者自己的 `DSH_SESSION_ID` 匹配 → 默认绑定。发往 DeepSeek 时不自动匹配会话；存在多个命名 link 且省略 `--link` 会拒绝，只有没有命名 link 歧义时才回落到默认绑定。

Codex 侧无法自动识别来源项目：实测桥接 MCP 进程的环境变量除 PID 外完全相同（2026-10-10），因此多项目并存时 `send_to_dsh` / `send_to_codex` 由调用方显式传 `link`；存在多个命名 link 时省略会拒绝，没有命名 link 歧义时才使用默认绑定。`link` 参数为 2026-10-10 新增，**已运行的 MCP 进程需要重连或新开一个 Codex 对话**才会加载。link 允许部分绑定（例如只有 `dsh_session_id`），未绑定方向的发送明确报错，不静默借用其他项目会话。

## 去重与未确认状态

用 `--id` 为一次逻辑消息指定稳定 ID。同一 ID 成功发送后重复调用只返回本地回执；改变目标、正文或回复关联会被拒绝。Harness 原生支持 ID 去重，未确认消息可查看 `history` 后加 `--retry` 明确重试。

Codex 原生 RPC 未承诺这条链路的端到端 ID 去重。超时、断线、错误 owner 或缺少有效 turn 回执时保留 `unconfirmed` 流水，并拒绝自动重发。只有原生明确回答活动 turn 已结束，才转为 `turn/start`；其他失败不会转 start。先在目标聊天核对是否收到，再决定是否用新 ID 发送。

## 实测与协议来源

2026-10-09，DeepSeek 使用更新后的入口发回 `direct-dsh-ack-20261009`（`DIRECT_LINK_ACK 17:14:07 +0800`），消息已进入原 Codex 会话；随后 `iso-1` 至 `iso-4`、`bi-5` 也通过直接通道收到。DeepSeek 确认 native IPC 回执为 `turn/steer`，旧队列没有新增记录。空闲分支、连接丢失和错误回执由隔离 Unix socket 测试覆盖。

官方 [OpenAI Docs：App server](https://developers.openai.com/codex/app-server/) 定义 `turn/steer` 和 `turn/start`。本机 CLI 生成的 JSON schema 已核对。当前桌面 app-server 仅使用 stdio，没有开放官方控制 socket，因此复用桌面已有 IPC relay；其 framing、版本、owner discovery 和 follower 方法已按本机安装版本核对，并做只读探测。桌面升级若改变该内部协议会明确失败。

`bridge.py`、`codex_client.py` 与 `dsh_plugin/` 保留为历史独立 CLI／队列方案，不用于当前直接跨助手消息。旧 `PROTOCOL.md` 的队列契约描述该历史方案；当前直接通道以本文及用户的直接发送要求为准。
