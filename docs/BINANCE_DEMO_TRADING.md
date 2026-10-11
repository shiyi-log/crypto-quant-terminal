# 币安 Demo Trading 执行对照

本功能只使用币安官方 **Demo Trading**，地址固定为 `https://demo-fapi.binance.com`。
CCXT 使用 `enable_demo_trading(True)`；不启用 sandbox。所有请求与签名后地址都验证 Demo
主机，禁止跳转、自动重试下单以及修改实盘配置。

## 前端操作与凭据

用户在前端配置独立 Demo API key/secret，由服务端加密保存在数据库。主密钥独立于数据库
和 Git；接口只返回是否已配置。不要使用实盘 API 密钥，不要把密钥发到聊天或提交到 Git。
检查按钮只读取 Demo 余额、仓位和三币账户报价费率；开始按钮明确选择一个已经冻结的纸面账户。

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

订单意图中的 `signal_reference_price` 冻结量化前的原始信号收盘价，来源固定为
`paper_closed_candle_from_live_market`；`reference_timestamp_ms` 是该信号 K 线的收盘时刻。
实际提交限价另存 `submitted_limit_price`；旧 `reference_price` 是提交限价的别名，不是原始
信号价格。止损和纸面退出能取得触发 K 线时也冻结此参考；缺失时保持 null，市价退出没有
提交限价。Demo 成交相对该实盘纸面参考的差异同时包含环境/定价差异和时延，不能命名为
可执行的实盘滑点，也不能证明实盘成交质量。

未成交率的人口是已经尝试提交或具有交易所订单号、且处于终结状态的入口意图，包含拒单；
不是已验证由交易所接受的订单人口。未提交便在本地过期的意图不计入分母，未知提交结果
在结果终结前也不计入。分子是该人口中报告成交数量为零的入口。

以上是 runner/Store 的尝试提交人口。页面“三个验证目标”使用全历史一致只读快照，主未成交
分母进一步限定为已有交易所订单号的已接受终结入口，交易所明确拒单、本地终结未确认、
待处理与未知入口分别列出；不能将两种人口的比例混用。报价费率与实收费用也分别展示。
详见 [三问题证据口径](DEMO_THREE_QUESTIONS.md)。

完成一个往返后，统计实际持有秒数、maker/taker 成交数、实际 realizedPnl 与各资产费用。
只有 realizedPnl 和全部 USDT 手续费均已知，才计算
`actual_net_trading_pnl_usdt = actual_realized_pnl_usdt - actual_commission_usdt`。
非 USDT 费用没有转换率时净额为 unknown；此净額不含尚未采集的资金费。
秒级利润只能依据实际对应秒级成交样本，不能用十五分钟回测结果替代。
以上冻结 K 线策略 runner 的实验范围为 `selected_candle_strategy_execution_only`，
`seconds_hypothesis_tested=False`：分钟/小时策略配合五秒轮询不构成秒级交易策略实验。

## 独立秒级首轮操作

秒级实验使用独立 CLI、信号来源和两个账本，不修改上述冻结账户的绑定，也不伪造它的
K 线 `order_submitted` 事件。下面是一轮最多八个信号、最多六百秒观察窗口、每次入口
报价名义不超过 75 USDT 的操作示例。保护、平仓和成交补查可以超过观察窗口；六百秒
到期只停止新入口，不中断已有仓位的保护。

默认 runner、冒烟测试和秒级 CLI 共用
`database/runtime/binance_demo_runner.lock` 这一个账户锁。namespace 只隔离审计账本，
不能隔离交易所净仓；不能为不同实验另设锁并行下单。秒级 CLI 自身不会停止、重启或
启动服务。交接流程如下：

1. 记录当前选定的冻结账户。通过已鉴权且带合法 Origin 的应用请求，向
   `/api/locals/binance-demo/stop` 发送 `POST {}`，暂停默认入口并继续核对、撤单及保护。
   此操作仍持有账户锁。
2. 向 `/api/locals/binance-demo/yield-flat` 发送 `POST {}`。控制器仅在其账本无净仓、
   无未决订单且订单成交数量与成交明细完全对账后，等待保护 worker 结束并释放锁；
   保存原选定账户且入口保持暂停。若核对未完成或 worker 未结束，返回冲突并保留锁与
   暂停状态下的保护。鉴权服务和网页继续在线，交接不要求停止鉴权服务。
3. 使用全新的 session ID 运行有限首轮：

   ```bash
   uv run python tools/run_binance_demo_second_level.py \
     --execute --session second_level_round1_20261012 \
     --pair BTC --max-signals 8 --duration-seconds 600 --max-notional-usdt 75
   ```

   CLI 取得同一把锁后，核查其他 Demo namespace 的所有历史未决订单、净仓和成交
   数量缺口，再从现有加密凭据 provider 加载密钥。首次运行还要求交易所全账户空仓、
   无挂单和单向持仓模式；预检失败不会启动新交易。`demo_account_lock_busy` 表示
   账户尚未交接，不能绕开锁。
