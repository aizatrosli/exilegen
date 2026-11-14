"""Produces/consumes mechanics graph (docs/build-search-design.md §4.1 and §4c method 2).

    python3 -m exilegen.builder.mechgraph [--top 400] [--print 30]

Nodes are *entities* (unique items, keystones, ascendancy notables, active skills, utility skills such as
curses / marks / heralds) and *tokens* (a state or stat: an ailment on the enemy, a charge type, a
condition on you, minions, corpses, a damage type, a resource ...). An entity

  produces a token  when one of its lines / skill types creates the state or stat
                    ("Fire can Shock", "+1 to Maximum Power Charges", a Cold skill chills, "Grants Skill: X");
  consumes a token  when a line's payoff is gated or scaled by it (PoB ModParser tags Condition /
                    ActorCondition / Multiplier / PerStat, or "per X / while X / against X enemies" text),
                    or when a skill consumes it (SkillConsumesFreeze, consumes Power Charges, targets corpses).

A *bridge* both consumes and produces (conversion: "Gain % of Physical as extra Lightning" consumes
dmg:Physical and produces dmg:Lightning).

Typed chains (every chain ends in the main skill's damage):
  self_feed   M -X-> B          main skill M produces X, B's payoff on X scales M          (length 2)
  enabler     A -X-> M          A produces X, main skill M consumes X                         (length 2)
  graft       A -X-> B => M     A produces X, B's payoff on X scales M                       (length 3)
  bridge      M -X-> B -Y-> C   M produces X, bridge B turns X into Y, C's payoff on Y scales M (length 3)

Score = mechanical fit (token specificity x link strength x scope compatibility)
        x (0.25 + 0.75 * novelty), novelty = low corpus co-occurrence of the chain's parts (and low play
        share of the main skill). Top chains are exported to data/chains.json for skeletons.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import catalog as catmod

# ---------------------------------------------------------------------------------------------- vocabulary
STATE_RX: dict[str, str] = {
    "enemy:Shocked": r"\bshock",
    "enemy:Frozen": r"\bfr(?:eez|oze)",
    "enemy:Chilled": r"\bchill",
    "enemy:Ignited": r"\bignit|\bburning\b",
    "enemy:Poisoned": r"\bpoison",
    "enemy:Bleeding": r"\bbleed",
    "enemy:Electrocuted": r"\belectrocut",
    "enemy:Cursed": r"\bcurse|\bhex(?:es|ed)?\b",
    "enemy:Marked": r"\bmark(?:ed|s)?\b",
    "enemy:Stunned": r"heavy stun|\bdazed?\b|primed for stun",
    "enemy:ArmourBroken": r"break(?:s|ing)? armour|armour break|fully broken",
    "enemy:Exposed": r"\bexposure\b",
    "enemy:Pinned": r"\bpin(?:ned|s)?\b",
    "enemy:Withered": r"\bwither",
    "enemy:Blinded": r"\bblind",
    "enemy:Intimidated": r"\bintimidat",
    "charge:Power": r"power charge",
    "charge:Frenzy": r"frenzy charge",
    "charge:Endurance": r"endurance charge",
    "rage": r"\brage\b",
    "minion": r"\bminions?\b|\bcompanions?\b",
    "corpse": r"\bcorpses?\b",
    "crit": r"critical hit",
    "life:low": r"low life",
    "life:full": r"full life",
    "mana:low": r"low mana",
    "es": r"energy shield",
    "block": r"\bblock",
    "totem": r"\btotems?\b",
    "infusion": r"\binfusions?\b",
    "remnant": r"\bremnants?\b",
    "glory": r"\bglory\b",
    "tailwind": r"\btailwind\b",
    "onslaught": r"\bonslaught\b",
    "mana_spent": r"mana spent|spend(?:ing)? mana",
    "life_spent": r"life spent|spend(?:ing)? life|cost[^.]{0,30}\blife\b",
    "presence": r"\bpresence\b",
    "deflect": r"\bdeflect",
    "evasion": r"\bevasion\b",
    "armour": r"\barmour\b",
    "flask": r"\bflasks?\b",
    "charm": r"\bcharms?\b",
    "kill": r"\bkill(?:ed|s|ing)?\b|\bslain\b",
    "stat:Str": r"\bstrength\b",
    "stat:Dex": r"\bdexterity\b",
    "stat:Int": r"\bintelligence\b",
    "stat:Life": r"maximum life",
    "stat:Mana": r"maximum mana",
    "stat:Spirit": r"\bspirit\b",
    "trigger": r"\btrigger",
}
STATE_COMPILED = {k: re.compile(v, re.I) for k, v in STATE_RX.items()}
# parsed ModParser tag var/stat -> token
TAG_TOKEN = {
    "Shocked": "enemy:Shocked", "Frozen": "enemy:Frozen", "Chilled": "enemy:Chilled", "Ignited": "enemy:Ignited",
    "Poisoned": "enemy:Poisoned", "Bleeding": "enemy:Bleeding", "Electrocuted": "enemy:Electrocuted",
    "Cursed": "enemy:Cursed", "CurseOnEnemy": "enemy:Cursed", "HeavyStunned": "enemy:Stunned", "Immobilised": "enemy:Stunned",
    "PowerCharge": "charge:Power", "RemovablePowerCharge": "charge:Power", "FrenzyCharge": "charge:Frenzy",
    "EnduranceCharge": "charge:Endurance", "Rage": "rage", "RageEffect": "rage", "CanGainRage": "rage",
    "LowLife": "life:low", "FullLife": "life:full", "LowMana": "mana:low", "FullMana": "mana:full",
    "CriticalStrike": "crit", "CritRecently": "crit", "KilledRecently": "kill",
    "CurrentEnergyShield": "es", "UseCurrentEnergyShield": "es", "HaveEnergyShield": "es", "MaximumEnergyShield": "es",
    "LowEnergyShield": "es", "EnergyShieldOnBody Armour": "es", "EnergyShieldOnAllArmourItems": "es",
    "BlockChance": "block", "BlockedRecently": "block", "UsingShield": "block",
    "Str": "stat:Str", "Dex": "stat:Dex", "Int": "stat:Int", "Life": "stat:Life", "LifeUnreserved": "stat:Life",
    "Mana": "stat:Mana", "Spirit": "stat:Spirit", "ManaSpentRecently": "mana_spent", "LifeCost": "life_spent",
    "SummonedMinion": "minion", "HaveTotem": "totem", "EnemyInPresence": "presence", "Evasion": "evasion",
    "EvasionOnBody Armour": "evasion", "ArmourOnAllArmourItems": "armour", "UsingFlask": "flask", "EmptyCharms": "charm",
}
DMG = ("Physical", "Fire", "Cold", "Lightning", "Chaos")
ELEMENTAL = ("Fire", "Cold", "Lightning")
INHERENT_AILMENT = {"Lightning": ["enemy:Shocked"], "Cold": ["enemy:Chilled", "enemy:Frozen"], "Fire": ["enemy:Ignited"],
                    "Chaos": [], "Physical": []}
SKILL_CONSUMES = {
    "SkillConsumesFreeze": "enemy:Frozen", "SkillConsumesShock": "enemy:Shocked", "SkillConsumesIgnite": "enemy:Ignited",
    "SkillConsumesBleeding": "enemy:Bleeding", "SkillConsumesPowerChargesOnUse": "charge:Power",
    "SkillConsumesFrenzyChargesOnUse": "charge:Frenzy", "SkillConsumesEnduranceChargesOnUse": "charge:Endurance",
    "ConsumesRage": "rage", "ConsumesFullyBrokenArmour": "enemy:ArmourBroken", "TargetsDestructibleCorpses": "corpse",
    "TargetsDestructibleRareCorpses": "corpse",
}
SKILL_PRODUCES_TYPE = {
    "AppliesCurse": "enemy:Cursed", "Mark": "enemy:Marked", "CreatesMinion": "minion", "SummonsTotem": "totem",
    "GeneratesInfusion": "infusion", "GeneratesRemnants": "remnant", "Warcry": "enemy:Intimidated",
}
# tokens too generic to anchor a chain on their own (they still count inside chains, at low specificity)
WEAK_TOKENS = {"stat:Life", "stat:Mana", "stat:Str", "stat:Dex", "stat:Int", "armour", "evasion", "flask", "charm", "kill",
               "trigger", "es"}

GATE_RX = r"(?:\bper\b|\bwhile\b|\bagainst\b|\bif\b|\bfor each\b|\bwhen\b|\bwith\b|\bon\b|\bduring\b|\bif you(?:'ve| have)\b)"
PAYOFF_RX = re.compile(r"damage|critical (hit|damage)|attack speed|cast speed|skill speed|penetrat|\bignore|as extra|more\b|"
                       r"skill speed|magnitude|effect of (shock|chill|ignite|poison|bleed|curse|exposure)|"
                       r"\bdeal\b|\btake\b.*\bincreased\b", re.I)
STRONG_RX = re.compile(r"\bmore\b|as extra|penetrat|\bignore|lucky|\bdouble|always|\btrigger", re.I)
NEG_PAYOFF_RX = re.compile(r"\byou take\b|damage taken|\btaken\b|reduced damage|less damage|cannot deal|deal no", re.I)
CONV_RX = re.compile(r"(physical|fire|cold|lightning|chaos|elemental)? ?damage[^.]{0,40}?(?:as extra|converted to|as) "
                     r"(physical|fire|cold|lightning|chaos)", re.I)
CANAIL_RX = re.compile(r"(physical|fire|cold|lightning|chaos) damage (?:also )?(?:can|contributes to) "
                       r"(shock|freeze|ignite|chill|poison|bleed|electrocute|flammability)", re.I)
AIL_TOKEN = {"shock": "enemy:Shocked", "freeze": "enemy:Frozen", "ignite": "enemy:Ignited", "chill": "enemy:Chilled",
             "poison": "enemy:Poisoned", "bleed": "enemy:Bleeding", "electrocute": "enemy:Electrocuted",
             "flammability": "enemy:Ignited"}
SCOPE_WORDS = {
    "Spell": r"\bspells?\b", "Attack": r"\battacks?\b", "Projectile": r"\bprojectiles?\b", "Minion": r"\bminions?\b",
    "Melee": r"\bmelee\b", "Area": r"\barea\b", "Totem": r"\btotems?\b", "Bow": r"\bbows?\b", "Crossbow": r"\bcrossbows?\b",
    "Spear": r"\bspears?\b", "Quarterstaff": r"\bquarterstaff", "Mace": r"\bmaces?\b", "Unarmed": r"\bunarmed\b",
    "Fire": r"\bfire\b", "Cold": r"\bcold\b", "Lightning": r"\blightning\b", "Chaos": r"\bchaos\b",
    "Physical": r"\bphysical\b", "Elemental": r"\belemental\b", "Herald": r"\bherald", "Warcry": r"\bwarcr",
    "Slam": r"\bslams?\b", "Strike": r"\bstrikes?\b", "Channel": r"\bchannell?ing\b", "DamageOverTime": r"damage over time",
    "Companion": r"\bcompanions?\b", "Curse": r"\bcurses?\b",
}
SCOPE_COMPILED = {k: re.compile(v, re.I) for k, v in SCOPE_WORDS.items()}
KW_SCOPE = {"Spell": "Spell", "Attack": "Attack", "Minion": "Minion", "Projectile": "Projectile", "Melee": "Melee",
            "Fire": "Fire", "Cold": "Cold", "Lightning": "Lightning", "Chaos": "Chaos", "Physical": "Physical",
            "Bow": "Bow", "Crossbow": "Crossbow", "Spear": "Spear", "Warstaff": "Quarterstaff", "Mace": "Mace",
            "Totem": "Totem", "Unarmed": "Unarmed", "Area": "Area"}


@dataclass
class Link:
    token: str
    weight: float
    line: str
    payoff: float = 0.0           # consumers: 0 = utility, 0.6 = increased-style, 1.0 = more/extra/penetration
    scope: tuple = ()             # consumers: skill tags the payoff applies to (empty = any skill)


@dataclass
class Entity:
    key: str                      # "unique:Name" | "keystone:Name" | "asc:Asc:Node" | "skill:Id" | "util:Id"
    kind: str
    name: str
    produces: dict[str, Link] = field(default_factory=dict)
    consumes: dict[str, Link] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def add(self, side: str, link: Link):
        d = self.produces if side == "p" else self.consumes
        old = d.get(link.token)
        if old is None or (link.weight + link.payoff) > (old.weight + old.payoff):
            d[link.token] = link


# ---------------------------------------------------------------------------------------------- line analysis
def _scope_of(line: str, mods: list[dict]) -> tuple:
    sc = set()
    for m in mods:
        for f in (m.get("kw") or []) + (m.get("flags") or []):
            if f in KW_SCOPE:
                sc.add(KW_SCOPE[f])
        for t in m.get("tags") or []:
            if t.get("type") == "SkillType" and not t.get("neg") and isinstance(t.get("skillType"), str):
                sc.add(t["skillType"])
        nm = m.get("name") or ""
        if nm.startswith("Minion") or (m.get("inner") and nm == "MinionModifier"):
            sc.add("Minion")
        for d in DMG:
            if nm.startswith(d + "Damage") and "GainAs" not in nm and "ConvertTo" not in nm:
                sc.add(d)
    if not sc:
        for k, rx in SCOPE_COMPILED.items():
            if rx.search(line):
                sc.add(k)
    # ailment / curse / generic words that are about the gate, not the scaled skill
    return tuple(sorted(sc))


def _payoff(line: str) -> float:
    if NEG_PAYOFF_RX.search(line) and not re.search(r"enem\w* take|enemies .* take", line, re.I):
        return 0.0
    if not PAYOFF_RX.search(line):
        return 0.0
    return 1.0 if STRONG_RX.search(line) else 0.6


def analyse_line(line: str, mods: list[dict]) -> tuple[list[Link], list[Link]]:
    """-> (produced links, consumed links) for one mod line."""
    prod, cons = [], []
    low = line.lower()
    pay = _payoff(line)
    scope = _scope_of(line, mods)
    gated_tokens = set()
    # parsed tags: precise gates
    for m in mods:
        for t in m.get("tags") or []:
            v = t.get("var") or t.get("stat")
            if t.get("type") in ("Condition", "ActorCondition", "Multiplier", "PerStat", "MultiplierThreshold",
                                 "StatThreshold", "PercentStat") and v in TAG_TOKEN:
                gated_tokens.add(TAG_TOKEN[v])
            for vv in t.get("varList") or []:
                if vv in TAG_TOKEN:
                    gated_tokens.add(TAG_TOKEN[vv])
        nm = m.get("name") or ""
        cm = re.match(r"(Physical|Fire|Cold|Lightning|Chaos|Elemental)?Damage(?:GainAs|ConvertTo)(Physical|Fire|Cold|Lightning|Chaos)", nm)
        if cm:
            src = cm.group(1)
            if src and src != "Elemental":
                cons.append(Link("dmg:" + src, 1.0, line, 0.0, ()))
            prod.append(Link("dmg:" + cm.group(2), 1.0, line))
        cm = re.match(r"(Physical|Fire|Cold|Lightning|Chaos)Can(Shock|Freeze|Ignite|Chill|Poison|Bleed|Electrocute)", nm)
        if cm:
            cons.append(Link("dmg:" + cm.group(1), 1.0, line, 0.0, ()))
            prod.append(Link(AIL_TOKEN[cm.group(2).lower()], 1.0, line))
        if nm in ("PowerChargesMax", "FrenzyChargesMax", "EnduranceChargesMax"):
            prod.append(Link("charge:" + nm.replace("ChargesMax", ""), 1.0, line))
        if nm == "MaximumRage" or nm == "Condition:CanGainRage":
            prod.append(Link("rage", 1.0, line))
        if nm.startswith("Enemy") and nm.endswith(("Chance", "Buildup")):
            for a, tok in (("Shock", "enemy:Shocked"), ("Freeze", "enemy:Frozen"), ("Ignite", "enemy:Ignited"),
                           ("Chill", "enemy:Chilled"), ("Poison", "enemy:Poisoned"), ("Bleed", "enemy:Bleeding"),
                           ("HeavyStun", "enemy:Stunned")):
                if a in nm:
                    prod.append(Link(tok, 0.8, line))
        if nm in ("BleedChance", "PoisonChance"):
            prod.append(Link("enemy:Bleeding" if nm == "BleedChance" else "enemy:Poisoned", 0.8, line))
    # text: conversion / can-ailment bridges
    for cm in CONV_RX.finditer(line):
        if cm.group(1) and cm.group(1).lower() != "elemental":
            cons.append(Link("dmg:" + cm.group(1).capitalize(), 1.0, line, 0.0, ()))
        prod.append(Link("dmg:" + cm.group(2).capitalize(), 1.0, line))
    for cm in CANAIL_RX.finditer(line):
        cons.append(Link("dmg:" + cm.group(1).capitalize(), 1.0, line, 0.0, ()))
        prod.append(Link(AIL_TOKEN[cm.group(2).lower()], 1.0, line))
    # text: state mentions, split into gated (consumed) vs produced
    for tok, rx in STATE_COMPILED.items():
        mm = rx.search(line)
        if not mm:
            continue
        before = low[max(0, mm.start() - 45): mm.start()]
        after = low[mm.end(): mm.end() + 25]
        gated = tok in gated_tokens or bool(re.search(GATE_RX + r"[^.,]*$", before)) or \
            bool(re.match(r"\w*\s+(enem\w+|you)?\s*take", after)) or bool(re.search(r"(enemies|enemy) (you |that )?\w*\s*$", before) and "take" in after)
        if tok.startswith("stat:") and not re.search(r"\bper\b|% of|equal to|for every", low):
            gated = False
        if gated and tok.startswith("enemy:"):
            # the state must be on the enemy, not on you ("while Bleeding", "hits against you")
            if re.search(r"against you|on you|while you|you are|while (?:" + STATE_RX[tok][2:] + ")|hits against you", low) \
                    and not re.search(r"enem|target|monster|inflict", low):
                continue
        near = low[max(0, mm.start() - 40): mm.start()]
        if re.search(r"instead of [^.]{0,12}$", near):
            continue            # "Gain Power Charges instead of Frenzy Charges": Frenzy is not produced
        if not gated and re.search(r"consum|detonat|\bremov|\blose\b|\bcannot\b|immun|\bavoid|you are|on you|"
                                   r"inflicted on you|you have|your\b[^.]{0,8}$|\bbenefits? from", near):
            if re.search(r"consum|detonat|benefits? from", near) and pay > 0:
                cons.append(Link(tok, 0.8, line, pay, scope))
            continue            # self-state / consumption / immunity: not a producer of the enemy state
        if gated:
            cons.append(Link(tok, 1.0, line, pay, scope))
        else:
            if tok.startswith("stat:") and re.search(r"^[+-]?\d", line.strip()) is None:
                continue
            w = 1.0 if re.search(r"chance to|\bgain|\binflict|\bapply|\bcreate|\bgrant|\bcan\b|maximum|\balways|"
                                 r"\btrigger|\bon (hit|kill|block|crit)|\bsummon|\bcause|\+\d", low) else 0.4
            prod.append(Link(tok, w, line))
    for t in gated_tokens:
        if not any(c.token == t for c in cons):
            cons.append(Link(t, 1.0, line, pay, scope))
    return prod, cons


# ---------------------------------------------------------------------------------------------- graph
class MechGraph:
    def __init__(self, cat: catmod.Catalog | None = None):
        self.cat = cat or catmod.load()
        self.entities: dict[str, Entity] = {}
        self._build()

    # ---- entities --------------------------------------------------------------------------------------
    def _lines_entity(self, key, kind, name, lines, meta):
        e = Entity(key, kind, name, meta=meta)
        for ln in lines:
            mods = (self.cat.parsed.get(ln) or {}).get("mods", [])
            p, c = analyse_line(ln, mods)
            for x in p:
                e.add("p", x)
            for x in c:
                e.add("c", x)
        return e

    def _skill_entity(self, s: dict, kind: str = "skill") -> Entity:
        e = Entity(f"{kind}:{s['id']}", kind, s["name"], meta={"skill": s["id"], "types": s["types"],
                                                                  "granted_by": s.get("granted_by", []),
                                                                  "asc_granted": s.get("asc_granted", [])})
        t = set(s["types"])
        desc = (s.get("description") or "")
        hit = bool({"Damage", "Attack"} & t)
        for d in s["damage_types"]:
            e.add("p", Link("dmg:" + d, 1.0, f"{s['name']} deals {d} damage"))
            if hit:
                for a in INHERENT_AILMENT[d]:
                    e.add("p", Link(a, 0.6, f"{s['name']} hits with {d} damage"))
        for tok, rx in STATE_COMPILED.items():
            if tok.startswith(("enemy:", "charge:")) or tok in ("corpse", "remnant", "infusion", "rage", "glory"):
                mm = rx.search(desc)
                if mm:
                    pre = desc[max(0, mm.start() - 30): mm.start()].lower()
                    if re.search(r"consum|detonat|against|explod", pre):
                        e.add("c", Link(tok, 1.0, f"{s['name']}: {desc[:160]}", 1.0, ()))
                    else:
                        e.add("p", Link(tok, 0.9, f"{s['name']}: {desc[:160]}"))
        for ty, tok in SKILL_CONSUMES.items():
            if ty in t:
                e.add("c", Link(tok, 1.0, f"{s['name']} consumes {tok.split(':')[-1]}", 1.0, ()))
        for ty, tok in SKILL_PRODUCES_TYPE.items():
            if ty in t:
                e.add("p", Link(tok, 1.0, f"{s['name']} ({ty})"))
        if hit:
            e.add("p", Link("crit", 0.3, f"{s['name']} hits can crit"))
        if "Spell" in t:
            e.add("p", Link("mana_spent", 0.3, f"{s['name']} costs mana"))
        return e

    def _build(self):
        cat = self.cat
        for u in cat.uniques.values():
            if u["slot"] in (None, "Flask", "Charm"):
                continue
            e = self._lines_entity(f"unique:{u['name']}", "unique", u["name"], u["implicits"] + u["lines"],
                                   {"slot": u["slot"], "weapon_class": u.get("weapon_class"), "two_handed": u.get("two_handed"),
                                    "base_type": u.get("base_type"), "grants": u.get("granted_skill_ids", [])})
            for gid in u.get("granted_skill_ids", []):
                gs = cat.skills.get(gid)
                if gs:
                    e.add("p", Link("skill:" + gid, 1.0, f"Grants Skill: {gs['name']}"))
                    if "Meta" in gs["types"] and "Triggers" in gs["types"]:
                        e.add("p", Link("trigger", 1.0, f"Grants {gs['name']}"))
            self.entities[e.key] = e
        for k in cat.keystones.values():
            e = self._lines_entity(f"keystone:{k['name']}", "keystone", k["name"], k["stats"], {})
            self.entities[e.key] = e
        for a in cat.ascendancies.values():
            for nd in a["notables"]:
                e = self._lines_entity(f"asc:{a['name']}:{nd['name']}", "asc", nd["name"], nd["stats"],
                                       {"ascendancy": a["name"], "class": a["class"]})
                self.entities[e.key] = e
        for s in cat.main_skill_candidates():
            self.entities[f"skill:{s['id']}"] = self._skill_entity(s)
        # utility producers: curses, marks, heralds, warcries (non-main helper skills)
        for s in cat.skills.values():
            t = set(s["types"])
            if s["is_gem"] and not s["hidden"] and (s["tier"] or 0) > 0 and ({"AppliesCurse", "Mark", "Warcry"} & t) \
                    and f"skill:{s['id']}" not in self.entities:
                self.entities[f"util:{s['id']}"] = self._skill_entity(s, "util")
        # indexes
        self.producers: dict[str, list[tuple[Entity, Link]]] = defaultdict(list)
        self.consumers: dict[str, list[tuple[Entity, Link]]] = defaultdict(list)
        for e in self.entities.values():
            for tok, l in e.produces.items():
                self.producers[tok].append((e, l))
            for tok, l in e.consumes.items():
                self.consumers[tok].append((e, l))
        n = max(1, len(self.entities))
        self.spec = {}
        for tok in set(self.producers) | set(self.consumers):
            npr = sum(1 for e, l in self.producers.get(tok, []) if l.weight >= 0.6)
            s = math.log(1 + n / (1 + npr)) / math.log(1 + n)
            self.spec[tok] = s * (0.35 if tok in WEAK_TOKENS else 0.6 if tok == "crit" else 1.0)

    def size(self) -> dict:
        return {"entities": len(self.entities), "tokens": len(self.spec),
                "produce_edges": sum(len(v) for v in self.producers.values()),
                "consume_edges": sum(len(v) for v in self.consumers.values()),
                "by_kind": dict(Counter(e.kind for e in self.entities.values()))}

    # ---- compatibility / novelty -------------------------------------------------------------------------
    def skill_tags(self, s: dict) -> set:
        t = set(s["types"]) | set(s["damage_types"])
        for w in s.get("weapon_types") or []:
            t.add({"One Hand Mace": "Mace", "Two Hand Mace": "Mace"}.get(w, w))
        if {"Fire", "Cold", "Lightning"} & t:
            t.add("Elemental")
        if "CreatesMinion" in t:
            t.add("Minion")
        if "MeleeSingleTarget" in t:
            t.add("Strike")
        if "DamageOverTime" in t or "DegenOnlySpellDamage" in t:
            t.add("DamageOverTime")
        return t

    def compat(self, link: Link, skill: dict) -> float:
        """How well a consumer link's payoff scope fits the main skill (0 = not at all)."""
        tags = self.skill_tags(skill)
        if not link.scope:
            return 0.1 if "Minion" in tags else 0.6
        sc = set(link.scope)
        minion_skill = "Minion" in tags
        if ("Minion" in sc) != minion_skill and ("Minion" in sc or "Companion" in sc):
            return 0.0
        if minion_skill and not ({"Minion", "Companion"} & sc):
            return 0.1          # player-side payoffs do not reach minion damage
        hard = sc & {"Spell", "Attack", "Projectile", "Melee", "Bow", "Crossbow", "Spear", "Quarterstaff", "Mace",
                     "Totem", "Unarmed", "Slam", "Strike", "Herald", "Warcry", "Channel", "Companion"}
        if hard and not hard <= tags | {"Minion"}:
            # all-of semantics is too strict for "Spell and Attack" lines: accept any-of for Spell/Attack pairs
            if not (hard & tags and hard <= {"Spell", "Attack", "Projectile", "Area", "Melee"} | tags):
                return 0.0
        elem = sc & {"Fire", "Cold", "Lightning", "Chaos", "Physical", "Elemental"}
        if elem and not elem & tags:
            return 0.15     # e.g. a cold-damage payoff for a lightning skill only works with conversion / gain-as
        return 1.0

    def corpus_pairs(self, parts: list[tuple[str, str]]) -> float:
        """Mean 1/sqrt(1+n) over corpus co-occurrence counts of the given (kind, a||b) pairs (1 = never seen)."""
        pair = (self.cat.corpus or {}).get("pair", {})
        if not parts:
            return 1.0
        vals = [1.0 / math.sqrt(1.0 + pair.get(k, {}).get(ab, 0)) for k, ab in parts]
        return sum(vals) / len(vals)

    def skill_share(self, s: dict) -> float:
        tot = (self.cat.corpus or {}).get("total_characters") or 1
        n = (self.cat.corpus or {}).get("ninja", {}).get("skills", {}).get(s["name"], 0)
        return n / tot

    # ---- chains -------------------------------------------------------------------------------------------
    def _chain(self, kind, path, tokens, main: dict, fit, links, extra=None) -> dict:
        uniques = [p.name for p in path if p.kind == "unique"]
        keys = [p.name for p in path if p.kind == "keystone"]
        ascs = [(p.meta["ascendancy"], p.name) for p in path if p.kind == "asc"]
        utils = [p.meta.get("skill") for p in path if p.kind == "util"]
        parts = [("unique_skill", f"{u}||{main['name']}") for u in uniques]
        parts += [("keystone_skill", f"{k}||{main['name']}") for k in keys]
        parts += [("skill_asc", f"{main['name']}||{a}") for a, _ in ascs]
        if len(uniques) > 1:
            a, b = sorted(uniques[:2])
            parts.append(("unique_unique", f"{a}||{b}"))
        nov = self.corpus_pairs(parts) * (1.0 - min(0.9, 4 * self.skill_share(main)))
        bonus = 1.0 + 0.15 * bool(uniques) + 0.25 * bool(main.get("granted_by"))
        score = fit * (0.25 + 0.75 * nov) * bonus
        names = [p.name for p in path]
        cid = hashlib.sha1(json.dumps([kind, names, tokens, main["id"]]).encode()).hexdigest()[:12]
        thesis = self._thesis(kind, path, tokens, main, links)
        d = {"id": cid, "kind": kind, "length": len(tokens) + 1, "path": [p.key for p in path], "tokens": tokens,
             "main_skill": main["id"], "main_name": main["name"], "uniques": uniques, "keystones": keys,
             "asc_nodes": ascs, "util_skills": utils, "fit": round(fit, 4), "novelty": round(nov, 4),
             "score": round(score, 4), "thesis": thesis, "evidence": [l.line for l in links]}
        if extra:
            d.update(extra)
        return d

    @staticmethod
    def _short(tok: str) -> str:
        return tok.split(":", 1)[-1] if ":" in tok else tok

    def _thesis(self, kind, path, tokens, main, links) -> str:
        n = [p.name for p in path]
        ev = [l.line for l in links]
        if kind == "self_feed":
            return f"{main['name']} applies {self._short(tokens[0])}, which {n[1]} cashes in ('{ev[1][:70]}')"
        if kind == "enabler":
            return f"{n[0]} supplies {self._short(tokens[0])} ('{ev[0][:60]}') that {main['name']} consumes for damage"
        if kind == "graft":
            return (f"{n[0]} provides {self._short(tokens[0])} ('{ev[0][:50]}'), {n[1]} pays off on it "
                    f"('{ev[1][:50]}') for {main['name']}")
        if kind == "bridge":
            return (f"{main['name']} -> {self._short(tokens[0])} -> {n[1]} turns it into {self._short(tokens[1])} -> "
                    f"{n[2]} ('{ev[2][:50]}')")
        return " -> ".join(n)

    def _legal(self, path: list[Entity], main: dict) -> bool:
        us = [{"name": p.name, **p.meta} for p in path if p.kind == "unique"]
        if catmod.slot_conflicts(us):
            return False
        for u in us:
            if u.get("slot") == "Weapon 1" and not catmod.weapon_ok(main, u.get("weapon_class")):
                return False
        ascs = {p.meta["ascendancy"] for p in path if p.kind == "asc"}
        ascs |= {a for a, _ in main.get("asc_granted") or []}
        if len(ascs) > 1:
            return False
        return True

    def _main_for(self, ent: Entity) -> dict | None:
        return self.cat.skills.get(ent.meta.get("skill")) if ent.kind == "skill" else None

    def chains(self, top: int = 400, per_token: int = 40, per_main: int = 6, per_entity: int = 8) -> list[dict]:
        out = []
        mains = [e for e in self.entities.values() if e.kind == "skill"]
        main_skill = {e.key: self.cat.skills[e.meta["skill"]] for e in mains}
        # length 2: self_feed (M -X-> B)
        for tok, cons in self.consumers.items():
            sp = self.spec.get(tok, 0)
            if sp <= 0:
                continue
            payoffs = [(b, l) for b, l in cons if b.kind in ("unique", "keystone", "asc") and l.payoff > 0]
            skill_cons = [(b, l) for b, l in cons if b.kind == "skill"]
            for m, lp in self.producers.get(tok, []):
                if m.kind == "skill":
                    s = main_skill[m.key]
                    for b, lc in payoffs:
                        c = self.compat(lc, s)
                        if c <= 0 or not self._legal([m, b], s):
                            continue
                        fit = sp * lp.weight * lc.payoff * c
                        out.append(self._chain("self_feed", [m, b], [tok], s, fit, [lp, lc]))
                elif m.kind in ("unique", "keystone", "asc", "util") and lp.weight >= 0.6:
                    # enabler (A -X-> M) and graft (A -X-> B => M)
                    for mm, lc in skill_cons:
                        s = main_skill[mm.key]
                        if self._legal([m, mm], s):
                            out.append(self._chain("enabler", [m, mm], [tok], s, 0.7 * sp * lp.weight * lc.payoff, [lp, lc]))
                    for b, lc in payoffs:
                        if b.key == m.key:
                            continue
                        # pick the best few compatible main skills for this payoff (mechanical fit, then novelty)
                        cands = []
                        for e in mains:
                            s = main_skill[e.key]
                            c = self.compat(lc, s)
                            if c < 1.0 or tok in e.produces and e.produces[tok].weight >= 0.6:
                                continue        # main makes X itself: that is a self_feed chain
                            if not self._legal([m, b, e], s):
                                continue
                            cands.append((c * (1 - min(0.9, 4 * self.skill_share(s))), s["id"], e, s))
                        cands.sort(key=lambda x: (-x[0], x[1]))
                        for _, _, e, s in cands[:3]:
                            fit = sp * lp.weight * lc.payoff * 1.0 * 0.9   # length-3 chains: small length discount
                            out.append(self._chain("graft", [m, b, e], [tok], s, fit, [lp, lc]))
        # length 3: bridge (M -X-> B -Y-> C)
        bridges = [e for e in self.entities.values() if e.kind in ("unique", "keystone", "asc")
                   and any(t.startswith("dmg:") or t.startswith("enemy:") for t in e.produces)
                   and any(t.startswith("dmg:") for t in e.consumes)]
        for b in bridges:
            for x, lx in b.consumes.items():
                if not x.startswith("dmg:"):
                    continue
                for y, ly in b.produces.items():
                    if y == x or not (y.startswith("dmg:") or y.startswith("enemy:")):
                        continue
                    payoffs = [(c, lc) for c, lc in self.consumers.get(y, []) if c.kind in ("unique", "keystone", "asc")
                               and lc.payoff > 0 and c.key != b.key]
                    for m, lp in self.producers.get(x, []):
                        if m.kind != "skill" or lp.weight < 1.0:
                            continue
                        s = main_skill[m.key]
                        if y in m.produces and m.produces[y].weight >= 0.6:
                            continue
                        for c, lc in payoffs:
                            cc = self.compat(lc, s)
                            if cc < 0.15 or not self._legal([m, b, c], s):
                                continue
                            # a bridge makes an off-type payoff work: treat the element mismatch as resolved
                            cc = max(cc, 0.8) if y.startswith("dmg:") else cc
                            fit = self.spec.get(y, 0) * ly.weight * lc.payoff * cc * 0.85
                            out.append(self._chain("bridge", [m, b, c], [x, y], s, fit, [lp, ly, lc]))
        # rank + diversity caps
        out.sort(key=lambda d: (-d["score"], d["id"]))
        seen, kept = set(), []
        by_tok, by_main, by_ent = Counter(), Counter(), Counter()
        for d in out:
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            tk = d["tokens"][-1]
            ents = [p for p in d["path"] if not p.startswith("skill:")]
            if by_tok[tk] >= per_token or by_main[d["main_skill"]] >= per_main or any(by_ent[e] >= per_entity for e in ents):
                continue
            by_tok[tk] += 1
            by_main[d["main_skill"]] += 1
            for e in ents:
                by_ent[e] += 1
            kept.append(d)
            if len(kept) >= top:
                break
        self.n_raw_chains = len(out)
        return kept


def export(top: int = 400, path: str | None = None) -> dict:
    g = MechGraph()
    ch = g.chains(top=top)
    info = {"graph": g.size(), "raw_chains": g.n_raw_chains, "exported": len(ch),
            "by_kind": dict(Counter(c["kind"] for c in ch))}
    json.dump({"info": info, "chains": ch}, open(path or os.path.join(catmod.DATA_DIR, "chains.json"), "w"), indent=0)
    return info


def load_chains(path: str | None = None) -> list[dict]:
    p = path or os.path.join(catmod.DATA_DIR, "chains.json")
    if not os.path.exists(p):
        export(path=p)
    return json.load(open(p))["chains"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="mechanics graph: export top chains")
    ap.add_argument("--top", type=int, default=400)
    ap.add_argument("--print", type=int, default=0)
    a = ap.parse_args(argv)
    info = export(a.top)
    print(json.dumps(info))
    if a.print:
        for c in load_chains()[:a.print]:
            print(f"{c['score']:.3f} {c['id']} fit={c['fit']:.2f} nov={c['novelty']:.2f} [{c['kind']}] {c['main_name']}: {c['thesis']}")


if __name__ == "__main__":
    main()
