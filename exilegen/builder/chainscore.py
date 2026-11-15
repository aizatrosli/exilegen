"""Score mechanics-graph chains in PoB: does the formula's ranking match what the engine says?

Each chain becomes a skeleton (the generator's mechanics_graph source), is built from nothing by the synthesizer
and scored by the honest scorer. One JSON line per chain goes to <out>/chain_scores.jsonl; a re-run skips chains
that already have a line, so a run can be stopped and resumed.

With `--baseline`, every chain also gets a baseline: the same main skill, ascendancy, weapon and defence built
without the chain's uniques, keystones, ascendancy notables and helper skills (baselines.jsonl, one per distinct
setup). The chain's gain is its honest DPS over its baseline's, which is the link's own contribution.

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
from . import states as statesmod


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


def _unverified(score: dict | None) -> bool:
    """The scorer could not check part of the build (same reading as archive.py: an "unverified" flag, or
    verified False). It used to read a key the scorer never writes, so every row said verified."""
    s = score or {}
    return s.get("verified") is False or any(str(f).startswith("unverified") or ":unverified" in str(f)
                                             for f in s.get("flags") or [])


def _worker(wid: int, jobs, results, budget: int, seed: int):
    from exilegen.engine import Engine
    from .skeletons import Generator
    from .synth import make_scorer, synthesize, summary_row
    from . import reactions as rxmod
    gen = Generator()
    engine, scorer = Engine(), make_scorer()
    while True:
        ch = jobs.get()
        if ch is None:
            break
        if "base_key" in ch and "kind" not in ch:      # a baseline job
            row = {"base_key": ch["base_key"], "main_name": ch["main_name"], "baseline": True}
            t0 = time.time()
            try:
                sk = gen.assemble(random.Random(f"{seed}:{ch['base_key']}"), ch["main_skill"], source="mechanics_graph",
                                  thesis="baseline without the chain", constraints=[], asc=ch["ascendancy"],
                                  weapon=ch["weapon"], defence=ch["defence"])
                if sk is None:
                    row.update(ok=False, error="no baseline skeleton")
                else:
                    res = synthesize(sk, engine, scorer, budget=budget, seed=seed)
                    row.update(summary_row(res))
                    row.update(skeleton_id=sk.id, unverified=_unverified(res.score), code=res.code,
                               uniques=[u["name"] for u in sk.uniques])
            except Exception as e:
                row.update(ok=False, error=f"{type(e).__name__}: {e}"[:300])
                try:
                    engine = Engine()
                except Exception:
                    pass
            row["seconds"] = round(time.time() - t0, 1)
            results.put(row)
            continue
        row = {"id": ch["id"], "rank": ch.get("rank"), "kind": ch["kind"], "main_name": ch["main_name"],
               "score": ch["score"], "score_raw": ch.get("score_raw"), "fit": ch["fit"], "novelty": ch["novelty"],
               "tempo": (ch.get("tempo") or {}).get("tempo"), "online_level": (ch.get("leveling") or {}).get("online_level"),
               "level_factor": (ch.get("leveling") or {}).get("factor"), "uniques": ch["uniques"],
               "keystones": ch["keystones"], "asc_nodes": ch["asc_nodes"], "thesis": ch["thesis"]}
        t0 = time.time()
        try:
            rng = random.Random(f"{seed}:{ch['id']}")
            # a reaction chain is the main skill by hand plus its meta gem and spell (reactions.py)
            sk = rxmod.skeleton(gen, rng, ch) if ch.get("reaction") else gen._src_mechanics_graph(rng, [], chain=ch)
            if sk is None:
                row.update(ok=False, error=(gen.last_reject if ch.get("reaction") else None) or "no skeleton for this chain")
            else:
                # a chain build keeps the chain's uniques: the unique trial would swap them for a tuned rare
                # whenever that scores higher, and the build would then measure the skill without the chain
                res = synthesize(sk, engine, scorer, budget=budget, seed=seed, unique_trial="off",
                                 **({"polish": rxmod.POLISH, "exclude_supports": rxmod.event_killers(ch)}
                                    if ch.get("reaction") else {}))
                s = res.score or {}
                row.update(summary_row(res))
                row.update(id=ch["id"], skeleton_id=sk.id, ascendancy=sk.ascendancy, main_skill=ch["main_skill"],
                           weapon=sk.weapon, defence=sk.defence,
                           base_key="|".join(str(x) for x in (ch["main_skill"], sk.ascendancy, sk.weapon, sk.defence)),
                           unverified=_unverified(s), code=res.code, notes=(res.notes or [])[-3:])
        except Exception as e:  # a broken chain must not stop the run
            row.update(ok=False, error=f"{type(e).__name__}: {e}"[:300])
            try:
                engine = Engine()
            except Exception:
                pass
        row["seconds"] = round(time.time() - t0, 1)
        results.put(row)


# ---------------------------------------------------------------------------------------------- states
# The locked profiles turn no conditional state on, so a chain whose point is "X makes the state, Y pays off on it"
# gets the cost of its parts and none of the payoff. This pass loads each finished build again and reads PoB's DPS
# with the chain's states switched on: for the chain build (it makes the state) and for its baseline (to see what
# the state alone is worth to a build without the payoff part). Which config values a token switches comes from
# the state registry (states.TOKENS); tokens without a switch there (damage types, minions, attributes) are
# worked out by PoB itself.
STATE_PROFILE = "pinnacle_v1"
_ENGINE = None


def state_vars(tokens: list[str], chain: dict | None = None) -> dict:
    """PoB config values that switch a chain's states on (states.TOKENS). The locked profile is a boss fight, so
    kill-gated states stay off; "spent Recently" counts come from the chain's own cost per second when known."""
    spent = ((chain or {}).get("tempo") or {}).get("cost_per_second")
    return statesmod.switch_for(tokens, clearing=False, spent_per_second=spent)


