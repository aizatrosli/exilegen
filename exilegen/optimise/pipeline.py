"""Optimisation pipeline: chain the steps on one build, judged by one Scorer.

    from exilegen.optimise.pipeline import optimise, optimise_many
    r = optimise(code, constraints=["one_button"], profiles=["pinnacle_v1", "mapping_v1"], budget=6000)
    r["start"]["objective"], r["end"]["objective"], r["code"], print(r["explain"])
    rs = optimise_many([code1, code2], workers=2, budget=3000)

Default chain (steps="auto"):
    [convert]  if a one_button constraint is given and the build violates it
    tree (warm) -> supports -> triggers (if the build has Meta+Triggers groups)
    ... the cycle repeats while a full cycle improves the objective by >= 1% (exp(d objective) - 1)
steps="tree,supports,triggers" (or a list) runs exactly that sequence once. Step names: convert, tree
(= tree_warm), tree_scratch, supports, triggers.

SupportStep and TreeStep are imported lazily; a missing module is skipped with a note.

The budget is a number of engine evaluations (Context.evals) shared by all steps: each step gets
an even share of what is left for the planned steps (unused budget rolls over).

Floors: scoring.floors.DEFAULT_FLOORS, except mana sustain, which passes in `either` mode (sustainable
in absolute terms OR no worse than the start). The scorer's purely relative rule rejects every
change that raises a triggered main skill's cost even when mana stays sustainable, because PoB
prices triggered spells at its own (energy-unaware) rate. Pass floors=... to override.
"""
from __future__ import annotations

import dataclasses
import json
import math
import random
import time
import traceback
from typing import Any

from exilegen.engine import Engine, EngineCrash, EnginePool, code_hash
from exilegen.scoring import BuildView, Scorer
from exilegen.scoring import floors as F

from .base import Context, StepResult, fields_for, has_meta_groups

DEFAULT_PROFILES = ["pinnacle_v1", "mapping_v1"]
CYCLE_MIN_GAIN = 0.01        # stop cycling when a full cycle gains < 1%
MAX_CYCLES = 3
ALIASES = {"tree": "tree_warm", "tree_warm": "tree_warm", "tree_scratch": "tree_scratch", "supports": "supports",
           "support": "supports", "triggers": "triggers", "trigger": "triggers", "convert": "convert"}


@dataclasses.dataclass
class FilteredScorer(Scorer):
    """Scorer that passes every stats doc (and baseline) through `stats_filter(doc, profile)` first, so
    every step, whatever scoring path it uses, sees the same corrected stats (energy-aware cost)."""
    stats_filter: Any = None

    def score_one(self, doc, profile, skills=None, baseline=None):
        f = self.stats_filter
        if f is not None:
            doc = f(doc, profile)
            if isinstance(baseline, dict):
                baseline = f(baseline, profile)
        return super().score_one(doc, profile, skills, baseline)


def default_floors() -> list:
    out = []
    for f in F.DEFAULT_FLOORS:
        if f.id == "mana_sustain":
            f = dataclasses.replace(f, mode="either", note=f.note + "; optimiser: sustainable OR no worse than start")
        out.append(f)
    return out


# ---------------------------------------------------------------------------------------------- steps
def make_step(name: str, seed: int | None = None, max_evals: int | None = None, notes: list | None = None):
    """Step instance by name, or None (with a note) when its module is not available."""
    name = ALIASES.get(name, name)
    try:
        if name == "convert":
            from .triggers import ConvertStep
            return ConvertStep()
        if name == "triggers":
            from .triggers import TriggerStep
            return TriggerStep()
        if name in ("tree_warm", "tree_scratch"):
            from .tree import TreeStep
            kw = {"mode": name.split("_")[1]}
            if max_evals:
                kw["max_evals"] = int(max_evals)
            if seed is not None:
                kw["seed"] = seed
            try:
                return TreeStep(verbose=False, **kw)
            except TypeError:
                return TreeStep(mode=kw["mode"])
        if name == "supports":
            from .supports import SupportStep
            try:
                return SupportStep(groups="main+triggered")
            except TypeError:
                return SupportStep()
    except ImportError as ex:
        if notes is not None:
            notes.append(f"step {name} skipped: module not available ({ex})")
        return None
    if notes is not None:
        notes.append(f"unknown step {name}")
    return None


