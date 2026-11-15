"""Machine interface for pob-redux's sidecar client (pob-engine `lab`): every command prints exactly one JSON
object on stdout; logs go to stderr. Exit status 0 means the object is a result; 2 means it is {"error": ...}.

    python3 -m exilegen.lab version
    python3 -m exilegen.lab optimise --file CODE_FILE [--constraint C ...] [--profile P ...] [--steps auto]
                                     [--budget 6000] [--seed 0] [--main auto]
    python3 -m exilegen.lab synth --skeleton JSON|@file [--constraint C ...] [--profile P ...] [--budget 6000]
    python3 -m exilegen.lab results --run-dir DIR [--top 20] [--tiers 0,1] [--codes]
    python3 -m exilegen.lab status --run-dir DIR
    python3 -m exilegen.lab chain --chain CHAIN_ID [--level N] [--exclude-support NAME ...] [--compare]
                                  [--spirit-gems auto|none|NAME,NAME] [--spirit-gear N] [--budget 6000] [--seed 7]
    python3 -m exilegen.lab graph [--requirements JSON|@file] [--scores RUN_DIR] [--main NAME] [--chain CHAIN_ID]
                                  [--top 10]

`graph` lists mechanics-graph chains for a set of requirements (the fields of builder/graphx.py `Requirements`:
cls, ascendancy, damage_types, uniques, keystones, chain_kinds, online_by, rank_by, spirit_source, spirit_level,
spirit_gear, ...), best first, in a short form: `id` (what `chain` takes), `kind`, `main`, `parts`, `links`,
`thesis`, the formula `score`, `fitted` (the score fitted to PoB runs), `doubts` (links that do not hold up when
their sentences are read again), `online_level` and `farm` from the leveling plan, and `spirit_gems` (the picks,
each with its Spirit cost, role and reason). `--scores` attaches what PoB measured for the chain in a chainscore
run (`measured`). `--main` keeps the chains of one main skill (a part of its name is enough); `--chain` returns one
chain in full, with its evidence lines, tempo, leveling steps and the Spirit gems that were passed over. `total`
counts the chains that matched before `--top` cut the list. No engine is needed and it answers in about a second.

`chain` builds one mechanics-graph chain (an id from the graph export) from nothing and reports the build:
code, honest DPS and EHP under the pinnacle profile, the support gems on the main skill, and the chain's
`parts` with `included`. `--level` builds what a character of that level has: parts whose leveling step is
above it are left out, and a level below the main skill's own step is an error. `--exclude-support` (repeatable,
display names) keeps supports out of every socket; each gets an `alternatives` entry, whose `was_used`,
`replaced_by` and `dps_without_exclusion` are null unless `--compare` also builds the unrestricted build
(a second engine at the same time: about the same wall time, twice the CPU). `over_level` appears when the
build still holds a unique above the level: the generator adds the unique that grants an item-only main skill.

`--spirit-gems` adds Spirit gems (heralds, auras, persistent buffs) to the build: `auto` takes the chain's picks
(builder/spiritx.py) for the Spirit a character of that level has, a comma-separated list takes exactly those gems;
`none` (the default) changes nothing. The Spirit is the campaign quest Spirit held at `--level` (30 from 11, 60 from
33, 100 from 55) plus `--spirit-gear N` from equipment; the build's config is set so that PoB counts the same. The
answer gains `spirit`: `budget`, `reserved` and `unreserved` as PoB reports them for the finished build, `gems`
(each with `enabled`: false when it was switched off for lack of Spirit, last-named first), `planned` (the budget
the gems were chosen for), `dps_mapping`, and `states`: PoB's raw DPS as built (`pob_dps`), with the linking states
switched on in the config, which the locked profiles leave off (`pob_dps_on`), and both again with the gems' groups
switched off in this same build (`pob_dps_gems_off`, `pob_dps_gems_off_on`). With `--compare` the same chain is also built without the Spirit gems:
`dps_without`, `dps_without_mapping` and the `states` figures of that build. `--compare` with both exclusions and
Spirit gems runs a third build after the first two.

Long searches use the ordinary `python3 -m exilegen build --run-dir DIR ...` (stop with a STOP file); `status`
and `results` read its run directory. The engine comes from the environment as for the CLI (EXG_POBCTL /
EXG_POB_ROOT, see engine.py); the client sets them.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
import traceback

LAB_VERSION = 1


def _clean(x):
    """JSON-safe: NaN/inf -> None, dataclasses -> dicts, unknown objects -> str."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [_clean(v) for v in x]
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return _clean(dataclasses.asdict(x))
    if x is None or isinstance(x, (str, int, bool)):
        return x
    return str(x)


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(_clean(obj), separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _log(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def _read(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    if arg.startswith("@"):
        with open(arg[1:], encoding="utf-8") as f:
            return f.read()
    return arg


def _main_arg(v):
    if v is None:
        return None
    return int(v) if str(v).isdigit() else v


# ------------------------------------------------------------------------------------------------ commands
def cmd_version(_a) -> dict:
    from .engine import _backend, find_binary, find_pob_root
    backend = _backend()
    return {"lab": LAB_VERSION, "backend": backend, "engine": find_binary(backend=backend),
            "pob_root": find_pob_root()}


def cmd_optimise(a) -> dict:
    from .cli import _read_code
    from .optimise.pipeline import DEFAULT_PROFILES, optimise
    code = _read_code(a)
    r = optimise(code, constraints=a.constraint, profiles=a.profile or list(DEFAULT_PROFILES), steps=a.steps,
                 budget=a.budget, seed=a.seed, log_fn=_log if a.verbose else None, main=_main_arg(a.main))
    r.pop("history", None)
    if r.get("error"):
        _log(r.pop("traceback", ""))
    return r


def _skeleton(raw: dict):
    from .builder.contracts import Skeleton
    from .builder.skeletons import skeleton_id
    fields = {f.name for f in dataclasses.fields(Skeleton)}
    unknown = sorted(set(raw) - fields)
    if unknown:
        raise ValueError(f"unknown skeleton fields: {', '.join(unknown)}")
    for k in ("cls", "ascendancy", "main_skill"):
        if not raw.get(k):
            raise ValueError(f"skeleton needs {k}")
    raw = dict(raw)
    raw["uniques"] = [u if isinstance(u, dict) else {"name": u} for u in raw.get("uniques") or []]
    sk = Skeleton(**dict(raw, id=raw.get("id") or ""))
    if not sk.id:
        sk.id = skeleton_id(sk)
    sk.source = sk.source or "user"
    return sk


def cmd_synth(a) -> dict:
    from .builder.synth import make_scorer, summary_row, synthesize
    from .engine import Engine
    sk = _skeleton(json.loads(_read(a.skeleton)))
    sk.constraints = list(a.constraint)
    scorer = make_scorer(None, profiles=a.profile or None, constraints=list(a.constraint))
    with Engine() as eng:
        res = synthesize(sk, eng, scorer, budget=a.budget, log_fn=_log if a.verbose else None, seed=a.seed)
    score = res.score or {}
    return {"ok": res.ok, "skeleton": sk.to_dict(), "code": res.code, "error": res.error,
            "summary": summary_row(res),
            "score": {k: score.get(k) for k in ("feasible", "verified", "objective", "dps_honest", "dps_raw", "ehp",
                                                "binding_profile", "violations", "flags", "below_target")},
            "notes": res.notes, "evals": res.evals, "seconds": res.seconds}


def cmd_chain(a) -> dict:
    from .builder import chainbuild
    try:
        out = chainbuild.build(a.chain, level=a.level, exclude=a.exclude_support, budget=a.budget, seed=a.seed,
                               compare=a.compare, log_fn=_log if a.verbose else None, spirit_gems=a.spirit_gems,
                               spirit_gear=a.spirit_gear)
    except chainbuild.ChainError as ex:
        return {"error": str(ex)}
    return {"lab": LAB_VERSION, **out}


def _part_names(doc: dict) -> dict:
    return {n["id"]: n["name"] for n in doc["mechanics"]["nodes"]}


def _chain_view(c: dict, names: dict) -> dict:
    """One chain in the short form the app's assistant reads."""
    lv = c.get("leveling") or {}
    sp = c.get("spirit") or {}
    out = {"id": c["id"], "kind": c["kind"], "main": c["main_name"],
           "parts": [names.get(k, k) for k in c["path"]], "links": [t.split(":", 1)[-1] for t in c["tokens"]],
           "ascendancies": sorted({a for a, _ in c.get("asc_nodes") or []}), "uniques": c.get("uniques") or [],
           "keystones": c.get("keystones") or [], "thesis": c.get("thesis"), "score": c.get("score"),
           "tempo": (c.get("tempo") or {}).get("tempo"), "online_level": lv.get("online_level"),
           "farm": lv.get("farm") or [],
           "spirit_gems": [{k: p.get(k) for k in ("name", "spirit", "role", "why")} for p in sp.get("picks") or []]}
    if c.get("predicted"):
        out["fitted"] = {k: c["predicted"].get(k) for k in ("score", "works", "gain")}
    if c.get("doubts"):
        out["doubts"] = c["doubts"]
    m = c.get("measured")
    if m:
        out["measured"] = {k: m[k] for k in ("built", "feasible", "dps", "ehp", "base_dps", "gain", "gain_fair",
                                             "gain_credited", "state_lift", "wrong_main", "thin", "error", "supports",
                                             "code") if m.get(k) is not None}
    if (c.get("ladder") or {}).get("cls"):
        out["ladder"] = c["ladder"]["cls"]
    return out


def cmd_graph(a) -> dict:
    from .builder import graphx
    req = json.loads(_read(a.requirements)) if a.requirements else {}
    if not isinstance(req, dict):
        raise ValueError("requirements must be a JSON object")
    want = max(1, a.top)
    if a.chain or a.main:
        req["top"] = 2000               # the filter runs after the ranking, so rank everything
    else:
        req["top"] = want
    doc = graphx.run(req, scores_dir=a.scores, codes=bool(a.chain))
    failed = [{"label": c["label"], "detail": c["detail"]} for c in doc["preflight"] if c["status"] == "fail"]
    if not doc["passed"]:
        return {"error": "requirements not met: " + "; ".join(f"{c['label']}: {c['detail']}" for c in failed)}
    mech = doc["mechanics"]
    names = _part_names(doc)
    warnings = [{"label": c["label"], "detail": c["detail"]} for c in doc["preflight"] if c["status"] == "warn"]
    if a.chain:
        hit = [c for c in mech["chains"] if c["id"].startswith(a.chain)]
        if not hit:
            return {"error": f"chain not found: {a.chain}"}
        c = hit[0]
        return {"lab": LAB_VERSION, "chain": {**_chain_view(c, names), "evidence": c.get("evidence"),
                                              "tempo": c.get("tempo"), "leveling": c.get("leveling"),
                                              "spirit": c.get("spirit"), "fitted": c.get("predicted"),
                                              "ladder": c.get("ladder"), "rank": mech["chains"].index(c) + 1},
                "warnings": warnings}
    chains = mech["chains"]
    if a.main:
        q = a.main.lower()
        chains = [c for c in chains if q in c["main_name"].lower() or q == c["main_skill"].lower()]
    info = mech["info"]
    return {"lab": LAB_VERSION, "rank_by": doc["requirements"]["rank_by"], "pool": info["pool"],
            "total": len(chains) if a.main else info["kept"], "doubted": info.get("doubted"),
            "measured": info.get("measured"), "spirit": {k: info["spirit"].get(k) for k in ("budget", "source", "level")
                                                         if isinstance(info.get("spirit"), dict)},
            "warnings": warnings, "chains": [_chain_view(c, names) for c in chains[:want]]}


def _entry_view(e, codes: bool) -> dict:
    rec = e.rec
    sk = rec.get("skeleton") or {}
    s = rec.get("score") or {}
    out = {"uid": e.uid, "cell": e.cell, "tier": e.tier, "feasible": e.feasible, "verified": e.verified,
           "objective": e.objective, "dps_honest": e.dps_honest, "ehp": e.ehp,
           "max_hit": rec.get("max_hit"),
           "class": sk.get("cls"), "ascendancy": sk.get("ascendancy"), "main_skill": sk.get("main_skill"),
           "main_skill_name": (sk.get("tags") or {}).get("main_skill_name"),
           "uniques": [u.get("name") for u in sk.get("uniques") or []], "trigger": sk.get("trigger"),
           "keystones": sk.get("keystones"), "defence": sk.get("defence"), "source": sk.get("source"),
           "thesis": sk.get("thesis"), "descriptor": rec.get("descriptor"),
           "flags": [f for f in s.get("flags") or [] if "unverified" in str(f)][:8]}
    if codes:
        out["code"] = rec.get("code")
    return out


def cmd_results(a) -> dict:
    from .builder.archive import Archive
    arch = Archive.open(a.run_dir)
    tiers = tuple(int(t) for t in a.tiers.split(","))
    elites = arch.elites(tiers=tiers)
    return {"run_dir": os.path.abspath(a.run_dir), "stats": arch.stats(), "total": len(elites),
            "elites": [_entry_view(e, a.codes) for e in elites[: a.top]]}


def cmd_status(a) -> dict:
    d = a.run_dir
    prog = None
    p = os.path.join(d, "progress.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            prog = json.load(f)
    return {"run_dir": os.path.abspath(d), "exists": os.path.isdir(d), "progress": prog,
            "stopping": os.path.exists(os.path.join(d, "STOP")),
            "report": os.path.join(d, "report.md") if os.path.exists(os.path.join(d, "report.md")) else None}


# ------------------------------------------------------------------------------------------------ main
def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python3 -m exilegen.lab", description="exilegen sidecar commands (JSON out)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version")

    def scoring(p):
        p.add_argument("--constraint", action="append", default=[])
        p.add_argument("--profile", action="append", default=[])
        p.add_argument("--budget", type=int, default=6000)
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("-v", "--verbose", action="store_true")

    po = sub.add_parser("optimise", aliases=["optimize"])
    src = po.add_mutually_exclusive_group(required=True)
    src.add_argument("--code")
    src.add_argument("--file")
    po.add_argument("--steps", default="auto")
    po.add_argument("--main", default=None)
    scoring(po)
    ps = sub.add_parser("synth")
    ps.add_argument("--skeleton", required=True, help="skeleton JSON, @file or - for stdin")
    scoring(ps)
    pr = sub.add_parser("results")
    pr.add_argument("--run-dir", required=True)
    pr.add_argument("--top", type=int, default=20)
    pr.add_argument("--tiers", default="0,1", help="0 feasible+verified, 1 feasible unverified, 2 infeasible")
    pr.add_argument("--codes", action="store_true", help="include each elite's PoB code")
    pt = sub.add_parser("status")
    pt.add_argument("--run-dir", required=True)
    pc = sub.add_parser("chain")
    pc.add_argument("--chain", required=True, help="chain id from the graph export")
    pc.add_argument("--level", type=int, default=None, help="character level to build at (default: end level)")
    pc.add_argument("--exclude-support", action="append", default=[], metavar="NAME",
                    help="support gem display name that must not be socketed (repeatable)")
    pc.add_argument("--compare", action="store_true",
                    help="also build without the exclusions (fills the alternatives) and without the Spirit gems "
                         "(fills spirit.dps_without)")
    pc.add_argument("--spirit-gems", default="none", metavar="auto|none|NAME,NAME",
                    help="Spirit gems to add: the chain's picks for the level's Spirit, none, or exactly these gems")
    pc.add_argument("--spirit-gear", type=float, default=0, metavar="N", help="Spirit from equipment to assume")
    pc.add_argument("--budget", type=int, default=6000)
    pc.add_argument("--seed", type=int, default=7)
    pc.add_argument("-v", "--verbose", action="store_true")
    pg = sub.add_parser("graph")
    pg.add_argument("--requirements", help="requirements JSON, @file or - for stdin (default: no restrictions)")
    pg.add_argument("--scores", help="chainscore run directory: attach what PoB measured")
    pg.add_argument("--main", help="keep the chains of this main skill (part of the name)")
    pg.add_argument("--chain", help="return this chain in full")
    pg.add_argument("--top", type=int, default=10)
    return ap


COMMANDS = {"version": cmd_version, "optimise": cmd_optimise, "optimize": cmd_optimise, "synth": cmd_synth,
            "results": cmd_results, "status": cmd_status, "chain": cmd_chain, "graph": cmd_graph}


def main(argv=None) -> int:
    a = parser().parse_args(argv)
    try:
        out = COMMANDS[a.cmd](a)
    except Exception as ex:  # noqa: BLE001 - the client reads the error object
        _log(traceback.format_exc())
        _emit({"error": f"{type(ex).__name__}: {ex}"})
        return 2
    _emit(out)
    return 2 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
