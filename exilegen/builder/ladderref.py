"""The poe.ninja ladder corpus as one compact reference file for comparing generated builds with the meta.

    python3 -m exilegen.builder.ladderref --corpus-dir DIR [--out exilegen/builder/data/ladder_ref.json]
    python3 -m exilegen.builder.ladderref --corpus-dir DIR --evaluate [--workers 2]     (needs the engine)

    load(path=PATH) -> the reference dict

Inputs, all in the corpus directory: overview.json (poe.ninja's counts over the whole ladder), builds.jsonl
(the sampled builds with their PoB codes) and evaluated.jsonl (per build: PoB's stats under the fresh
Pinnacle config, and tags with the socket groups, uniques, keystones and ascendancy nodes).

A build's main skill is the skill PoB measured (the exported main socket group), not poe.ninja's stratum
skill: the stratum skill is often a herald or a curse, and the DPS belongs to the measured skill.

DPS. evaluated.jsonl holds PoB's CombinedDPS under the config that exilegen calls pinnacle_v1. Generated
builds are scored as honest DPS: the same engine numbers passed through the Scorer (energy-limited triggers,
triggered spells added to the main skill, condition credits). The two differ by up to several times on trigger
builds, so --evaluate re-scores every sampled build the way chainscore does and stores the result in
data/ladder_honest.jsonl; with that file the reference holds pinnacle_v1 honest DPS (chain rows:
"pinnacle_v1.dps_honest"). Without it the reference falls back to PoB's CombinedDPS and says it is not
comparable.

Shares come from overview.json (the whole ladder); every other fraction, count and quantile is over the
sampled builds, which are stratified by skill and ascendancy and taken from the top of the ladder.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "data", "ladder_ref.json")
HONEST_PATH = os.path.join(HERE, "data", "ladder_honest.jsonl")
PROFILE = "pinnacle_v1"
PROFILES = ["pinnacle_v1", "mapping_v1"]          # what chainscore's default Scorer holds

METRIC_HONEST = ("pinnacle_v1 honest DPS: exilegen Scorer dps_honest of the engine's pinnacle_v1 stats (fresh PoB "
                 "config, Pinnacle boss, no conditional toggles, quest rewards kept) for the PoB-exported main skill; "
                 "same as chain rows' 'pinnacle_v1.dps_honest'")
METRIC_POB = ("PoB CombinedDPS of the exported main skill (minion CombinedDPS if larger) under the corpus "
              "'pinnacle_locked' config (= pinnacle_v1), without exilegen's Scorer corrections")


def load(path: str = PATH) -> dict:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------------------------- small maths
def quantile(values, q: float):
    """Linear-interpolated quantile of a list; None when empty."""
    v = sorted(values)
    if not v:
        return None
    x = q * (len(v) - 1)
    lo = int(x)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (x - lo)


def _r(x):
    return None if x is None else int(round(x))


def _dps(values) -> dict | None:
    v = [x for x in values if x and x > 0]
    return {"median": _r(quantile(v, 0.5)), "p90": _r(quantile(v, 0.9)), "max": _r(max(v))} if v else None


def _ehp(values) -> dict | None:
    v = [x for x in values if x and x > 0]
    return {"median": _r(quantile(v, 0.5)), "p90": _r(quantile(v, 0.9))} if v else None


def _top(counter: Counter, n: int, top: int) -> list:
    rows = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return [[k, round(c / n, 3)] for k, c in rows]


# ---------------------------------------------------------------------------------------------- corpus rows
def _key(meta: dict) -> str:
    return (meta.get("character_sha256") or "")[:16]


def read_honest(path: str | None) -> dict[str, dict]:
    """{build key: honest row} from --evaluate's file; rows with an error carry no DPS."""
    out: dict[str, dict] = {}
    if path and os.path.exists(path):
        for line in open(path):
            if line.strip():
                r = json.loads(line)
                out[r["k"]] = r
    return out