def _gain(a: dict | None, b: dict | None) -> float:
    """Relative gain of score b over a (objective is log-scale: exp(d) - 1); feasibility gained = inf."""
    if not a or not b:
        return 0.0
    if b.get("feasible") and not a.get("feasible"):
        return float("inf")
    if a.get("objective") is None or b.get("objective") is None:
        return 0.0
    return math.exp(min(50.0, b["objective"] - a["objective"])) - 1.0


# ---------------------------------------------------------------------------------------------- summary
def summarize(res: dict, stats_by_profile: dict) -> dict:
    """Readable facts of a score result + stats: per profile honest DPS, EHP, max hit, movement, flags."""
    out = {"objective": res.get("objective"), "feasible": res.get("feasible"), "verified": res.get("verified"),
           "dps_honest": res.get("dps_honest"), "dps_raw": res.get("dps_raw"), "ehp": res.get("ehp"),
           "binding_profile": res.get("binding_profile") or res.get("profile"),
           "violations": res.get("violations") or [], "flags": res.get("flags") or [],
           "below_target": res.get("below_target") or [], "profiles": {}}
    per = res.get("profiles") or {res.get("profile"): res}
    for prof, r in per.items():
        st = (stats_by_profile or {}).get(prof) or {}
        bv = BuildView(st, None, prof)
        mh = F.m_min_max_hit(bv)
        out["profiles"][prof] = {
            "objective": r.get("objective"), "feasible": r.get("feasible"),
            "dps_honest": r.get("dps_honest"), "dps_raw": r.get("dps_raw"), "dps_energy": r.get("dps_energy"),
            "ehp": r.get("ehp"), "max_hit_min": None if mh in (None, float("inf")) else mh,
            "movement_speed": bv.num("movement_speed", None, record=False),
            "spirit_unreserved": bv.num("spirit_unreserved", None, record=False),
            "main_skill": (st.get("mainSkill") or {}).get("name"),
            "energy_factor": (r.get("factors") or {}).get("energy"),
            "violations": r.get("violations") or [],
            "unverified": [f for f in r.get("flags") or [] if f.startswith("unverified")],
            "flags": r.get("flags") or [],
        }
    return out


def _gem_table(view: dict) -> dict:
    out = {}
    for g in (view or {}).get("groups") or []:
        if g.get("grantedBy") == "Explode":
            continue
        out[g["index"]] = {"enabled": g.get("enabled", True) is not False, "granted": g.get("grantedBy"),
                           "gems": [x.get("name") for x in g.get("gems") or [] if x.get("enabled", True) is not False]}
    return out


def diff_builds(before: dict, after: dict) -> list[str]:
    """Human-readable gem / group / tree changes between two snapshots {skills, tree}."""
    lines = []
    a, b = _gem_table(before.get("skills")), _gem_table(after.get("skills"))
    for gi in sorted(set(a) | set(b)):
        ga, gb = a.get(gi), b.get(gi)
        if ga is None:
            lines.append(f"group {gi} added: {', '.join(gb['gems'])}" + ("" if gb["enabled"] else " (disabled)"))
            continue
        if gb is None:
            lines.append(f"group {gi} removed")
            continue
        name = ga["gems"][0] if ga["gems"] else f"group {gi}"
        if ga["enabled"] != gb["enabled"]:
            lines.append(f"{name} (g{gi}) {'enabled' if gb['enabled'] else 'disabled'}")
        rem = [x for x in ga["gems"] if x not in gb["gems"]]
        add = [x for x in gb["gems"] if x not in ga["gems"]]
        if rem or add:
            lines.append(f"{name} (g{gi}): " + ", ".join([f"-{x}" for x in rem] + [f"+{x}" for x in add]))
    ta, tb = before.get("tree") or {}, after.get("tree") or {}
    if ta and tb:
        na, nb = set(ta.get("allocated") or []), set(tb.get("allocated") or [])
        names = {**{int(k): v for k, v in (ta.get("nodes") or {}).items()}, **{int(k): v for k, v in (tb.get("nodes") or {}).items()}}

        def nm(i):
            n = names.get(int(i)) or {}
            return n.get("name") or str(i)

        def notable(i):
            return (names.get(int(i)) or {}).get("kind") in ("keystone", "notable")
        rem, add = sorted(na - nb), sorted(nb - na)
        if rem or add:
            lines.append(f"passive tree: +{len(add)} / -{len(rem)} nodes")
            ka = [nm(i) for i in add if notable(i)]
            kr = [nm(i) for i in rem if notable(i)]
            if ka:
                lines.append("  notables/keystones added: " + ", ".join(ka))
            if kr:
                lines.append("  notables/keystones removed: " + ", ".join(kr))
        ca, cb = ta.get("attributeChoices") or {}, tb.get("attributeChoices") or {}
        nch = sum(1 for k in set(ca) | set(cb) if ca.get(k) != cb.get(k))
        if nch:
            lines.append(f"  attribute choices changed: {nch}")
    return lines


