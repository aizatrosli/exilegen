"""Spirit gems for a chain: which persistent skills help the combo, and which set fits the Spirit there is.

    python3 -m exilegen.builder.spiritx [--chain ID] [--budget 100] [--level 33]     # print the catalogue / a chain's picks

A Spirit gem is a persistent skill that reserves Spirit (a herald, an aura, a persistent buff, a permanent minion,
a meta gem). The mechanics graph (mechgraph) knows none of them as helpers: it only builds curses, marks and
warcries as utility entities. So this module derives each gem's links itself, from the gem's description and
skill types, with mechgraph's own vocabulary (STATE_COMPILED, SKILL_CONSUMES, SKILL_PRODUCES_TYPE):

  needs    states the gem waits for ("when you Freeze ...", "killing Shocked enemies", "Consuming ...")
  gives    states the gem makes ("grants you a Frenzy Charge", "periodically Withers enemies")
  payoff   0..1: what meeting `needs` is worth in damage (a herald's explosion, Berserk's stronger Rage)
  scales   skill tags whose damage the gem raises without any state (Archmage: Spell; Trinity: Elemental)
  boosts   states the gem makes easier to apply, for a skill that already applies them (Overwhelming Presence)

FIX holds what a description cannot say (how strong a payoff is, which skills a buff reaches) and corrects the
few sentences the pattern reads wrongly. Every link keeps the description sentence it came from; `why` quotes it.

What makes a gem good for a chain, in this order (pick() never trades a higher tier for a lower one):

  1. feeds     it gives a state that a payoff part of the chain consumes (or the main skill consumes). A gem that
               itself waits for a state only counts when the chain's main skill makes that state.
     pays_off  the main skill makes a state the gem needs, and the gem turns it into damage.
  2. scales    no state link, but the gem raises damage of the main skill's kind.
  3. utility   generic damage (Attrition, War Banner), defence or recovery. At most one is picked, as a filler,
               and only when the chain gives it a reason (the main skill applies the state it waits for and shares
               its attributes). The rest are listed under "more" as "weaker": true of any build, so not a pick.

A gem the chain cannot use is left out: one that needs a weapon the main skill does not use, an Attack-only
herald for a spell, a gem with no level requirement in the game data (item- or league-granted). Meta gems
(Cast on ..., invocations, Mirage Archer) need skills socketed in them and their reservation grows with those
sockets, and the Skeletal minion gems reserve per minion and more at low gem level: both are in the catalogue,
flagged, and never suggested automatically (a minion gem is suggested only to feed a minion or corpse state).

The Spirit budget: `budget(req, cfg)`. "fixed" is the request's `spirit`; "level" is the quest Spirit held at
`spirit_level` (leveling config `quest_spirit`: character levels, from the campaign guide) plus `spirit_gear`.
GEAR_PRESETS lists what equipment gives, with the PoB data file each number comes from.
"""
from __future__ import annotations

import argparse
import itertools
import json
import re

from . import catalog as catmod
from . import leveling as levelmod
from . import mechgraph
from . import tempo as tempomod

FINAL_LEVEL = 90                # skeletons.Generator's end level: "spirit_level 0" means this
SOURCES = ("fixed", "level")
MORE_MAX = 6
MIN_SCORE = 0.2                 # below this a gem is not worth its Spirit for this chain
UTILITY_PICK_MIN = 0.3         # a utility gem is picked only with a reason in the chain (a state it waits for, its attributes)
ROLE_TIER = {"feeds": 0, "pays_off": 0, "scales": 1, "utility": 2}

# Equipment that gives Spirit, for the app's picker. Numbers from PoB data (checked by the tests when the data is there).
GEAR_PRESETS = [
    {"id": "sceptre", "label": "Sceptre", "spirit": 100,
     "note": "every sceptre base has spirit = 100 (Data/Bases/sceptre.lua, 21 bases)"},
    {"id": "body", "label": "Body armour, Spirit roll", "spirit": 61,
     "note": "top prefix +(57-61) to Spirit at item level 78; +(43-46) at 46, +(30-33) at 16 (Data/ModItem.lua IncreasedSpirit1-8)"},
    {"id": "amulet", "label": "Amulet, Spirit roll", "spirit": 50,
     "note": "top amulet prefix +(47-50) to Spirit at item level 54; +(30-33) at 16 (Data/ModItem.lua IncreasedSpirit1-5)"},
    {"id": "body_mantle", "label": "Corvus or Conjurer Mantle (implicit)", "spirit": 30,
     "note": "body armour base implicit +(20-30) to Spirit (Data/Bases/body.lua)"},
    {"id": "solar_amulet", "label": "Solar Amulet (implicit)", "spirit": 15,
     "note": "amulet base implicit +(10-15) to Spirit (Data/Bases/amulet.lua)"},
]

