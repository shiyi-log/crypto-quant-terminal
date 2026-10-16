# 运行手册：实验室切换到完整群体（Fnew 修复落地后）

**日期**：2026-10-16 · **作者**：DSH
**前置**：Codex 完成 `strategy_simulation.py` 的 `risk_sizing` legacy 分支（R398）
**当前状态**：实验室以 `--no-fnew` 运行（PID 99564 · 290 账户 · `observing`）

---

## 0. 为何需要切换

| 项 | `--no-fnew`（当前）| 完整群体（目标）|
|---|---|---|
| 账户数 | **290** | **335**（+ 45 Fnew）|
| 缺失 | **45 个 Fnew**（s32/s33/s34/s35/s36 各 9）· **s35 全部** | — |
| 退役生效 | ✅ | ✅（应为同样生效）|

⇒ **`--no-fnew` 只是 interim**；完整群体才是设计状态。

---

## 1. 切换前必须通过的验证（缺一不可）

```bash
cd /Users/shiyi/DeepSeek/量化
```

### 1.1 45/45 的 hash 一致性（**决定性**）

对全部 45 个 Fnew 持久化 definition 复算，要求 **`rule_hash` 与 `execution_hash` 与持久化行完全一致**：

```bash
QUANT_DATABASE_URL="$(tools/dburl.sh)" .venv/bin/python3 - <<'PY'
import os, json, psycopg
from strategy_lab_service import normalize_account_spec
from strategy_simulation import execution_identity
def J(v): return v if isinstance(v,dict) else json.loads(v)
ok = bad = 0
with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    rows = c.execute("""select strategy_id, definition, rule_hash, execution_hash
                          from quant.strategy_sim_accounts
                         where strategy_id like '%_Fnew_%' order by strategy_id""").fetchall()
for sid, d, rh, eh in rows:
    d = J(d)
    try:
        n = normalize_account_spec(d); i = execution_identity(d)
        if n.get('rule_hash') == rh and i.get('execution_hash') == eh: ok += 1
        else: bad += 1; print('  🔴', sid[:56])
    except Exception as e:
        bad += 1; print('  🔴', sid[:56], type(e).__name__, e)
print(f'  hash 一致 {ok}/{len(rows)} · 不一致 {bad}')
PY
```

**通过线：`hash 一致 45/45 · 不一致 0`**（不是 27/45 —— 那是修复前的状态）

### 1.2 完整启动路径复算

```bash
QUANT_DATABASE_URL="$(tools/dburl.sh)" .venv/bin/python3 -c "
from data_store import DataStore
from strategy_lab_service import (load_specs, load_cost_adjusted_specs, load_2x2_specs,
                                  normalize_account_spec, resume_frozen_specs)
from strategy_factorial import load_factorial_specs
from strategy_fnew import load_fnew_specs
req = (load_specs() + load_cost_adjusted_specs() + load_2x2_specs(activate=True)
       + load_factorial_specs(normalizer=normalize_account_spec))
ex = {s['strategy_id'] for s in req}
req += [s for s in load_fnew_specs(paired_execution_enabled=True) if s['strategy_id'] not in ex]
print('requested', len(req))
sel, defd = resume_frozen_specs(DataStore(root='.'), req)
print('✅ resume 成功: selected', len(sel), '· deferred', len(defd))
"
```

**通过线：打印 `✅ resume 成功`**（修复前此步抛 `persisted frozen strategy identity mismatch`）

### 1.3 新身份仍走新逻辑

确认**新生成**的 Fnew 身份使用新公式（`2*(entry_fee_rate_per_side+exit_fee_rate_per_side)`），
且带 `exit_fee_rate_per_side=0.0004`。

---

## 2. 切换步骤

### 2.1 停止当前实例（`--no-fnew`）

```bash
cd /Users/shiyi/DeepSeek/量化
# 只停实验室 · 不动 market_stream / Demo / trade
kill -TERM "$(cat logs/manage-strategy-lab.pid)"
# 等待退出并释放锁
for i in $(seq 1 20); do
  ps -p "$(cat logs/manage-strategy-lab.pid)" >/dev/null 2>&1 || break
  sleep 1
done
ps -eo pid,command | grep strategy_lab_service | grep -v grep   # 应为空
lsof database/runtime/strategy_lab.lock                          # 应无人持锁
```

⚠️ **必须确认【无进程且无锁】** —— 否则新实例会因 flock 失败
`raise SystemExit("strategy lab already running")` 而**静默退出**（R395）

