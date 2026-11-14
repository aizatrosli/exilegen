"""Task 3: anti-meta gap candidates (shared mechanic tags, rare/absent together in corpus) + unique what-if job file."""
import csv, hashlib, json, os, re, sys, random
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb
KEY = {'fire', 'cold', 'lightning', 'chaos', 'physical', 'ignite', 'shock', 'freeze', 'chill', 'poison', 'bleed', 'crit', 'minion', 'totem', 'trigger',
       'conversion', 'projectile', 'curse', 'mark', 'herald', 'remnant', 'dot', 'infusion', 'exposure', 'grenade', 'charges', 'spell', 'attack'}
STRONG = KEY - {'spell', 'attack', 'projectile', 'crit'}
SK = kb.load_skills(); UQ = kb.load_uniques()
byname = {}
for s in SK.values(): byname.setdefault(s['name'], s)
ov = json.load(open('data/overview.json'))
pop_items = dict(ov['dimensions']['items']['counts']); pop_all = dict(ov['dimensions']['allskills']['counts'])
T = json.load(open('out/builds_table.json'))
skill_n = Counter(b['skill'] for b in T if b['resid'] is not None)
skills = [s for s, n in skill_n.items() if n >= 8]
def skill_tags(name):
    s = byname.get(name)
    if not s: return set(), []
    return kb.mech_tags(s['name'] + ' ' + s['desc'], s['types']) & KEY, s['types']
sup_use = defaultdict(Counter); uq_use = defaultdict(Counter); uq_n = Counter()
for b in T:
    for x in b['sup']: sup_use[b['skill']][x] += 1
    for u in set(b['uniques']): uq_use[b['skill']][u] += 1; uq_n[u] += 1
sup_all = Counter(x for b in T for x in b['sup'])
rows = []
sups = [s for s in SK.values() if s['support'] and s['name'] in sup_all]  # supports seen in corpus at all
for sk in skills:
    st, types = skill_tags(sk)
    if not st: continue
    tset = set(types)
    for s in sups:
        req = [r for r in s['req'] if r not in ('AND', 'OR', 'NOT')]
        if req and not (set(req) & tset): continue
        stg = kb.mech_tags(s['name'] + ' ' + s['desc']) & KEY
        shared = stg & st & STRONG
        if not shared: continue
        if sup_use[sk][s['name']] > 1: continue
        rows.append(dict(kind='support-skill', a=s['name'], b=sk, shared='|'.join(sorted(shared)), n_together=sup_use[sk][s['name']],
                         n_a_corpus=sup_all[s['name']], n_b_corpus=skill_n[sk], global_pop_a=pop_all.get(s['name'], 0)))
    for u, info in UQ.items():
        if uq_n[u] < 3 or info['slot'] in ('flask', 'jewel', 'soulcore', 'incursionlimb', 'tincture', 'fishing'): continue
        ut = kb.mech_tags(info['text']) & KEY
        shared = ut & st & STRONG
        if not shared or uq_use[sk][u] > 0: continue
        rows.append(dict(kind='unique-skill', a=u, b=sk, shared='|'.join(sorted(shared)), n_together=0, n_a_corpus=uq_n[u], n_b_corpus=skill_n[sk],
                         global_pop_a=pop_items.get(u, 0)))
pair_n = Counter()
for b in T:
    us = sorted(set(b['uniques']))
    for i in range(len(us)):
        for j in range(i + 1, len(us)): pair_n[(us[i], us[j])] += 1
common = [u for u, n in uq_n.items() if n >= 5 and u in UQ]
for i, a in enumerate(sorted(common)):
    ta = kb.mech_tags(UQ[a]['text']) & STRONG
    for c in sorted(common)[i + 1:]:
        if UQ[a]['slot'] == UQ[c]['slot'] and UQ[a]['slot'] not in ('ring', 'jewel'): continue
        shared = ta & kb.mech_tags(UQ[c]['text'])
        if len(shared) >= 1 and pair_n[(a, c)] == 0:
            rows.append(dict(kind='unique-unique', a=a, b=c, shared='|'.join(sorted(shared)), n_together=0, n_a_corpus=uq_n[a], n_b_corpus=uq_n[c],
                             global_pop_a=min(pop_items.get(a, 0), pop_items.get(c, 0))))
for r in rows:
    r['score'] = len(r['shared'].split('|')) * (1 + r['n_a_corpus']) ** 0.5 * (1 + r['n_b_corpus']) ** 0.5
rows.sort(key=lambda r: -r['score'])
with open('out/antimeta_candidates.csv', 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=['kind', 'a', 'b', 'shared', 'n_together', 'n_a_corpus', 'n_b_corpus', 'global_pop_a', 'score']); w.writeheader()
    for r in rows: w.writerow({k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()})
print(Counter(r['kind'] for r in rows))
for k in ('support-skill', 'unique-skill', 'unique-unique'):
    print('==', k)
    for r in [x for x in rows if x['kind'] == k][:25]: print(f"  {r['a'][:28]:28} {r['b'][:28]:28} {r['shared']:30} nA={r['n_a_corpus']} nB={r['n_b_corpus']} glob={r['global_pop_a']}")
