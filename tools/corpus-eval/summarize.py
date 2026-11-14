# Summary of evaluated.jsonl / errors.jsonl: counts, error causes, tag sanity.
import json, sys, collections, re
d = sys.argv[1] if len(sys.argv) > 1 else 'data/corpus/poe2/'
def rd(f):
    out = []
    for l in open(f):
        try: out.append(json.loads(l))
        except Exception: pass
    return out
ev = rd(d + 'evaluated.jsonl'); er = rd(d + 'errors.jsonl')
print('evaluated', len(ev), 'errors', len(er))
c = collections.Counter()
for e in er:
    msg = re.sub(r'stack traceback.*', '', e['error'], flags=re.S)
    msg = re.sub(r'\d+', 'N', msg)[:140]
    c[(e['stage'], msg)] += 1
for k, v in c.most_common(12): print(v, k)
if not ev: sys.exit()
C = collections.Counter
print('tree', C(r['pob']['treeVersion'] for r in ev).most_common(), 'target', C(r['pob']['targetVersion'] for r in ev).most_common())
print('asc', C(r['tags']['ascendancy'] for r in ev).most_common(12))
print('main', C((r['as_exported']['mainSkill'] or {}).get('name') for r in ev).most_common(12))
print('oneButton', C(r['tags']['oneButton'] for r in ev), 'noCast', C(r['tags']['noCast'] for r in ev))
print('zero dps exported', sum(1 for r in ev if not r['as_exported']['stats'].get('CombinedDPS') and not (r['as_exported']['minionStats'] or {}).get('CombinedDPS')))
print('unsupported lines median', sorted(r['tags']['unsupportedModLines'] for r in ev)[len(ev)//2], 'max', max(r['tags']['unsupportedModLines'] for r in ev))
print('cfg nondefault mean', sum(r['playerConfig']['nonDefaultCount'] for r in ev)/len(ev), 'boss', C(r['playerConfig']['enemyIsBoss'] for r in ev))
cv = C(k for r in ev for k in r['playerConfig']['nonDefault']); print('top cfg vars', cv.most_common(15))
ratio = sorted(r['pinnacle_locked']['stats'].get('CombinedDPS', 0) / r['as_exported']['stats']['CombinedDPS'] for r in ev if r['as_exported']['stats'].get('CombinedDPS'))
if ratio: print('locked/exported DPS ratio p10/50/90', ratio[len(ratio)//10], ratio[len(ratio)//2], ratio[len(ratio)*9//10])
print('ms median', sorted(r['timingMs']['total'] for r in ev)[len(ev)//2])
