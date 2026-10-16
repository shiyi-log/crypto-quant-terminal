"""退役【v2 执行模型】账户（无 ATR 止损 · 违反 ≤50 USDT 约束）。

依据:
  · R430: v2 账户无止损 ⇒ 实际最大损失 = 全部保证金（中位 92.59 USDT）⇒ 违反用户约束
  · R430: v2（中位 −7.95 bp）与 v3（−14.92 bp）是两个不同总体 ⇒ 保留会造成混样
  · 触发器 `quant.strategy_sim_immutable_account` 的受控例外: 仅允许
    【只改 execution_enabled】且【true/null → false】

安全措施:
  · `--dry-run` 默认开启：只打印计划
  · `--apply` 才写
  · 每条 UPDATE 后校验: execution 除 execution_enabled 外【完全不变】
  · 记录退役清单到 artifacts/
"""
import argparse, datetime as dt, json, os, pathlib, re
import psycopg


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--model', default='liquidation-v2', help='要退役的 execution.version 子串')
    args = ap.parse_args()

    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
        rows = c.execute('select strategy_id, execution from quant.strategy_sim_accounts').fetchall()
        target = []
        for sid, exe in rows:
            e = J(exe)
            if args.model in str(e.get('version') or ''):
                if e.get('execution_enabled') is not False:
                    target.append((sid, e))
        print(f'  待退役（{args.model}）: 【{len(target)}】个')

        plan = []
        for sid, e in target:
            before = e.get('execution_enabled')
            plan.append({'strategy_id': sid, 'before': before, 'after': False})
        if not args.apply:
            print('  （dry-run：未写入）前 10 个:')
            for p in plan[:10]:
                print(f'    {p["strategy_id"][:58]:<60} {p["before"]} → false')
            return

        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        outdir = pathlib.Path('artifacts') / f'retire-v2-{stamp}'
        outdir.mkdir(parents=True, exist_ok=True)

        changed, failed = 0, []
        for p in plan:
            sid = p['strategy_id']
            try:
                # 用 jsonb_set 只在 execution 上设置键；触发器据此判定"只改了 execution_enabled"
                c.execute(
                    """update quant.strategy_sim_accounts
                          set execution = jsonb_set(execution, '{execution_enabled}', 'false'::jsonb, true),
                              updated_at = now()
                        where strategy_id = %s and execution->>'version' like %s""",
                    (sid, f'%{args.model}%'))
                c.commit()
                # 校验: 只有 execution_enabled 变了
                row2 = c.execute('select execution from quant.strategy_sim_accounts where strategy_id=%s', (sid,)).fetchone()
                e2 = J(row2[0])
                ok = (e2.get('execution_enabled') is False
                      and {k: v for k, v in e2.items() if k != 'execution_enabled'}
                      == {k: v for k, v in dict(target[[x[0] for x in target].index(sid)][1]).items()
                          if k != 'execution_enabled'})
                if ok:
                    changed += 1
                else:
                    failed.append((sid, 'verify mismatch'))
            except Exception as exc:
                c.rollback()
                failed.append((sid, f'{type(exc).__name__}: {exc}'))

        (outdir / 'retired.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'  ✅ 已退役 {changed}/{len(plan)} · 失败 {len(failed)}')
        for sid, why in failed[:10]:
            print(f'    ❌ {sid[:56]}: {why}')
        # 复核
        n = 0
        for sid, _ in target:
            r = c.execute('select execution->>\'execution_enabled\' from quant.strategy_sim_accounts where strategy_id=%s', (sid,)).fetchone()
            if r and r[0] == 'false':
                n += 1
        print(f'  ✅ 复核: {n}/{len(target)} 已为 false')
        print(f'  记录: {outdir}/retired.json')


if __name__ == '__main__':
    main()
