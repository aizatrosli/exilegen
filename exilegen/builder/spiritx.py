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

What a gem is worth to a chain is one score, 0..1 (pick() takes the set worth most for the Spirit there is):

  feeds     it gives a state that a payoff part of the chain (or the main skill) consumes. Worth = how often it
            gives the state x the payoff. A gem that itself waits for a state only counts when the chain makes it.
  pays_off  the chain makes a state the gem needs, and the gem turns it into damage. The five heralds act on a
            kill: they are marked "clear_only", scored low, and at most one is picked.
  scales    no state link, but the gem raises the main skill's own damage. Said to be generic; strength depends
            on the build (Trinity needs two elements, Archmage a Mana build, Presence auras a melee skill).
  utility   minions of its own, defence or recovery. At most one, and only with a reason in the chain.

Every link of the mechanics graph is read again first (really_makes): "shockwave" is not Shock, "Shocks you" is
not on the enemy, a skill that consumes an ailment does not apply it, a maximum or an "instead of" swap makes
nothing, and a drawback ("Take 100 Fire Damage when you Ignite") is not a payoff.

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
import glob
import json
import os
import re

from . import catalog as catmod
from . import leveling as levelmod
from . import mechgraph
from . import tempo as tempomod

FINAL_LEVEL = 90                # skeletons.Generator's end level: "spirit_level 0" means this
SOURCES = ("fixed", "level")
MORE_MAX = 6
LEVELS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "spirit_levels.json")
# weapon classes a persistent gem cannot be used with, whatever the main skill is (not martial weapons)
CASTER_WEAPONS = {"Wand", "Sceptre", "Staff"}
MIN_SCORE = 0.2                 # below this a gem is not listed for this chain at all
PICK_MIN = 0.35                 # below this a gem is listed under "more" but never picked: true, but not worth its Spirit
UTILITY_PICK_MIN = 0.3         # a utility gem is picked only with a reason in the chain (a state it waits for, its attributes)
GEM_COST = 0.12                 # what each further gem must add to be picked: a socket, supports, a button
MAX_PICKS = 4
PRESENCE_METRES = 4             # base Presence radius (PoB Data/Misc.lua base_presence_radius = 40)
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
    # the five heralds act on a kill ("kills"): an explosion of their own while clearing packs, nothing on a lone boss
    "HeraldOfIcePlayer": {"payoff": 0.6, "req": ["Attack"], "drop_gives": ["enemy:Chilled", "enemy:Frozen"], "kills": True,
                          "say": "when an Attack hit of {main} kills a Frozen enemy, it explodes for Cold Attack damage"},
    "HeraldOfThunderPlayer": {"payoff": 0.6, "req": ["Attack"], "kills": True,
                              "say": "after an Attack hit of {main} kills a Shocked enemy, the next Attack hits call lightning bolts"},
    # its explosion Ignites the neighbours of an enemy you just overkilled, never the enemy you are fighting: not a feeder
    "HeraldOfAshPlayer": {"req": ["Attack"], "kills": True, "drop_gives": ["enemy:Ignited"],
                          "generic": (["Attack"], 0.22, "{gem}: an enemy that {main} overkills explodes and Ignites the enemies around it")},
    "HeraldOfBloodPlayer": {"payoff": 0.6, "own": True, "kills": True,
                            "say": "an enemy killed while Bleeding explodes for Physical damage", "drop_gives": ["corpse", "enemy:Bleeding"], "needs_add": ["enemy:Bleeding"]},
    "HeraldOfPlaguePlayer": {"payoff": 0.4, "own": True, "kills": True, "dmg": ["Physical", "Chaos"], "drop_gives": ["enemy:Poisoned"],
                             "needs_add": ["enemy:Poisoned"], "say": "killing a Poisoned enemy spreads its Poison to the enemies around it"},
    # Poison is worked out from Physical and Chaos hit damage only (PoB Modules/Data.lua, ScalesFrom)
    "PlagueBearerPlayer": {"payoff": 0.6, "dmg": ["Physical", "Chaos"], "needs_add": ["enemy:Poisoned"], "drop_gives": ["enemy:Poisoned"]},
    "BerserkPlayer": {"payoff": 0.7, "needs_add": ["rage"], "req": ["Attack"],                   # Rage is Attack damage
                      "say": "Berserk makes each point of Rage stronger", "spends": "ConsumesRage",
                      "downside": "you lose Life whenever your Rage is not dropping"},
    "ChargeRegulationPlayer": {"payoff": 0.6, "needs_add": list(ALL_CHARGES),                    # "based on your active Charges"
                               "say": "Charge Regulation turns held Charges into a Buff", "downside": "it Consumes a Charge every few seconds"},
    "CombatFrenzyPlayer": {"own": True, "gives_w": {"charge:Frenzy": 0.5}},                      # "once every few seconds"
    # one Remnant per dodge roll, and only if an enemy destroys the copy
    "LingeringIllusionPlayer": {"gives_w": {"charge:Power": 0.12, "remnant": 0.12}},
    "MagmaBarrierPlayer": {"needs_add": ["block"], "item": "Shield"},
    "EternalRagePlayer": {"gives_w": {"rage": 1.0}},
    "WitheringPresencePlayer": {"gives_w": {"enemy:Withered": 0.9}, "scales": (["Chaos"], 0.6), "presence": 0.6},
    "OverwhelmingPresencePlayer": {"drop_gives": ["enemy:Stunned"], "presence": 0.0, "boosts": ["enemy:Shocked", "enemy:Frozen", "enemy:Ignited", "enemy:Chilled",
                                              "enemy:Stunned", "enemy:Electrocuted"]},
    # extra Lightning damage per 100 maximum Mana, and a higher Mana cost: strong only in a build that has or spends Mana
    "ArchmagePlayer": {"scales": (["Spell"], 0.2, "adds Lightning damage to {main} for every 100 maximum Mana, so it is worth as much as the Mana pool"),
                       "req": ["Spell"], "not": ["Channel", "Triggered", "InbuiltTrigger"], "drop_gives": ["dmg:Lightning"],
                       "hit": True, "mana": 0.9},
    # more Elemental damage per Affinity; a skill with one element fills one of the three Affinities
    "TrinityPlayer": {"scales": (["Elemental"], 0.7, "gives more Elemental damage as {main} alternates its elements"), "hit": True, "elements": 2,
                      "few": (0.2, "gives more Elemental damage per Affinity, but {main} deals one element and fills only one of the three")},
    # 59% more damage with one element at a time, re-rolled every 8 seconds: about a third of that on average
    "ElementalConfluxPlayer": {"scales": (["Elemental"], 0.4, "gives much more damage with one element at a time, a third of the time for each element")},
    # more Hit damage to Rare and Unique enemies the longer they have been in your Presence
    "AttritionPlayer": {"scales": (["Attack", "Spell"], 0.5, "gives more Hit damage to Rare and Unique enemies the longer the fight lasts (bosses, not packs)"),
                        "hit": True, "presence": 0.4},
    "WarBannerReservationPlayer": {"generic": (["Attack"], 0.18), "req": ["Attack"], "drop_needs": ["glory"]},
    "DefianceBannerReservationPlayer": {"req": ["Attack"], "drop_needs": ["glory"]},
    "DreadBannerReservationPlayer": {"req": ["Attack"], "drop_needs": ["glory"]},
    # minions of its own: they do not raise the main skill's damage
    "RagingSpiritsPlayer": {"generic": (["Fire"], 0.3, "{gem} adds minions of its own each time a Fire Spell such as {main} is cast; it does not raise {main}'s damage"),
                            "req": ["Spell"], "req_all": ["Fire"],
                            "not": ["Triggered", "InbuiltTrigger", "Persistent"]},      # "Fire Spells you use yourself"
    "LeylinesPlayer": {"scales": (["Spell"], 0.5), "req": ["Spell"]},
    # a 5% chance per ailment, and the Infusion has the element of the ailment
    "SiphonElementsPlayer": {"own": True, "gives_w": {"infusion": 0.1, "remnant": 0.1}},
    "ManaRemnantsPlayer": {"recovery": True, "own": True, "needs_add": ["crit", "enemy:Shocked", "enemy:Frozen", "enemy:Ignited"], "drop_needs": ["remnant"],
                           "gives_w": {"remnant": 0.9}},        # "Critically Hitting a target with Elemental Ailments"
    "SacrificePlayer": {"needs_add": ["minion"], "drop_gives": ["minion"], "main_stat": "is_resummoning_minion",
                        "req_all": ["CreatesUndeadMinion"]},            # "your undead Reviving Minions"
    "GrimFeastPlayer": {"recovery": True, "needs_add": ["minion"], "drop_gives": ["minion"],
                        "main_stat": "is_resummoning_minion"},          # "your Reviving Minions store ..."
    "ScavengedPlatingPlayer": {},
    "ShardScavengerPlayer": {"payoff": 0.3, "consumed": True},        # the states must be consumed, not just applied
    "SavageFuryPlayer": {"req": ["Attack"], "req_all": ["Shapeshift"]},     # "forcing you into animal form"
    "BriarpatchPlayer": {"req": ["Attack"], "req_all": ["Physical"]},
    "RavenousSwarmPlayer": {"gives_w": {"enemy:Poisoned": 0.6, "minion": 0.6}},
    "ArcticArmourPlayer": {"drop_gives": ["enemy:Chilled", "enemy:Frozen"]},
    "TimeOfNeedPlayer": {"drop_gives": ["enemy:Cursed"]},
    "RhoaMountPlayer": {"drop_gives": ["enemy:Marked", "enemy:Stunned"], "weapons": ["Bow", "Spear"]},
    "SummonSkeletalBrutesPlayer": {"drop_gives": ["enemy:Intimidated", "enemy:Stunned"], "drop_needs": ["enemy:Frozen"]},
    "MetaRemnantsOfKalguurPlayer": {"drop_gives": ["enemy:Stunned"]},
    "MetaCastOnMinionDeathPlayer": {"drop_gives": ["minion"], "needs_add": ["minion"]},
    "VerisiumManifestationPlayer": {"drop_needs": ["minion"], "req": ["Attack"]},
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


