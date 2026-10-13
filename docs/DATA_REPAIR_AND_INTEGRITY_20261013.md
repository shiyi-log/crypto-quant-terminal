# 数据落库与持续采集：缺口审计、修复与常驻自检（2026-10-13）

> 由 DSH 会话执行。本文记录**实测缺口 → 修复动作 → 修复后验证 → 常驻监控**，
> 每个数字都可由文中命令复现。所有写操作默认 dry-run，`--apply` 必须新建备份文件，
> 且只补缺失字段、绝不覆盖已存在的源值（唯一例外见 §3.7，有单列规则与备份）。

---

## 一、结论先行

| 项 | 修复前 | 修复后 |
|---|---|---|
| 5m `futures` 源字段覆盖（BTC/ETH/TRX） | TRX 缺失 292,242 行 flow_imbal；BTC/ETH 缺失 3,746 行订单流字段 | **源字段 100%**；flow_imbal 877,640/877,671（缺 31 = 零成交量根，数学上无定义） |
| 5m `orderflow` 序列 | **4 天空洞**（2026-10-08 23:40 → 2026-10-13 03:05，BTC/ETH 各缺 1,191~1,192 根） | **缺口 0 根**；三币序列连续 |
| `check_candle_integrity` 5m 审计 | `ok=false`，error_count **30**（含 6 个 gap 报错） | 覆盖审计 `ok=true`，error_count **0**；法证审计仅剩 2 项已知脏数据 |
| 常驻完整性自检 | 无（只能人工发现） | 受管服务 `integrity`，每 300 秒一次，写状态文档 |

**上游判定：数据确实在落库、采集也确实在跑**（tick/盘口/5m 实时无中断），
但**历史导入段缺订单流字段、orderflow 镜像序列断了 4 天、且没有任何机制会发现新缺口**——
这三项就是「数据落库和持续采集还没搞定」的实际内容。

---

## 二、修复前实测（2026-10-13 03:20 快照）

```
check_candle_integrity --pair BTC --pair ETH --pair TRX --timeframe 5m --max-age-bars 3
→ ok=false, error_count=30
  BTC/ETH futures: num_trades 等 4 字段 288,806/292,552 = 98.72%；flow_imbal 98.61%
  TRX   futures: flow_imbal 310/292,552 = 0.11%（源字段 99.85%）
  BTC/ETH orderflow: gap_count=1（1192/1191 根缺失）
  TRX   orderflow: 仅 3 行
```

补充实测：

```
TRX 5m futures   缺失 flow_imbal 292,242 行，其中 291,783 行可由库内源字段推导
BTC/ETH 5m futures 缺失 flow_imbal 4,076 行（325 可推导）+ num_trades 3,746 行
  其中 2024-01-01..01-12 共 3,356 行（早于 orderflow feather 起点 2024-01-12）
  其中 2026-10-08..10-11 共 390 行（REST 窗口内可补）
17 个非策略白名单币 5m futures 各缺 440 行订单流字段（2026-10-08..10-11）
orderflow 序列 last_ts 停在 2026-10-08 23:40（= 本地 orderflow feather 末行）
  BTC/ETH 各缺 1,191~1,192 根；TRX 从未有过 5m orderflow 序列
```

**为什么 data_sync 没自动补上**：`data_sync` 在 2026-10-13 03:05 才加入 `flow_imbal`
推导，而它只处理**指纹变化过**的来源文件；这些历史文件早已导入完成，因此长期为
`files: 0, rows: 0, skipped: 1438`，不会回头修已入库的行。

---

## 三、修复动作（全部可回滚）

备份与报告目录：`artifacts/data-repair-20261013/`

