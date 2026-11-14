"""Gem catalogue for the optimiser (no engine call): names, ids, skill types, cast times, tiers.

Built from the PoB data under the engine's PoB root (Data/Gems.lua + Data/Skills/*.lua via
exilegen.scoring.pobdata). Used to
  * resolve a gem reference (gem id, granted-effect/skill id or name) to its facts;
  * list candidate triggerable spells, meta gems and energy supports;
  * patch an engine skills() view for a what-if gem change (base.patch_skills), so the Scorer's
    constraints and condition registry see the candidate's gems, not the committed ones.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field

from exilegen.scoring import pobdata

_LOCK = threading.Lock()
_CACHE: dict[str, "Catalog"] = {}

# skill types that make a spell unusable as a meta-gem payload in our search
_SPELL_EXCLUDE = {"Meta", "Persistent", "HasReservation", "Minion", "CreatesMinion", "InbuiltTrigger", "Channel",
                  "Triggered"}
MODELLED_METAS = ("MetaCastOnCritPlayer", "MetaCastOnElementalAilmentPlayer")
ENERGY_STATS = ("energy_generated_+%", "trigger_skills_refund_half_energy_spent_chance_%")


@dataclass
class GemInfo:
    gem_id: str                 # Metadata/Items/Gems/...
    skill_id: str | None        # granted effect id (what the registry matches)
    name: str
    support: bool = False
    tier: int | None = None
    gem_type: str | None = None
    tags: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)     # PoB SkillType names of the granted effect
    cast_time: float | None = None
    constant_stats: dict = field(default_factory=dict)
    hidden: bool = False

    @property
    def meta(self) -> bool:
        return not self.support and "Meta" in self.types and "Triggers" in self.types

    @property
    def damaging(self) -> bool:
        return bool({"Damage", "Attack", "DamageOverTime", "DegenOnlySpellDamage"} & set(self.types))

    @property
    def spell(self) -> bool:
        return "Spell" in self.types

    @property
    def energy_support(self) -> bool:
        return self.support and any((self.constant_stats.get(s) or 0) > 0 for s in ENERGY_STATS)


class Catalog:
    def __init__(self, pob_root: str):
        self.pob_root = pob_root
        skills = pobdata.read_skills(pob_root)
        self.by_gem: dict[str, GemInfo] = {}
        self.by_skill: dict[str, GemInfo] = {}
        self.by_name: dict[str, GemInfo] = {}
        text = open(os.path.join(pob_root, "Data", "Gems.lua"), encoding="utf-8", errors="replace").read()
        for m in re.finditer(r'\n\t\["([^"]+)"\] = \{(.*?)\n\t\},', text, re.S):
            key, b = m.group(1), m.group(2)
            ge = re.search(r'grantedEffectId = "([^"]+)"', b)
            nm = re.search(r'\tname = "((?:[^"\\]|\\.)*)"', b)
            tier = re.search(r'\tTier = (\d+)', b)
            gt = re.search(r'\tgemType = "([^"]*)"', b)
            tags = re.search(r'tags = \{(.*?)\}', b, re.S)
            sid = ge.group(1) if ge else None
            sd = skills.get(sid) if sid else None
            info = GemInfo(gem_id=key, skill_id=sid, name=(nm.group(1).replace("\\'", "'") if nm else (sd.name if sd else key)),
                           support=bool(sd.support) if sd else ("support = true" in b),
                           tier=int(tier.group(1)) if tier else None, gem_type=gt.group(1) if gt else None,
                           tags=re.findall(r'(\w+) = true', tags.group(1)) if tags else [],
                           types=list(sd.skill_types) if sd else [], cast_time=sd.cast_time if sd else None,
                           constant_stats=dict(sd.constant_stats) if sd else {}, hidden=bool(sd.hidden) if sd else False)
            self.by_gem[key] = info
            if sid and sid not in self.by_skill:
                self.by_skill[sid] = info
            self.by_name.setdefault(info.name.lower(), info)

    # ------------------------------------------------------------------ lookup
    def get(self, ref) -> GemInfo | None:
        if ref is None:
            return None
        if isinstance(ref, GemInfo):
            return ref
        r = str(ref)
        return self.by_gem.get(r) or self.by_skill.get(r) or self.by_name.get(r.lower())

    # ------------------------------------------------------------------ candidate lists
    def trigger_spells(self) -> list[GemInfo]:
        """Damaging spells a Meta+Triggers gem can socket (POC spellCandidates)."""
        out = []
        for g in self.by_skill.values():
            t = set(g.types)
            if g.support or g.hidden or not g.skill_id or g.skill_id.startswith("WeaponGranted"):
                continue
            if "Spell" in t and "Triggerable" in t and ({"Damage", "DamageOverTime"} & t) and not (t & _SPELL_EXCLUDE) \
                    and (g.cast_time or 0) > 0 and (g.tier or 0) > 0:
                out.append(g)
        return sorted(out, key=lambda g: g.name)

    def feeder_spells(self, max_cast_time: float = 1.0) -> list[GemInfo]:
        """Fast-hitting spells usable as a 1-button feeder (manual, damaging)."""
        return [g for g in self.trigger_spells() if (g.cast_time or 9) <= max_cast_time]

    def metas(self, modelled_only: bool = True) -> list[GemInfo]:
        out = [g for g in self.by_skill.values() if g.meta and not g.hidden]
        if modelled_only:
            out = [g for g in out if g.skill_id in MODELLED_METAS]
        return sorted(out, key=lambda g: g.name)

    def energy_supports(self) -> list[GemInfo]:
        return sorted((g for g in self.by_skill.values() if g.energy_support and not g.hidden), key=lambda g: g.name)


def catalog(pob_root: str | None = None) -> Catalog:
    root = pob_root or os.environ.get("EXG_POB_ROOT") or os.environ.get("POB_ROOT")
    if not root:
        raise FileNotFoundError("PoB root unknown (pass pob_root or set EXG_POB_ROOT)")
    with _LOCK:
        if root not in _CACHE:
            _CACHE[root] = Catalog(root)
        return _CACHE[root]
