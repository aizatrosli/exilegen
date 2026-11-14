"""Skeleton generator: proposes build ideas (class/ascendancy + main skill + trigger setup + 0-3 uniques +
keystones + defence hint) for the from-scratch meta builder.  Implements contracts.SkeletonGenerator.

Sources (each skeleton records `source` and a one-line `thesis`):
  corpus_prior     (skill, ascendancy) sampled by corpus play x p90 PoB power; strong-but-rare up-weighted
  build_around     a build-around unique (corpus within-skill gap, granted skill, strong gated payoff line)
                   paired with a skill whose tags its payoff scales; unique-granted skills become the main skill
  mechanics_graph  a typed produces/consumes chain from mechgraph (data/chains.json) forced into a skeleton
  anti_meta        corpus anti-meta pairs (shared mechanics, never seen together) and strong-but-rare skill x rare
                   ascendancy pairs
  mutation         one-step change of a parent: skill (same tags), unique (same slot / same mechanic),
                   ascendancy (same class), keystone, trigger meta / feeder

Constraints (scoring.constraints DSL) are respected at generation time with a static check (`violations`):
  one_button        the main skill is not a manual skill: meta-gem trigger (+ <=1 manual damaging feeder),
                    a unique's own trigger, persistent or minion
  no_cast[:lenient] no manual spell: spells only triggered by a non-spell feeder / no feeder (block, dodge,
                    minion death, unique trigger on travel ...); attacks, minions, persistent skills are free
  no_cast:strict    no spell at all; no_minions; require_/forbid_ gem / unique / keystone
Legality: ascendancy belongs to the class, ascendancy-granted skills force their ascendancy, attack skills
match the weapon (unique weapon or hint), unique slots (one per slot, rings x2, two-handed blocks the
off-hand except bow + quiver, quiver needs a bow), shield skills get a shield.

Feedback (all keys optional):
  {"source_success": {source: rate}, "source_weights": {source: w}, "cell_counts": {cell_key(sk): n},
   "skill_counts": {main_skill: n}, "unique_counts": {name: n}, "elites": [skeleton dicts to mutate],
   "seen_ids": [ids to skip]}
Under-explored cells / skills / uniques are accepted more often (rejection weight 1/sqrt(1+n)); sources
with a higher success rate get more of the batch.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter

from exilegen.scoring.constraints import Constraint

from . import catalog as catmod
from . import mechgraph
from .contracts import Skeleton

SOURCES = ("corpus_prior", "build_around", "mechanics_graph", "anti_meta", "mutation")
DEFAULT_WEIGHTS = {"corpus_prior": 0.18, "build_around": 0.26, "mechanics_graph": 0.26, "anti_meta": 0.15, "mutation": 0.15}

META_INFO = {
    # meta id: (name, needs feeder, modelled by the optimiser's TriggerStep)
    "MetaCastOnCritPlayer": ("Cast on Critical", True, True),
    "MetaCastOnElementalAilmentPlayer": ("Cast on Elemental Ailment", True, True),
    "MetaCastOnMinionDeathPlayer": ("Cast on Minion Death", False, False),
    "MetaCastOnBlockPlayer": ("Cast on Block", False, False),
    "MetaCastOnDodgePlayer": ("Cast on Dodge", False, False),
}
SPELL_FEEDERS = ["Spark", "Arc", "Frost Darts", "Lightning Bolt", "Ice Nova", "Flame Wall", "Fireball", "Frostbolt"]
ATTACK_FEEDERS = ["Lightning Arrow", "Rapid Shot", "Ice Shot", "Quarterstaff Strike", "Tempest Flurry", "Falling Thunder",
                  "Spear Stab", "Twister", "Whirling Slash", "Rapid Assault", "Fragmentation Rounds", "Galvanic Shards",
                  "Mace Strike", "Crossbow Shot", "Bow Shot", "Killing Palm", "Primal Strikes"]
MINION_FODDER = ["Skeletal Warrior", "Raise Zombie", "Skeletal Sniper"]
CLASS_DEFENCE = {
    "Sorceress": ["es", "hybrid", "ci", "life"], "Witch": ["es", "life", "ci", "hybrid"], "Monk": ["evasion", "es", "hybrid"],
    "Ranger": ["evasion", "life", "hybrid"], "Huntress": ["evasion", "life", "armour"], "Warrior": ["armour", "life"],
    "Mercenary": ["armour", "evasion", "life"], "Druid": ["armour", "life", "hybrid"],
}
KEYSTONE_DEFENCE = {"Chaos Inoculation": "ci", "Mind Over Matter": "mom", "Eldritch Battery": "es", "Zealot's Oath": "es",
                    "Blood Magic": "life", "Iron Reflexes": "armour", "Pain Attunement": "life", "Vaal Pact": "life"}
KEYSTONE_CONFLICT = [{"Chaos Inoculation", "Blood Magic"}, {"Chaos Inoculation", "Pain Attunement"},
                     {"Chaos Inoculation", "Vaal Pact"}, {"Blood Magic", "Eldritch Battery"},
                     {"Chaos Inoculation", "Eternal Youth"}, {"Resolute Technique", "Heartstopper"}]
DEFENCE_CONFLICT = {"Chaos Inoculation": {"life", "mom", "armour"}, "Blood Magic": {"es", "ci"},
                    "Eldritch Battery": {"ci", "life"}, "Zealot's Oath": {"ci"}}
TWO_HANDED = catmod.TWO_HANDED
SPELL_WEAPONS_2H = ["Staff"]
SPELL_WEAPONS_1H = ["Wand", "Sceptre"]
_CONTENT = ("cls", "ascendancy", "main_skill", "level", "supports", "trigger", "extra_skills", "weapon", "uniques",
            "keystones", "ascendancy_nodes", "defence", "budget_tier")


# ---------------------------------------------------------------------------------------------- helpers
def skeleton_id(sk: Skeleton) -> str:
    d = sk.to_dict()
    body = {k: d[k] for k in _CONTENT}
    body["uniques"] = sorted(({"name": u["name"], "slot": u.get("slot")} for u in body["uniques"]), key=lambda u: u["name"])
    body["keystones"] = sorted(body["keystones"])
    body["ascendancy_nodes"] = sorted(body["ascendancy_nodes"])
    body["extra_skills"] = sorted(body["extra_skills"])
    return hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def cell_key(sk: Skeleton) -> str:
    """Coarse behaviour cell used for feedback when the archive passes cell_counts keyed the same way."""
    t = sk.tags or {}
    return f"{t.get('primary_damage', '?')}|{t.get('activation', '?')}|{sk.defence or '?'}"


def _wchoice(rng: random.Random, items: list, weights: list[float]):
    tot = sum(max(0.0, w) for w in weights)
    if not items or tot <= 0:
        return None
    r = rng.random() * tot
    acc = 0.0
    for it, w in zip(items, weights):
        acc += max(0.0, w)
        if r <= acc:
            return it
    return items[-1]


def _parse_constraints(constraints) -> list[Constraint]:
    return [Constraint.parse(c) for c in (constraints or [])]


# ---------------------------------------------------------------------------------------------- generator
class Generator:
    """contracts.SkeletonGenerator implementation. Deterministic for a given seed and catalogue cache."""

    def __init__(self, cat: catmod.Catalog | None = None, chains: list[dict] | None = None,
                 weights: dict | None = None, level: int = 90, budget_tier: str = "mid"):
        self.cat = cat or catmod.load()
        self.graph = mechgraph.MechGraph(self.cat)
        self.chains = chains if chains is not None else mechgraph.load_chains()
        self.weights = dict(DEFAULT_WEIGHTS, **(weights or {}))
        self.level, self.budget_tier = level, budget_tier
        self.mains = {s["id"]: s for s in self.cat.main_skill_candidates()}
        self.by_name: dict[str, dict] = {}
        for s in sorted(self.mains.values(), key=lambda x: (bool(x["granted_by"]) and not self._gem_ok(x), x["id"])):
            self.by_name.setdefault(s["name"].lower(), s)
        corpus = self.cat.corpus or {}
        self.ninja_skill = corpus.get("ninja", {}).get("skills", {})
        self.ninja_class = corpus.get("ninja", {}).get("class", {})
        self.skill_by_asc = corpus.get("skill_by_asc", {})
        self.power_skill = corpus.get("power_skill", {})
        self.power_asc = corpus.get("power_asc", {})
        self.pair = corpus.get("pair", {})
        self.unique_triggers = self._unique_triggers()
        self._build_around = self._build_around_list()

    # ---- catalogue views ---------------------------------------------------------------------------------
    @staticmethod
    def _gem_ok(s: dict) -> bool:
        return bool(s["is_gem"] and (s["tier"] or 0) > 0)

    def requires_unique(self, s: dict) -> list[str]:
        """Uniques one of which must be equipped for the skill to exist (empty = socketable gem)."""
        if self._gem_ok(s) or (s["is_gem"] and not s["granted_by"] and not s["id"].startswith("Unique")):
            return []
        return list(s["granted_by"])

    def _unique_triggers(self) -> dict[str, dict]:
        """skill id -> {unique, text, feeder_kind} for uniques whose lines trigger the granted skill."""
        out = {}
        for u in self.cat.uniques.values():
            for gid in u.get("granted_skill_ids", []):
                s = self.cat.skills.get(gid)
                if not s:
                    continue
                for ln in u["lines"]:
                    low = ln.lower()
                    if "trigger" in low and s["name"].lower() in low:
                        if "travel" in low:
                            kind = "none"            # movement: no button at all
                        elif "casting a spell" in low:
                            kind = "spell"
                        elif "critical" in low:
                            kind = "hit"
                        elif "with this weapon" in low:
                            kind = "weapon_attack"
                        elif "kill" in low or "consume a charge" in low:
                            kind = "hit"
                        else:
                            kind = "hit"
                        out.setdefault(gid, {"unique": u["name"], "text": ln, "feeder_kind": kind})
                if gid not in out and ("InbuiltTrigger" in s["types"] or "Triggered" in s["types"]):
                    out[gid] = {"unique": u["name"], "text": "inbuilt trigger", "feeder_kind": "hit"}
        return out

    def _unique_entry(self, name: str, role: str = "") -> dict:
        u = self.cat.uniques[name]
        d = {"name": name, "slot": u["slot"]}
        if role:
            d["role"] = role
        if u.get("granted_skill_ids"):
            d["grants"] = list(u["granted_skill_ids"])
        return d

    def _ufacts(self, name: str) -> dict:
        u = self.cat.uniques[name]
        return {"name": name, "slot": u["slot"], "two_handed": u.get("two_handed"), "weapon_class": u.get("weapon_class"),
                "base_type": u.get("base_type")}

    def _build_around_list(self) -> list[tuple[str, float, str]]:
        """(unique, weight, why) for build-around uniques."""
        out: dict[str, tuple[float, str]] = {}
        for r in (self.cat.corpus or {}).get("uniques_buildaround", []):
            nm = r.get("unique")
            if nm not in self.cat.uniques:
                continue
            gap = catmod._f(r.get("within_skill_gap_log10"))
            if gap >= 0.15:
                out[nm] = (1.0 + 2.0 * gap, f"corpus within-skill DPS gap x{10 ** gap:.1f}")
            elif r.get("grants"):
                out[nm] = (1.0, "grants " + r["grants"].split("/")[0])
        for gid, info in self.unique_triggers.items():
            if gid in self.mains:
                out.setdefault(info["unique"], (1.6, f"triggers {self.cat.skills[gid]['name']}"))
        for s in self.mains.values():
            for u in self.requires_unique(s):
                if u in self.cat.uniques:
                    out.setdefault(u, (1.3, f"grants {s['name']}"))
        for e in self.graph.entities.values():
            if e.kind != "unique" or e.name in out:
                continue
            strong = [l for l in e.consumes.values() if l.payoff >= 1.0 and self.graph.spec.get(l.token, 0) > 0.3]
            if strong:
                out[e.name] = (0.8, f"gated payoff: '{strong[0].line[:60]}'")
        return sorted(((k, v[0], v[1]) for k, v in out.items()), key=lambda x: x[0])

    # ---- activation / trigger planning ---------------------------------------------------------------------
    def _plans(self, s: dict, cons: list[Constraint], forced_uniques: list[str]) -> list[str]:
        acts = set(self.cat.activations(s))
        types = set(s["types"])
        spell = "Spell" in types
        ctypes = {c.type: c for c in cons}
        plans = []
        ut = self.unique_triggers.get(s["id"])
        if ut and (ut["unique"] in forced_uniques or (len(forced_uniques) < 3 and not any(
                s["id"] in self.cat.uniques[u].get("granted_skill_ids", []) for u in forced_uniques if u in self.cat.uniques))):
            plans.append("inbuilt_trigger")
        for a in ("manual", "meta_trigger", "persistent", "minion", "totem"):
            if a in acts:
                plans.append(a)
        if "inbuilt_trigger" in acts and "inbuilt_trigger" not in plans and (s.get("asc_granted") or self.requires_unique(s)):
            plans.append("inbuilt_trigger")
        ok = []
        for p in plans:
            if "one_button" in ctypes and p in ("manual", "totem"):
                continue
            if "no_cast" in ctypes:
                strict = ctypes["no_cast"].arg == "strict"
                if spell and (strict or p in ("manual", "totem")):
                    continue
                if spell and p == "inbuilt_trigger" and self.unique_triggers.get(s["id"], {}).get("feeder_kind") == "spell":
                    continue
            if "no_minions" in ctypes and p == "minion":
                continue
            ok.append(p)
        return ok

    def _feeder(self, rng, kind: str, weapon: str | None, no_spell: bool, elemental: bool = False) -> dict | None:
        """A manual damaging feeder skill. kind: 'spell' | 'attack' | 'any'."""
        pools = []
        if kind in ("spell", "any") and not no_spell:
            pools += [self.by_name.get(n.lower()) for n in SPELL_FEEDERS]
        if kind in ("attack", "any"):
            pools += [self.by_name.get(n.lower()) for n in ATTACK_FEEDERS]
        pools = [p for p in pools if p and catmod.weapon_ok(p, weapon) and not self.requires_unique(p)
                 and "manual" in self.cat.activations(p)
                 and (weapon is not None or "Attack" not in p["types"] or not p["weapon_types"])]
        if elemental:
            pools = [p for p in pools if set(p["damage_types"]) & {"Fire", "Cold", "Lightning"}] or pools
        if not pools and kind in ("attack", "any") and weapon:
            pools = [p for p in self.mains.values() if "Attack" in p["types"] and catmod.weapon_ok(p, weapon)
                     and p["weapon_types"] and "manual" in self.cat.activations(p) and not self.requires_unique(p)]
            pools.sort(key=lambda p: p["id"])
        if not pools:
            return None
        return pools[rng.randrange(len(pools))]

    # ---- weapon / slot planning ------------------------------------------------------------------------------
    def _weapon_for(self, rng, s: dict, uniques: list[str], prefer: str | None = None) -> str | None:
        for u in uniques:
            uu = self.cat.uniques[u]
            if uu["slot"] == "Weapon 1":
                return uu.get("weapon_class")
        has_offhand = any(self.cat.uniques[u]["slot"] == "Weapon 2" for u in uniques)
        quiver = any(self.cat.uniques[u].get("base_type") == "Quiver" for u in uniques)
        if quiver:
            return "Bow"
        wt = [w for w in s.get("weapon_types") or [] if w != "None"]
        if "Attack" in s["types"] and wt and "NonWeaponAttack" not in s["types"]:
            if prefer in wt:
                return prefer
            if has_offhand:
                wt = [w for w in wt if w not in TWO_HANDED] or wt
            return wt[rng.randrange(len(wt))]
        if "RequiresShield" in s["types"] or "RequiresBuckler" in s["types"]:
            return "One Hand Mace" if "Attack" in s["types"] else SPELL_WEAPONS_1H[0]
        if prefer:
            return prefer
        if "Minion" in s["types"] or "CreatesMinion" in s["types"]:
            return "Sceptre"
        pool = SPELL_WEAPONS_1H if has_offhand else SPELL_WEAPONS_1H + SPELL_WEAPONS_2H
        return pool[rng.randrange(len(pool))]

    def _uniques_ok(self, uniques: list[str], weapon: str | None, s: dict) -> bool:
        facts = [self._ufacts(u) for u in uniques if u in self.cat.uniques]
        if len(facts) != len(uniques) or catmod.slot_conflicts(facts):
            return False
        w2 = [f for f in facts if f["slot"] == "Weapon 2"]
        if weapon in TWO_HANDED and w2 and not (weapon == "Bow" and w2[0].get("base_type") == "Quiver"):
            return False
        if any(f.get("base_type") == "Quiver" for f in w2) and weapon != "Bow":
            return False
        w1 = [f for f in facts if f["slot"] == "Weapon 1"]
        if w1 and not catmod.weapon_ok(s, w1[0].get("weapon_class")):
            return False
        if ("RequiresShield" in s["types"] or "RequiresBuckler" in s["types"]) and w2 and w2[0].get("base_type") != "Shield":
            return False
        return True

    # ---- class / ascendancy ----------------------------------------------------------------------------------
    def _pick_asc(self, rng, s: dict, mode: str = "prior") -> str:
        forced = s.get("asc_granted") or []
        if forced:
            return forced[0][0]
        ascs = sorted(self.cat.ascendancies)
        by = self.skill_by_asc.get(s["name"], {})
        ws = []
        for a in ascs:
            n = by.get(a, 0)
            pw = self.power_asc.get(a, {})
            p90 = max(pw.get("p90_dps", 0), 1.0)
            strong_rare = 1.5 if "STRONG-RARE" in (pw.get("flag") or "") else 1.0
            if mode == "prior":
                w = (n + 2.0) ** 0.6 * (p90 / 1e6) ** 0.3 * strong_rare
            else:   # anti-meta: unexplored ascendancies for this skill, still powerful overall
                w = (p90 / 1e6) ** 0.5 * strong_rare / math.sqrt(1.0 + n)
            ws.append(w)
        return _wchoice(rng, ascs, ws)

    def _defence(self, rng, cls: str, keystones: list[str], uniques: list[str]) -> str:
        for k in keystones:
            if k in KEYSTONE_DEFENCE:
                return KEYSTONE_DEFENCE[k]
        text = " ".join(" ".join(self.cat.uniques[u]["lines"]) for u in uniques).lower()
        opts = list(CLASS_DEFENCE.get(cls, ["life", "hybrid"]))
        if "per 100 maximum life" in text or "maximum life" in text and "energy shield" not in text:
            opts = ["life"] + opts
        if "energy shield" in text:
            opts = ["es"] + opts
        banned = set().union(*[DEFENCE_CONFLICT.get(k, set()) for k in keystones]) if keystones else set()
        opts = [o for o in opts if o not in banned] or ["hybrid"]
        return opts[0] if rng.random() < 0.5 else opts[rng.randrange(len(opts))]

    # ---- assembly --------------------------------------------------------------------------------------------
    def assemble(self, rng: random.Random, main: str | dict, *, source: str, thesis: str, constraints=None,
                 uniques: list | None = None, keystones: list | None = None, asc: str | None = None,
                 asc_nodes: list | None = None, extra_skills: list | None = None, supports: list | None = None,
                 plan: str | None = None, meta: str | None = None, feeder: str | None = None,
                 weapon: str | None = None, defence: str | None = None, parent: str | None = None,
                 extra_tags: dict | None = None, asc_mode: str = "prior") -> Skeleton | None:
        s = main if isinstance(main, dict) else (self.mains.get(main) or self.by_name.get(str(main).lower())
                                                 or self.cat.skill(str(main)))
        if not s or s["id"] not in self.mains:
            return None
        cons = _parse_constraints(constraints)
        uniques = list(dict.fromkeys(u["name"] if isinstance(u, dict) else u for u in (uniques or [])))
        roles = {u["name"]: u.get("role", "") for u in (uniques or []) if isinstance(u, dict)}
        # skill must exist: add its granting unique if needed
        req = self.requires_unique(s)
        if req and not set(req) & set(uniques):
            pick = sorted(req)[rng.randrange(len(req))]
            uniques.insert(0, pick)
            roles[pick] = "grants_main"
        for c in cons:
            if c.type == "require_unique" and c.arg in self.cat.uniques and c.arg not in uniques:
                uniques.append(c.arg)
            if c.type == "forbid_unique" and c.arg in uniques:
                return None
        if len(uniques) > 3:
            uniques = uniques[:3]
        keystones = sorted(set(keystones or []))
        for c in cons:
            if c.type == "require_keystone" and c.arg in self.cat.keystones and c.arg not in keystones:
                keystones.append(c.arg)
            if c.type == "forbid_keystone" and c.arg in keystones:
                return None
        if any(pair <= set(keystones) for pair in KEYSTONE_CONFLICT):
            return None
        # ascendancy / class
        asc_nodes = list(asc_nodes or [])
        if s.get("asc_granted"):
            ga, gn = s["asc_granted"][0]
            if asc and asc != ga:
                return None
            asc = ga
            if gn not in asc_nodes:
                asc_nodes.append(gn)
        asc = asc or self._pick_asc(rng, s, asc_mode)
        cls = self.cat.class_of(asc)
        if not cls:
            return None
        valid_nodes = {n["name"] for n in self.cat.ascendancies[asc]["notables"]}
        if any(n not in valid_nodes for n in asc_nodes):
            return None
        # activation plan
        plans = self._plans(s, cons, uniques)
        if not plans:
            return None
        if plan not in plans:
            ctypes = {c.type for c in cons}
            if "one_button" in ctypes or "no_cast" in ctypes:
                order = ["inbuilt_trigger", "persistent", "minion", "meta_trigger", "manual", "totem"]
            else:
                order = ["inbuilt_trigger", "manual", "persistent", "minion", "meta_trigger", "totem"]
            pref = [p for p in order if p in plans]
            plan = pref[0] if rng.random() < 0.7 else pref[rng.randrange(len(pref))]
        no_cast = next((c for c in cons if c.type == "no_cast"), None)
        no_spell_feeder = no_cast is not None
        weapon = self._weapon_for(rng, s, uniques, weapon)
        trigger, extra = None, list(extra_skills or [])
        if plan == "meta_trigger":
            metas = sorted(META_INFO)
            if any(c.type == "no_minions" for c in cons):
                metas = [m for m in metas if m != "MetaCastOnMinionDeathPlayer"]
            if meta not in metas:
                ws = [3.0 if META_INFO[m][2] else 1.0 for m in metas]
                if no_spell_feeder:   # prefer feeder-less metas when spells may not be cast by hand
                    ws = [w * (2.0 if not META_INFO[m][1] else 1.0) for m, w in zip(metas, ws)]
                meta = _wchoice(rng, metas, ws)
            name, needs_feeder, modelled = META_INFO[meta]
            trigger = {"meta": meta, "meta_name": name, "spells": [s["id"]], "modelled": modelled}
            if needs_feeder:
                fd = self.by_name.get(feeder.lower()) if feeder else None
                if fd is None or (no_spell_feeder and "Spell" in fd["types"]) or not catmod.weapon_ok(fd, weapon):
                    fd = self._feeder(rng, "attack" if no_spell_feeder else "any", weapon if no_spell_feeder or weapon not in SPELL_WEAPONS_1H + SPELL_WEAPONS_2H else None,
                                      no_spell_feeder, elemental=meta == "MetaCastOnElementalAilmentPlayer")
                    if fd and "Attack" in fd["types"] and fd["weapon_types"] and not catmod.weapon_ok(fd, weapon):
                        if any(self.cat.uniques[u]["slot"] == "Weapon 1" for u in uniques):
                            return None
                        weapon = next((w for w in fd["weapon_types"] if w != "None"), weapon)
                if fd is None:
                    return None
                trigger["feeder"] = fd["id"]
                trigger["feeder_name"] = fd["name"]
            elif meta == "MetaCastOnMinionDeathPlayer":
                fod = next((self.by_name[n.lower()] for n in MINION_FODDER if n.lower() in self.by_name
                            and not self.requires_unique(self.by_name[n.lower()])), None)
                if fod:
                    extra.append(fod["id"])
            elif meta == "MetaCastOnBlockPlayer":
                if weapon in TWO_HANDED:
                    weapon = SPELL_WEAPONS_1H[rng.randrange(2)]
                trigger["offhand"] = "Shield"
        elif plan == "inbuilt_trigger":
            info = self.unique_triggers.get(s["id"])
            if info is None and s.get("asc_granted"):
                info = {"unique": None, "asc_node": s["asc_granted"][0][1], "text": "ascendancy-granted trigger",
                        "feeder_kind": "hit"}
            info = info or {"unique": (req or [None])[0], "feeder_kind": "hit", "text": "inbuilt trigger"}
            trigger = {"meta": None, "source": info["unique"] or info.get("asc_node"), "text": info.get("text", ""),
                       "spells": [s["id"]]}
            if info["unique"] and info["unique"] not in uniques:
                uniques.insert(0, info["unique"])
                roles[info["unique"]] = "grants_main"
            fk = info.get("feeder_kind", "hit")
            if fk != "none":
                if fk == "spell" and no_spell_feeder:
                    return None
                fd = self._feeder(rng, "spell" if fk == "spell" else ("attack" if no_spell_feeder or fk == "weapon_attack"
                                                                      else "any"), weapon, no_spell_feeder)
                if fd is None:
                    return None
                if "Attack" in fd["types"] and not catmod.weapon_ok(fd, weapon):
                    return None
                trigger["feeder"], trigger["feeder_name"] = fd["id"], fd["name"]
        if len(uniques) > 3:
            return None
        if not self._uniques_ok(uniques, weapon, s):
            return None
        if trigger and trigger.get("offhand") == "Shield" and weapon in TWO_HANDED:
            return None
        defence = defence or self._defence(rng, cls, keystones, uniques)
        if any(defence in DEFENCE_CONFLICT.get(k, set()) for k in keystones):
            return None
        primary = (s["damage_types"] or ["Physical"])[0] if s["damage_types"] else ("Minion" if "Minion" in s["types"] else "Physical")
        tags = {"activation": plan, "main_skill_name": s["name"], "damage_types": s["damage_types"],
                "primary_damage": primary, "spell": "Spell" in s["types"], "attack": "Attack" in s["types"],
                "minion": "Minion" in s["types"] or "CreatesMinion" in s["types"]}
        if extra_tags:
            tags.update(extra_tags)
        sk = Skeleton(id="", cls=cls, ascendancy=asc, main_skill=s["id"], level=self.level,
                      supports=list(supports or []), trigger=trigger, extra_skills=sorted(set(extra)), weapon=weapon,
                      uniques=[self._unique_entry(u, roles.get(u, "")) for u in uniques], keystones=keystones,
                      ascendancy_nodes=sorted(asc_nodes), defence=defence, budget_tier=self.budget_tier,
                      source=source, thesis=thesis, parent=parent, tags=tags)
        if self.violations(sk, constraints):
            return None
        sk.tags["cell"] = cell_key(sk)
        sk.id = skeleton_id(sk)
        return sk

    # ---- static constraint / legality check ---------------------------------------------------------------
    def violations(self, sk: Skeleton, constraints=None) -> list[str]:
        out = []
        s = self.cat.skills.get(sk.main_skill)
        if s is None:
            return ["unknown main skill"]
        if self.cat.class_of(sk.ascendancy) != sk.cls:
            out.append(f"{sk.ascendancy} is not a {sk.cls} ascendancy")
        for ga, _ in s.get("asc_granted") or []:
            if ga != sk.ascendancy:
                out.append(f"{s['name']} needs {ga}")
        names = [u["name"] for u in sk.uniques]
        if any(n not in self.cat.uniques for n in names):
            out.append("unknown unique")
            return out
        if len(names) > 3:
            out.append("more than 3 uniques")
        if not self._uniques_ok(names, sk.weapon, s):
            out.append("unique slot / weapon conflict")
        if not catmod.weapon_ok(s, sk.weapon) and not any(self.cat.uniques[n]["slot"] == "Weapon 1" for n in names):
            out.append(f"{s['name']} cannot use {sk.weapon}")
        req = self.requires_unique(s)
        if req and not set(req) & set(names):
            out.append(f"{s['name']} needs one of {req}")
        act = (sk.tags or {}).get("activation")
        feeder = self.cat.skills.get((sk.trigger or {}).get("feeder") or "")
        manual_dmg = []          # manual damaging skills (names)
        manual_spells = []
        all_spells = []
        if act in ("manual", "totem"):
            manual_dmg.append(s["name"])
            if "Spell" in s["types"]:
                manual_spells.append(s["name"])
        if "Spell" in s["types"]:
            all_spells.append(s["name"])
        if feeder:
            manual_dmg.append(feeder["name"])
            if "Spell" in feeder["types"]:
                manual_spells.append(feeder["name"])
                all_spells.append(feeder["name"])
            if "Attack" in feeder["types"] and not catmod.weapon_ok(feeder, sk.weapon):
                out.append(f"feeder {feeder['name']} cannot use {sk.weapon}")
        for x in sk.extra_skills:
            e = self.cat.skills.get(x)
            if e and catmod.Catalog.is_damaging(e) and "manual" in self.cat.activations(e):
                manual_dmg.append(e["name"])
                if "Spell" in e["types"]:
                    manual_spells.append(e["name"])
            if e and "Spell" in e["types"] and catmod.Catalog.is_damaging(e):
                all_spells.append(e["name"])
        for c in _parse_constraints(constraints):
            if c.type == "one_button":
                if act in ("manual", "totem"):
                    out.append("one_button: main skill is manual")
                if len(manual_dmg) > 1:
                    out.append(f"one_button: manual damaging {manual_dmg}")
            elif c.type == "no_cast":
                if c.arg == "strict" and all_spells:
                    out.append(f"no_cast:strict spells {all_spells}")
                elif manual_spells:
                    out.append(f"no_cast manual spells {manual_spells}")
            elif c.type == "no_minions":
                if (sk.tags or {}).get("minion") or any("CreatesMinion" in (self.cat.skills.get(x) or {}).get("types", [])
                                                         for x in sk.extra_skills):
                    out.append("no_minions")
            elif c.type == "require_unique" and c.arg not in names:
                out.append(f"missing unique {c.arg}")
            elif c.type == "forbid_unique" and c.arg in names:
                out.append(f"forbidden unique {c.arg}")
            elif c.type == "require_keystone" and c.arg not in sk.keystones:
                out.append(f"missing keystone {c.arg}")
            elif c.type == "forbid_keystone" and c.arg in sk.keystones:
                out.append(f"forbidden keystone {c.arg}")
            elif c.type in ("require_gem", "forbid_gem"):
                ids = {sk.main_skill, *(sk.extra_skills or []), *(sk.supports or [])}
                if sk.trigger:
                    ids |= {sk.trigger.get("meta") or "", sk.trigger.get("feeder") or ""}
                nm = {(self.cat.skills.get(i) or {}).get("name", i).lower() for i in ids} | {i.lower() for i in ids if i}
                present = (c.arg or "").lower() in nm
                if c.type == "require_gem" and not present:
                    out.append(f"missing gem {c.arg}")
                if c.type == "forbid_gem" and present:
                    out.append(f"forbidden gem {c.arg}")
        return out

    # ---- sources -------------------------------------------------------------------------------------------
    def _src_corpus_prior(self, rng, cons):
        names, ws = [], []
        for nm, p in sorted(self.power_skill.items()):
            s = self.by_name.get(nm.lower())
            if not s:
                continue
            pop = self.ninja_skill.get(nm, 0) + 1
            w = pop ** 0.25 * (max(p["p90_dps"], 1) / 1e6) ** 0.5 * (2.0 if "STRONG-RARE" in p["flag"] else 1.0)
            names.append(s)
            ws.append(w)
        s = _wchoice(rng, names, ws)
        if not s:
            return None
        asc = self._pick_asc(rng, s, "prior")
        p = self.power_skill.get(s["name"], {})
        flag = " strong-but-rare;" if "STRONG-RARE" in p.get("flag", "") else ""
        thesis = (f"Proven core{flag} {s['name']} p90 {p.get('p90_dps', 0) / 1e6:.1f}M pinnacle DPS in corpus "
                  f"({self.ninja_skill.get(s['name'], 0)} chars), on {asc}")
        return self.assemble(rng, s, source="corpus_prior", thesis=thesis, constraints=cons, asc=asc)

    def _compatible_mains(self, ent: mechgraph.Entity, weapon_unique: dict | None) -> list[tuple[dict, float, str]]:
        out = []
        pays = [l for l in ent.consumes.values() if l.payoff > 0]
        for s in self.mains.values():
            if weapon_unique and not catmod.weapon_ok(s, weapon_unique.get("weapon_class")):
                continue
            if self.requires_unique(s) and ent.name not in s["granted_by"]:
                continue
            best, line = 0.0, ""
            for l in pays:
                c = self.graph.compat(l, s) * l.payoff * (0.5 + self.graph.spec.get(l.token, 0.3))
                if c > best:
                    best, line = c, l.line
            if best <= 0:
                continue
            n = self.pair.get("unique_skill", {}).get(f"{ent.name}||{s['name']}", 0)
            out.append((s, best / math.sqrt(1 + n), line))
        out.sort(key=lambda x: (-x[1], x[0]["id"]))
        return out

    def _src_build_around(self, rng, cons):
        cands = self._build_around
        u, w, why = _wchoice(rng, cands, [c[1] for c in cands]) or (None, 0, "")
        if not u:
            return None
        uu = self.cat.uniques[u]
        granted = [g for g in uu.get("granted_skill_ids", []) if g in self.mains and catmod.Catalog.is_damaging(self.mains[g])]
        if granted and rng.random() < 0.75:
            s = self.mains[granted[rng.randrange(len(granted))]]
            trig = self.unique_triggers.get(s["id"])
            how = f"'{trig['text'][:60]}'" if trig and trig["unique"] == u and trig.get("text") != "inbuilt trigger" \
                else "a granted skill"
            thesis = f"Build around {u}: {s['name']} via {how} ({why}); tree + supports re-solved around it"
            return self.assemble(rng, s, source="build_around", thesis=thesis, constraints=cons,
                                 uniques=[{"name": u, "role": "grants_main"}], extra_tags={"defining_unique": u})
        ent = self.graph.entities.get(f"unique:{u}")
        if not ent:
            return None
        wu = self._ufacts(u) if uu["slot"] == "Weapon 1" else None
        mains = self._compatible_mains(ent, wu)[:12]
        if not mains:
            return None
        s, sc, line = _wchoice(rng, mains, [m[1] for m in mains])
        thesis = f"Build around {u} ('{line[:70]}') scaling {s['name']} ({why})"
        return self.assemble(rng, s, source="build_around", thesis=thesis, constraints=cons,
                             uniques=[{"name": u, "role": "build_around"}], extra_tags={"defining_unique": u})

    def _src_mechanics_graph(self, rng, cons, chain: dict | None = None):
        ch = chain or _wchoice(rng, self.chains, [c["score"] ** 2 for c in self.chains])
        if not ch:
            return None
        asc = ch["asc_nodes"][0][0] if ch.get("asc_nodes") else None
        extra = [u for u in ch.get("util_skills") or [] if u]
        return self.assemble(rng, ch["main_skill"], source="mechanics_graph", thesis=ch["thesis"], constraints=cons,
                             uniques=[{"name": u, "role": "chain"} for u in ch["uniques"]], keystones=ch["keystones"],
                             asc=asc, asc_nodes=[n for _, n in ch.get("asc_nodes") or []], extra_skills=extra,
                             extra_tags={"chain_id": ch["id"], "chain_kind": ch["kind"], "chain_tokens": ch["tokens"],
                                         "defining_unique": (ch["uniques"] or [None])[0]})

    def _src_anti_meta(self, rng, cons):
        rows = (self.cat.corpus or {}).get("antimeta", [])
        if rows and rng.random() < 0.7:
            r = _wchoice(rng, rows, [catmod._f(x.get("score")) ** 0.5 for x in rows])
            kind, a, b, shared = r["kind"], r["a"], r["b"], r.get("shared", "")
            thesis = f"Anti-meta: {a} + {b} share {shared.replace('|', '/')} yet never co-occur in the corpus"
            if kind == "unique-skill" and a in self.cat.uniques and b.lower() in self.by_name:
                return self.assemble(rng, self.by_name[b.lower()], source="anti_meta", thesis=thesis, constraints=cons,
                                     uniques=[{"name": a, "role": "anti_meta"}], asc_mode="anti",
                                     extra_tags={"defining_unique": a})
            if kind == "support-skill" and b.lower() in self.by_name:
                return self.assemble(rng, self.by_name[b.lower()], source="anti_meta", thesis=thesis, constraints=cons,
                                     supports=[a], asc_mode="anti")
            if kind == "unique-unique" and a in self.cat.uniques and b in self.cat.uniques:
                ent = self.graph.entities.get(f"unique:{a}") or self.graph.entities.get(f"unique:{b}")
                if ent:
                    wu = next((self._ufacts(x) for x in (a, b) if self.cat.uniques[x]["slot"] == "Weapon 1"), None)
                    mains = self._compatible_mains(ent, wu)[:10]
                    if mains:
                        s = mains[rng.randrange(len(mains))][0]
                        return self.assemble(rng, s, source="anti_meta", thesis=thesis + f", for {s['name']}",
                                             constraints=cons, uniques=[{"name": a, "role": "anti_meta"},
                                                                        {"name": b, "role": "anti_meta"}], asc_mode="anti")
            return None
        # strong-but-rare skill x unexplored ascendancy
        items, ws = [], []
        for nm, p in sorted(self.power_skill.items()):
            s = self.by_name.get(nm.lower())
            if s and p["p90_dps"] > 0:
                items.append(s)
                ws.append((p["p90_dps"] / 1e6) ** 0.5 / math.sqrt(1 + self.ninja_skill.get(nm, 0) / 500))
        s = _wchoice(rng, items, ws)
        if not s:
            return None
        asc = self._pick_asc(rng, s, "anti")
        n = self.skill_by_asc.get(s["name"], {}).get(asc, 0)
        thesis = (f"Anti-meta: strong {s['name']} (p90 {self.power_skill[s['name']]['p90_dps'] / 1e6:.1f}M) on "
                  f"{asc}, {n} ladder chars play this pair")
        return self.assemble(rng, s, source="anti_meta", thesis=thesis, constraints=cons, asc=asc)

    # ---- mutation ------------------------------------------------------------------------------------------
    def _similar_skills(self, s: dict, weapon: str | None, has_weapon_unique: bool) -> list[dict]:
        st = set(s["types"])
        key = {"Spell", "Attack", "Minion", "Projectile", "Melee", "Area", "Persistent"} & st
        out = []
        for o in self.mains.values():
            if o["id"] == s["id"] or o["name"] == s["name"]:
                continue
            ot = set(o["types"])
            if has_weapon_unique and not catmod.weapon_ok(o, weapon):
                continue
            score = len(key & ot) + 2 * len(set(o["damage_types"]) & set(s["damage_types"]))
            if ({"Spell", "Attack", "Minion"} & st) != ({"Spell", "Attack", "Minion"} & ot):
                continue
            if score >= 2:
                out.append((score, o["id"], o))
        out.sort(key=lambda x: (-x[0], x[1]))
        return [o for _, _, o in out[:15]]

    def _related_uniques(self, name: str, rng: random.Random) -> list[str]:
        u = self.cat.uniques[name]
        same_slot = sorted(x for x, v in self.cat.uniques.items() if v["slot"] == u["slot"] and x != name)
        ent = self.graph.entities.get(f"unique:{name}")
        mech = []
        if ent:
            toks = set(ent.consumes) | set(ent.produces)
            toks = {t for t in toks if self.graph.spec.get(t, 0) > 0.3}
            for e in self.graph.entities.values():
                if e.kind == "unique" and e.name != name and toks & (set(e.consumes) | set(e.produces)):
                    mech.append(e.name)
        mech = sorted(set(mech))
        rng.shuffle(mech)
        slot = list(same_slot)
        rng.shuffle(slot)
        return mech[:12] + slot[:12]

    def mutate(self, parent: Skeleton, n: int = 1, seed: int | None = None, constraints=None) -> list[Skeleton]:
        rng = random.Random(seed)
        out, seen = [], {parent.id}
        tries = 0
        while len(out) < n and tries < 40 * max(1, n):
            tries += 1
            m = self._mutate_once(rng, parent, constraints)
            if m and m.id not in seen:
                seen.add(m.id)
                out.append(m)
        return out

    def _mutate_once(self, rng, p: Skeleton, constraints=None) -> Skeleton | None:
        ops = ["skill", "unique", "ascendancy", "keystone", "trigger"]
        op = ops[rng.randrange(len(ops))]
        main, uniques = p.main_skill, [dict(u) for u in p.uniques]
        asc, keys, trig = p.ascendancy, list(p.keystones), dict(p.trigger) if p.trigger else None
        nodes = list(p.ascendancy_nodes)
        meta = (trig or {}).get("meta")
        feeder = (trig or {}).get("feeder_name")
        plan = (p.tags or {}).get("activation")
        s = self.cat.skills.get(main)
        if s is None:
            return None
        has_wu = any(self.cat.uniques.get(u["name"], {}).get("slot") == "Weapon 1" for u in uniques)
        what = ""
        if op == "skill":
            opts = self._similar_skills(s, p.weapon, has_wu)
            if not opts:
                return None
            new = opts[rng.randrange(len(opts))]
            what = f"skill {s['name']} -> {new['name']}"
            uniques = [u for u in uniques if u.get("role") != "grants_main"]
            main = new["id"]
            if s.get("asc_granted"):
                nodes = [x for x in nodes if x not in {g[1] for g in s["asc_granted"]}]
            plan = None if plan == "inbuilt_trigger" else plan
        elif op == "unique":
            mutable = [u for u in uniques if u.get("role") != "grants_main"]
            r = rng.random()
            if mutable and r < 0.6:
                old = mutable[rng.randrange(len(mutable))]
                rel = self._related_uniques(old["name"], rng)
                if not rel:
                    return None
                newn = rel[rng.randrange(len(rel))]
                uniques = [u if u["name"] != old["name"] else {"name": newn, "role": "mutation"} for u in uniques]
                what = f"unique {old['name']} -> {newn}"
            elif len(uniques) < 3 and (r < 0.9 or not mutable):
                pool = [x[0] for x in self._build_around]
                newn = pool[rng.randrange(len(pool))]
                uniques.append({"name": newn, "role": "mutation"})
                what = f"add unique {newn}"
            elif mutable:
                old = mutable[rng.randrange(len(mutable))]
                uniques = [u for u in uniques if u["name"] != old["name"]]
                what = f"drop unique {old['name']}"
            else:
                return None
        elif op == "ascendancy":
            if s.get("asc_granted"):
                return None
            sibs = [a for a in self.cat.classes.get(p.cls, []) if a != asc]
            if not sibs:
                return None
            asc = sibs[rng.randrange(len(sibs))]
            nodes = []
            what = f"ascendancy {p.ascendancy} -> {asc}"
        elif op == "keystone":
            pool = sorted(self.cat.keystones)
            if keys and rng.random() < 0.35:
                old = keys[rng.randrange(len(keys))]
                keys.remove(old)
                what = f"drop keystone {old}"
            else:
                k = pool[rng.randrange(len(pool))]
                if k in keys:
                    return None
                if keys and rng.random() < 0.5:
                    old = keys.pop(rng.randrange(len(keys)))
                    what = f"keystone {old} -> {k}"
                else:
                    what = f"add keystone {k}"
                keys.append(k)
        elif op == "trigger":
            if trig and trig.get("meta"):
                if rng.random() < 0.5 and META_INFO.get(meta, (0, False))[1]:
                    feeder = None
                    what = "new trigger feeder"
                else:
                    metas = [m for m in sorted(META_INFO) if m != meta]
                    meta = metas[rng.randrange(len(metas))]
                    what = f"meta {trig.get('meta_name')} -> {META_INFO[meta][0]}"
            elif "meta_trigger" in self.cat.activations(s) and plan != "meta_trigger":
                plan, meta = "meta_trigger", None
                what = "convert to meta-gem trigger"
            else:
                return None
        defence = p.defence if op not in ("keystone", "ascendancy", "unique") else None
        thesis = f"{what} | from: {p.thesis[:110]}"
        return self.assemble(rng, main, source="mutation", thesis=thesis, constraints=constraints, uniques=uniques,
                             keystones=keys, asc=asc, asc_nodes=nodes, plan=plan, meta=meta, feeder=feeder,
                             weapon=p.weapon if op in ("ascendancy", "keystone", "trigger") else None,
                             defence=defence, parent=p.id, supports=p.supports,
                             extra_tags={"mutation": op, "parent_source": p.source})

    # ---- main entry ---------------------------------------------------------------------------------------
    def _source_weights(self, feedback: dict | None) -> dict[str, float]:
        w = dict(self.weights)
        fb = feedback or {}
        for k, v in (fb.get("source_weights") or {}).items():
            if k in w:
                w[k] = float(v)
        succ = fb.get("source_success") or {}
        if succ:
            mean = sum(float(v) for v in succ.values()) / max(1, len(succ))
            for k in w:
                if k in succ:
                    w[k] *= (0.3 + float(succ[k])) / (0.3 + mean) if mean >= 0 else 1.0
        return w

    def _accept(self, rng, sk: Skeleton, fb: dict) -> bool:
        p = 1.0
        cc = fb.get("cell_counts") or {}
        if cc:
            p *= 1.0 / math.sqrt(1.0 + float(cc.get(cell_key(sk), 0)) / 2.0)
        sc = fb.get("skill_counts") or {}
        if sc:
            p *= 1.0 / math.sqrt(1.0 + float(sc.get(sk.main_skill, sc.get((sk.tags or {}).get("main_skill_name"), 0))) / 2.0)
        uc = fb.get("unique_counts") or {}
        if uc and sk.uniques:
            p *= 1.0 / math.sqrt(1.0 + max(float(uc.get(u["name"], 0)) for u in sk.uniques) / 3.0)
        return rng.random() < max(0.15, p)

    def generate(self, n: int, constraints: list | None = None, feedback: dict | None = None,
                 seed: int | None = None) -> list[Skeleton]:
        rng = random.Random(seed)
        fb = feedback or {}
        weights = self._source_weights(fb)
        elites = []
        for e in fb.get("elites") or []:
            try:
                elites.append(e if isinstance(e, Skeleton) else Skeleton(**e))
            except TypeError:
                pass
        seen = set(fb.get("seen_ids") or [])
        out: list[Skeleton] = []
        # stratified source quotas (largest remainder), so small batches still mix sources
        srcs = [s for s in SOURCES if weights.get(s, 0) > 0]
        tot = sum(weights[s] for s in srcs)
        quota = {s: n * weights[s] / tot for s in srcs}
        alloc = {s: int(q) for s, q in quota.items()}
        for s in sorted(srcs, key=lambda s: (-(quota[s] - alloc[s]), s))[: n - sum(alloc.values())]:
            alloc[s] += 1
        order = []
        for s in srcs:
            order += [s] * alloc[s]
        rng.shuffle(order)
        # mutation needs parents: run it last
        order = [s for s in order if s != "mutation"] + [s for s in order if s == "mutation"]
        fns = {"corpus_prior": self._src_corpus_prior, "build_around": self._src_build_around,
               "mechanics_graph": self._src_mechanics_graph, "anti_meta": self._src_anti_meta}
        for src in order:
            got = None
            for _ in range(60):
                if src == "mutation":
                    parents = elites or out
                    if not parents:
                        break
                    par = parents[rng.randrange(len(parents))]
                    sk = self._mutate_once(rng, par, constraints)
                else:
                    sk = fns[src](rng, constraints)
                if sk is None or sk.id in seen:
                    continue
                if not self._accept(rng, sk, fb):
                    continue
                got = sk
                break
            if got is None:     # fall back to another source so the batch still reaches n
                for alt in ("mechanics_graph", "build_around", "corpus_prior", "anti_meta"):
                    for _ in range(40):
                        sk = fns[alt](rng, constraints)
                        if sk is not None and sk.id not in seen:
                            got = sk
                            break
                    if got:
                        break
            if got is not None:
                seen.add(got.id)
                out.append(got)
        return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="generate skeleton samples")
    ap.add_argument("--out", default="/mnt/project-files/builder/skeletons_sample.jsonl")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--n", type=int, default=20)
    a = ap.parse_args(argv)
    import os
    g = Generator()
    rows = []
    for i, cons in enumerate([[], ["one_button"], ["no_cast"]]):
        for sk in g.generate(a.n, cons, seed=a.seed + i):
            d = sk.to_dict()
            d["constraints"] = cons
            rows.append(d)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        for d in rows:
            f.write(json.dumps(d) + "\n")
    print(json.dumps({"written": len(rows), "out": a.out, "sources": Counter(r["source"] for r in rows)}))


if __name__ == "__main__":
    main()
