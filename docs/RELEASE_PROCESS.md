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

当前 CI 包含隔离 PostgreSQL 17 全后端测试、冻结身份硬门禁、量化/API/认证
Vitest、TypeScript 类型检查及生产构建。关键端到端浏览器流程尚未纳入 CI；
本机合成夹具的截图仅用于视觉复核，不能替代真实交易或审批验收。