| # | 动作 | 影响行数 | 备份文件 |
|---|---|---|---|
| 3.1 | 由库内源字段推导 flow_imbal/flow_imbal_base/flow_imbal_q | 391,475 | `backup-derived-flow-fields-20261013.jsonl`（235 MB） |
| 3.2 | REST 补 BTC/ETH/TRX 的 5m/15m/1h 源字段 | 8,330 | `backup-rest-flow-fields-20261013.jsonl` |
| 3.3 | 再次推导（3.2 新增的源字段需要派生字段） | 8,330 | `backup-derived-flow-fields-pass2-20261013.jsonl` |
| 3.4 | 镜像补 `orderflow` 序列（仅补缺失时间戳） | 3,576 行**插入** | `backup-orderflow-mirror-20261013.jsonl` |
| 3.5 | 推导 `orderflow` 序列（同上，全 candle_type 复扫） | 1,170 | `backup-derived-flow-fields-pass3-20261013.jsonl` |
| 3.6 | REST 补其余 17 个币的 5m 源字段 | 7,480 | `backup-rest-allpairs-20261013.jsonl` |
| 3.7 | 官方月度归档补 2024-01（BTC/ETH 5m futures） | 3,356 / 3,357 | `backup-archive-2024-01-{BTC,ETH}-20261013.jsonl` |
| 3.8 | 修正「用了另一种口径」的派生字段 | 13 + 4 | `backup-mislabeled-imbalance*.jsonl` |

### 3.4 镜像的边界

只镜像 **2026-10-08T15:40Z 之后**缺失的时间戳（`--since`）。
`market_stream.py` 对每根已收盘 5m K 线**同时写** `candle_type='futures'` 与
`'orderflow'`（5675b8a 引入），所以镜像行与实时写入的行逐字段一致，不含任何编造。
**没有**镜像 2024-01-01..01-12 的 orderflow 历史：那段 `futures` 行本身也没有订单流
字段，镜像只会制造「有行、无字段」的假完整性。

### 3.8 修正规则（单列、可证）

只有当某派生字段**既不符合自己的公式、又精确匹配兄弟口径**时才改写：

```python
not isclose(stored, own, rel_tol=1e-6) and isclose(stored, other, rel_tol=1e-2, abs_tol=1e-3)
```

例：`volume=405.68, taker_buy_base=76.851` → base 口径 `-0.6211`，
而 `flow_imbal_base` 存的是 quote 口径 `0.1353` → 判定为错标并改回 `-0.6211`。
两个口径重合、或两个都不匹配的行**一律不动**。

---

## 四、修复后验证

```
# 覆盖/缺口/新鲜度（常驻自检用的同一命令）
check_candle_integrity --pair BTC --pair ETH --pair TRX --timeframe 5m \
  --max-age-bars 3 --no-formula-check
→ ok=true, error_count=0, 6 个数据集全部 gap=0, tail_passes=true

# 法证模式（含派生公式对账）
check_candle_integrity ... --max-age-bars 3
→ error_count=2（仅剩 §6.1 的 6 行已知脏数据）
```

| 数据集 | 行数 | gap | 源字段覆盖 | flow_imbal |
|---|---:|---:|---:|---:|
| BTC/ETH/TRX 5m futures | 877,671 | 0 | **100%** | 877,640（缺 31 = 零成交量根） |
| BTC/ETH/TRX 5m orderflow | 579,602 | 0 | **100%** | **579,602（100%）** |

报告文件：`candle-integrity-before-5m.json` / `candle-integrity-final-5m.json`。

---

## 五、新增工具与常驻自检

| 文件 | 作用 |
|---|---|
| `tools/backfill_candle_flow_fields.py` | 缺失订单流字段回填：`derived`（库内推导）/`rest`（Binance REST 已收盘）/`archive`（官方月度归档）。dry-run 默认，`--apply` 必须 `--backup`，只补 null |
| `tools/mirror_orderflow_candles.py` | 由 `futures` 补齐 `orderflow` 序列的**缺失时间戳**，仅插入、可 `--since` 限定；备份即回滚键清单 |
| `tools/check_candle_integrity.py`（扩展） | 新增 `--watch/--interval/--status-document/--max-cycles`、`--no-formula-check`、零分母根不参与派生字段分母、自动加载项目 `.env` |
| `manage.py` 受管服务 `integrity` | `--watch --interval 300`，只读，写 `database/runtime/candle_integrity_status.json` 与 `candle_integrity_report.json` |

