"""按【族】退役账户（通用版）。不可逆 ⇒ 先快照 + 逐条校验。

用法:
  python3 tools/retire_families.py --families s12,s18,s21 --dry-run
  python3 tools/retire_families.py --families s12,s18,s21 --apply
"""
import argparse
import datetime as dt
import json
import os
import pathlib

import psycopg


def J(v):
    return v if isinstance(v, dict) else json.loads(v or '{}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--families', required=True, help='逗号分隔的族前缀，如 s12,s18,s21')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    fams = tuple(x.strip() for x in args.families.split(',') if x.strip())

    with psycopg.connect(os.environ['QUANT_DATABASE_URL']) as c:
        rows = c.execute('select strategy_id, execution from quant.strategy_sim_accounts').fetchall()
        target = []
        for sid, exe in rows:
            if not sid.startswith(tuple(f + '_' for f in fams)):
                continue
            e = J(exe)
            if e.get('execution_enabled') is not False:
                target.append((sid, e))
        print(f'  族 {fams} ⇒ 待退役 【{len(target)}】个账户')

        if not args.apply:
            for sid, e in target:
                print(f'    {sid[:60]:<62} {e.get("execution_enabled")} → false')
            print('  （dry-run：未写入）')
            return

        # 快照
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        outdir = pathlib.Path('artifacts') / f'retire-families-{stamp}'
        outdir.mkdir(parents=True, exist_ok=True)
        snap = [{'strategy_id': sid, 'execution': e} for sid, e in target]
        (outdir / 'before.json').write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'  快照: {outdir}/before.json')

        changed, failed = 0, []
        for sid, e_before in target:
            try:
                c.execute(
                    """update quant.strategy_sim_accounts
                          set execution = jsonb_set(execution, '{execution_enabled}', 'false'::jsonb, true),
                              updated_at = now()
                        where strategy_id = %s""", (sid,))
                c.commit()
                r = c.execute("select execution from quant.strategy_sim_accounts where strategy_id=%s", (sid,)).fetchone()
                e_after = J(r[0])
                if (e_after.get('execution_enabled') is False
                        and {k: v for k, v in e_after.items() if k != 'execution_enabled'}
                        == {k: v for k, v in e_before.items() if k != 'execution_enabled'}):
                    changed += 1
                else:
                    failed.append((sid, 'verify mismatch'))
            except Exception as exc:
                c.rollback()
                failed.append((sid, f'{type(exc).__name__}: {exc}'))

        (outdir / 'result.json').write_text(json.dumps(
            {'families': fams, 'changed': changed, 'failed': failed}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'  ✅ 已退役 {changed}/{len(target)} · 失败 {len(failed)}')
        for sid, why in failed[:10]:
            print(f'    ❌ {sid[:56]}: {why}')
        n = 0
        for sid, _ in target:
            r = c.execute("select execution->>'execution_enabled' from quant.strategy_sim_accounts where strategy_id=%s", (sid,)).fetchone()
            if r and r[0] == 'false':
                n += 1
        print(f'  ✅ 复核: {n}/{len(target)} 已为 false')
        print(f'  记录: {outdir}/result.json')


if __name__ == '__main__':
    main()
