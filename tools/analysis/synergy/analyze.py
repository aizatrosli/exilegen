"""Tasks 1,2,4: co-occurrence lift/PMI + DPS residual association, within-skill ridge model + residual interactions, uniques."""
import csv, itertools, json, math, os, sys
from collections import Counter, defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import features, kb
OUT = 'out'
B = features.load()
med = features.add_residuals(B, min_n=5)
N = len(B)
for b in B:
    b['comps'].add('M:' + str(b['skill'])); b['comps'].add('C:' + str(b['asc']))
cnt = Counter(c for b in B for c in b['comps'])
src = Counter(b['src'] for b in B)
print('builds', N, 'src', src, 'skills w/ median', len(med), 'with resid', sum(b['resid'] is not None for b in B))
print('pob main == meta', sum(b['pob_main'] == b['meta_skill'] for b in B), 'best == meta', sum(b['skill'] == b['meta_skill'] for b in B))

def wcsv(name, rows, cols):
    with open(os.path.join(OUT, name), 'w', newline='') as f:
        w = csv.writer(f); w.writerow(cols)
        for r in rows: w.writerow([round(r[c], 4) if isinstance(r[c], float) else r[c] for c in cols])

# ---------------------------------------------------------------- 1. co-occurrence
pairtypes = [('M', 'S'), ('M', 'U'), ('U', 'U'), ('K', 'C'), ('K', 'A'), ('A', 'U'), ('K', 'U'), ('N', 'N'), ('M', 'K'), ('M', 'G'), ('U', 'S')]
pc = Counter()
for b in B:
    cs = sorted(b['comps'])
    for a, c in itertools.combinations(cs, 2):
        pc[(a, c)] += 1
resid_of = {b['hash']: b['resid'] for b in B}
mean = lambda v: sum(v) / len(v) if v else float('nan')
rows = []
comp_builds = defaultdict(list)
for b in B:
    for c in b['comps']: comp_builds[c].append(b)
for (a, c), nab in pc.items():
    ta, tc = a[0], c[0]
    if (ta, tc) not in pairtypes and (tc, ta) not in pairtypes: continue
    if nab < 3: continue
    na, nc = cnt[a], cnt[c]
    lift = nab * N / (na * nc)
    pmi = math.log2(lift); npmi = pmi / (-math.log2(nab / N)) if nab < N else 0
    # DPS association
    both = [b for b in comp_builds[a] if c in b['comps']]
    rb = [b['resid'] for b in both if b['resid'] is not None]
    if ta == 'M' or tc == 'M':
        sk, other = (a, c) if ta == 'M' else (c, a)
        without = [b['resid'] for b in comp_builds[sk] if other not in b['comps'] and b['resid'] is not None]
    else:
        only = [b['resid'] for b in comp_builds[a] + comp_builds[c] if not (a in b['comps'] and c in b['comps']) and b['resid'] is not None]
        without = only
    rows.append(dict(type=ta + '-' + tc, a=a, b=c, n_a=na, n_b=nc, n_ab=nab, lift=lift, pmi=pmi, npmi=npmi,
                     resid_with=mean(rb), resid_without=mean(without), d_resid=mean(rb) - mean(without) if rb and without else float('nan'), n_resid=len(rb), n_without=len(without)))
rows.sort(key=lambda r: -r['npmi'])
wcsv('cooc_pairs.csv', rows, ['type', 'a', 'b', 'n_a', 'n_b', 'n_ab', 'lift', 'pmi', 'npmi', 'resid_with', 'resid_without', 'd_resid', 'n_resid', 'n_without'])
syn = [r for r in rows if r['n_ab'] >= 5 and r['n_resid'] >= 4 and r['n_without'] >= 5 and r['lift'] >= 1.5 and r['d_resid'] == r['d_resid'] and r['d_resid'] > 0.15
       and not (r['type'] in ('N-N',) and r['lift'] > 30)]
syn.sort(key=lambda r: -(r['npmi'] * r['d_resid']))
wcsv('synergy_pairs_highdps.csv', syn, ['type', 'a', 'b', 'n_a', 'n_b', 'n_ab', 'lift', 'npmi', 'resid_with', 'resid_without', 'd_resid', 'n_resid', 'n_without'])
print('\n== top synergy pairs w/ DPS (by type)')
for t in sorted({r['type'] for r in syn}):
    for r in [x for x in syn if x['type'] == t][:5]:
        print(f"{t:5} {r['a'][:32]:32} {r['b'][:32]:32} n={r['n_ab']:3} lift={r['lift']:.1f} dres={r['d_resid']:+.2f}")

