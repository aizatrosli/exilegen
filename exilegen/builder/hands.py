"""Which weapons a chain needs in the hands, and whether one weapon set can hold them.

A chain can ask for a main-hand weapon in three ways: a weapon unique among its parts, a skill that only a
weapon grants (a unique's own skill, or the implicit skill of a weapon base: Spellslinger on the Dueling Wand,
Freezing Shards on the Gelid Staff), and a meta gem that comes from a weapon base. The graph used to look at the
uniques only, so "Spellslinger + Liminal Coil" (two wands) passed as one build.

What one weapon set can hold (PoB PoE2 ItemsTab rules): one main-hand weapon; beside a one-handed weapon an
off-hand shield, focus or sceptre, or a second one-handed martial weapon (never a wand, never a spear); nothing
beside a two-handed weapon except a quiver with a bow. Two weapons that do not fit together need the second
weapon set (weapon swap): each set's own stats apply only while that set is in use, and exilegen builds one set.
"""
from __future__ import annotations

import json
import os

from . import catalog as catmod

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "leveling.json")
_bases: dict | None = None
MARTIAL_1H = {"Claw", "Dagger", "Flail", "One Hand Axe", "One Hand Mace", "One Hand Sword"}
BASE_CLASS = (("Quarterstaff", "Quarterstaff"), ("Staff", "Staff"), ("Wand", "Wand"), ("Sceptre", "Sceptre"),
              ("Spear", "Spear"), ("Crossbow", "Crossbow"), ("Bow", "Bow"), ("Talisman", "Talisman"))
TWO_HANDED = catmod.TWO_HANDED | {"Talisman"}


def base_granted() -> dict:
    global _bases
    if _bases is None:
        try:
            with open(_PATH, encoding="utf-8") as f:
                _bases = json.load(f).get("base_granted") or {}
        except OSError:
            _bases = {}
    return _bases


def _base_class(base: str) -> str | None:
    return next((c for word, c in BASE_CLASS if base.endswith(word)), None)


def needs(chain: dict, cat: catmod.Catalog) -> list[dict]:
    """[{"for": what asks, "options": [{"item", "class"}]}]: every part of the chain that fixes a weapon."""
    out = []
    for u in chain.get("uniques") or []:
        d = cat.uniques.get(u) or {}
        if d.get("slot") == "Weapon 1":
            out.append({"for": u, "options": [{"item": u, "class": d.get("weapon_class")}]})
    rx = chain.get("reaction") or {}
    held = set(rx.get("spells") or [])
    sids = [chain["main_skill"]] + [k.split(":", 1)[1] for k in chain.get("path") or [] if k.startswith(("util:", "skill:"))]
    for sid in dict.fromkeys(sids):
        s = cat.skills.get(sid) or {}
        if s.get("tier") != 0 or s.get("asc_granted") or sid in held or sid == rx.get("meta"):
            continue                      # a gem, or a notable's skill: no weapon needed for it
        givers = [u for u in s.get("granted_by") or [] if u in cat.uniques]
        if any(cat.uniques[u].get("slot") != "Weapon 1" for u in givers):
            continue                      # armour or jewellery can grant it too
        opts = [{"item": u, "class": cat.uniques[u].get("weapon_class")} for u in givers]
        b = base_granted().get(s.get("name"))
        if b:
            opts.append({"item": b["base"], "class": _base_class(b["base"]), "base": True})
        if opts:
            out.append({"for": s["name"], "options": opts})
    src = rx.get("source") or {}
    if src.get("kind") == "base":
        out.append({"for": rx["meta_name"], "options": [{"item": src["name"], "class": _base_class(src["name"]), "base": True}]})
    return out


def _together(a: dict, b: dict) -> bool:
    """Can two different weapons be held in one weapon set?"""
    ca, cb = a.get("class"), b.get("class")
    if ca in TWO_HANDED or cb in TWO_HANDED or ca is None or cb is None:
        return False
    if "Sceptre" in (ca, cb):
        return not (ca == cb)             # a sceptre sits in the off-hand beside a wand or a one-hander
    return ca in MARTIAL_1H and cb in MARTIAL_1H


def check(chain: dict, cat: catmod.Catalog) -> dict | None:
    """None when one weapon set holds everything the chain needs, else {"weapons": [...], "why": sentence}."""
    ns = needs(chain, cat)
    if len(ns) < 2:
        return None

    def fits(picked: list[dict], rest: list[dict]) -> bool:
        if not rest:
            return True
        for o in rest[0]["options"]:
            if any(o["item"] == p["item"] for p in picked):
                if fits(picked, rest[1:]):
                    return True
            elif len(picked) < 2 and all(_together(o, p) for p in picked):
                if fits(picked + [o], rest[1:]):
                    return True
        return False
    if fits([], ns):
        return None
    parts = [f"{n['options'][0]['item']}" + (f" (or {len(n['options']) - 1} more)" if len(n["options"]) > 1 else "") +
             (f" for {n['for']}" if n["for"] != n["options"][0]["item"] else "") for n in ns]
    return {"weapons": [{"for": n["for"], "items": [o["item"] for o in n["options"]]} for n in ns],
            "why": "Needs weapons that do not fit in one weapon set: " + ", ".join(parts) +
                   ". Only possible with weapon swap, where each weapon's stats apply only while its set is in use; "
                   "the builder makes one set."}