def _snapshot_view(engine) -> dict:
    out = {"skills": engine.skills()}
    try:
        out["tree"] = engine.tree(frontier=False)
    except Exception:
        out["tree"] = None
    return out


def _fmt(v, nd=0):
    if v is None:
        return "?"
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:.{nd}f}" if nd else f"{v:.2f}"


def format_result(r: dict, show_code: bool = True) -> str:
    """Before/after summary for the CLI."""
    L = []
    s, e = r.get("start") or {}, r.get("end") or {}
    L.append(f"build {r.get('hash')}  constraints={r.get('constraints') or []}  profiles={r.get('profiles')}")
    if r.get("error"):
        L.append(f"ERROR: {r['error']}")
    L.append(f"objective {_fmt(s.get('objective'))} -> {_fmt(e.get('objective'))}"
             f"  ({'feasible' if s.get('feasible') else 'INFEASIBLE'} -> {'feasible' if e.get('feasible') else 'INFEASIBLE'})"
             f"  evals {r.get('evals')}  {r.get('seconds', 0):.0f}s")
    hdr = f"  {'profile':<12} {'':>6} {'honest DPS':>14} {'raw DPS':>14} {'EHP':>9} {'max hit':>8} {'move':>5} {'spirit':>6}  main skill"
    L.append(hdr)
    for prof in (e.get("profiles") or s.get("profiles") or {}):
        for tag, d in (("before", s), ("after", e)):
            p = (d.get("profiles") or {}).get(prof) or {}
            if not p:
                continue
            L.append(f"  {prof:<12} {tag:>6} {_fmt(p.get('dps_honest')):>14} {_fmt(p.get('dps_raw')):>14} {_fmt(p.get('ehp')):>9}"
                     f" {_fmt(p.get('max_hit_min')):>8} {_fmt(p.get('movement_speed')):>5} {_fmt(p.get('spirit_unreserved')):>6}  {p.get('main_skill')}")
        bp, ap = (s.get("profiles") or {}).get(prof) or {}, (e.get("profiles") or {}).get(prof) or {}
        if bp.get("dps_honest") and ap.get("dps_honest") is not None:
            L.append(f"  {'':<12} {'':>6} {'x' + format(ap['dps_honest'] / bp['dps_honest'], '.2f'):>14}")
    for tag, d in (("before", s), ("after", e)):
        for v in d.get("violations") or []:
            L.append(f"  {tag} violation: {v.get('profile', '')} {v.get('rule')} {v.get('entry')}: {v.get('detail')}")
    unv = sorted({f for p in (e.get("profiles") or {}).values() for f in p.get("unverified") or []})
    if unv:
        L.append("  unverified (after): " + "; ".join(unv))
    L.append("steps:")
    for st in r.get("steps") or []:
        L.append(f"  {st['name']:<13} {'improved' if st.get('improved') else 'no gain':<8} obj {_fmt(st.get('start_objective'))}"
                 f" -> {_fmt(st.get('end_objective'))}  {len(st.get('changes') or [])} changes, {st.get('evals')} evals,"
                 f" {st.get('seconds', 0):.0f}s")
        for n in (st.get("notes") or [])[:6]:
            L.append(f"      note: {str(n)[:160]}")
    for n in r.get("notes") or []:
        L.append(f"  note: {n}")
    if r.get("changes"):
        L.append("what changed:")
        L += [f"  {x}" for x in r["changes"]]
    if show_code and r.get("code"):
        L.append("exported PoB code:")
        L.append(r["code"])
    return "\n".join(L)


