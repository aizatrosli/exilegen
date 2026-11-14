"""Task 5 jobs: for selected builds, set main group = best-DPS group, measure removal loss of each socketed support, and PoB's sortByDps ranking."""
import json, sys, hashlib, random
from collections import defaultdict
tmpl = open('engine-exp/remloss.lua.tmpl').read()
T = json.load(open('out/builds_table.json'))
codes = {}
for l in open('data/builds.jsonl'):
    b = json.loads(l); c = b.get('code') or ''
    if c.strip(): codes[hashlib.sha256(c.strip().encode()).hexdigest()[:32]] = c
scan = {}
for l in open('out_scan.jsonl'):
    r = json.loads(l)
    for x in r['res']:
        if x['label'] == 'scan' and isinstance(x['r'], dict): scan[r['id']] = x['r']
by = defaultdict(list)
for b in T:
    if b['resid'] is not None and b['g'] and b['dps'] > 1000 and len(b['sup']) >= 3: by[b['skill']].append(b)
random.seed(1)
sel = []
top = sorted(by, key=lambda k: -len(by[k]))[:int(sys.argv[1]) if len(sys.argv) > 1 else 15]
per = int(sys.argv[2]) if len(sys.argv) > 2 else 3
for sk in top:
    bs = sorted(by[sk], key=lambda b: b['resid'])
    pick = [bs[len(bs) // 2], bs[-1], bs[len(bs) // 4]][:per]   # median, best, lower-quartile
    sel += pick
with open('jobs_sup.jsonl', 'w') as f:
    for b in sel:
        g = b['g']
        s = next((row['s'] for row in scan.get(b['hash'], {}).get('rows', []) if row['g'] == g and row['name'] == b['skill']), 1)
        ops = [{'op': 'call', 'm': 'set_main_skill', 'p': {'index': g}},
               {'op': 'call', 'm': 'set_main_skill_options', 'p': {'groupIndex': g, 'mainActiveSkill': s}},
               {'op': 'stats', 'label': 'base'},
               {'op': 'eval', 'lua': tmpl.replace('__G__', str(g)), 'label': 'remloss'},
               {'op': 'call', 'm': 'list_valid_supports', 'p': {'groupIndex': g, 'sortByDps': True}, 'keep': True, 'label': 'valid'}]
        f.write(json.dumps({'id': b['hash'], 'skill': b['skill'], 'code': codes[b['hash']], 'ops': ops}) + '\n')
print(len(sel), 'jobs;', top)
