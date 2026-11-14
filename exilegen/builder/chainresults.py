"""Read a chainscore run directory into one record per chain (no engine needed).

    measured(dir) -> {chain id: {"built", "feasible", "unverified", "dps", "ehp", "error",
                                 "base_dps", "gain", "state_lift", "base_state_lift", "payoff", "gain_credited"}}

gain           chain DPS over its baseline's (same skill, ascendancy, weapon and defence without the chain)
state_lift     chain DPS with its own states switched on over the same build with them off
gain_credited  gain x state_lift: the chain credited for the state it produces (assumes it is always up)
payoff         state_lift over the baseline's: above 1 means the chain's payoff part uses the state better
"""
from __future__ import annotations

import json
import os


def _read(path: str) -> list[dict]:
    return [json.loads(l) for l in open(path) if l.strip()] if os.path.exists(path) else []


def measured(out: str) -> dict[str, dict]:
    base = {r["base_key"]: r for r in _read(os.path.join(out, "baselines.jsonl"))}
    st = {r["id"]: r for r in _read(os.path.join(out, "states.jsonl")) if not r.get("error")}
    res: dict[str, dict] = {}
    for r in _read(os.path.join(out, "chain_scores.jsonl")):
        dps = r.get("dps_honest") or 0
        m = {"built": bool(r.get("ok")), "feasible": bool(r.get("feasible")), "unverified": bool(r.get("unverified")),
             "dps": round(dps) if dps else 0, "ehp": round(r["ehp"]) if r.get("ehp") else None}
        if r.get("error"):
            m["error"] = str(r["error"])[:120]
        b = base.get(r.get("base_key"))
        if dps > 0 and b and (b.get("dps_honest") or 0) > 0:
            m["base_dps"] = round(b["dps_honest"])
            m["gain"] = round(dps / b["dps_honest"], 3)
            x = st.get(r["id"])
            if x and x.get("chain_off"):
                m["state_lift"] = round(x["chain_on"] / x["chain_off"], 3)
                if x.get("base_off") and x.get("base_on"):
                    m["base_state_lift"] = round(x["base_on"] / x["base_off"], 3)
                    m["payoff"] = round(m["state_lift"] / m["base_state_lift"], 3)
            m["gain_credited"] = round(m["gain"] * m.get("state_lift", 1.0), 3)
        res[r["id"]] = m
    return res


def summary(res: dict[str, dict], pool: int) -> dict:
    g = sorted(m["gain_credited"] for m in res.values() if m.get("gain_credited") is not None)
    return {"scored": len(res), "pool": pool, "built": sum(1 for m in res.values() if m["built"]),
            "feasible": sum(1 for m in res.values() if m["feasible"]), "compared": len(g),
            "median_gain": g[len(g) // 2] if g else None, "better_by_10pct": sum(1 for x in g if x >= 1.1),
            "worse_by_10pct": sum(1 for x in g if x <= 0.9)}
