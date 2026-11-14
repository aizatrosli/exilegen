"""Task 3 what-if: equip a candidate unique into builds of a skill (unique-skill gaps) or into builds holding unique A (unique-unique gaps)."""
import csv, json, hashlib, sys, os
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb
UQ = kb.load_uniques()
SLOT = {'amulet': ['Amulet'], 'belt': ['Belt'], 'body': ['Body Armour'], 'helmet': ['Helmet'], 'gloves': ['Gloves'], 'boots': ['Boots'], 'ring': ['Ring 1', 'Ring 2']}
T = json.load(open('out/builds_table.json'))
ev = {}
for l in open('data/evaluated.jsonl'):
    r = json.loads(l); ev[r['hash']] = r['tags']['items']
codes = {}
for l in open('data/builds.jsonl'):
    b = json.loads(l); c = b.get('code') or ''
    if c.strip(): codes[hashlib.sha256(c.strip().encode()).hexdigest()[:32]] = c
scan_s = {}
for l in open('out_scan.jsonl'):
    r = json.loads(l)
    for x in r['res']:
        if x['label'] == 'scan' and isinstance(x['r'], dict):
            for row in x['r'].get('rows', []): scan_s[(r['id'], row['g'], row['name'])] = row['s']
cands = list(csv.DictReader(open('out/antimeta_candidates.csv')))
MAXC = int(sys.argv[1]) if len(sys.argv) > 1 else 14
PER = int(sys.argv[2]) if len(sys.argv) > 2 else 3
picked = []; seen_u = defaultdict(int)
def slot_for(h, u):
    sl = SLOT.get(UQ[u]['slot'])
    if not sl: return None
    items = {i['slot']: i for i in ev.get(h, [])}
    for s in sl:
        if s not in items or items[s]['rarity'] != 'UNIQUE': return s
    return None
jobs = []
for kind, quota in (('unique-skill', MAXC), ('unique-unique', MAXC // 2)):
    k = 0
    for c in cands:
        if c['kind'] != kind or k >= quota: continue
        if kind == 'unique-skill':
            u, target = c['a'], c['b']
            pool = [b for b in T if b['skill'] == target and b['resid'] is not None and b['dps'] > 0]
        else:
            # equip the less common one into builds that hold the other
            a, bq = c['a'], c['b']
            u, hold = (bq, a) if int(c['n_a_corpus']) >= int(c['n_b_corpus']) else (a, bq)
            pool = [b for b in T if hold in b['uniques'] and b['resid'] is not None and b['dps'] > 0]
            target = hold
        if u not in UQ or UQ[u]['slot'] not in SLOT or seen_u[u] >= 2: continue
        pool.sort(key=lambda b: b['resid'])
        pick = []
        for b in [pool[len(pool) // 2], pool[-1], pool[len(pool) // 4], pool[0]] if pool else []:
            if b not in pick and slot_for(b['hash'], u): pick.append(b)
        pick = pick[:PER]
        if not pick: continue
        k += 1; seen_u[u] += 1
        for b in pick:
            g = b['g']; s = scan_s.get((b['hash'], g, b['skill']), 1); sl = slot_for(b['hash'], u)
            ops = [{'op': 'call', 'm': 'set_main_skill', 'p': {'index': g}},
                   {'op': 'call', 'm': 'set_main_skill_options', 'p': {'groupIndex': g, 'mainActiveSkill': s}},
                   {'op': 'stats', 'label': 'base'},
                   {'op': 'callstats', 'm': 'item_db_equip', 'p': {'db': 'unique', 'name': u + ', ' + UQ[u]['base'], 'slotName': sl}, 'label': 'after'}]
            jobs.append({'id': b['hash'], 'cand': f'{kind}:{u}->{target}', 'unique': u, 'target': target, 'slot': sl, 'code': codes[b['hash']], 'ops': ops})
meta = {}
with open('jobs_wif.jsonl', 'w') as f:
    for i, j in enumerate(jobs):
        j['id'] = j['id'] + '#' + str(i); meta[j['id']] = {k: v for k, v in j.items() if k not in ('code', 'ops')}
        f.write(json.dumps(j) + '\n')
json.dump(meta, open('jobs_wif_meta.json', 'w'))
print(len(jobs), 'jobs', len({j['cand'] for j in jobs}), 'cands')
