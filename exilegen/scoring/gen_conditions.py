"""Generate the auto-scanned draft of the condition registry (conditions_auto.yaml).

    python3 -m exilegen.scoring.gen_conditions --pob <PoB-PoE2 root> [--out exilegen/scoring/conditions_auto.yaml]

Scans gem/support descriptions, gem stat ids, keystone text and unique mod lines for condition
keywords. Every generated entry is `verified: false`. Hand-curated entries in conditions.yaml
override generated ones with the same id (or the same gem id / name), so re-running this script
never loses curation.

Risk classes:
  high   - a hard state (curse, corpse, minion, freeze, ...) and a gating phrase ("detonate", "consume",
           "only takes effect if", "can only be used after", ...) in the same sentence, or a corpse-targeting
           skill type. PoB usually computes such damage as if the state were present, so the registry applies
           the profile's availability credit (auto entries: never below 0.5, never block).
           The Gems.lua "payoff" tag alone is only medium: PoB often reads those states from config.
  medium - a damage/speed bonus behind the condition. PoB mostly reads these from config conditions
           (which locked profiles turn off), so the entry is flagged but not discounted.
  low    - non-damage mention (recovery, utility). Flag only.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import yaml

from . import pobdata

# (condition id, description regex, stat-id regex)
COND_RULES: list[tuple[str, str, str | None]] = [
    ("enemy_cursed", r"\bcurse[sd]?\b|\bhex(es|ed)?\b", r"curse"),
    ("corpses_present", r"\bcorpses?\b", r"corpse"),
    ("kills_recently", r"\bkill(s|ed|ing)?\b|\bslain\b", r"on_kill|_killed|_kill_"),
    ("moving", r"while (you are )?moving|have moved|moved a (certain )?distance|after (you have )?moved|\btravel(led)? a", r"distance_travelled|_moved_"),
    ("stationary", r"\bstationary\b|standing still|haven't moved", r"stationary"),
    ("minion_present", r"\b(one of )?your minions?\b|\bif you have a minion\b|\bdrain(ing)? .*minion", r"drain_minion|sacrifice_minion"),
    ("glory", r"\bglory\b", r"glory"),
    ("enemy_low_life", r"enem(y|ies) (on|are on|is on) low life|low life enem", None),
    ("low_life", r"(you are|while you are|while|you're) on low life|\blow life\b(?! enem)(?<!enemies on low life)(?<!enemy on low life)", r"low_life"),
    ("full_life", r"\bfull life\b", r"full_life"),
    ("combo", r"\bcombo\b", r"combo"),
    ("charges", r"\b(power|frenzy|endurance) charges?\b|\bconsum(e|es|ing) .*charges?\b", r"consume.*_charge"),
    ("ramp_up_time", r"\baccumulat|the (more|longer)|\bincreasingly\b|\bramp|builds? up", r"accumulat|_ramp"),
    ("block", r"\bblock(ed|ing|s)?\b", r"on_block|_block_"),
    ("been_hit", r"when (you are )?hit\b|\bbeen hit\b|when you take damage", r"when_hit|been_hit"),
    ("enemy_frozen", r"\bfrozen\b|\bfreezes?\b", None),
    ("enemy_shocked", r"\bshock(ed|s)?\b", None),
    ("enemy_ignited", r"\bignit(e|ed|es)\b", None),
    ("enemy_chilled", r"\bchill(ed|s)?\b", None),
    ("enemy_poisoned", r"\bpoison(ed|s)?\b", None),
    ("enemy_bleeding", r"\bbleed(ing|s)?\b|\bblood loss\b", None),
    ("enemy_electrocuted", r"\belectrocut", None),
    ("enemy_stunned", r"\bheavy stun|\bstunned\b|\bprimed for stun|\bdazed? (enemies|enemy)", None),
    ("armour_broken", r"fully broken|armour break|\bbreaks? armour", r"armour_break|fully_broken"),
    ("rage", r"\brage\b", r"_rage"),
    ("archon_infusion_remnant", r"\barchon\b|\binfusions?\b|\bremnants?\b|\bsurges?\b", r"remnant|infusion|archon"),
    ("cooldown_gate", r"every few seconds|cannot be used until|once every|can only be used", None),
    ("on_death", r"\bon death\b|when you die|minions? die|minion death", r"on_death"),
    ("proximity", r"close to you|close range|further away|far away|\bat a distance", r"close_range"),
    ("parry", r"\bparr(y|ied|ies)\b", r"parry"),
    ("marked", r"\bmark(ed)?\b", r"_mark_"),
    ("unbound", r"\bunbound\b", None),
    ("low_mana", r"\blow mana\b", None),
]
DRAWBACK_RULES: list[tuple[str, str, str | None]] = [
    ("self_daze", r"daze(s)? you\b|will daze you", r"daze_self"),
    ("life_loss_on_use", r"lose (life|.*\blife\b)", r"lose_.*life|life_loss"),
    ("life_cost", r"life cost|cost(s)? .*\blife\b", r"life_cost"),
    ("move_slower", r"move slower|reduced movement speed while", None),
    ("self_damage_taken", r"you take .*(more|increased) damage|take (more|increased) damage", None),
    ("less_defences", r"less (armour|evasion|energy shield|defences)", None),
]
HIGH_RISK_RE = re.compile(r"detonat|\bconsum(e|es|ing)\b|only takes effect if|can only be used after|"
                          r"only be used after|requires? (a|an|you)|gain a glory requirement|glory requirement|"
                          r"draining the life|on reaching maximum|for which at least|cause a corpse", re.I)
# states a Pinnacle fight does not provide by itself and PoB does not check for skill-inherent requirements
HARD_STATES = {"enemy_cursed", "corpses_present", "minion_present", "glory", "moving", "combo", "armour_broken",
               "parry", "enemy_frozen", "enemy_shocked", "enemy_ignited", "enemy_chilled", "enemy_bleeding",
               "enemy_poisoned", "enemy_electrocuted", "enemy_stunned", "kills_recently", "on_death", "marked", "block"}
DAMAGE_RE = re.compile(r"\bdamage\b|\bmore\b|critical|skill speed|attack speed|cast speed", re.I)


def _match(rules, text: str, stat_ids: list[str]):
    hits = []
    for cid, rx, srx in rules:
        why = None
        m = re.search(rx, text, re.I)
        if m:
            why = f"text '{m.group(0).strip()}'"
        elif srx:
            for s in stat_ids:
                if re.search(srx, s):
                    why = f"stat {s}"
                    break
        if why:
            hits.append((cid, why))
    return hits


def scan_gems(skills: dict) -> list[dict]:
    out = []
    for sid, sd in sorted(skills.items()):
        if not sd.gem_name or sd.hidden:
            continue
        desc = sd.description or ""
        conds = _match(COND_RULES, desc, sd.stat_ids)
        if "TargetsDestructibleCorpses" in sd.skill_types or "TargetsDestructibleRareCorpses" in sd.skill_types:
            conds.append(("corpses_present", "skillType TargetsDestructibleCorpses"))
        draws = _match(DRAWBACK_RULES, desc, sd.stat_ids)
        is_minion_skill = any(t in sd.skill_types for t in ("Minion", "CreatesMinion")) or desc.lower().startswith("supports minion")
        if is_minion_skill:
            conds = [c for c in conds if c[0] not in ("minion_present", "on_death")]
        # ailment words in an ailment-applying skill/support describe what it does, not a condition
        if not re.search(r"against|affected by|consum|while .* (are|is)|on (frozen|shocked|ignited|chilled|poisoned)", desc, re.I):
            conds = [c for c in conds if not c[0].startswith("enemy_") or c[0] in ("enemy_cursed", "enemy_stunned")]
        if not conds and not draws:
            continue
        damaging = sd.support or any(t in sd.skill_types for t in ("Damage", "Attack", "DamageOverTime"))
        payoff = "payoff" in sd.gem_tags
        hard = [c for c, _ in conds if c in HARD_STATES]
        # high risk: a hard state and a gating phrase in the SAME sentence (or a corpse-targeting skill type)
        gated = any(HIGH_RISK_RE.search(sent) and not re.search(r"infusion|remnant", sent, re.I) and _match([r for r in COND_RULES if r[0] in HARD_STATES], sent, [])
                    for sent in re.split(r"(?<=\.)\s+", desc))
        gated = gated or any(w.startswith("skillType") for c, w in conds if c == "corpses_present")
        if gated and damaging and hard:
            risk = "high"
        elif DAMAGE_RE.search(desc) and damaging and (conds or draws):
            risk = "medium"
        else:
            risk = "low"
        out.append({
            "id": ("support." if sd.support else "gem.") + re.sub(r"[^a-z0-9]+", "_", sd.gem_name.lower()).strip("_"),
            "kind": "support" if sd.support else "gem",
            "name": sd.gem_name,
            "match": {"ids": [sid]},
            "conditions": sorted({c for c, _ in conds}),
            "drawbacks": sorted({d for d, _ in draws}),
            "risk": risk,
            "role": "support" if sd.support else ("skill" if damaging else "utility"),
            "verified": False,
            "source": "auto-scan " + sd.file + ": " + "; ".join(f"{c}<-{w}" for c, w in conds + draws),
            "text": desc[:300],
        })
    return out


def scan_keystones(keys) -> list[dict]:
    out = []
    for k in keys:
        text = " ".join(k.stats)
        conds = _match(COND_RULES, text, [])
        draws = _match(DRAWBACK_RULES, text, [])
        if not conds and not draws:
            continue
        out.append({
            "id": "keystone." + re.sub(r"[^a-z0-9]+", "_", k.name.lower()).strip("_"),
            "kind": "keystone", "name": k.name, "match": {"names": [k.name]},
            "conditions": sorted({c for c, _ in conds}), "drawbacks": sorted({d for d, _ in draws}),
            "risk": "medium" if DAMAGE_RE.search(text) else "low", "role": "global",
            "verified": False, "source": "auto-scan tree: " + "; ".join(f"{c}<-{w}" for c, w in conds + draws),
            "text": text[:300],
        })
    return out


def scan_uniques(uniques) -> list[dict]:
    out, seen = [], set()
    for u in uniques:
        if u.name in seen:
            continue
        text = " | ".join(u.lines)
        conds = _match(COND_RULES, text, [])
        draws = _match(DRAWBACK_RULES, text, [])
        # only uniques whose damage/speed lines are gated by a condition
        gated = [l for l in u.lines if DAMAGE_RE.search(l) and _match(COND_RULES, l, [])]
        if not gated and not draws:
            continue
        seen.add(u.name)
        out.append({
            "id": "unique." + re.sub(r"[^a-z0-9]+", "_", u.name.lower()).strip("_"),
            "kind": "unique", "name": u.name, "match": {"names": [u.name]},
            "conditions": sorted({c for c, _ in conds}), "drawbacks": sorted({d for d, _ in draws}),
            "risk": "medium" if gated else "low", "role": "global",
            "verified": False, "source": f"auto-scan uniques/{u.file}: " + "; ".join(f"{c}<-{w}" for c, w in conds + draws),
            "text": " | ".join(gated or u.lines)[:300],
        })
    return out


def generate(pob_root: str, tree_version: str = "0_5") -> dict:
    skills = pobdata.read_skills(pob_root)
    entries = scan_gems(skills) + scan_keystones(pobdata.read_keystones(pob_root, tree_version)) + \
        scan_uniques(pobdata.read_uniques(pob_root))
    # make ids unique
    seen: dict[str, int] = {}
    for e in entries:
        n = seen.get(e["id"], 0)
        seen[e["id"]] = n + 1
        if n:
            e["id"] = f"{e['id']}_{n + 1}"
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["risk"]] = counts.get(e["risk"], 0) + 1
    return {"generated_by": "exilegen.scoring.gen_conditions", "pob_root_hint": os.path.basename(pob_root.rstrip("/")),
            "counts": counts, "entries": entries}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pob", required=True, help="PoB-PoE2 root (contains Data/, TreeData/)")
    ap.add_argument("--tree", default="0_5")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "conditions_auto.yaml"))
    a = ap.parse_args(argv)
    doc = generate(a.pob, a.tree)
    with open(a.out, "w") as f:
        f.write("# GENERATED by exilegen.scoring.gen_conditions -- do not hand-edit; curate in conditions.yaml\n")
        yaml.safe_dump(doc, f, sort_keys=False, width=140, allow_unicode=True)
    print(f"wrote {len(doc['entries'])} entries {doc['counts']} -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
