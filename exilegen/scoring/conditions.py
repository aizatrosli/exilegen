"""Condition registry: blocks or discounts gems, keystones and uniques whose PoB value assumes a state.

    reg = Registry.load()                       # conditions.yaml (curated) over conditions_auto.yaml (generated)
    hits = reg.evaluate(build_view, "pinnacle_v1", parts)

`parts` are the DPS components the scorer adds up (main skill, each energy-triggered spell). Each hit says
which entry matched, the credit, the action and the effect on each part. See README.md for the rules.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import yaml

from .adapter import BuildView, Gem, Group

HERE = os.path.dirname(__file__)
CURATED = os.path.join(HERE, "conditions.yaml")
AUTO = os.path.join(HERE, "conditions_auto.yaml")
AUTO_MIN_CREDIT = 0.5


@dataclass
class Part:
    """One DPS component: a skill in a socket group."""
    label: str
    group: Group | None
    skill_id: str | None
    skill_name: str | None
    dps: float                  # after energy correction (or PoB raw for non-triggered)
    dps_raw: float              # PoB raw
    factors: dict = field(default_factory=dict)   # entry id -> multiplier applied

    def matches_skill(self, entry: dict) -> bool:
        ids = set(entry.get("match", {}).get("ids") or [])
        names = {n.lower() for n in entry.get("match", {}).get("names") or []}
        return (self.skill_id in ids) or ((self.skill_name or "").lower() in names)


@dataclass
class Hit:
    entry_id: str
    name: str
    action: str             # allow | discount | block | flag | adjust
    credit: float
    reason: str
    verified: bool
    parts: dict = field(default_factory=dict)     # part label -> factor applied
    flags: list[str] = field(default_factory=list)
    utility: dict = field(default_factory=dict)
    on_dps: bool = True     # the gem sits in a group that contributes to the scored DPS

    def as_dict(self):
        return dict(self.__dict__)


def _credit_value(v, signal_value, unknown):
    """Resolve an availability spec to (credit, used_unknown)."""
    if isinstance(v, bool):
        return (1.0 if v else 0.0), False
    if isinstance(v, (int, float)):
        return float(v), False
    if isinstance(v, str):
        if v == "build" or v.startswith("build_or:"):
            if signal_value is None:
                return float(unknown), True
            if isinstance(signal_value, bool):
                if signal_value:
                    return 1.0, False
                return (float(v.split(":", 1)[1]) if ":" in v else 0.0), False
            return max(0.0, min(1.0, float(signal_value))), False
        try:
            return float(v), False
        except ValueError:
            pass
    return 1.0, False


class Signals:
    """Build facts the registry conditions on. Each returns bool / fraction / None (unknown)."""

    CURSE_UNIQUES = {"Rondel de Ezo"}

    def __init__(self, bv: BuildView):
        self.bv = bv
        self._cache: dict = {}

    def get(self, name: str):
        if name not in self._cache:
            fn = getattr(self, "s_" + name, None)
            self._cache[name] = fn() if fn else None
        return self._cache[name]

    def _gems(self):
        return self.bv.skill_gems() if self.bv.groups() else None

    def _all_gems(self):
        return [gem for g in self.bv.groups() if g.enabled for gem in g.gems if gem.enabled]

    def s_applies_curse(self):
        gems = self._gems()
        if gems is None:
            return None
        for _, gem in gems:
            if any(t in gem.types for t in ("AppliesCurse", "Curse", "Hex")) or gem.id == "MetaCastCurseOnBlockPlayer":
                return True
        if self.CURSE_UNIQUES & set(self.bv.names("uniques")):
            return True
        return False

    def s_has_minions(self):
        if self.bv.num("dps.minion", None, record=False):
            return True
        gems = self._gems()
        if gems is None:
            return None
        return any("minion" in gem.proxy_kind or "CreatesMinion" in gem.types or "Minion" in gem.types for _, gem in gems)

    def s_creates_corpses(self):
        gems = self._gems()
        if gems is None:
            return None
        sacrifice = any(gem.id == "SacrificePlayer" or gem.name == "Sacrifice" for _, gem in gems)
        return bool(sacrifice and self.get("has_minions"))

    def s_spends_life(self):
        lc = self.bv.num("life_cost_per_sec", None, record=False)
        if lc is not None and lc > 0:
            return True
        if "Blood Magic" in self.bv.names("keystones"):
            return True
        if self.bv.groups() and any(g.name == "Atalui's Bloodletting" for g in self._all_gems()):
            return True
        if lc is None and not self.bv.groups():
            return None
        return False

    def s_is_ci(self):
        ks = self.bv.raw("keystones")
        if ks is None:
            return None
        return "Chaos Inoculation" in self.bv.names("keystones")

    def s_low_life(self):
        life, unres = self.bv.num("life", None, False), self.bv.num("life_unreserved", None, False)
        if not life or unres is None:
            return None
        return unres / life <= 0.35

    def s_block_chance(self):
        b = self.bv.num("block_chance", None, False)
        return None if b is None else min(1.0, b / 100.0)

    def _chance(self, key):
        v = self.bv.num(key, None, False)
        return None if v is None else min(1.0, v / 100.0)

    def s_applies_shock(self): return self._chance("shock_chance")
    def s_applies_ignite(self): return self._chance("ignite_chance")
    def s_applies_freeze(self): return self._chance("freeze_chance")
    def s_applies_chill(self): return self._chance("chill_chance")
    def s_applies_poison(self): return self._chance("poison_chance")
    def s_applies_bleed(self): return self._chance("bleed_chance")

    def s_has_parry(self):
        gems = self._gems()
        return None if gems is None else any(gem.name == "Parry" for _, gem in gems)

    def s_applies_mark(self):
        gems = self._gems()
        return None if gems is None else any("Mark" in gem.types for _, gem in gems)


class Registry:
    def __init__(self, doc: dict, auto: dict | None = None):
        self.conditions = doc.get("conditions", {}) or {}
        self.overrides: dict[str, dict] = {}     # profile -> {condition: availability}, from profiles.json
        self.drawbacks = doc.get("drawbacks", {}) or {}
        curated = [dict(e, curated=True) for e in doc.get("entries", []) or []]
        cur_keys = set()
        for e in curated:
            cur_keys.add(e["id"])
            cur_keys.update(e.get("match", {}).get("ids") or [])
            cur_keys.update(n.lower() for n in e.get("match", {}).get("names") or [])
        merged = list(curated)
        for e in (auto or {}).get("entries", []) or []:
            keys = {e["id"]} | set(e.get("match", {}).get("ids") or []) | {n.lower() for n in e.get("match", {}).get("names") or []}
            if keys & cur_keys:
                continue
            merged.append(dict(e, curated=False))
        self.entries = merged
        self.by_id: dict[str, list[dict]] = {}
        self.by_name: dict[str, list[dict]] = {}
        for e in merged:
            for i in e.get("match", {}).get("ids") or []:
                self.by_id.setdefault(i, []).append(e)
            for n in e.get("match", {}).get("names") or []:
                self.by_name.setdefault(n.lower(), []).append(e)
            if e.get("name"):
                self.by_name.setdefault(e["name"].lower(), [])
                if e not in self.by_name[e["name"].lower()]:
                    self.by_name[e["name"].lower()].append(e)

    @classmethod
    def load(cls, curated: str = CURATED, auto: str | None = AUTO) -> "Registry":
        with open(curated) as f:
            doc = yaml.safe_load(f) or {}
        ad = None
        if auto and os.path.exists(auto):
            with open(auto) as f:
                ad = yaml.safe_load(f)
        return cls(doc, ad)

    def __len__(self):
        return len(self.entries)

    def stats(self) -> dict:
        out = {"total": len(self.entries), "curated": sum(e["curated"] for e in self.entries),
               "verified": sum(bool(e.get("verified")) for e in self.entries)}
        for e in self.entries:
            k = f"{e.get('kind')}:{e.get('risk', 'curated') if not e['curated'] else 'curated'}"
            out[k] = out.get(k, 0) + 1
        return out

    def lookup(self, gem_id: str | None = None, name: str | None = None) -> list[dict]:
        out = []
        for e in (self.by_id.get(gem_id, []) if gem_id else []) + (self.by_name.get((name or "").lower(), []) if name else []):
            if e not in out:
                out.append(e)
        return out

    # ------------------------------------------------------------------ credit
    def _avail(self, table: dict, key: str, profile: str, signals: Signals, flags: list[str]) -> float:
        spec = table.get(key)
        ov = self.overrides.get(profile, {}).get(key)
        if spec is not None and ov is not None:
            spec = dict(spec, **{profile: ov})
        if spec is None:
            flags.append(f"unverified:unknown_condition:{key}")
            return 1.0
        v = spec.get(profile, spec.get("default", 1.0))
        sig = spec.get("signal")
        sv = signals.get(sig) if sig else None
        c, unk = _credit_value(v, sv, spec.get("unknown", 0.5))
        if unk:
            flags.append(f"unverified:signal_unknown:{sig}")
        return c

    def entry_credit(self, e: dict, profile: str, signals: Signals) -> tuple[str, float, str, list[str]]:
        """-> (action, credit, reason, flags) before custom checks / part application."""
        flags: list[str] = []
        if not e["curated"] and e.get("risk") != "high":
            # medium/low auto-scan entries are informational: they never change a number
            return "flag", 1.0, "auto-scan " + ",".join((e.get("conditions") or []) + (e.get("drawbacks") or [])), flags
        tr = (e.get("treatment") or {})
        explicit = tr.get(profile, tr.get("default"))
        # requirements first
        for req in e.get("requires") or []:
            sv = signals.get(req)
            if sv is None:
                flags.append(f"unverified:signal_unknown:{req}")
                continue
            if not sv:
                un = e.get("if_unmet") or {"action": "block"}
                if profile == "as_exported" and explicit and explicit.get("action") == "allow":
                    break
                return un.get("action", "block"), float(un.get("credit", 0.0)), un.get("reason", f"requires {req}"), flags
        if explicit:
            a = explicit.get("action", "discount")
            c = float(explicit.get("credit", 1.0 if a in ("allow", "flag", "adjust") else 0.0))
            return a, c, f"explicit treatment for {profile}", flags
        credit = 1.0
        reasons = []
        for c in e.get("conditions") or []:
            x = self._avail(self.conditions, c, profile, signals, flags)
            if x < 1.0:
                reasons.append(f"{c}={x:g}")
            credit *= x
        for d in e.get("drawbacks") or []:
            x = self._avail(self.drawbacks, d, profile, signals, flags)
            if x < 1.0:
                reasons.append(f"{d}={x:g}")
            credit *= x
        if not e["curated"]:
            # unreviewed auto-scan entries never block: at most discount to AUTO_MIN_CREDIT, always unverified
            if credit >= 0.999:
                return "allow", 1.0, ",".join(reasons) or "conditions hold", flags
            return "discount", max(credit, AUTO_MIN_CREDIT), "auto-scan " + ",".join(reasons), flags
        if credit >= 0.999:
            return ("flag" if e.get("drawbacks") else "allow"), 1.0, ",".join(reasons) or "conditions hold", flags
        if credit <= 1e-9:
            on_never = e.get("on_never") or ("block" if e.get("role") == "skill" else "discount")
            return on_never, 0.0, ",".join(reasons) + " (never holds)", flags
        return "discount", credit, ",".join(reasons), flags

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, bv: BuildView, profile: str, parts: list[Part], include_low: bool = False) -> list[Hit]:
        signals = Signals(bv)
        hits: list[Hit] = []
        seen: set[tuple] = set()
        groups = bv.groups()
        dps_groups = {id(p.group) for p in parts if p.group is not None}
        # gems: actives + supports of enabled groups
        for g in groups:
            if not g.enabled:
                continue
            for gem in g.gems:
                if not gem.enabled:
                    continue
                for e in self.lookup(gem.id, gem.name):
                    if not include_low and not e["curated"] and e.get("risk") == "low":
                        continue
                    key = (e["id"], g.index)
                    if key in seen:
                        continue
                    seen.add(key)
                    on_dps = id(g) in dps_groups
                    if not on_dps and not e["curated"]:
                        continue
                    hits.append(self._apply(e, bv, profile, signals, g, gem, [p for p in parts if p.group is g], on_dps))
        # keystones / uniques: global
        for kind, key in (("keystone", "keystones"), ("unique", "uniques")):
            for nm in bv.names(key) if bv.has(key) else []:
                for e in self.lookup(None, nm):
                    if e.get("kind") != kind or (not include_low and not e["curated"] and e.get("risk") == "low"):
                        continue
                    if (e["id"], None) in seen:
                        continue
                    seen.add((e["id"], None))
                    hits.append(self._apply(e, bv, profile, signals, None, None, parts, True))
        return hits

    def _apply(self, e, bv, profile, signals, group, gem, parts, on_dps) -> Hit:
        action, credit, reason, flags = self.entry_credit(e, profile, signals)
        check = e.get("check")
        dps_mult = None
        whole_skill = e.get("role") == "skill"
        if check and not (action == "block" and e.get("requires")):
            r = CHECKS[check](e, bv, profile, signals, group, parts)
            if r:
                action = r.get("action", action)
                credit = r.get("credit", credit)
                reason = r.get("reason", reason)
                flags += r.get("flags", [])
                dps_mult = r.get("dps_multiplier")
                whole_skill = r.get("whole_skill", whole_skill)
        explicit = (e.get("treatment") or {}).get(profile)
        if explicit and explicit.get("action") in ("allow", "flag") and not e.get("requires"):
            action, credit, dps_mult = explicit["action"], 1.0, None
        hit = Hit(e["id"], e.get("name") or e["id"], action, credit, reason, bool(e.get("verified")), flags=flags,
                  utility=(e.get("utility") or {}).get(profile, {}))
        hit.on_dps = on_dps
        if not on_dps:
            hit.utility = {}
        if not on_dps or action in ("allow", "flag"):
            # informational: signal-unknown flags do not matter when nothing changes
            hit.flags = [f for f in hit.flags if not f.startswith("unverified:signal_unknown")]
            return hit
        if not e.get("verified"):
            hit.flags.append(f"unverified:registry_entry:{e['id']}")
        for p in parts:
            if action == "adjust" and dps_mult is not None:
                f = dps_mult
            elif action == "block":
                f = 0.0 if (whole_skill and (gem is None or p.matches_skill(e) or e.get("kind") in ("support", "rule"))) else None
                if f is None:
                    continue
            elif whole_skill and (gem is None or gem.support or p.matches_skill(e)):
                f = credit
            elif e.get("role") == "skill" and not p.matches_skill(e):
                continue
            else:
                share = float(e.get("typical_share", 0.25 if e.get("role") != "global" else 0.2))
                m = bv.marginal(group.index if group else None, gem) if gem is not None else None
                if m is not None and p.dps_raw > 0:
                    f = (m + credit * (p.dps_raw - m)) / p.dps_raw
                else:
                    f = 1.0 - (1.0 - credit) * share
                    hit.flags.append(f"no_marginal:{e['id']}")
            p.factors[e["id"]] = p.factors.get(e["id"], 1.0) * f
            hit.parts[p.label] = f
        return hit


# ---------------------------------------------------------------------------- custom checks
def _uses_per_sec(bv: BuildView):
    return bv.num("speed", None, False)


def check_atziri_life_loss(e, bv, profile, signals, group, parts):
    if profile == "as_exported":
        return {"action": "flag", "credit": 1.0}
    frac = float((e.get("params") or {}).get("loss_fraction", 0.25))
    pool = (bv.num("life", 0, False) or 0) + (bv.num("es", 0, False) or 0)
    uses = _uses_per_sec(bv)
    rec = bv.num("life_regen", None, False)
    leech = bv.num("life_leech", 0, False) or 0
    if not pool or uses is None or rec is None:
        return {"flags": ["unverified:atziri_inputs_missing"]}
    loss = frac * pool * uses
    if loss > rec + leech:
        return {"action": "block", "credit": 0.0, "whole_skill": True,
                "reason": f"loses {loss:.0f} life/s vs {rec + leech:.0f} recovery"}
    return {"action": "flag", "credit": 1.0, "reason": f"life loss {loss:.0f}/s sustainable"}


def check_life_cost_sustain(e, bv, profile, signals, group, parts):
    if profile == "as_exported":
        return {"action": "flag", "credit": 1.0}
    cost = bv.num("life_cost_per_sec", None, False)
    if cost is None:
        return {"action": "flag", "credit": 1.0, "flags": ["unverified:life_cost_per_sec_missing"]}
    rec = (bv.num("life_regen", 0, False) or 0) + (bv.num("life_leech", 0, False) or 0)
    pool = bv.num("life_unreserved", None, False) or bv.num("life", 0, False) or 0
    window = 60.0 if profile.startswith("pinnacle") else 20.0
    if cost <= rec + pool / window:
        return {"action": "flag", "credit": 1.0, "reason": "life cost sustainable"}
    usable = (rec + pool / window) / cost
    return {"action": "discount", "credit": usable, "whole_skill": True,
            "reason": f"life cost {cost:.0f}/s vs {rec:.0f}/s recovery: usable {usable:.0%} of the time"}


def check_zerphi_glory(e, bv, profile, signals, group, parts):
    if profile == "as_exported":
        return {"action": "allow", "credit": 1.0}
    p = e.get("params") or {}
    need, pct = float(p.get("glory_required", 50)), float(p.get("glory_per_life_pct", 5))
    cost = bv.num("life_cost_per_sec", None, False)
    uses = _uses_per_sec(bv)
    if cost is None or uses is None:
        sp = signals.get("spends_life")
        if sp is False:
            return {"action": "discount", "credit": 0.0, "whole_skill": True, "reason": "no life spending: no Glory"}
        return {"action": "discount", "credit": 0.3, "whole_skill": True, "flags": ["unverified:glory_inputs_missing"],
                "reason": "Glory rate unknown"}
    glory = cost * pct / 100.0
    frac = min(1.0, glory / (need * uses)) if uses > 0 else 1.0
    return {"action": "discount" if frac < 1 else "allow", "credit": frac, "whole_skill": True,
            "reason": f"Glory {glory:.1f}/s supports {frac:.0%} of {uses:.2f} uses/s"}


def check_uhtred_one_other_support(e, bv, profile, signals, group, parts):
    if group is None:
        return None
    others = [s for s in group.supports if s.name != "Uhtred's Omen"]
    if len(others) == 1:
        return {"action": "allow", "credit": 1.0, "reason": "exactly one other support"}
    return {"action": "discount", "credit": 0.0,
            "reason": f"{len(others)} other supports in the group (PoB counts only those applied to the skill)"}


def check_spell_cascade_group(e, bv, profile, signals, group, parts):
    if profile == "as_exported":
        return {"action": "allow", "credit": 1.0}
    p = e.get("params") or {}
    m = 1.0 + float(p.get("pinnacle_overlap_hits", 0.43))
    return {"action": "adjust", "credit": 1.0, "dps_multiplier": m,
            "reason": f"PoB ignores the extra cascades; x{m:.2f} on every spell in the group (net neutral vs no cascade)"}


def check_ailment_payoff(e, bv, profile, signals, group, parts):
    if profile == "as_exported":
        return {"action": "allow", "credit": 1.0}
    vals = [signals.get("applies_" + a) for a in (e.get("params") or {}).get("ailments", [])]
    known = [v for v in vals if v is not None]
    if not known:
        return {"action": "discount", "credit": 0.3, "whole_skill": True, "flags": ["unverified:ailment_chance_missing"],
                "reason": "ailment chances unknown"}
    c = max(known)
    if c >= 0.999:
        return {"action": "allow", "credit": 1.0}
    return {"action": "discount" if c > 0 else "block", "credit": c, "whole_skill": True,
            "reason": f"best ailment chance {c:.0%}"}


CHECKS = {
    "atziri_life_loss": check_atziri_life_loss,
    "life_cost_sustain": check_life_cost_sustain,
    "zerphi_glory": check_zerphi_glory,
    "uhtred_one_other_support": check_uhtred_one_other_support,
    "spell_cascade_group": check_spell_cascade_group,
    "ailment_payoff": check_ailment_payoff,
}


def combined_factor(p: Part) -> float:
    return math.prod(p.factors.values()) if p.factors else 1.0
