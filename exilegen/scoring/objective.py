"""Scorer: honest DPS + defence/utility objective, feasibility, flags, multi-profile min, Pareto helpers.

    s = Scorer(profile=["pinnacle_v1", "mapping_v1"], constraints=["one_button"])
    r = s.score({"pinnacle_v1": stats_p, "mapping_v1": stats_m}, skills, baseline={"pinnacle_v1": base_p, ...})

score() returns
    feasible, violations[], objective, dps_raw, dps_honest, ehp,
    factors {energy, conditions{entry: factor}}, flags[], verified, below_target[], explain, profiles{name: ...}
With several profiles the top-level numbers are those of the profile with the lowest objective
(score = min over profiles) and feasibility requires every profile to pass.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field

from . import constraints as C
from . import energy as E
from . import floors as F
from .adapter import BuildView
from .conditions import Part, Registry, combined_factor
from .profiles import Profiles

DEFAULT_WEIGHTS = {
    "ehp": 0.3,          # log(EHP), as the tree-solver POC
    "max_hit": 0.1,      # log(lowest max hit)
    "movement": 0.5,     # log(movement speed multiplier): +10% ms ~ +5% DPS
    "sustain": 0.2,      # log(min(1, cost sustain ratio))
    "ailment": 0.05,     # log(ailment threshold / (life+ES))
    "aoe": {"mapping_v1": 0.3},   # log(area multiplier from registry utility adjustments)
}
_CREDIT = None
_TIER = re.compile(r"\s+(?:I|II|III|IV|V)$", re.I)


def support_credit() -> dict:
    """support_credit.json: clear-speed and conditional credit of support gems (see its notes)."""
    global _CREDIT
    if _CREDIT is None:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "support_credit.json"), encoding="utf-8") as f:
            _CREDIT = json.load(f)
    return _CREDIT


def credited_family(name: str) -> bool:
    """Does the support earn clear or conditional credit? (the support search screens these first)"""
    data = support_credit()
    fam = _TIER.sub("", name or "").strip().lower()
    return fam in data["clear"] or fam in data["conditional"]


def main_support_credit(bv, profile: str) -> tuple[float, float, list[str]]:
    """(clear multiplier, conditional damage multiplier, the supports that earned either) for the main group."""
    data = support_credit()
    g = bv.main_group()
    clear, cond, names = 1.0, 1.0, []
    for gem in (g.gems if g else []):
        if not gem.support or not gem.enabled:
            continue
        fam = _TIER.sub("", gem.name or "").strip().lower()
        m = _TIER.search(gem.name or "")
        tier = m.group(0).strip().upper() if m else "I"
        if fam in data["clear"]:
            v = data["clear"][fam]
            clear *= float(v.get(tier, v.get("default", 1.0)) if isinstance(v, dict) else v)
            names.append(gem.name)
        c = data["conditional"].get(fam)
        if c:
            more = c["more"].get(tier, c["more"].get("default", 0.0)) if isinstance(c["more"], dict) else c["more"]
            share = c["share"].get(profile, c["share"].get("default", 0.0)) if isinstance(c["share"], dict) else c["share"]
            cond *= 1.0 + float(more) * float(share)
            names.append(gem.name)
    return min(clear, float(data.get("clear_cap", 3.0))), cond, names


KNOWN_PROFILES = {"pinnacle_v1", "mapping_v1", "as_exported", "pinnacle_locked"}


def _w(weights, key, profile):
    v = weights.get(key, 0.0)
    if isinstance(v, dict):
        return float(v.get(profile, v.get("default", 0.0)))
    return float(v)


def _safe_log(x, lo=1e-9):
    return math.log(max(lo, x))


@dataclass
class Scorer:
    profile: str | list[str] = "pinnacle_v1"
    floors: list | None = None
    constraints: list | None = None
    weights: dict | None = None
    registry: Registry | None = None
    profiles: Profiles | None = None
    floor_mode: str | None = None            # override every floor's mode (absolute|relative|either)
    unmodelled_energy_factor: float = 1.0
    reject_unverified: bool = False
    _cache: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.floors = F.DEFAULT_FLOORS if self.floors is None else self.floors
        self.weights = {**DEFAULT_WEIGHTS, **(self.weights or {})}
        self.registry = self.registry or Registry.load()
        self.profiles = self.profiles or Profiles.load()
        self.profile_list = [self.profile] if isinstance(self.profile, str) else list(self.profile)

    # ------------------------------------------------------------------ public
    def score(self, stats, skills=None, baseline=None) -> dict:
        docs = self._split(stats)
        bases = self._split(baseline) if baseline is not None else {}
        per = {}
        for prof, doc in docs.items():
            sk = skills.get(prof) if isinstance(skills, dict) else skills
            per[prof] = self.score_one(doc, prof, sk, bases.get(prof))
        if len(per) == 1:
            return next(iter(per.values()))
        worst = min(per.values(), key=lambda r: r["objective"])
        out = dict(worst)
        out["feasible"] = all(r["feasible"] for r in per.values())
        out["violations"] = [dict(v, profile=p) for p, r in per.items() for v in r["violations"]]
        out["flags"] = sorted({f"{p}:{f}" for p, r in per.items() for f in r["flags"]})
        out["verified"] = all(r["verified"] for r in per.values())
        out["profile"] = "min(" + ",".join(per) + ")"
        out["binding_profile"] = worst["profile"]
        out["profiles"] = per
        out["explain"] = "\n".join(f"[{p}] {r['explain']}" for p, r in per.items()) + \
            f"\nscore = min over profiles = {worst['objective']:.3f} ({worst['profile']})"
        return out

    def _split(self, stats) -> dict:
        if isinstance(stats, BuildView):
            return {stats.profile or self.profile_list[0]: stats}
        if isinstance(stats, dict) and stats and all(k in KNOWN_PROFILES or k in self.profile_list for k in stats) \
                and all(isinstance(v, (dict, BuildView)) for v in stats.values()):
            return {k: v for k, v in stats.items() if k in self.profile_list or len(self.profile_list) == 1 and len(stats) == 1}
        return {self.profile_list[0]: stats}

    # ------------------------------------------------------------------ one profile
    def score_one(self, doc, profile: str, skills=None, baseline=None) -> dict:
        bv = doc if isinstance(doc, BuildView) else BuildView(doc, skills, profile)
        bv.profile = profile
        bbv = None
        if baseline is not None:
            bbv = baseline if isinstance(baseline, BuildView) else BuildView(baseline, None, profile)
        flags: list[str] = []
        violations: list[dict] = []
        reg_profile = self.profiles.registry_profile(profile)

        # 1. energy
        power, defaulted = self.profiles.param(profile, "enemy_power")
        egroups = bv.energy_groups()
        if egroups and defaulted:
            flags.append(f"profile_default:enemy_power={power}")
        block = bv.num("block_chance", None, False)
        main = {"hits_per_sec": bv.num("hits_per_sec", None, False),
                "block": None if block is None else (block / 100.0 if block > 1 else block)}
        eres = E.correct(egroups, float(power or 20), self.unmodelled_energy_factor, main=main)
        flags += eres.flags

        # 2. DPS parts
        parts, pflags = self._parts(bv, eres)
        flags += pflags

        # 3. conditions
        self.registry.overrides[reg_profile] = self.profiles.condition_overrides(profile)
        hits = self.registry.evaluate(bv, reg_profile, parts)
        utility = {"movement_speed_mult": 1.0, "aoe_mult": 1.0}
        cond_factors = {}
        for h in hits:
            flags += h.flags
            if h.action == "block" and not h.on_dps:
                flags.append(f"condition_off_dps:{h.entry_id} ({h.reason})")
            elif h.action == "block":
                violations.append({"rule": "condition", "entry": h.entry_id, "detail": f"{h.name}: {h.reason}"})
            elif h.action in ("discount", "adjust") and h.parts:
                cond_factors[h.entry_id] = h.parts
            elif h.action == "flag":
                flags.append(f"condition_flag:{h.entry_id}" + (f" ({h.reason})" if h.reason else ""))
            for k, v in (h.utility or {}).items():
                utility[k] = utility.get(k, 1.0) * float(v)

        dps_raw = sum(p.dps_raw for p in parts)
        dps_energy = sum(p.dps for p in parts)
        dps_honest = sum(p.dps * combined_factor(p) for p in parts)

        # 4. floors
        fres = F.check(self.floors, bv, bbv, self.floor_mode)
        below = []
        for r in fres:
            if r["passed"] is None:
                if r["tier"] == "hard":
                    flags.append(f"unverified:floor_unknown:{r['id']}")
            elif not r["passed"]:
                if r["tier"] == "hard":
                    violations.append({"rule": "floor", "entry": r["id"], "shortfall": r.get("shortfall", 1.0), "detail":
                                       f"{r['metric']}={_fmt(r['value'])} vs {r['threshold']}" +
                                       (f" (baseline {_fmt(r['baseline'])})" if r["baseline"] is not None else "")})
                else:
                    below.append(r["id"])

        # 5. constraints
        cv, cflags = C.evaluate(self.constraints, bv)
        violations += [{"rule": "constraint", "entry": v["constraint"], "detail": v["detail"]} for v in cv]
        flags += cflags

        # 6. unsupported lines
        nun = bv.num("unsupported_lines", None, record=False)
        if nun:
            flags.append(f"unsupported_lines:{int(nun)}")
        msu = bv.raw("main_skill_unsupported")
        if msu:
            flags.append(f"unverified:main_skill_unsupported_lines:{msu if isinstance(msu, (int, float)) else len(msu)}")
        if dps_raw > 0 and eres.dps_raw > 0.5 * dps_raw and not eres.spells:
            flags.append("unverified:triggered_dps_without_energy")

        # 7. objective
        ehp = bv.num("ehp", None)
        terms = {"dps": math.log(dps_honest + 1.0)}
        if ehp is not None:
            terms["ehp"] = _w(self.weights, "ehp", profile) * math.log(ehp + 1.0)
        mh = F.m_min_max_hit(bv)
        if mh is not None and mh != float("inf"):
            terms["max_hit"] = _w(self.weights, "max_hit", profile) * math.log(mh + 1.0)
        ms = bv.num("movement_speed", None, record=False)
        if ms is not None:
            terms["movement"] = _w(self.weights, "movement", profile) * _safe_log(ms * utility.get("movement_speed_mult", 1.0), 0.1)
        sus = [x for x in (F.m_mana_sustain(bv), F.m_life_sustain(bv)) if x is not None]
        if sus:
            terms["sustain"] = _w(self.weights, "sustain", profile) * _safe_log(min(1.0, min(sus)), 0.05)
        thr = bv.num("ailment_threshold", None, record=False)
        pool = (bv.num("life", 0, False) or 0) + (bv.num("es", 0, False) or 0)
        if thr is not None and pool > 0:
            terms["ailment"] = _w(self.weights, "ailment", profile) * math.log(max(0.1, min(10.0, thr / pool)))
        if utility.get("aoe_mult", 1.0) != 1.0:
            terms["aoe"] = _w(self.weights, "aoe", profile) * _safe_log(utility["aoe_mult"], 0.1)
        clear, cond, credited = main_support_credit(bv, profile)
        if clear != 1.0:
            terms["clear"] = float(support_credit().get("clear_weight", 0.3)) * _safe_log(clear, 0.1)
            utility["clear_mult"] = clear
        if cond != 1.0 and dps_honest > 0:
            terms["conditional"] = math.log(cond)
            utility["conditional_mult"] = cond
        objective = sum(terms.values())

        # 8. flags + verdict
        imp = sorted(k for k in bv.missing if k not in ("hits_per_sec",))
        if imp:
            flags.append("unverified:missing_fields:" + ",".join(imp))
        flags = list(dict.fromkeys(flags))
        verified = not any(f.startswith("unverified") for f in flags)
        feasible = not violations and not (self.reject_unverified and not verified)
        res = {
            "profile": profile, "feasible": feasible, "violations": violations, "objective": objective,
            "dps_raw": dps_raw, "dps_energy": dps_energy, "dps_honest": dps_honest, "ehp": ehp,
            "factors": {"energy": eres.factor if eres.dps_raw > 0 else 1.0,
                        "energy_half_ratio": eres.sensitivity if eres.dps_raw > 0 else 1.0,
                        "conditions": cond_factors,
                        "honest_over_raw": dps_honest / dps_raw if dps_raw > 0 else 1.0},
            "terms": terms, "utility": utility, "below_target": below, "flags": flags, "verified": verified,
            "parts": [{"label": p.label, "dps_raw": p.dps_raw, "dps_energy": p.dps, "factors": p.factors,
                       "dps_honest": p.dps * combined_factor(p)} for p in parts],
            "energy": eres.as_dict() if eres.spells else None,
            "conditions": [h.as_dict() for h in hits],
            "floors": fres,
            "enemy_power": power,
        }
        res["explain"] = _explain(res)
        return res

    def _parts(self, bv: BuildView, eres: E.EnergyResult):
        flags = []
        parts: list[Part] = []
        groups = {g.index: g for g in bv.groups()}
        mg = bv.main_group()
        ms = bv.main_skill()
        egi = {s.group for s in eres.spells}
        main_dps = bv.main_dps()
        if main_dps is not None and not (mg is not None and mg.index in egi):
            if mg is not None and mg.meta and not eres.spells:
                flags.append("unverified:triggered_main_skill_without_energy_facts")
            parts.append(Part(f"main:{ms.get('name')}", mg, ms.get("id"), ms.get("name"), main_dps, main_dps))
        for s in eres.spells:
            g = groups.get(s.group)
            sid = s.id
            if sid is None and g is not None:
                for gem in g.actives:
                    if gem.name == s.name:
                        sid = gem.id
            p = Part(f"trig:{s.group}:{s.name}", g, sid, s.name, s.corrected, s.pob_dps)
            parts.append(p)
        return parts, flags


def _fmt(v):
    if v is None:
        return "?"
    if isinstance(v, float) and (v == float("inf")):
        return "inf"
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.2f}"


def _explain(r: dict) -> str:
    lines = [f"{r['profile']}: {'FEASIBLE' if r['feasible'] else 'INFEASIBLE'}"
             f"{'' if r['verified'] else ' (unverified)'}; objective {r['objective']:.3f}",
             f"DPS raw {_fmt(r['dps_raw'])} -> energy {_fmt(r['dps_energy'])} -> honest {_fmt(r['dps_honest'])}"
             f" (x{r['factors']['honest_over_raw']:.2f}); EHP {_fmt(r['ehp'])}"]
    if r["energy"]:
        for s in r["energy"]["spells"]:
            lines.append(f"  energy {s['name']} ({s['meta']}): factor {s['factor']:.2f}"
                         f" [half energy {s['factor_half_energy']:.2f}] PoB rate {s['pob_rate']}/s vs energy rate"
                         f" {s['energy_rate'] if s['energy_rate'] is None else round(s['energy_rate'], 3)}/s")
    flagged = {}
    for h in r["conditions"]:
        if h["action"] == "flag" or not h["on_dps"]:
            flagged.setdefault(h["name"], h["reason"] if h["on_dps"] else "not on a scored group")
        elif h["action"] != "allow":
            lines.append(f"  condition {h['name']}: {h['action']} credit {h['credit']:.2f} - {h['reason']}"
                         + (f" -> {', '.join(f'{k} x{v:.2f}' for k, v in h['parts'].items())}" if h["parts"] else ""))
    if flagged:
        lines.append("  flagged (no DPS change): " + "; ".join(f"{k} ({v})" for k, v in flagged.items()))
    for v in r["violations"]:
        lines.append(f"  VIOLATION {v['rule']} {v['entry']}: {v['detail']}")
    if r["below_target"]:
        lines.append("  below target: " + ", ".join(r["below_target"]))
    lines.append("  terms: " + ", ".join(f"{k} {v:.3f}" for k, v in r["terms"].items()))
    return "\n".join(lines)


# ------------------------------------------------------------------ Pareto helpers
def dominates(a: dict, b: dict, keys=("dps_honest", "ehp")) -> bool:
    """a dominates b: >= on every key (maximised) and > on at least one. Missing values count as -inf."""
    ge, gt = True, False
    for k in keys:
        x, y = a.get(k), b.get(k)
        x = float("-inf") if x is None else x
        y = float("-inf") if y is None else y
        if x < y:
            ge = False
            break
        if x > y:
            gt = True
    return ge and gt


def front(results: list[dict], keys=("dps_honest", "ehp"), feasible_only: bool = True) -> list[dict]:
    """Non-dominated subset (stable order)."""
    pool = [r for r in results if r.get("feasible", True) or not feasible_only]
    return [r for r in pool if not any(dominates(o, r, keys) for o in pool if o is not r)]
