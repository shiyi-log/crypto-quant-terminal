# DeepSeek 与 Codex 双向直接联动

当前跨助手消息使用 `desktop_link.py`，复用双方已经打开的桌面会话与模型设置。消息直接投递，不进入队列。

- Codex → DeepSeek：Harness `session/prompt`，固定 `mode=steer`。
- DeepSeek → Codex：桌面已有 IPC owner 转发 `turn/steer`；会话明确空闲后直接 `turn/start`。
- MCP `send_to_dsh`、`send_to_codex`：调用相同的桌面直接入口。已运行的旧 MCP 进程需要重连该 server 才会加载新实现，当前即时互发可直接使用 CLI 入口。

不会调用 `codex queue`、新开模型会话、另开 app-server 或并行 `thread/resume`。工具执行期间，消息仍可能要到下一安全边界才被处理。`accepted` 仅表示原生接口确认受理，不等于已回复。

完整说明和实测记录见 [DESKTOP_LINK.md](DESKTOP_LINK.md)。本机 `/Applications/ChatGPT.app` 的实际运行内容是 Codex 桌面版，消息目标绑定在 `~/.dsh-codex-bridge/desktop.json`。

## 快速使用

在项目根目录执行：

```bash
python3 tools/codex_bridge/desktop_link.py status
python3 tools/codex_bridge/desktop_link.py send --to codex --id msg-001 --text '请检查这项实现。'
python3 tools/codex_bridge/desktop_link.py send --to deepseek --reply-to msg-001 --text '这是检查结论。'
python3 tools/codex_bridge/desktop_link.py history
```

长文本从 stdin 传入，避免 shell 解释正文：

```bash
python3 tools/codex_bridge/desktop_link.py send --to deepseek < message.txt
```

`status` 只读核对两端，成功显示 `ready: true` 与 `delivery_mode: direct`。绑定或 socket 不可用时明确报错，不改用旧队列。

更换现有会话：

```bash
python3 tools/codex_bridge/desktop_link.py sessions
python3 tools/codex_bridge/desktop_link.py bind \
  --codex-thread <Codex会话UUID> \
  --dsh-session <DeepSeek会话ID>
```

## 多项目命名 link

`desktop.json` 是**唯一的默认绑定**，保持原语义不变（正在运行的应用、watchdog 与已加载的 MCP
进程都还在用它）。要同时服务多个项目，用 `links.json` 登记命名 link：

```bash
python3 tools/codex_bridge/desktop_link.py bind --link wdhash \
  --codex-thread <Codex会话UUID> --dsh-session <DeepSeek会话ID> --note '说明'
python3 tools/codex_bridge/desktop_link.py links
python3 tools/codex_bridge/desktop_link.py status --link wdhash
python3 tools/codex_bridge/desktop_link.py send --to codex --link wdhash --text '...'
```

解析规则：显式 `--link` 优先；发往 Codex 且未指定 `--link` 时，按调用者自己的
`DSH_SESSION_ID` 匹配 link，匹配不到才回落到默认绑定；发往 DeepSeek 时不自动匹配，
存在多个命名 link 且未指定 `--link` 会直接拒绝，没有命名 link 歧义时才回落到默认绑定。

Codex 侧**无法自动识别自己在哪个项目**——实测桥接 MCP 进程的环境变量除 PID 外完全相同，
所以多项目并存时 `send_to_dsh` / `send_to_codex` 需要显式传 `link`。link 参数是
2026-10-10 新增的，**已在运行的 MCP 进程仍是旧代码**，需要重连（或新开一个 Codex 对话）
才会加载。link 允许是部分绑定（例如只有 `dsh_session_id`），未绑定方向的发送会明确报错，
不会静默借用别的项目会话。

## 文件与状态

| 文件 | 用途 |
|---|---|
| `desktop_link.py` | 默认绑定、命名 link 路由、双向直接发送、投递流水 |
| `link_registry.py` | `links.json` 命名 link 注册表（多项目并存） |
| `direct_codex.py` | 当前桌面 native IPC，仅直接 turn/steer 与 turn/start |
| `dsh_send_fixed.py` | 原应急发送器的兼容入口，复用主通道与投递防重 |
| `link_watchdog.py` | 单次发送探针与健康告警；未确认时不重试或改走旁路 |
| `mcp_server.py` | MCP stdio 直接发送入口；旧队列只读工具标为 legacy |
| `DESKTOP_LINK.md` | 当前通道、回执、去重、验收与协议来源 |
| `bridge.py`、`codex_client.py`、`bridge_core.py` | 历史独立 CLI／文件队列方案 |
| `dsh_plugin/` | 历史插件推送方案，需要独立安装与重启生效 |
| `PROTOCOL.md` | 历史队列协议，不能覆盖当前用户的直接发送要求 |
| `tests/` | 隔离的 Harness、Unix socket、MCP stdio 与历史 CLI 测试 |

当前状态：

```text
~/.dsh-codex-bridge/desktop.json    # 默认绑定的双方会话，不存 Cookie
~/.dsh-codex-bridge/links.json      # 命名 link（多项目并存），权限 600
~/.dsh-codex-bridge/desktop.sqlite  # 尝试与回执流水，不是消息队列
~/.codex/ipc/ipc.sock              # 已运行桌面的 IPC，不启动新服务
```

配置与流水可用 `CODEX_BRIDGE_HOME` 隔离，权限为 `600`。DeepSeek 登录态每次只读复用，只发送给匹配端口的本机 Harness；Codex socket 验证当前用户所有权。

## 验证

```bash
python3 -m unittest discover -s tools/codex_bridge/tests -v
```

默认测试不启动真实模型、不连接真实聊天。直接通道覆盖活动/空闲投递、字面正文、错误回执、连接丢失、缺绑定和拒绝 queue；历史 CLI 的联网测试默认跳过。

`link_watchdog.py` 的每次检查会明确发送一条探针。超时、失败、缺少受理回执时只告警，不通过另一个发送器重发。原 `dsh_send_fixed.py` 命令保留旧参数，内部共用 `desktop_link.py` 的流水与防重；原诊断证据保留在 `DSH_BUGREPORT-direct_codex-method-check.md`。

2026-10-09，DeepSeek 用当前代码发回 `DIRECT_LINK_ACK 17:14:07 +0800`，已进入原 Codex 会话；后续交叉测试消息也已收到，native 回执为 `turn/steer`，旧队列没有新增。

## 历史方案

`bridge.py send/flush/poll` 与守护进程会使用文件队列和独立 CLI 会话；`dsh_plugin/` 另有安装流程。这些文件为已有独立用途保留，不用于当前跨助手直接消息。原队列详解见 [PROTOCOL.md](PROTOCOL.md)，当前消息请始终使用上面的桌面入口。
