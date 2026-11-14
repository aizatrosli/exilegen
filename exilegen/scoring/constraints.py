"""Constraint DSL (docs/build-search-design.md section 2), evaluated on the engine's skill list.

Constraints are hard filters. Each is a string or a dict:

    "one_button"                      <= 1 manual damaging skill (corpus / POC definition; utility, curses,
                                      movement, persistent, minion and triggered skills are free)
    "one_button:strict"               additionally <= 2 manual skills in total (the damaging one + a movement skill)
    "no_cast" / "no_cast:lenient"     no manual spell (triggered spells allowed)
    "no_cast:strict"                  no spell at all, manual or triggered
    "no_minions"                      no minion skill and no minion DPS
    "max_manual_skills:N"             <= N manual skills (damaging or not)
    "require_gem:<name|id>"  "forbid_gem:<name|id>"
    "require_unique:<name>"  "forbid_unique:<name>"
    "require_keystone:<name>" "forbid_keystone:<name>"
    {"type": "max_manual_skills", "n": 3}   (dict form, same names)

Counting rules follow tools/corpus-eval/src/extract.lua: weapon default attacks never count; an item-granted
manual damaging skill counts only when the player supports it or it is the main skill.
"""
from __future__ import annotations

from dataclasses import dataclass

from .adapter import BuildView, Gem, Group


@dataclass
class Constraint:
    type: str
    arg: str | None = None
    n: int | None = None

    @classmethod
    def parse(cls, c) -> "Constraint":
        if isinstance(c, Constraint):
            return c
        if isinstance(c, dict):
            return cls(c["type"], c.get("arg") or c.get("mode") or c.get("name"), c.get("n"))
        t, _, arg = str(c).partition(":")
        t = t.strip()
        if t == "max_manual_skills":
            return cls(t, None, int(arg))
        return cls(t, arg.strip() or None)

    def __str__(self):
        return self.type + (f":{self.arg}" if self.arg else "") + (f":{self.n}" if self.n is not None else "")


def _counted(bv: BuildView, g: Group, gem: Gem) -> bool:
    if gem.weapon_default:
        return False
    if gem.item_granted or g.granted_by:
        return bool(g.supports) or g.main
    return True


def manual_skills(bv: BuildView, damaging_only: bool = False) -> list[str]:
    out = []
    for g, gem in bv.skill_gems():
        if gem.activation is None:
            bv.missing.add("skill.activation")
            continue
        if gem.activation == "manual" and _counted(bv, g, gem) and (gem.damaging or not damaging_only):
            out.append(gem.name)
    return out


def _has_named(items: list[str], arg: str) -> bool:
    a = arg.lower()
    return any(a == (x or "").lower() for x in items)


def evaluate(constraints, bv: BuildView) -> tuple[list[dict], list[str]]:
    """-> (violations, flags). A violation is {constraint, detail}."""
    viol, flags = [], []
    if constraints and not bv.groups():
        flags.append("unverified:constraints_no_skill_data")
        return viol, flags
    for raw in constraints or []:
        c = Constraint.parse(raw)
        detail = None
        if c.type == "one_button":
            md = manual_skills(bv, True)
            ma = manual_skills(bv, False)
            if len(md) > 1:
                detail = f"{len(md)} manual damaging skills: {md}"
            elif c.arg == "strict" and len(ma) > 2:
                detail = f"{len(ma)} manual skills (max 2 with strict): {ma}"
        elif c.type == "no_cast":
            strict = c.arg == "strict"
            bad = [gem.name for g, gem in bv.skill_gems() if gem.spell and gem.damaging and not gem.weapon_default
                   and (gem.activation == "manual" or (strict and gem.activation == "triggered"))
                   and _counted(bv, g, gem)]
            if bad:
                detail = ("spells: " if strict else "manual spells: ") + str(bad)
        elif c.type == "no_minions":
            bad = [gem.name for _, gem in bv.skill_gems() if "minion" in gem.proxy_kind or "CreatesMinion" in gem.types]
            if bv.num("dps.minion", None, record=False):
                bad.append("minion DPS")
            if bad:
                detail = f"minions: {bad}"
        elif c.type == "max_manual_skills":
            ma = manual_skills(bv, False)
            if len(ma) > (c.n or 0):
                detail = f"{len(ma)} manual skills > {c.n}: {ma}"
        elif c.type in ("require_gem", "forbid_gem"):
            present = any(c.arg in gem.key() or (c.arg or "").lower() in gem.key()
                          for g in bv.groups() if g.enabled for gem in g.gems if gem.enabled)
            if c.type == "require_gem" and not present:
                detail = f"missing gem {c.arg}"
            if c.type == "forbid_gem" and present:
                detail = f"forbidden gem {c.arg}"
        elif c.type in ("require_unique", "forbid_unique", "require_keystone", "forbid_keystone"):
            items = bv.names("uniques" if c.type.endswith("unique") else "keystones")
            present = _has_named(items, c.arg or "")
            if c.type.startswith("require") and not present:
                detail = f"missing {c.type.split('_')[1]} {c.arg}"
            if c.type.startswith("forbid") and present:
                detail = f"forbidden {c.type.split('_')[1]} {c.arg}"
        else:
            flags.append(f"unknown_constraint:{c}")
            continue
        if detail:
            viol.append({"constraint": str(c), "detail": detail})
    if "skill.activation" in bv.missing:
        flags.append("unverified:skill_activation_missing")
    return viol, flags
