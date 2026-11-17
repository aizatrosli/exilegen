"""The ONLY module that knows engine / corpus field names.

Everything else in exilegen.scoring asks a BuildView for canonical keys ("res.fire", "max_hit.physical",
"hits_per_sec", ...). FIELDS maps each canonical key to candidate dotted paths in the input document; the
first one present wins. When the engine schema settles, fix names here only.

Accepted inputs (all normalised into one merged dict):
  * engine stats (superset of tools/corpus-eval/src/extract.lua): {"stats": {...PoB output...}, "socketGroups": [...],
    "keystones": [...], "uniques": [...], "energy": [...], ...}
  * corpus record (evaluated.jsonl): use BuildView.from_corpus(record, "pinnacle_locked" | "as_exported")
  * one-button POC driver output section (raw_driver_output.jsonl): energy_from_poc(...)

Missing fields never raise: get() returns the default and records the key in `missing`, which the scorer
turns into an "unverified" flag.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# canonical key -> candidate paths (dotted) in the merged document
FIELDS: dict[str, list[str]] = {
    # damage
    "dps.combined": ["stats.CombinedDPS", "dps.player", "CombinedDPS"],
    "dps.full": ["stats.FullDPS"],
    "dps.total_hit": ["stats.TotalDPS"],
    "dps.minion": ["minionStats.CombinedDPS", "dps.minion", "stats.Minion.CombinedDPS"],
    "average_hit": ["stats.AverageHit"],
    "speed": ["stats.Speed", "offence.speed"],
    "hits_per_sec": ["offence.hitsPerSec", "stats.HitsPerSec", "hitsPerSec"],
    "crit_chance": ["offence.critChance", "stats.CritChance"],
    "shock_chance": ["offence.shockChance", "stats.ShockChance"],
    "ignite_chance": ["offence.igniteChance", "stats.IgniteChance"],
    "freeze_chance": ["offence.freezeChance", "stats.FreezeChance"],
    "chill_chance": ["offence.chillChance", "stats.ChillChance"],
    "bleed_chance": ["offence.bleedChance", "stats.BleedChance"],
    "poison_chance": ["offence.poisonChance", "stats.PoisonChance"],
    "skill_life_cost": ["sustain.lifeCost", "stats.LifeCost"],
    # defence
    "ehp": ["stats.TotalEHP"],
    "life": ["stats.Life"],
    "life_unreserved": ["stats.LifeUnreserved"],
    "es": ["stats.EnergyShield"],
    "mana": ["stats.Mana"],
    "mana_unreserved": ["stats.ManaUnreserved"],
    "res.fire": ["stats.FireResist"], "res.cold": ["stats.ColdResist"],
    "res.lightning": ["stats.LightningResist"], "res.chaos": ["stats.ChaosResist"],
    "res_max.fire": ["stats.FireResistMax"], "res_max.cold": ["stats.ColdResistMax"],
    "res_max.lightning": ["stats.LightningResistMax"], "res_max.chaos": ["stats.ChaosResistMax"],
    "max_hit.physical": ["maxHit.Physical", "stats.PhysicalMaximumHitTaken"], "max_hit.fire": ["maxHit.Fire", "stats.FireMaximumHitTaken"],
    "max_hit.cold": ["maxHit.Cold", "stats.ColdMaximumHitTaken"], "max_hit.lightning": ["maxHit.Lightning", "stats.LightningMaximumHitTaken"],
    "max_hit.chaos": ["maxHit.Chaos", "stats.ChaosMaximumHitTaken"],
    "block_chance": ["stats.EffectiveBlockChance", "stats.BlockChance"],
    "spell_block_chance": ["stats.EffectiveSpellBlockChance", "stats.SpellBlockChance"],
    "armour": ["stats.Armour"], "evasion": ["stats.Evasion"],
    "evade_chance": ["stats.EvadeChance", "stats.MeleeEvadeChance"],
    "deflect_chance": ["stats.EffectiveDeflectChance", "stats.DeflectChance"],
    "phys_dr": ["stats.PhysicalDamageReduction"],
    "stun_threshold": ["stats.StunThreshold"],
    "res_overcap.fire": ["stats.FireResistOverCap"], "res_overcap.cold": ["stats.ColdResistOverCap"],
    "res_overcap.lightning": ["stats.LightningResistOverCap"], "res_overcap.chaos": ["stats.ChaosResistOverCap"],
    "charges_max.power": ["stats.PowerChargesMax"], "charges_max.frenzy": ["stats.FrenzyChargesMax"],
    "charges_max.endurance": ["stats.EnduranceChargesMax"],
    "ailment_threshold": ["ailmentThreshold.ailment", "stats.AilmentThreshold"],
    "movement_speed": ["movementSpeed", "stats.MovementSpeedMod"],
    # resources
    "spirit": ["spirit.total", "stats.Spirit"],
    "spirit_unreserved": ["spirit.unreserved", "stats.SpiritUnreserved"],
    "mana_cost_per_sec": ["sustain.manaUsePerSec", "stats.ManaPerSecondCost"],
    "life_cost_per_sec": ["sustain.lifeUsePerSec", "stats.LifePerSecondCost"],
    "es_cost_per_sec": ["stats.ESPerSecondCost"],
    "mana_regen": ["sustain.manaRegen", "stats.ManaRegenRecovery", "stats.ManaRegen"],
    "life_regen": ["sustain.lifeRegen", "stats.LifeRegenRecovery", "stats.LifeRegen"],
    "mana_leech": ["sustain.manaLeech", "stats.ManaLeechGainRate", "stats.ManaLeechRate"],
    "life_leech": ["sustain.lifeLeech", "stats.LifeLeechGainRate", "stats.LifeLeechRate"],
    # attributes
    "str": ["stats.Str"], "dex": ["stats.Dex"], "int": ["stats.Int"],
    "req_str": ["requirements.Str.need", "stats.ReqStr"], "req_dex": ["requirements.Dex.need", "stats.ReqDex"],
    "req_int": ["requirements.Int.need", "stats.ReqInt"],
    # build facts
    "unsupported_lines": ["unsupported.count", "unsupportedModLines", "tags.unsupportedModLines"],
    "unsupported_sample": ["unsupported.sample", "unsupportedModSample", "tags.unsupportedModSample"],
    "main_skill_unsupported": ["mainSkillUnsupportedLines", "mainSkill.unsupportedLines"],
    "socket_groups": ["socketGroups", "tags.socketGroups", "groups", "skillsView.groups"],
    "keystones": ["keystones", "tags.keystones", "tree.keystones"],
    "uniques": ["uniques", "tags.uniques", "items.uniques"],
    "main_skill": ["mainSkill"],
    "main_group": ["main.group"],
    "minion_stats": ["minionStats"],
    "energy": ["energy", "energyGroups", "meta"],
    "marginals": ["marginals", "dropOne"],
    "profile": ["profile", "profileName"],
}


def _dig(doc: Any, path: str):
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return float(v)
    return None


@dataclass
class Gem:
    id: str | None
    name: str
    support: bool
    enabled: bool = True
    activation: str | None = None       # manual | triggered | persistent | proxy
    damaging: bool = False
    spell: bool = False
    types: list[str] = field(default_factory=list)
    proxy_kind: list[str] = field(default_factory=list)
    item_granted: bool = False
    weapon_default: bool = False

    def key(self) -> set[str]:
        k = {self.name.lower()} if self.name else set()
        if self.id:
            k.add(self.id)
        return k


@dataclass
class Group:
    index: int
    enabled: bool
    main: bool
    meta: bool
    granted_by: str | None
    gems: list[Gem]

    @property
    def actives(self):
        return [g for g in self.gems if not g.support]

    @property
    def supports(self):
        return [g for g in self.gems if g.support and g.enabled]


@dataclass
class EnergySpell:
    name: str
    id: str | None
    cast_time: float | None
    pob_rate: float | None          # PoB's assumed triggers/s for this spell
    pob_dps: float | None


@dataclass
class EnergyGroup:
    group: int | None
    meta_id: str | None
    meta_name: str | None
    max_energy: float | None        # energy units (10 per 0.1 s base cast time)
    gen_pct: float                  # energy_generated_+% (gem level/quality + supports + global mods)
    refund_pct: float               # trigger_skills_refund_half_energy_spent_chance_%
    per_event: dict[str, float]     # energy per event per monster power: crit/shock/ignite/freeze/...
    feeder: dict[str, float]        # hits_per_sec, crit, shock, ignite, freeze (fractions 0..1), *_events_per_sec
    spells: list[EnergySpell]
    pob_assumed_rate: float | None = None
    raw: dict = field(default_factory=dict)


def _first(d: dict, *names, default=None):
    for n in names:
        if isinstance(d, dict) and n in d and d[n] is not None:
            return d[n]
    return default


def _pct(v):
    """Percent (0..100) -> fraction."""
    v = _num(v)
    return None if v is None else v / 100.0


def _norm_feeder(f: dict | None) -> dict:
    """Feeder event facts. Plain names (crit, shock, ...) are fractions 0..1 (POC); names ending in
    'Chance' / '_chance' are percentages 0..100 (PoB output, engine)."""
    f = f or {}
    out = {}
    hps = _first(f, "hits_per_sec", "hitsPerSec", "hitRate", "HitsPerSec")
    if _num(hps) is not None:
        out["hits_per_sec"] = _num(hps)
    for k in ("crit", "shock", "ignite", "freeze", "chill", "electrocute"):
        if _num(f.get(k)) is not None:
            out[k] = _num(f[k])
        else:
            v = _first(f, f"{k}Chance", f"{k}_chance", k.capitalize() + "Chance")
            if _num(v) is not None:
                out[k] = _pct(v)
    for k in ("crit", "shock", "ignite", "freeze", "block"):
        v = _first(f, f"{k}_events_per_sec", f"{k}EventsPerSec", {"crit": "critsPerSec", "shock": "shocksPerSec",
                                                                    "ignite": "ignitesPerSec", "freeze": "freezesPerSec",
                                                                    "block": "blocksPerSec"}[k])
        if _num(v) is not None:
            out[f"{k}_events_per_sec"] = _num(v)
    # energy scaling facts: average crit hit, average fire hit, enemy ailment threshold (PoB EnemyAilmentThreshold)
    for k, names in {"crit_hit": ("crit_hit", "critHit"), "fire_hit": ("fire_hit", "fireHit"),
                     "ailment_threshold": ("ailment_threshold", "enemyAilmentThreshold", "EnemyAilmentThreshold")}.items():
        v = _first(f, *names)
        if _num(v) is not None:
            out[k] = _num(v)
    v = _first(f, "minion_deaths_per_sec", "minionDeathsPerSec")
    if _num(v) is not None:
        out["minion_deaths_per_sec"] = _num(v)
    return out


def normalise_energy_group(e: dict, default_feeder: dict | None = None) -> EnergyGroup:
    per = _first(e, "per_event", "perEvent", default=None) or {}
    per_event = {}
    for k, names in {"crit": ("crit", "perCrit", "per_crit"), "shock": ("shock", "perShock", "per_shock"),
                     "ignite": ("ignite", "perIgnite", "per_ignite"), "freeze": ("freeze", "perFreeze", "per_freeze"),
                     "block": ("block", "perBlock"), "minion_death": ("minion_death", "perMinionDeath")}.items():
        v = _first(per, *names)
        if v is None:
            v = _first(e, *names[1:])
        if _num(v) is not None:
            per_event[k] = float(v)
    # engine: `spells` = socketed gems with castTime, `triggered` = calculated skills with dps/pobRate
    socketed = _first(e, "spells", "socketed", default=[]) or []
    calc = _first(e, "triggered", default=None)
    ct_by = {}
    for x in socketed:
        for k in (x.get("id"), x.get("name")):
            if k is not None:
                ct_by[k] = _num(_first(x, "cast_time", "castTime", "baseCastTime"))
    src = calc if calc is not None else socketed
    spells = []
    for x in src:
        if x.get("spell") is False:
            continue
        spells.append(EnergySpell(
            name=str(_first(x, "name", default="?")), id=_first(x, "id", "skillId"),
            cast_time=_num(_first(x, "cast_time", "castTime", "baseCastTime")) or ct_by.get(x.get("id")) or ct_by.get(x.get("name")),
            pob_rate=_num(_first(x, "pob_rate", "pobRate", "pobAssumedRate", "assumedRate")),
            pob_dps=_num(_first(x, "pob_dps", "pobDps", "dps", "CombinedDPS"))))
    feeder_raw = _first(e, "feeder", "feederRates", "events", default=None)
    feeder = _norm_feeder(feeder_raw if isinstance(feeder_raw, dict) else default_feeder)
    return EnergyGroup(
        group=_first(e, "group", "gi", "groupIndex"),
        meta_id=_first(e, "meta_id", "metaSkillId", "metaId"),
        meta_name=_first(e, "meta_name", "metaName", "meta"),
        max_energy=_num(_first(e, "max_energy", "maxEnergy")),
        gen_pct=_num(_first(e, "gen_pct", "genBonus", "gen", "energyGeneratedPct")) or 0.0,
        refund_pct=_num(_first(e, "refund_pct", "refund", "refundChance")) or 0.0,
        per_event=per_event, feeder=feeder, spells=spells,
        pob_assumed_rate=_num(_first(e, "pob_assumed_rate", "pobAssumedRate")),
        raw=e)


class BuildView:
    """Read-only canonical view over one profile's engine output."""

    def __init__(self, doc: dict, skills: list | None = None, profile: str | None = None):
        self.doc = doc or {}
        self.skills_in = skills
        self.missing: set[str] = set()
        self.profile = profile or self.get("profile", None, record=False)

    # -------------------------------------------------------------- construction helpers
    @classmethod
    def from_corpus(cls, record: dict, section: str = "pinnacle_locked", profile: str | None = None):
        """A corpus record (evaluated.jsonl): tags + one evaluated section."""
        doc = dict(record.get("tags") or {})
        sec = record.get(section) or {}
        doc.update(sec)
        return cls(doc, profile=profile or section)

    # -------------------------------------------------------------- raw access
    def raw(self, key: str):
        for p in FIELDS.get(key, [key]):
            v = _dig(self.doc, p)
            if v is not None:
                return v
        return None

    def get(self, key: str, default=None, record: bool = True):
        v = self.raw(key)
        if v is None:
            if record:
                self.missing.add(key)
            return default
        n = _num(v)
        return n if n is not None else v

    def num(self, key: str, default=None, record: bool = True):
        v = self.get(key, None, record)
        n = _num(v)
        if n is None:
            if record:
                self.missing.add(key)
            return default
        return n

    def has(self, key: str) -> bool:
        return self.raw(key) is not None

    # -------------------------------------------------------------- structured facts
    def groups(self) -> list[Group]:
        if hasattr(self, "_groups"):
            return self._groups
        out = []
        src = None
        sk = self.skills_in
        if isinstance(sk, dict) and isinstance(sk.get("groups"), list):
            src = self._groups_from_view(sk["groups"])
        if src is None:
            src = self.raw("socket_groups")
            if isinstance(src, list) and src and isinstance(src[0], dict) and "skills" in src[0]:
                src = self._groups_from_view(src)
        if src is None:
            flat = sk if isinstance(sk, list) else self.raw("skills")
            if isinstance(flat, list) and flat:
                src = self._groups_from_skills(flat)
        if src is None:
            self.missing.add("socket_groups")
        for i, g in enumerate(src or []):
            gems = []
            for e in g.get("gems", []) or []:
                gems.append(Gem(
                    id=e.get("id") or e.get("skillId"), name=e.get("name") or "?",
                    support=bool(e.get("support")), enabled=e.get("enabled", True) is not False,
                    activation=e.get("activation"), damaging=bool(e.get("damaging")), spell=bool(e.get("spell")),
                    types=list(e.get("types") or []), proxy_kind=list(e.get("proxyKind") or e.get("proxy_kind") or []),
                    item_granted=bool(e.get("itemGranted") or e.get("item_granted")),
                    weapon_default=bool(e.get("weaponDefault") or e.get("weapon_default"))))
            out.append(Group(index=g.get("index", i + 1), enabled=g.get("enabled", True) is not False,
                             main=bool(g.get("main")), meta=bool(g.get("metaGroup") or g.get("meta")),
                             granted_by=g.get("grantedBy") or g.get("granted_by"), gems=gems))
        self._groups = out
        return out

    @staticmethod
    def _groups_from_view(groups: list) -> list[dict]:
        """engine skills() view: group.gems (gem slots, `skillId` = granted effect id) + group.skills (calculated
        skill facts with activation/damaging/...). Active gems take their facts from the matching skill."""
        out = []
        for g in groups:
            facts = {}
            for f in g.get("skills") or []:
                facts.setdefault(f.get("id"), f)
                if f.get("gemSlot") is not None:
                    facts.setdefault(("slot", f.get("gemSlot")), f)
            gems = []
            for gm in g.get("gems") or []:
                e = dict(gm)
                e["id"] = gm.get("skillId") or gm.get("id")
                if not gm.get("support"):
                    f = facts.get(("slot", gm.get("slot"))) or facts.get(e["id"]) or {}
                    for k in ("activation", "damaging", "spell", "proxyKind", "itemGranted", "weaponDefault"):
                        if k in f:
                            e[k] = f[k]
                    e["types"] = f.get("types") or gm.get("gemTypes") or []
                    if f.get("enabled") is False:
                        e["enabled"] = False
                gems.append(e)
            # skills granted without a gem slot (items) become extra actives
            slots = {gm.get("slot") for gm in g.get("gems") or []}
            for f in g.get("skills") or []:
                if f.get("gemSlot") is None or f.get("gemSlot") not in slots:
                    if not any(x.get("id") == f.get("id") for x in gems):
                        gems.append(dict(f, support=False))
            out.append({"index": g.get("index"), "enabled": g.get("enabled", True), "main": bool(g.get("main")),
                        "metaGroup": bool(g.get("metaGroup")), "grantedBy": g.get("grantedBy"), "gems": gems})
        return out

    @staticmethod
    def _groups_from_skills(skills: list) -> list[dict]:
        """engine skills(): flat list of skills with a group index -> extract.lua-like groups."""
        by: dict[Any, dict] = {}
        for s in skills:
            gi = s.get("group", s.get("gi", s.get("groupIndex")))
            g = by.setdefault(gi, {"index": gi, "enabled": s.get("groupEnabled", True), "main": s.get("main", False),
                                   "metaGroup": s.get("metaGroup", False), "grantedBy": s.get("grantedBy"), "gems": []})
            g["main"] = g["main"] or bool(s.get("main"))
            g["gems"].append(s)
            for sup in s.get("supports", []) or []:
                if isinstance(sup, str):
                    sup = {"name": sup}
                if not any((x.get("name") == sup.get("name")) for x in g["gems"] if x.get("support")):
                    g["gems"].append(dict(sup, support=True))
        return list(by.values())

    def skill_gems(self) -> list[tuple[Group, Gem]]:
        """Enabled active gems of enabled groups."""
        return [(g, gem) for g in self.groups() if g.enabled for gem in g.actives if gem.enabled]

    def names(self, key: str) -> list[str]:
        v = self.raw(key)
        if v is None:
            self.missing.add(key)
            return []
        return [x.get("name") if isinstance(x, dict) else str(x) for x in v]

    def main_group(self) -> Group | None:
        mg = self.raw("main_group")
        for g in self.groups():
            if (mg is not None and g.index == mg) or (mg is None and g.main):
                return g
        return None

    def main_skill(self) -> dict:
        ms = self.raw("main_skill") or {}
        return {"name": ms.get("name"), "id": ms.get("id"), "types": ms.get("types") or [],
                "minion": ms.get("minion")}

    def main_dps(self) -> float | None:
        """PoB's main-skill DPS (minion DPS if larger, like the support POC)."""
        d = self.num("dps.combined", None)
        m = self.num("dps.minion", None, record=False)
        if d is None and m is None:
            return None
        return max(d or 0.0, m or 0.0)

    def hits_per_sec(self) -> float | None:
        v = self.num("hits_per_sec", None, record=False)
        if v is not None:
            return v
        tot, avg = self.num("dps.total_hit", None, False), self.num("average_hit", None, False)
        if tot is not None and avg:
            return tot / avg
        self.missing.add("hits_per_sec")
        return None

    def energy_groups(self) -> list[EnergyGroup] | None:
        e = self.raw("energy")
        if e is None:
            return None
        default_feeder = None
        if isinstance(e, dict):
            default_feeder = e.get("feeder")
            e = e.get("groups") or e.get("energy") or []
        return [normalise_energy_group(x, default_feeder) for x in e]

    def marginal(self, group_index, gem: Gem, group_level: bool = False) -> float | None:
        """DPS of the group's skill with `gem` disabled (engine drop-one), if supplied.
        group_level=True prefers the group's own DPS (group_dps_without) for triggered/meta parts."""
        m = self.raw("marginals")
        if not m:
            return None
        for r in m:
            if r.get("group") not in (None, group_index):
                continue
            if r.get("gem_id") in gem.key() or (r.get("gem") or "").lower() in gem.key() or r.get("name", "").lower() in gem.key():
                if group_level and r.get("group_dps_without") is not None:
                    return _num(r.get("group_dps_without"))
                return _num(r.get("dps_without", r.get("dpsWithout")))
        return None


