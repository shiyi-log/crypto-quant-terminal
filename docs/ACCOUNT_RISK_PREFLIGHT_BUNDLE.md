# 账户风险预检 Bundle 模板

这是 `tools/export_account_risk_preflight.py` 的输入格式说明和空模板。文件中的占位符不是风险证据，不能关闭 `risk_gate`，也不能创建 `research_id`。账户原始响应、请求签名材料和场景损失数组必须留在私有证据目录，不应提交到仓库。

## 运行命令

```bash
.venv/bin/python tools/export_account_risk_preflight.py \
  --input /path/to/private/preflight-bundle.json \
  --output /path/to/private/account-risk-preflight
```

输出目录必须不存在。命令创建 `700` 目录，并写入 `600` 的 `report.json` 和 `manifest.json`。标准输出只包含预检状态和报告 hash。

## 顶层结构

```json
{
  "config": {
    "initial_equity_usdt": "10000",
    "max_leverage": "100",
    "margin_mode": "isolated",
    "n_max_usdt": "50000",
    "per_symbol_caps_usdt": [["BTCUSDT", "25000"], ["ETHUSDT", "25000"], ["TRXUSDT", "25000"]],
    "per_symbol_leverages": [["BTCUSDT", "<frozen actual leverage>"], ["ETHUSDT", "<frozen actual leverage>"], ["TRXUSDT", "<frozen actual leverage>"]],
    "account_p99_limit_pct": "0.05",
    "required_n_gap": 100,
    "required_account_windows": 100,
    "account_scope": "<opaque reviewed account alias>",
    "tier_snapshot_max_age_hours": "24",
    "account_loss_authority_scenario_id": "<A_long|A_short|B|C>",
    "tier_snapshot_hash": "<64 lowercase hex>"
  },
  "tier_schedules": [],
  "gap_p99s": null,
  "execution_evidence": null,
  "as_of_utc": "2026-10-24T00:00:00Z"
}
```

`tier_schedules` 必须包含每个研究 symbol 的完整、连续、已独立核验的 `account_api_snapshot`，包括 `bands`、`notional_cap_inclusive` 和 signed USER_DATA provenance。`gap_p99s` 必须包含每个 symbol/direction 的 trade 与 mark P99 记录。`execution_evidence` 必须绑定同一个档位快照、执行模型和窗口数据，并包含全部 8 项执行假设、22 个固定场景、至少 100 个同步账户窗口及对应赤字观察。

金额、杠杆、P99 和滑点倍数使用十进制字符串；JSON 不允许浮点金额。禁止添加 API key、secret、签名值、Authorization、带鉴权参数的 URL、原始请求头或本地私有路径。未知字段、重复 key、超限输入和已有输出目录会被拒绝。

空模板运行的预期结果是 `not_estimable`。只有账户材料、独立来源/签名/账户范围核验、冻结杠杆矩阵、完整执行证据和风险预检全部满足协议后，结果才可能进入 `pass`；导出命令本身不会自动完成这些核验。
