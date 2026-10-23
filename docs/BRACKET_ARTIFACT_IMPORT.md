# 离线账户档位导入（待审产物）

`core.execution.bracket_import` 将已经留存的 `/fapi/v1/leverageBracket` 原始响应字节转为可重解析的待审档位清单。它不发送请求、不读取环境文件或账户凭据、不验证签名或响应来源、不选择实际杠杆，也不运行策略。

## 输入与运行

输入由两份私有文件组成：

1. 原始响应文件：保留收到的 UTF-8 JSON 字节，不格式化、不重写；大小不超过 8 MiB。
2. 脱敏元数据 JSON：大小不超过 16 KiB，只允许以下字段。示例中的 hash 和时间必须替换为该次输入对应值，账户范围和请求 ID 只能使用非敏感别名。

```json
{
  "account_scope": "opaque-account-alias",
  "request_id": "evidence-request-001",
  "source": "binance-usdm-account-evidence",
  "endpoint": "/fapi/v1/leverageBracket",
  "http_method": "GET",
  "security_type": "USER_DATA",
  "environment": "testnet",
  "signed_at_utc": "2026-10-23T00:00:00Z",
  "effective_at_utc": "2026-10-23T00:00:00Z",
  "fetched_at_utc": "2026-10-23T00:00:01Z",
  "response_sha256": "<64位小写hex：原始响应字节SHA-256>",
  "notional_cap_inclusive": false,
  "boundary_evidence_sha256": "<64位小写hex：档位端点归属约定的独立证据>",
  "raw_artifact_ref": "raw_response.json"
}
```

`environment` 必须为 `production`、`testnet` 或 `demo`。时间必须带 UTC offset，满足 `signed/effective <= fetched <= imported`。`effective_at_utc`、账户范围和档位上界是否包含端点都属于调用者声明，不能从响应自动推定；导入后仍待独立复核。`source` 是证据来源的非敏感标识，不是带签名或查询参数的 URL。不要向元数据放入 API key、secret、签名值、请求头或带鉴权参数的请求 URL。

在仓库根目录运行，路径由操作者选择；输出父目录必须已存在，目标目录必须不存在：

```bash
.venv/bin/python -m core.execution.bracket_import \
  --response /path/to/private/response.json \
  --metadata /path/to/private/metadata.json \
  --symbols BTCUSDT ETHUSDT TRXUSDT \
  --output /path/to/private/new-bracket-bundle
```

这些输入和输出应保存在私有证据目录，不提交账户原始响应或账户元数据到公共仓库。CLI 只读取显式指定的两个文件。目录权限为 `700`，两份产物权限为 `600`；已有目录一律拒绝，不覆盖历史证据。标准输出只含待审状态和绑定 hash。

## 产物与可复核边界

目录包含 `raw_response.json`（精确原始字节）和 `manifest.json`。清单保存：

- 原始响应字节 hash、选定 symbol、parser contract 和 schema version；响应里额外 symbol 也先校验，但只输出请求集合。
- 元数据 hash、解析载荷 hash、绑定清单 hash；数值先转精确十进制字符串，避免 float 或 Decimal context 舍入。
- canonical symbol（如 `BTC/USDT:USDT`）与对应 compact `execution_symbol`（如 `BTCUSDT`），供复核映射。
- `pending_tier_snapshot` 及其独立产物 hash；该 hash 不是可执行的 `tier_snapshot_hash`。

`verify_bracket_artifact(raw, manifest)` 会重新解析原始字节并重建整个清单；修改 bands、账户范围、hash、验证状态或任何额外字段均不能通过一致性复核。该步骤证明的是输入与产物对应，不证明来源真实、账户适用或请求签名已获核验。

每条待审 schedule 固定为 `verified=false`、`snapshot_kind=candidate_import`，执行 `snapshot_hash=null`。`load_tier_snapshot()` 拒绝该产物，即使调用者只给它补一个形似 SHA-256 的顶层 hash。导入器不提供自动批准或 `--verified` 开关。

解析器拒绝重复 JSON key/symbol、请求 symbol 缺失、非连续档位、错误累计维持保证金、非有限数值、布尔/字符串冒充数值、非法 UTF-8、疑似凭据字段、超限输入及非 1 的 `notionalCoef`。非 1 系数需要另外实现经过审查的账户系数处理，不能忽略或自行放大名义档位。

## 权威账户预检报告导出

账户档位完成独立核验、执行假设和同步损失场景准备后，可把一个受控 JSON bundle 交给 `tools/export_account_risk_preflight.py`。bundle 顶层字段固定为 `config`、`tier_schedules`、`gap_p99s`、`execution_evidence` 和 `as_of_utc`；金额和倍数必须使用十进制字符串，档位必须是已核验的 `account_api_snapshot`。未知字段、重复 JSON key、浮点金额、超过 16 MiB 的输入和已有输出目录都会被拒绝。

```bash
.venv/bin/python tools/export_account_risk_preflight.py \
  --input /path/to/private/preflight-bundle.json \
  --output /path/to/private/account-risk-preflight
```