# ------------------------------------------------------------------ POC adapters
# per-event energy of the modelled meta gems (centienergy per monster power / 100) and refund chance of
# Energy Retention, from PoB-PoE2 0.23.1 gem data (Data/Skills/act_int.lua, sup_int.lua).
POC_META = {
    "Cast on Critical": ("MetaCastOnCritPlayer", {"crit": 1.0}),
    "Cast on Elemental Ailment": ("MetaCastOnElementalAilmentPlayer", {"shock": 1.0, "ignite": 1.0, "freeze": 10.0}),
}
POC_REFUND = {"Energy Retention": 35.0}


def energy_from_poc(section: dict, groups: list[dict] | None = None, refund_by_name: dict | None = None) -> dict:
    """One-button POC driver output section (r.baseline / r.final) -> engine-style energy document.

    `groups` is r.groups (gem names per group, the final state); used only to read refund supports.
    """
    refund_by_name = refund_by_name or POC_REFUND
    feeder = section.get("feeder") or {}
    gems_by_gi = {g["gi"]: g["gems"] for g in (groups or [])}
    by_gi: dict[int, dict] = {}
    for t in section.get("triggered", []):
        gi = t["gi"]
        meta_id, per = POC_META.get(t.get("meta"), (None, {}))
        e = by_gi.setdefault(gi, {
            "group": gi, "meta_name": t.get("meta"), "meta_id": meta_id if t.get("kind") != "unmodelled" else None,
            "max_energy": t.get("maxEnergy"), "gen_pct": t.get("gen", 0.0),
            "refund_pct": sum(refund_by_name.get(n, 0.0) for n in gems_by_gi.get(gi, [])),
            "per_event": per,
            "feeder": {"hits_per_sec": feeder.get("hitsPerSec"), "crit": feeder.get("crit"),
                       "shock": feeder.get("shock"), "ignite": feeder.get("ignite")},
            "spells": []})
        e["spells"].append({"name": t["name"], "pob_rate": t.get("pobRate"), "pob_dps": t.get("dps"),
                            "poc_factor": t.get("factor"), "poc_energy_rate": t.get("energyRate")})
    return {"energy": list(by_gi.values()),
            "stats": {"CombinedDPS": feeder.get("dps"), "Speed": feeder.get("speed"),
                      "TotalEHP": (section.get("def") or {}).get("TotalEHP")},
            "mainSkill": {"name": feeder.get("name")}}