# Ascendancy notables that change how much Spirit there is (catalogue text, checked in notes()).
ASC_RULES = (("you have no spirit", "none", "{} ({}) leaves the character with no Spirit at all"),
             ("cannot gain spirit from equipment", "no_gear", "{} ({}) stops Spirit from equipment"))

STATE_TOKEN = re.compile(r"^(enemy:|charge:)|^(rage|corpse|remnant|infusion|minion|glory|crit|block|onslaught)$")
# words mechgraph's vocabulary does not have, as they appear in Spirit gem descriptions
EXTRA_RX = {
    "enemy:Frozen": r"\bshatter\w*", "enemy:Bleeding": r"blood loss", "crit": r"critically hit",
    "enemy:Stunned": r"\bstun\b", "enemy:ArmourBroken": r"fully breaking",
}
ALL_CHARGES = ("charge:Power", "charge:Frenzy", "charge:Endurance")
NEED_RX = re.compile(r"(?:\bwhen(?:ever)?|\bif|\bonce) you(?:'ve| have)? [^.]*$|\bkilling [^.]*$|\bconsum\w* [^.]*$|"
                     r"\bshattering|\bhitting [^.]*$|\bbased on [^.]*$|\bfully breaking", re.I)
SKIP_RX = re.compile(r"remov\w*[^.]{0,25}$|destroys?[^.]{0,12}$|\bimmun|\blos\w+ [^.]{0,10}$|\bcannot\b[^.]*$|\buse \w+, |"
                     r"being hit will cause[^.]*$|\btheir [^.]{0,6}$", re.I)