命令只调用权威 `evaluate_preflight()`，不联网、不读凭据、不创建 `research_id` 或风险账本事件。输出目录为 `700`，其中 `report.json` 和 `manifest.json` 为 `600`；标准输出只包含 `status` 和报告 hash。缺少账户或执行证据时，输出必须是 `not_estimable`，不能用旧版静态表诊断器代替。

## 下一步门槛

导入后始终是 `pending_manual_verification`、`risk_feasibility_preflight=not_estimable`、`risk_segment_status=open`。需要独立核验来源、签名请求材料、账户范围、端点归属约定和原始字节到档位的关系，再单独完成逐币实际杠杆冻结、8 项执行假设、22 个压力场景与账户级预检。导入器不会建立 `research_id`、关闭 `risk_gate` 或启动候选策略。

正式快照的预检与执行层使用相同 canonical symbol/hash 契约；别名不能改变同一档位集的内容 hash，也不能通过别名提供重复 schedule。规范化规则适用于新账户风险快照，旧冻结账户的执行版本和标量杠杆路径不迁移。

## 2026-10-23 独立复审与真实样本验收

DeepSeek 只读复审 `quant-bracket-import-review-reply-20261023-01` 为 `PASS`，覆盖原始字节/解析/manifest 绑定、待审状态、执行 loader 拒绝、symbol/hash 跨层契约、Decimal 精确性与 CLI 私有目录。受影响测试为 **253 passed / 90 subtests**；全仓库回归为 **1,903 passed / 64 skipped / 2,997 subtests**，`py_compile` 和 `git diff --check` 通过。全部使用合成 fixture，未读取账户凭据或候选策略结果。

尚无真实账户原始响应 fixture，因此正式导入前须核对目标响应中 `notionalCoef`、`cum` 及档位字段的 JSON 类型和边界连续性。当前契约只接受 JSON number、单位系数和精确 `cum` 连续性；字符串数值或舍入不连续均会拒绝。若真实响应不符合此契约，先保留精确字节并补独立复核过的 parser 修订与回归测试，不凭示例猜测类型、不临时放宽容差。原始账户 fixture 留在私有证据目录，公共仓库只保存脱敏的合成回归样本与复核记录。

## 2026-10-23 精确门禁修复与采集状态

parser、执行快照 loader 与账户预检共用 `tier_math.exact_margin_offset()`，以 `Fraction` 精确计算档位边界维持保证金；负值、连续性、初始保证金缓冲及单币额度合计与 `N_max` 的比较不再依赖 active Decimal context。新增合成回归在 `prec=28/80` 下验证 `1e-30` 非连续/负值均拒绝、真正连续的高精度档位接受、缓冲边界两侧与等值条件一致、略低于 `N_max` 的额度合计拒绝而恰好足额接受。

本轮验证：受影响 8 文件 **273 passed / 90 subtests**；全仓库 **1,923 passed / 64 skipped / 2,997 subtests**；`py_compile` 和 `git diff --check` 通过。这些结果是代码回归证据，不是账户风险通过或策略盈利证据；本轮精度修复等待 DeepSeek 增量只读复审，前节 `PASS` 仅对应前节所述版本。

用户于 2026-10-23 确认账户 `leverageBracket` 原始响应及脱敏元数据“尚未采集”，当前无可导入的真实账户材料。既有公开行情快照不能替代账户适用档位；不读取凭据、不自动发起账户请求。取得两份私有输入后才进入离线导入与独立核验，当前保持 `risk_feasibility_preflight=not_estimable`、`risk_segment_status=open`，无 `research_id`、未启动候选研究。

DeepSeek 增量只读复审 `quant-bracket-context-review-reply-20261023-01` 已对上述五文件 SHA 判 `PASS`。两项非阻断 P3 保留：envelope 构造/线性损失报表的 Decimal 末位可能随 context 变化；typed loader/preflight 暂无 parser 同等的 digit/exponent 上限。该次 `PASS` 不覆盖随后账户 P99 百分比门槛的新增修复。

随后独立复查发现 `equity × pct` 的 Decimal 乘积同样可能使账户 P99 门槛随 context 改判；现由 `_account_p99_loss_limit()` 返回精确 `Fraction`，场景级和顶层账户比较共用该 helper，并保持严格 `>`（等于限值通过）。新增合成回归在 `prec=28/80` 下检查精确门槛的 `-1e-30 / 0 / +1e-30` 三档，同时断言全部适用场景与顶层原因；测试夹具使用精确字符串及 `Fraction` 核验，避免先舍入边界。该增量受影响 8 文件 **279 passed / 90 subtests**、全仓库 **1,929 passed / 64 skipped / 2,997 subtests**，`py_compile` 与 `git diff --check` 通过。DeepSeek 增量只读复审 `quant-p99-limit-review-reply-20261023-01` 核对 SHA、复跑 279/90 并判 `PASS`；其唯一新 P3 是私有 helper 可增加直接输入防御，非阻断。账户材料与风险状态不变。
