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

`DemoRunnerController.start(account_id)` 是人工启动闸门。每个运行会话绑定一个
冻结策略身份和一个 Demo 执行政策；同一个 Demo 账户可并行运行最多四个会话，
每个会话独立记账，共用交易所保证金和余额。同一币同一时刻只能由一个会话持仓或挂单。
首版只支持已启用且冻结 ATR 风险完整的普通单腿账户；配对账户拒绝启动。

停止会暂停入口并撤销未成交入口，继续核对成交和保护已有仓位。重启后自动恢复核对与
保护，但入口保持暂停，必须由用户再次开始。显式平仓也只针对可由本地成交记录归属的
Demo 仓位，使用 `reduceOnly` 市价退出。未知订单或仓位阻止新入口，并产生可查告警。

### 切换策略与历史会话

策略下拉框可以先选择目标；选择本身不改变正在运行的策略。点击“暂停并切换策略”
后，后端暂停入口并核对旧会话，等待保护线程退出，在持续持有同一账户锁的情况下
重新签名读取交易所全账户仓位、挂单和持仓模式。任何实际持仓、挂单、提交结果未知，
或任一历史会话的成交数量缺口都会拒绝切换，旧会话保持暂停并继续保护；不会为了
切换自动平仓。费用字段仍按原始成交记录保留，缺失费用不会补零。

通过检查后，新策略使用新的会话账本，旧的冻结绑定、订单、成交和证据全部保留。
新会话准备成功后才提交恢复指针，且入口仍暂停；用户需再次点击“手动开始 Demo”。
若指针保存的结果无法确认，禁止开始和继续切换，旧线程也不覆盖指针。重启时按
持久化会话恢复核对与保护；缺少会话字段的旧部署兼容 `demo_default`。

“查看记录”可以选择历史会话，订单、成交、对照和证据按同一会话读取；顶部指标、
交易所仓位、余额和账户报价费率属于共享交易所账户。各策略的归属持仓和交易损益另列，
不能把余额快照当作某个策略的独立钱包。历史账本只供查看，不能通过查询
参数变成交易目标。会话之间继续共用 `binance_demo_runner.lock`，不会获得多个独立
交易钱包，也不能借新会话替换账户凭据身份。

接口为 `POST /api/locals/binance-demo/switch`（仅 `accountId`），
`GET /api/locals/binance-demo/sessions`，及订单/成交/对照/证据的只读 `session` 参数。
所有操作沿用本地客户端、Bearer、Origin 和严格字段检查。GET 不访问交易所。
前端通过 `switchSupported` 识别后端版本，旧鉴权进程未加载更新时会显示待服务更新，
而非发送不支持的切换请求。此功能的后端加载需要定向重启 `auth`；须按用户的进程
重启限制取得授权，鉴权重启后 Demo 入口保持暂停，需要人工开始。

2026-10-12 验证：相关后端 155 项通过，前端 Vitest 24 项、Vue 类型检查和生产构建
通过。真实模块集成使用独立 `quant_test` 临时 schema 和离线 HTTP stub；未向交易所
请求或实际切换策略。新静态页面已发布，HTTP 返回与构建一致；鉴权后端尚待授权
重启加载（该验证时刻的状态）。完整验证记录见
`artifacts/binance-demo-switch-validation-20261012.json`。

同日北京时间 13:53，取得用户明确授权后执行 `uv run python manage.py restart auth`，
认证进程从 81980 更新为 4144。健康检查和 `switchSupported=true` 均已通过，
原策略、会话、冻结绑定及启动边界保留，共享锁由新认证进程持有。Demo 恢复为
`paused_flat`，已有保护 worker 正常、入口暂停、无新增开仓提交；实验室、交易 API、
回测 API 和前端进程不变。当前可刷新页面后切换策略，新策略仍须人工开始。
部署验证见 `artifacts/binance-demo-auth-restart-validation-20261012.json`。

