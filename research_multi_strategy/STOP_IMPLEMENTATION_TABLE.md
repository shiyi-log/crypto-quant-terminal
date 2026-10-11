# 28 项止损实现：逐策略落地表（turnkey，已修正）

> 生成 2026-10-12 · DSH ｜ ⚠️ 第一版有正则 bug（匹配到 _required_bars），此为修正版

## 统一实现

```python
# 放在 _single() 各策略分支的 `if pos:` 块内，作为【第一段】逻辑
atr = pos.get("entry_atr")
if atr is None:
    ev = pos.get("entry_evidence") or {}
    atr = ev.get("atr") if isinstance(ev, dict) else None
if atr is None or atr <= 0:
    return None, "frozen_entry_atr_unavailable", evidence, 1.0
k = params.get("risk_stop_atr_mult", 1.5)
stop = pos["entry_price"] - pos["side"] * k * atr
evidence.update(stop_level=float(stop), entry_atr=float(atr), risk_stop_atr_mult=float(k))
if (close - stop) * pos["side"] <= 0:
    return 0, "stop_loss_close_confirmed", evidence, 1.0
```

**三个要点**
1. 用**入场时冻结的 ATR**（`entry_atr`），使止损位在开仓后固定
2. **收盘确认**（与 s08 一致），不是盘中触发
3. `exit_reason` 用与 s08 **完全相同**的字符串 `stop_loss_close_confirmed`，便于统一统计

## 逐策略表

| 策略 | ATR 窗口 | 实现行 | 现状 | 需要的动作 |
|---|---|---|---|---|
| `s01_orb_breakout` | 14* | 602 | 无任何止损 | 新增硬止损 |
| `s02_dual_thrust` | 14* | 608 | 无任何止损 | 新增硬止损 |
| `s03_tsmom_vol_target` | 14* | 965 | 无任何止损 | 新增硬止损 |
| `s04_btc_eth_pairs` | 14* | — | 无法定位 | 需人工定位 |
| `s05_bollinger_fade` | 14* | 1018 | 无任何止损 | 新增硬止损 |
| `s06_volume_climax_reversal` | 14* | 1031 | 无任何止损 | 新增硬止损 |
| `s07_surge_continuation` | 14* | 1055 | 无任何止损 | 新增硬止损 |
| `s08_wick_rejection` | 14 | 1076 | 已有硬止损 | 仅按风险档反解名义（不改退出逻辑） |
| `s09_trend_pullback` | 14 | 1113 | 无任何止损 | 新增硬止损 |
| `s10_lead_lag_btc_eth` | 14* | — | 无法定位 | 需人工定位 |
| `s11_tsmom_bollinger_pullback` | 14* | 981 | 无任何止损 | 新增硬止损 |
| `s12_surge_expansion_breakout` | 14* | 617 | 无任何止损 | 新增硬止损 |
| `s13_daily_trend_multi_horizon` | 14* | 643 | 无任何止损 | 新增硬止损 |
| `s14_vol_compression_release` | 14 | 666 | 无任何止损 | 新增硬止损 |
| `s16_weekly_seasonality` | 14* | 701 | 无任何止损 | 新增硬止损 |
| `s17_overnight_range_fade` | 14* | 712 | 无任何止损 | 新增硬止损 |
| `s18_drawdown_recovery` | 14* | 733 | 有软退出 | 保留软退出 + 新增硬止损作兜底 |
| `s19_multi_coin_breadth` | 14* | 959 | 空壳(永不交易) | 无需加（本就永不交易） |
| `s20_intraday_range_position` | 14* | 751 | 无任何止损 | 新增硬止损 |
| `s21_liquidation_cascade_fade` | 14* | 772 | 无任何止损 | 新增硬止损 |
| `s22_dual_confirmation_trend` | 14* | 801 | 无任何止损 | 新增硬止损 |
| `s23_triple_confirmation_breakout` | 14 | 818 | 无任何止损 | 新增硬止损 |
| `s24_cross_sectional_lowvol` | 14* | 841 | 空壳(永不交易) | 无需加（本就永不交易） |
| `s25_vol_regime_momentum` | 14* | 843 | 无任何止损 | 新增硬止损 |
| `s26_surge_gated_trend` | 14* | 872 | 无任何止损 | 新增硬止损 |
| `s27_long_horizon_mean_reversion` | 14* | 907 | 有软退出 | 保留软退出 + 新增硬止损作兜底 |
| `s28_weekly_breakout` | 14* | 926 | 无任何止损 | 新增硬止损 |
| `s29_trend_with_trailing` | 14* | 937 | 有软退出 | 保留软退出 + 新增硬止损作兜底 |
| `s30_dual_asset_confirmation` | 14* | 957 | 空壳(永不交易) | 无需加（本就永不交易） |