def parse_levels(pob_root: str) -> dict[str, list]:
    """Skills whose Spirit cost changes with gem level -> [[character level the gem level needs, cost], ...]."""
    out = {}
    for path in sorted(glob.glob(os.path.join(pob_root, "Data", "Skills", "*.lua"))):
        text = open(path, encoding="utf-8", errors="replace").read()
        heads = list(re.finditer(r'^skills\["([^"]+)"\]\s*=\s*\{', text, re.M))
        for i, m in enumerate(heads):
            body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
            rows = []
            for k, row in re.findall(r"^\t\t\[(\d+)\] = \{(.*)\},?\s*$", body, re.M):
                lv, sp = re.search(r"levelRequirement = (\d+)", row), re.search(r"spiritReservationFlat = (\d+(?:\.\d+)?)", row)
                if lv and sp and int(k) <= 20:
                    rows.append([int(lv.group(1)), float(sp.group(1))])
            if len({c for _, c in rows}) > 1:
                out[m.group(1)] = rows
    return out


def load_levels(path: str = LEVELS_PATH) -> dict[str, list]:
    return json.load(open(path))["skills"] if os.path.exists(path) else {}


def cost_at(spirit: float, by_level: list | None, level: int | None) -> float:
    """Spirit a gem reserves for a character of `level` (None: gem level 20): the highest gem level that
    character can use. Skeleton gems cost far more at low gem level."""
    if level is None or not by_level:
        return float(spirit)
    ok = [c for at, c in by_level if at <= level]
    return float(min(ok)) if ok else float(by_level[0][1])