2026-10-13 北京时间 17:56，经用户授权，实际切换到
`s35_cascade_continuation_15m_Fnew_Demo_R1000_k1.0_100x_maker`，新会话为
`demo_strategy_f4c7f945f41c41b092db0943c2fa4052`，并显式开始前向交易。
`demo_default` 的 s01 冻结绑定和审计记录保留。新会话只接收启动边界之后的信号，
已有纸面仓位不会复制为 Demo 持仓；启动成功不等于已经产生实际订单。
运行核对保存在 PostgreSQL documents 的 `binance_demo_switch_s35_20261013.json`。
该文档在切换后依据当时操作输出和数据库复核整理，记录操作者、旧/新身份、预检、
HTTP 结果和当前状态；切换时的原始签名响应未复制到此文档，门控通过的依据明确标为
成功 `/switch` 及其强制执行路径，不将事后核对冒充同时刻的交易所快照。

同日北京时间 20:08，经用户指示切换并启动
`s02_dual_thrust_B15m_R1000_k1.0_100x_maker`，新会话为
`demo_strategy_dc0fc0b01eff44c2bf4b5d6e2f50ce60`。
本次核对文档为 PostgreSQL documents 的 `binance_demo_switch_s02_20261013.json`，
保留操作前的签名仓位、挂单和持仓模式响应、全账本数量对账及 HTTP 200 结果。
这些预检有独立时间戳；切换接口内部再次强制检查的通过依据是成功响应，不把预检
描述成内部检查的原始响应。旧 s35 绑定和记录保留。Demo 名义上限仍为 1,000 USDT，
纸面 R1000 风险公式得到的名义不会直接复制到交易所。实际提交名义应逐笔用量化后的
数量乘限价核对，实际成交名义则使用成交价乘成交数量；未出现订单时保持未知。

### 单账户并行策略与独立核算

采用单向模式和动态币对独占，而不修改已冻结策略的交易范围。不同币可以同时由
不同策略下单；同币已有任何会话的持仓、未终态意图或挂单，另一会话的新信号记录为
`entry_denied/shared_pair_busy`，即使同方向也不合并。该信号消费后不会排队回放。
共享调度政策单独记录，旧 rule/execution/policy hash、namespace 和前向边界不改。

订单和成交的全历史记录是币对占用的持久依据。数据库在预留新意图时取得币对事务锁，
只有原会话净成交数量为零、全部订单终态且成交数量完整对账，才允许另一会话使用该币。
超时和重启不释放未知提交，POST 认领后仍只尝试一次。中央 worker 顺序处理各会话，
共用一把账户锁；恢复时先注册全部会话，再暂停入口进行保护和对账。恢复信息保存于
PostgreSQL documents 的 `binance_demo_shared_sessions.json`。

退出只能减少当前币的唯一归属会话，且本地完整成交数量必须与交易所数量一致。
手工挂单、未注册账本、数量缺口、身份冲突继续告警，不能靠全账户净额相抵冒充对账。
各会话各自使用冻结 ATR、风险档和 1,000 USDT 名义上限；保证金仍共享，不能承诺账户
之间独立的强平风险。实际止损执行方式另记 `actual_stop_execution_mode`，不会把纸面
intrabar 规格当成 Demo 已实现的盘中触发。

可核验接口（都保留本地 Bearer、Origin 和严格字段门控）：

- `POST /api/locals/binance-demo/attach {accountId}`：新建暂停会话，保留其它策略。
- `POST /api/locals/binance-demo/adopt-session {session}`：恢复旧不可变 namespace 为暂停成员。
- `POST /api/locals/binance-demo/start-session {session}`、`stop-session {session}`：逐会话开闸或暂停，保护继续。
- `GET /api/locals/binance-demo/strategies`：各策略全历史成交核算、归属持仓、实际费用及已平仓净交易损益。
- `GET /api/locals/binance-demo/orders|fills|comparisons|evidence?session=...`：逐会话原始证据。

净交易损益只来自本策略真实成交的 realizedPnl 和已知 USDT 手续费，缺回报或缺数量
保留未知；不使用纸面盈亏，也不假造资金费或 MAE/MFE。发生共享均价混账时拒绝继续
入场和错误归属，不能直接把交易所订单 realizedPnl 分成独立策略利润。

固定分币方案可行，但会缩窄 s35 已冻结 universe，因此此处改为动态独占。Hedge 模式
仍会把同币同向策略合并到交易所均价，现有适配器也仅支持单向；它不能自动解决独立
盈亏。允许净额需要另建成交分配和虚拟持仓规则，此版本明确拒绝。多个 Demo 子账户
依赖交易所实际提供的账户能力，本实现不假定该能力存在。

