# 纯行情风险预检（2026-10-20）

## 结论

**数据完整性：通过。账户级风险：`not_estimable`，`risk_segment_status=open`。** 2024-01-01 00:00 UTC 至 2026-10-20 00:00 UTC 的 BTCUSDT、ETHUSDT、TRXUSDT USDⓈ-M 15m 交易价与标记价序列各有 98,208 根有效 K 线；六个序列均无无效行、重复、预期时点缺失或非连续相邻对。每条价格路径、每币每方向的非零不利跳空数均远高于 `N_gap >= 100`。

本报告只证明纯行情数据完整性及边际跳空统计可复核，不代表 5% 单窗口账户损失分布 P99 门槛通过。该单窗口指标不覆盖多根 K 线累计权益路径风险。尚未完成同步 trade/mark 账户损失分布、精确维持保证金/强平模型、1,000 ms 延迟和成交压力、基准滑点 2×及强平后 1/5 tick 的组合计算。策略信号、回测、OOS、前向结果和策略收益均未读取。

## 数据和口径

- 区间：`[2024-01-01T00:00:00Z, 2026-10-20T00:00:00Z)`，15m。
- 交易价 OHLC：Binance Vision USDⓈ-M 月度归档（2024-01 至 2026-09；每个归档 SHA-256 与官方 `.CHECKSUM` 相符），再由官方 USDⓈ-M Kline REST 接口补齐 2026-10-01 至 2026-10-20。
- 标记价 OHLC：Binance USDⓈ-M `markPriceKlines` REST 分页。
- 只对相邻有效 K 线且 `next_open_time - prior_open_time = 900,000 ms` 计算 `gap = (next_open - prior_close) / prior_close`。多头不利幅度为 `max(-gap, 0)`，空头为 `max(gap, 0)`；P99 对非零不利幅度升序取 nearest-rank `ceil(0.99 × N)`。
- `1 bp = 10^-4`。下表 P99 和最大跳空单位均为 bp；不是交易收益单位，也不表示包含执行成本。
- 原始请求分页 URL、各响应 SHA-256、归档 URL/官方校验值及规范化序列 SHA-256 见 [source_manifest.json](source_manifest.json)。收到的原始归档和 REST 页面已保存为 [raw_snapshot.tar.gz](raw_snapshot.tar.gz)，SHA-256 为 `d012129359a134155c0fa80b1ffb599025ef79a2006943742cff56b172697052`。manifest 记录 `fetched_at_utc=2026-10-20T16:25:20Z`、99 个 Vision 月档及校验文件、6 个交易价 REST 页、198 个标记价 REST 页和 REST 权重估算 2,040；采集器未单独记录重试次数，最终失败页面数为 0。

## 结果

| 价格源 | 标的 | 多头 `N_gap` | 多头 P99 | 多头最大 | 空头 `N_gap` | 空头 P99 | 空头最大 |
|---|---|---:|---:|---:|---:|---:|---:|
| 交易价 | BTCUSDT | 23,852 | 0.160 | 14.02 | 23,921 | 0.171 | 12.06 |
| 交易价 | ETHUSDT | 24,433 | 0.286 | 20.68 | 23,832 | 0.341 | 60.51 |
| 交易价 | TRXUSDT | 22,751 | 1.348 | 9.15 | 23,255 | 1.396 | 5.35 |
| 标记价 | BTCUSDT | 36,364 | 2.344 | 16.63 | 31,940 | 2.748 | 22.92 |
| 标记价 | ETHUSDT | 37,237 | 2.900 | 44.89 | 32,764 | 3.395 | 45.80 |
| 标记价 | TRXUSDT | 21,827 | 3.442 | 32.23 | 20,961 | 3.635 | 47.08 |

标记价的分位数整体高于交易价，且最大值的差距也明显。单条路径最大不利跳空分别为交易价 ETHUSDT 空头 60.51 bp（0.6051%）及标记价 TRXUSDT 空头 47.08 bp（0.4708%）；每个序列的最大跳空时间戳在 manifest 中。样本最大值须单独披露，但 Codex 将其定为 5% P99 门槛之外的尾部诊断，不因最大值单独超过 5% 自动否决；任一压力若出现负权益或未覆盖赤字仍然失败。不同时间的币种最大值若拼接，只能叫“合成最大包络”，不得称同步历史事件。

双路径角色已冻结：标记价用于未实现盈亏、保证金状态和强平触发；交易价用于止损/强平成交、已实现盈亏、手续费与滑点。强平后按交易价成交模型结算，并移除同仓位的标记价浮亏，不重复计入同一价格变动。账户 P99 应来自同步 trade/mark 历史窗口下的账户损失分布；同时把各币/方向边际 trade P99 与 mark P99 施加的“联合边际 P99 压力包络”独立报告，不能把它称为账户损失分布 P99。trade-only、mark-only 敏感性也须单列。价格职责依据见 [Binance Futures Liquidation Protocols](https://www.binance.com/en/support/faq/detail/360033525271) 和 [USDⓈ-M 强平价格说明](https://www.binance.com/en/support/faq/detail/b3c689c1f50a44cabb3a84e663b81d93)。

## 合成账户边界与待补模型

用户已确认首个研究最高杠杆 `100x`、账户单窗口损失分布 P99 上限为初始权益的 `5%`。Codex 按授权确定合成研究基准：初始权益 `10,000 USDT`，isolated，单笔风险 cap `100 USDT`，`N_max=50,000 USDT`，单币上限 `25,000 USDT`，币池 BTCUSDT/ETHUSDT/TRXUSDT；预检实际 `position_leverage=100x`。逐仓初始抵押金按仓位名义/实际杠杆分配，未分配权益不自动补仓。`N_max` 是独立名义风险预算，不是按杠杆推导的最大可开名义；该合成参数不代表用户真实账户，也不授权真实交易。

合成边界已齐，完整账户级试算尚未完成：需核实 Binance USDⓈ-M 合约规则和对应名义档的维持保证金率/金额，并获得可复算的强平成交、1,000 ms 延迟、基准滑点 2×及强平后 1/5 tick 执行模型。15m OHLC 不足以还原盘中触发顺序或延迟后真实成交；没有逐笔/盘口证据或独立验证的保守成交上界时，状态保持 `not_estimable`，风险段不得关闭。当前没有调用账户端点，不使用任何账户密钥。

合成 A/B/C 每档必须基于逐个同步 trade/mark 15m 相邻窗口、每窗重置权益后的单窗口损失分布计算 P99，并同时报告同步最大窗口损失、联合边际 P99 压力包络、trade-only/mark-only 敏感性和完整执行压力结果。它不代表多根 K 线累计权益路径 P99；任何策略候选还需连续持仓路径的独立风险裁定。当前只完成单币方向的边际跳空 P99/最大值统计，尚未运行此账户模拟。

## 来源

- [Binance USDⓈ-M Market Data API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)：15m Kline、mark-price Kline 与当前交易规则接口。
- [Binance USDⓈ-M Account API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)：账户杠杆档和 symbol 配置属于 USER_DATA；本次没有调用账户端点。
- [Binance Futures Liquidation Protocols](https://www.binance.com/en/support/faq/detail/360033525271)：Last 用于已实现盈亏；Mark 用于强平价和未实现盈亏。文档亦说明强平后的 IOCO、保险基金/ADL 处理，不能作为本次合成账户已获覆盖的证据。
- [Binance USDⓈ-M 强平价格说明](https://www.binance.com/en/support/faq/detail/b3c689c1f50a44cabb3a84e663b81d93)：维持保证金依名义档位等参数计算；精确档位仍需核验。