def build_row(rec: dict, honest: dict | None = None) -> dict | None:
    """One evaluated.jsonl record as the facts the reference needs; None when PoB measured no skill.
    `honest` is that build's row from --evaluate (None: fall back to PoB's CombinedDPS)."""
    sec = rec.get("pinnacle_locked") or {}
    tags = rec.get("tags") or {}
    meta = rec.get("meta") or {}
    stats = sec.get("stats") or {}
    skill = (sec.get("mainSkill") or {}).get("name")
    if not skill:
        return None
    pob = max(stats.get("CombinedDPS") or 0, (sec.get("minionStats") or {}).get("CombinedDPS") or 0)
    asc = tags.get("ascendancy") or meta.get("class") or (meta.get("pob") or {}).get("ascendClassName")
    main = next((g for g in tags.get("socketGroups") or [] if g.get("main")), None) or {}
    if honest is not None and not honest.get("error"):
        if honest.get("main") and honest["main"] != skill:
            skill = honest["main"]
        dps, ehp = honest.get("h"), honest.get("e") or stats.get("TotalEHP")
    else:
        dps, ehp = (None if honest is not None else pob), stats.get("TotalEHP")
    if skill.endswith(" Minion"):          # PoB's name of the minion's own attack: the gem is "Skeletal Brute"
        skill = skill[:-len(" Minion")]
    return {"skill": skill, "asc": asc, "dps": dps, "pob_dps": pob, "ehp": ehp,
            "supports": sorted({s for s in main.get("supports") or [] if s}),
            "uniques": sorted({i["name"] for i in tags.get("items") or [] if i.get("rarity") == "UNIQUE" and i.get("name")}
                              | set(tags.get("uniques") or [])),
            "keystones": sorted({k["name"] for k in tags.get("keystones") or [] if k.get("name")}),
            "asc_nodes": sorted({n["name"] for n in tags.get("ascendancyNodes") or []
                                 if n.get("type") == "Notable" and n.get("name")})}


