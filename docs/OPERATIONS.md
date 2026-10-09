# Python 运维与 uv 环境

所有项目启动、停止、构建、审计、同步与截图操作统一使用 Python。根目录旧的 `.sh` 仅留在原机器作为历史文件，不进入开源仓库。这里的命令只管理前端、认证代理、Freqtrade API、文件同步与高频行情采集。模型迭代、策略研究、研究刷新和原监控守护由原负责人维护。

## 安装依赖

安装 [uv](https://docs.astral.sh/uv/) 后，在项目根目录执行：

```text
uv sync --locked
```

项目固定使用 Python 3.12，依赖版本记录在 `uv.lock`。运行命令会自动使用根目录 `.venv`，无需手动激活虚拟环境。已有机器的 Freqtrade 优先使用 `bot/.venv/bin/python`；这个环境与模型环境保持独立管理。全新机器如果没有旧交易环境，可以额外安装交易引擎：

```text
uv sync --locked --extra trading
```

前端使用 `vben/package.json` 指定的 pnpm 版本和 `vben/.node-version` 指定的 Node 版本，安装命令为：

```text
pnpm --dir vben install --frozen-lockfile
```

将 `.env.example` 复制为 `.env`，填写本地数据库连接与认证设置。`runtime_config.load_environment()` 自动加载项目根目录 `.env`，已在终端或 CI 中设置的变量优先。`.env`、认证记录、数据库目录、日志和行情原始数据不提交到 Git。使用 `uv run python manage.py init-config` 生成本机随机凭据的模拟盘配置后，按本机情况填写交易所 API 凭据与交易设置；配置中的 `dry_run` 应与计划的运行模式一致。

## 服务生命周期

```text
uv run python manage.py status
uv run python manage.py start
uv run python manage.py stop
uv run python manage.py restart
```

可在命令后指定一个或多个服务，避免重启整个系统：

```text
uv run python manage.py start sync market market-live
uv run python manage.py restart auth
uv run python manage.py status auth web api webserver sync market market-live
```

| 服务名 | 作用 | 地址或配置 |
|---|---|---|
| `web` | 已构建前端 | `http://127.0.0.1:8888` |
| `auth` | 登录、代理及数据库查询 | `http://127.0.0.1:8890` |
| `api` | Freqtrade 交易 API | `127.0.0.1:8889`，`bot/user_data/config_trend_live.json` |
| `webserver` | Freqtrade 回测与下载 API | `127.0.0.1:8891`，`bot/user_data/config_trend_webserver.json` |
| `sync` | 文件与旧数据库增量接入 PostgreSQL | `data_sync.py --daemon` |
| `market` | 逐笔成交与盘口持续采集 | `market_stream.py` |
| `market-live` | 按页面选币及周期推送实时 K 线 | `127.0.0.1:8892`，`market_live.py` |

`start` 复用已运行的本项目服务。`stop` 在发信号前核验进程入口和实际工作目录，使用 `SIGTERM` 等待正常退出；如果进程超时，命令报告失败并保留进程。端口属于其他进程时，命令会中止，不会按端口强制终止服务。管理 PID 保存在 `logs/manage-*.pid`，日志为 `logs/{服务名}.log`。

前端的一键登录入口继续保留。`QUANT_STREAM_ENABLED=false` 会使启动命令跳过逐笔与盘口采集。采集交易对通过 `QUANT_STREAM_PAIRS` 指定，默认 BTC 与 ETH；通过 `QUANT_STREAM_MARKET` 选择 `futures` 或 `spot`。

行情图表实时连接独立于逐笔/盘口归档。`market-live` 根据所有打开图表的交易对及周期共享订阅交易所 K 线，不受 `QUANT_STREAM_PAIRS` 的 BTC/ETH 范围限制；没有页面订阅时关闭上游连接。`QUANT_LIVE_PORT` 可更改服务端口。`/api/locals/market-stream-info` 需登录，返回浏览器连接地址；本机默认 `ws://127.0.0.1:8892/ws/market`。外部 HTTPS 部署时配置 `QUANT_LIVE_WS_URL=wss://你的域名/行情路径`，由反向代理将该路径转发到本机 8892，并支持 WebSocket Upgrade。登录令牌在连接后的首条认证消息中发送，不放入 URL。

## 构建与发布前端

```text
uv run python manage.py build
```

该命令先执行 `pnpm typecheck`，再执行 `pnpm build:antd`。完整构建产物通过临时目录准备好后替换 `web/`，旧产物保存在 `logs/web-backups/`。目录替换失败时会恢复旧版本。运行时 API 默认写入 `http://127.0.0.1:8890/api`，可以通过 `API_URL` 覆盖。

## 入库、状态与测试

```text
uv run python manage.py sync
uv run python manage.py sync --fast
uv run python manage.py sync --force
uv run python manage.py db status
uv run python manage.py audit
uv run python manage.py audit --skip-frontend
uv run python manage.py shot market --wait 8
```

`sync` 默认单次执行，增量同步源文件、旧 SQLite 交易库与用户记录。`--fast` 只处理经常变化的业务快照；`--force` 强制重扫。服务模式 `start sync` 使用后台常驻同步。数据库状态命令调用 `DataStore.stats()`，不打印数据库密码；PostgreSQL 原生服务的安装及启动配置见数据库文档。

`audit` 只解析前后端运维与数据层 Python 入口、执行 `tests/` 下的 unittest，并检查前端类型。不会调用研究审计、模型训练或迭代程序。GitHub Actions 的后端测试使用 PostgreSQL 服务容器与 Python 3.12；前端任务独立安装、检查并构建 Vben。截图使用现有 Python `shot.py`，无需 shell 包装。

## 新机器初始化 PostgreSQL

安装 PostgreSQL 17 的 `initdb`、`pg_ctl` 与 `psql` 工具后，可执行：

```text
uv run python manage.py db init
uv run python manage.py db start
```

`db init` 创建本项目独立 cluster、应用角色、业务库和测试库，生成随机密码并写入私有 `.env`。已有 `.env` 或 cluster 时拒绝覆盖。默认仅监听本机 5433。macOS 可使用 Homebrew 的 postgresql@17；其他安装路径可通过 `QUANT_PG_BIN` 指定。`db stop` 只操作本项目 cluster。外部 PostgreSQL 由外部运维管理，在 `.env` 中配置连接串即可。