# ---------------------------------------------------------------------------------------------- core
def _context(engine, code, constraints, profiles, budget, floors, log_fn, seed, main=None):
    engine.load(code=code, profile=profiles[0], main=main)
    from .triggers import energy_cost_filter
    scorer = FilteredScorer(profile=list(profiles), constraints=list(constraints or []),
                            floors=floors if floors is not None else default_floors())
    scorer.stats_filter = energy_cost_filter(scorer)
    ctx = Context(engine, scorer, budget=budget, log_fn=log_fn, seed=seed)
    ctx.fields = fields_for(ctx.skills())
    ctx.baseline_stats = ctx._stats_for(None)
    ctx.evals = 0
    ctx.refresh()
    ctx.evals = 0
    return ctx


def score_code(code: str, constraints=(), profiles=None, engine: Engine | None = None, floors=None, main=None) -> dict:
    """Score one build (no optimisation). -> {summary, explain, result}."""
    profiles = list(profiles or DEFAULT_PROFILES)
    own = engine is None
    eng = engine or Engine()
    try:
        ctx = _context(eng, code, constraints, profiles, 10 ** 9, floors, None, None, main)
        return {"hash": code_hash(code), "constraints": list(constraints or []), "profiles": profiles,
                "summary": summarize(ctx.current, ctx.current_stats), "explain": ctx.current.get("explain"),
                "result": ctx.current}
    finally:
        if own:
            eng.close()


def _plan(steps) -> list[str] | None:
    if steps in (None, "auto"):
        return None
    if isinstance(steps, str):
        steps = [x.strip() for x in steps.split(",") if x.strip()]
    return [ALIASES.get(s, s) for s in steps]


