# 重构页面浏览器 CI

这组 Playwright 用例运行 `web-antd` 生产构建中的真实 Vue 应用，覆盖交易台与验收页。测试不注入仿制页面，也不启动、停止或占用本地服务端口。浏览器的每条 HTTP 请求都由夹具从已构建的 `dist` 文件返回、生成合成 JSON 或拒绝；没有请求转发到真实后端。

## 执行

需要仓库声明的 Node.js、pnpm 和 `@vben/web-antd` 的直接开发依赖 `@playwright/test`。依赖版本由 `vben/pnpm-lock.yaml` 固定；使用同一包的 Playwright 命令安装浏览器，避免根目录另一个 Playwright 版本造成浏览器版本错配。

从仓库根目录运行：

```sh
pnpm --dir vben install --frozen-lockfile
pnpm --dir vben/apps/web-antd exec playwright install chromium --with-deps
pnpm --dir vben --filter @vben/web-antd build
pnpm --dir vben/apps/web-antd run test:e2e
```

本机若使用已安装的 Google Chrome：

```sh
PLAYWRIGHT_CHANNEL=chrome pnpm --dir vben/apps/web-antd run test:e2e
```

默认 URL `http://quant-e2e.invalid` 只提供浏览器中的同源路由上下文，没有 DNS 或 HTTP 网络访问。`QUANT_E2E_BASE_URL` 可以改变该上下文，夹具仍直接读取本机 `dist`，不会访问 URL 对应的服务。缺少 `dist/index.html` 时测试立即报错；源码变化后须重新生产构建。

## 验证范围

- 在 1920、1440、768 和 390 像素宽度进入真实交易台与验收页，检查页面没有文档级横向溢出，并保存完整截图。表格自身保留横向滚动。
- 通过实际登录组件和路由守卫完成自动登录，使用固定假令牌；检查默认首页是交易台。合成账户名称始终显示“合成测试”。不读取真实用户名、密码、Keychain 或交易所凭据。
- 检查长 Decimal 权益字符串原样显示；未知权益和实际配置仍显示未知。
- 用 503 模拟读取失败，检查交易台权益、验收页会话和待裁定状态保留。
- 在刷新期间挂起一条旧会话账户响应，再把状态切到新会话后释放；检查整组旧响应被丢弃、旧值保留，下次刷新才显示新会话事实。
- 账户订单、成交及验收证据请求固定到读取开始时的会话。只允许合成登录 POST；任何其它写请求、未定义 API、同源 fetch/XHR 或 WebSocket 都会被阻断并使断言失败。外部脚本、字体与图片被阻断。

共享布局仍含旧的交易控制组件。本组验证的只读范围是页面进入和刷新所产生的请求；它不把按钮存在本身解释为交易执行，也不点击真实交易命令。共享布局的配置、持仓计数和 WebSocket 令牌接口同样使用合成响应。

## CI 产物与限制

测试截图、失败 trace 和截图保存在仓库 `output/playwright/refactor-ci/`，HTML 报告保存在 `output/playwright/refactor-ci-report/`。CI 可用 `if: always()` 上传这两个目录。测试使用单 worker、零自动重试，避免失败被重试掩盖。

这些结果证明当前编译页面对合成输入的显示、路由、会话一致性及读取失败行为。它们不证明真实交易所连接、订单执行、原生条件单保护、真实资金安全或 30 天验收通过。验收夹具明确保留手续费和资金证据缺失、人工复核和待裁定状态。
