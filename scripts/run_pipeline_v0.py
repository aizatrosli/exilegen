#!/usr/bin/env python3
"""Run the full pipeline on the POC 1-button builds (constraint one_button) and a few mixed corpus builds
(unconstrained) with optimise_many; write pipeline.csv, builds.jsonl (exported codes) and results.jsonl.

    python3 scripts/run_pipeline_v0.py --out /mnt/project-files/poc/v0 --workers 2 --budget 4000

Env: EXG_ENGINE_BIN, EXG_POB_ROOT, EXG_CORPUS (default /mnt/project-files/corpus/poe2/).
"""
import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exilegen.optimise.pipeline import optimise_many  # noqa: E402

POC_ONE_BUTTON = [237, 523, 197, 350, 360, 321, 223, 1231]     # tools/poc/one-button "opt" builds
POC_CONVERT = [1134, 74]                                        # tools/poc/one-button conversions
MIXED = [185, 296, 407, 555, 777, 962]                          # Barrage, Whirling Slash, Wolf Pack, Flameblast, Tornado Shot, Grim Pillars


def corpus_rows(corpus, lines):
    want = set(lines)
    out = {}
    with open(os.path.join(corpus, "builds.jsonl")) as f:
        for i, row in enumerate(f, 1):
            if i in want:
                out[i] = json.loads(row)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--budget", type=int, default=4000)
    ap.add_argument("--corpus", default=os.environ.get("EXG_CORPUS", "/mnt/project-files/corpus/poe2/"))
    ap.add_argument("--convert", action="store_true", help="also run the 2 POC conversions")
    ap.add_argument("--only", default="", help="comma list of corpus lines")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    sets = [(ln, "poc_one_button", ["one_button"]) for ln in POC_ONE_BUTTON]
    if args.convert:
        sets += [(ln, "poc_convert", ["one_button"]) for ln in POC_CONVERT]
    sets += [(ln, "mixed", []) for ln in MIXED]
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        sets = [s for s in sets if s[0] in keep]
    rows = corpus_rows(args.corpus, [s[0] for s in sets])
    jobs = [{"id": f"{kind}:{ln}", "code": rows[ln]["code"], "constraints": cons} for ln, kind, cons in sets]
    meta = {f"{kind}:{ln}": rows[ln] for ln, kind, _ in sets}
    t0 = time.time()

    def prog(done, n):
        print(f"[{time.time() - t0:6.0f}s] {done}/{n} done", flush=True)

    res = optimise_many(jobs, workers=args.workers, budget=args.budget, progress=prog)
    wall = time.time() - t0
    with open(os.path.join(args.out, "results.jsonl"), "w") as f:
        for r in res:
            f.write(json.dumps(r) + "\n")
    cols = ["id", "class", "main_skill", "constraints", "start_feasible", "end_feasible",
            "pin_honest_start", "pin_honest_end", "pin_x", "map_honest_start", "map_honest_end", "map_x",
            "pin_raw_start", "pin_raw_end", "ehp_start", "ehp_end", "maxhit_start", "maxhit_end",
            "move_start", "move_end", "obj_start", "obj_end", "gain_pct", "evals", "seconds", "steps", "changes",
            "unverified_end", "violations_end", "error"]
    with open(os.path.join(args.out, "pipeline.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in res:
            m = meta.get(r.get("id"), {})
            s, e = r.get("start") or {}, r.get("end") or {}
            sp, ep = s.get("profiles") or {}, e.get("profiles") or {}

            def g(d, p, k):
                return (d.get(p) or {}).get(k)

            def x(a, b):
                return round(b / a, 3) if a and b is not None else None
            w.writerow({
                "id": r.get("id"), "class": m.get("class"), "main_skill": m.get("main_skill"),
                "constraints": ";".join(r.get("constraints") or []),
                "start_feasible": s.get("feasible"), "end_feasible": e.get("feasible"),
                "pin_honest_start": g(sp, "pinnacle_v1", "dps_honest"), "pin_honest_end": g(ep, "pinnacle_v1", "dps_honest"),
                "pin_x": x(g(sp, "pinnacle_v1", "dps_honest"), g(ep, "pinnacle_v1", "dps_honest")),
                "map_honest_start": g(sp, "mapping_v1", "dps_honest"), "map_honest_end": g(ep, "mapping_v1", "dps_honest"),
                "map_x": x(g(sp, "mapping_v1", "dps_honest"), g(ep, "mapping_v1", "dps_honest")),
                "pin_raw_start": g(sp, "pinnacle_v1", "dps_raw"), "pin_raw_end": g(ep, "pinnacle_v1", "dps_raw"),
                "ehp_start": g(sp, "pinnacle_v1", "ehp"), "ehp_end": g(ep, "pinnacle_v1", "ehp"),
                "maxhit_start": g(sp, "pinnacle_v1", "max_hit_min"), "maxhit_end": g(ep, "pinnacle_v1", "max_hit_min"),
                "move_start": g(sp, "pinnacle_v1", "movement_speed"), "move_end": g(ep, "pinnacle_v1", "movement_speed"),
                "obj_start": s.get("objective"), "obj_end": e.get("objective"),
                "gain_pct": round(100 * r["gain"], 1) if isinstance(r.get("gain"), (int, float)) else r.get("gain"),
                "evals": r.get("evals"), "seconds": round(r.get("seconds") or 0, 1),
                "steps": " ".join(f"{st['name']}:{len(st.get('changes') or [])}" for st in r.get("steps") or []),
                "changes": " | ".join(r.get("changes") or [])[:1500],
                "unverified_end": ";".join(sorted({f for p in ep.values() for f in p.get("unverified") or []})),
                "violations_end": ";".join(f"{v.get('rule')}:{v.get('entry')}" for v in e.get("violations") or []),
                "error": r.get("error"),
            })
    with open(os.path.join(args.out, "builds.jsonl"), "w") as f:
        for r in res:
            m = meta.get(r.get("id"), {})
            f.write(json.dumps({"id": r.get("id"), "class": m.get("class"), "main_skill": m.get("main_skill"),
                                "constraints": r.get("constraints"), "feasible": (r.get("end") or {}).get("feasible"),
                                "objective_start": (r.get("start") or {}).get("objective"),
                                "objective_end": (r.get("end") or {}).get("objective"),
                                "changes": r.get("changes"), "code": r.get("code")}) + "\n")
    print(f"{len(res)} builds, {wall:.0f}s wall, {args.workers} engines, "
          f"{sum(r.get('evals') or 0 for r in res)} evals")


if __name__ == "__main__":
    main()