* 该策略 params 无 `atr_window`，统一用 14；这是设计选择，请在 definition 里显式记录。

## ⚠️ s04 / s10 的特殊处理（跨资产原子腿）

`s04_btc_eth_pairs` 与 `s10_lead_lag_btc_eth` 的实现**不在** `_single()` 里，
而在 `evaluate()` 的跨资产分支。它们的两个腿是**原子**的：

```python
# s04 的原子性校验（现有实现）
if not btc_pos or not eth_pos or btc_pos["side"] != -eth_pos["side"] or \
        btc_pos["opened_candle_ms"] != eth_pos["opened_candle_ms"]:
    target, reason = (0, 0), "atomic_position_inconsistent"
```

**⚠️ 给单腿加硬止损会破坏原子性**（一腿被止损平掉、另一腿还在）。
所以三个选项，请主会话判断：

| 方案 | 做法 | 代价 |
|---|---|---|
| **A（推荐）** | **配对级止损**：两腿的合成价差逆向达到 k×ATR(价差) 时**同时**平两腿 | 需新增「价差 ATR」概念 |
| B | 不加止损，只靠 `exit_band` 收敛 + `H` 持有期 | 单笔亏损不受 50 上限约束 |
| C | 单腿止损 + 立即平另一腿（模拟配对止损） | 实现复杂度高，且可能残留裸露腿 |

**⚠️ 我倾向 A**，但这属于执行层设计，我不擅自决定。**s04/s10 在修好之前不应计入「28 项已加止损」的完成度。**

## 名义反解（与止损联动）

```python
stop_distance_pct = k * atr / entry_price
notional = max_loss_per_trade_usdt / (stop_distance_pct + 0.0004)
```

风险档: R50=50 · R200=200 · R1000=1000 USDT（三档并行）

## 验收标准

1. 所有需要新增硬止损的策略都能在持仓时返回 `stop_loss_close_confirmed`
2. 事件 payload 含 `stop_level` / `entry_atr` / `risk_stop_atr_mult`
3. 硬止损在已有软退出之前判定（兜底优先）
4. 名义按风险档反解：15m R50 k=1.5 应得 ≈8,921
5. 不破坏 s08 的现有 stop/take-profit 逻辑

## 完成度记账（避免高估）

| 组别 | 项数 | 状态 |
|---|---|---|
| 已在实现里定位、需新增硬止损 | 20 | 待做 |
| 有软退出、需加硬止损兜底 | 3 (s18/s27/s29) | 待做 |
| 跨资产原子腿（s04/s10） | 2 | **方案未定**（见上） |
| ⚠️ **被我误判为空壳**（s19/s24） | 2 | 实为 `evaluate` 有可交易实现，因我规格的 `universe` 字段非法而 fail-closed |
| 空壳（真永不交易） | 1 (s30) | 见 s30 分支，需人工确认 |
| 已有硬止损 | 1 (s08) | 仅需名义反解 |
| **合计** | **29** | |

⚠️ 因此「28 项加止损」这个说法不准确 —— **实际可立即执行的是 23 项；s04/s10 待定方案；3 项空壳无需做。**


---

## ⚠️ 修正记录（2026-10-12，由备用3号复核触发）

**修正 1: s10 不是双腿原子腿**
```python
if canonical_id == "s10_lead_lag_btc_eth":
    signals[BTC]["reason"] = "signal_source_only"   # BTC 只是信号源
    pos = actual_positions[ETH]                     # 只交易 ETH
```
→ s10 只需给 **ETH 腿** 加止损；s04 才是双腿原子腿。

**修正 2: s19/s24 不是「空壳」，是「因我规格字段非法而 fail-closed」**
```
evaluate 读 spec["research_universe"]；账户里的值是:
  s19: ['BTC/USDT:USDT（交易）', '20 币（算宽度）']   ← 中文描述（我写的）
  s24: ['20 个白名单币']                              ← 中文描述（我写的）
→ s19 的 'BTC/USDT:USDT（交易）' 含 "/" → 被当币对 → 取不到数据 → fail-closed
→ s24 的 '20 个白名单币' 不含 "/" → context 为空 → fail-closed
```
我已把 `universe` 改为真实币对列表并新增 `research_universe_context`（20 币上下文）。

**⚠️ 待主会话决定**：是否重建账户（让 s19/s24 真正交易）。
- 若重建 → s19/s24 也开始交易 → **止损范围从 23 项扩到 25 项**
- 若不重建 → 保持 fail-closed，但应在规格里**显式写明**（而非依赖字段错误）

**修正 3: s19/s24/s30 的「空壳」判断来源**
我只读了 `_single()` 里的实现（那三者确实返回 `multi_asset_context_unavailable`），
**漏读了 `evaluate()` 里行 1223 / 1282 / 1343 的独立分支**。
