# 迭代前端实施状态

当前阶段：主代理已运行 RED，缺失模块导入失败符合预期；生产模块与组件已写，等待主代理串行 GREEN 和类型检查。

独立复核后新增规范指标与北京时间用例，主代理实测 **2 failed、10 passed**；两处实现已修正，再次等待最终 GREEN。证据：`/private/tmp/quant-frontend-boundary-output.txt`。

命令（工作目录 `vben/`）：

```sh
pnpm exec vitest run apps/web-antd/src/views/quant/iteration/__tests__/runDisplay.test.ts
```

已观测 RED：`../runDisplay` 尚未存在。测试覆盖执行与研究状态区分、零值与未知指标、保留最近成功快照、无效列表、空列表与错误、刷新并发，以及详情响应串轮和乱序。

已新增 `IterationRuns.vue` 独立组件、`runDisplay.ts` 展示/刷新状态逻辑和 typed API client，插入原迭代页面；旧研究内容保持可查看。尚未构建或发布运行产物。

API 依据：`tests/test_iterations_api.py` 已定义 `items/total`、`run_id`、`execution_status`、`research_verdict`；其余证据字段待后端最终对齐。

当前已对齐后端的 `model`、`finished_at`、`timerange`、`progress` 和 `result_source`。证据原样作为结构化文本展示，不自动拼接任意链接；缺失值显示未记录。