def _activation(info: dict, in_meta: bool, is_meta: bool) -> str:
    t = set(info.get("types") or [])
    if is_meta:
        return "persistent"
    if in_meta or "Triggered" in t or "InbuiltTrigger" in t:
        return "triggered"
    if "Persistent" in t or "HasReservation" in t:
        return "persistent"
    if t & {"Minion", "CreatesMinion", "SummonsTotem", "Trapped", "RemoteMined"}:
        return "proxy"
    return "manual"


def doc_from_poc(r: dict, section: str = "final") -> dict:
    """One-button POC run (raw_driver_output.jsonl `r`, groups enriched with `gem_info` by the fixture
    builder) -> engine-style document: energy facts + socket groups + the stats the POC recorded."""
    sec = r.get(section) or {}
    groups = r.get("groups") if section == "final" else None
    doc = energy_from_poc(sec, r.get("groups"))
    feeder = sec.get("feeder") or {}
    d = sec.get("def") or {}
    doc["stats"].update({
        "CritChance": 100 * (feeder.get("crit") or 0), "ShockChance": 100 * (feeder.get("shock") or 0),
        "IgniteChance": 100 * (feeder.get("ignite") or 0), "Life": d.get("Life"), "EnergyShield": d.get("ES"),
        "SpiritUnreserved": d.get("SpiritUnreserved"),
        **{f"{el}Resist": d.get(el) for el in ("Fire", "Cold", "Lightning", "Chaos") if d.get(el) is not None}})
    doc["stats"] = {k: v for k, v in doc["stats"].items() if v is not None}
    if groups:
        sg = []
        for g in groups:
            infos = g.get("gem_info") or [{} for _ in g["gems"]]
            meta = bool(g.get("meta"))
            gems = []
            for name, info in zip(g["gems"], infos):
                t = info.get("types") or []
                is_meta = "Meta" in t and "Triggers" in t
                gems.append({"name": name, "id": info.get("id"), "support": bool(info.get("support")), "types": t,
                             "activation": None if info.get("support") else _activation(info, meta, is_meta),
                             "damaging": bool(set(t) & {"Damage", "Attack", "DamageOverTime"}), "spell": "Spell" in t,
                             "proxyKind": ["minion"] if set(t) & {"Minion", "CreatesMinion"} else []})
            sg.append({"index": g["gi"], "enabled": True, "metaGroup": meta, "gems": gems,
                       "main": bool(g["gems"]) and g["gems"][0] == feeder.get("name") and not meta})
        doc["socketGroups"] = sg
    return doc
