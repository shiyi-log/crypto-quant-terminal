# Binance Demo 真实手续费采集

这份流程用于在 Binance Demo 账户上分别取得 BTC、ETH、TRX 的实际 maker/taker
成交手续费。它只访问固定的 `https://demo-fapi.binance.com`，不接触实盘配置。
手续费以成交回报中的 `commission`、`commissionAsset` 和 `is_maker` 为准；账户
费率报价只能作为对照，不能替代真实成交。

## 交接前置条件

费率采集工具与默认 Demo runner、冒烟测试共用
`database/runtime/binance_demo_runner.lock`。先暂停默认 runner，并等待页面或 API
确认以下三项：账户实际空仓、没有未决订单、所有 Demo namespace 的订单与成交已完成
对账。账户不为空、存在外部订单/仓位、持仓模式为 hedge，或历史成交数量有缺口时，
采集必须停止。

确认 flat 和账本对账完成后，按下面的确切命令停止认证服务，释放 runner 对交易所的
连接和锁：

```bash
uv run python manage.py stop auth
```

不要在前置检查未完成时停 auth，也不要并行启动默认 runner、冒烟测试或另一轮费率
采集。采集工具本身不会停止、重启或自动恢复任何服务。

## 默认采集

每次采集必须显式传入 `--execute` 和新的 `fee_capture_` 会话 ID。默认配置覆盖
BTC/USDT:USDT、ETH/USDT:USDT、TRX/USDT:USDT；每笔入口名义上限为 75 USDT，
每币最多观察 600 秒，maker 入口 TTL 为 30 秒：

```bash
uv run python tools/run_binance_demo_fee_capture.py \
  --execute \
  --session fee_capture_20261012_round1 \
  --research-id "$RESEARCH_ID" --account-scope "$DEMO_ACCOUNT_SCOPE"
```

每个币会先提交一笔 GTX post-only 买单（买一价，maker，默认存活 30 秒），终结后
才决定是否需要使用新的 client order ID 重新定价。只有交易所已报告终结且零成交时
才允许这次新身份重定价；未知提交结果、未知撤单结果或部分成交都不能重新挂单。
部分成交会先撤掉剩余挂单，再只对真实成交数量提交 reduce-only MARKET 卖单。

随后提交一笔带保护价格的 LIMIT/IOC 买单（taker），并以真实成交数量提交
reduce-only MARKET 卖单。入口每一次 POST 都先写入不可变意图并原子认领，提交次数
最多一次；网络超时或结果未知时只按原 client order ID 查询，绝不重试 POST。

可以把 maker 等待窗口扩到 60 秒，但必须显式传参；总观察窗口不能小于 TTL，且不超过
600 秒：

```bash
uv run python tools/run_binance_demo_fee_capture.py \
  --execute \
  --session fee_capture_20261012_round1_ttl60 \
  --maker-ttl-seconds 60 \
  --research-id "$RESEARCH_ID" --account-scope "$DEMO_ACCOUNT_SCOPE" \
  --total-timeout-seconds 600
```

每个新 session 在身份文档中冻结闭合 gate 的 `research_id/event_id/boundary_hash/inputs_hash/target_binding_hash`
及账户范围别名。
已经存在的 session 自动进入 recovery-only，复用被冻结身份；不要求当前 gate 可读取，且不会
首次提交旧的未 claim entry。参数或凭据身份不匹配仍会拒绝恢复。

工具不会把“挂单被接受”当作 maker 成交。只有该币出现实际 `is_maker=true` 的成交，
且成交手续费和资产完整，才会把该币的 maker 费率标记为已验证。没有 maker 成交时，
报告明确保持 `maker_actual_fill_verified=false`，不能用报价费率补写。

## 中断恢复

进程中断、网络超时、未知提交或未知撤单时，必须复用原会话和**完全相同的配置**，
优先只做恢复查询：

```bash
uv run python tools/run_binance_demo_fee_capture.py \
  --execute \
  --recover-only \
  --session fee_capture_20261012_round1 \
  --research-id "$RESEARCH_ID" --account-scope "$DEMO_ACCOUNT_SCOPE"
```

`--recover-only` 只查询已有 client order ID、撤销已确认的自有未决订单、补取成交，
并保护/平掉该会话实际拥有的仓位；它不会提交新的入口。会话的配置、Demo endpoint
和加密凭据 API key 指纹都被冻结，任何参数漂移、凭据轮换、缺失 identity 或未解决的
其它 Demo namespace 风险都会拒绝执行。禁止用新 session 绕过未解决的提交结果。

若会话已成功完成，再次使用同一 session 只会读取和核对已有记录，不会新增交易或
重新 POST。采集结束必须同时满足报告中的 `position_flat`、`open_orders_zero`、
`fill_reconciliation_complete` 和 `persistence_complete`；否则继续恢复，不得恢复
默认 runner。

## 费率口径

每条 fill 的费率使用十进制精确计算：

`rate = commission / (price × quantity)`

仅当 `commissionAsset == "USDT"` 且 commission、价格、数量完整时，才可给出 USDT
费率核对。BNB 或其它非 USDT 费用原样保留并标记 unknown，不做汇率转换，也不把未知
费用当作零。每个币分别报告 maker/taker；不同币、不同资产不能合并成一个“总费率”。

报告中同时保留交易所原始成交字段和标准化字段；stdout 每行输出一个实际 fill 的
JSON，最后输出一个脱敏 summary JSON。不会输出 API key、secret、DSN、签名 URL 或
原始敏感响应。

持久化位置如下：

```text
binance_demo_fee_capture/<session>.identity.json
binance_demo_fee_capture/<session>.json
binance_demo_fee_capture/<session>/reports/<timestamp>_<uuid>.json
```

identity 只保存环境、固定 Demo endpoint、会话配置和非密凭据 fingerprint；每次运行
的完整报告按时间和随机 ID 留存，latest 指针只方便读取，历史报告不覆盖。

## 恢复服务

确认采集报告已平仓、无挂单、成交数量完全对账，并且没有 `recoveryRequired` 后，
恢复认证服务：

```bash
uv run python manage.py start auth
```

认证服务恢复后，默认 Demo runner 仍保持入口暂停，不会自动开始新的策略开仓。需要
在页面或已鉴权 API 中由用户再次手动点击“开始”，并重新确认选择的冻结策略和账户
状态。若报告仍有未知订单/仓位或费用缺口，保持暂停并继续使用同一 session 做恢复。

本文件描述的是可审计的操作流程；实际采集结果必须以对应 session 的报告和交易所
成交回报为准，不能由命令成功退出或账户费率报价推断三币均已获得 maker 成交证据。