# What the description cannot say, or says in a way the pattern misreads. Keys are skill ids.
#   payoff  damage worth of meeting `needs`        scales  (tags, strength): raises damage of skills with a tag
#   boosts  states made easier to apply            req     the main skill must have one of these types
#   needs_add / gives_add / drop_needs / drop_gives: corrections, each with the reason
FIX: dict[str, dict] = {
    "HeraldOfIcePlayer": {"payoff": 0.6, "req": ["Attack"], "drop_gives": ["enemy:Chilled", "enemy:Frozen"]},
    "HeraldOfThunderPlayer": {"payoff": 0.6, "req": ["Attack"]},
    "HeraldOfAshPlayer": {"payoff": 0.4, "req": ["Attack"], "gives_w": {"enemy:Ignited": 0.6}},   # only around a kill
    "HeraldOfBloodPlayer": {"payoff": 0.6, "drop_gives": ["corpse", "enemy:Bleeding"], "needs_add": ["enemy:Bleeding"]},
    "HeraldOfPlaguePlayer": {"payoff": 0.4, "gives_w": {"enemy:Poisoned": 0.5}},                 # spreads what is there
    "PlagueBearerPlayer": {"payoff": 0.6, "needs_add": ["enemy:Poisoned"], "drop_gives": ["enemy:Poisoned"]},
    "BerserkPlayer": {"payoff": 0.7, "needs_add": ["rage"]},                                     # "strengthens your Rage"
    "ChargeRegulationPlayer": {"payoff": 0.6, "needs_add": list(ALL_CHARGES)},                   # "based on your active Charges"
    "CombatFrenzyPlayer": {},
    "LingeringIllusionPlayer": {"gives_w": {"charge:Power": 0.5, "remnant": 0.5}},               # needs a dodge roll and a kill of the copy
    "MagmaBarrierPlayer": {"needs_add": ["block"], "item": "Shield"},
    "EternalRagePlayer": {"gives_w": {"rage": 1.0}},
    "WitheringPresencePlayer": {"gives_w": {"enemy:Withered": 0.9}, "scales": (["Chaos"], 0.6)},
    "OverwhelmingPresencePlayer": {"drop_gives": ["enemy:Stunned"], "boosts": ["enemy:Shocked", "enemy:Frozen", "enemy:Ignited", "enemy:Chilled",
                                              "enemy:Stunned", "enemy:Electrocuted"]},
    "ArchmagePlayer": {"scales": (["Spell"], 0.9), "not": ["Channel"], "drop_gives": ["dmg:Lightning"]},
    "TrinityPlayer": {"scales": (["Elemental"], 0.7), "hit": True},
    "ElementalConfluxPlayer": {"scales": (["Elemental"], 0.6)},
    "AttritionPlayer": {"generic": (["Attack", "Spell"], 0.22), "hit": True},      # more Hit damage, for any hit skill
    "WarBannerReservationPlayer": {"generic": (["Attack"], 0.18), "drop_needs": ["glory"]},
    "DefianceBannerReservationPlayer": {"req": ["Attack"], "drop_needs": ["glory"]},
    "DreadBannerReservationPlayer": {"req": ["Attack"], "drop_needs": ["glory"]},
    "RagingSpiritsPlayer": {"scales": (["Fire"], 0.4, "summons a Raging Spirit each time a Fire Spell such as {main} is cast"),
                            "req": ["Spell"], "req_all": ["Fire"]},
    "LeylinesPlayer": {"scales": (["Spell"], 0.5)},
    "SiphonElementsPlayer": {},
    "ManaRemnantsPlayer": {"recovery": True, "needs_add": ["crit", "enemy:Shocked", "enemy:Frozen", "enemy:Ignited"], "drop_needs": ["remnant"],
                           "gives_w": {"remnant": 0.9}},        # "Critically Hitting a target with Elemental Ailments"
    "SacrificePlayer": {"needs_add": ["minion"], "drop_gives": ["minion"]},
    "GrimFeastPlayer": {"recovery": True, "needs_add": ["minion"], "drop_gives": ["minion"]},
    "ScavengedPlatingPlayer": {},
    "ShardScavengerPlayer": {"payoff": 0.3, "consumed": True},        # the states must be consumed, not just applied
    "SavageFuryPlayer": {"req": ["Attack"]},
    "BriarpatchPlayer": {"req": ["Attack"], "req_all": ["Physical"]},
    "RavenousSwarmPlayer": {"gives_w": {"enemy:Poisoned": 0.6, "minion": 0.6}},
    "ArcticArmourPlayer": {"drop_gives": ["enemy:Chilled", "enemy:Frozen"]},
    "TimeOfNeedPlayer": {"drop_gives": ["enemy:Cursed"]},
    "RhoaMountPlayer": {"drop_gives": ["enemy:Marked", "enemy:Stunned"], "weapons": ["Bow", "Spear"]},
    "SummonSkeletalBrutesPlayer": {"drop_gives": ["enemy:Intimidated", "enemy:Stunned"]},
    "MetaRemnantsOfKalguurPlayer": {"drop_gives": ["enemy:Stunned"]},
    "MetaCastOnMinionDeathPlayer": {"drop_gives": ["minion"], "needs_add": ["minion"]},
    "VerisiumManifestationPlayer": {"drop_needs": ["minion"]},
    "SummonSkeletalReaversPlayer": {"drop_needs": ["rage"], "drop_gives": ["rage"]},     # the minions' Rage, not yours
}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.])\s+", text or "") if s.strip()]


def read_description(desc: str) -> tuple[dict, dict]:
    """-> (needs, gives): {token: sentence}. A state named after "when you", "killing", "consuming" or
    "based on" is a need; one that is removed, lost or on you is neither; anything else is given."""
    needs, gives = {}, {}
    rx = {k: [v] for k, v in mechgraph.STATE_COMPILED.items() if STATE_TOKEN.search(k)}
    for k, v in EXTRA_RX.items():
        rx.setdefault(k, []).append(re.compile(v, re.I))
    for sent in _sentences(desc):
        for tok, pats in rx.items():
            for pat in pats:
                for m in pat.finditer(sent):
                    pre = sent[: m.start()]
                    if SKIP_RX.search(pre):
                        continue
                    if NEED_RX.search(sent[: m.end()]):
                        needs.setdefault(tok, sent)
                    else:
                        gives.setdefault(tok, sent)
    for tok in list(gives):
        if tok in needs:
            del gives[tok]
    return needs, gives


def _kind(types: set) -> str:
    if "Meta" in types:
        return "meta"
    if "Herald" in types:
        return "herald"
    if "Banner" in types:
        return "banner"
    if {"CreatesMinion", "Minion"} & types and "Buff" not in types or "Companion" in types:
        return "minion"
    if "Aura" in types:
        return "aura"
    return "buff"


def _attrs(skill: dict) -> list[str]:
    return [a for a in ("strength", "dexterity", "intelligence") if a in (skill.get("gem_tags") or [])]