2026-10-13 北京时间 20:44，s02、s35、s07 与价格爆发延续实验均已显式开闸。
s35 沿用原 namespace 和前向边界，s07 与价格爆发实验使用各自的新 namespace。
部署、空仓交接、逐会话 HTTP 操作和最终核对保存在 PostgreSQL documents 的
`binance_demo_parallel_activation_20261013.json`。20:55 的只读核对确认四会话均为
`observing`、入口已启用、单一 worker 正常，恢复和登记持久化均无未决状态；
四会话当时均为 0 订单、0 成交、无归属持仓。这是运行状态核对，尚无真实成交结果。

### 独立价格爆发延续实验

用户已选择爆发后延续，冻结新身份 `price_burst_continuation_5m_v1`，不会使用
`price_candidate` 的未来 outcome 标签。仅当连续完整实时已收盘 5m 数据满足
`close[t]/close[t-3]-1 >= 1.5%` 做多，或 `<= -1.5%` 做空；上一根尚未越过同方向
阈值才触发，避免同一爆发重复下单。ATR 为决策根之前 14 个真实波幅的均值，止损 1×ATR，
风险 sizing 档 R50，实际 Demo 名义仍至多 1,000 USDT。

账户定义、事件与断点都在 PostgreSQL documents，显式创建后只接收新的闭盘信号。
未收盘、缺根、陈旧或非实时导入数据拒绝；入场按实际确认后的下单时间，不能回填
爆发起点价格。最大持有 15 分钟从首笔真实入场成交开始计算；轮询、请求和执行会使
实际退出稍晚。止损为最新闭盘价确认后 reduce-only 退出，不能保证精确风险上限。
这是新的前向研究假设；没有真实成交时样本为零，不降低阈值凑样本或宣称已有盈利优势。

“内部模拟 vs Demo”默认使用“信号与执行”视图，查询
`GET /api/locals/binance-demo/comparisons?view=signals`。数据库在取最近 100 条前排除
`binding`、`runner_checkpoint`、`actual_execution_config`，避免运行心跳淹没业务信号。
异常及未分类阶段仍保留。选择“全部审计日志”会重新请求 `view=audit`；API 不传
`view` 时也保持完整审计视图。历史 `session` 与两个视图都兼容，已存证据不删除。
非成交事件的内部成交价显示 `—`；成交事件缺价显示“未记录”，已平仓事件显示
`exit_price` 并标注“平仓价”。

## 共用信号与执行差异

对于已有纸面策略，runner 只读 `strategy_sim_accounts/strategy_sim_events`，使用未来自然产生的
`order_submitted` 事件。首次启动截断历史事件；不回放历史下单，不改纸面账本。
绑定保存原 `strategy_id/rule_hash/execution_hash`、输入 fingerprint 和启动边界。
已有纸面仓位不会复制成 Demo 仓位；其未来平仓若 Demo 空仓，仅留下对照记录。

Demo 的信号参考和保护读取同样支持完整闭盘的低周期合成：目标周期原表缺失时，
从连续已收盘 5m 数据合成对应 15m/更高周期，保存合成来源和输入指纹。最新保护参考
必须对应最近完整目标周期；原表陈旧、源缺根或目标尚未收盘时返回不可用，不能使用
昨天的高周期价格保护今天的持仓。合成保留数据库十进制收盘价，不填补行情缺口。

2026-10-12 修正事件时钟：账户核对期间可能产生新事件，因此每条事件在处理前重新
读取本机时间，首次启动也走该路径；不再用周期开始时的旧时间判断新事件的时效。
信号仍须满足 `0 <= processing_ms - observed_at_ms <= 90000`，未来时间和真正过期
信号仍拒绝。证据、意图和撤单期限使用该处理时间；显式传入的 `cycle(now_ms=...)`
仍作为固定时间覆盖。暂停、冲突和时效拒绝仍消费游标，避免后续开始时重放旧入口。
已误拒的历史信号不重放，冻结身份、执行政策及 90 秒限制不变。验证记录见
`artifacts/binance-demo-signal-clock-validation-20261012.json`。同日北京时间 13:53 的
定向认证重启已加载此修复，部署核对通过；未回放历史误拒信号。

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

