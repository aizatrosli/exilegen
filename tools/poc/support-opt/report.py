# Turn supportopt out.jsonl (+ jobs.jsonl) into results.csv, builds.jsonl and summary stats.
import csv, json, statistics, sys

jobs_p, out_p, dest = sys.argv[1], sys.argv[2], sys.argv[3].rstrip('/') + '/'
jobs = {json.loads(l)['id']: json.loads(l) for l in open(jobs_p) if l.strip()}
rows, recs = [], []
for l in open(out_p):
    r = json.loads(l)
    j = jobs[r['id']]
    o = r.get('opt') or {}
    err = r.get('err') or o.get('err')
    rec = {"id": r['id'], "hash": j['hash'], "skill": j['skill'], "asc": j.get('asc'), "group": j['g'],
           "n_skill_builds": j.get('n_skill_builds'), "error": err, "code_before": j['code']}
    if not err:
        base = o['base']
        b, s = o['best'], o['strict']
        ver = (r.get('verify') or {}).get('CombinedDPS')
        vmin = (r.get('verify') or {}).get('MinionCombinedDPS')
        ex = r.get('exports') or {}
        rec.update({
            "base_dps": base, "strict_dps": s['dps'], "best_dps": b['dps'],
            "strict_gain_pct": (s['dps'] / base - 1) * 100 if base > 0 else None,
            "best_gain_pct": (b['dps'] / base - 1) * 100 if base > 0 else None,
            "supports_before": o['before'], "supports_after_strict": s['names'], "supports_after_best": b['names'],
            "best_case_conditional_new": [c['name'] + " (" + c['why'] + ")" for c in b['new_conditional']],
            "before_conditional": o['before_conditional'], "removal_loss_pct": o['removal_loss'],
            "swaps_strict": s['log'], "swaps_best": b['log'], "guard_rejections": s['rejected'] + b['rejected'],
            "evals": o['evals'], "full_evals": o['full_evals'], "candidates": o['n_cands'], "seconds": r['secs'],
            "ehp_before": o['base_full']['ehp'], "ehp_after_strict": s.get('ehp'),
            "verify_reimport_dps": max(ver or 0, vmin or 0),
            "code_after": (ex.get('strict') or {}).get('code'), "code_after_best_case": (ex.get('best') or {}).get('code'),
        })
    recs.append(rec)
    rows.append({k: (" | ".join(v) if isinstance(v, list) and v and isinstance(v[0], str) else v) for k, v in rec.items()
                 if k in ("id", "skill", "asc", "group", "n_skill_builds", "error", "base_dps", "strict_dps", "best_dps",
                          "strict_gain_pct", "best_gain_pct", "supports_before", "supports_after_strict",
                          "supports_after_best", "best_case_conditional_new", "evals", "full_evals", "seconds",
                          "verify_reimport_dps", "hash")})
with open(dest + 'builds.jsonl', 'w') as f:
    for rec in recs: f.write(json.dumps(rec) + "\n")
cols = ["id", "skill", "asc", "group", "n_skill_builds", "base_dps", "strict_dps", "strict_gain_pct", "best_dps",
        "best_gain_pct", "supports_before", "supports_after_strict", "supports_after_best", "best_case_conditional_new",
        "evals", "full_evals", "seconds", "verify_reimport_dps", "error", "hash"]
with open(dest + 'results.csv', 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
    for r in rows:
        w.writerow({k: (round(r[k], 1) if isinstance(r.get(k), float) else r.get(k, "")) for k in cols})
ok = [r for r in recs if not r['error'] and r['base_dps'] > 0]
def q(xs, p): xs = sorted(xs); return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]
sg = [r['strict_gain_pct'] for r in ok]; bg = [r['best_gain_pct'] for r in ok]
print(json.dumps({"n": len(recs), "ok": len(ok), "errors": [(r['skill'], r['error']) for r in recs if r['error']],
                  "strict_median": statistics.median(sg), "strict_p90": q(sg, .9), "best_median": statistics.median(bg),
                  "best_p90": q(bg, .9), "n_improved_strict": sum(g > 0.05 for g in sg),
                  "evals_total": sum(r['evals'] for r in ok), "secs_total": sum(r['seconds'] for r in ok),
                  "secs_median": statistics.median(r['seconds'] for r in ok),
                  "verify_mismatch": [(r['skill'], r['strict_dps'], r['verify_reimport_dps']) for r in ok
                                      if abs(r['verify_reimport_dps'] - r['strict_dps']) > 1e-3 * max(1, r['strict_dps'])]},
                 indent=1))
for r in ok:
    print(f"{r['skill'][:28]:28} {r['base_dps']:>12,.0f} -> {r['strict_dps']:>12,.0f} ({r['strict_gain_pct']:+6.1f}%) best {r['best_gain_pct']:+6.1f}%  "
          f"{r['evals']} evals {r['seconds']:.0f}s\n   before: {r['supports_before']}\n   strict: {r['supports_after_strict']}\n   best:   {r['supports_after_best']} cond={r['best_case_conditional_new']}")
