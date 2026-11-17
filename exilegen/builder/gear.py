"""Gear for the synthesizer: weapon choice, requested uniques, template rares, optimise_gear.

Everything here works on the build open in the engine through raw pob-redux bridge calls
(Engine.raw): list_bases, list_affixes, craft_rare, item_db_list / item_db_equip, optimise_gear,
equip_item_raw. The engine's own snapshot/restore does not cover items, so callers re-score
(ctx.refresh) after any gear change.

    plan = GearPlanner(engine, level=90, tier="mid", defence=None, cls="Sorceress")
    plan.equip_unique("Choir of the Storm")              # -> slot
    plan.equip_weapons(["Staff"], uniques_taken)          # main hand (+ off-hand / quiver)
    plan.fill_templates(skip=slots_taken)                 # template rares in every empty slot
    plan.optimise(weights={"dps": 1, "life": 0.2, "ehp": 0.4}, keep=slots_taken)

Template rares and tiers live in builder/data/gear_templates.json.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

_DATA = os.path.join(os.path.dirname(__file__), "data", "gear_templates.json")
_TEMPLATES: dict | None = None

ARMOUR_SLOTS = {"Helmet": "Helmet", "Body Armour": "Body Armour", "Gloves": "Gloves", "Boots": "Boots"}
JEWELLERY_SLOTS = ("Amulet", "Ring 1", "Ring 2", "Belt")
GEAR_SLOTS = ("Weapon 1", "Weapon 2", "Helmet", "Body Armour", "Gloves", "Boots", "Amulet", "Ring 1", "Ring 2", "Belt")


def templates() -> dict:
    global _TEMPLATES
    if _TEMPLATES is None:
        with open(_DATA, encoding="utf-8") as f:
            _TEMPLATES = json.load(f)
    return _TEMPLATES


def tier_params(tier: str | None) -> dict:
    t = templates()["tiers"]
    return dict(t.get(tier or "mid") or t["mid"])


def defence_type(defence: str | None, cls: str | None) -> str:
    """Armour base family suffix ("Armour", "Evasion/Energy Shield", ...) for a defence hint / class."""
    T = templates()
    d = T["defence_types"].get((defence or "life").lower())
    if d:
        return d
    return T["class_defence"].get(cls or "", "Armour/Evasion")


def weapon_spec(name: str | None) -> dict | None:
    """Weapon hint or skill weapon requirement ("Bow", "Quarterstaff", "One Hand Mace, Two Hand Mace") ->
    {type, hands, offhand?} of the first known type."""
    if not name:
        return None
    T = templates()
    name = T["requirement_aliases"].get(name.strip(), name)
    for part in [p.strip() for p in re.split(r",| or ", name) if p.strip()]:
        part = T["requirement_aliases"].get(part, part)
        if part in T["weapon_types"]:
            return dict(T["weapon_types"][part], name=part)
        for k, v in T["weapon_types"].items():
            if k.lower() == part.lower() or (v["type"] or "").lower() == part.lower():
                return dict(v, name=k)
    return None


class GearPlanner:
    def __init__(self, engine, level: int = 90, tier: str = "mid", defence: str | None = None,
                 cls: str | None = None, notes: list | None = None):
        self.e = engine
        self.level = int(level)
        self.tier = tier or "mid"
        self.tp = tier_params(self.tier)
        self.defence = defence
        self.dtype = defence_type(defence, cls)
        self.es = self.dtype == "Energy Shield" or (defence or "").lower() in ("es", "ci", "mom")
        self.notes = notes if notes is not None else []
        self._bases: dict = {}
        self._affixes: dict = {}
        self.taken: dict[str, str] = {}          # slot -> what holds it ("unique:<name>", "template", ...)
        self.two_handed = False
        self.skill_types: set = set()            # main skill types, set by synth before templates are crafted

    # ------------------------------------------------------------------ bases
    def bases(self, type_name: str) -> list[dict]:
        if type_name not in self._bases:
            try:
                self._bases[type_name] = self.e.raw("list_bases", {"type": type_name, "limit": 500}).get("bases") or []
            except Exception:
                self._bases[type_name] = []
        return self._bases[type_name]

    def pick_base(self, type_name: str, how: str = "defence") -> dict | None:
        """Best base of a typed list (exact type, or a family) that the level can wear."""
        best, bv = None, None
        for b in self.bases(type_name):
            if ":" not in type_name and b.get("type") != type_name and type_name in ("Staff",):
                continue          # "Staff" family also holds the warstaves
            req = (b.get("req") or {}).get("level") or 0
            if req > self.level or (self.level < 70 and b["name"].startswith("Runeforged ")):
                continue
            if b["name"].startswith("Runeforged "):
                continue          # same numbers as the plain base plus a rune socket we do not fill
            if how == "weapon" and b.get("weapon"):
                w = b["weapon"]
                v = ((w.get("physMin") or 0) + (w.get("physMax") or 0)) / 2 * (w.get("attackRate") or 1)
                v = (v, req)
            elif how == "spirit":
                v = (b.get("spirit") or 0, req)
            elif how == "level":
                v = (req, 0)
            else:
                a = b.get("armour") or {}
                v = ((a.get("armour") or 0) + (a.get("evasion") or 0) + (a.get("energyShield") or 0) * 2, req)
            if best is None or v > bv:
                best, bv = b, v
        return best

    def armour_base(self, slot: str) -> tuple[str, dict] | None:
        fam = ARMOUR_SLOTS[slot]
        for d in (self.dtype, self.dtype.split("/")[0], "Armour"):
            t = f"{fam}: {d}"
            b = self.pick_base(t, "defence")
            if b:
                return t, b
        return None

    def jewellery_base(self, slot: str) -> tuple[str, dict] | None:
        fam = "Ring" if slot.startswith("Ring") else slot
        names = templates()["jewellery_bases"].get(slot) or []
        rows = self.bases(fam)
        for n in names:
            for b in rows:
                if b["name"] == n and ((b.get("req") or {}).get("level") or 0) <= self.level:
                    return fam, b
        return (fam, rows[0]) if rows else None

    # ------------------------------------------------------------------ affixes
    def affix_groups(self, type_name: str, base: str) -> tuple[list[str], list[str], int, int]:
        key = (type_name, base)
        if key not in self._affixes:
            try:
                r = self.e.raw("list_affixes", {"type": type_name, "baseName": base, "itemLevel": self.tp["itemLevel"]})
                self._affixes[key] = ([x["group"] for x in r.get("prefixes") or []],
                                      [x["group"] for x in r.get("suffixes") or []],
                                      int(r.get("prefixLimit") or 3), int(r.get("suffixLimit") or 3))
            except Exception:
                self._affixes[key] = ([], [], 3, 3)
        return self._affixes[key]

    @staticmethod
    def _resolve(wishes: list, pool: list[str], limit: int) -> list[str]:
        out: list[str] = []
        for alts in wishes:
            if len(out) >= limit:
                break
            for rx in alts:
                hit = next((g for g in pool if re.search(rx, g) and g not in out), None)
                if hit:
                    out.append(hit)
                    break
        return out

    LIFE_RX = re.compile(r"Life")
    ES_WISH = ["^IncreasedEnergyShield$", "^EnergyShield$", "^GlobalEnergyShieldPercent$"]

    def _gem_level_wish(self) -> list[str] | None:
        """The +skill-level affix that fits the main skill (None: none fits / unknown skill)."""
        t = self.skill_types
        if not t:
            return None
        if t & {"Minion", "CreatesMinion"}:
            return ["^GlobalIncreaseMinionSpellSkillGemLevel$", "^GlobalIncreaseMinionSkillGemLevel$"]
        if "Spell" in t:
            return ["^GlobalIncreaseSpellSkillGemLevel$"]
        if "Attack" in t:
            alts = ["^GlobalIncreaseMeleeSkillGemLevel$"] if "Melee" in t else []
            if t & {"Projectile", "ProjectilesFromUser"}:
                alts.append("^GlobalIncreaseProjectileSkillGemLevel$")
            return alts or None
        return None

    def _follow_plan(self, t: dict) -> dict:
        """Templates follow the defence plan and skill kind: no life affixes on CI (ES first on ES builds),
        and the +level affix of the main skill's kind (no +spell levels on an attack amulet)."""
        ci = (self.defence or "").lower() == "ci"

        def fix(wishes):
            out = []
            for alts in wishes:
                if any("GemLevel" in a for a in alts):
                    w = self._gem_level_wish()
                    if w:
                        out.append(w)
                    continue
                if any(self.LIFE_RX.search(a) for a in alts):
                    if ci:
                        out.append(self.ES_WISH)
                        continue
                    if self.es and alts == ["^IncreasedLife$"]:
                        out.append(self.ES_WISH + alts)
                        continue
                out.append(alts)
            return out
        return {k: fix(v) if k in ("prefixes", "suffixes") else v for k, v in t.items()}

    def template_for(self, slot: str, kind: str) -> dict:
        T = templates()["templates"]
        if kind == "armour":
            t = dict(T["armour"]["es" if self.es else "life"])
            if slot == "Boots":
                t = {"prefixes": T["Boots"]["extra_prefix_first"] + t["prefixes"], "suffixes": t["suffixes"]}
            return self._follow_plan(t)
        return self._follow_plan(T.get(kind) or T.get(slot) or {"prefixes": [], "suffixes": []})

    def craft(self, slot: str, type_name: str, base: dict, kind: str, title: str | None = None) -> dict | None:
        pre, suf, pl, sl = self.affix_groups(type_name, base["name"])
        t = self.template_for(slot, kind)
        prefixes = self._resolve(t.get("prefixes") or [], pre, pl)
        suffixes = self._resolve(t.get("suffixes") or [], suf, sl)
        params = {"type": base.get("type") or type_name, "baseName": base["name"], "prefixes": prefixes,
                  "suffixes": suffixes, "itemLevel": self.tp["itemLevel"], "range": self.tp["range"],
                  "slot": slot, "title": title or f"Synth {slot}"}
        for attempt in range(3):
            try:
                r = self.e.raw("craft_rare", params)
                self.taken[slot] = f"template:{base['name']}"
                return r
            except Exception as ex:              # a family clash: drop the last wish and retry
                msg = str(ex)
                if params["suffixes"]:
                    params["suffixes"] = params["suffixes"][:-1]
                elif params["prefixes"]:
                    params["prefixes"] = params["prefixes"][:-1]
                else:
                    self.notes.append(f"gear: craft {slot} {base['name']} failed: {msg[:120]}")
                    return None
        self.notes.append(f"gear: craft {slot} {base['name']} failed after retries")
        return None

    # ------------------------------------------------------------------ uniques
    def find_unique(self, name: str) -> dict | None:
        try:
            rows = self.e.raw("item_db_list", {"query": name, "limit": 20}).get("items") or []
        except Exception:
            return None
        exact = [r for r in rows if r["name"].lower() == name.lower()]
        return (exact or rows or [None])[0]

    def equip_unique(self, name: str, slot: str | None = None, variant=None) -> str | None:
        row = self.find_unique(name)
        if row is None:
            self.notes.append(f"gear: unique {name!r} not in PoB's item DB")
            return None
        want = slot or row.get("slot")
        cands = [want]
        if want and want.startswith("Ring"):
            cands = ["Ring 1", "Ring 2"] if not slot else [want]
        for s in cands:
            if s and s in self.taken and not slot:
                continue
            p = {"name": row["name"]}
            if s:
                p["slotName"] = s
            if variant is not None:
                p["variant"] = variant
            try:
                r = self.e.raw("item_db_equip", p)
            except Exception as ex:
                self.notes.append(f"gear: equip unique {row['name']} in {s}: {str(ex)[:120]}")
                continue
            got = r.get("slot") or s
            self.taken[got] = f"unique:{row['name']}"
            if got == "Weapon 1":
                spec = weapon_spec(row.get("type"))
                self.two_handed = bool(spec and spec.get("hands") == 2)
            self.notes.append(f"gear: unique {row['name']} -> {got}")
            return got
        return None

    # ------------------------------------------------------------------ weapons
    def equip_weapons(self, spec: dict | None) -> None:
        """Main hand from `spec` (weapon_spec) unless a unique holds it; off-hand when one-handed.
        An "Unarmed" spec (type None) leaves both hands empty: any weapon disables unarmed skills."""
        if spec and spec.get("type") is None:
            self.notes.append("gear: unarmed (no weapon)")
            return
        if "Weapon 1" not in self.taken and spec:
            how = templates()["weapon_pick"].get(spec["type"], "weapon")
            b = self.pick_base(spec["type"], how)
            if b is None:
                self.notes.append(f"gear: no {spec['type']} base at level {self.level}")
            else:
                self.craft("Weapon 1", spec["type"], b, "weapon")
                self.two_handed = spec.get("hands") == 2
                self.notes.append(f"gear: weapon {b['name']} ({spec['type']})")
        if "Weapon 2" in self.taken:
            return
        off = (spec or {}).get("offhand")
        if self.two_handed and off != "Quiver":
            return
        if off is None:
            return
        if off == "Buckler":
            # bucklers are the evasion shields (PoB tags them buckler; Parry needs one)
            b = self.pick_base("Shield: Evasion", "defence")
            if b:
                self.craft("Weapon 2", "Shield: Evasion", b, "Shield")
            return
        if off == "Armoured Shield":
            # Shield Charge / Resonating Shield need a shield with Armour
            kinds = [d for d in (self.dtype, self.dtype.split("/")[0]) if "Armour" in d] + \
                ["Armour", "Armour/Evasion", "Armour/Energy Shield"]
            for d in kinds:
                b = self.pick_base(f"Shield: {d}", "defence")
                if b:
                    self.craft("Weapon 2", f"Shield: {d}", b, "Shield")
                    return
            return
        if off == "Shield":
            for d in (self.dtype, self.dtype.split("/")[0], "Armour"):
                b = self.pick_base(f"Shield: {d}", "defence")
                if b:
                    self.craft("Weapon 2", f"Shield: {d}", b, "Shield")
                    return
            return
        b = self.pick_base(off, templates()["weapon_pick"].get(off, "level"))
        if b:
            self.craft("Weapon 2", off, b, off)

    # ------------------------------------------------------------------ templates
    def fill_templates(self) -> None:
        for slot in ARMOUR_SLOTS:
            if slot in self.taken:
                continue
            tb = self.armour_base(slot)
            if tb:
                self.craft(slot, tb[0], tb[1], "armour")
        for slot in JEWELLERY_SLOTS:
            if slot in self.taken:
                continue
            tb = self.jewellery_base(slot)
            if tb:
                self.craft(slot, tb[0], tb[1], "Ring" if slot.startswith("Ring") else slot)

    # ------------------------------------------------------------------ optimise_gear
    def optimise(self, weights: dict | None = None, preset: str | None = None, slots: list[str] | None = None,
                 timeout: float = 300) -> dict:
        """Run the bridge's greedy per-slot affix optimiser on the non-unique slots and equip every proposal.
        -> {"equipped": [slot...], "skipped": [...], "summary": str}"""
        keep = {s for s, v in self.taken.items() if v.startswith("unique:")}
        todo = [s for s in (slots or GEAR_SLOTS) if s not in keep]
        if self.two_handed and "Weapon 2" in todo and not str(self.taken.get("Weapon 2", "")).startswith("template:"):
            todo.remove("Weapon 2")
        p: dict[str, Any] = {"slots": todo, "itemLevel": self.tp["itemLevel"], "range": self.tp["range"],
                             "titlePrefix": "Synth"}
        if weights:
            p["weights"] = weights
        else:
            p["preset"] = preset or "balanced"
        r = self.e.request({"m": "optimise_gear", "p": p}, timeout=timeout)
        self.e._mutated = True
        equipped = []
        for prop in r.get("proposals") or []:
            try:
                self.e.raw("equip_item_raw", {"text": prop["raw"], "slot": prop["slot"]})
                equipped.append(prop["slot"])
                self.taken.setdefault(prop["slot"], "optimised")
            except Exception as ex:
                self.notes.append(f"gear: equip proposal {prop.get('slot')}: {str(ex)[:120]}")
        return {"equipped": equipped, "skipped": r.get("skipped") or [], "summary": r.get("summary"),
                "delta": r.get("delta")}

    def slots_view(self) -> list[dict]:
        try:
            return self.e.raw("list_slots", {}).get("slots") or []
        except Exception:
            return []
