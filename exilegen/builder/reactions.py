"""Chain reactions: a meta gem ("Cast on ...") turns something the main skill does into a second skill.

    main skill --event--> meta gem --casts--> hosted spell --state--> payoff

The main skill makes the event (a Critical Hit, a Shock, a dodge roll, a Block ...), the meta gem gains Energy from
it and casts the spell socketed in it, and that spell either makes a state that something pays off on (a unique,
a keystone, an ascendancy notable, or the main skill itself: a loop) or is the payoff by its damage.

The game allows no longer relay than that: skills socketed in a meta gem gain no Energy, so a triggered spell never
feeds another meta gem (data/meta_gems.json, rules.no_relay). One event can fill several gems, which is a wider
build, not a longer chain, and is left to the builder.

These chains are added to the pool of mechgraph's chains by graphx.mechanics with kind "reaction". Their numbers
are estimates: the judged reliability of the event against a boss, and an Energy figure from the gem's own stats
(Power 20, gem level 20). PoB calculates no Energy; exilegen's scoring models Cast on Critical and Cast on
Elemental Ailment only, so every other meta gem is marked `modelled: false`.

    python3 -m exilegen.builder.reactions            # counts and the best chains
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter

from . import catalog as catmod
from .mechgraph import Entity, Link

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "meta_gems.json")
KIND = "reaction"
PER_PAYOFF = 2            # main skills kept per (meta, spell, state, payoff)
PER_MAIN = 4              # reactions kept per main skill
PER_META = 80
PER_SPELL = 30            # reactions kept per (meta, hosted spell)
FLOOR_PER_META = 6        # ... and at least this many per meta gem
MIN_SCORE = 0.03
DAMAGE_MIN_LADDER = 5     # a spell hosted for its damage needs this many ladder builds doing it
ONE_HAND = {"One Hand Mace", "One Hand Axe", "One Hand Sword", "Claw", "Dagger", "Flail", "Spear"}
EVENT_NAMES = {"crit": "Critical Hits", "minion": "minion deaths", "kill": "melee kills", "es": "hits on Energy Shield",
               "dodge": "dodge rolls", "block": "Blocks", "charm": "Charm use", "melee_hit": "melee hits",
               "spell_cast": "spell casts"}
_cache: dict | None = None


def load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except OSError:
            _cache = {"metas": [], "ladder": {}, "rules": {}}
    return _cache


def metas() -> list[dict]:
    return load().get("metas") or []


def meta(mid: str) -> dict | None:
    return next((m for m in metas() if m["id"] == mid or m["name"] == mid), None)


def cast_token(m: dict) -> str:
    return "cast:" + m["name"]


def say(token: str) -> str:
    return EVENT_NAMES.get(token) or token.split(":", 1)[-1]


# ---------------------------------------------------------------------------------------------- what can be hosted
def hostable(m: dict, s: dict) -> bool:
    t = set(s.get("types") or [])
    if not {"Spell", "Triggerable"} <= t or "Meta" in t or s.get("hidden") or not s.get("is_gem"):
        return False
    if s.get("granted_by") or s.get("asc_granted") or not float(s.get("cast_time") or 0) > 0:
        return False
    if m.get("host_types") and not set(m["host_types"]) & t:
        return False
    return not set(m.get("exclude_types") or []) & t


def hosts(m: dict, cat: catmod.Catalog) -> list[dict]:
    return sorted((s for s in cat.skills.values() if hostable(m, s)), key=lambda s: s["id"])


NOT_USED_BY_HAND = {"Persistent", "OngoingSkill", "NoAttackOrCastTime", "Buff", "Aura", "HasReservation", "Meta"}
NO_DAMAGE_RX = re.compile(r"(doesn't|does not|do not|don't) deal damage", re.I)
# game stat ids of a support that stops its skill from making the event (data/stat_ids.json)
EVENT_KILLERS = {
    "crit": ("global_cannot_crit",),
    "enemy:Shocked": ("never_shock", "cannot_inflict_elemental_ailments", "active_skill_never_freeze_shock_ignite", "deal_no_elemental_damage"),
    "enemy:Frozen": ("never_freeze", "cannot_inflict_elemental_ailments", "active_skill_never_freeze_shock_ignite", "deal_no_elemental_damage"),
    "enemy:Ignited": ("never_ignite", "cannot_inflict_elemental_ailments", "active_skill_never_freeze_shock_ignite", "deal_no_elemental_damage"),
}


def feeds_by_hand(s: dict) -> bool:
    """A main skill the player uses and that deals damage of its own: a buff that reserves Spirit (Fulmination,
    Herald of Ash) or a skill whose text says it deals none (Frostflame Nova) makes no event for a meta gem."""
    t = set(s.get("types") or [])
    if s.get("name") in ((load().get("rules") or {}).get("no_damage_mains") or {}).get("skills", []):
        return False
    if {"CreatesMinion", "Persistent"} <= t:
        return True                          # a persistent minion: judged by its own event (minion deaths)
    return not (t & NOT_USED_BY_HAND or NO_DAMAGE_RX.search(s.get("description") or ""))


def event_killers(chain: dict) -> list[str]:
    """Support gems that must stay off a reaction build: they stop the main skill from making the event that
    feeds the meta gem (Controlled Destruction: no Critical Hits, so Cast on Critical never fires)."""
    from . import edges
    want = EVENT_KILLERS.get((chain.get("reaction") or {}).get("event"), ())
    if not want:
        return []
    return sorted(k.split(":", 1)[1] for k, v in edges.load_ids().items()
                  if k.startswith("support:") and set(v.get("ids") or []) & set(want))


def makes_event(ev: dict, s: dict, ent: Entity | None) -> float:
    """How well a main skill makes the event (0 = it does not)."""
    t = set(s.get("types") or [])
    minion = bool({"CreatesMinion", "Minion"} & t)
    need = ev["needs"]
    if not feeds_by_hand(s):
        return 0.0
    if need == "minion":
        return 1.0 if {"CreatesMinion", "Persistent"} <= t else 0.0     # only persistent minions give Energy
    if minion and need != "any":
        return 0.0                           # a minion's hits are not yours
    if need == "hit":
        return 1.0 if {"Damage", "Attack"} & t else 0.0
    if need == "makes":
        l = ent.produces.get(ev["token"]) if ent else None
        return 0.0 if l is None else (1.0 if l.weight >= 0.9 else 0.75)
    if need == "melee":
        return 1.0 if {"Melee", "Attack"} <= t else 0.0
    if need == "spell":
        return 1.0 if "Spell" in t and float(s.get("cast_time") or 0) > 0 else 0.0
    if need == "shield":
        wt = [w for w in s.get("weapon_types") or [] if w != "None"]
        return 1.0 if "Attack" not in t or not wt or set(wt) & ONE_HAND else 0.0
    return 1.0


def energy(m: dict, ev: dict, spell: dict) -> dict:
    """Energy arithmetic for one event against a boss, from the gem's stats: plain, and marked as an estimate."""
    r = load().get("rules") or {}
    gain = 1.0 + float(r.get("energy_gain_at_gem_level_20", 0.57))
    power = float((r.get("power") or {}).get("boss", 20)) if ev.get("per_power") and ev.get("power") != "minion" else 1.0
    cast = float(spell.get("cast_time") or 1.0)
    cost = round(100.0 * cast, 1)                                  # Energy one cast of the spell takes
    per = round(float(ev["energy"]) * float(ev.get("units", 1)) * power * gain, 1)
    out = {"per_event": per, "per_cast": cost, "events_per_cast": round(cost / per, 2) if per else None,
           "assumes": "a boss (Power 20)" if power > 1 else "no enemy Power scaling", "estimate": True}
    if m.get("max") == 500:
        out["bank"] = 500
    if ev.get("scaled"):
        out["scaled"] = ev["scaled"]
    return out


