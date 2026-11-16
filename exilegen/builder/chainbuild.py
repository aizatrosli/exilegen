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

  spirit    `spirit_gems` puts Spirit gems into the build as extra skill groups: "auto" takes spiritx's picks for
            the chain as it stands at the level, a list of names takes exactly those gems. The character then has
            the Spirit of its level and no more: PoB's quest rewards give all 100 Spirit at any level, so the
            quests the level has not reached are switched off in the build's config (it travels with the code),
            and `spirit_gear` is added as a custom modifier ("+N to Spirit"). The locked scoring profiles start
            from a fresh config that keeps quest rewards but not custom modifiers, so SpiritEngine writes the
            modifier into each profile's config after every load. synth switches extra skill groups off, last
            first, while unreserved Spirit is negative: the gems are added best first, so the weakest goes first.
            A gem of an attribute the class and the main skill are not built on goes in at gem level 1 (`gem_level`).
            The answer's `spirit` block is PoB's own count for the finished build.

The pure parts (parts_at, restricted, alternatives, spirit_request, spirit_report) need no engine and are what the
tests cover.
"""
from __future__ import annotations

from exilegen.scoring import energy as _energy
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
    if chain.get("reaction"):             # the main skill by hand, with the meta gem and its spell beside it
        from . import reactions
        return reactions.skeleton(gen, random.Random(f"{seed}:{chain['id']}"), chain, asc)
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


# ---------------------------------------------------------------------------------------------- Spirit gems
SPIRIT_PROFILES = ("pinnacle_v1", "mapping_v1")       # synth.DEFAULT_PROFILES: the config sets the Spirit goes into

_QUEST_LUA = """
local ct = launch.main.modes["BUILD"].configTab
local qs = {}
for _, q in ipairs(data.questRewards) do
  if q.Stat and q.Stat:find("to Spirit") and q.useConfig ~= false then qs[#qs + 1] = q end
end
table.sort(qs, function(a, b) return (a.AreaLevel or 0) < (b.AreaLevel or 0) end)
for i, q in ipairs(qs) do
  if i > %d then ct.input["quest" .. q.Description .. q.Area .. q.Info] = false end
end
"""
_GEAR_LUA = """
local ct = launch.main.modes["BUILD"].configTab
local line = "+%d to Spirit"
local cur = ct.input.customMods or ""
if not cur:find(line, 1, true) then ct.input.customMods = (cur ~= "" and (cur .. "\\n") or "") .. line end
"""
_REBUILD_LUA = ' ct:BuildModList() ct:BuildModList() if EXG then EXG.state.dirty = true end return "ok"'


# The attributes a class starts on. A Spirit gem of another attribute is socketed at gem level 1: at the level the
# character could use, its attribute requirement makes the gear search trade damage for attributes (seen with
# Lingering Illusion on a Witch: 32k -> 19k DPS for a gem PoB credits with nothing).
CLASS_ATTRS = {"Warrior": {"strength"}, "Ranger": {"dexterity"}, "Huntress": {"dexterity"}, "Witch": {"intelligence"},
               "Sorceress": {"intelligence"}, "Mercenary": {"strength", "dexterity"}, "Monk": {"dexterity", "intelligence"},
               "Druid": {"strength", "intelligence"}}
_ATTRS = ("strength", "dexterity", "intelligence")


def low_level_gems(gems: list[dict], cls: str | None, main: dict | None, skills: dict) -> dict[str, str]:
    """{gem id: gem name} of the plan's gems that need an attribute neither the class nor the main skill is built on."""
    have = set(CLASS_ATTRS.get(cls or "", set())) | {a for a in _ATTRS if a in ((main or {}).get("gem_tags") or [])}
    out = {}
    for g in gems:
        s = skills.get(g.get("skill") or "") or {}
        need = {a for a in _ATTRS if a in (s.get("gem_tags") or [])}
        if have and need - have and s.get("gem_id") and not g.get("skip"):
            out[s["gem_id"]] = g["name"]
    return out


def spirit_engine(quests: int | None, gear: float, low: dict | None = None):
    """An Engine whose builds hold the Spirit of `quests` campaign rewards plus `gear` from equipment, and whose
    `low` gems (gem ids) arrive at gem level 1."""
    from exilegen.engine import Engine
    low = low or {}

    class SpiritEngine(Engine):
        def raw(self, method, params=None):
            if method == "add_gem" and (params or {}).get("gemId") in low and "level" not in params:
                params = dict(params, level=1)
            r = super().raw(method, params)
            if method == "new_build":
                body = (_QUEST_LUA % quests if quests is not None else "") + (_GEAR_LUA % gear if gear > 0 else "")
                if body:
                    self.lua(body + _REBUILD_LUA)
            return r

        def load(self, code=None, xml=None, profile=None, main=None, timeout=None, gc=None):
            r = super().load(code=code, xml=xml, profile=profile, main=main, timeout=timeout, gc=gc)
            if gear > 0:
                names = [p for p in SPIRIT_PROFILES if p != profile] + ([profile] if profile in SPIRIT_PROFILES else [])
                for p in names:                 # the loaded profile last, so it is the active one again
                    self.op("set_profile", name=p)
                    self.op("lua", chunk=_GEAR_LUA % gear + _REBUILD_LUA)
                if profile not in SPIRIT_PROFILES and names:
                    self.op("set_profile", name=profile or "as_exported")
            return r

    return SpiritEngine()


def quests_at(level: int, cfg: dict | None = None) -> int:
    """How many of the campaign's Spirit rewards a character of `level` holds (leveling config `quest_spirit`)."""
    table = (cfg or levelmod.DEFAULT_CONFIG)["quest_spirit"]
    return sum(1 for at in table if int(at) <= level)


def included_path(chain: dict, parts: list[dict], entities: dict) -> list[str]:
    """The chain's path entities whose part is included at the level (entities: mechgraph's, key -> Entity)."""
    gone = {(p["kind"], p["name"]) for p in parts if not p["included"]}
    kind_of = {"unique": "unique", "keystone": "keystone", "asc": "asc", "skill": "skill", "util": "skill"}
    out = []
    for key in chain.get("path") or []:
        e = entities.get(key)
        if e is None or (kind_of.get(e.kind), e.name) not in gone:
            out.append(key)
    return out


def spirit_request(spirit_gems, spirit_gear=0) -> tuple[str | list[str], float]:
    """-> ("none" | "auto" | [names], gear). Raises ChainError for a bad value."""
    gear = spirit_gear or 0
    if isinstance(gear, bool) or not isinstance(gear, (int, float)) or gear < 0:
        raise ChainError(f"spirit gear must be a number, 0 or more, not {spirit_gear!r}")
    if spirit_gems is None or spirit_gems == [] or str(spirit_gems).strip().lower() in ("", "none"):
        return "none", float(gear)
    if isinstance(spirit_gems, str) and spirit_gems.strip().lower() == "auto":
        return "auto", float(gear)
    names = clean_names(spirit_gems.split(",") if isinstance(spirit_gems, str) else spirit_gems)
    if not names:
        return "none", float(gear)
    return names, float(gear)


def spirit_plan(original: dict, chain: dict, parts: list[dict], level: int, mode, gear: float, cat) -> dict:
    """What goes into the build: {"budget", "quest", "gear", "quests", "gems": [catalogue pick or named gem], ...}."""
    from . import mechgraph, spiritx
    sc = spiritx.Scorer(mechgraph.MechGraph(cat), cat)
    quest = spiritx.quest_spirit(level)
    plan = {"quest": quest, "gear": gear, "quests": quests_at(level), "budget": quest + gear, "mode": "auto" if mode == "auto" else "named"}
    path = included_path(original, parts, sc.g.entities)
    sug = sc.suggest(dict(original, tokens=chain["tokens"]), quest + gear, level, path=path, gear=gear)
    plan.update(budget=sug["budget"], own=sug["own"], free=sug["free"], notes=sug.get("notes") or [])
    if mode == "auto":
        plan["gems"] = sug["picks"]
        plan["more"] = sug["more"]
        return plan
    by = {c["name"].lower(): c for c in sc.candidates(original, path, level)}
    gems = []
    for n in mode:
        g = sc.find(n)
        if g is None:
            gems.append({"name": n, "skill": None, "spirit": None, "level": None, "skip": "unknown"})
            continue
        c = by.get(g["name"].lower())
        d = {"name": g["name"], "skill": g["skill"], "level": g["level"],
             "spirit": spiritx.cost_at(g["spirit"], g["spirit_by_level"], level)}
        if c:
            d.update({k: c[k] for k in ("role", "token", "why", "score") if k in c})
        if g["flags"]:
            d["flags"] = list(g["flags"])
        if g["level"] is not None and g["level"] > level:
            d["skip"] = "level"           # the character cannot use the gem yet: it stays out of the build
        gems.append(d)
    left = float(plan["free"])
    for d in gems:                        # in the order given: a gem that does not fit what is left stays out
        if d.get("skip"):
            continue
        if d["spirit"] > left:
            d["skip"] = "spirit"
        else:
            left -= d["spirit"]
    plan["gems"] = gems
    return plan


def _group_of(skills_view, gem: dict) -> dict | None:
    """The gem's socket group in the engine's skills view (synth labels an extra group with the gem's name)."""
    groups = (skills_view or {}).get("groups") or []
    low = gem["name"].lower()
    for g in groups:
        if str(g.get("label") or "").lower() == low and not g.get("grantedBy"):
            return g
    for g in groups:
        if any((x.get("id") or "") == gem["skill"] or str(x.get("name") or "").lower() == low for x in g.get("skills") or []):
            return g
    return None


def spirit_report(plan: dict, stats: dict | None, skills_view) -> dict:
    """The answer's `spirit` block: PoB's Spirit for the finished build, and which gems are enabled."""
    sp = (stats or {}).get("spirit") or {}
    gems = []
    for g in plan["gems"]:
        grp = None if g.get("skip") else _group_of(skills_view, g)
        d = {"name": g["name"], "spirit": g["spirit"], "enabled": bool(grp is not None and grp.get("enabled") is not False)}
        for k in ("role", "token", "why"):
            if g.get(k):
                d[k] = g[k]
        if not d["enabled"]:              # "level": unlocks later; "spirit": switched off, the Spirit ran out
            d["reason"] = g.get("skip") or ("spirit" if grp is not None else "unknown")
        if g.get("skip") == "level":
            d["note"] = f"needs character level {g['level']}"
        elif g.get("skip") == "spirit":
            d["note"] = "left out: it does not fit the Spirit that is free"
        elif g.get("skip") == "unknown":
            d["note"] = "not a Spirit gem this version knows"
        elif grp is None:
            d["note"] = "not in the build: the engine has no such gem"
        elif g.get("flags"):
            d["note"] = "; ".join(g["flags"])
        if d["enabled"] and g["name"] in (plan.get("low") or {}).values():
            d["gem_level"] = 1            # off-attribute for this character: socketed at the cheapest requirement
        gems.append(d)
    out = {"budget": sp.get("total"), "reserved": sp.get("reserved"), "unreserved": sp.get("unreserved"), "gems": gems,
           "planned": {"budget": plan["budget"], "quest": plan["quest"], "gear": plan["gear"], "own": plan.get("own"),
                       "free": plan.get("free")}}
    if sp.get("total") is not None and sp["total"] != plan["budget"]:
        out["planned"]["note"] = (f"PoB counts {sp['total']:g} Spirit, the plan {plan['budget']:g}: the difference comes from "
                                  "the build's own equipment, passives or ascendancy")
    if plan.get("notes"):
        out["notes"] = plan["notes"]
    return out


def _states(eng, code: str, tokens: list[str], skills_view=None, gems: list[dict] | None = None) -> dict | None:
    """PoB's DPS of a finished build with and without the states of `tokens` switched on in the config
    (chainscore's state pass: the locked profile turns no conditional state on). With `gems`, the same four
    figures again with those gems' groups switched off in this very build: what the gems alone are worth,
    free of the difference between two searches."""
    from . import chainscore
    vars_ = chainscore.state_vars(tokens)
    read = lambda: eng.stats(fields=["dps"])["dps"]["best"]
    eng.ensure_loaded(code=code, profile=chainscore.STATE_PROFILE)
    out = {"vars": sorted(vars_), "pob_dps": read()}
    off = {str(g["index"]): False for g in (_group_of(skills_view, x) for x in gems or []) if g and g.get("enabled") is not False}
    if vars_:
        chainscore._set_vars(eng, vars_)
        out["pob_dps_on"] = read()
    if off:
        eng.apply(group_enabled=off, fields=False)
        if vars_:
            out["pob_dps_gems_off_on"] = read()
            chainscore._set_vars(eng, {k: None for k in vars_})
        out["pob_dps_gems_off"] = read()
    return out


def _synth(sk, exclude, points, budget, seed, log_fn, out: dict, key: str, engine=None, state_tokens=None,
           state_gems=None) -> None:
    from exilegen.engine import Engine
    from .synth import make_scorer, summary_row, synthesize
    try:
        sk = copy.deepcopy(sk)
        sk.constraints = [f"forbid_gem:{x}" for x in exclude]
        drop_forced(sk, exclude)
        with (engine() if engine else Engine()) as eng:
            from . import reactions
            res = synthesize(sk, eng, make_scorer(constraints=list(sk.constraints)), budget=budget, seed=seed,
                             log_fn=log_fn, exclude_supports=exclude, asc_points=points,
                             **({"polish": reactions.POLISH} if (sk.tags or {}).get("reaction") else {}),
                             unique_trial="off")   # the chain's uniques are the point of the build
            if state_tokens is not None and res.ok and res.code:
                try:
                    res.descriptor["states"] = _states(eng, res.code, state_tokens, res.skills, state_gems)
                except Exception as ex:  # noqa: BLE001 - the build stands without the state figures
                    res.notes.append(f"state pass failed: {ex}")
        out[key] = (res, summary_row(res))
    except Exception as ex:  # noqa: BLE001 - reported by the caller
        out[key] = ex


def _dps(row: dict, profile: str = "pinnacle_v1"):
    return row.get(f"{profile}.dps_honest", row.get("dps_honest") if profile == "pinnacle_v1" else None)


def build(chain_id: str, level: int | None = None, exclude=None, budget: int = 6000, seed: int = 7,
          compare: bool = False, log_fn=None, chains: list[dict] | None = None, spirit_gems=None,
          spirit_gear: float = 0) -> dict:
    from . import catalog as catmod
    from . import chainresults
    from .skeletons import Generator
    t0 = time.time()
    level = check_level(level)
    exclude = clean_names(exclude)
    mode, gear = spirit_request(spirit_gems, spirit_gear)
    original = find_chain(chain_id, chains)
    parts = parts_at(original, level)
    cat = catmod.load()
    chain = restricted(original, parts, level, {sid: s.get("name") for sid, s in cat.skills.items()})
    if original.get("weapon_swap"):
        raise ChainError(original["weapon_swap"]["why"])
    rx, rx_note, rx_ban = original.get("reaction"), None, []
    if rx:
        from . import reactions
        gone = {p["name"] for p in parts if not p["included"]}
        if rx["meta_name"] in gone or set(rx["spell_names"]) & gone:
            chain.pop("reaction", None)       # below the meta gem's level the build is the main skill alone
            rx_note = f"{rx['meta_name']} or its spell is not available at level {level}: built without the reaction"
        elif reactions.buildable(chain):
            raise ChainError(reactions.buildable(chain))
        else:                                 # supports that would switch the reaction's own event off stay out
            rx_ban = [k for k in reactions.event_killers(chain) if k.lower() not in supportgems.norm(exclude)]
    gen = Generator(cat)
    sk = skeleton(gen, chain, original, level, seed)
    if sk is None:
        raise ChainError(f"no skeleton for this chain at level {level}: {gen.last_reject or 'rejected'}")
    points = asc_points(level)
    plan = engine = None
    plain_sk = sk
    if mode != "none":
        plan = spirit_plan(original, chain, parts, level, mode, gear, cat)
        plan["low"] = low_level_gems(plan["gems"], sk.cls, cat.skills.get(original["main_skill"]), cat.skills)
        engine = lambda: spirit_engine(plan["quests"], plan["budget"] - plan["quest"], plan["low"])
        sk = copy.deepcopy(sk)
        have = {str(x).lower() for x in sk.extra_skills or []}
        sk.extra_skills = list(sk.extra_skills or []) + [g["skill"] for g in plan["gems"] if not g.get("skip") and g["skill"].lower() not in have]
        if log_fn:
            log_fn(f"spirit: budget {plan['budget']:g} ({plan['quest']:g} quests + {plan['gear']:g} gear), chain reserves "
                   f"{plan.get('own')}, gems {[g['name'] for g in plan['gems']] or 'none'}")
    tokens = sorted({g["token"] for g in (plan or {}).get("gems", []) if g.get("token") and not g.get("skip")} | set(chain.get("tokens") or [])) if plan else None
    if log_fn:
        log_fn(f"chain {chain_id} {original['main_name']} level {level}, {points} ascendancy points, "
               f"left out: {[p['name'] for p in parts if not p['included']] or 'nothing'}, excluded: {exclude or 'nothing'}")
    got: dict = {}
    both = bool(compare and exclude)
    bare = bool(compare and plan and any(not g.get("skip") for g in plan["gems"]))        # the same build without the Spirit gems
    # one build runs beside the main one: the one without Spirit gems when there are any, else the unrestricted one
    side_args = (plain_sk, exclude + rx_ban, points, budget, seed, None, got, "bare", engine, tokens) if bare else \
        (sk, rx_ban, points, budget, seed, None, got, "base", engine)
    side = threading.Thread(target=_synth, args=side_args, daemon=True) if both or bare else None
    if side:
        side.start()
    _synth(sk, exclude + rx_ban, points, budget, seed, log_fn, got, "main", engine, tokens,
           [g for g in plan["gems"] if not g.get("skip")] if plan else None)
    if side:
        side.join()
    if both and bare:                                     # a third build, after the two: never three engines at once
        _synth(sk, rx_ban, points, budget, seed, None, got, "base", engine)
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
    if plan:
        sp = spirit_report(plan, (res.stats or {}).get("pinnacle_v1") if isinstance(res.stats, dict) else None, res.skills)
        sp["dps_mapping"] = _dps(row, "mapping_v1")
        if (res.descriptor or {}).get("states"):
            sp["states"] = dict(res.descriptor["states"])
        if plan.get("more"):
            sp["more"] = plan["more"]
        st = sp.get("states") or {}
        if st.get("pob_dps_gems_off"):       # the same build with only the Spirit gems switched off: what they add, nothing else
            sp["effect"] = round(st["pob_dps"] / st["pob_dps_gems_off"], 3)
            if st.get("pob_dps_gems_off_on") and st.get("pob_dps_on"):
                sp["effect_states_on"] = round(st["pob_dps_on"] / st["pob_dps_gems_off_on"], 3)
        if compare:
            sp["dps_without"] = None
            if not bare:
                sp["dps_without"] = out["dps"]            # nothing was added: the build is the comparison
            elif not isinstance(got.get("bare"), Exception):
                wres, wrow = got["bare"]
                if wres.ok and wres.code:
                    sp["dps_without"] = _dps(wrow)
                    sp["dps_without_mapping"] = _dps(wrow, "mapping_v1")
                    ws = (wres.descriptor or {}).get("states")
                    if ws and "states" in sp:
                        sp["states"].update(pob_dps_without=ws["pob_dps"], pob_dps_without_on=ws.get("pob_dps_on"))
            elif log_fn:
                log_fn(f"the build without Spirit gems failed: {got.get('bare')}")
        out["spirit"] = sp
        out.setdefault("alternatives", [])
    if rx:
        group = next((n for n in res.notes or [] if str(n).startswith("trigger group")), None)
        out["reaction"] = {"meta_gem": rx["meta_name"], "casts": rx["spell_names"], "fed_by": rx["event_label"],
                           "built": bool(group) and not rx_note, "modelled": rx["modelled"], "energy": rx["energy"],
                           "kept_out": rx_ban,
                           "note": rx_note or (None if rx["modelled"] else
                                               f"{rx['meta_name']}'s Energy is estimated from judged event rates, not "
                                               f"measured: the triggered spell's damage is a rough figure"
                                               if rx["meta"] in _energy.ESTIMATES else
                                               f"the scorer has no Energy model for {rx['meta_name']}: the triggered "
                                               f"spell's damage is counted as if it always fired")}
    over = over_level(sk, level)
    if over:
        out["over_level"] = over
    out.update(parts=parts, ascendancy=sk.ascendancy, asc_points=points, skeleton_id=sk.id, evals=res.evals,
               seconds=round(time.time() - t0, 1))
    if res.error:
        out["warning"] = res.error
    if not out.get("dps"):                 # PoB gives the main skill no damage here: the search had nothing to rank by
        out["feasible"] = False
        out["warning"] = "; ".join(x for x in (out.get("warning"), f"{main_name} deals no damage in this build: "
                                               "its supports were not chosen by damage") if x)
    return out
