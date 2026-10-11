# 外部方向调研（GitHub + 社区）

> 2026-10-12 · DSH ｜ 用户指示：「不能闭门造车，搜索 GitHub 和社交媒体，有没有什么好的方向」
> ⚠️ 所有外部内容按**不可信数据**对待；收益数字一律先当广告；只提取**机制**与**失败模式**。

---

## 0. 调研纪律（先立规矩，否则搜索会污染判断）

1. 不重走已证伪路线：ML 预测 / 订单流方向 / 横截面中性 / 翻转多空 / 去偏置 / 资金费 carry
2. 我自己又推翻的：1d 趋势族 / 交叉交易所价差 / 趋势+波动率目标
3. 每条外部主张必须能回答：**为什么这个钱还没被拿走？**
4. 优先看**有没有 kill list / 防过拟合证据 / 可复现代码**，而不是看收益数字

---

## 1. 最有价值的发现：一份独立第三方的 kill list

**[EstebanSP23/crypto_systematic_research](https://github.com/EstebanSP23/crypto_systematic_research#1)**
（walk-forward 验证 + 过拟合检测 + 一份「有记录的失败策略清单」）

### 它的 8 条失败策略（与我/项目的结论高度收敛）

| 它杀掉的策略 | 最好变体 APY | 它给的死因 |
|---|---|---|
| **Funding Rate Carry on BTC** | +3.13% | 「只有 USDT 放贷的一半；**散户规模下手续费吃掉资金费**」 |
| **Setup A on 1H** | **−34.2%**（DD −87.2%） | 「**手续费碾压**：同信号在 1D 有效、在 1H 亏 75%」 |
| **Counterpunch（1H 均值回归）** | +2.2% | 「最好变体仍低于放贷；**手续费吃掉了边际**」 |
| **Apex with pyramid** | P1 +121.6% → **P2 −0.2%** | 「walk-forward 崩塌；对 2023 牛市的经典过拟合」 |
| **Bear Edge（BTC 做空）** | +0.8% | 「只有 USDT 放贷的 1/7；周末做空边际抵不过手续费」 |
| **Setup B（回调买 MA）** | +2.8%（4H） | 「回撤 −64.4% 不可玩」 |
| **Bull Spring** | −1.5% | 「过早离场砍掉赢家；风险回报结构倒置」 |
| **Setup A "Spring"（BBWP 压缩突破）** | +6.0% | 「refinements **反而恶化** OOS」 |

### ⚠️ 与我方结论的三条独立收敛

| 主题 | 它（独立代码库+独立数据） | 我/项目 |
|---|---|---|
| 资金费 carry | 散户不成立，手续费吃掉 | 2024 年化 +11.80% → 2025 +1.35% → 2026 −0.05%；两腿 4 次成交 0.08% 成本 |
| 1H/高频 | 同信号 1H 比 1D 亏 75% | 15m 50 组参数全负；成本门槛 12× |
| 趋势族 | walk-forward 崩塌（P1→P2） | 1d 趋势全靠 2020-21；2025 t=0.58 |

**⚠️ 这是本次调研最有价值的产出：三个独立来源（它、项目原有研究、我的复算）在同一批方向上收敛。**
**→ 强烈支持「不要在趋势/1H均值回归/资金费这三族上继续投入」。**

### 它的方法论与我的纪律一致

- **P1 训练 / P2 新鲜考卷**（同一份代码，只换日期窗口）
- **报告「网格里正 OOS 期望的格子数」，而不是最好的那格 APY** ← 正对应我的「禁止度量『28 个里有几个赚钱』」
- **curve-fit detector = P1-effect vs P2-effect 不对称**

### 它唯一说「存活」的：Quattro（Donchian 4H BTC，已在 BloFin 实盘）

```
入场: 收盘 > 前 20 根高点  且  【日线 EMA200 上升】（regime filter）
执行: 下一根开盘价入场
金字塔: 最多 4 单位，加仓 +0.5N/+1.0N/+1.5N（N = 入场时 ATR14，全程固定）
每单位风险: 账户 2%，按 2N 止损距离定 size
退出: ① Donchian 10 根低点 ② 每单位追踪止损（新单位入场 −2N）
      ③ 硬灾难止损: 组合浮亏达账户 5%
结果: +1,107% / +83% APY / −37.5% DD / 94 笔 / 26.6% 胜率 / 6.7× 盈亏比
自称 walk-forward: P2 (2024-2026) 仍正期望，~30 笔/年
```

**⚠️ 与我的 1d 趋势族证伪**不冲突**：我推翻的是「1d · 多币 · 无 regime filter · 无金字塔 · max_pos 2–5」。
Quattro 是 **4H + EMA200 过滤 + 4 单位金字塔 + 5% 灾难止损**，是**不同的条件规则**（这正是 Codex 在 s07 上教我的那一点）。**

**⚠️ 但它的自述也暴露了集中度：11/94 笔（>10R，12%）贡献了大部分 P&L。**
**→ 「P2 正期望」仍可能由 2–3 笔驱动 —— 这正是我推翻 1d 趋势族的同一个理由。**

**→ 我的判断：值得作为【新假设】登记并前向验证，但不能因为它的 +83% APY 就当作已验证。**

---

## 2. 一条可能**颠覆项目成本假设**的独立分析

**[traderabyss.com — Negative Funding Looks Like Free Money. Here's the Math.](https://traderabyss.com/artigos/negative-perpetual-funding-looks-like-free-money-in)**

```
「Binance 永续上，10 万美元 delta 中性往返，在资金费付你一个基点之前，先付 200 美元 taker 手续费」
「delta 中性 carry 有两条腿 → 40 个基点（0.40%）是地板」
「标题年化 APR 在撒谎」: 快照资金费率 × 期数；而「资金费率均值回归非常剧烈」
「carry 按名义值付费，不是按保证金」→ 100× 放大资金收益的同时也放大基差爆仓风险
「机构有 prime-broker 费率与 sub-bps 成本才赚钱；散户层级这笔交易是抛硬币」
```

### 🔴 与我方成本模型的差异

| | 费率 | 两腿往返 |
|---|---|---|
| **我方主口径** | maker 0.02%/边 | **0.08%** |
| 该文（Binance taker 0.1%） | taker 0.1%/边 | **0.40%** |

**→ 差 5 倍。差异全在「maker vs taker」。**

### ⚠️ 我查了项目实际建模，发现一处**内部张力**（不是错误，但需登记为风险）

```
strategy_simulation.py:
  fee_rate_per_side:      0.0002   (maker 0.02%)
  maker_fee_per_side:     0.0002
  taker_fee_per_side:     0.0005   ← 已记录但主口径未用
  fee_basis: "maker_limit_orders_observed_in_live_trades"
  fill_model: "next_closed_bar_open_proxy"        ← 【下一根开盘价成交 = 市价语义】
  non_fill_risk: "unknown_limit_order_non_fill; next_open_proxy_assumes_fill"
```

**⚠️ 张力：`fill_model` 用的是「下一根开盘价」（保证成交 = 市价 = taker），但费用按 maker 扣。**
**⚠️ 项目自己已标注 `next_open_proxy_assumes_fill`（假设限价单一定成交）—— 这是**乐观假设**。**

**→ 建议（不是改代码，是登记）：**
1. 把 **taker 0.05%/边**作为**敏感性边界**跑一遍同样的因子实验（项目注释里已说 taker 是「保守敏感性边界」，但未见实际跑）
2. 在裁定规则里写明：**若边际在 maker 口径下 < 2×成本差，则 taker 口径下必然为负 → 该配置直接不计入**
3. 前向账户若实测限价单未成交率显著，需据实调整

---

## 3. 社区的普遍说法（按证据质量降级看待）

**[BTCC — Can Independent Crypto Traders Really Outperform in 2026?](https://www.btcc.com/en-CA/square/AltH4ck3r/1567003)**
⚠️ **这是平台内容营销**（BTCC 卖交易所服务），数字一律当广告：
```
「96% 的加密交易者亏钱」
「手续费衰减: 0.1%/笔 × 每日 5 笔 = 年亏 35%」
「2026 年超 60% 成交量来自算法」
匿名交易者: 「我不再预测价格，开始研究链上足迹 —— 真正的 alpha 在那里」
「纯 TA 打不过算法；链上数据 + 流动性分析才行」
```
**⚠️ 唯一可提取的机制线索是「链上足迹 / 交易所净流入」，但这条话本身没有可操作定义，也没给样本。**
**→ 记为【待评估方向】，不因其宣传而提高优先级。**

**[Wickra Radar](https://github.com/wickra-lib/wickra-radar#1)** — 清算级联预警
```
「在清算级联发生前看到它 —— 跨所有永续并行打分 OI / 资金费 / 订单簿 / 清算事件」
514 个流式指标，10 语言绑定
```
⚠️ **商业产品（wickra.org，满屏徽章）→ 主张属营销。**
**⚠️ 但机制值得注意：项目**已有** s21_liquidation_cascade_fade，且 DB 有 `orderflow_*` 表与清算事件。**
**→ 这条与项目已有能力最接近，是「低成本可试」的方向。**

---

## 4. 方向排序（我的建议）

| 优先级 | 方向 | 理由 | 成本 |
|---|---|---|---|
| **A** | **Quattro 型 Donchian（4H + EMA200 过滤 + 金字塔 + 灾难止损）→ 前向假设** | 唯一有独立 walk-forward 与实盘记录的存活项；与我方证伪**不冲突**（条件规则不同） | 低（只需登记规格 + 建账户） |
| **B** | **清算级联（s21）强化** | 项目已有实现 + DB 已有清算/订单流数据；外部有商业化产品说明需求真实 | 中（需补齐清算事件数据） |
| **C** | **成本敏感性（maker vs taker）横切全部 261 账户** | 这一条不新增方向，但**决定其他方向是否成立**；外部证据显示散户层级成本是主导因素 | 低（改一个参数重跑） |
| **D** | 保证金/放贷类「无 alpha」收益（staking+Lending 8–15% APY） | 社区共识；作为**基准**而非策略 —— 若策略打不过它就不值得做 | 极低（只做基准） |
| **E** | 链上足迹/交易所净流入 | 只有营销话术，无可操作定义 | 高（需新数据源） |
| **✗** | 资金费 carry / 1H 均值回归 / 趋势突破（多币·1d） | **三源收敛的证伪** —— 不要再投入 | — |

### ⚠️ 排序里最重要的不是 A，而是 C

```
如果成本口径错了 5 倍（maker 0.08% vs taker 0.40%），
那么所有「边际刚好为正」的配置都可能是负的。
所以 C 应该【先做】—— 它是其他所有结论的地基。
```

---

## 5. 建议的下一步（交 Codex / 主会话）

1. **C 先行**：用 taker 0.05%/边（甚至 0.1%）重跑因子实验的名义反解，看有多少配置从正转负
2. **A 登记**：把 Quattro 型规则写成规格（4H · BTC · EMA200 regime · 4 单位金字塔 · 5% 灾难止损），标为**前向研究假设**，加入因子实验
3. **B 评估**：查 DB 里清算事件的覆盖度，判断 s21 能否前向验证
4. **D 作为基准**：在裁定规则里加入「必须打败 USDT 放贷收益」这一条（否则不值得承担回撤风险）

⚠️ **以上都不改变已冻结的规格与账户**；新方向一律标前向研究假设，禁止标盈利/通过/可用。

---

# 第二轮扩大搜索（学术 + 社区 + 更多机制族）

## 6. 🔥 最有价值的一条：一篇**用正确统计检验否证整族**的论文

**[On the Performance of Lagged Momentum and Reversal Strategies Across Daytime and Overnight Sessions in Bitcoin and Ethereum](https://econpapers.repec.org/article/gamjjrfmx/v_3a19_3ay_3a2026_3ai_3a9_3ap_3a692-_3ad_3a2034368.htm)**（JRFM 2026, 19(9), 692）

```
数据: 2016-2025 Kraken 小时线 · 每天分两个互补 12h session
搜索: 25 种有序组合（cash/long/short/momentum/reversal）× 12 个小时边界
      = 【300 个 cutoff-strategy 组合】

全样本最优: BTC → Reversal/Reversal @08:00 UTC · ETH → Long/Reversal @05:00 UTC
           （所选规则的全样本终值/回撤/Sharpe 都优于 buy-and-hold）

⚠️ 作者自己逐条打掉:
  ① 「这些差异在 paired bootstrap 检验中【不显著】」
  ② 「Hansen 的 Superior Predictive Ability 检验，在计入 300 个组合的搜索后
      【无法拒绝原假设】」
  ③ 「时序留出（2016-2020 选 / 2021-2025 评）: ETH 规则持续，
      但【BTC 训练选出的规则跑输 buy-and-hold】」
  ④ 结论: 「应解读为【资产特定的历史收益结构】的证据，
      而不是稳定或可实施异常利润的证明」
  ⑤ 「0-2 基点成本场景只是示意，【排除了滑点、市场冲击、借币、资金费】」
```

### ⚠️ 对本项目的直接影响

- 这**直接关系我项目里的 s16（周内季节）/ s17（隔夜区间）** —— 同一族
- 它给出的判据比我用的更严：**多重检验校正（Hansen SPA）** + **时序留出**
- **→ 建议：s16/s17 的裁定规则加入「多重检验校正后的显著性」要求**，
  否则 300 个组合里挑出来的最好者天然好看

## 7. 一篇**架构与我的项目同构**的基准论文

**[Can AI Make Money in Crypto? Measuring the Gap from Backtests to Real Markets](https://arxiv.org/html/2609.34510v1)**（arXiv 2609.34510，2026-09-28）

```
覆盖: ML / RL / LLM / agent 交易方法
【三个递进现实性阶段】:
   ① historical backtesting          （历史回测）
   ② prospective exchange-based paper trading（前向 · 交易所纸面）
   ③ real-money live trading         （真实资金实盘）
执行环境: OKX
目标: 「量化 backtest-to-realization gap」· 「识别性能【何时】开始退化」
     · 「区分性能退化源于【对未来市场的泛化不足】还是【实盘执行】」
```

### ⚠️ 它给了一个我没想到的视角

**不是问「这策略行不行」，而是问「差距在哪个阶段出现」。**
两维分解：**时间现实性**（历史→未见未来）× **执行现实性**（离线模拟→实盘）。
**→ 我的项目正在阶段②（前向纸面），正是它衡量差距的关键节点。**
**→ 建议：把「阶段②到③的差距」写进事前登记 —— 即前向纸面的结果不能直接当作可实盘。**

## 8. 🔴 一个反面案例：**宣称 walk-forward 但设计泄漏**

**[JudyaiLab/hackathon-trading-agent](https://github.com/JudyaiLab/hackathon-trading-agent#1)**

```
宣称: 82.2% OOS 胜率 · walk-forward 8 窗口 · 366 笔 · 11 天纸面 −0.4% 回撤
⚠️ 但同一份 README 自陈: 「Honest Reporting: Live 40% WR vs backtest 82.2% shown side-by-side」
   → 【回测-实盘差 42 个百分点】

设计泄漏（我能看出来的）:
  · 「36-Cell Strategy Matrix: per-cell optimized combos（6 币 × 6 regime）」
    → 36 格【各自单独优化】→ 366 笔 / 36 ≈ 【每格仅 10 笔】
    → 然后每格挑最好的策略 → 【选择偏差】
    → 【walk-forward 救不了「按格优化」的设计】
  · 11 天纸面交易 = 纯噪声窗口
```

**→ 写进纪律：宣称 walk-forward 不等于无过拟合。必须问「参数是在多少个格子里挑的」。**

## 9. ✅ 一篇**验证了我成本口径**的学术论文

**[Adaptive copula-based pairs trading with market overlay](https://www.aimspress.com/article/id/6a2a8eb6ba35de0d193b2bf1)**（AIMS QFE 2026）

```
方法: 协整配对选择（ADF + KSS）· copula 依赖建模（Gaussian/t/Clayton/Gumbel/Frank）
      信号 = Copula Mispricing Index (CMI)
数据: 26,257 小时观测 · 10 个 Binance USDT 永续 · 2021-01 → 2023-12
基线成本: 【0.08% per round trip】
结果: market-neutral 版【最大回撤 < 20%】（远好于趋势族的 38~64%）
      Alpha Overlay 版（加方向暴露）密切跟踪 buy-and-hold
      强调「交易成本与策略设计的重要性」
```

### ⚠️ 两条要点

1. **它的 0.08% 往返成本 = 我项目的 maker 口径（0.04%/边 × 2）** → **在市场中性策略上我的口径有学术背书**
   （因为中性策略可挂限价单；趋势突破「下一根开盘」则更像 taker）
2. **风险特征：中性版 DD < 20%，远优于趋势族。** 这是目前外部证据里**风险收益结构最好的一族**

## 10. ⚠️ 机构也会追不显著的异象

**[Why is an ETF seeking to capture Bitcoin's night premium](https://www.marketscreener.com/news/why-is-an-etf-seeking-to-capture-bitcoin-s-night-premium-ce7e50dedc8bf221?utm_source=copy&utm_medium=social&utm_campaign=share#1)**

- 有 ETF 在捕捉「比特币夜间溢价」
- **但第 6 节那篇论文刚证明：夜间/日间 session 效应在 300 组合搜索 + SPA 检验下不显著，BTC 规则时序留出失败**
- **→ 教训：有机构产品 ≠ 异象显著。机构也可能在追噪声。**

## 11. 其余线索（证据强度较低，记录备查）

| 来源 | 内容 | 评级 |
|---|---|---|
| [Wickra Radar](https://github.com/wickra-lib/wickra-radar#1) | 清算级联预警：OI/资金费/订单簿/清算事件跨全永续打分 | 商业产品 → 机制可行，主张属营销 |
| [Kimchi/Coinbase Premium](https://blockcircle.com/blog/kimchi-premium-coinbase-premium-signals) | 地区基差（韩国溢价、Coinbase 溢价）作市场结构信号 | 有可操作定义，但需跨市场数据 |
| [pulsefeed-ledger](https://github.com/aqualang89/pulsefeed-ledger#1) | 「用真实发生过的事给加密信号打分」的账本 | README 404，但**思路与本项目的前向模拟一致** |
| [Anchorage: 系统性 BTC 备兑开仓](https://www.anchorage.com/insights/synthetic-yield-on-bitcoin-implementation-discipline-and-performance-boundaries-of-systematic-covered-call-writing) | 机构级卖波动率分析（含 performance boundaries） | 报告被表单挡住；**机制属我项目完全未覆盖的一族** |
| [Kaiko 因子指数白皮书](https://marketing.kaiko.com/hubfs/Case%20Studies%20+%20Data%20Gudies/Kaiko%20Factor%20Indices%20Whitepaper.pdf) | 因子分类框架 | 未能解析 PDF |

## 12. 第二轮搜索后的方向重排

| 优先级 | 方向 | 外部证据 | 我项目覆盖 | 判断 |
|---|---|---|---|---|
| **A** | **市场中性配对（协整选择 + copula 信号）** | ✅ 学术：DD<20% · 0.08% 成本下有学术背书 | ⚠️ 仅 s04（BTC-ETH 单对） | **最值得扩** |
| **B** | **成本敏感性（maker vs taker 横切全部因子）** | ✅ 三源证据：成本是散户主导因素 | ❌ 未做 | **先做，是地基** |
| **C** | 清算级联（强化 s21） | ⚠️ 商业产品存在 | ✅ 已有实现 + 数据 | 低成本可试 |
| **D** | Quattro 型 Donchian（4H+EMA200+金字塔+灾难止损） | ✅ 有独立 walk-forward 与实盘 | ❌ 未覆盖（与已否证的 1d 族条件不同） | 值得登记 |
| **E** | 「阶段②→③ 差距」纳入裁定规则 | ✅ arXiv 基准论文 | ❌ 未做 | 方法论改进 |
| **F** | 备兑/卖波动率 | ⚠️ 机构在做（报告被挡） | ❌ 无期权数据 | 不可行（缺数据） |
| **✗** | **s16/s17 季节/隔夜族** | ❌ 被 300 组合 + SPA 检验否证 | 已有实现 | **应降级** |
| **✗** | 15m/5m 高频族 | ❌ 内部全负 + 外部「1H 比 1D 亏 75%」 | 占实验室主要容量 | **应停止投入** |
| **✗** | 资金费 carry | ❌ 三源收敛否证 | 已 refuted | 不再投入 |

### ⚠️ 一个坦率的结构性发现

```
我项目 261 个因子账户里，绝大多数是【15m】—— 而 15m 是：
  · 内部证据: 50 组参数全负
  · 外部证据: 「同信号在 1D 有效、1H 亏 75%，手续费碾压」
  · 且 15m 是【唯一能快速达 N_min=100 的周期】

→ 这是本项目的核心两难: 唯一快可达裁定的周期，恰是被双重否证的周期。
→ 坦率结论: 【不要指望 15m 因子实验产出正期望策略】；
   它的价值在于【用最快速度验证「高换手在散户成本下不可行」这一结构性判断】，
   从而把资源转向 A/B/C/D。
```