def _rate(en: dict) -> float:
    n = en.get("events_per_cast") or 1.0
    return max(0.3, min(1.0, (1.0 / n) ** 0.5))


# ---------------------------------------------------------------------------------------------- graph entities
def _state_links(e: Entity) -> list[tuple[str, Link]]:
    return [(t, l) for t, l in e.produces.items() if l.weight >= 0.6 and
            (t.startswith(("enemy:", "charge:")) or t in ("infusion", "remnant", "corpse"))]


def extend(g, cat: catmod.Catalog | None = None) -> dict:
    """Register the meta gems and the spells they can host as entities of the graph (kind "util", like the
    graph's own helper skills), so that everything that looks a chain's path up finds them. Safe to call twice.
    -> {meta id: Entity}."""
    cat = cat or g.cat
    done = getattr(g, "_reaction_metas", None)
    if done is not None:
        return done
    out = {}
    for m in metas():
        s = cat.skills.get(m["id"])
        if not s:
            continue
        e = Entity("util:" + m["id"], "util", m["name"],
                   meta={"skill": m["id"], "types": s.get("types") or [], "granted_by": s.get("granted_by", []),
                         "asc_granted": s.get("asc_granted", []), "meta_gem": True})
        for ev in m["events"]:
            e.add("c", Link(ev["token"], 1.0, f"{m['name']} gains {ev['energy']:g} Energy when {ev['label']}"))
        e.add("p", Link(cast_token(m), 1.0, f"{m['name']} casts the spells socketed in it when its Energy is full"))
        g.entities[e.key] = out[m["id"]] = e
        for x in hosts(m, cat):
            xe = g.entities.get("util:" + x["id"])
            if xe is None:
                xe = g.entities["util:" + x["id"]] = g._skill_entity(x, "util")
            xe.add("c", Link(cast_token(m), 1.0, f"{x['name']} is a Triggerable Spell: {m['name']} can cast it"))
    g._reaction_metas = out
    return out


