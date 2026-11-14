#!/usr/bin/env python3
"""Turn onebutton driver output into results.csv + builds.jsonl (with PoB codes).
usage: report.py out.jsonl outdir"""
import csv, json, sys

src, outdir = sys.argv[1], sys.argv[2]
rows, blds = [], []


def at_power(trig, scale, feeder_dps):
    """re-score with energy gain scaled (sensitivity: enemy power 10 instead of 20)."""
    tot = feeder_dps
    for t in trig or []:
        f = 1.0
        if t.get('energyRate') is not None and t.get('pobRate', 0) > 0:
            f = min(1.0, t['energyRate'] * scale / t['pobRate'])
        tot += t['dps'] * f
    return tot


for line in open(src):
    d = json.loads(line)
    r = d.get('r') or {}
    mode, ln = d['id'].split(':')
    base, fin = r.get('baseline') or {}, r.get('final') or {}
    row = {'mode': mode, 'line': int(ln), 'status': 'ok' if fin and not r.get('err') and not d.get('err') else (r.get('err') or d.get('err')),
           'converted_spell': r.get('converted', ''), 'meta': r.get('meta', ''), 'feeder': (fin.get('feeder') or {}).get('name', ''),
           'base_raw': base.get('raw'), 'base_corr': base.get('corrected'), 'final_raw': fin.get('raw'), 'final_corr': fin.get('corrected')}
    if row['base_raw'] and row['final_raw']:
        row['gain_raw'] = round(row['final_raw'] / row['base_raw'], 3)
        row['gain_corr'] = round(row['final_corr'] / row['base_corr'], 3)
        # sensitivity: half the energy (enemy power 10)
        if mode == 'opt':
            b10 = at_power(base.get('triggered'), 0.5, (base.get('feeder') or {}).get('dps', 0))
        else:
            b10 = base.get('raw')
        f10 = at_power(fin.get('triggered'), 0.5, (fin.get('feeder') or {}).get('dps', 0))
        row['base_corr_p10'], row['final_corr_p10'] = b10, f10
        row['gain_corr_p10'] = round(f10 / b10, 3) if b10 else None
        row['base_energy_factor'] = ';'.join(f"{t['name']}={t['factor']:.2f}" for t in base.get('triggered') or [])
        row['final_energy_factor'] = ';'.join(f"{t['name']}={t['factor']:.2f}" for t in fin.get('triggered') or [])
        row['ehp_base'], row['ehp_final'] = base['def']['TotalEHP'], fin['def']['TotalEHP']
        row['floors_ok'], row['one_button'] = fin.get('floors'), fin.get('oneButton')
        row['changes'] = ' | '.join(f"{c['what']}: {c['from']} -> {c['to']}" for c in r.get('log', []) if 'to' in c and 'rejected' not in c)
        row['disabled_for_spirit'] = r.get('disabledForSpirit') or ''
    rows.append(row)
    if r.get('code'):
        blds.append({'id': d['id'], 'line': int(ln), 'mode': mode, 'base_raw': row['base_raw'], 'base_corr': row['base_corr'],
                     'final_raw': row['final_raw'], 'final_corr': row['final_corr'], 'groups': r.get('groups'),
                     'final_triggered': fin.get('triggered'), 'final_feeder': fin.get('feeder'), 'combos': r.get('combos'), 'log': r.get('log'), 'code': r['code']})

cols = ['mode', 'line', 'status', 'converted_spell', 'meta', 'feeder', 'base_raw', 'base_corr', 'final_raw', 'final_corr', 'gain_raw', 'gain_corr',
        'base_corr_p10', 'final_corr_p10', 'gain_corr_p10', 'base_energy_factor', 'final_energy_factor', 'ehp_base', 'ehp_final', 'floors_ok', 'one_button',
        'disabled_for_spirit', 'changes']
with open(f'{outdir}/results.csv', 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
    w.writeheader()
    for r in rows:
        w.writerow({k: (round(v) if isinstance(v, float) and not k.startswith('gain') and k.endswith(('raw', 'corr', 'p10', 'base', 'final')) else v) for k, v in r.items()})
with open(f'{outdir}/builds.jsonl', 'w') as f:
    for b in blds:
        f.write(json.dumps(b) + '\n')
for r in rows:
    print({k: r.get(k) for k in ['mode', 'line', 'status', 'converted_spell', 'base_raw', 'base_corr', 'final_raw', 'final_corr', 'gain_raw', 'gain_corr', 'gain_corr_p10', 'base_energy_factor', 'final_energy_factor', 'changes', 'disabled_for_spirit']})