4. 结束后核对报告的 `position_flat/open_orders_zero/persistence_complete`、全部
   订单与成交数量，以及实际交易所仓位和挂单。需要恢复时按下一节操作。只有交接账本
   与交易所状态均已确认无未决事项，才通过
   `/api/locals/binance-demo/start` 发送 `POST {"accountId":"原选定冻结账户ID"}`
   显式恢复默认入口；CLI 不会自动恢复它。

同一轮分别使用 `second_level_<session>_maker` 与 `second_level_<session>_taker`
两个账本，挂单距离保存在样本元数据中。每次只运行一个预分配 arm，确认账户已平仓后
再评估下一个信号。每秒读取最新收到的实盘 futures 深度快照，`|OBI5| >= 0.9` 触发，
回到 `|OBI5| <= 0.3` 后重新武装；拒绝超过五秒、重复、倒退、未来或 session 前的输入。
八个合格信号依次分配为 maker 0bp、taker、maker 1bp、taker、maker 2bp、taker、
maker 5bp、taker，与成交结果无关。观察窗口内可能不足八个信号；未观察、未被交易所
接受和未成交的档位分别记录，不能据配置声称覆盖完成。

maker 入口为 Demo 当时最优买/卖报价向外偏移指定距离的 `GTX` post-only 限价单。
taker 入口为带 10bp 穿透价格上限的 `LIMIT/IOC`，可以部分成交或完全未成交，不能称
百分之百成交的市价单。退出均用实际可归属数量的 `reduceOnly MARKET`；maker 入口
加市价退出是混合执行，不能按双边 maker 的 4bp 往返成本计算。

退出目标是**信号决策时间加十秒**，决策时映射为固定单调时钟截止点，不是在成交后
重新计时十秒。请求、核对或撤单延迟会压缩或延长实际持仓；实际持有时间另按成交回报
时间统计。maker 三秒 TTL 从本地提交认领时的调用开始计时，包含预算等待和 HTTP
请求耗时，不保证在交易所挂满三秒。报告保留本地调用开始/返回、单调时钟耗时和
交易所原始时间；调用耗时与纯网络耗时须按字段来源区分，未校验两端时钟前，也不能
把两类时间直接相减称作真实网络延迟。

## 秒级恢复与证据口径

一旦 session 已有来源 checkpoint 或订单，再用**同一 session 和完全相同参数**运行
只会恢复原冻结订单、补查成交并保护已有仓位，不新增信号；可明确加上 `--recover-only`：

```bash
uv run python tools/run_binance_demo_second_level.py \
  --execute --recover-only --session second_level_round1_20261012 \
  --pair BTC --max-signals 8 --duration-seconds 600 --max-notional-usdt 75
```

来源进度在交给执行器前持久化。策略政策、参数、币对或加载凭据的非秘密身份指纹
发生变化会拒绝恢复，不能换 session ID 绕过未知提交。下单原子认领后只尝试一次；
已认领或结果未知的订单只按原 clientOrderId 查询，绝不重新 POST。收到部分成交时先
撤销剩余入口、确认终态，再退出真实数量。未知订单、缺失成交或保护失败会保留恢复
标记，不能当作实验成功或自行丢弃订单。

报告保存在数据库 documents 的
`binance_demo_second_level/<session>/report.json`，每次另存独立历史报告；来源状态在
同目录 `source.json`。原生订单、成交和同一 `signal_id` 的对照仍保存在隔离 Demo 表中。
每个信号的纸面对照使用**决策之后收到的 Demo bookTicker** 作为 crossing quote 代理，
没有深度、排队或实际成交保证；maker 排队及是否成交保持 unknown，不以报价触及
限价伪造成交。纸面费用仅在提供账户报价时作为明确标注的假设计算，未提供报价则
保持 unknown；当前 CLI 不把报价补成实际费用。实际费用仅来自 Demo `userTrades`
的 `commission/commissionAsset/isMaker`，缺失仍为 unknown。

`observed_arms/accepted_arms/filled_arms` 区分评估、交易所接受与真实成交。认领尝试
人口含本地 POST 前保护拒绝；未成交率必须明确使用哪一种人口，不能将本地拒绝或
未知结果混进已接受终结订单的分母。净交易损益仍要求实际 realizedPnl 和全部费用
完整，不包含资金费；短样本仅验证 Demo 管道与本轮执行情况，不证明盈利、实盘有效
或可上线。

已审计的手动冒烟往返见
[实际收据](../artifacts/binance-demo-smoke-20261012.json)：BTCUSDT 开仓和平仓均成交，
两边为 taker，实收约 0.0400%/边，合计 0.04014969 USDT，扣费净交易损益
−0.04026968 USDT，最终空仓且无挂单。该手动测试不构成秒级策略样本，也未验证
maker 实收费率。

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
