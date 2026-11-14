"""When does a chain come online while leveling, and what has to be farmed for it?

A chain is a set of parts: skill gems, uniques, keystones and ascendancy notables. Each part has a gate:

  skill gem           the character level its level-1 gem needs
  unique              the item's level requirement, and where it drops (boss-only items need that boss farmed)
  keystone            the passive points needed to path to it from the class start, turned into a level
  ascendancy notable  the ascendancy points needed to reach it, turned into the level of the trial that gives them

`plan()` lists the parts in the order they unlock. The chain is "online" at the highest of those levels. With the
leveling option on, graphx multiplies the chain score by `factor`: chains that only work late, or that hang on a
boss drop, rank lower, and chains with an item that can no longer drop are removed.

The facts come from PoB data (`python3 -m exilegen.builder.leveling --pob-root <PathOfBuilding-PoE2>/src`).
The schedule in ASSUMED is not in PoB. It is taken from the PoE2 Codex campaign guide for patch 0.5.5
(https://domistae.github.io/poe2-leveling/, MIT, by domistae): the zone level of each quest that gives passive
points or Spirit, and the level of each ascendancy trial. It is a community guide, so treat it as a good estimate.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from collections import deque

from . import catalog as catmod

PATH = os.path.join(catmod.DATA_DIR, "leveling.json")

ASSUMED = {
    # ascendancy points held -> earliest character level for the trial that gives them. The guide runs the 1st at
    # 28 for an easier trial and says the 4th usually comes around level 85; these are the earliest keys.
    "ascendancy_points_level": {2: 22, 4: 33, 6: 60, 8: 75},
    # character level -> passive points from campaign quests held by then (2 per quest, 24 in total)
    "quest_passive_points": {10: 2, 12: 4, 22: 6, 26: 8, 30: 10, 39: 12, 47: 14, 48: 16, 52: 18, 54: 20, 56: 22, 58: 24},
    # character level -> Spirit from campaign quests held by then (+30 Act 1, +30 Act 3, +40 Interludes)
    "quest_spirit": {11: 30, 33: 60, 55: 100},
    # boss-only drops come from endgame bosses; Tier 11 maps are area level 75
    "boss_drop_level": 75,
    "max_level": 100,
}
EARLY_LEVEL = 30        # online by here costs nothing
LATE_LEVEL = 90         # online only here costs the most
LATE_FACTOR = 0.4
BOSS_FACTOR = 0.8       # per boss-only unique
FARM_FLOOR = 0.5


# ---------------------------------------------------------------------------------------------- parse PoB data
def _skills(pob_root: str, tiers: dict[str, int]) -> dict[str, int]:
    """Character level at which each skill's gem can first be cut: the level requirement of the gem level equal to
    the gem's tier (tier 1 -> 1, 3 -> 6, 5 -> 14, ... 13 -> 52). Tier 0 skills come from items or ascendancies and
    are gated by that source instead."""
    out = {}
    for path in sorted(glob.glob(os.path.join(pob_root, "Data", "Skills", "*.lua"))):
        text = open(path, encoding="utf-8", errors="replace").read()
        heads = list(re.finditer(r'^skills\["([^"]+)"\]\s*=\s*\{', text, re.M))
        for i, m in enumerate(heads):
            body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
            reqs = {int(k): int(v) for k, v in re.findall(r"^\t\t\[(\d+)\] = \{.*?levelRequirement = (\d+)", body, re.M)}
            tier = tiers.get(m.group(1))
            if reqs and tier:
                out[m.group(1)] = max(reqs.get(tier, 0), 1)
    return out


def _base_granted(pob_root: str) -> dict[str, dict]:
    """Skills granted by a weapon or shield base: skill name -> the lowest-level base that grants it."""
    out: dict[str, dict] = {}
    for path in glob.glob(os.path.join(pob_root, "Data", "Bases", "*.lua")):
        text = open(path, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r'itemBases\["([^"]+)"\] = \{(.*?)\n\}', text, re.S):
            g = re.search(r'implicit = "Grants Skill: (?:Level \([\d-]+\) )?([^"\\]+)', m.group(2))
            lv = re.search(r"req = \{[^}]*level = (\d+)", m.group(2))
            if g:
                level = int(lv.group(1)) if lv else 1
                if g.group(1) not in out or level < out[g.group(1)]["level"]:
                    out[g.group(1)] = {"level": max(level, 1), "base": m.group(1)}
    return out


def _uniques(pob_root: str) -> dict[str, dict]:
    base_level = {}
    for path in glob.glob(os.path.join(pob_root, "Data", "Bases", "*.lua")):
        text = open(path, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r'itemBases\["([^"]+)"\] = \{(.*?)\n\}', text, re.S):
            lv = re.search(r"req = \{[^}]*level = (\d+)", m.group(2))
            base_level[m.group(1)] = int(lv.group(1)) if lv else 0
    out = {}
    for path in sorted(glob.glob(os.path.join(pob_root, "Data", "Uniques", "*.lua"))):
        text = open(path, encoding="utf-8", errors="replace").read()
        for block in re.findall(r"\[\[\n(.*?)\]\]", text, re.S):
            lines = [l.strip() for l in block.strip().split("\n")]
            if len(lines) < 2:
                continue
            d: dict = {"level": base_level.get(lines[1], 0)}
            for l in lines[2:]:
                m = re.match(r"Requires Level (\d+)", l)
                if m:
                    d["level"] = int(m.group(1))
                elif l.startswith("Source: No longer obtainable"):
                    d["unobtainable"] = True
                elif l.startswith("Source: Drops from"):
                    names = re.findall(r"\{([^}]+)\}", l)
                    d["boss"] = names[0] if names else l[len("Source: Drops from"):].strip()
                    if len(names) > 1:
                        d["area"] = names[1]
                elif l.startswith("League: "):
                    d["league"] = l[len("League: "):]
            out.setdefault(lines[0], d)
    return out


def _tree(pob_root: str, version: str = "0_5") -> dict:
    t = json.load(open(os.path.join(pob_root, "TreeData", version, "tree.json"), encoding="utf-8"))
    nodes = {str(k): n for k, n in t["nodes"].items() if isinstance(n, dict)}
    adj: dict[str, set] = {k: set() for k in nodes}
    for k, n in nodes.items():
        for c in n.get("connections") or []:
            o = str(c["id"] if isinstance(c, dict) else c)
            if o in nodes:
                adj[k].add(o)
                adj[o].add(k)

    def bfs(start: str, same_asc: str | None) -> dict[str, int]:
        dist, q = {start: 0}, deque([start])
        while q:
            k = q.popleft()
            for o in adj[k]:
                if o not in dist and nodes[o].get("ascendancyName") == same_asc:
                    dist[o] = dist[k] + 1
                    q.append(o)
        return dist

    keystones: dict[str, dict] = {}
    for k, n in nodes.items():
        if not n.get("classesStart"):
            continue
        dist = bfs(k, None)
        for o, d in dist.items():
            if nodes[o].get("isKeystone"):
                for cls in n["classesStart"]:
                    cur = keystones.setdefault(nodes[o]["name"], {})
                    cur[cls] = min(d, cur.get(cls, d))
    asc: dict[str, dict] = {}
    for k, n in nodes.items():
        if n.get("isAscendancyStart"):
            a = n["ascendancyName"]
            asc[a] = {nodes[o]["name"]: d for o, d in bfs(k, a).items() if nodes[o].get("isNotable")}
    return {"keystones": keystones, "ascendancy": asc}


def parse(pob_root: str, tiers: dict[str, int] | None = None) -> dict:
    if tiers is None:
        tiers = {k: int(v.get("tier") or 0) for k, v in catmod.load().skills.items()}
    return {"skills": _skills(pob_root, tiers), "base_granted": _base_granted(pob_root), "uniques": _uniques(pob_root), **_tree(pob_root)}


def load(path: str = PATH) -> dict:
    return json.load(open(path)) if os.path.exists(path) else {}


# ---------------------------------------------------------------------------------------------- levels
def level_for_passives(points: int) -> int:
    a = ASSUMED
    for lvl in range(1, a["max_level"] + 1):
        quest = max([n for at, n in a["quest_passive_points"].items() if at <= lvl], default=0)
        if lvl - 1 + quest >= points:
            return lvl
    return a["max_level"]


def level_for_spirit(spirit: float) -> tuple[int, bool]:
    """Level at which campaign quests alone give this much Spirit, and whether gear has to add the rest."""
    table = sorted(ASSUMED["quest_spirit"].items())
    for lvl, have in table:
        if spirit <= have:
            return lvl, False
    return table[-1][0], True


def level_for_ascendancy(points: int) -> int:
    for have, lvl in sorted(ASSUMED["ascendancy_points_level"].items()):
        if points <= have:
            return lvl
    return ASSUMED["max_level"]


def factor(online_level: int, boss_items: int, unobtainable: bool) -> float:
    if unobtainable:
        return 0.0
    span = min(max((online_level - EARLY_LEVEL) / (LATE_LEVEL - EARLY_LEVEL), 0.0), 1.0)
    return round((1 - (1 - LATE_FACTOR) * span) * max(BOSS_FACTOR ** boss_items, FARM_FLOOR), 3)


def _skill_step(sid: str, sk: dict, chain: dict, data: dict) -> dict:
    name = sk.get("name") or sid
    s = {"kind": "skill", "name": name, "level": 1, "gate": "gem", "known": True, "main": sid == chain["main_skill"]}
    lv = data.get("skills", {}).get(sid)
    if lv is not None:
        s["level"] = lv
        return s
    # no gem tier: the skill comes from an ascendancy notable, a unique or a weapon base
    if sk.get("asc_granted"):
        a, n = sk["asc_granted"][0]
        pts = (data.get("ascendancy", {}).get(a) or {}).get(n)
        s.update(gate="ascendancy", ascendancy=a, points=pts, known=pts is not None,
                 level=level_for_ascendancy(pts) if pts is not None else ASSUMED["ascendancy_points_level"][2])
        return s
    base = data.get("base_granted", {}).get(name)
    owners = [u for u in sk.get("granted_by") or [] if u in data.get("uniques", {})]
    if base and not (set(owners) & set(chain.get("uniques") or [])):
        s.update(gate="base", base=base["base"], level=base["level"])
    elif owners:
        own = [u for u in owners if u in (chain.get("uniques") or [])] or owners
        u = min(own, key=lambda x: data["uniques"][x].get("level", 0))
        s.update(gate="item", item=u, level=max(data["uniques"][u].get("level", 1), 1))
    else:
        s["known"] = False
    return s


def plan(chain: dict, data: dict, cls: str | None = None, skills: dict | None = None, path_skills: list | None = None,
         spirit: float = 0.0) -> dict:
    """Leveling path for one chain. `cls` is the class the build starts as (None = the closest class per keystone).
    `skills` is the catalogue's skill table; `path_skills` are the ids of the helper skills in the chain's path; `spirit` is the Spirit the chain reserves."""
    steps = []
    skills = skills or {}
    seen = set()
    for sid in [chain["main_skill"]] + list(path_skills or []):
        if sid and sid not in seen:
            seen.add(sid)
            sk = dict(skills.get(sid) or {})
            if sid == chain["main_skill"]:
                sk.setdefault("name", chain["main_name"])
            steps.append(_skill_step(sid, sk, chain, data))
    for u in chain.get("uniques") or []:
        d = data.get("uniques", {}).get(u)
        s = {"kind": "unique", "name": u, "level": max((d or {}).get("level", 1), 1), "gate": "drop", "known": d is not None}
        if d and d.get("unobtainable"):
            s["gate"] = "unobtainable"
        elif d and d.get("boss"):
            s.update(gate="boss", boss=d["boss"], level=max(s["level"], ASSUMED["boss_drop_level"]))
            if d.get("area"):
                s["area"] = d["area"]
        if d and d.get("league"):
            s["league"] = d["league"]
        steps.append(s)
    for k in chain.get("keystones") or []:
        per = data.get("keystones", {}).get(k) or {}
        pick = cls if cls in per else (min(per, key=per.get) if per else None)
        pts = per.get(pick)
        steps.append({"kind": "keystone", "name": k, "gate": "passives", "known": pts is not None,
                      "points": pts, "cls": pick, "level": level_for_passives(pts) if pts is not None else 1})
    for a, n in chain.get("asc_nodes") or []:
        pts = (data.get("ascendancy", {}).get(a) or {}).get(n)
        steps.append({"kind": "asc", "name": n, "ascendancy": a, "gate": "ascendancy", "known": pts is not None,
                      "points": pts, "level": level_for_ascendancy(pts) if pts is not None else ASSUMED["ascendancy_points_level"][2]})
    need = float(spirit or 0)
    if need > 0:
        lvl, gear = level_for_spirit(need)
        steps.append({"kind": "spirit", "name": f"{need:g} Spirit", "level": lvl, "gate": "spirit_gear" if gear else "spirit",
                      "known": True, "points": need, "quest": max(ASSUMED["quest_spirit"].values())})
    steps.sort(key=lambda s: (s["level"], s["kind"], s["name"]))
    online = max((s["level"] for s in steps), default=1)
    start = next((s["level"] for s in steps if s.get("main")), 1)
    bosses = [s for s in steps if s["gate"] == "boss"]
    dead = any(s["gate"] == "unobtainable" for s in steps)
    return {"steps": steps, "start_level": start, "online_level": online,
            "farm": sorted({s["boss"] for s in bosses}), "drops": [s["name"] for s in steps if s["gate"] == "drop"],
            "unobtainable": dead, "unknown": [s["name"] for s in steps if not s["known"]],
            "factor": factor(online, len(bosses), dead)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract level and drop gates for skills, uniques and tree nodes from PoB data")
    ap.add_argument("--pob-root", required=True, help="PathOfBuilding-PoE2/src")
    ap.add_argument("--source", default="", help="label for the data revision, e.g. 'PathOfBuilding-PoE2 <commit>'")
    ap.add_argument("--out", default=PATH)
    a = ap.parse_args(argv)
    d = parse(a.pob_root)
    with open(a.out, "w") as f:
        json.dump({**d, "source": a.source, "assumed": {k: ({str(x): y for x, y in v.items()} if isinstance(v, dict) else v)
                                                        for k, v in ASSUMED.items()}}, f, sort_keys=True, separators=(",", ":"))
    print(f"{len(d['skills'])} skills, {len(d['uniques'])} uniques, {len(d['keystones'])} keystones, "
          f"{len(d['ascendancy'])} ascendancies -> {a.out}")


if __name__ == "__main__":
    main()
