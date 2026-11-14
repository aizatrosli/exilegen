"""Defensive / resource floors, as data.

Numbers come from docs/analysis-meta.md section 4 (1,488 ladder builds, pinnacle_locked profile):
  elemental res: 57% of builds cap all three; p25 = 74-75
  chaos res (non-CI): 99.5% keep >= 0; p25 = 31
  EHP: p25 = 15.8k, p50 = 26.5k          lowest max hit: p25 = 4.36k, p50 = 6.34k (physical is the limiter)
Mana cost: 67% of ladder builds spend more mana/s than PoB's regen (leech, flasks, Remnants are not in
PoB's regen), so absolute sustain is a `target`; the hard rule is "no worse than the baseline".

tier  hard   -> violation (build excluded)
      target -> reported in `below_target`, never excludes
mode  absolute -> compare with `value`
      relative -> compare with the baseline's value (no worse than the starting build, within `tol`)
      either   -> pass if absolute OR relative passes (default: a below-floor baseline may not get worse)
"""
from __future__ import annotations

from dataclasses import dataclass

from .adapter import BuildView

ELEMENTS = ("fire", "cold", "lightning")


def m_res(el):
    def f(bv: BuildView):
        r = bv.num(f"res.{el}", None)
        if r is None:
            return None
        return r - min(75.0, bv.num(f"res_max.{el}", 75.0, record=False))   # 0 = capped at 75 (or at a lower max)
    return f


def m_chaos(bv: BuildView):
    if "Chaos Inoculation" in bv.names("keystones"):
        return float("inf")
    return bv.num("res.chaos", None)


def m_ehp(bv):
    return bv.num("ehp", None)


def m_min_max_hit(bv):
    types = ["physical", "fire", "cold", "lightning"]
    if "Chaos Inoculation" not in bv.names("keystones"):
        types.append("chaos")
    vals = [bv.num(f"max_hit.{t}", None) for t in types]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def m_phys_max_hit(bv):
    return bv.num("max_hit.physical", None)


def m_spirit(bv):
    return bv.num("spirit_unreserved", None)


def _sustain(bv, cost_key, regen_key, leech_key, pool_key, window):
    cost = bv.num(cost_key, None)
    if cost is None:
        return None
    if cost <= 0:
        return float("inf")
    rec = (bv.num(regen_key, 0.0) or 0.0) + (bv.num(leech_key, 0.0, record=False) or 0.0)
    pool = bv.num(pool_key, 0.0, record=False) or 0.0
    return (rec + pool / window) / cost        # >= 1: can keep using the skill for `window` seconds


def m_mana_sustain(bv):
    return _sustain(bv, "mana_cost_per_sec", "mana_regen", "mana_leech", "mana_unreserved", 30.0)


def m_life_sustain(bv):
    return _sustain(bv, "life_cost_per_sec", "life_regen", "life_leech", "life_unreserved", 30.0)


def m_attr_deficit(bv):
    """max(requirement - attribute) over Str/Dex/Int; <= 0 means all requirements met."""
    d = []
    for a in ("str", "dex", "int"):
        req, have = bv.num(f"req_{a}", None), bv.num(a, None)
        if req is None or have is None:
            return None
        d.append(req - have)
    return max(d)


METRICS = {
    "res.fire": m_res("fire"), "res.cold": m_res("cold"), "res.lightning": m_res("lightning"),
    "res.chaos": m_chaos, "ehp": m_ehp, "max_hit.min": m_min_max_hit, "max_hit.physical": m_phys_max_hit,
    "spirit_unreserved": m_spirit, "mana_sustain": m_mana_sustain, "life_sustain": m_life_sustain,
    "attr_deficit": m_attr_deficit,
}


@dataclass
class Floor:
    id: str
    metric: str
    op: str             # ">=" or "<="
    value: float
    tier: str = "hard"  # hard | target
    mode: str = "either"
    tol: float = 0.0    # relative tolerance (fraction for >=, absolute for res)
    note: str = ""