def reference(rows: list[dict], overview: dict | None = None, honest: bool = False, note: str = "") -> dict:
    """The contract's dict from build rows (see build_row) and poe.ninja's overview."""
    ov = overview or {}
    total = ov.get("totalCharacters")
    ninja = dict(((ov.get("dimensions") or {}).get("skills") or {}).get("counts") or [])
    by_asc = {k: dict(v.get("byAscendancy") or []) for k, v in (ov.get("skillByAscendancy") or {}).items()}
    share = lambda c: round(c / total, 5) if c is not None and total else None

    by_skill: dict[str, list[dict]] = defaultdict(list)
    by_combo: dict[str, list[dict]] = defaultdict(list)
    pairs = {"unique_skill": Counter(), "keystone_skill": Counter(), "asc_node_skill": Counter()}
    for r in rows:
        s = r["skill"]
        by_skill[s].append(r)
        by_combo[f"{s}||{r['asc']}"].append(r)
        for u in r["uniques"]:
            pairs["unique_skill"][f"{u}||{s}"] += 1
        for k in r["keystones"]:
            pairs["keystone_skill"][f"{k}||{s}"] += 1
        for a in r["asc_nodes"]:
            pairs["asc_node_skill"][f"{r['asc']}:{a}||{s}"] += 1

    skills = {}
    for s, rs in sorted(by_skill.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        n = len(rs)
        cnt = lambda f: Counter(x for r in rs for x in f(r))
        skills[s] = {"n": n, "share": share(ninja.get(s)),
                     "dps": _dps(r["dps"] for r in rs), "ehp": _ehp(r["ehp"] for r in rs),
                     "ascendancies": _top(Counter(r["asc"] for r in rs if r["asc"]), n, 5),
                     "supports": _top(cnt(lambda r: r["supports"]), n, 12),
                     "uniques": _top(cnt(lambda r: r["uniques"]), n, 12),
                     "keystones": _top(cnt(lambda r: r["keystones"]), n, 8)}
    combos = {}
    for key, rs in sorted(by_combo.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        s, a = key.split("||", 1)
        combos[key] = {"n": len(rs), "share": share(by_asc.get(s, {}).get(a)),
                       "dps": _dps(r["dps"] for r in rs), "ehp": _ehp(r["ehp"] for r in rs)}
    return {"version": 1,
            "source": {"builds": len(rows), "characters": total,
                       "dps_metric": METRIC_HONEST if honest else METRIC_POB,
                       "comparable_to_chainscore": bool(honest), "note": note},
            "skills": skills, "combos": combos,
            "pairs": {k: dict(sorted(v.items())) for k, v in pairs.items()}}


NOTE = ("Skill = the skill PoB measured (exported main socket group), not poe.ninja's stratum skill. share = "
        "poe.ninja main-skill count / all ladder characters (a character can count toward several skills; null when "
        "poe.ninja does not list the name; combo share only for poe.ninja's top 75 main skills). Everything else is "
        "over the sample: stratified by skill x ascendancy (about 6 builds per combination) and taken from the top of "
        "the experience ladder, so fractions and quantiles describe strong ladder builds, not ladder frequency. "
        "DPS quantiles skip builds with no DPS. ")


def build(corpus_dir: str, honest_path: str | None = HONEST_PATH) -> dict:
    c = corpus_dir.rstrip("/") + "/"
    with open(c + "overview.json") as f:
        ov = json.load(f)
    hon = read_honest(honest_path)
    rows, missing, failed = [], 0, 0
    with open(c + "evaluated.jsonl") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            h = None
            if hon:
                h = hon.get(_key(rec.get("meta") or {}))
                if h is None:
                    missing += 1
                    h = {"error": "not evaluated"}
                elif h.get("error"):
                    failed += 1
            row = build_row(rec, h)
            if row:
                rows.append(row)
    if hon:
        note = NOTE + (f"Honest DPS from an engine re-evaluation of {len(hon) - failed} sampled builds "
                       f"({failed} failed, {missing} not evaluated: counted in n, left out of DPS). Chain rows' plain "
                       "'dps_honest' is the binding profile's (pinnacle_v1 or mapping_v1): compare with "
                       "'pinnacle_v1.dps_honest'.")
    else:
        note = NOTE + "No honest re-evaluation file: DPS is PoB's own number and overstates or understates trigger builds."
    return reference(rows, ov, honest=bool(hon), note=note)


# ---------------------------------------------------------------------------------------------- re-evaluation
def evaluate(corpus_dir: str, out: str = HONEST_PATH, workers: int = 2, limit: int | None = None, log=print) -> int:
    """Score every sampled build as chainscore scores a generated one (default Scorer, pinnacle_v1 +
    mapping_v1, PoB's exported main skill) and append one row per build to `out`. Resumes: builds already
    in `out` are skipped. Row: k (key), main, h (pinnacle_v1 honest DPS), r (its raw sum), p (PoB DPS),
    e (EHP), m (mapping_v1 honest DPS), b (binding profile), f (feasible), u (unverified) or error."""
    import threading
    from exilegen.engine import EnginePool
    from exilegen.optimise.base import fields_for
    from .synth import make_scorer

    done = read_honest(out)
    jobs = []
    with open(corpus_dir.rstrip("/") + "/builds.jsonl") as f:
        for line in f:
            if line.strip():
                b = json.loads(line)
                if _key(b) not in done and b.get("code"):
                    jobs.append({"k": _key(b), "code": b["code"]})
    jobs = jobs[:limit] if limit else jobs
    local = threading.local()
    lock = threading.Lock()
    fh = open(out, "a")

    def one(eng, job):
        if not hasattr(local, "sc"):
            local.sc = make_scorer(profiles=list(PROFILES))
        sc, row = local.sc, {"k": job["k"]}
        try:
            eng.load(code=job["code"], profile=PROFILE, main="pob")
            view = eng.skills()
            fields = fields_for(view)
            stats = {}
            for p in PROFILES:
                if p != PROFILE:
                    eng.set_profile(p)
                st = eng.stats(fields=fields)
                stats[p] = sc.stats_filter(st, p) if sc.stats_filter else st
            s = sc.score(stats, {p: view for p in stats})
            per = s.get("profiles") or {}
            pin = per.get(PROFILE) or {}
            row.update(main=(stats[PROFILE].get("mainSkill") or {}).get("name"),
                       h=_r(pin.get("dps_honest")), r=_r(pin.get("dps_raw")),
                       p=_r((stats[PROFILE].get("dps") or {}).get("best")), e=_r(pin.get("ehp")),
                       m=_r((per.get("mapping_v1") or {}).get("dps_honest")), b=s.get("binding_profile"),
                       f=bool(s.get("feasible")), u=not s.get("verified", True))
        except Exception as ex:  # noqa: BLE001 - one bad build must not stop the run
            row["error"] = str(ex)[:160]
        with lock:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            fh.flush()
        return row

    with EnginePool(workers) as pool:
        pool.map(one, jobs, progress=lambda d, n: log(f"  {d}/{n}") if d % 50 == 0 or d == n else None)
    fh.close()
    return len(jobs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus-dir", required=True)
    ap.add_argument("--out", default=PATH)
    ap.add_argument("--honest", default=HONEST_PATH, help="honest DPS rows from --evaluate ('' to ignore)")
    ap.add_argument("--evaluate", action="store_true", help="re-score the sampled builds with the engine first")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    if a.evaluate:
        n = evaluate(a.corpus_dir, a.honest or HONEST_PATH, a.workers, a.limit,
                     log=lambda m: print(m, file=sys.stderr, flush=True))
        print(f"evaluated {n} builds -> {a.honest}", file=sys.stderr)
    ref = build(a.corpus_dir, a.honest or None)
    with open(a.out, "w") as f:
        json.dump(ref, f, separators=(",", ":"), ensure_ascii=False)
        f.write("\n")
    src = ref["source"]
    print(f"{a.out}: {len(ref['skills'])} skills, {len(ref['combos'])} combos, "
          f"{sum(len(v) for v in ref['pairs'].values())} pairs from {src['builds']} builds; "
          f"comparable_to_chainscore={src['comparable_to_chainscore']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