def catalogue(cat: catmod.Catalog | None = None, tempo: dict | None = None, levels: dict | None = None) -> list[dict]:
    """Every skill gem that reserves Spirit, with its cost at gem level 20, unlock level and links."""
    cat = cat or catmod.load()
    tempo = tempomod.load() if tempo is None else tempo
    lv = (levelmod.load() if levels is None else levels).get("skills", {})
    out = []
    for sid, t in sorted(tempo.items()):
        s = cat.skills.get(sid)
        if not t.get("spirit") or not s or s.get("hidden"):
            continue                      # support gems that add Spirit to another gem's reservation are not in the catalogue
        types = set(s.get("types") or [])
        fix = FIX.get(sid, {})
        desc = s.get("description") or ""
        needs, gives = read_description(desc)
        first = (_sentences(desc) or [s["name"]])[0]
        for ty, tok in mechgraph.SKILL_CONSUMES.items():
            if ty in types:
                needs.setdefault(tok, first)
        for ty, tok in mechgraph.SKILL_PRODUCES_TYPE.items():
            if ty in types and STATE_TOKEN.search(tok):
                gives.setdefault(tok, first)
        for tok in fix.get("needs_add", []):
            needs.setdefault(tok, first)
            gives.pop(tok, None)
        for tok in fix.get("drop_needs", []):
            needs.pop(tok, None)
        for tok in fix.get("drop_gives", []):
            gives.pop(tok, None)
        gw = {tok: 0.9 for tok in gives}
        for tok, w in (fix.get("gives_w") or {}).items():
            gw[tok] = w
            gives.setdefault(tok, first)
        kind = _kind(types)
        flags = []
        level = lv.get(sid)
        if level is None:
            flags.append("no level requirement in the game data: granted by an item or a league mechanic, not a gem you can cut")
        if kind == "meta":
            flags.append("meta gem: needs skills socketed in it, and its reservation grows with what is socketed")
        if "MultipleReservation" in types:
            flags.append("reserves this much per minion, and more at low gem level (cost shown is one minion at gem level 20)")
        if "Companion" in types:
            flags.append("a companion reserves a share of your Spirit, not this flat amount")
        if fix.get("item"):
            flags.append(f"needs a {fix['item']}")
        weapons = list(fix.get("weapons") or s.get("weapon_types") or [])
        scales = fix.get("scales")
        out.append({
            "name": s["name"], "skill": sid, "spirit": float(t["spirit"]), "level": level, "kind": kind,
            "types": sorted(types), "damage_types": list(s.get("damage_types") or []), "attrs": _attrs(s),
            "weapon_types": weapons, "description": desc,
            "needs": needs, "gives": {tok: {"w": gw[tok], "line": gives[tok]} for tok in gives},
            "payoff": float(fix.get("payoff", 0.0)), "consumed": bool(fix.get("consumed")),
            "boosts": list(fix.get("boosts") or []),
            "scales": {"tags": list(scales[0]), "strength": scales[1], "say": scales[2] if len(scales) > 2 else None} if scales else None,
            "generic": {"tags": list(fix["generic"][0]), "base": fix["generic"][1]} if fix.get("generic") else None,
            "recovery": bool(fix.get("recovery")),
            "hit": bool(fix.get("hit")), "req": list(fix.get("req") or []), "req_all": list(fix.get("req_all") or []),
            "not": list(fix.get("not") or []), "item": fix.get("item"),
            "auto": level is not None and kind != "meta", "flags": flags,
        })
    return out


# ---------------------------------------------------------------------------------------------- budget
def quest_spirit(level: int, cfg: dict | None = None) -> float:
    """Spirit from campaign quests held at a character level (leveling config `quest_spirit`)."""
    table = levelmod._table((cfg or levelmod.DEFAULT_CONFIG)["quest_spirit"])
    return float(max([v for at, v in table if at <= level], default=0))


def check(req) -> list[str]:
    """What is wrong with the request's Spirit fields (empty = fine)."""
    errors = []
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
    if req.spirit_source not in SOURCES:
        errors.append(f"spirit_source {req.spirit_source!r} is not 'fixed' or 'level'")
    lv = req.spirit_level
    if isinstance(lv, bool) or not isinstance(lv, int) or not 0 <= lv <= 100:
        errors.append("spirit_level must be a whole level from 0 to 100 (0 = the final level)")
    if not num(req.spirit_gear) or req.spirit_gear < 0:
        errors.append("spirit_gear must be a number, 0 or more")
    if not isinstance(req.spirit_gems, bool):
        errors.append("spirit_gems must be true or false")
    return errors