### 2.2 记录切换时刻（用于退役验证边界）

```bash
CUT=$(date -u '+%Y-%m-%dT%H:%M:%SZ'); echo "切换时刻 $CUT"
```

### 2.3 以完整群体启动（**用 start_new_session 脱离**）

⚠️ **macOS 没有 `setsid`**（R395）· **不要用 `nohup ... &` 之外的裸后台** ·
**必须用 `start_new_session=True`**：

```bash
.venv/bin/python3 - <<'PY'
import subprocess
log = open('logs/strategy-lab.log', 'a')
p = subprocess.Popen(
    ['.venv/bin/python3', '-u', 'strategy_lab_service.py', '--fnew-paired-execution'],
    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    start_new_session=True, cwd='.')
open('logs/manage-strategy-lab.pid', 'w').write(str(p.pid))
print('启动 PID', p.pid)
PY
```

（与 `manage.py:384-388` 的做法一致 · 只是少了 `--no-fnew`）

⚠️ **不要**用 `manage.py restart strategy-lab` —— 它不带 `--no-fnew` 也不带 `--fnew`，
实际等价于本命令；但若 `manage.py` 的 Service 定义有其它默认值，以本命令为准。

### 2.4 启动后确认

```bash
sleep 15
ps -eo pid,ppid,command | grep strategy_lab_service | grep -v grep      # 应有 1 个 · PPID=1
.venv/bin/python3 -c "
import json; d=json.load(open('database/runtime/strategy_lab_status.json'))
print(d.get('pid'), d.get('running'), d.get('status'), d.get('account_count'), d.get('last_error'))
"
```

**通过线**：`running True` · `status` 为 `starting`/`waiting_new_closed_candle`/`observing` ·
**`account_count` 应为 335**（290 + 45）· `last_error` 为 None

⚠️ **若 `account_count` 不是 335 ⇒ 停下报告实际值，不要自行调整**

---

## 3. 切换后的验证（1–2 个决策周期 = 15–30 分钟）

### 3.1 退役仍生效

```bash
QUANT_DATABASE_URL="$(tools/dburl.sh)" .venv/bin/python3 - <<'PY'
import os, psycopg, datetime as dt, sys
CUT = sys.argv[1] if len(sys.argv) > 1 else None
with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
    for fam in ('s14','s34','s09','s03','s17'):
        r = c.execute("""select count(*) from quant.strategy_sim_events
                          where strategy_id like %s and event_type='decision'""",
                      (fam + '_%',)).fetchone()[0]
        print(f'  {fam}: {r} 条 decision')
PY
```

**通过线**：5 族在**切换时刻之后**产出的事件数为 **0**（用 2.2 的 `CUT`）

### 3.2 其它族（含 Fnew）正常产出

确认 **s32/s33/s34/s35/s36 的 Fnew 账户**开始产出 decision
（⚠️ 其中 **s34 已退役** ⇒ 其 Fnew 账户应仍为 0 —— 这与"Fnew 恢复"不矛盾）

### 3.3 既有工具

```bash
QUANT_DATABASE_URL="$(tools/dburl.sh)" .venv/bin/python3 \
  tools/verify_r269_retirement.py --since "$CUT"
```

**通过线**：`problems = 0`

---

## 4. 回滚（若切换失败）

```bash
# 停掉新实例 → 以 --no-fnew 重新启动（用 2.3 的脚本 + "--no-fnew"）
# 数据层不受影响：退役的 61 行 execution_enabled=false 不会被回滚
```

**关键**：**切换只影响实验室进程，不动数据库业务数据**。

---

## 5. 已知风险与注意

| 风险 | 说明 | 缓解 |
|---|---|---|
| **锁冲突静默退出** | 旧实例未完全退出 ⇒ 新实例撞 flock ⇒ `raise SystemExit`（**不写日志**）| 步骤 2.1 必须确认无进程无锁 |
| **macOS 无 `setsid`** | 不能用于脱离 | 用 `start_new_session=True` |
| **既有账户成本不变** | 45 个既有 Fnew 账户 `exit_fee_rate_per_side=None` ⇒ 保留 **4 bp** 契约（R399）| 无需处理 |
| **新身份成本 6 bp** | 新生成的 Fnew 身份带 `exit_fee_rate_side=0.0004` | 本项目不新建身份 ⇒ 不影响 |
| **2×2 未启用** | `--risk-execution-2x2` 不在启动命令中 ⇒ 2×2 仍停 | 本轮不处理（R392 已定不可行）|