# Default floors. Elemental res: "75 or not reduced"; others as in analysis-meta.md section 4.
DEFAULT_FLOORS: list[Floor] = [
    *[Floor(f"res_{el}", f"res.{el}", ">=", 0.0, "hard", "either", 0.5, "capped (75) or not reduced") for el in ELEMENTS],
    Floor("res_chaos", "res.chaos", ">=", 0.0, "hard", "either", 0.5, "non-CI chaos res >= 0 (99.5% of ladder)"),
    Floor("ehp", "ehp", ">=", 15800.0, "hard", "either", 0.02, "corpus p25"),
    Floor("max_hit_min", "max_hit.min", ">=", 4400.0, "hard", "either", 0.02, "corpus p25 lowest max hit"),
    Floor("spirit", "spirit_unreserved", ">=", 0.0, "hard", "either", 0.0,
          "unreserved Spirit >= min(0, baseline): PoB shows negative unreserved Spirit for 62% of ladder builds"),
    Floor("mana_sustain", "mana_sustain", ">=", 1.0, "hard", "relative", 0.05, "no worse than baseline (67% of ladder fails absolute)"),
    Floor("life_sustain", "life_sustain", ">=", 1.0, "hard", "either", 0.05, "life costs payable for 30 s"),
    Floor("attributes", "attr_deficit", "<=", 0.0, "hard", "absolute", 0.0, "attribute requirements met"),
    # targets (p25/p50): reported, never exclude
    Floor("res_chaos_target", "res.chaos", ">=", 30.0, "target", "absolute", 0.0, "corpus p25"),
    Floor("ehp_target", "ehp", ">=", 26500.0, "target", "absolute", 0.0, "corpus p50"),
    Floor("max_hit_min_target", "max_hit.min", ">=", 6300.0, "target", "absolute", 0.0, "corpus p50"),
    Floor("max_hit_phys_target", "max_hit.physical", ">=", 4600.0, "target", "absolute", 0.0, "corpus p25 physical"),
    Floor("spirit_target", "spirit_unreserved", ">=", 0.0, "target", "absolute", 0.0, "no over-reserved Spirit"),
    Floor("mana_sustain_target", "mana_sustain", ">=", 1.0, "target", "absolute", 0.0, "mana cost payable for 30 s from regen+leech+pool"),
]

# corpus p10 (hard) preset for permissive searches
LENIENT_FLOORS: list[Floor] = [
    *[Floor(f"res_{el}", f"res.{el}", ">=", -20.0, "hard", "either", 0.5, "p10 ~55") for el in ELEMENTS],
    Floor("res_chaos", "res.chaos", ">=", 0.0, "hard", "either", 0.5),
    Floor("ehp", "ehp", ">=", 10000.0, "hard", "either", 0.02, "corpus p10"),
    Floor("max_hit_min", "max_hit.min", ">=", 3300.0, "hard", "either", 0.02, "corpus p10"),
    Floor("spirit", "spirit_unreserved", ">=", 0.0, "hard", "either", 0.0),
    Floor("attributes", "attr_deficit", "<=", 0.0, "hard", "absolute", 0.0),
]


def _cmp(op, a, b):
    return a >= b if op == ">=" else a <= b


def check(floors: list[Floor], bv: BuildView, baseline: BuildView | None = None, mode_override: str | None = None) -> list[dict]:
    """-> one result per floor: {id, tier, passed (True/False/None=unknown), value, threshold, baseline, note}."""
    out = []
    for fl in floors:
        fn = METRICS[fl.metric]
        v = fn(bv)
        b = fn(baseline) if baseline is not None else None
        mode = mode_override or fl.mode
        r = {"id": fl.id, "tier": fl.tier, "metric": fl.metric, "value": v, "threshold": fl.value, "baseline": b,
             "mode": mode, "note": fl.note}
        if v is None:
            r["passed"] = None
            out.append(r)
            continue
        abs_ok = _cmp(fl.op, v, fl.value - (fl.tol if fl.metric.startswith("res") else 0.0))
        rel_ok = None
        if b is not None:
            if fl.metric.startswith("res") or fl.metric == "attr_deficit" or fl.metric == "spirit_unreserved":
                rel_ok = _cmp(fl.op, v, b - fl.tol if fl.op == ">=" else b + fl.tol)
            else:
                rel_ok = _cmp(fl.op, v, b * (1 - fl.tol) if fl.op == ">=" else b * (1 + fl.tol))
        if fl.id == "spirit" and b is None and mode != "absolute":
            ok = True        # PoB's unreserved Spirit is unreliable in absolute terms; spirit_target reports it
            r["note"] = (r["note"] + "; no baseline").strip("; ")
        elif mode == "absolute":
            ok = abs_ok
        elif mode == "relative":
            ok = rel_ok if rel_ok is not None else (abs_ok if fl.tier == "target" else True)
            if rel_ok is None:
                r["note"] = (r["note"] + "; no baseline").strip("; ")
        else:
            ok = abs_ok or bool(rel_ok)
        if fl.id == "spirit" and b is not None and mode != "absolute":
            ok = v >= min(0.0, b) - fl.tol
        r["passed"] = bool(ok)
        out.append(r)
    return out