```bash
uv run python manage.py start integrity     # 常驻自检
uv run python manage.py status integrity
cat database/runtime/candle_integrity_status.json
```

自检的保证（每 300 秒）：**时间戳零空洞 + 尾部新鲜（≤3 根）+ 源字段 100% 覆盖**。
`--no-formula-check` 是刻意的：法证对账会因 §6.1 的历史脏数据长期为红，
而采集断流的告警不能被长期红的噪声淹没；法证对账单独按需运行。

---

## 六、已知残留（未修复，需用户裁定）

### 6.1 `taker_buy_base > volume` 的 6 行（法证审计仍报错）

BTC/ETH 5m futures 各 3 行（2024-10-29、2025-01-14、2025-01-29 等），
源字段自相矛盾（买入量大于总成交量），派生 base 口径会超出 `[-1, 1]`，工具
**拒绝编造**，因此 `flow_imbal_base` 保持原值。审计报 `flow_imbal_base_formula`。

### 6.2 零成交量根 31 行

`quote_volume = 0` 时 `2×taker/quote−1` 是 `0/0`，字段按定义不存在。
审计已把这类根移出派生字段分母并单列 `undefined` 计数，源字段覆盖仍要求 100%。

### 6.3 原生 15m/1h 与现货 5m 不是连续采集

| 数据集 | 最后写入 | 原因 |
|---|---|---|
| BTC/ETH/TRX 15m futures | 2026-10-12 18:42 | 来源是 feather 文件导入，文件自 10-08 未再更新 |
| BTC/ETH/TRX 1h futures | 2026-10-12 18:42 | 同上 |
| 现货 5m | 2026-10-12 18:48 | 同上（现货 1h 由 `spot-basis` 持续喂数，正常） |

策略读取端对原生周期不足的窗口**回落到由 5m 重采样**（`strategy_data.load_spec_candles`
/ `resample_closed_candles`），因此不阻塞决策；但 DB 里这三类序列**不会自动前进**，
本次以 REST 补齐了它们最近 1,500 根的**字段**（§3.2），**没有**插入缺失的 15m/1h 行
（35 根 15m、9 根 1h 每币），因为那需要先决定「原生周期是否由 5m 滚动生成」这一
设计问题。**建议**：若要它们连续，正确做法是让采集器在 15m/1h 边界落库，
而不是事后用 REST 补洞。

---

## 七、回滚方法

```bash
# 3.1/3.3/3.5/3.8：按 JSONL 里保存的原始 extras 逐行还原
# 3.2/3.6/3.7：同结构，仅 extras
python - <<'PY'
import json
# 逐行 UPDATE candles SET extras=<原值> WHERE ...（键在每行内）
PY

# 3.4：镜像插入的键在备份里，删除即可
# 每行 type=inserted，含 exchange/market/pair/timeframe/candle_type/timestamp
```

`.gitignore` 已排除 `artifacts/`，这些备份与报告只保留在本机。

---

## 八、复现命令

```bash
set -a && . ./.env && set +a

# 覆盖审计（常驻自检同一命令）
.venv/bin/python tools/check_candle_integrity.py \
  --pair BTC/USDT:USDT --pair ETH/USDT:USDT --pair TRX/USDT:USDT \
  --timeframe 5m --max-age-bars 3 --no-formula-check

# 法证审计（含派生公式对账）
.venv/bin/python tools/check_candle_integrity.py \
  --pair BTC/USDT:USDT --pair ETH/USDT:USDT --pair TRX/USDT:USDT \
  --timeframe 5m --max-age-bars 3

# 回填工具（默认 dry-run；--apply 必须 --backup）
.venv/bin/python tools/backfill_candle_flow_fields.py --source derived --timeframe 5m
.venv/bin/python tools/backfill_candle_flow_fields.py --source rest --window 1500 --timeframe 5m
.venv/bin/python tools/mirror_orderflow_candles.py --timeframe 5m --since 2026-10-08T15:40:00Z

# 新工具测试
.venv/bin/python -m unittest tests.test_candle_flow_backfill tests.test_candle_integrity -v
```
