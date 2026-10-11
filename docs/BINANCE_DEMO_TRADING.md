# 币安 Demo Trading 执行对照

本功能只使用币安官方 **Demo Trading**，地址固定为 `https://demo-fapi.binance.com`。
CCXT 使用 `enable_demo_trading(True)`；不启用 sandbox。所有请求与签名后地址都验证 Demo
主机，禁止跳转、自动重试下单以及修改实盘配置。

## 前端操作与凭据

用户在前端配置独立 Demo API key/secret，由服务端加密保存在数据库。主密钥独立于数据库
和 Git；接口只返回是否已配置。不要使用实盘 API 密钥，不要把密钥发到聊天或提交到 Git。
检查按钮只读取 Demo 余额和仓位；开始按钮明确选择一个已经冻结的纸面账户。

部署时先显式安装隔离的 Demo 表和加密凭据表，然后重启本机鉴权服务：

```bash
uv run python tools/setup_binance_demo.py
```

该安装命令不创建密钥、不连接交易所、不下单。之后在前端配置 Demo 密钥，点击检查，
核对 Demo 地址与余额，再选择一个冻结账户并点击开始。服务启动不会自行恢复新入口。

`DemoRunnerController.start(account_id)` 是人工启动闸门。一个 Demo 账户只能绑定一个
冻结策略身份和一个 Demo 执行政策，不将 261 个独立纸面钱包合并成一个净头寸钱包。
首版只支持已启用且冻结 ATR 风险完整的普通单腿账户；配对账户拒绝启动。

停止会暂停入口并撤销未成交入口，继续核对成交和保护已有仓位。重启后自动恢复核对与
保护，但入口保持暂停，必须由用户再次开始。显式平仓也只针对可由本地成交记录归属的
Demo 仓位，使用 `reduceOnly` 市价退出。未知订单或仓位阻止新入口，并产生可查告警。

## 共用信号与执行差异

runner 只读已有 `strategy_sim_accounts/strategy_sim_events`，使用未来自然产生的
`order_submitted` 事件。首次启动截断历史事件；不回放历史下单，不改纸面账本。
绑定保存原 `strategy_id/rule_hash/execution_hash`、输入 fingerprint 和启动边界。
已有纸面仓位不会复制成 Demo 仓位；其未来平仓若 Demo 空仓，仅留下对照记录。

入口使用信号收盘价的 post-only 限价单，按 Demo 实际 `exchangeInfo` 的数量步长向下取整，
买入价向下、卖出价向上取价格步长。三十秒未成交则撤单，再次查询并记录并发部分成交。
每次下单前持久化意图并原子认领；认领之后只尝试一次。网络超时或结果不明时只按
clientOrderId 查询，绝不重新提交。

Demo 名义默认上限 1,000 USDT。止损距离定仓使用原纸面风险档和费用假设作为计算输入，
再应用 Demo 名义上限与交易所精度。**这不是 Demo 实亏 R50/R200/R1000 的保证**，也
不是与纸面完全相同的资金组合。实际 Demo 杠杆、保证金与权益从交易所快照记录，不
自动设置为纸面 100 倍。对照定位为同一信号的成交质量差异，不能据此归因完整组合收益。

保护止损冻结信号 ATR，并使用实际 Demo 入场成交均价计算固定价位；新闭盘价越过
价位时使用 reduce-only 市价退出。入口门控或无关外部订单不会阻断可归属仓位的退出。
收盘确认、请求延迟、跳空、部分成交和实际手续费仍可能让实际亏损超过计算风险档。

## 审计与真实费用

独立 `demo_orders/demo_fills/demo_positions/demo_account` 保存意图、实际订单、实际成交
以及仓位/账户快照；第五张 `demo_signal_comparisons` 关联同一冻结信号的两条执行路径。
交易所订单与成交原始 camelCase 字段保留；十进制以字符串展示，不经过浮点转换。
意图的冻结 ATR、政策 hash 与过期时间单独保存在不可变 `intent_payload`。

纸面下一根 open 成交是假设代理；Demo 实际 price、timestamp、部分成交、拒单、请求
延迟和未成交率单独记录。手续费只读实际成交的 `commission/commissionAsset/isMaker`；
缺失为 unknown，不能使用配置费率补写真实费用，也不能把非 USDT 手续费按零处理。
终结订单的缺失手续费资产、maker/taker 或 realizedPnl 会继续补查，十分钟后以一分钟
间隔核对；没有新回报时始终保留 unknown。收到相互矛盾的部分费用回报时净额也为 unknown。

完成一个往返后，统计实际持有秒数、maker/taker 成交数、实际 realizedPnl 与各资产费用。
只有 realizedPnl 和全部 USDT 手续费均已知，才计算
`actual_net_trading_pnl_usdt = actual_realized_pnl_usdt - actual_commission_usdt`。
非 USDT 费用没有转换率时净额为 unknown；此净額不含尚未采集的资金费。
秒级利润只能依据实际对应秒级成交样本，不能用十五分钟回测结果替代。

## 结论边界与验证

币安官方 FAQ 说明 Demo 的图表数据、订单簿定价和订单执行可能与实盘不同，Demo 表现
不是未来实盘收益的可靠指标。FAQ 未保证实际手续费与实盘相同，必须实测。
Demo 是前向研究假设与准入证据的一项；不自动上线、不宣称盈利或可上线。

只读账户预检：

```bash
uv run python demo_execution_service.py --account YOUR_FROZEN_PAPER_ACCOUNT
```

此命令不读取 Demo 密钥、不连接交易所、不下单。离线测试覆盖幂等提交、未知结果恢复、
部分成交撤单、外部仓位门控、保护退出、实际费用缺失、前向边界及重启入口暂停。
