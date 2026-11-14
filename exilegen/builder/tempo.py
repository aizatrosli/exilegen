"""Per-skill tempo facts: what a skill costs to use, how long a use takes and how often it can be used.

    python3 -m exilegen.builder.tempo --pob-root <PoB src dir>      # rebuilds data/skill_tempo.json

Read from PoB's Data/Skills/*.lua at gem level 20 (or the highest level a skill has):
  cost            {"Mana": 56}; channelled skills carry "ManaPerMinute". A few skills cost Ward or Rage
                  instead; those are shown but not priced, because the budget is in mana
  cast_time       seconds per use for spells and other timed skills
  attack_speed    attackSpeedMultiplier in percent, for attacks (use time depends on the weapon)
  cooldown        seconds, with stored_uses
  spirit          flat Spirit reservation of a persistent skill

`weigh(...)` turns these into a 0..1 factor for a mechanics-graph chain. It is a screen, not a score:
PoB still decides what a build is worth. The factor only stops the graph from ranking a chain as if its
skills were free, instant and always available.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re

from . import catalog as catmod

PATH = os.path.join(catmod.DATA_DIR, "skill_tempo.json")
REF_ATTACKS_PER_SECOND = 1.4      # a typical weapon; attacks have no cast time of their own
REAPPLY_SECONDS = 6.0             # how often a manually used helper (curse, mark, warcry) is re-used
FLOOR = 0.25                      # no single factor may sink a chain below this
DEFAULT_BUDGET = {"mana_per_second": 250.0, "spirit": 200.0}

_NUM = r"(-?\d+(?:\.\d+)?)"


def parse(pob_root: str) -> dict[str, dict]:
    out = {}
    for path in sorted(glob.glob(os.path.join(pob_root, "Data", "Skills", "*.lua"))):
        text = open(path, encoding="utf-8", errors="replace").read()
        heads = list(re.finditer(r'^skills\["([^"]+)"\]\s*=\s*\{', text, re.M))
        for i, m in enumerate(heads):
            body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
            levels = {int(k): v for k, v in re.findall(r"^\t\t\[(\d+)\] = \{(.*)\},?\s*$", body, re.M)}
            if not levels:
                continue
            lvl = 20 if 20 in levels else max(k for k in levels if k <= 20) if any(k <= 20 for k in levels) else min(levels)
            row = levels[lvl]
            d: dict = {"level": lvl}
            cost = re.search(r"cost = \{([^}]*)\}", row)
            if cost:
                c = {k: float(v) for k, v in re.findall(r"(\w+) = " + _NUM, cost.group(1))}
                if c:
                    d["cost"] = c
            for key, name in (("cooldown", "cooldown"), ("storedUses", "stored_uses"), ("spiritReservationFlat", "spirit"),
                              ("attackSpeedMultiplier", "attack_speed")):
                mm = re.search(key + " = " + _NUM, row)
                if mm and float(mm.group(1)) != 0:
                    d[name] = float(mm.group(1))
            ct = re.search(r"^\tcastTime = " + _NUM, body, re.M)
            if ct:
                d["cast_time"] = float(ct.group(1))
            out[m.group(1)] = d
    return out


def load(path: str = PATH) -> dict[str, dict]:
    return json.load(open(path))["skills"] if os.path.exists(path) else {}


INSTANT_TYPES = {"Instant", "NoAttackOrCastTime"}
TRIGGERED_TYPES = {"Triggered", "InbuiltTrigger"}
PRICED = ("Mana", "ManaPerMinute")


def use_time(skill: dict, t: dict) -> float:
    """Seconds one use takes (0 for instant and triggered skills)."""
    types = set(skill.get("types") or [])
    if types & (INSTANT_TYPES | TRIGGERED_TYPES):
        return 0.0
    if "Attack" in (skill.get("types") or []):      # PoB gives attacks a placeholder castTime of 1
        return 1.0 / (REF_ATTACKS_PER_SECOND * (1.0 + t.get("attack_speed", 0.0) / 100.0))
    return t.get("cast_time") or skill.get("cast_time") or 1.0


def facts(skill: dict, t: dict) -> dict:
    """Display facts for one skill: seconds per use, uses per second, resource per second, Spirit."""
    types = set(skill.get("types") or [])
    ut = use_time(skill, t)
    cd = t.get("cooldown", 0.0)
    persistent = bool({"Persistent", "HasReservation"} & types) or bool(t.get("spirit"))
    cost = {k: v for k, v in (t.get("cost") or {}).items() if v}
    # an instant skill with no cooldown is still pressed about once a second
    rate = 0.0 if persistent and not cost else 1.0 / max(ut, cd, 0.05 if ut else 1.0)
    # channelled skills are priced per minute of channelling, everything else per use
    per_second = sum(v / 60.0 if k.endswith("PerMinute") else v * rate for k, v in cost.items() if k in PRICED)
    return {"use_time": round(ut, 2), "cooldown": cd, "stored_uses": int(t.get("stored_uses", 0)),
            "cost": cost, "cost_per_second": round(per_second, 1), "spirit": t.get("spirit", 0.0),
            "persistent": persistent, "instant": bool(types & INSTANT_TYPES), "triggered": bool(types & TRIGGERED_TYPES),
            "unpriced": sorted(k for k in cost if k not in PRICED)}


def weigh(main: dict, helpers: list[dict], tempo: dict[str, dict], budget: dict | None = None) -> dict:
    """-> {"tempo": 0..1, "factors": {resource, spirit, cooldown, time}, "main": facts, "helpers": [...]}.

    resource   cost per second of the main skill plus hand-used helpers, against a sustainable budget
    spirit     the chain's total Spirit reservation against the Spirit budget
    cooldown   share of the time the main skill is actually usable (square-rooted: PoB already counts
               the cooldown in DPS, this only stops cooldown skills from ranking as if spammable)
    time       seconds taken from the main skill by helpers that must be used by hand
    """
    b = {**DEFAULT_BUDGET, **(budget or {})}
    mf = facts(main, tempo.get(main["id"], {}))
    hf = [{"id": h["id"], "name": h["name"], **facts(h, tempo.get(h["id"], {}))} for h in helpers]
    manual = [h for h in hf if not h["persistent"] and not h["triggered"]]     # triggered helpers cost no casts
    helper_cost = sum(h["cost"].get("Mana", 0.0) / max(h["cooldown"], REAPPLY_SECONDS) for h in manual)
    per_second = mf["cost_per_second"] + helper_cost
    resource = 1.0 / (1.0 + per_second / b["mana_per_second"])
    total_spirit = mf["spirit"] + sum(h["spirit"] for h in hf)
    spirit = 1.0 if total_spirit <= b["spirit"] else b["spirit"] / total_spirit
    cooldown = 1.0 if mf["cooldown"] <= mf["use_time"] else (mf["use_time"] / mf["cooldown"]) ** 0.5
    taken = sum(h["use_time"] / max(h["cooldown"], REAPPLY_SECONDS) for h in manual)
    time = 1.0 - min(0.75, taken)
    f = {k: round(max(FLOOR, v), 3) for k, v in (("resource", resource), ("spirit", spirit), ("cooldown", cooldown), ("time", time))}
    return {"tempo": round(f["resource"] * f["spirit"] * f["cooldown"] * f["time"], 3), "factors": f, "main": mf,
            "helpers": hf, "spirit_total": total_spirit, "cost_per_second": round(per_second, 1)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="extract per-skill cost, cast time, cooldown and Spirit from PoB data")
    ap.add_argument("--pob-root", required=True, help="PoB src dir (holds Data/Skills)")
    ap.add_argument("--source", default="", help="note on where the data came from (commit, version)")
    a = ap.parse_args(argv)
    skills = parse(a.pob_root)
    json.dump({"source": a.source, "skills": skills}, open(PATH, "w"), separators=(",", ":"), sort_keys=True)
    print(json.dumps({"skills": len(skills), "with_cost": sum("cost" in s for s in skills.values()),
                      "with_cooldown": sum("cooldown" in s for s in skills.values()),
                      "with_spirit": sum("spirit" in s for s in skills.values())}))


if __name__ == "__main__":
    main()