def catalogue(cat: catmod.Catalog | None = None, tempo: dict | None = None, levels: dict | None = None) -> list[dict]:
    """Every skill gem that reserves Spirit, with its cost at gem level 20, unlock level and links."""
    by_level = load_levels()
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
        if fix.get("item"):
            flags.append(f"needs a {fix['item']}")
        weapons = list(fix.get("weapons") or s.get("weapon_types") or [])
        scales = fix.get("scales")
        out.append({
            "name": s["name"], "skill": sid, "spirit": float(t["spirit"]), "level": level, "kind": kind,
            "types": sorted(types), "damage_types": list(s.get("damage_types") or []), "attrs": _attrs(s),
            "weapon_types": weapons, "description": desc, "spirit_by_level": by_level.get(sid),
            "own": bool(fix.get("own")), "main_stat": fix.get("main_stat"),
            "needs": needs, "gives": {tok: {"w": gw[tok], "line": gives[tok]} for tok in gives},
            "payoff": float(fix.get("payoff", 0.0)), "consumed": bool(fix.get("consumed")),
            "boosts": list(fix.get("boosts") or []),
            "scales": {"tags": list(scales[0]), "strength": scales[1], "say": scales[2] if len(scales) > 2 else None} if scales else None,
            "generic": {"tags": list(fix["generic"][0]), "base": fix["generic"][1],
                        "say": fix["generic"][2] if len(fix["generic"]) > 2 else None} if fix.get("generic") else None,
            "kills": bool(fix.get("kills")), "say": fix.get("say"), "downside": fix.get("downside"), "presence": fix.get("presence"),
            "dmg": list(fix.get("dmg") or []), "mana": fix.get("mana"), "elements": fix.get("elements"), "few": fix.get("few"),
            "spends": fix.get("spends"),
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


# ---------------------------------------------------------------------------------------------- reading the graph's links again
# The mechanics graph matches states by word. Before a Spirit gem is hung on a link, the sentence is read again:
NOT_MADE_RX = re.compile(r"\bconsum|\bexpend|\bspent\b|\bspending\b|\boffer\b|\bmaximum\b|\binstead of\b|\bremov|\bimmun", re.I)
ON_YOU_RX = re.compile(r"\b(?:shock|ignite|freeze|chill|poison|curse|stun|blind)s you\b|\bto be \w+|"
                       r"\bwhile (?:you are |you're )?(?:shocked|ignited|frozen|chilled|poisoned|bleeding)\b", re.I)
MAGNITUDE_RX = re.compile(r"contributes? to [^.]*magnitude", re.I)
DRAWBACK_RX = re.compile(r"(?:^|: )take \d|\byou take\b|contributes? to\b", re.I)
WORD_NOT = {"enemy:Shocked": re.compile(r"shockwave|aftershock", re.I)}
NATURAL = {"Fire": ("enemy:Ignited",), "Cold": ("enemy:Frozen", "enemy:Chilled"), "Lightning": ("enemy:Shocked",)}
SWAP_RX = re.compile(r"(fire|cold|lightning) damage from hits contributes to ([^.]*?) instead of ([^.]*)", re.I)
AILMENT_WORDS = (("shock", "enemy:Shocked"), ("ignit", "enemy:Ignited"), ("flammab", "enemy:Ignited"), ("freez", "enemy:Frozen"),
                 ("chill", "enemy:Chilled"))
INFUSION_RX = re.compile(r"consumes? an? (\w+) infusion", re.I)
INFUSION_FROM = {"fire": "enemy:Ignited", "cold": "enemy:Frozen", "lightning": "enemy:Shocked"}
PHRASE = {"charge:Power": "Power Charges", "charge:Frenzy": "Frenzy Charges", "charge:Endurance": "Endurance Charges", "minion": "minions",
          "rage": "Rage", "corpse": "Corpses", "remnant": "Remnants", "infusion": "Infusions", "glory": "Glory", "block": "Blocks",
          "crit": "Critical Hits", "onslaught": "Onslaught", "mana_spent": "Mana spent"}


def really_makes(tok: str, line: str) -> bool:
    """Does the sentence say the state is made on the enemy (or the resource gained)? False for "shockwave", for a
    state on you ("Shocks you", "chance to be Poisoned"), for a spender ("expending an Endurance Charge"), a
    maximum, an "instead of" swap, or a magnitude with no chance to apply. A line that does not name the state at
    all (a damage-type rule, a PoB tag) is left to the graph."""
    pat = mechgraph.STATE_COMPILED.get(tok)
    if pat is None or not line:
        return True
    seen = False
    for sent in _sentences(line):
        for m in pat.finditer(sent):
            seen = True
            word = sent[m.start(): m.end() + 12].split(" ")[0]
            near = sent[max(0, m.start() - 45): m.end() + 25]
            if (tok in WORD_NOT and WORD_NOT[tok].search(word)) or NOT_MADE_RX.search(near) or MAGNITUDE_RX.search(sent):
                continue
            if ON_YOU_RX.search(sent) and not re.search(r"\benem", sent, re.I):
                continue
            return True
    return not seen


def _swaps(lines) -> list[tuple]:
    """"Lightning Damage from Hits Contributes to Freeze Buildup instead of Shock Chance" -> [("Lightning", {to}, {lost})]."""
    out = []
    for line in lines or []:
        for m in SWAP_RX.finditer(line):
            toks = lambda text: {t for w, t in AILMENT_WORDS if w in text.lower()}
            out.append((m.group(1).capitalize(), toks(m.group(2)), toks(m.group(3)), line))
    return out


def _say(tok: str) -> str:
    return PHRASE.get(tok) or (tok.split(":", 1)[-1] if ":" in tok else tok)


def _quote(line: str, tok: str | None = None, n: int = 150) -> str:
    """The sentence of `line` that names the state, clipped."""
    pat = mechgraph.STATE_COMPILED.get(tok) if tok else None
    if pat is not None:
        for sent in _sentences(line):
            if pat.search(sent):
                return _clip(sent, n)
    return _clip(line, n)


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
    def _text(self, e, link) -> str:
        """The whole sentence behind a link: the graph keeps a clipped copy of a skill's description."""
        sk = self.cat.skills.get((e.meta or {}).get("skill") or e.key.split(":", 1)[-1]) if e.kind in ("skill", "util") else None
        if sk and (link.line or "").startswith(e.name + ": "):
            return sk.get("description") or link.line
        return link.line or ""

    def _sides(self, chain: dict, path: list[str] | None = None) -> dict:
        main = self.cat.skills.get(chain["main_skill"]) or {"id": chain["main_skill"], "name": chain.get("main_name"),
                                                             "types": [], "damage_types": []}
        ment = self.g.entities.get("skill:" + chain["main_skill"])
        eats = set(ment.consumes if ment else {})
        made = {t: l for t, l in (ment.produces if ment else {}).items() if l.weight >= 0.6 and really_makes(t, self._text(ment, l))}
        main_made = set(made)
        wants: dict[str, tuple] = {}             # token -> (payoff 0..1, part name, line, in chain tokens)
        for t, l in (ment.consumes if ment else {}).items():
            if l.payoff > 0:
                wants[t] = (l.payoff, main.get("name"), l.line, t in chain["tokens"])
        for key in (chain["path"] if path is None else path):
            e = self.g.entities.get(key)
            if e is None or key == "skill:" + chain["main_skill"]:
                continue
            for t, l in e.produces.items():
                if e.kind in ("unique", "keystone", "asc", "util") and l.weight >= 0.6 and STATE_TOKEN.search(t) \
                        and really_makes(t, self._text(e, l)):
                    made.setdefault(t, l)
            if e.kind not in ("unique", "keystone", "asc"):
                continue
            for t, l in e.consumes.items():
                if l.payoff <= 0 or DRAWBACK_RX.search(l.line or ""):
                    continue                 # "Take 100 Fire Damage when you Ignite an Enemy" is a price, not a payoff
                c = self.g.compat(l, main) if main.get("types") else 0.6
                v = l.payoff * (1.0 if c >= 0.6 else c)
                if c > 0 and (t not in wants or v > wants[t][0]):
                    wants[t] = (v, e.name, l.line, t in chain["tokens"])
        # a unique that moves an element's ailment ("... Contributes to Freeze Buildup instead of Shock Chance")
        for u in chain.get("uniques") or []:
            for dmg, to, lost, line in _swaps((self.cat.uniques.get(u) or {}).get("lines")):
                if dmg in (main.get("damage_types") or []):
                    for t in lost:
                        if t in made and not mechgraph.STATE_COMPILED[t].search(made[t].line or ""):
                            del made[t]
                            main_made.discard(t)
                    for t in to:
                        made.setdefault(t, mechgraph.Link(t, 0.6, f"{u}: {line}"))
                        main_made.add(t)
        desc = main.get("description") or ""
        inf = INFUSION_RX.search(desc)
        held = {(self.cat.uniques.get(u) or {}).get("weapon_class") for u in chain.get("uniques") or []} - {None}
        shield = any((self.cat.uniques.get(u) or {}).get("base_type") == "Shield" for u in chain.get("uniques") or [])
        known = set(main.get("weapon_types") or [])
        if not known:                             # a skill granted by a weapon unique is used with that weapon
            known = {(self.cat.uniques.get(u) or {}).get("weapon_class") for u in main.get("granted_by") or []} - {None}
        return {"weapons": known, "held": held, "shield": shield, "main": main, "tags": self.g.skill_tags(main) if main.get("types") is not None else set(),
                "made": made, "main_made": main_made, "eats": eats, "wants": wants,
                "cull": "only target enemies in culling range" in desc.lower(),
                "infusion": inf.group(1).lower() if inf else None,
                "elements": {d for d in main.get("damage_types") or [] if d in NATURAL}}

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
        minion_main = "Minion" in tags
        if gem["weapon_types"]:
            want = set(gem["weapon_types"])
            known, held = side.get("weapons") or set(), side.get("held") or set()
            if not (known - {"None"}) and not held:
                return "needs a weapon, and nothing in the chain says which one is held"
            if (known and not known & want) or held - want:
                return "needs a weapon type the chain does not use"
        if gem["req"] == ["Attack"] and "Herald" in gem["types"] and "Herald" in types:
            return "does not work with a Herald's own hits"
        if gem.get("item") == "Shield":
            two = (side.get("weapons") or set()) and (side.get("weapons") or set()) <= catmod.TWO_HANDED | {"Talisman"}
            if two or not (side.get("shield") or "RequiresShield" in types):
                return "needs a Shield"
        if gem.get("dmg") and not set(gem["dmg"]) & set(main.get("damage_types") or []):
            return f"needs {' or '.join(gem['dmg'])} damage, and {main.get('name')} deals none"
        if gem.get("own") and minion_main:
            return "waits for your own hits and kills, not your minions'"
        if gem.get("main_stat") and gem["main_stat"] not in (main.get("stat_ids") or []):
            return "needs Reviving minions"
        if gem["skill"] == main.get("id"):
            return "is the main skill"
        return None

    def score(self, gem: dict, chain: dict, side: dict | None = None) -> dict | None:
        """The gem's best use for the chain: {"role", "token", "score", "why"} (+ "clear_only"), or None when it
        does nothing. The score is the worth of the gem to this build, 0..1: how often the link happens, how
        much it pays, and whether it reaches the main skill against a boss."""
        side = side or self._sides(chain)
        if self.usable(gem, side):
            return None
        main, tags, made, wants = side["main"], side["tags"], side["made"], side["wants"]
        mname = main.get("name") or chain.get("main_name")
        minion_main = "Minion" in tags
        mtypes = set(main.get("types") or [])
        best = None
        # an aura on enemies in your Presence reaches what a melee skill hits; a ranged skill's targets are rarely that near
        near = 1.0 if gem["presence"] is None or "Melee" in tags else gem["presence"]
        reach = "" if near == 1.0 else f" Only for enemies within {PRESENCE_METRES} m of you, and {mname} is not a melee skill."
        down = f" But {gem['downside']}." if gem["downside"] else ""

        def offer(role, token, score, why, **extra):
            nonlocal best
            if score > 0 and (best is None or score > best["score"]):
                best = {"role": role, "token": token, "score": round(min(score, 1.0), 3), "why": why + down, **extra}

        state_needs = [t for t in gem["needs"] if STATE_TOKEN.search(t) and t != "crit"]
        if gem["consumed"]:
            met = [t for t in state_needs if t in side["eats"]]
        else:
            met = [t for t in state_needs if t in made or (t == "minion" and minion_main)]
        ready = not state_needs or bool(met)
        # 1a. feeds: the gem gives a state a payoff part (or the main skill) consumes
        for tok, gv in gem["gives"].items():
            if tok not in wants or not STATE_TOKEN.search(tok) or tok in mechgraph.WEAK_TOKENS or not ready:
                continue                 # (not ready: it waits for a state nothing in the chain makes)
            pay, part, line, in_chain = wants[tok]
            if gem["kind"] == "minion" and tok not in ("minion", "corpse"):
                continue
            via = met[0] if state_needs else None
            if tok == "infusion":        # an Infusion has the element of the ailment that made it
                need = INFUSION_FROM.get(side["infusion"] or "")
                if need is None or need not in met:
                    continue
                via = need
            v = gv["w"] * pay * (1.0 if in_chain else 0.75) * near
            if gem["kind"] == "minion":
                v *= 0.2                 # one more minion beside the main ones
            if tok in made and made[tok].weight >= 0.9:
                v *= 0.4                 # the chain already keeps this state up
            cond = f" when {mname} applies {_say(via)}" if via else ""
            rate = " It happens rarely, so it is a small help." if gv["w"] <= 0.25 else ""
            offer("feeds", tok, 0.25 + 0.7 * v,
                  f"{gem['name']} gives {_say(tok)}{cond} (\"{_quote(gv['line'], tok)}\"), which {part} pays off on "
                  f"(\"{_quote(line, tok)}\").{rate}{reach}")
        # 1a'. boosts: makes the chain's own state easier to apply
        for tok in gem["boosts"]:
            if tok in wants and tok in side["main_made"] and wants[tok][3] and near > 0:
                pay, part, line, _ = wants[tok]
                offer("feeds", tok, 0.25 + 0.7 * 0.5 * pay * near,
                      f"{gem['name']} makes {_say(tok)} easier for {mname} to apply to enemies within {PRESENCE_METRES} m "
                      f"(\"{_clip(gem['description'])}\"), and {part} pays off on it (\"{_quote(line, tok)}\").")
        # 1b. pays_off: the main skill makes a state the gem turns into something
        # (not a state the main skill itself consumes, nor charges a payoff part already spends: the gem would compete)
        cash = [t for t in met if not (t.startswith("charge:") and t in wants) and (gem["consumed"] or t not in side["eats"])]
        if gem["payoff"] > 0 and cash and not (minion_main and gem["kind"] != "minion"):
            tok = max(cash, key=lambda t: (t in chain["tokens"], made[t].weight if t in made else 1.0, t))
            w = made[tok].weight if tok in made else 1.0
            if "Attack" in gem["types"] and "Attack" not in tags:
                w = 0.0
            if gem["spends"] and gem["spends"] in mtypes:
                w *= 0.6                 # the main skill spends the resource the gem multiplies
            src = made[tok].line.split(":", 1)[0] if tok in made and ":" in (made[tok].line or "") else mname
            if tok in side["main_made"] or len(src) > 40:
                src = mname
            can = "consumes" if gem["consumed"] else "gives" if not tok.startswith("enemy:") else "applies" if w >= 0.9 else "can apply"
            say = (gem["say"] or f"{gem['name']} turns that into damage").format(main=mname)
            quote = f" (\"{_quote(gem['needs'][tok], tok)}\")" if not gem["say"] else ""
            if w > 0 and gem["kills"]:
                offer("pays_off", tok, 0.22 + 0.25 * w * gem["payoff"] + (0.03 if tok in chain["tokens"] else 0.0),
                      f"{src} {can} {_say(tok)}: {say}. Works while clearing packs; against a lone boss it does nothing, "
                      f"and the damage is the herald's own, not {mname}'s.", clear_only=True)
            elif w > 0:
                offer("pays_off", tok, 0.35 + 0.5 * w * gem["payoff"] + (0.05 if tok in chain["tokens"] else 0.0),
                      f"{src} {can} {_say(tok)}: {say}{quote}.")
        # 2. scales: raises the main skill's own damage, no state needed
        sc = gem["scales"]
        hit = bool({"Damage", "Attack"} & mtypes) and "DegenOnlySpellDamage" not in tags and not side["cull"]
        kept = any(t in made and made[t].weight >= 0.9 for t in gem["gives"])       # e.g. Withering Ground beside Withering Presence
        if sc and set(sc["tags"]) & tags and not minion_main and ready and (hit or not gem["hit"]) and not kept:
            kind = sorted(set(sc["tags"]) & tags)[0]
            strength, say = sc["strength"], sc["say"] or "raises {kind} damage, which is what {main} deals"
            if gem["elements"] and len(side["elements"]) < gem["elements"]:
                strength, say = gem["few"]
            mana = [t for t in ("mana_spent", "stat:Mana") if t in chain["tokens"]]
            if gem["mana"] and mana:     # the chain builds or spends Mana: this is where the gem belongs
                part = wants[mana[0]][1] if mana[0] in wants else "the chain"
                offer("feeds" if mana[0] in wants else "scales", mana[0] if mana[0] in wants else None, 0.25 + 0.4 * gem["mana"],
                      f"{gem['name']} raises {mname}'s Mana cost and adds Lightning damage for every 100 maximum Mana; "
                      f"{part} is built on Mana, so both sides pay.")
            else:
                offer("scales", None, 0.25 + 0.4 * strength * near,
                      f"{gem['name']} {say.format(kind=kind, main=mname)}. Generic: true of any such skill, not a link to this chain.{reach}")
        # 3. utility: generic damage, defence or recovery, matched to the main skill's attributes
        share = len(set(gem["attrs"]) & set(_attrs(main))) / max(1, len(gem["attrs"]))
        gen = gem["generic"]
        if best is None and gen and set(gen["tags"]) & tags and not minion_main and (hit or not gem["hit"]):
            if gen["say"]:
                why = gen["say"].format(gem=gem["name"], main=mname) + "."
            else:
                why = f"{gem['name']} is generic damage, not tied to this chain: \"{_clip(gem['description'])}\"."
            extra = {"clear_only": True} if gem["kills"] else {}
            offer("utility", None, gen["base"] + 0.06 * share, why + (" Pack clear only." if gem["kills"] else ""), **extra)
        plain = gem["payoff"] == 0 and not sc and not gen and gem["kind"] in ("buff", "aura", "banner") and gem["item"] is None
        if best is None and plain and (not gem["gives"] or gem["recovery"]) and not gem["boosts"]:
            if not state_needs or met:
                v = 0.14 + 0.1 * share + (0.08 if met else 0.0) - (0.04 if gem["spirit"] > 30 else 0.0)
                link = (f" {mname} " + ("applies" if met[0].startswith("enemy:") else "has") + f" the {_say(met[0])} it waits for.") if met else ""
                offer("utility", None, v, f"{gem['name']} is defence or recovery, not damage: \"{_clip(gem['description'])}\".{link}")
        return best

    def candidates(self, chain: dict, path: list[str] | None = None, level: int | None = None) -> list[dict]:
        """Every gem that does something for the chain, best first, in the graph document's pick shape
        (plus "tier" and "job", which pick() uses and strips). `level` prices each gem at the gem level a
        character of that level can use."""
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
            c = {"name": gem["name"], "skill": gem["skill"], "spirit": cost_at(gem["spirit"], gem["spirit_by_level"], level), "role": r["role"]}
            if r["token"]:
                c["token"] = r["token"]
            if r.get("clear_only"):
                c["clear_only"] = True
            c.update(why=r["why"], level=gem["level"], score=r["score"], tier=ROLE_TIER[r["role"]], job=job)
            out.append(c)
        out.sort(key=lambda c: (-c["score"], c["spirit"], c["name"]))
        return out

    def own(self, chain: dict, path: list[str] | None = None, level: int | None = None) -> float:
        """Spirit the chain's own skills reserve (the main skill and the helper skills on its path), at the gem
        level a character of `level` can use."""
        if path is None and level is None and "tempo" in chain:
            return float(chain["tempo"].get("spirit_total", 0.0))
        by = {g["skill"]: g["spirit_by_level"] for g in self.gems}
        total, seen = 0.0, set()
        for key in ["skill:" + chain["main_skill"]] + list(chain["path"] if path is None else path):
            e = self.g.entities.get(key)
            sid = e.meta.get("skill") if e is not None and e.kind in ("skill", "util") else None
            if sid and sid not in seen:
                seen.add(sid)
                total += cost_at(float((self.tempo.get(sid) or {}).get("spirit", 0.0)), by.get(sid), level)
        return total

    def suggest(self, chain: dict, limit: float, level: int | None = None, path: list[str] | None = None,
                gear: float = 0.0) -> dict:
        """chain.spirit of the graph document. `level` (None = no level limit) blocks gems that unlock later;
        `gear` is the part of `limit` that comes from equipment (an ascendancy can void it)."""
        ns = notes(chain, self.cat)
        for n in ns:
            if n["taken"]:
                limit = 0.0 if n["rule"] == "none" else max(0.0, limit - gear)
        own = self.own(chain, path, level)
        out = pick(self.candidates(chain, path, level), limit - own, level)
        out = {"budget": _n(limit), "own": _n(own), "free": _n(limit - own), **out}
        texts = [n["text"] + ("" if n["taken"] else " (only if that notable is taken)") for n in ns]
        at = (levelmod.load().get("skills") or {}).get(chain.get("main_skill"))
        if level is not None and at and at > level:
            texts.append(f"{chain.get('main_name')} itself unlocks at level {at}, after this level {level} budget")
        if texts:
            out["notes"] = texts
        return out


def _n(x: float):
    return int(x) if float(x).is_integer() else round(float(x), 1)


def _short(tok: str) -> str:
    return tok.split(":", 1)[-1] if ":" in tok else tok


def _clip(s: str, n: int = 110) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------------------------------------- picking
def _best_set(cands: list[dict], free: float) -> list[dict]:
    """The set worth most for the Spirit there is. Every gem after the first must add more than GEM_COST (a
    socket, supports and a button each); no job is done twice; at most one gem that only works while clearing
    packs and one utility gem; ties go to the set that reserves less."""
    cands = [c for c in cands if c["spirit"] <= free][:14]
    best, bv = [], (0.0, 0.0)
    for n in range(1, min(len(cands), MAX_PICKS) + 1):
        for combo in itertools.combinations(cands, n):
            if sum(c["spirit"] for c in combo) > free or len({c["job"] for c in combo}) < n:
                continue
            if sum(1 for c in combo if c.get("clear_only")) > 1 or sum(1 for c in combo if c["role"] == "utility") > 1 \
                    or sum(1 for c in combo if c["role"] == "scales") > 2:
                continue
            v = (round(sum(c["score"] for c in combo) - GEM_COST * (n - 1), 6), -sum(c["spirit"] for c in combo))
            if v > bv:
                best, bv = list(combo), v
    return best


def pick(cands: list[dict], free: float, level: int | None = None) -> dict:
    """-> {"picks", "more", "used"}. One choice over all roles, by what each gem is worth to the build (its
    score): a strong generic gem can beat a weak state link. A gem under PICK_MIN (UTILITY_PICK_MIN for utility)
    is true but not worth its Spirit and is only listed; a gem that unlocks above `level` is never picked."""
    late = [c for c in cands if level is not None and c["level"] is not None and c["level"] > level]
    ok = [c for c in cands if c not in late and c["score"] >= (UTILITY_PICK_MIN if c["role"] == "utility" else PICK_MIN)]
    got = _best_set(sorted(ok, key=lambda c: (-c["score"], c["spirit"], c["name"])), float(free))
    picks = sorted(got, key=lambda c: (-c["score"], c["name"]))
    jobs = {c["job"] for c in picks}
    left = float(free) - sum(c["spirit"] for c in picks)
    more = []
    for c in sorted((c for c in cands if c not in picks), key=lambda c: (-c["score"], c["name"])):
        why = "level" if c in late else "weaker" if c not in ok or c["job"] in jobs else "budget" if c["spirit"] > left else "weaker"
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
