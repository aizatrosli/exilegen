"""Read a chainscore run directory into one record per chain (no engine needed).

    measured(dir) -> {chain id: {"built", "feasible", "unverified", "dps", "ehp", "error",
                                 "base_dps", "gain", "state_lift", "base_state_lift", "payoff", "gain_credited"}}

gain           chain DPS over its baseline's (same skill, ascendancy, weapon and defence without the chain)
typical_dps    median DPS of the plain builds on the same main skill (feasible ones when any are), over typical_n builds
gain_fair      chain DPS over the stronger of its own baseline and typical_dps, so one weak baseline cannot inflate it
supports       support gems the search socketed on the main skill in the chain build; base_supports for the plain build
main_granted   the main skill comes from an item, which the search does not give support gems
thin           true when fewer than 3 plain builds exist for the skill: gain_fair then rests on little evidence
state_lift     chain DPS with its own states switched on over the same build with them off
gain_credited  gain_fair x state_lift: the chain credited for the state it produces (assumes it is always up)
payoff         state_lift over the baseline's: above 1 means the chain's payoff part uses the state better
"""
from __future__ import annotations

import base64
import json
import os
import xml.etree.ElementTree as ET
import zlib


def _read(path: str) -> list[dict]:
    return [json.loads(l) for l in open(path) if l.strip()] if os.path.exists(path) else []


def supports(code: str | None, main_name: str | None) -> tuple[list[str], bool] | None:
    """(support gems on the main skill's group, whether an item grants the skill), read from a PoB build code."""
    if not code or not main_name:
        return None
    try:
        root = ET.fromstring(zlib.decompress(base64.urlsafe_b64decode(code)))
    except Exception:
        return None
    best = None
    for sk in root.iter("Skill"):
        names = [g.get("nameSpec") or "" for g in sk.findall("Gem")]
        if main_name in names and (best is None or len(names) > len(best[0]) + 1):
            best = ([n for n in names if n and n != main_name], bool(sk.get("source")))
    return best


def measured(out: str) -> dict[str, dict]:
    base = {r["base_key"]: r for r in _read(os.path.join(out, "baselines.jsonl"))}
    by_skill: dict[str, list[dict]] = {}
    for b in base.values():
        if (b.get("dps_honest") or 0) > 0:
            by_skill.setdefault(b.get("main_name"), []).append(b)
    typical: dict[str, tuple[float, int]] = {}
    for k, rows in by_skill.items():
        rows = [b for b in rows if b.get("feasible")] or rows
        v = sorted(b["dps_honest"] for b in rows)
        n = len(v)
        typical[k] = ((v[n // 2] if n % 2 else (v[n // 2 - 1] * v[n // 2]) ** 0.5), n)
    st = {r["id"]: r for r in _read(os.path.join(out, "states.jsonl")) if not r.get("error")}
    res: dict[str, dict] = {}
    for r in _read(os.path.join(out, "chain_scores.jsonl")):
        dps = r.get("dps_honest") or 0
        m = {"built": bool(r.get("ok")), "feasible": bool(r.get("feasible")), "unverified": bool(r.get("unverified")),
             "dps": round(dps) if dps else 0, "ehp": round(r["ehp"]) if r.get("ehp") else None}
        if r.get("error"):
            m["error"] = str(r["error"])[:120]
        b = base.get(r.get("base_key"))
        sup = supports(r.get("code"), r.get("main_name"))
        if sup:
            m["supports"], m["main_granted"] = sup
            bsup = supports(b.get("code"), b.get("main_name")) if b else None
            if bsup:
                m["base_supports"] = bsup[0]
        if dps > 0 and b and (b.get("dps_honest") or 0) > 0:
            m["base_dps"] = round(b["dps_honest"])
            m["gain"] = round(dps / b["dps_honest"], 3)
            t, n = typical.get(r.get("main_name"), (b["dps_honest"], 1))
            m["typical_dps"], m["typical_n"] = round(t), n
            m["gain_fair"] = round(dps / max(b["dps_honest"], t), 3)
            if n < 3:
                m["thin"] = True
            x = st.get(r["id"])
            if x and x.get("chain_off"):
                m["state_lift"] = round(x["chain_on"] / x["chain_off"], 3)
                if x.get("base_off") and x.get("base_on"):
                    m["base_state_lift"] = round(x["base_on"] / x["base_off"], 3)
                    m["payoff"] = round(m["state_lift"] / m["base_state_lift"], 3)
            m["gain_credited"] = round(m["gain_fair"] * m.get("state_lift", 1.0), 3)
        res[r["id"]] = m
    return res


def summary(res: dict[str, dict], pool: int) -> dict:
    g = sorted(m["gain_credited"] for m in res.values() if m.get("gain_credited") is not None)
    return {"scored": len(res), "pool": pool, "built": sum(1 for m in res.values() if m["built"]),
            "feasible": sum(1 for m in res.values() if m["feasible"]), "compared": len(g),
            "median_gain": g[len(g) // 2] if g else None, "better_by_10pct": sum(1 for x in g if x >= 1.1),
            "worse_by_10pct": sum(1 for x in g if x <= 0.9)}