需要主动采集三币实际 maker/taker 成交费率时，使用独立的[Demo 真实手续费采集流程](BINANCE_DEMO_FEE_CAPTURE.md)。该流程说明暂停默认 runner、账户空仓与账本对账前置条件、30 秒 GTX maker 与保护价 IOC taker、同会话恢复和服务交接；采集工具不会自动停止或恢复服务。

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

首轮秒级实验 `20261012_seconds_first` 的
[实际收据](../artifacts/binance-demo-second-level-20261012.json) 已落盘：预定十分钟内
得到七个合格信号，十张订单和八条成交明细全部对账，每张订单只认领一次。
maker 0/1/2/5bp 各一张 `GTX` 均被交易所接受后撤单，全部零成交；这是四次观察，
不能外推总体未成交率，也没有 maker 实收费率证据。三个 `IOC` 入场与 reduce-only
市价退出均完成，实际持仓分别为 8.815、8.858、8.983 秒。实际 USDT 手续费合计
0.12042579，交易所 realizedPnL 合计 −0.01325998，扣费净交易损益 −0.13368577
（不含资金费）。所有同信号纸面价格对照均齐备，纸面费用和净损益保持 unknown。
结束后另行签名查询确认全账户无仓位、无挂单，全历史账本无未决或成交数量缺口；
原选定默认账户已显式恢复入场与观察。此有限样本没有盈利或上线裁定。

## ORB 周期修复与加载

`strategy_signals.evaluate()` 的实际 `bar_ms` 已经沿 `_single → _single_mechanism →
_orb` 传递，覆盖旧规则和 ATR 保护分支。开盘区间按账户实际 K 线间隔检查；日末判断
使用该周期最后一根 K 线。旧逻辑在 15m 下漏判 23:45 的最后一根，而非提前退出。
默认 5m 私有辅助调用保持兼容；1d 的十二根日线不能组成当日开盘区间，此修复不会
让这种配置成为可交易的日内 ORB。

[验证记录](../artifacts/orb-timeframe-validation-20261012.json) 包括八项新回归、
150 项联合信号测试，以及针对相同 310 行冻结定义的修复前后隔离进程比较：没有
ORB 引入的身份变化。当前请求恢复的 290 个账户全部与持久化 rule/execution hash
兼容；其余二十个未选择的旧身份存在的兼容性问题在修复前后相同，未做迁移。

`strategy-lab` 使用 Python 模块缓存，写入文件不会自动加载修复。2026-10-12
北京时间 10:53:32，经用户明确授权执行 `uv run python manage.py restart strategy-lab`，
实验室 PID 从 49682 更新为 94359。新进程已完成 bootstrap，恢复 290 个活动账户，
其中 261 个为因子账户，心跳正常且 `last_error=null`。源文件 SHA-256 与此前已验证
版本一致；启动前后全部 310 个账户的冻结定义及 rule/execution hash 逐值一致。

启动后的独立快照核对确认 127 条持仓、现金、累计损益、费用和 49 条待执行意图
均保留。重启建立新的周期启动边界，旧纸面意图在各自周期首次有效闭合线处理时
按既有 `restart_cancel` 规则取消，bootstrap 本身不立即取消：此次有 6 个 15m 和
3 个 1h 旧出场意图、38 个日线入场意图；另 2 个未选择旧账户的意图不由本服务处理。

11:00 的首个新闭合线已被新进程处理。选定 s01 R1000 账户的真实 `decision`
事件 sequence 16952 显示：BTC 为 `no_session_breakout`，ETH 为
`daily_first_entry_already_consumed`，均不再错误返回 `session_range_not_ready`。
该根未产生 s01 入场；已有历史首次突破不会补开。9 个上述旧 15m/1h 意图已逐单
对应 `order_cancelled/restart_cancel` 事件，剩余 40 个旧意图保留，冻结定义仍全部一致。

auth PID 81980 持续运行，Demo 入场仍开启。实验室持有 `strategy_lab.lock`，auth
持有独立的 `binance_demo_runner.lock`；本次重启不会释放 Demo 账户锁，也无需再次
点击“开始”。其他实验运行器应通过现有 `/yield-flat` 在线交接，而非停止 auth。

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
