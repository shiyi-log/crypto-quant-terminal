# Crypto Quant Terminal

基于 Freqtrade 与 Vue Vben Admin 的加密资产量化终端，包含行情图表、持仓和成交、回测任务、研究结果展示与运维页面。

## 项目结构

| 目录 / 文件 | 用途 |
| --- | --- |
| `vben/apps/web-antd/` | Vue 前端与量化页面 |
| `auth_service.py` | 登录、权限校验、Freqtrade API 代理与本地数据接口 |
| `bot/` | 现有策略、数据采集和研究代码 |
| `database/` | 统一数据库结构与运行配置 |
| `docs/DEVELOPMENT_LEDGER.md` | 开发任务、变更、验证与交接记录 |
| `manage.py` | Python 本地运维入口，由 uv 管理运行环境 |

当前开发职责为前端、后端与数据库。模型训练、模型自动迭代及策略决策由独立流程负责；数据层只接入其产物，不修改训练或上线行为。

## 数据架构

统一业务数据层使用 PostgreSQL，支持历史行情、逐笔成交、盘口、研究结果、任务进度与用户数据。Freqtrade 原有交易 SQLite 由交易引擎维护，数据层通过只读同步接入。

历史数据主要来自 Binance，补充数据来自 OKX 与 Deribit。运行中的模型及研究脚本继续生成现有文件，由增量同步服务写入统一数据库。具体建设状态见开发台账。

## 本地运行

仓库不包含交易所凭据、用户库、行情、交易数据库、训练缓存或模型权重。项目使用 Python 3.12 与 uv，前端使用仓库指定的 Node.js 与 pnpm。

```text
uv sync --locked
pnpm --dir vben install --frozen-lockfile
uv run python manage.py init-config
uv run python manage.py db init
uv run python manage.py db start
uv run python manage.py sync
uv run python manage.py build
uv run python manage.py start
```

数据库初始化使用本机 PostgreSQL 工具，生成的凭据保存在忽略的 `.env` 中。远程数据库请按 `.env.example` 配置。新机器还需 `uv sync --locked --extra trading` 安装交易引擎。配置模板默认模拟盘；一键登录入口保留。

运维说明见 [docs/OPERATIONS.md](docs/OPERATIONS.md)，数据接入见 [docs/DATA_SYNC.md](docs/DATA_SYNC.md)，采集范围和限制见 [docs/MARKET_STREAM.md](docs/MARKET_STREAM.md)。CI 在每次推送和 PR 时运行后端 PostgreSQL 集成测试及前端类型检查、构建。

默认前端端口 8888，认证代理 8890，Freqtrade 8889，研究服务 8891。公网部署前应自行配置认证、网络隔离与交易所权限。

## 许可证与上游

根目录保留项目现有 GNU GPL v3 许可证；Vue Vben Admin 保留其 MIT 许可证。上游来源及版本记录见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
