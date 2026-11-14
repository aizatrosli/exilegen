"""Per-build feature table: main skill (best-DPS group from engine scan if present), DPS, component sets."""
import json, math, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb import family

def dps_of(st):
    if not st: return 0.0
    return max(st.get('CombinedDPS') or 0, st.get('MinionCombinedDPS') or 0)

def load(scan_path='out_scan.jsonl'):
    scan = {}
    if os.path.exists(scan_path):
        for l in open(scan_path):
            r = json.loads(l)
            if r.get('err'): continue
            res = {x['label']: x['r'] for x in r['res']}
            scan[r['id']] = res
    builds = []
    for l in open('data/evaluated.jsonl'):
        r = json.loads(l)
        t, p = r['tags'], r['pinnacle_locked']
        st = p['stats']; mst = p.get('minionStats') or {}
        pob_main = (p.get('mainSkill') or {}).get('name')
        pob_dps = max(st.get('CombinedDPS') or 0, mst.get('CombinedDPS') or 0)
        groups = t['socketGroups']
        best = dict(skill=pob_main, dps=pob_dps, g=next((g['index'] for g in groups if g['main']), None), src='pob_main')
        sc = scan.get(r['hash'])
        if sc and 'scan' in sc and isinstance(sc['scan'], dict):
            allow = set(r['meta'].get('main_skills') or []) | {r['meta']['main_skill']}
            for row in sc['scan'].get('rows', []):
                if row['name'] not in allow: continue
                d = dps_of(row.get('r', row))
                if d > best['dps'] * 1.0001:
                    best = dict(skill=row['name'], dps=d, g=row['g'], src='scan')
        gmap = {g['index']: g for g in groups}
        mg = gmap.get(best['g']) or {}
        main_sup = [s for s in mg.get('supports', [])]
        # meta groups: triggered skill shares group; supports apply to all
        comps = set()
        comps |= {'S:' + s for s in main_sup}
        comps |= {'U:' + u for u in t['uniques'] or []}
        comps |= {'K:' + k['name'] for k in t['keystones'] or []}
        comps |= {'A:' + a['name'] for a in t['ascendancyNodes'] or [] if a['type'] == 'Notable'}
        comps |= {'N:' + n['name'] for n in t['notables'] or []}
        others = set()
        for g in groups:
            if not g['enabled']: continue
            for gm in g['gems']:
                if not gm.get('support') and gm.get('enabled', True) and gm['name'] != best['skill']:
                    others.add(gm['name'])
        comps |= {'G:' + o for o in others}
        all_sup = {s for g in groups if g['enabled'] for s in g['supports']}
        builds.append(dict(hash=r['hash'], asc=t['ascendancy'] or t['className'], cls=t['className'], meta_skill=r['meta']['main_skill'],
                           pob_main=pob_main, pob_dps=pob_dps, skill=best['skill'], dps=best['dps'], src=best['src'], main_group=mg,
                           main_sup=main_sup, comps=comps, all_sup=all_sup, uniques=list(t['uniques'] or []), granted=t['grantedSkills'] or [],
                           stratum=r['meta']['stratum'], ehp=st.get('TotalEHP') or 0, life=st.get('Life') or 0, es=st.get('EnergyShield') or 0,
                           unsupported=t.get('unsupportedModLines') or 0, items=t['items'], groups=groups,
                           hitdt=st.get('HitDamageByType') or {}, dot=st.get('TotalDotDPS') or 0, crit=st.get('CritChance') or 0))
    return builds

def add_residuals(builds, min_n=5):
    from statistics import median
    by = {}
    for b in builds:
        b['logdps'] = math.log10(b['dps']) if b['dps'] > 1 else None
        if b['logdps'] is not None: by.setdefault(b['skill'], []).append(b['logdps'])
    med = {k: median(v) for k, v in by.items() if len(v) >= min_n}
    for b in builds:
        b['resid'] = (b['logdps'] - med[b['skill']]) if (b['logdps'] is not None and b['skill'] in med) else None
    return med
