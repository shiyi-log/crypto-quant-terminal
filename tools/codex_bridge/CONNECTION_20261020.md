# 当前联合开发连接

2026-10-20（北京时间）在恢复后的仓库完成当前桌面会话双向验证。

- 工作区：`/Users/shiyi/Documents/ChatGPT/量化`
- 命名通道：`quant`；本机绑定位于 `~/.dsh-codex-bridge/links.json`。
- Codex 会话：`01a1556b-983b-71e3-866c-f6524a8497a8`
- DeepSeek 会话：`session-2f91d371-064a-453c-af7b-a85f23d13889`
- 当前 Harness 地址：以 DeepSeek 运行时的 `DSH_WEB_URL` 为准（本次重启后为 `http://127.0.0.1:51680`）；不要把端口写死到新任务里。
- `status --link quant` 返回 `ready: true`，Codex 使用 `desktop-native-ipc`。
- 发送 `quant-connect-20261020-codex-01` 获 `accepted: true`；DeepSeek 以 `quant-connect-20261020-dsh-ack-01` 回复，实际进入当前 Codex 会话。

## 后续协作

双方使用共享工作区，开始任务前明确职责、文件范围和验证标准，避免同时编辑同一文件。每条消息显式指定 `--link quant`，使用唯一消息 ID，回复带 `--reply-to`。未确认投递时不自动重发；`accepted` 与对方完成任务分别记录。

```bash
python3 tools/codex_bridge/desktop_link.py status --link quant
python3 tools/codex_bridge/desktop_link.py send --to deepseek --link quant --id <唯一ID> --text '<任务或评审请求>'
python3 tools/codex_bridge/desktop_link.py send --to codex --link quant --id <唯一ID> --reply-to <原消息ID> --text '<结果与验证证据>'
```

当前绑定复用已有桌面会话。历史交接文档中的旧路径、旧会话和旧端口不代表当前连接状态。源码恢复不包含本机绑定或登录态；另一台机器需重新绑定。
