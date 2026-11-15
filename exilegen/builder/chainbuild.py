"""Build one mechanics-graph chain for real, at a character level and without named support gems.

    build(chain_id, level=None, exclude=[], budget=6000, seed=7, compare=False) -> dict   (the lab `chain` answer)

The chain is looked up by id in the whole chain pool (chainscore.pool_chains, default requirements), turned into
a skeleton the way chainscore does and synthesized. Two things differ from a chainscore build:

  level     Parts of the chain whose leveling step is above the level are left out of the skeleton (uniques,
            keystones, ascendancy notables, helper skills), the skeleton is built at that level (gem levels, passive
            points, gear bases and support sockets follow it) and only the ascendancy points of the trials done by
            then are spent. A level below the main skill's own step is an error: there is nothing to build.
  exclude   Support gems no search step may socket (supportgems.banned), with "forbid_gem" constraints as a
            backstop, so one that got in anyway shows up as a violation.

The pure parts (parts_at, restricted, alternatives) need no engine and are what the tests cover.
"""
from __future__ import annotations

import copy
import random
import threading
import time

from . import leveling as levelmod
from . import supportgems

DEFAULT_LEVEL = 90          # skeletons.Generator's end level
MAX_LEVEL = 100


class ChainError(ValueError):
    """The request cannot be built (unknown chain, level too low, ...): reported as {"error": ...}."""


def find_chain(chain_id: str, chains: list[dict] | None = None) -> dict:
    if chains is None:
        from . import chainscore
        chains = chainscore.pool_chains()
    ch = next((c for c in chains if c["id"] == chain_id), None)
    if ch is None:
        raise ChainError(f"unknown chain {chain_id!r} ({len(chains)} chains in the pool)")
    return ch


def check_level(level) -> int:
    if level is None:
        return DEFAULT_LEVEL
    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= MAX_LEVEL:
        raise ChainError(f"level must be a whole number from 1 to {MAX_LEVEL}, not {level!r}")
    return level


def clean_names(names) -> list[str]:
    """The request's exclusions: trimmed, empty ones and repeats (any case) dropped, order kept."""
    out, seen = [], set()
    for n in names or []:
        n = str(n).strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def parts_at(chain: dict, level: int) -> list[dict]:
    """The chain's leveling steps as parts, each marked `included` when its level is not above `level`.
    A chain without a leveling path (no level data) lists its parts with level None, all included."""
    steps = (chain.get("leveling") or {}).get("steps")
    if steps is None:
        steps = ([{"kind": "skill", "name": chain.get("main_name"), "level": None, "gate": None, "main": True}] +
                 [{"kind": "unique", "name": u, "level": None, "gate": None} for u in chain.get("uniques") or []] +
                 [{"kind": "keystone", "name": k, "level": None, "gate": None} for k in chain.get("keystones") or []] +
                 [{"kind": "asc", "name": n, "level": None, "gate": None} for _, n in chain.get("asc_nodes") or []])
    out = []
    for s in steps:
        p = {"kind": s["kind"], "name": s["name"], "level": s.get("level"), "gate": s.get("gate"),
             "included": s.get("level") is None or s["level"] <= level}
        if s.get("main"):
            p["main"] = True
        out.append(p)
    return out


def restricted(chain: dict, parts: list[dict], level: int, skill_names: dict | None = None) -> dict:
    """A copy of the chain without the parts that are not included. `skill_names` maps skill id -> display name,
    to match helper skills (the chain holds their ids, the steps their names). Raises ChainError when the main
    skill itself is not available at `level`."""
    main = next((p for p in parts if p.get("main")), None)
    if main is not None and not main["included"]:
        how = {"gem": "its gem", "base": "the weapon base that grants it", "item": "the unique that grants it",
               "ascendancy": "the ascendancy notable that grants it"}.get(main.get("gate"), "it")
        raise ChainError(f"{main['name']} is not available at level {level}: {how} needs level {main['level']}")
    gone = {(p["kind"], p["name"]) for p in parts if not p["included"]}
    names = skill_names or {}
    ch = copy.deepcopy(chain)
    ch["uniques"] = [u for u in chain.get("uniques") or [] if ("unique", u) not in gone]
    ch["keystones"] = [k for k in chain.get("keystones") or [] if ("keystone", k) not in gone]
    ch["asc_nodes"] = [[a, n] for a, n in chain.get("asc_nodes") or [] if ("asc", n) not in gone]
    ch["util_skills"] = [u for u in chain.get("util_skills") or []
                         if u and ("skill", names.get(u, u)) not in gone and ("skill", u) not in gone]
    return ch


