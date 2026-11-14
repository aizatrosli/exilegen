"""Machine interface for pob-redux's sidecar client (pob-engine `lab`): every command prints exactly one JSON
object on stdout; logs go to stderr. Exit status 0 means the object is a result; 2 means it is {"error": ...}.

    python3 -m exilegen.lab version
    python3 -m exilegen.lab optimise --file CODE_FILE [--constraint C ...] [--profile P ...] [--steps auto]
                                     [--budget 6000] [--seed 0] [--main auto]
    python3 -m exilegen.lab synth --skeleton JSON|@file [--constraint C ...] [--profile P ...] [--budget 6000]
    python3 -m exilegen.lab results --run-dir DIR [--top 20] [--tiers 0,1] [--codes]
    python3 -m exilegen.lab status --run-dir DIR

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
    return ap


COMMANDS = {"version": cmd_version, "optimise": cmd_optimise, "optimize": cmd_optimise, "synth": cmd_synth,
            "results": cmd_results, "status": cmd_status}


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