def budget(req, cfg: dict | None = None) -> dict:
    """The Spirit limit of a request: {"budget", "source"} and, for source "level", {"level", "quest", "gear"}."""
    if getattr(req, "spirit_source", "fixed") != "level":
        return {"budget": float(req.spirit), "source": "fixed"}
    level = int(req.spirit_level) or FINAL_LEVEL
    quest = quest_spirit(level, cfg)
    gear = float(req.spirit_gear or 0)
    return {"budget": quest + gear, "source": "level", "level": level, "quest": quest, "gear": gear}


def notes(chain: dict, cat: catmod.Catalog) -> list[dict]:
    """Ascendancy notables that change the chain's Spirit: [{"rule": "none"|"no_gear", "taken": bool, "text"}].
    `taken` is true when the notable is a part of the chain, false when it is only on the chain's ascendancy."""
    main = cat.skills.get(chain.get("main_skill")) or {}
    ascs = [a for a, _ in chain.get("asc_nodes") or []] + [a for a, _ in main.get("asc_granted") or []]
    taken = {n for _, n in chain.get("asc_nodes") or []}
    out = []
    for a in dict.fromkeys(ascs):
        for nd in (cat.ascendancies.get(a) or {}).get("notables") or []:
            for st in nd.get("stats") or []:
                for needle, rule, text in ASC_RULES:
                    if needle in st.lower():
                        out.append({"rule": rule, "taken": nd["name"] in taken, "text": text.format(nd["name"], a)})
    return out