def asc_points(level: int, cfg: dict | None = None) -> int:
    """Ascendancy points a character holds at `level`: 2 per trial whose level it has reached."""
    cfg = cfg or levelmod.DEFAULT_CONFIG
    return 2 * sum(1 for t in cfg["trial_levels"] if level >= t)


def alternatives(excluded: list[str], supports: list[str] | None, base_supports: list[str] | None,
                 base_dps: float | None = None) -> list[dict]:
    """One entry per excluded support. `supports` are the restricted build's, `base_supports` the unrestricted
    build's (None when it was not built: was_used and replaced_by are then unknown, None).

    replaced_by: the support in the excluded one's socket when the unrestricted build does not have that support
    anywhere; else the next support that only the restricted build has; None when there is none."""
    if base_supports is None:
        return [{"excluded": x, "was_used": None, "replaced_by": None, "dps_without_exclusion": None} for x in excluded]
    sup = list(supports or [])
    low = [s.lower() for s in base_supports]
    new = [s for s in sup if s.lower() not in low]          # supports only the restricted build has
    out = []
    for x in excluded:
        used = x.lower() in low
        rep = None
        if used and new:
            i = low.index(x.lower())
            rep = sup[i] if i < len(sup) and sup[i] in new else new[0]
            new.remove(rep)
        out.append({"excluded": x, "was_used": used, "replaced_by": rep, "dps_without_exclusion": base_dps})
    return out


def over_level(sk, level: int, data: dict | None = None) -> list[dict]:
    """Uniques in the skeleton that need a higher level than the build's. The generator adds the unique that
    grants an item-only main skill by itself (it cannot build the skill from a plain weapon base), so a low-level
    build can hold one although no chain part was left in above the level."""
    data = levelmod.load() if data is None else data
    out = []
    for u in sk.uniques or []:
        name = u["name"] if isinstance(u, dict) else u
        lv = ((data.get("uniques") or {}).get(name) or {}).get("level")
        if lv is not None and lv > level:
            out.append({"kind": "unique", "name": name, "level": lv,
                        "role": (u.get("role") if isinstance(u, dict) else None) or ""})
    return out


def only_supports(gems: list[str], known: list[str] | None = None) -> list[str]:
    """The support gems among a skill group's other gems: a trigger setup also holds its meta gem (Cast on Block)
    and other spells, which chainresults.supports lists too. Unchanged when there is no support gem list."""
    known = supportgems.names() if known is None else known
    if not known:
        return list(gems)
    low = supportgems.norm(known)
    return [g for g in gems if g.lower() in low]


def skeleton(gen, chain: dict, original: dict, level: int, seed: int):
    """The chain as a skeleton at `level`. Same call as Generator._src_mechanics_graph (same rng seed as
    chainscore, so the default build matches a measured one), except that the ascendancy comes from the original
    chain: when the level drops every ascendancy notable the build must not move to another ascendancy."""
    asc = (original.get("asc_nodes") or chain.get("asc_nodes") or [[None]])[0][0]
    gen.level = level
    return gen.assemble(random.Random(f"{seed}:{chain['id']}"), chain["main_skill"], source="mechanics_graph",
                        thesis=chain["thesis"], constraints=[],
                        uniques=[{"name": u, "role": "chain"} for u in chain["uniques"]], keystones=chain["keystones"],
                        asc=asc, asc_nodes=[n for _, n in chain.get("asc_nodes") or []],
                        extra_skills=[u for u in chain.get("util_skills") or [] if u],
                        extra_tags={"chain_id": chain["id"], "chain_kind": chain["kind"], "chain_tokens": chain["tokens"],
                                    "defining_unique": (chain["uniques"] or [None])[0]})


