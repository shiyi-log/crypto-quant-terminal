"""检查 v1 基础账户是否仍在实验室的加载清单里。只读。"""
import sys
sys.path.insert(0, '.')
from strategy_lab_service import (load_specs, load_cost_adjusted_specs, load_2x2_specs,
                                  normalize_account_spec)
from strategy_factorial import load_factorial_specs

V1 = ['s02_dual_thrust', 's07_surge_continuation', 's01_orb_breakout', 's04_btc_eth_pairs',
      's03_tsmom_vol_target', 's08_wick_rejection', 's10_lead_lag_btc_eth',
      's05_bollinger_fade', 's06_volume_climax_reversal', 's09_trend_pullback']

req = (load_specs() + load_cost_adjusted_specs() + load_2x2_specs(activate=True)
       + load_factorial_specs(normalizer=normalize_account_spec))
ids = {s['strategy_id'] for s in req}
print(f'  实验室 spec 总数: {len(req)}')
print()
print('  ══ 10 个 v1 基础账户是否在清单中 ══')
for v in V1:
    inside = v in ids
    print(f'    {v:<34} {"✅ 在清单" if inside else "❌ 不在清单"}')
    if inside:
        s = [x for x in req if x['strategy_id'] == v][0]
        print(f'        exec_enabled={s.get("execution_enabled")} · warmup={s.get("min_warmup_bars")} · tf={s.get("timeframe")}')
        for k in ('stop_spec', 'params', 'risk_stop_atr_mult'):
            if k in s:
                v2 = s[k]
                print(f'        {k} = {str(v2)[:120]}')
