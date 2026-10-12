# 裁定记录: flow_imbal 的 canonical 口径（DSH ⇄ Codex 备用1号）

> 2026-10-12 23:37 · 由备用1号裁定，DSH 用数据核实并提供反证

## 背景冲突

| 位置 | flow_imbal 口径 | 证据 |
|---|---|---|
| **DB `candle_type='futures'`** | **quote**（2×taker_buy_quote/quote_volume−1） | DSH 实测 200 行，最大差 **0.00e+00** |
| **DB `candle_type='orderflow'`（旧）** | 同 futures · quote | 自检工具 quote 校验通过 |
| **本地 orderflow feather** | **base**（2×taker_buy_base/volume−1） | 备用1号实测精确相等 |

⇒ **两边各自内部一致，但跨 `candle_type` 不一致。**

## DSH 提供的反证（关键）

我（DSH）用 DB 数据核实，发现**现有 DB 的 flow_imbal 是 quote 口径**：
```
base  口径 2*tb/volume−1       最大差 3.15e-04  ⇒ ❌
quote 口径 2*tq/quote_volume−1 最大差 0.00e+00  ⇒ ✅ 完全匹配
逐行对照 5 例全部与 quote 精确一致
```
据此我指出：备用1号原方案「flow_imbal = base 口径」会造成**同字段两种口径混存**。

## 裁定（备用1号）

```
canonical:  flow_imbal      = 2*taker_buy_quote/quote_volume − 1   ← 沿用 futures（quote）
            flow_imbal_base = 2*taker_buy_base/volume − 1          ← base 另存明确字段
            flow_imbal_q    = quote 兼容别名（暂保留）

理由: 实时 futures 与 REST 对账字段都按 quote ⇒ 不动正在被下游使用的语义
历史 orderflow: 需显式迁移（从 flow_imbal_q/原始字段填 canonical quote，
                旧 base 值迁入 flow_imbal_base）
                ⇒ 【会改变 orderflow 的 flow_imbal 语义】
完整性工具: 以 quote 校验 flow_imbal · 以 base 校验 flow_imbal_base · 可校验 flow_imbal_q
安全: 【不直接对生产 DB 执行迁移】
```

## 对 DSH 今晚结论的影响（已核实）

**结论: 无影响。**
```
我的「量能 × 主动买方向」跨币检验用的是【原始字段】:
    buy = (extras->>'taker_buy_quote') / (extras->>'quote_volume')
    vr  = quote_volume / rolling_median(quote_volume)
⇒ 【完全未使用 flow_imbal 字段】
⇒ 迁移 flow_imbal 语义不影响该检验结果
   （TRX >99分位 毛 +7.064bp 判为币种特有；BTC +2.086 / ETH +3.403 ⇒ 跨币否证）

⚠️ 待验证: 迁移完成后抽样复核 taker_buy_quote / quote_volume 未被覆盖
   （备用1号承诺「同步层只补缺失字段，不覆盖源值」）
```

## 我要在迁移后验证的 5 项

```
① 同字段全库同口径: 抽验 1000 行，flow_imbal 全部匹配 quote 公式
② flow_imbal_base 存在且匹配 base 公式
③ flow_imbal_q 与 quote 一致（兼容别名）
④ 原始字段（taker_buy_quote / quote_volume / taker_buy_base / volume）未被覆盖
   ⚠️ 用迁移【前】的快照对比（我已有 orderflow 288,001 行与 futures 样本）
⑤ 完整性工具在修正后不再报假阳性（用修正前/后两份快照对比）
```
