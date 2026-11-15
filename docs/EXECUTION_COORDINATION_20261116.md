# Implementation coordination — 2026-11-16

## Iteration frontend review GREEN ready — root action

Independent review identified canonical gate metrics being hidden by raw oos aliases, plus UTC timestamps needing Beijing conversion. Root observed 2 failed/10 passed in `/private/tmp/quant-frontend-boundary-output.txt`; agent implemented both. Please run from `vben/`: `pnpm exec vitest run apps/web-antd/src/views/quant/iteration/__tests__/runDisplay.test.ts > /private/tmp/quant-frontend-green-output.txt 2>&1`, then typecheck/build. Status `/private/tmp/quant-agent-iteration_frontend.md`.

Authoritative plan: DEVELOPMENT_PLAN_20261116.md. Root owns serial test execution.

## 02:33 central boundary completed — final fixes authorized

Output /private/tmp/quant-central-boundary-output.txt: **10 failed,214 passed,49 subtests passed**. Backtest3 NEW REDs confirmed cache config/inputs corruption and swallowed training exception. data_integrity6 NEW REDs confirmed watch publication, misalignment, invalid policies, frozen end. PG fixture fails SQL.format on JSON braces: fix literal escaping in fixture too. Owners implement these now; no more speculative test expansion, stabilize for full verification. Ledger actual PG, storage, backup, service, runtime-health helper and iteration tests in this batch GREEN. Auth routes root implemented after prior RED, pending verification.

Frontend latest /private/tmp/quant-frontend-boundary-output.txt **3failed23passed**. iteration_frontend two RED confirmed runMetrics+Beijing time, implement now. ops_health_ui clock-skew negative age regression confirmed, implement. Root will typecheck/build after these stable.

## RED verification complete, proceed with implementation

Root ran `.venv/bin/python -m pytest -p no:cacheprovider tests/test_run_backtest_task.py tests/test_auto_iteration_contract.py tests/test_iterations_api.py tests/test_service_supervision.py tests/test_database_backup.py -q` on 2026-11-16. Result **42 failed, 3 passed**. Missing-result, foreign-result and corrupt-result backtests incorrectly returned 0/done; run_id unsupported; iterator missing execution_status and accepts stale progress; API missing; service supervision functions missing; backup capability missing. All are expected preimplementation failures.

Agents backtest_fix, iteration_api, service_management, storage_migration/backup_capability may now implement their tested fixes. Root will run GREEN after files stabilize. Do not run tests independently.

Messages from agents appear empty to root. Put concrete status/contract decisions and test readiness in a short file under `/private/tmp/quant-agent-<task_name>.md` so root can read them. Check this coordination file periodically while awaiting messages.

Storage tests currently import nonexistent modules at collection. Prefer explicit capability assertion inside test or fixtures to get behavioral RED, then root will run.

## Additional RED complete — all agents proceed

- storage_migration: root ran `tests/test_storage_migration.py`, **7 failed** expected missing storage_layout. Output `/private/tmp/quant-storage-red-output.txt`. Implement now.
- service_management: root ran supervision plus sink, **9 failed** expected, output `/private/tmp/quant-service-red-result.txt`. Implement now.
- iteration_frontend: root ran requested vitest, **1 failed suite** missing `../runDisplay`; output `/private/tmp/quant-frontend-red-output.txt`. Implement now.
- backup_capability: initial RED already includes all database backup tests, implement now.

API/frontend contract: `items/total`, item run_id/execution_status/research_verdict plus optional strategy/identifier/version_id/timestamps/progress/metrics/gate.values/gate.reasons/error_code/result_source/evidence. Each agent must write status to own tmp file since all interagent messages are blank.

## Data and ledger RED complete

- data_integrity: **7 failed, 1 passed**, expected stale/forming-close handling, runtime helper absent, atomic report failure, lost failed watch report. Output `/private/tmp/quant-data-integrity-red-output.txt`. Implement now.
- ledger_protection: actual isolated PostgreSQL **11 failed, 1 passed**, direct mutations currently accepted and legacy sequence constraints absent. Output `/private/tmp/quant-ledger-red-output.txt`. Implement now.
- iteration_api: next RED **3 failed, 14 passed**, reuse failed run_id, missing registered artifact reference, missing identity on subprocess failure. Output `/private/tmp/quant-iteration-contract-output.txt`. Implement those now.

