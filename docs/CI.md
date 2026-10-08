# CI 测试流

仓库根目录的 `.github/workflows/ci.yml` 在每次推送、Pull Request 和手动触发时运行。后端、前端两个任务并发执行，同一分支的新提交会取消尚未结束的旧测试。工作流只有源码读取权限，不部署，也不调用交易所 API 或模型迭代脚本。

## 后端

- Python 使用 3.12，由 uv 管理；先检查 `uv.lock`，再按锁文件安装默认依赖和 `dev` 依赖组。
- 编译根目录的认证、静态服务、Python 管理入口、数据库及采集代码，对新增后端执行 Ruff 静态检查，并运行 `tests/` 下的全部 unittest。
- 提供一次性 PostgreSQL 17 服务。`QUANT_TEST_DATABASE_URL` 只指向 CI 容器，公开测试账户不对应任何真实账户；集成测试使用独立随机 schema，结束后删除自身 schema。
- 数据库连接失败、未发现 `DataStoreIntegrationTests` 或集成测试被跳过都视为失败，避免数据库代码没有实际测试却通过 CI。
- 认证测试使用临时目录存放签名密钥，用户存储使用桩对象，不读取本机账户。覆盖用户要求保留的本机免密登录入口、令牌响应以及非本机请求拒绝。

本地检查命令：

```text
uv lock --check
uv sync --locked --group dev
uv run --frozen --group dev ruff check
uv run --frozen --group dev python -m unittest discover -s tests -v
```

没有 `QUANT_TEST_DATABASE_URL` 时，本地数据库集成测试会明确显示跳过。需要完整验证时，把它设置为专用测试数据库的连接地址；不要使用承载真实业务数据的数据库。CI 已提供此变量并强制执行集成测试。

## 前端

Node.js 版本取自 `vben/.node-version`，pnpm 版本取自 `vben/package.json` 的 `packageManager`，避免 CI 与项目声明不一致。依赖使用 `vben/pnpm-lock.yaml` 锁定；构建前将公开的 `.env.example` 复制为应用 `.env`。

```text
pnpm --dir vben install --frozen-lockfile
pnpm --dir vben/apps/web-antd typecheck
pnpm --dir vben build:antd
```

CI 构建仅验证产物能生成；运行中的本机前端发布仍由 Python 管理入口负责。

## 依赖与工作流维护

GitHub Actions 固定到已核验的完整 commit SHA，并在旁边标明版本。升级 Python 或 Node 时同步更新项目版本声明与锁文件，再确认两个任务都通过。工作流安装方式参考 [Astral setup-uv](https://github.com/astral-sh/setup-uv)、[GitHub setup-node](https://github.com/actions/setup-node) 和 [pnpm action-setup](https://github.com/pnpm/action-setup) 的官方文档。