def drop_forced(sk, exclude: list[str]) -> None:
    """Take excluded supports out of the skeleton's forced lists (they bypass the search)."""
    ban = supportgems.norm(exclude)
    if not ban:
        return
    keep = lambda xs: [x for x in xs or [] if str(x).strip().lower() not in ban]
    sk.supports = keep(sk.supports)
    for k in ("supports", "feeder_supports"):
        if sk.trigger and sk.trigger.get(k):
            sk.trigger[k] = keep(sk.trigger[k])


def _synth(sk, exclude, points, budget, seed, log_fn, out: dict, key: str) -> None:
    from exilegen.engine import Engine
    from .synth import make_scorer, summary_row, synthesize
    try:
        sk = copy.deepcopy(sk)
        sk.constraints = [f"forbid_gem:{x}" for x in exclude]
        drop_forced(sk, exclude)
        with Engine() as eng:
            res = synthesize(sk, eng, make_scorer(constraints=list(sk.constraints)), budget=budget, seed=seed,
                             log_fn=log_fn, exclude_supports=exclude, asc_points=points)
        out[key] = (res, summary_row(res))
    except Exception as ex:  # noqa: BLE001 - reported by the caller
        out[key] = ex


def build(chain_id: str, level: int | None = None, exclude=None, budget: int = 6000, seed: int = 7,
          compare: bool = False, log_fn=None, chains: list[dict] | None = None) -> dict:
    from . import catalog as catmod
    from . import chainresults
    from .skeletons import Generator
    t0 = time.time()
    level = check_level(level)
    exclude = clean_names(exclude)
    original = find_chain(chain_id, chains)
    parts = parts_at(original, level)
    cat = catmod.load()
    chain = restricted(original, parts, level, {sid: s.get("name") for sid, s in cat.skills.items()})
    gen = Generator(cat)
    sk = skeleton(gen, chain, original, level, seed)
    if sk is None:
        raise ChainError(f"no skeleton for this chain at level {level}: {gen.last_reject or 'rejected'}")
    points = asc_points(level)
    if log_fn:
        log_fn(f"chain {chain_id} {original['main_name']} level {level}, {points} ascendancy points, "
               f"left out: {[p['name'] for p in parts if not p['included']] or 'nothing'}, excluded: {exclude or 'nothing'}")
    got: dict = {}
    both = bool(compare and exclude)
    side = threading.Thread(target=_synth, args=(sk, [], points, budget, seed, None, got, "base"), daemon=True) if both else None
    if side:
        side.start()
    _synth(sk, exclude, points, budget, seed, log_fn, got, "main")
    if side:
        side.join()
    if isinstance(got["main"], Exception):
        raise got["main"]
    res, row = got["main"]
    if not res.ok or not res.code:
        raise ChainError(f"the build failed: {res.error or 'no build code'}")
    main_name = original["main_name"]
    sup = only_supports((chainresults.supports(res.code, main_name) or ([], False))[0])
    out = {"chain": chain_id, "main_name": main_name, "level": level, "ok": bool(res.ok),
           "feasible": bool(row.get("feasible")), "violations": row.get("violations") or "", "code": res.code,
           "dps": row.get("pinnacle_v1.dps_honest", row.get("dps_honest")), "ehp": row.get("pinnacle_v1.ehp", row.get("ehp")),
           "supports": sup, "excluded": exclude}
    if exclude:
        base_sup = base_dps = None
        if both and not isinstance(got.get("base"), Exception):
            bres, brow = got["base"]
            if bres.ok and bres.code:
                base_sup = only_supports((chainresults.supports(bres.code, main_name) or ([], False))[0])
                base_dps = brow.get("pinnacle_v1.dps_honest", brow.get("dps_honest"))
        elif both and log_fn:
            log_fn(f"the build without exclusions failed: {got.get('base')}")
        out["alternatives"] = alternatives(exclude, sup, base_sup, base_dps)
        slipped = sorted(supportgems.norm(exclude) & supportgems.norm(sup))
        if slipped:
            out["excluded_present"] = [s for s in sup if s.lower() in slipped]
    over = over_level(sk, level)
    if over:
        out["over_level"] = over
    out.update(parts=parts, ascendancy=sk.ascendancy, asc_points=points, skeleton_id=sk.id, evals=res.evals,
               seconds=round(time.time() - t0, 1))
    if res.error:
        out["warning"] = res.error
    return out
