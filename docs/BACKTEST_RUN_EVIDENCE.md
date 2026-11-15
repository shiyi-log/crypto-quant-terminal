# 回测运行与结果证据

2026-11-16：包装器将子进程退出、结果验证和研究判定分开处理。子进程退出码为零时，还必须找到本轮唯一 ZIP，验证策略、模型、有效 FreqAI identifier、请求时间区间及有限数值，完成归档后才写入 `status=done` / `execution_status=succeeded`。零交易是有效执行结果，后续研究门槛仍可判为样本不足。

从仓库根目录执行：

```bash
.venv/bin/python bot/run_backtest_task.py \
  --config bot/user_data/config_freqai_smoke.json \
  --strategy FreqaiExampleStrategy --freqaimodel LightGBMRegressor \
  --timerange 20240101-20261023 --run-id research-example-001 \
  --progress /absolute/path/to/research-example-001-progress.json
```

每个 `run_id` 只能创建一次。省略时自动生成时间加随机后缀。结果位于配置的 `user_data_dir/backtest_results/runs/<run_id>/`：

- `config.json`：私有有效配置快照，合并 include、环境变量与 CLI，文件权限 0600。运行目录权限 0700；不要通过 HTTP 直接公开此文件。
- 缓存复用时 `config.json` 代表本轮请求配置，`execution_config.json` 保存原结果实际使用的配置；manifest 的 `config_role` 和配置哈希区分两者。
- `inputs.json`：市场数据文件、策略/模型/执行源码和已安装包的内容指纹清单。
- 引擎生成的 ZIP：本次原始统计、配置和策略来源。
- `result.json`：既有面板指标口径的摘要与证据引用。
- `manifest.json`：仅成功结果生成的运行身份、输入指纹和结果哈希。
- `progress.json`：该轮最后状态；外部 `--progress` 也是同一状态视图。

包装器显式设置 `--backtest-directory`、`--datadir` 和 `--cache none`。当前 Freqtrade 已忽略自定义回测文件名，不能依靠 `--backtest-filename` 隔离任务。FreqAI 模型和预测另有缓存，因此每次重新计算使用独立有效 identifier `<请求identifier>-<run_id>`，避免配置或数据改变后沿用旧预测。请求 identifier 保留供旧模型摘要接口使用。

可通过 `--reuse-run /absolute/path/to/previous/run-directory` 显式请求缓存复用。只有成功 manifest schema、结果哈希、配置与输入清单文件哈希，以及完整配置、数据、源码、依赖指纹一致才复用，并记录原运行来源。无法验证时重新计算，进度中写入 `cache_rejection`。公共结果目录中的旧 ZIP 不作为可信缓存。

输入指纹覆盖所选交易所的整个数据目录，包括关联周期、mark 与 funding，运行前后重新核验。训练期间这些输入或源码发生变化，本轮明确失败，保留原始产物供诊断。它不提供数据写入冻结；稳定长任务应使用已冻结的数据目录。模型反向回测 `freqai_backtest_live_models=true` 不属于本包装器的独立训练语义，会明确拒绝。

`models/<请求identifier>/backtest_result.json` 保留为兼容的最新摘要视图，不是不可变事实来源。结果解析失败、结果身份不符、没有结果、归档失败或非零退出均返回非零，不得仅凭训练进度达到 100% 判断成功。

当前安装的 FreqAI 在某轮模型训练抛异常时可能记录 `Training ... raised exception ..., skipping.`，随后用中性预测继续运行并返回零。包装器识别该日志信号，将其记录为 `model_training_failed`，与合法零交易分开处理，不发布成功 manifest。

验证入口为 `tests/test_run_backtest_task.py`。测试替换外部 Freqtrade 子进程，真实执行结果 ZIP、进度、配置和归档的读写；完整模型训练及可选 FreqAI 环境应在独立训练验收中执行。