def _set_vars(engine, values: dict) -> None:
    lit = lambda v: "true" if v is True else "nil" if v is None else repr(v)
    body = " ".join(f'ct.input["{k}"] = {lit(v)}' for k, v in values.items())
    engine.lua('local ct = launch.main.modes["BUILD"].configTab ' + body +
               ' ct:BuildModList() ct:BuildModList() if EXG then EXG.state.dirty = true end return "ok"')


def _dps_pair(engine, code: str, values: dict) -> tuple[float, float]:
    """PoB DPS of a build with the states off and on (raw PoB DPS under the locked pinnacle profile)."""
    engine.ensure_loaded(code=code, profile=STATE_PROFILE)
    off = engine.stats(fields=["dps"])["dps"]["best"]
    _set_vars(engine, values)
    try:
        on = engine.stats(fields=["dps"])["dps"]["best"]
    finally:
        _set_vars(engine, {k: None for k in values})
    return off, on


def _state_job(job: dict) -> dict:
    global _ENGINE
    from exilegen.engine import Engine
    out = {"id": job["id"], "vars": sorted(job["vars"])}
    try:
        if _ENGINE is None:
            _ENGINE = Engine()
        out["chain_off"], out["chain_on"] = _dps_pair(_ENGINE, job["code"], job["vars"])
        if job.get("base_code"):
            out["base_off"], out["base_on"] = _dps_pair(_ENGINE, job["base_code"], job["vars"])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:200]
        _ENGINE = None
    return out


def states(out: str, workers: int) -> None:
    """Read PoB's DPS with each chain's states on, for the chain build and its baseline (states.jsonl)."""
    spath = os.path.join(out, "states.jsonl")
    done = {r["id"] for r in _read(spath)}
    base = {r["base_key"]: r for r in _read(os.path.join(out, "baselines.jsonl"))}
    pool = {c["id"]: c for c in pool_chains()}
    jobs = []
    for r in _read(os.path.join(out, "chain_scores.jsonl")):
        if r["id"] in done or not r.get("code") or r["id"] not in pool:
            continue
        v = state_vars(pool[r["id"]]["tokens"], pool[r["id"]])
        if v:
            jobs.append({"id": r["id"], "code": r["code"], "vars": v, "base_code": (base.get(r.get("base_key")) or {}).get("code")})
    print(f"states: {len(jobs)} chains with a switchable state ({len(done)} already done)", flush=True)
    if not jobs:
        return
    with mp.Pool(workers) as p, open(spath, "a") as f:
        for i, res in enumerate(p.imap_unordered(_state_job, jobs), 1):
            f.write(json.dumps(res) + "\n")
            f.flush()
            if i % 20 == 0:
                print(f"states {i}/{len(jobs)}", flush=True)


