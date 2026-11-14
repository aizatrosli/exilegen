#!/usr/bin/env python3
"""Micro-benchmark of exg-engine ops: ms per op (load, stats, whatif node, whatif gem, whatif_many)
on a few corpus builds, one engine.

    EXG_ENGINE_BIN=... EXG_POB_ROOT=... python3 scripts/bench_engine.py [--builds 3] [--reps 20]
"""
import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from exilegen.engine import Engine  # noqa: E402


def corpus_codes(path, n, skip):
    out = []
    with open(path) as f:
        for i, line in enumerate(f):
            if i < skip:
                continue
            out.append(json.loads(line)["code"])
            if len(out) >= n:
                break
    return out


def timed(fn, reps):
    ts = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    return statistics.median(ts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=os.environ.get("EXG_CORPUS", "/mnt/project-files/corpus/poe2/") + "builds.jsonl")
    ap.add_argument("--builds", type=int, default=3)
    ap.add_argument("--skip", type=int, default=0)
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--profile", default="pinnacle_v1")
    a = ap.parse_args()
    codes = corpus_codes(a.corpus, a.builds, a.skip)
    rows = []
    with Engine() as e:
        print(f"engine boot {e.info.get('boot_ms')} ms")
        for code in codes:
            t = time.perf_counter()
            summ = e.load(code=code, profile=a.profile)
            load_ms = (time.perf_counter() - t) * 1000
            r = {"build": f"{summ['className']}/{summ['ascendancy']}", "load(auto main)": load_ms}
            r["load(main=pob)"] = timed(lambda: e.load(code=code, profile=a.profile, main="pob"), 2)
            r["stats core"] = timed(lambda: e.stats(fields=["core"]), a.reps)
            r["stats dps"] = timed(lambda: e.stats(fields=["dps"]), a.reps)
            r["stats all"] = timed(lambda: e.stats(), max(2, a.reps // 5))
            tr = e.tree(max_dist=2)
            nodes = [f["id"] for f in tr["frontier"] if not tr["nodes"][str(f["id"])].get("asc")][:a.reps]
            it = iter(nodes * 2)
            r["whatif node core"] = timed(lambda: e.whatif(add_nodes=[next(it)]), len(nodes))
            it = iter(nodes * 2)
            r["whatif node dps"] = timed(lambda: e.whatif(add_nodes=[next(it)], fields=["dps"]), len(nodes))
            main = summ["main"]["group"]
            vs = e.valid_supports(main)
            if vs["slots"]:
                slot = vs["slots"][0]
                cands = [c["id"] for c in vs["supports"] if not c["socketed"] and all(x["slot"] == slot for x in c["conflicts"])]
                it = iter(cands * 2)
                r["whatif gem dps"] = timed(lambda: e.whatif(gem_changes=[{"group": main, "slot": slot, "gem": next(it)}], fields=["dps"]), min(a.reps, len(cands)))
                var = [{"gem_changes": [{"group": main, "slot": slot, "gem": c}]} for c in cands[:50]]
                t = time.perf_counter()
                e.whatif_many(var, fields=["dps"])
                r["whatif_many gem /variant"] = (time.perf_counter() - t) * 1000 / len(var)
            var = [{"add_nodes": [n]} for n in nodes]
            t = time.perf_counter()
            e.whatif_many(var, fields=["dps"])
            r["whatif_many node /variant"] = (time.perf_counter() - t) * 1000 / len(var)
            tok = e.snapshot()
            r["apply node"] = timed(lambda: e.apply(add_nodes=[nodes[0]], fields=["dps"]), 1)
            r["restore"] = timed(lambda: e.restore(tok), 3)
            r["tree"] = timed(lambda: e.tree(), 2)
            r["skills"] = timed(lambda: e.skills(), 3)
            r["valid_supports"] = timed(lambda: e.valid_supports(main), 3)
            r["export"] = timed(lambda: e.export(), 2)
            rows.append(r)
    keys = [k for k in rows[0] if k != "build"]
    for r in rows:
        for k in keys:
            r.setdefault(k, float("nan"))
    w = max(len(k) for k in keys)
    print(f"{'ms (median)':<{w}}  " + "  ".join(f"{r['build'][:24]:>24}" for r in rows))
    for k in keys:
        print(f"{k:<{w}}  " + "  ".join(f"{r[k]:>24.1f}" for r in rows))


if __name__ == "__main__":
    main()