# ---------------------------------------------------------------- 2. ridge on within-skill residual
D = [b for b in B if b['resid'] is not None]
feat = sorted(c for c, n in cnt.items() if n >= 8 and c[0] in 'SUKANGC' and c not in ('G:',))
fi = {c: i for i, c in enumerate(feat)}
X = np.zeros((len(D), len(feat))); y = np.array([b['resid'] for b in D])
for i, b in enumerate(D):
    for c in b['comps']:
        if c in fi: X[i, fi[c]] = 1
Xm = X - X.mean(0)
def ridge(Xtr, ytr, lam):
    return np.linalg.solve(Xtr.T @ Xtr + lam * np.eye(Xtr.shape[1]), Xtr.T @ (ytr - ytr.mean())), ytr.mean()
rng = np.random.default_rng(0); folds = rng.integers(0, 5, len(D))
best = None
for lam in [3, 10, 30, 100, 300]:
    oof = np.zeros(len(D))
    for k in range(5):
        tr, te = folds != k, folds == k
        w, b0 = ridge(Xm[tr], y[tr], lam); oof[te] = Xm[te] @ w + b0
    r2 = 1 - ((y - oof) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    print('lam', lam, 'oof R2', round(r2, 3))
    if best is None or r2 > best[1]: best = (lam, r2, oof.copy())
lam, r2, oof = best
w, b0 = ridge(Xm, y, lam)
coef = sorted(((feat[i], w[i], cnt[feat[i]]) for i in range(len(feat))), key=lambda t: -t[1])
wcsv('ridge_coefs.csv', [dict(comp=c, coef=v, n=n) for c, v, n in coef], ['comp', 'coef', 'n'])
res = y - oof
for b, r, p in zip(D, res, oof): b['mres'] = float(r); b['pred'] = float(p)
print(f'\nridge lam={lam} oof R2={r2:.3f}; top+ coefs:', [(c, round(v, 2)) for c, v, n in coef[:12]])
print('top- coefs:', [(c, round(v, 2)) for c, v, n in coef[-8:]])
# outliers
Ds = sorted(D, key=lambda b: -b['mres'])
top = Ds[:40]
wcsv('dps_outliers.csv', [dict(hash=b['hash'][:12], skill=b['skill'], asc=b['asc'], dps=b['dps'], resid=b['resid'], pred=b['pred'], model_resid=b['mres'],
     uniques='|'.join(b['uniques']), supports='|'.join(b['main_sup']), keystones='|'.join(c[2:] for c in b['comps'] if c[0] == 'K'),
     unsupported=b['unsupported']) for b in top], ['hash', 'skill', 'asc', 'dps', 'resid', 'pred', 'model_resid', 'uniques', 'supports', 'keystones', 'unsupported'])
# interaction candidates: pairs/triples whose builds have mean model residual >> 0
ints = []
mres = {b['hash']: b['mres'] for b in D}
sd = float(np.std(res))
pool = [c for c, n in cnt.items() if n >= 5 and c[0] in 'SUKAMGC' and c not in ('G:', 'C:None')]
pset = set(pool)
pairb = defaultdict(list); trib = defaultdict(list)
for b in D:
    cs = sorted(c for c in b['comps'] if c in pset)
    for p in itertools.combinations(cs, 2): pairb[p].append(b['mres'])
topset = set(b['hash'] for b in Ds[:120])
for p, v in pairb.items():
    if len(v) < 5: continue
    m = mean(v); t = m / (sd / math.sqrt(len(v)))
    ints.append(dict(k=2, comps=' + '.join(p), n=len(v), mean_model_resid=m, t=t))
# triples among top-residual builds
tc = Counter()
for b in Ds[:120]:
    cs = sorted(c for c in b['comps'] if c in pset and c[0] in 'SUKAM')
    for t3 in itertools.combinations(cs, 3): tc[t3] += 1
for t3, k in tc.most_common(4000):
    if k < 4: break
    v = [b['mres'] for b in D if all(c in b['comps'] for c in t3)]
    if len(v) < 5: continue
    m = mean(v); ints.append(dict(k=3, comps=' + '.join(t3), n=len(v), mean_model_resid=m, t=m / (sd / math.sqrt(len(v)))))
ints.sort(key=lambda r: -r['t'])
# drop pairs/triples dominated by a single component that already has big coef (keep those whose t exceeds both singles)
single_t = {}
for c in pool:
    v = [b['mres'] for b in D if c in b['comps']]
    if len(v) >= 5: single_t[c] = mean(v) / (sd / math.sqrt(len(v)))
for r in ints:
    r['max_single_t'] = max(single_t.get(c, 0) for c in r['comps'].split(' + '))
ints2 = [r for r in ints if r['t'] > 2.5 and r['t'] > r['max_single_t'] + 1.0]
wcsv('interaction_candidates.csv', ints2[:300], ['k', 'comps', 'n', 'mean_model_resid', 't', 'max_single_t'])
print('\n== interaction candidates (t > singles+1):')
for r in ints2[:25]: print(f"{r['k']} n={r['n']:3} m={r['mean_model_resid']:+.2f} t={r['t']:.1f} single={r['max_single_t']:.1f}  {r['comps']}")
print('\n== top outlier builds')
for b in top[:15]:
    print(f"{b['skill'][:22]:22} {b['asc'][:14]:14} dps={b['dps']:.3g} resid={b['resid']:+.2f} mres={b['mres']:+.2f} U={b['uniques']} S={b['main_sup']} unsup={b['unsupported']}")

# ---------------------------------------------------------------- 4. uniques
U = kb.load_uniques()
ug = []
uni = Counter(u for b in B for u in set(b['uniques']))
grants = defaultdict(Counter)
for b in B:
    for g in b['granted']: grants[g['source']][g['name'] + '/' + g['activation']] += 1
mainsrc = Counter()
for b in B:
    mg = b['main_group']
    if mg.get('grantedBy'): mainsrc[mg['grantedBy']] += 1
for u, n in uni.items():
    if n < 4: continue
    # within-skill gap: for each skill with >=2 with and >=2 without, mean diff of logdps
    diffs = []; wsum = 0
    for sk in {b['skill'] for b in B if u in b['uniques']}:
        w_ = [b['logdps'] for b in B if b['skill'] == sk and u in b['uniques'] and b['logdps'] is not None]
        wo = [b['logdps'] for b in B if b['skill'] == sk and u not in b['uniques'] and b['logdps'] is not None]
        if len(w_) >= 2 and len(wo) >= 2:
            diffs.append((len(w_), mean(w_) - mean(wo), sk))
    gap = sum(k * d for k, d, _ in diffs) / sum(k for k, _, _ in diffs) if diffs else float('nan')
    sk_c = Counter(b['skill'] for b in B if u in b['uniques'])
    top_sk, top_n = sk_c.most_common(1)[0]
    conc = top_n / n
    coefv = dict((c, v) for c, v, _ in coef).get('U:' + u, float('nan'))
    ug.append(dict(unique=u, n=n, slot=U.get(u, {}).get('slot', '?'), top_skill=top_sk, top_skill_share=conc, skills=len(sk_c),
                   within_skill_gap_log10=gap, n_skill_cmp=len(diffs), ridge_coef=coefv,
                   grants='|'.join(f'{k}:{v}' for k, v in grants.get(u, {}).items()), main_from_unique=mainsrc.get(u, 0)))
ug.sort(key=lambda r: -(r['within_skill_gap_log10'] if r['within_skill_gap_log10'] == r['within_skill_gap_log10'] else -9))
wcsv('uniques.csv', ug, ['unique', 'n', 'slot', 'top_skill', 'top_skill_share', 'skills', 'within_skill_gap_log10', 'n_skill_cmp', 'ridge_coef', 'grants', 'main_from_unique'])
print('\n== build-around uniques (gap):')
for r in ug[:20]: print(f"{r['unique'][:26]:26} n={r['n']:3} gap={r['within_skill_gap_log10']:+.2f} ({r['n_skill_cmp']} sk) coef={r['ridge_coef']:+.2f} top={r['top_skill']} {r['top_skill_share']:.0%} grants={r['grants'][:60]}")
print('\n== unique main-skill sources:', mainsrc.most_common(15))
json.dump([dict(hash=b['hash'], skill=b['skill'], asc=b['asc'], dps=b['dps'], logdps=b['logdps'], resid=b['resid'], mres=b.get('mres'), pob_main=b['pob_main'],
                meta=b['meta_skill'], src=b['src'], g=b['main_group'].get('index'), sup=b['main_sup'], uniques=b['uniques']) for b in B], open(OUT + '/builds_table.json', 'w'))