def _read(path: str) -> list[dict]:
    return [json.loads(l) for l in open(path) if l.strip()] if os.path.exists(path) else []


def _base_job(row: dict) -> dict:
    return {k: row.get(k) for k in ("base_key", "main_skill", "main_name", "ascendancy", "weapon", "defence")}


def run(out: str, sample: int, workers: int, minutes: float, budget: int, seed: int, baseline: bool = False) -> None:
    os.makedirs(out, exist_ok=True)
    path, bpath = os.path.join(out, "chain_scores.jsonl"), os.path.join(out, "baselines.jsonl")
    prior = _read(path)
    done = {r["id"] for r in prior}
    based = {r["base_key"] for r in _read(bpath)}
    chains = pool_chains()
    picked = [c for c in spread(chains, sample) if c["id"] not in done]
    todo: list[dict] = []
    if baseline:                       # baselines owed by chains scored in an earlier session
        for r in prior:
            if r.get("base_key") and r["base_key"] not in based:
                based.add(r["base_key"])
                todo.append(_base_job(r))
    print(f"pool {len(chains)} chains, {len(picked)} to score ({len(done)} already done), "
          f"{len(todo)} baselines owed, {workers} workers", flush=True)
    todo += picked
    jobs, results = mp.Queue(), mp.Queue()
    procs = [mp.Process(target=_worker, args=(i, jobs, results, budget, seed), daemon=True) for i in range(workers)]
    for p in procs:
        p.start()
    flying, t0, n = 0, time.time(), 0
    deadline = t0 + minutes * 60
    with open(path, "a") as f, open(bpath, "a") as bf:
        while todo or flying:
            while todo and flying < workers and time.time() < deadline:
                jobs.put(todo.pop(0))
                flying += 1
            if not flying:
                break
            row = results.get()
            flying -= 1
            n += 1
            if row.get("baseline"):
                bf.write(json.dumps(row) + "\n")
                bf.flush()
            else:
                f.write(json.dumps(row) + "\n")
                f.flush()
                if baseline and row.get("base_key") and row["base_key"] not in based:
                    based.add(row["base_key"])
                    todo.insert(0, _base_job(row))
            print(f"[{n}] {row.get('seconds')}s {'base ' if row.get('baseline') else ''}ok={row.get('ok')} "
                  f"feasible={row.get('feasible')} dps={row.get('dps_honest')} {row['main_name']} "
                  f"{'(' + row['kind'] + ') ' if row.get('kind') else ''}{row.get('error') or ''}", flush=True)
    for _ in procs:
        jobs.put(None)
    print(f"finished {n} jobs in {(time.time() - t0) / 60:.1f} min, {len(todo)} left", flush=True)


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
    base = {r["base_key"]: r for r in _read(os.path.join(out, "baselines.jsonl"))}
    gains = []
    for r in dps:
        b = base.get(r.get("base_key"))
        if b and b.get("ok") and (b.get("dps_honest") or 0) > 0:
            r["base_dps"] = b["dps_honest"]
            r["gain"] = round(r["dps_honest"] / b["dps_honest"], 3)
            gains.append(r)
    if gains:
        g = sorted(r["gain"] for r in gains)
        rep["gain"] = {"compared": len(gains), "median": g[len(g) // 2], "better_by_10pct": sum(1 for x in g if x >= 1.1),
                       "worse_by_10pct": sum(1 for x in g if x <= 0.9),
                       "spearman_vs_gain": {k: spearman([r[k] for r in gains if r.get(k) is not None],
                                                        [r["gain"] for r in gains if r.get(k) is not None])
                                            for k in ("score", "score_raw", "fit", "novelty", "tempo")},
                       "by_kind": {}}
        for k in sorted({r["kind"] for r in gains}):
            gk = sorted(r["gain"] for r in gains if r["kind"] == k)
            rep["gain"]["by_kind"][k] = {"n": len(gk), "median": gk[len(gk) // 2], "better_by_10pct": sum(1 for x in gk if x >= 1.1)}
        st = {r["id"]: r for r in _read(os.path.join(out, "states.jsonl")) if not r.get("error")}
        cred, payoff = [], []
        for r in gains:
            x = st.get(r["id"])
            if not x:
                r["gain_credited"] = r["gain"]          # no switchable state: PoB already works the link out itself
                continue
            r["switched"] = True
            if x.get("chain_off"):
                r["state_lift"] = round(x["chain_on"] / x["chain_off"], 3)
                r["gain_credited"] = round(r["gain"] * r["state_lift"], 3)
                cred.append(r)
                if x.get("base_off") and x.get("base_on"):
                    r["base_state_lift"] = round(x["base_on"] / x["base_off"], 3)
                    r["payoff"] = round(r["state_lift"] / r["base_state_lift"], 3)
                    payoff.append(r)
        gc = sorted(r["gain_credited"] for r in gains if r.get("gain_credited") is not None)
        if gc:
            rep["gain_credited"] = {
                "note": "chain DPS with its own states on, over the baseline with them off (assumes the state is always up)",
                "compared": len(gc), "median": gc[len(gc) // 2], "better_by_10pct": sum(1 for x in gc if x >= 1.1),
                "worse_by_10pct": sum(1 for x in gc if x <= 0.9),
                "with_switchable_state": len(cred), "state_changes_dps": sum(1 for r in cred if abs(r["state_lift"] - 1) > 0.01),
                "payoff_beats_baseline": sum(1 for r in payoff if r["payoff"] >= 1.05), "payoff_compared": len(payoff),
                "spearman_vs_gain_credited": {k: spearman([r[k] for r in gains if r.get(k) is not None and r.get("gain_credited") is not None],
                                                          [r["gain_credited"] for r in gains if r.get(k) is not None and r.get("gain_credited") is not None])
                                              for k in ("score", "score_raw", "fit", "novelty", "tempo")},
                "by_kind": {}}
            for k in sorted({r["kind"] for r in gains}):
                gk = sorted(r["gain_credited"] for r in gains if r["kind"] == k and r.get("gain_credited") is not None)
                if gk:
                    rep["gain_credited"]["by_kind"][k] = {"n": len(gk), "median": gk[len(gk) // 2], "better_by_10pct": sum(1 for x in gk if x >= 1.1)}
            rep["best_gain_credited"] = [{k: r.get(k) for k in ("main_name", "kind", "rank", "score", "gain", "gain_credited", "state_lift",
                                                                 "base_state_lift", "payoff", "dps_honest", "base_dps", "feasible",
                                                                 "uniques", "keystones", "asc_nodes", "thesis")}
                                          for r in sorted((r for r in gains if r.get("gain_credited") is not None), key=lambda r: -r["gain_credited"])[:25]]
        rep["best_gain"] = [{k: r.get(k) for k in ("main_name", "kind", "rank", "score", "gain", "dps_honest", "base_dps", "feasible",
                                                   "uniques", "keystones", "asc_nodes", "thesis")}
                            for r in sorted(gains, key=lambda r: -r["gain"])[:20]]
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
    ap.add_argument("--baseline", action="store_true", help="also build each chain's setup without the chain, to measure its gain")
    ap.add_argument("--report", action="store_true", help="only summarise an existing run")
    a = ap.parse_args(argv)
    if not a.report:
        run(a.out, a.sample, a.workers, a.minutes, a.budget, a.seed, a.baseline)
        if a.baseline:
            states(a.out, a.workers)
    print(json.dumps(report(a.out), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
