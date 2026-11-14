"""Score mechanics-graph chains in PoB: does the formula's ranking match what the engine says?

Each chain becomes a skeleton (the generator's mechanics_graph source), is built from nothing by the synthesizer
and scored by the honest scorer. One JSON line per chain goes to <out>/chain_scores.jsonl; a re-run skips chains
that already have a line, so a run can be stopped and resumed.

    python3 -m exilegen.builder.chainscore --out runs/chains --sample 150 --workers 3 --minutes 100
    python3 -m exilegen.builder.chainscore --out runs/chains --report

`--sample N` takes an even spread over the formula's ranking (not the top N), so the comparison covers weak
chains too. `--sample 0` scores every chain in the pool. Needs EXG_ENGINE_BIN and EXG_POB_ROOT.
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import random
import time

from . import catalog as catmod
from . import graphx


def pool_chains(top: int = 100000) -> list[dict]:
    """Every chain the graph export can rank, best first, with tempo and leveling attached."""
    cat = catmod.load()
    req, _ = graphx.Requirements.from_dict({"top": top})
    return graphx.mechanics(cat, req)["chains"]


def spread(chains: list[dict], n: int) -> list[dict]:
    if n <= 0 or n >= len(chains):
        return list(chains)
    step = len(chains) / n
    picked = [chains[int(i * step)] for i in range(n)]
    for rank, c in enumerate(chains):
        c["rank"] = rank
    return picked


def _worker(wid: int, jobs, results, budget: int, seed: int):
    from exilegen.engine import Engine
    from .skeletons import Generator
    from .synth import make_scorer, synthesize, summary_row
    gen = Generator()
    engine, scorer = Engine(), make_scorer()
    while True:
        ch = jobs.get()
        if ch is None:
            break
        row = {"id": ch["id"], "rank": ch.get("rank"), "kind": ch["kind"], "main_name": ch["main_name"],
               "score": ch["score"], "score_raw": ch.get("score_raw"), "fit": ch["fit"], "novelty": ch["novelty"],
               "tempo": (ch.get("tempo") or {}).get("tempo"), "online_level": (ch.get("leveling") or {}).get("online_level"),
               "level_factor": (ch.get("leveling") or {}).get("factor"), "uniques": ch["uniques"],
               "keystones": ch["keystones"], "asc_nodes": ch["asc_nodes"], "thesis": ch["thesis"]}
        t0 = time.time()
        try:
            sk = gen._src_mechanics_graph(random.Random(f"{seed}:{ch['id']}"), [], chain=ch)
            if sk is None:
                row.update(ok=False, error="no skeleton for this chain")
            else:
                res = synthesize(sk, engine, scorer, budget=budget, seed=seed)
                s = res.score or {}
                row.update(summary_row(res))
                row.update(id=ch["id"], skeleton_id=sk.id, ascendancy=getattr(sk, "ascendancy", None),
                           unverified=bool(s.get("unverified")), code=res.code, notes=(res.notes or [])[-3:])
        except Exception as e:  # a broken chain must not stop the run
            row.update(ok=False, error=f"{type(e).__name__}: {e}"[:300])
            try:
                engine = Engine()
            except Exception:
                pass
        row["seconds"] = round(time.time() - t0, 1)
        results.put(row)


def run(out: str, sample: int, workers: int, minutes: float, budget: int, seed: int) -> None:
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "chain_scores.jsonl")
    done = set()
    if os.path.exists(path):
        done = {json.loads(l)["id"] for l in open(path) if l.strip()}
    chains = pool_chains()
    picked = [c for c in spread(chains, sample) if c["id"] not in done]
    print(f"pool {len(chains)} chains, {len(picked)} to score ({len(done)} already done), {workers} workers", flush=True)
    jobs, results = mp.Queue(), mp.Queue()
    procs = [mp.Process(target=_worker, args=(i, jobs, results, budget, seed), daemon=True) for i in range(workers)]
    for p in procs:
        p.start()
    todo, flying, t0, n = list(picked), 0, time.time(), 0
    deadline = t0 + minutes * 60
    with open(path, "a") as f:
        while todo or flying:
            while todo and flying < workers and time.time() < deadline:
                jobs.put(todo.pop(0))
                flying += 1
            if not flying:
                break
            row = results.get()
            flying -= 1
            n += 1
            f.write(json.dumps(row) + "\n")
            f.flush()
            print(f"[{n}/{len(picked)}] {row.get('seconds')}s ok={row.get('ok')} feasible={row.get('feasible')} "
                  f"dps={row.get('dps_honest')} {row['main_name']} ({row['kind']}) {row.get('error') or ''}", flush=True)
    for _ in procs:
        jobs.put(None)
    print(f"scored {n} chains in {(time.time() - t0) / 60:.1f} min, {len(todo)} left", flush=True)


# ---------------------------------------------------------------------------------------------- report
def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return round(cov / math.sqrt(va * vb), 3) if va and vb else None


def report(out: str) -> dict:
    rows = [json.loads(l) for l in open(os.path.join(out, "chain_scores.jsonl")) if l.strip()]
    built = [r for r in rows if r.get("ok")]
    dps = [r for r in built if (r.get("dps_honest") or 0) > 0]
    feas = [r for r in built if r.get("feasible")]
    log = lambda r: math.log10(r["dps_honest"])
    rep = {"scored": len(rows), "built": len(built), "with_dps": len(dps), "feasible": len(feas),
           "verified_feasible": sum(1 for r in feas if not r.get("unverified")),
           "median_seconds": sorted(r["seconds"] for r in rows)[len(rows) // 2] if rows else None,
           "errors": {}}
    for r in rows:
        if not r.get("ok"):
            k = (r.get("error") or "unknown")[:60]
            rep["errors"][k] = rep["errors"].get(k, 0) + 1
    rep["spearman_vs_log_dps"] = {k: spearman([r[k] for r in dps if r.get(k) is not None], [log(r) for r in dps if r.get(k) is not None])
                                  for k in ("score", "score_raw", "fit", "novelty", "tempo", "level_factor")}
    ok_flag = [1.0 if r.get("ok") and (r.get("dps_honest") or 0) > 0 else 0.0 for r in rows]
    rep["spearman_vs_built"] = {k: spearman([r[k] for r in rows], ok_flag) for k in ("score", "fit", "novelty")}
    thirds = {}
    srt = sorted(rows, key=lambda r: -r["score"])
    for name, part in (("top", srt[: len(srt) // 3]), ("middle", srt[len(srt) // 3: 2 * len(srt) // 3]), ("bottom", srt[2 * len(srt) // 3:])):
        d = sorted(r["dps_honest"] for r in part if r.get("ok") and (r.get("dps_honest") or 0) > 0)
        thirds[name] = {"n": len(part), "with_dps": len(d), "feasible": sum(1 for r in part if r.get("feasible")),
                        "median_dps": round(d[len(d) // 2]) if d else None}
    rep["by_formula_third"] = thirds
    kinds = {}
    for r in rows:
        k = kinds.setdefault(r["kind"], {"n": 0, "with_dps": 0, "feasible": 0, "dps": []})
        k["n"] += 1
        if r.get("ok") and (r.get("dps_honest") or 0) > 0:
            k["with_dps"] += 1
            k["dps"].append(r["dps_honest"])
        k["feasible"] += bool(r.get("feasible"))
    rep["by_kind"] = {k: {**{x: v[x] for x in ("n", "with_dps", "feasible")},
                          "median_dps": round(sorted(v["dps"])[len(v["dps"]) // 2]) if v["dps"] else None} for k, v in kinds.items()}
    rep["best"] = [{k: r.get(k) for k in ("main_name", "kind", "rank", "score", "dps_honest", "ehp", "feasible", "unverified", "uniques", "thesis")}
                   for r in sorted(dps, key=lambda r: -r["dps_honest"])[:15]]
    json.dump(rep, open(os.path.join(out, "report.json"), "w"), indent=1)
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="build and score mechanics-graph chains in PoB")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample", type=int, default=150, help="chains to score, spread evenly over the ranking (0 = all)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--minutes", type=float, default=100)
    ap.add_argument("--budget", type=int, default=6000, help="engine evaluations per chain")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--report", action="store_true", help="only summarise an existing run")
    a = ap.parse_args(argv)
    if not a.report:
        run(a.out, a.sample, a.workers, a.minutes, a.budget, a.seed)
    print(json.dumps(report(a.out), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
