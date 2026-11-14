"""Command line: optimise, score or check a PoE2 PoB build.

    python3 -m exilegen optimise --code CODE [--constraint one_button] [--profile pinnacle_v1 --profile mapping_v1]
                                 [--budget N] [--steps tree,supports,triggers] [--seed S] [--out out.json]
    python3 -m exilegen score --code CODE [--profile ...] [--constraint ...]
    python3 -m exilegen check --code CODE --constraint one_button      (exit status 0 feasible, 1 infeasible)

--file F reads the code from a file: a bare share code, or JSON / JSONL with a "code" field (first line).
Needs the engine: EXG_ENGINE_BIN (or engine/target) and EXG_POB_ROOT.
"""
from __future__ import annotations

import argparse
import json
import sys


def _read_code(args) -> str:
    if args.code:
        return args.code.strip()
    if not args.file:
        raise SystemExit("give --code CODE or --file F")
    text = (sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()).strip()
    if text.startswith("{"):
        return json.loads(text.splitlines()[0])["code"].strip()
    return text


def _common(p: argparse.ArgumentParser) -> None:
    src = p.add_mutually_exclusive_group()
    src.add_argument("--code", help="PoB share code")
    src.add_argument("--file", help="file with a share code (or JSON with a 'code' field); - for stdin")
    p.add_argument("--constraint", action="append", default=[],
                   help="one_button[:strict], no_cast[:strict|lenient], no_minions, max_manual_skills:N, "
                        "require_gem:X, forbid_gem:X, ... (repeatable)")
    p.add_argument("--profile", action="append", default=[], help="scoring profile (repeatable; default "
                                                                   "pinnacle_v1 + mapping_v1)")
    p.add_argument("--main", default=None, help='main skill selection: auto (default) | pob | group index')
    p.add_argument("--out", help="write the full result as JSON here")
    p.add_argument("-v", "--verbose", action="store_true", help="log step progress to stderr")


def _profiles(args):
    from .optimise.pipeline import DEFAULT_PROFILES
    return args.profile or list(DEFAULT_PROFILES)


def _main_arg(v):
    if v is None:
        return None
    return int(v) if str(v).isdigit() else v


def _print_score(r: dict) -> None:
    from .optimise.pipeline import _fmt
    s = r["summary"]
    print(f"build {r['hash']}  constraints={r['constraints']}  profiles={r['profiles']}")
    print(f"objective {_fmt(s['objective'])}  {'FEASIBLE' if s['feasible'] else 'INFEASIBLE'}"
          f"{'' if s['verified'] else ' (unverified)'}")
    for prof, p in s["profiles"].items():
        print(f"  {prof:<12} honest DPS {_fmt(p['dps_honest'])}  raw {_fmt(p['dps_raw'])}  EHP {_fmt(p['ehp'])}"
              f"  max hit {_fmt(p['max_hit_min'])}  move {_fmt(p['movement_speed'])}  spirit {_fmt(p['spirit_unreserved'])}"
              f"  main {p['main_skill']}")
        for v in p["violations"]:
            print(f"    violation {v.get('rule')} {v.get('entry')}: {v.get('detail')}")
        if p["unverified"]:
            print("    unverified: " + "; ".join(p["unverified"]))
    print()
    print(r["explain"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m exilegen", description="exilegen PoE2 build optimiser")
    sub = ap.add_subparsers(dest="cmd", required=True)
    po = sub.add_parser("optimise", aliases=["optimize"], help="optimise a build")
    _common(po)
    po.add_argument("--budget", type=int, default=6000, help="engine evaluations (default 6000)")
    po.add_argument("--steps", default="auto", help="auto | comma list of convert,tree,tree_scratch,supports,triggers")
    po.add_argument("--seed", type=int, default=0)
    po.add_argument("--no-code", action="store_true", help="do not print the exported code")
    ps = sub.add_parser("score", help="score a build")
    _common(ps)
    pc = sub.add_parser("check", help="check constraints and floors (exit 1 if infeasible)")
    _common(pc)
    args = ap.parse_args(argv)
    code = _read_code(args)
    log = (lambda *a: print(*a, file=sys.stderr)) if args.verbose else None

    if args.cmd in ("optimise", "optimize"):
        from .optimise.pipeline import format_result, optimise
        r = optimise(code, constraints=args.constraint, profiles=_profiles(args), steps=args.steps,
                     budget=args.budget, seed=args.seed, log_fn=log, main=_main_arg(args.main))
        print(format_result(r, show_code=not args.no_code))
        if args.out:
            json.dump(r, open(args.out, "w"), indent=1)
        if r.get("error"):
            print(r.get("traceback", ""), file=sys.stderr)
            return 2
        return 0

    from .optimise.pipeline import score_code
    r = score_code(code, constraints=args.constraint, profiles=_profiles(args), main=_main_arg(args.main))
    if args.out:
        json.dump(r, open(args.out, "w"), indent=1, default=str)
    if args.cmd == "score":
        _print_score(r)
        return 0
    s = r["summary"]
    print(f"{'FEASIBLE' if s['feasible'] else 'INFEASIBLE'}  constraints={r['constraints']}  profiles={r['profiles']}")
    for v in s["violations"]:
        print(f"  {v.get('profile', '')} {v.get('rule')} {v.get('entry')}: {v.get('detail')}")
    if not s["verified"]:
        print("  unverified: " + "; ".join(sorted({f for f in s["flags"] if "unverified" in f})))
    return 0 if s["feasible"] else 1


if __name__ == "__main__":
    sys.exit(main())