# ---------------------------------------------------------------------------------------------- scoring
class Scorer:
    """Scores Spirit gems for chains of one mechanics graph."""

    def __init__(self, graph: mechgraph.MechGraph, cat: catmod.Catalog | None = None, gems: list[dict] | None = None,
                 tempo: dict | None = None):
        self.g = graph
        self.cat = cat or graph.cat
        self.tempo = tempomod.load() if tempo is None else tempo
        self.gems = catalogue(self.cat, self.tempo) if gems is None else gems
        self.by_name = {x["name"].lower(): x for x in self.gems}
        self.by_id = {x["skill"].lower(): x for x in self.gems}

    def find(self, ref: str) -> dict | None:
        r = str(ref).strip().lower()
        return self.by_name.get(r) or self.by_id.get(r) or self.by_id.get(r + "player")

    # ---- the chain's side
    def _sides(self, chain: dict, path: list[str] | None = None) -> dict:
        main = self.cat.skills.get(chain["main_skill"]) or {"id": chain["main_skill"], "name": chain.get("main_name"),
                                                             "types": [], "damage_types": []}
        ment = self.g.entities.get("skill:" + chain["main_skill"])
        made = {t: l for t, l in (ment.produces if ment else {}).items() if l.weight >= 0.6}
        wants: dict[str, tuple] = {}             # token -> (payoff 0..1, part name, line, in chain tokens)
        for t, l in (ment.consumes if ment else {}).items():
            if l.payoff > 0:
                wants[t] = (l.payoff, main.get("name"), l.line, t in chain["tokens"])
        for key in (chain["path"] if path is None else path):
            e = self.g.entities.get(key)
            if e is None or key == "skill:" + chain["main_skill"]:
                continue
            for t, l in e.produces.items():
                if e.kind in ("unique", "keystone", "asc", "util") and l.weight >= 0.6 and STATE_TOKEN.search(t):
                    made.setdefault(t, l)
            if e.kind not in ("unique", "keystone", "asc"):
                continue
            for t, l in e.consumes.items():
                if l.payoff <= 0:
                    continue
                c = self.g.compat(l, main) if main.get("types") else 0.6
                v = l.payoff * (1.0 if c >= 0.6 else c)
                if c > 0 and (t not in wants or v > wants[t][0]):
                    wants[t] = (v, e.name, l.line, t in chain["tokens"])
        return {"main": main, "tags": self.g.skill_tags(main) if main.get("types") is not None else set(),
                "made": made, "main_made": {t for t, l in (ment.produces if ment else {}).items() if l.weight >= 0.6},
                "eats": set((ment.consumes if ment else {})), "wants": wants}

    def usable(self, gem: dict, side: dict) -> str | None:
        """None when the chain can use the gem, else the reason it cannot."""
        main, tags = side["main"], side["tags"]
        types = set(main.get("types") or [])
        if gem["req"] and not set(gem["req"]) & tags:
            return f"works with {' or '.join(gem['req'])} skills only"
        if gem["req_all"] and not set(gem["req_all"]) <= tags:
            return f"needs a {' '.join(gem['req_all'])} skill"
        if set(gem["not"]) & tags:
            return f"does not work with {' or '.join(gem['not'])} skills"
        if gem["weapon_types"]:
            mw = set(main.get("weapon_types") or [])
            if "Attack" not in types or "NonWeaponAttack" in types or (mw and not mw & set(gem["weapon_types"])):
                return "needs a weapon type the main skill does not use"
        if gem["skill"] == main.get("id"):
            return "is the main skill"
        return None

    def score(self, gem: dict, chain: dict, side: dict | None = None) -> dict | None:
        """The gem's best role for the chain: {"role", "token", "score", "why"}, or None when it does nothing."""
        side = side or self._sides(chain)
        if self.usable(gem, side):
            return None
        main, tags, made, wants = side["main"], side["tags"], side["made"], side["wants"]
        mname = main.get("name") or chain.get("main_name")
        minion_main = "Minion" in tags
        best = None

        def offer(role, token, score, why):
            nonlocal best
            if best is None or score > best["score"]:
                best = {"role": role, "token": token, "score": round(min(score, 1.0), 3), "why": why}

        state_needs = [t for t in gem["needs"] if STATE_TOKEN.search(t) and t != "crit"]
        if gem["consumed"]:
            met = [t for t in state_needs if t in side["eats"]]
        else:
            met = [t for t in state_needs if t in made or (t == "minion" and minion_main)]
        ready = not state_needs or bool(met)
        # 1a. feeds: the gem gives a state a payoff part (or the main skill) consumes
        for tok, gv in gem["gives"].items():
            if tok not in wants or not STATE_TOKEN.search(tok) or tok in mechgraph.WEAK_TOKENS:
                continue
            pay, part, line, in_chain = wants[tok]
            if gem["kind"] == "minion" and tok not in ("minion", "corpse"):
                continue
            v = gv["w"] * pay * (1.0 if in_chain else 0.75)
            if tok in side["main_made"] and self.g.entities["skill:" + main["id"]].produces[tok].weight >= 0.9:
                v *= 0.6                 # the main skill already keeps this state up
            if not ready:
                continue                 # it waits for a state nothing in the chain makes
            cond = f" once {mname} applies {_short(met[0])}" if state_needs else ""
            offer("feeds", tok, 0.6 + 0.4 * v,
                  f"{gem['name']} supplies {_short(tok)}{cond} (\"{_clip(gv['line'])}\"), which {part} pays off on (\"{_clip(line)}\").")
        # 1a'. boosts: makes the chain's own state easier to apply
        for tok in gem["boosts"]:
            if tok in wants and tok in side["main_made"] and wants[tok][3]:
                pay, part, line, _ = wants[tok]
                offer("feeds", tok, 0.6 + 0.4 * 0.5 * pay,
                      f"{gem['name']} makes {_short(tok)} easier for {mname} to apply (\"{_clip(gem['description'])}\"), "
                      f"and {part} pays off on it (\"{_clip(line)}\").")
        # 1b. pays_off: the main skill makes a state the gem turns into damage
        # (charges a payoff part already spends are not on offer: the gem would compete with the chain)
        cash = [t for t in met if not (t.startswith("charge:") and t in wants)]
        if gem["payoff"] > 0 and cash and not (minion_main and gem["kind"] != "minion"):
            tok = max(cash, key=lambda t: (t in chain["tokens"], made[t].weight if t in made else 1.0, t))
            w = made[tok].weight if tok in made else 1.0
            if "Attack" in gem["types"] and "Attack" not in tags:
                w = 0.0
            if w > 0:
                src = mname if tok in side["main_made"] or tok in side["eats"] else "the chain"
                verb = "consumes" if gem["consumed"] else "applies" if tok.startswith("enemy:") else "makes"
                offer("pays_off", tok, 0.55 + 0.35 * w * gem["payoff"] + (0.05 if tok in chain["tokens"] else 0.0),
                      f"{src} {verb} {_short(tok)}, which {gem['name']} cashes in (\"{_clip(gem['needs'][tok])}\").")
        # 2. scales: raises damage of the main skill's kind, no state needed
        sc = gem["scales"]
        if sc and set(sc["tags"]) & tags and not minion_main and ready:
            hit = bool({"Damage", "Attack"} & set(main.get("types") or [])) and "DegenOnlySpellDamage" not in tags
            if not gem["hit"] or hit:
                kind = sorted(set(sc["tags"]) & tags)[0]
                say = (sc["say"] or "raises {kind} damage, which is what {main} deals").format(kind=kind, main=mname)
                offer("scales", None, 0.3 + 0.25 * sc["strength"], f"{gem['name']} {say} (\"{_clip(gem['description'])}\").")
        # 3. utility: generic damage, defence or recovery, matched to the main skill's attributes
        share = len(set(gem["attrs"]) & set(_attrs(main))) / max(1, len(gem["attrs"]))
        gen = gem["generic"]
        if best is None and gen and set(gen["tags"]) & tags and not minion_main:
            hit = bool({"Damage", "Attack"} & set(main.get("types") or [])) and "DegenOnlySpellDamage" not in tags
            if not gem["hit"] or hit:
                offer("utility", None, gen["base"] + 0.06 * share,
                      f"{gem['name']} is generic damage, not tied to this chain: \"{_clip(gem['description'])}\".")
        plain = gem["payoff"] == 0 and not sc and not gen and gem["kind"] in ("buff", "aura", "banner") and gem["item"] is None
        if best is None and plain and (not gem["gives"] or gem["recovery"]) and not gem["boosts"]:
            if not state_needs or met:
                v = 0.14 + 0.1 * share + (0.08 if met else 0.0) - (0.04 if gem["spirit"] > 30 else 0.0)
                link = f" {mname} applies the {_short(met[0])} it waits for." if met else ""
                offer("utility", None, v, f"{gem['name']} is defence or recovery, not damage: \"{_clip(gem['description'])}\".{link}")
        return best

    def candidates(self, chain: dict, path: list[str] | None = None) -> list[dict]:
        """Every gem that does something for the chain, best first, in the graph document's pick shape
        (plus "tier" and "job", which pick() uses and strips)."""
        side = self._sides(chain, path)
        out = []
        for gem in self.gems:
            if not gem["auto"]:
                continue
            r = self.score(gem, chain, side)
            if r is None or r["score"] < MIN_SCORE:
                continue
            job = ("feeds:" + r["token"]) if r["role"] == "feeds" else ("pays_off:" + r["token"] + (":" + ",".join(sorted(gem["gives"])) if gem["gives"] else "")) \
                if r["role"] == "pays_off" else ("scales:" + ",".join(sorted(gem["scales"]["tags"]))) if r["role"] == "scales" else "utility"
            c = {"name": gem["name"], "skill": gem["skill"], "spirit": gem["spirit"], "role": r["role"]}
            if r["token"]:
                c["token"] = r["token"]
            c.update(why=r["why"], level=gem["level"], score=r["score"], tier=ROLE_TIER[r["role"]], job=job)
            out.append(c)
        out.sort(key=lambda c: (c["tier"], -c["score"], c["spirit"], c["name"]))
        return out

    def own(self, chain: dict, path: list[str] | None = None) -> float:
        """Spirit the chain's own skills reserve (the main skill and the helper skills on its path)."""
        if path is None and "tempo" in chain:
            return float(chain["tempo"].get("spirit_total", 0.0))
        total, seen = 0.0, set()
        for key in ["skill:" + chain["main_skill"]] + list(chain["path"] if path is None else path):
            e = self.g.entities.get(key)
            sid = e.meta.get("skill") if e is not None and e.kind in ("skill", "util") else None
            if sid and sid not in seen:
                seen.add(sid)
                total += float((self.tempo.get(sid) or {}).get("spirit", 0.0))
        return total

    def suggest(self, chain: dict, limit: float, level: int | None = None, path: list[str] | None = None,
                gear: float = 0.0) -> dict:
        """chain.spirit of the graph document. `level` (None = no level limit) blocks gems that unlock later;
        `gear` is the part of `limit` that comes from equipment (an ascendancy can void it)."""
        ns = notes(chain, self.cat)
        for n in ns:
            if n["taken"]:
                limit = 0.0 if n["rule"] == "none" else max(0.0, limit - gear)
        own = self.own(chain, path)
        out = pick(self.candidates(chain, path), limit - own, level)
        out = {"budget": _n(limit), "own": _n(own), "free": _n(limit - own), **out}
        if ns:
            out["notes"] = [n["text"] + ("" if n["taken"] else " (only if that notable is taken)") for n in ns]
        return out


