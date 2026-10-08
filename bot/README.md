# Crypto Quant Bot —— 项目说明

基于 **Freqtrade + FreqAI** 的加密货币量化交易系统。
**币安负责研究回测，Bybit 负责实盘**，现货与合约双市场配置。

---

## 目录结构

```
量化/
├── crypto-quant-开源项目调研.md     # 开源项目选型（含真实星数/活跃度）
├── crypto-quant-AI接入方案.md       # AI 模型接入三条路线与推荐架构
├── 交易所适配清单.md                # 币安/Bybit/OKX 硬约束与实盘检查清单
└── bot/
    ├── .venv/                      # Python 3.12 虚拟环境（uv 管理）
    ├── user_data/                  # Freqtrade 工作目录
    │   ├── config_*.json           # 各交易所配置
    │   ├── strategies/             # 策略代码
    │   ├── freqaimodels/           # 自定义 AI 模型（继承 IFreqaiModel）
    │   ├── data/                   # 历史行情数据
    │   ├── models/                 # FreqAI 训练产物
    │   ├── backtest_results/       # 回测结果
    │   └── logs/
    └── README.md                   # 本文件
```

---

## 环境

| 项 | 值 |
|---|---|
| 机器 | MacBook Air, Apple M5, 10 核, **32 GB 统一内存**, arm64 |
| Python | **3.12.14**（uv 管理，不用系统 3.9.6） |
| 包管理 | uv |
| TA-Lib | PyPI 预编译 arm64 wheel（**无需 brew 编译**） |

> ⚠️ **重要：所有 GPU（MPS）相关工作必须在原生环境跑，不要在 Docker 里跑。**
> Docker 的 Linux 容器无法访问 Apple Metal/MPS。Docker 只用来跑数据服务或 Linux 专属依赖。

---

## 激活环境

```bash
cd /Users/shiyi/DeepSeek/量化/bot
source .venv/bin/activate
freqtrade --version
```

---

## 双交易所分工（币安 + OKX 优先）

| 用途 | 交易所 | 原因 |
|---|---|---|
| 研究 / 长历史回测 / FreqAI 训练 | **币安** | 合约数据最完整（candles + mark + funding_rate） |
| 现货实盘 | **币安 + OKX** | 两家现货均无硬约束 |
| 合约实盘 | **币安优先** | ⚠️ OKX 的 mark K 线仅约 3 个月，合约策略验证窗口太短 |
| Bybit（备选） | 保留 | Demo Trading 适合作为上线前最后一关验证 |

> ⚠️ **OKX 合约限制**：mark K 线只有约 3 个月，更早的回测无法正确计算资金费率。
> 推荐做法是「币安做长历史研究 → 验证通过后迁移到 OKX 实盘」。

### ⚠️ 许可证提醒

**Freqtrade 是 GPL-3.0。** 若本项目要以 MIT / Apache-2.0 开源发布，需改用
Jesse (MIT) 或 NautilusTrader (LGPL-3.0)。详见 `../开源许可证选型.md`。

---

## 常用命令

```bash
# 拉取币安数据（现货 + 合约，含资金费率与未平仓量）
freqtrade download-data --config user_data/config_binance_futures.json \
  --pairs BTC/USDT:USDT ETH/USDT:USDT --timeframes 5m 1h \
  --trading-mode futures --timerange 20240101-

# 回测
freqtrade backtesting --config user_data/config_binance_futures.json \
  --strategy MyStrategy --timerange 20250101-

# 前视偏差检查（必做）
freqtrade lookahead-analysis --config user_data/config_binance_futures.json \
  --strategy MyStrategy

# 参数优化
freqtrade hyperopt --config user_data/config_binance_futures.json \
  --strategy MyStrategy --hyperopt-loss SharpeHyperOptLoss --spaces buy sell

# 干跑（实盘前必须）
freqtrade trade --config user_data/config_bybit_futures.json --strategy MyStrategy --dry-run
```

## ✅ 环境验证记录（2026-10-08 实测通过）

| 验证项 | 命令 | 结果 |
|---|---|---|
| 版本 | `freqtrade --version` | freqtrade **2026.9** / CCXT 4.5.85 / Python 3.12.14 |
| 数据下载 | `freqtrade download-data` | 现货 BTC/ETH/SOL 各 291,257 根 5m；合约含 mark + funding_rate 各 3,034 条 |
| 现货回测 | `backtesting` | 880 笔成交，管线正常 |
| 合约回测 | `backtesting --trading-mode futures` | 545 笔成交，**资金费率数据被正确读取** |
| 前视偏差检查 | `lookahead-analysis` | **no bias detected** ✅ |
| FreqAI 训练 | `--freqaimodel LightGBMRegressor` | **训练 + 预测 + 回测全通**，落盘 11 个模型产物，含 1 多 5 空（做空正常） |
| Apple MPS | 2000×2000 matmul ×10 | **0.23 秒**，GPU 可用 |

### FreqAI 可用模型（本机实测 15 个全部 OK）

```
LightGBM{Classifier, Regressor, ClassifierMultiTarget, RegressorMultiTarget}
XGBoost{Classifier, Regressor, RFClassifier, RFRegressor, RegressorMultiTarget}
PyTorchMLP{Classifier, Regressor}   PyTorchTransformerRegressor
ReinforcementLearner, ReinforcementLearner_multiproc
SKLearnRandomForestClassifier
```

> ⚠️ **CatBoost 不可用**：官方不提供 arm64 wheel，Apple Silicon 上装不了。用 LightGBM 替代即可。

### 🐛 踩坑记录（苹果芯片专属）

1. **LightGBM / XGBoost 装完直接导入失败**
   - 报错：`Library not loaded: @rpath/libomp.dylib`
   - 原因：macOS 不带 OpenMP 运行时
   - 修复：`brew install libomp`（**必须**，否则 FreqAI 主力模型全挂）
2. **TA-Lib 不需要 brew 编译**：PyPI 有 `ta_lib-0.8.1-cp312-macosx_14_0_arm64.whl`，直接装。
3. **macOS 没有 GNU `timeout`**：脚本里别用 `timeout 900 freqtrade ...`，会 `command not found`。
4. **不要用 Docker 跑 GPU 任务**：Linux 容器访问不到 Apple Metal/MPS。

### FreqAI 必须显式指定模型

```bash
freqtrade backtesting --config user_data/config_binance_futures_freqai.json \
  --strategy FreqaiExampleStrategy --freqaimodel LightGBMRegressor \
  --timerange 20250901-20251001
```
不加 `--freqaimodel` 会报 `No freqaimodel set`。

---

## 安全红线

1. API Key **只开必要权限 + 绑定 IP 白名单**，绝不用主账户，**一个 bot 一个子账户**。
2. 配置文件里**不要明文写 Key**——用环境变量 `FREQTRADE__EXCHANGE__KEY` / `__SECRET` / `__PASSWORD`。
3. `user_data/config*.json` 已加入 `.gitignore`，永远不要提交密钥。
4. **上实盘前必须**：样本外验证 → `lookahead-analysis` 无告警 → dry-run 或 Bybit Demo 跑满 2 周以上。