def _source(m: dict, g) -> tuple[list[Entity], bool]:
    """Entities a meta gem needs beside itself (the unique or notable that grants it), and whether it exists."""
    src = m.get("source") or {}
    if src.get("kind") == "unique":
        e = g.entities.get("unique:" + src["name"])
        return ([e], True) if e else ([], False)
    if src.get("kind") == "asc":
        e = g.entities.get(f"asc:{src['ascendancy']}:{src['name']}")
        return ([e], True) if e else ([], False)
    return [], True


def chains(g, cat: catmod.Catalog | None = None, typed: dict | None = None) -> list[dict]:
    """Every reaction chain worth showing. `typed` is edges.typed(g): a state link the game data contradicts is
    not used."""
    cat = cat or g.cat
    ents = extend(g, cat)
    ladder = load().get("ladder") or {}
    penalty = (load().get("rules") or {}).get("energy_penalty") or {}
    mains = [(e, cat.skills[e.meta["skill"]]) for e in g.entities.values() if e.kind == "skill"]
    payoffs = {}
    for tok, cons in g.consumers.items():
        payoffs[tok] = [(b, l) for b, l in cons if b.kind in ("unique", "keystone", "asc") and l.payoff > 0]
    bad = {(k.split(":", 1)[-1], tok) for (k, side, tok), te in (typed or {}).items()
           if side == "p" and te.verdict == "contradicted" and k.startswith(("skill:", "util:"))}
    raw = []
    for m in metas():
        me = ents.get(m["id"])
        if me is None:
            continue
        extra, ok = _source(m, g)
        if not ok:
            continue
        seen_hosted = (ladder.get(m["name"]) or {}).get("hosted") or {}
        for ev in m["events"]:
            feeders = []
            for e, s in mains:
                w = makes_event(ev, s, e) * (1.0 + penalty.get(s["name"], 0) / 100.0)
                if w > 0 and (m.get("source") or {}).get("kind") != "base" or w > 0 and "Spell" in s["types"]:
                    feeders.append((e, s, w))
            for x in hosts(m, cat):
                xe = g.entities["util:" + x["id"]]
                en = energy(m, ev, x)
                base = ev["boss"] * _rate(en) * (1.0 if m.get("modelled") else 0.85)
                n_lad = int(seen_hosted.get(x["name"], 0))
                lad = 1.0 + 0.15 * min(1.0, n_lad ** 0.5 / 8)
                info = {"meta": m["id"], "meta_name": m["name"], "spells": [x["id"]], "spell_names": [x["name"]],
                        "event": ev["token"], "event_label": ev["label"], "energy": en,
                        "modelled": bool(m.get("modelled")), "buildable": bool(m.get("buildable")),
                        "boss": ev["boss"], "clearing": ev["clear"], "spirit": m.get("spirit", 0),
                        "source": m.get("source") or {"kind": "gem"}, "ladder_builds": n_lad}
                ev_link = me.consumes[ev["token"]]
                cast_p, cast_c = me.produces[cast_token(m)], xe.consumes[cast_token(m)]
                for st, lx in _state_links(xe):
                    if st == ev["token"] or (x["id"], st) in bad:
                        continue
                    sp = g.spec.get(st, 0)
                    if sp <= 0:
                        continue
                    for e, s, w in feeders:
                        if s["id"] == x["id"]:
                            continue
                        own = e.produces.get(st)
                        if own is not None and own.weight >= lx.weight:
                            continue                       # the main skill makes the state itself
                        if not g._legal([e, *extra], s):
                            continue
                        lm = Link(ev["token"], w, f"{s['name']}: {ev['label']}")
                        loop = e.consumes.get(st)
                        if loop is not None and loop.payoff > 0:
                            fit = base * w * sp * lx.weight * loop.payoff * lad
                            raw.append((fit, 1.0, (m["id"], x["id"], st, "loop:" + s["id"]),
                                        [e, *extra, me, xe], [ev["token"], cast_token(m), st], s,
                                        [lm, ev_link, cast_p, lx, loop], dict(info, payoff="loop", state=st)))
                        for b, lc in payoffs.get(st, []):
                            c = g.compat(lc, s)
                            if c < 1.0 or not g._legal([e, *extra, b], s) or b in extra:
                                continue
                            fit = base * w * sp * lx.weight * lc.payoff * 0.9 * lad
                            nov = 1 - min(0.9, 4 * g.skill_share(s))
                            raw.append((fit, nov, (m["id"], x["id"], st, b.key),
                                        [e, *extra, me, xe, b], [ev["token"], cast_token(m), st], s,
                                        [lm, ev_link, cast_p, lx, lc], dict(info, payoff="state", state=st)))
                if n_lad >= DAMAGE_MIN_LADDER and "Damage" in x["types"]:
                    for e, s, w in feeders:
                        if s["id"] == x["id"] or not g._legal([e, *extra], s):
                            continue
                        fit = base * w * (0.3 + 0.3 * min(1.0, n_lad ** 0.5 / 12))
                        raw.append((fit, 1 - min(0.9, 4 * g.skill_share(s)), (m["id"], x["id"], "damage", "damage"),
                                    [e, *extra, me, xe], [ev["token"], cast_token(m)], s,
                                    [Link(ev["token"], w, f"{s['name']}: {ev['label']}"), ev_link, cast_p, cast_c],
                                    dict(info, payoff="damage")))
    # caps: the best few main skills per payoff, then per main skill, per meta gem and per hosted spell
    raw.sort(key=lambda r: (-r[0] * (0.5 + 0.5 * r[1]), r[5]["id"], r[2]))
    by_pay, picked = Counter(), []
    for r in raw:
        if by_pay[r[2]] < PER_PAYOFF:
            by_pay[r[2]] += 1
            picked.append(r)
    out = []
    for fit, _, _, path, tokens, s, links, info in picked:
        d = g._chain(KIND, path, tokens, s, fit, links, {"reaction": info})
        d["thesis"] = thesis(d, path, s)
        d["util_skills"] = [u for u in d["util_skills"] if u not in (info["meta"], *info["spells"])]
        out.append(d)
    out.sort(key=lambda d: (-d["score"], d["id"]))
    out = [d for d in out if d["score"] >= MIN_SCORE]
    by_main, by_meta, by_spell, seen, kept = Counter(), Counter(), Counter(), set(), []

    def take(d):
        rx = d["reaction"]
        seen.add(d["id"])
        by_main[d["main_skill"]] += 1
        by_meta[rx["meta"]] += 1
        by_spell[(rx["meta"], rx["spells"][0])] += 1
        kept.append(d)

    for d in out:                              # every meta gem keeps its best few, whatever the other caps say
        if d["id"] not in seen and by_meta[d["reaction"]["meta"]] < FLOOR_PER_META and by_main[d["main_skill"]] < 2:
            take(d)
    for d in out:
        rx = d["reaction"]
        if (d["id"] in seen or by_main[d["main_skill"]] >= PER_MAIN or by_meta[rx["meta"]] >= PER_META
                or by_spell[(rx["meta"], rx["spells"][0])] >= PER_SPELL):
            continue
        take(d)
    kept.sort(key=lambda d: (-d["score"], d["id"]))
    return kept