def _n(x: float):
    return int(x) if float(x).is_integer() else round(float(x), 1)


def _short(tok: str) -> str:
    return tok.split(":", 1)[-1] if ":" in tok else tok


def _clip(s: str, n: int = 110) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------------------------- picking
def _best_set(cands: list[dict], free: float, jobs: set) -> list[dict]:
    """The subset with the highest total score that fits `free` and does no job twice (ties: less Spirit)."""
    cands = [c for c in cands if c["spirit"] <= free and c["job"] not in jobs][:12]
    best, bv = [], (0.0, 0.0)
    for n in range(1, len(cands) + 1):
        for combo in itertools.combinations(cands, n):
            if sum(c["spirit"] for c in combo) > free or len({c["job"] for c in combo}) < n:
                continue
            v = (round(sum(c["score"] for c in combo), 6), -sum(c["spirit"] for c in combo))
            if v > bv:
                best, bv = list(combo), v
    return best


def pick(cands: list[dict], free: float, level: int | None = None) -> dict:
    """-> {"picks", "more", "used"}. Tier by tier (state links, then scaling, then one utility gem): the best
    set of a tier takes its Spirit before the next tier sees what is left. A gem that unlocks above `level`
    is never picked."""
    picks, jobs, left = [], set(), float(free)
    late = [c for c in cands if level is not None and c["level"] is not None and c["level"] > level]
    ok = [c for c in cands if c not in late]
    for tier in sorted(set(ROLE_TIER.values())):
        pool = [c for c in ok if c["tier"] == tier]
        if tier == ROLE_TIER["utility"]:
            pool = [c for c in pool if c["spirit"] <= left and c["score"] >= UTILITY_PICK_MIN][:1]
        got = _best_set(pool, left, jobs)
        picks += sorted(got, key=lambda c: (-c["score"], c["name"]))
        jobs |= {c["job"] for c in got}
        left -= sum(c["spirit"] for c in got)
    more = []
    for c in sorted((c for c in cands if c not in picks), key=lambda c: (c["tier"], -c["score"], c["name"])):
        filler = c["tier"] == ROLE_TIER["utility"] and ("utility" in jobs or c["score"] < UTILITY_PICK_MIN)
        why = "level" if c in late else "weaker" if c["job"] in jobs or filler else "budget" if c["spirit"] > left else "weaker"
        d = {**_strip(c), "blocked": why}
        same = next((p["name"] for p in picks if p["job"] == c["job"]), None)
        if why == "weaker" and same:
            d["same_job_as"] = same       # a pick already does this job: adding this gem too would be redundant
        more.append(d)
    return {"picks": [_strip(c) for c in picks], "more": more[:MORE_MAX], "used": _n(sum(c["spirit"] for c in picks))}


