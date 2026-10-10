# Freqtrade 直接 API 访问日志

`manage.py` 的交易 API 启动命令保留原来的主配置和策略，并在主配置之后增加：

```text
--config config_api_access_log.json
```

这个独立配置只覆盖 `api_server.verbosity=info` 和 `log_config`，不含密钥、交易参数或策略参数。
Freqtrade 2026.9 的 `setup_logging()` 支持 `log_config`，按 Python `dictConfig` 加载；
`ApiServer.start_api()` 中的 `uvicorn.Config(log_config=None)` 是实现约定，
不是配置遗漏，Uvicorn 因而保留上述日志配置。

## 生效状态与入口

本次修改没有重启现有机器人，也没有向交易 API 发写请求。
因此**现有机器人进程尚未加载此配置，直连访问日志尚未生效**；
已经发生的 ADA `force_exit` 不能由新日志追溯调用者。
以后正常启动 `manage.py` 的 `api` 服务时自动加载。
直接运行 Freqtrade 时也需按相同顺序带上两个配置：

```sh
cd bot
.venv/bin/python -m freqtrade trade \
  --config user_data/config_trend_live.json \
  --config config_api_access_log.json \
  --strategy TrendFollowing
```

上述命令是下一次正常启动的加载入口，不是本次执行记录。
`manage.py start api` 会复用仍在运行的进程，不会让旧进程热加载日志配置。
如果后续额外配置或环境变量再次覆盖 `api_server.verbosity` 或 `log_config`，
应重新检查最终合并配置；不要为日志修改原有交易配置优先级。

## 字段与边界

HTTP 请求追加至 `bot/logs/api-access.log`，每行只有以下 JSON 字段：

| 字段 | 含义 |
| --- | --- |
| `time_utc` | UTC 响应日志时间，不是请求开始时间 |
| `method` | HTTP 方法 |
| `path` | 不含 query 和 fragment 的路径 |
| `client` | Uvicorn 记录的连接来源地址和端口 |
| `status` | HTTP 响应状态码 |

文件以 10 MiB 轮转，保留 10 个备份。代理请求的连接来源可能是本机代理；
访问日志无法证明具体操作者、目标交易或外部干预结果，仍须结合业务审计事件。
请求正文、Authorization、查询参数和任意额外日志字段不进入访问日志。
未知格式的访问日志记录直接舍弃，不能回退输出原始消息。

Uvicorn 的 WebSocket 接受/拒绝消息使用 `uvicorn.error`，默认也会包含 query token。
因此该 logger 先过滤所有 URL 查询参数和 Authorization 文本，再向机器人普通日志传播。
异常堆栈同样先过滤。原有 Freqtrade 控制台、API 日志缓冲与 `--logfile` 功能保留。
这不构成对所有第三方 logger 的通用脱敏保证。

## 验证

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_api_access_logging.py' -v
```

测试包含 HTTP query、WebSocket 接受/拒绝、Authorization、异常堆栈、未知格式拒绝，
以及用 `bot/.venv/bin/python` 运行真实 Freqtrade 配置加载和日志初始化的隔离集成检查。
集成检查不启动服务器、不创建交易机器人、不连接交易所。