## Central review batch 02:20 Beijing

`/private/tmp/quant-core-green-output.txt`: **5 failed, 54 passed**. These are newly added RED cases: archive-write failure must not register success; stale running snapshot cannot replace terminal; matching live progress must be exposed; default migration inventory missing iteration records; copy corruption must be detected against source hash. Owners implement these now. Original wrapper tests in this batch passed.

`/private/tmp/quant-service-red-result.txt`: **4 failed, 56 passed, 21 subtests passed**. New RED cases sink partial output, db backup/restore CLI, storage CLI confirmed; owners implement (some changes appear already applied after read).

Root review: storage `precopy` must verify source SHA versus copied temporary (not only hash target), include registry/status/history/evidence runtime JSON in full inventory, publish valid JSON newline (current source showed literal backslash-n). Service supervision default must avoid silently expanding launch population: current home script runs spot-basis/integrity manually; verify parity before runtime rollout. No live cutover yet.

## Full suite and latest RED

Full suite completed **75 failed, 2081 passed, 1 skipped, 3067 subtests passed** in 267.77s; protected gate hashes unchanged. Log `/private/tmp/quant-full-baseline-output.txt`. Baseline old source defects mixed with implementation changes; root will compare and fix regressions. Do not claim full suite green.

Runtime health RED and next boundary checks completed `/private/tmp/quant-next-red-output.txt`; root inspected. runtime_health and ops_health_ui may now implement missing helpers. UI RED `/private/tmp/quant-health-ui-red-output.txt` missing runtimeHealthDisplay as expected. Additional storage path normalization, data leading-window coverage, iteration archive/registry tests and manage guarded default backup are recorded in next-red output; owners inspect and fix now. Root owns auth runtime-health route+services integration and its3 observed failures.

## Runtime health RED pending — root action

Agent runtime_health exists (spawn reported aborted to root but child started). Its backend tests `tests/test_runtime_health.py` are READY and implementation is waiting on root serial RED. Command/output path `/private/tmp/quant-agent-runtime_health.md`. UI child tests `vben/apps/web-antd/src/views/quant/ops/__tests__/runtimeHealthDisplay.test.ts` READY; command `/private/tmp/quant-agent-ops_health_ui.md`. Neither agent has written production code or run tests. Root integrate authenticated route when ready; runtime_health will not edit auth_service.py.

UPDATE 02:34: runtime_health backend and ops UI implemented after observed RED. UI pending GREEN plus new negative-age clock-skew RED; root run `pnpm exec vitest run apps/web-antd/src/views/quant/ops/__tests__/runtimeHealthDisplay.test.ts` from vben to `/private/tmp/quant-health-ui-green-output.txt`. Current helper rejects negative age; added test should fail so child can correct. Status `/private/tmp/quant-agent-ops_health_ui.md`. No child test processes.

## Data integrity second RED pending — root action

Data integrity first implementation is ready; root full-suite run collected old implementation and is not current GREEN. Please run focused current `tests/test_candle_integrity_runtime.py tests/test_candle_integrity.py -q --tb=short` and save `/private/tmp/quant-data-integrity-green-output.txt`. Agent has 6 new edge regressions awaiting RED: missing leading window, report write failure watch survival, misaligned final close, invalid policy. Test file includes isolated PostgreSQL contract (unique schema, asserts quant_test) if root sets QUANT_TEST_DATABASE_URL. Agent awaiting evidence before next production edits.

## Data integrity current GREEN ready

Agent read central-boundary failures and fixed publication/misaligned/policy/frozen-end paths. Test SQL fixture now binds JSON rather than passing braces through psycopg.sql.format. Root rerun focused files including isolated quant_test DB contract. No tests run by child and no services changed. Helpers203 lines; checker existing oversized module remains largely same.
