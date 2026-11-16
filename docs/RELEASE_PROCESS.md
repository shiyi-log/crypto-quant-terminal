# 代码归档、版本与发布

代码发布与交易批准分别验证。发布包不会创建策略上线、资金升级或异常恢复
批准，也不能解除账户风险锁。正式实盘仍遵循重构计划的人工批准。

## 发布顺序

1. 按小节完成测试、中文台账与独立中文提交，推送 main。
2. 项目版本以 `pyproject.toml` 为唯一权威，同步 `uv.lock`；变更记录写入
   `docs/CHANGELOG.md`。版本必须为 X.Y.Z。
3. 等待待交付精确 SHA 的 push CI 成功。main 成功会自动产生源码/前端归档。
4. 在已通过的同一提交创建唯一的附注标签 `vX.Y.Z` 并推送。标签与项目版本
   必须完全相同；不能在同一提交增加多个发布标签。
5. 显式触发 CD 的 `workflow_dispatch`，输入 `ref=vX.Y.Z`。工作流只接受 main
   或版本标签，且提交必须属于 origin/main 历史，并有同 SHA 的成功 push CI。
6. CD 重新构建，在 build.json 记录 commit/version/package；归档与 SHA-256
   作为 Actions 产物。独立 Release job 重新核验远端标签、包名、校验和和
   build.json 身份，然后创建 GitHub Release。已有 Release 不覆盖。

main 成功事件只自动打包。标签在 main CI 成功之后才创建，因此必须执行第
5 步的标签发布调度；不能把 main 的成功归档当成已经有 Release。

## 完成证据

交付记录需包含版本、提交、标签、精确 SHA 的 CI、CD、Release 和归档校验和。
只有 CI/CD 结束并确认 Release 存在后才报告发布完成。数据库变更同时需独立
备份恢复证据；代码回滚保留新的成交、风险和审批事实。

当前 CI 包含隔离 PostgreSQL 17 全后端测试、冻结身份硬门禁、全前端
Vitest、TypeScript 类型检查、生产构建和锁定版本 Chromium 的 Playwright
流程。浏览器夹具读取真实 dist，拦截全部 API 和外部请求，覆盖四种视口、
读取失败、未知状态、迟到响应、认证恢复入口及环境栏滚动保持。截图与失败
证据作为 Actions 产物保存，不能替代真实账户、交易或审批验收。

2026-11-17 已核对提交 `7f36e395b066d6dd6b52cec6d0332d8081f0f9e0` 的
[CI](https://github.com/shiyi-log/crypto-quant-terminal/actions/runs/43940765567)
和 [main 归档 CD](https://github.com/shiyi-log/crypto-quant-terminal/actions/runs/43941792615)
成功。该提交仍为 0.2.1 之后的开发代码，没有新的版本标签；main 归档不构成
新的 GitHub Release。后续发布必须重新核对实际交付 SHA。