def thesis(d: dict, path: list[Entity], main: dict) -> str:
    rx = d["reaction"]
    head = f"{main['name']} feeds {rx['meta_name']} with {say(rx['event'])}, which casts {rx['spell_names'][0]}"
    if rx["payoff"] == "damage":
        return f"{head} for its damage, beside {main['name']} ({rx['ladder_builds']} ladder builds host it this way)"
    st = say(rx["state"])
    if rx["payoff"] == "loop":
        return f"{head}; it makes {st}, which {main['name']} itself uses: a loop"
    return f"{head}; it makes {st}, which {path[-1].name} pays off on ('{d['evidence'][-1][:60]}')"


def doubts(chain: dict) -> list[str]:
    """Plain cautions for a reaction chain: what is estimated and what the builder cannot do yet."""
    rx = chain.get("reaction") or {}
    out = []
    if rx.get("boss", 1) < 0.5:
        out.append(f"{say(rx['event']).capitalize()} are rare against a lone boss: this reaction is for clearing "
                   f"(judged {rx['boss']:g} on a boss, {rx['clearing']:g} while clearing)")
    return out


# ---------------------------------------------------------------------------------------------- building one
def buildable(chain: dict) -> str | None:
    """None when the builder can make the chain, else why not."""
    rx = chain.get("reaction") or {}
    if rx.get("buildable"):
        return None
    src = rx.get("source") or {}
    what = {"unique": f"the unique {src.get('name')}", "asc": f"the {src.get('ascendancy')} notable {src.get('name')}",
            "base": f"the {src.get('name')} weapon base"}.get(src.get("kind"), "an item")
    return (f"{rx.get('meta_name')} comes from {what}, not from a gem: the builder cannot socket spells into an "
            f"item-granted meta gem yet")