def _strip(c: dict) -> dict:
    return {k: v for k, v in c.items() if k not in ("tier", "job")}


def info(req, cfg: dict | None = None, gems: list[dict] | None = None) -> dict:
    """mechanics.info.spirit of the graph document."""
    gems = catalogue() if gems is None else gems
    return {**{k: _n(v) if isinstance(v, float) else v for k, v in budget(req, cfg).items()},
            "gear_presets": [dict(p) for p in GEAR_PRESETS], "gems": len(gems),
            "suggested_from": sum(1 for x in gems if x["auto"])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Spirit gems: the catalogue, or one chain's suggestions")
    ap.add_argument("--chain", help="chain id: print its candidates and picks")
    ap.add_argument("--budget", type=float, default=100.0)
    ap.add_argument("--level", type=int, default=None)
    a = ap.parse_args(argv)
    cat = catmod.load()
    if not a.chain:
        for x in catalogue(cat):
            print(json.dumps({k: x[k] for k in ("name", "skill", "spirit", "level", "kind", "auto", "payoff", "scales", "boosts", "flags")}
                             | {"needs": sorted(x["needs"]), "gives": {t: v["w"] for t, v in x["gives"].items()}}))
        return 0
    from . import chainscore
    ch = next((c for c in chainscore.pool_chains() if c["id"] == a.chain), None)
    if ch is None:
        print(json.dumps({"error": f"unknown chain {a.chain!r}"}))
        return 2
    print(json.dumps(Scorer(mechgraph.MechGraph(cat), cat).suggest(ch, a.budget, a.level), indent=1))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
