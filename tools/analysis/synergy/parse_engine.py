"""Parse task-5 (support ranking) and task-3 (unique what-if) engine outputs into CSVs."""
import csv, json, os, sys
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb import family
def load(p):
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []
def dps(st): return max(st.get('CombinedDPS') or 0, st.get('MinionCombinedDPS') or 0) if isinstance(st, dict) else 0

# ---- task 5
jobs = {json.loads(l)['id']: json.loads(l)['skill'] for l in open('jobs_sup.jsonl')} if os.path.exists('jobs_sup.jsonl') else {}
rows, agg_top = [], Counter(); agg_neg = Counter(); agg_used_zero = Counter(); per_skill_top = defaultdict(Counter)
for r in load('out_sup.jsonl'):
    res = {x['label']: x['r'] for x in r['res']}
    if r.get('err') or 'valid' not in res or 'remloss' not in res or not isinstance(res['valid'], dict) or 'supports' not in res['valid']: continue
    base = res['remloss']['base']
    if base <= 0: continue
    socketed = {x['name']: 1 - x['dps_without'] / base for x in res['remloss']['rows']}
    fams = {family(n) for n in socketed}
    cand = [(s['name'], s['dps_delta'] / base) for s in res['valid']['supports'] if s.get('dps_delta') is not None and not s.get('socketed') and family(s['name']) not in fams]
    cand.sort(key=lambda t: -t[1])
    # within-family upgrade (e.g. II -> III) is allowed too: compare by removal of same family
    nonneg = {k: v for k, v in socketed.items() if v >= 0}
    weakest = min(nonneg.items(), key=lambda t: t[1]) if nonneg else ('-', 0)
    negative = [f"{k} ({v:+.0%})" for k, v in socketed.items() if v < -0.01]
    for k in negative: agg_neg[k.split(' (')[0]] += 1
    nP = len(socketed)
    union = sorted(list(socketed.items()) + cand, key=lambda t: -t[1])
    topN = {n for n, _ in union[:nP]}
    overlap = len(topN & set(socketed)) / nP if nP else float('nan')
    # greedy swap estimate: replace weakest socketed with best candidate (additive approximation)
    swap_gain = (1 + cand[0][1]) / (1 + 0) * (1 - weakest[1]) - 1 if cand else 0.0
    zero = [n for n, v in socketed.items() if abs(v) < 0.01]
    for n in zero: agg_used_zero[n] += 1
    for n, v in cand[:3]:
        agg_top[n] += 1; per_skill_top[jobs.get(r['id'], '?')][n] += 1
    rows.append(dict(hash=r['id'][:12], skill=jobs.get(r['id'], '?'), base_dps=round(base), n_sup=nP, overlap_topN=round(overlap, 2),
                     weakest=f"{weakest[0]} ({weakest[1]:+.1%})", best_unused=f"{cand[0][0]} (+{cand[0][1]:.1%})" if cand else '',
                     unused_2_3='; '.join(f"{n} (+{v:.1%})" for n, v in cand[1:3]), swap_gain_est=round(swap_gain, 3),
                     player_supports='; '.join(f"{n} ({v:+.1%})" for n, v in sorted(socketed.items(), key=lambda t: -t[1])),
                     zero_dps_supports=len(zero), negative_value_supports='; '.join(negative)))
if rows:
    with open('out/support_vs_pob.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    import statistics as S
    print('task5 builds', len(rows), 'median overlap', S.median(r['overlap_topN'] for r in rows), 'median swap gain', S.median(r['swap_gain_est'] for r in rows),
          'mean zero-dps supports', S.mean(r['zero_dps_supports'] for r in rows))
    print('most-recommended unused supports:', agg_top.most_common(15))
    print('socketed supports with ~0 DPS value:', agg_used_zero.most_common(10))
    print('socketed supports that LOWER PoB DPS:', agg_neg.most_common(10))
    for r in sorted(rows, key=lambda r: -r['swap_gain_est'])[:12]:
        print(f"  {r['skill'][:20]:20} ov={r['overlap_topN']} swap={r['swap_gain_est']:+.0%} weakest={r['weakest']} best={r['best_unused']}")
    json.dump({k: v.most_common(5) for k, v in per_skill_top.items()}, open('out/support_top_unused_by_skill.json', 'w'), indent=1)

# ---- task 3 what-if
meta = json.load(open('jobs_wif_meta.json')) if os.path.exists('jobs_wif_meta.json') else {}
by = defaultdict(list); errs = Counter()
for r in load('out_wif.jsonl'):
    m = meta.get(r['id']); res = {x['label']: x for x in r['res']}
    if not m: continue
    if r.get('err') or 'after' not in res or not res['after']['ok']:
        errs[m['cand']] += 1; continue
    b, a = dps(res['base']['r']), dps(res['after']['r'])
    if b > 0: by[m['cand']].append((a / b - 1, res['after']['r'].get('TotalEHP', 0) / max(1, res['base']['r'].get('TotalEHP', 1)) - 1, m['slot']))
out = []
for c, v in by.items():
    d = sorted(x[0] for x in v)
    out.append(dict(candidate=c, n=len(v), median_dps_change=round(d[len(d) // 2], 3), min=round(d[0], 3), max=round(d[-1], 3),
                    median_ehp_change=round(sorted(x[1] for x in v)[len(v) // 2], 3), slot=v[0][2], errors=errs.get(c, 0)))
out.sort(key=lambda r: -r['median_dps_change'])
if out:
    with open('out/whatif_uniques.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(out[0])); w.writeheader(); w.writerows(out)
    for r in out: print(f"  {r['candidate'][:60]:60} n={r['n']} dDPS={r['median_dps_change']:+.1%} [{r['min']:+.0%},{r['max']:+.0%}] dEHP={r['median_ehp_change']:+.0%} {r['slot']}")
print('wif errors', dict(errs))