def optimise(code: str, constraints=(), profiles=None, steps="auto", budget: int = 6000, seed: int | None = 0,
             engine: Engine | None = None, floors=None, log_fn=None, main=None, max_cycles: int = MAX_CYCLES) -> dict:
    """Optimise one build. Returns a JSON-serialisable dict:
    {hash, constraints, profiles, start, end (summaries), steps [StepResult dicts], history, code (exported),
     changes (readable diff), explain, evals, seconds, notes, error?}"""
    t0 = time.time()
    profiles = list(profiles or DEFAULT_PROFILES)
    constraints = list(constraints or [])
    if seed is not None:
        random.seed(seed)
    own = engine is None
    eng = engine or Engine()
    notes: list[str] = []
    out: dict[str, Any] = {"hash": code_hash(code), "constraints": constraints, "profiles": profiles,
                           "budget": budget, "seed": seed, "notes": notes, "steps": []}
    try:
        ctx = _context(eng, code, constraints, profiles, budget, floors, log_fn, seed, main)
        start = ctx.current
        out["start"] = summarize(start, ctx.current_stats)
        out["start_explain"] = start.get("explain")
        before = _snapshot_view(eng)
        plan = _plan(steps)
        results: list[StepResult] = []

        def run_step(name: str, share: int) -> StepResult | None:
            step = make_step(name, seed, share if name.startswith("tree") else None, notes)
            if step is None:
                return None
            if name == "triggers" and not has_meta_groups(ctx.skills()):
                return None
            if name == "convert":
                from .triggers import needs_conversion
                if not needs_conversion(ctx):
                    return None
            cap_total = ctx.budget
            ctx.budget = min(cap_total, ctx.evals + max(1, share))
            ev0, ts = ctx.evals, time.time()
            obj0 = (ctx.current or {}).get("objective")
            before_res, before_stats, n_hist = ctx.current, ctx.current_stats, len(ctx.history)
            tok = eng.snapshot()
            try:
                ctx.log(f"[pipeline] {name}: budget share {share}")
                r = step.run(ctx)
            except EngineCrash as ex:
                raise
            except Exception as ex:          # noqa: BLE001 - one failing step must not lose the run
                notes.append(f"step {name} failed: {type(ex).__name__}: {ex}")
                ctx.log(traceback.format_exc())
                try:
                    ctx.refresh()
                except Exception:
                    pass
                r = StepResult(name, False, obj0, (ctx.current or {}).get("objective"),
                               notes=[f"failed: {type(ex).__name__}: {ex}"])
            finally:
                ctx.budget = cap_total
            # guard: a step never leaves the build worse than it found it (feasibility, then objective)
            try:
                ctx.refresh()
                if Context.better(before_res, ctx.current, 1e-6):
                    eng.restore(tok)
                    ctx.current, ctx.current_stats, ctx._skills = before_res, before_stats, None
                    del ctx.history[n_hist:]
                    r.notes.append("rolled back: the step's end state scored worse than its start")
                    r.changes, r.improved, r.end_objective = [], False, obj0
                    notes.append(f"step {name} rolled back (worse than its start)")
            finally:
                ctx._drop(tok)
            r.evals = r.evals or (ctx.evals - ev0)
            r.seconds = r.seconds or (time.time() - ts)
            # fields may need energy now (a conversion adds a meta group)
            want = fields_for(ctx.skills())
            if want != "core" and ctx.fields == "core":
                ctx.fields = want
                ctx.refresh()
            results.append(r)
            return r

        def share_for(n_left: int) -> int:
            return max(50, ctx.budget_left() // max(1, n_left))

        if plan is not None:
            for i, name in enumerate(plan):
                if ctx.budget_left() <= 0:
                    notes.append(f"budget exhausted before {name}")
                    break
                run_step(name, share_for(len(plan) - i))
        else:
            from .triggers import needs_conversion
            if needs_conversion(ctx):
                run_step("convert", share_for(6))
            cycle_names = ["tree_warm", "supports", "triggers"]
            for cyc in range(max_cycles):
                if ctx.budget_left() <= 50:
                    break
                c0 = ctx.current
                todo = [n for n in cycle_names if n != "triggers" or has_meta_groups(ctx.skills())]
                planned_left = len(todo) * (2 if cyc == 0 else 1)
                for i, name in enumerate(todo):
                    if ctx.budget_left() <= 50:
                        break
                    run_step(name, share_for(planned_left - i))
                g = _gain(c0, ctx.current)
                ctx.log(f"[pipeline] cycle {cyc + 1}: gain {g:.2%}")
                if g < CYCLE_MIN_GAIN:
                    break
        end = ctx.current
        out["end"] = summarize(end, ctx.current_stats)
        out["explain"] = end.get("explain")
        out["steps"] = [dataclasses.asdict(r) for r in results]
        out["history"] = [{k: v for k, v in h.items() if k != "t"} for h in ctx.history]
        out["changes"] = diff_builds(before, _snapshot_view(eng))
        out["code"] = eng.export()
        out["evals"] = ctx.evals
        out["gain"] = _gain(start, end)
    except Exception as ex:          # noqa: BLE001
        out["error"] = f"{type(ex).__name__}: {ex}"
        out["traceback"] = traceback.format_exc()
    finally:
        out["seconds"] = time.time() - t0
        if own:
            eng.close()
    return json.loads(json.dumps(out, default=_json_default))


def _json_default(o):
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    if isinstance(o, float) and (math.isinf(o) or math.isnan(o)):
        return str(o)
    return str(o)


JOB_KEYS = ("constraints", "profiles", "steps", "budget", "seed", "floors", "main", "max_cycles", "log_fn")


def optimise_many(codes, workers: int = 2, progress=None, engine_kwargs: dict | None = None, **kw) -> list[dict]:
    """Optimise several builds independently on `workers` persistent engines (EnginePool).
    codes: list of share codes or dicts {code, id?, constraints?, profiles?, steps?, budget?} (per-job
    overrides of **kw). Returns results in input order; a failed job gives {"error": ...}."""
    jobs = []
    for i, c in enumerate(codes):
        if isinstance(c, str):
            jobs.append({"code": c, "id": i})
        else:
            jobs.append(dict(c, id=c.get("id", i)))

    def run(eng, job):
        args = dict(kw)
        args.update({k: v for k, v in job.items() if k in JOB_KEYS})
        r = optimise(job["code"], engine=eng, **args)
        r["id"] = job["id"]
        return r

    with EnginePool(workers, **(engine_kwargs or {})) as pool:
        res = pool.map(run, jobs, return_exceptions=True, progress=progress)
    return [r if isinstance(r, dict) else {"id": j["id"], "error": f"{type(r).__name__}: {r}"} for r, j in zip(res, jobs)]