def apply(sk, chain: dict):
    """Turn a manual skeleton of the main skill into the reaction: the meta gem with its spell beside the main
    skill, which stays the feeder. The synthesizer builds the meta group from `trigger`."""
    from .skeletons import skeleton_id
    rx = chain["reaction"]
    hosted = {rx["meta"], *rx["spells"]}
    sk.extra_skills = [x for x in sk.extra_skills or [] if x not in hosted]
    sk.trigger = {"meta": rx["meta"], "meta_name": rx["meta_name"], "spells": list(rx["spells"]),
                  "feeder": chain["main_skill"], "feeder_name": chain["main_name"], "modelled": bool(rx["modelled"])}
    sk.tags = dict(sk.tags or {}, reaction={"event": rx["event"], "state": rx.get("state"), "payoff": rx["payoff"]})
    sk.id = skeleton_id(sk)
    return sk


def skeleton(gen, rng, chain: dict, asc: str | None = None):
    """The reaction chain as a skeleton, or None (gen.last_reject says why)."""
    why = buildable(chain)
    if why:
        gen.last_reject = why
        return None
    asc = asc or (chain["asc_nodes"][0][0] if chain.get("asc_nodes") else None)
    sk = gen.assemble(rng, chain["main_skill"], source="mechanics_graph", thesis=chain["thesis"], constraints=[],
                      uniques=[{"name": u, "role": "chain"} for u in chain["uniques"]], keystones=chain["keystones"],
                      asc=asc, asc_nodes=[n for _, n in chain.get("asc_nodes") or []],
                      extra_skills=[u for u in chain.get("util_skills") or [] if u], plan="manual",
                      extra_tags={"chain_id": chain["id"], "chain_kind": chain["kind"], "chain_tokens": chain["tokens"],
                                  "defining_unique": (chain["uniques"] or [None])[0]})
    if sk is None:
        return None
    if sk.trigger and sk.trigger.get("spells") and chain["main_skill"] in sk.trigger["spells"]:
        gen.last_reject = "the main skill cannot be used by hand, so it cannot feed the meta gem"
        return None
    return apply(sk, chain)


# the triggers polish step may move spells between meta gems; a reaction's meta gem and spell are the point
POLISH = ("supports", "tree_warm", "supports")


def main(argv=None) -> int:
    from . import mechgraph
    from . import edges as edgesmod
    cat = catmod.load()
    g = mechgraph.MechGraph(cat)
    typed = edgesmod.typed(g)
    out = chains(g, cat, typed)
    print(json.dumps({"chains": len(out), "by_meta": dict(Counter(c["reaction"]["meta_name"] for c in out)),
                      "by_payoff": dict(Counter(c["reaction"]["payoff"] for c in out)),
                      "buildable": sum(1 for c in out if c["reaction"]["buildable"])}, indent=1))
    for c in out[:25]:
        print(f"{c['score']:.3f} {c['id']} {c['thesis']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
