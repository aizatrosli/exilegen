"""State registry: every state the mechanics graph can name, keyed by owner, kind and name.

    python3 -m exilegen.builder.states --check [--pob-root <PoB src dir>]

`data/states.json` holds the states PoB knows and some mod line actually reads (a snapshot taken from PoB's
ModCache, ModParser and ConfigOptions; `source` in the file says which PoB). Each row:
  key       "enemy|condition|Shocked": owner (self, enemy, minion, ally), kind (condition, count, stat), PoB name
  family    ailment, debuff, charge, resource, buff, recent, level, minion, action, ground, context, fact, ...
  lines     how many distinct mod lines in PoB's cache are gated or scaled by it
  options   the general config options that set it: [{"var", "type": check|count|list, "gate"?}]
  auto      PoB's calculation sets it without a config option (charges with their use* option, curses, heralds)
  params    divisors, limits and thresholds seen on its consumers ({"div=200": 10})

`TOKENS` is the hand layer that ties the mechanics graph's tokens to these states: which states a token stands
for, the config values that switch it on for a DPS measurement (`switch`), its cap, and what kind of fight it
needs. A token with `verified: False` names a mechanic PoB does not calculate; a chain on it cannot be credited.

    switch_for(tokens)                -> the config values chain scoring sets to credit a chain's states
    state(key) / for_token(token)     -> registry rows
"""
from __future__ import annotations

import argparse
import json
import os
import re

from . import catalog as catmod

PATH = os.path.join(catmod.DATA_DIR, "states.json")
MANA_WINDOW = 4.0          # "Recently" is the past 4 seconds
DEFAULT_MANA_RECENT = 800  # mana spent recently when the chain's cost is unknown

# token -> states it stands for (first is the primary), the config that credits it, and how far to trust that.
#   needs   "kills": only true while clearing, so the boss profile does not credit it
#   spent   the switch value is the mana (or life) spent in MANA_WINDOW seconds, taken from the chain's cost
#   verified False: PoB has no calculation for it
#   note    what the switch does not cover
TOKENS: dict[str, dict] = {
    "enemy:Shocked": {"states": ["enemy|condition|Shocked"], "switch": {"conditionEnemyShocked": True}},
    "enemy:Frozen": {"states": ["enemy|condition|Frozen", "enemy|condition|Immobilised"], "switch": {"conditionEnemyFrozen": True}},
    "enemy:Chilled": {"states": ["enemy|condition|Chilled"], "switch": {"conditionEnemyChilled": True}},
    "enemy:Ignited": {"states": ["enemy|condition|Ignited", "enemy|condition|Burning"],
                      "switch": {"conditionEnemyIgnited": True, "conditionEnemyBurning": True}},
    "enemy:Poisoned": {"states": ["enemy|condition|Poisoned", "enemy|count|PoisonStacks"],
                       "switch": {"conditionEnemyPoisoned": True, "multiplierPoisonOnEnemy": 1}, "cap": {"default": 1}},
    "enemy:Bleeding": {"states": ["enemy|condition|Bleeding"], "switch": {"conditionEnemyBleeding": True}},
    "enemy:Electrocuted": {"states": ["enemy|condition|Electrocuted", "enemy|condition|Immobilised"],
                           "switch": {"conditionEnemyElectrocuted": True}},
    "enemy:Cursed": {"states": ["enemy|condition|Cursed"], "switch": {"conditionEnemyCursed": True}, "cap": {"default": 1}},
    "enemy:Marked": {"states": ["enemy|condition|Marked"], "note": "PoB sets it only while a Mark skill is active"},
    # the old switch (conditionEnemyStunned) credits 1 line; Heavy Stun is what 11 lines read and it implies Immobilised
    "enemy:Stunned": {"states": ["enemy|condition|HeavyStunned", "enemy|condition|Stunned", "enemy|condition|Immobilised"],
                      "switch": {"conditionEnemyHeavyStunned": True, "conditionEnemyStunned": True}},
    "enemy:ArmourBroken": {"states": ["enemy|condition|ArmourFullyBroken"], "switch": {"conditionEnemyArmourBroken": True}},
    "enemy:Exposed": {"states": ["enemy|condition|HasExposure"],
                      "note": "typed per element; the config options apply the exposure themselves, so no switch until the token is typed"},
    "enemy:Pinned": {"states": ["enemy|condition|Pinned", "enemy|condition|Immobilised"], "switch": {"conditionEnemyPinned": True}},
    "enemy:Withered": {"states": ["enemy|count|WitheredStack"], "switch": {"multiplierWitheredStackCount": 10}, "cap": {"default": 10}},
    "enemy:Blinded": {"states": ["enemy|condition|Blinded"], "switch": {"conditionEnemyBlinded": True}},
    "enemy:Intimidated": {"states": ["enemy|condition|Intimidated"], "switch": {"conditionEnemyIntimidated": True}},
    "charge:Power": {"states": ["self|count|PowerCharge"], "switch": {"usePowerCharges": True},
                     "cap": {"stat": "PowerChargesMax", "default": 3}, "aliases": ["PowerCharges", "RemovablePowerCharge"]},
    "charge:Frenzy": {"states": ["self|count|FrenzyCharge"], "switch": {"useFrenzyCharges": True},
                      "cap": {"stat": "FrenzyChargesMax", "default": 3}, "aliases": ["FrenzyCharges", "RemovableFrenzyCharge"]},
    "charge:Endurance": {"states": ["self|count|EnduranceCharge"], "switch": {"useEnduranceCharges": True},
                         "cap": {"stat": "EnduranceChargesMax", "default": 3}, "aliases": ["EnduranceCharges", "RemovableEnduranceCharge"]},
    "rage": {"states": ["self|count|RageEffect", "self|count|Rage"], "switch": {"multiplierRage": 30},
             "cap": {"stat": "MaximumRage", "default": 30}, "note": "PoB ignores the value unless the build has a Rage source it parsed"},
    "corpse": {"states": ["self|count|NearbyCorpse", "self|condition|ConsumedCorpseRecently"],
               "switch": {"multiplierNearbyCorpse": 1, "conditionConsumedCorpseRecently": True}},
    # two states under one token: the per-hit crit pass (automatic) and "if you've Crit Recently" (19 lines, a switch)
    "crit": {"states": ["self|condition|CritRecently", "self|condition|CriticalStrike"], "switch": {"conditionCritRecently": True}},
    "life:low": {"states": ["self|condition|LowLife"], "switch": {"conditionLowLife": True}},
    "life:full": {"states": ["self|condition|FullLife"], "switch": {"conditionFullLife": True}},
    "mana:low": {"states": ["self|condition|LowMana"], "switch": {"conditionLowMana": True}},
    "mana:full": {"states": ["self|condition|FullMana"], "switch": {"conditionFullMana": True}},
    "block": {"states": ["self|condition|BlockedRecently"], "switch": {"conditionBlockedRecently": True}},
    "flask": {"states": ["self|condition|UsingFlask"], "switch": {"conditionUsingFlask": True}},
    "charm": {"states": ["self|condition|UsingCharm"], "switch": {"conditionUsingCharm": True}},
    "totem": {"states": ["self|condition|HaveTotem", "self|stat|TotemsSummoned"], "switch": {"conditionHaveTotem": True}},
    "infusion": {"states": ["self|condition|InfusionConsumedRecently"], "switch": {"conditionInfusionConsumedRecently": True},
                 "note": "no element and no count in PoB"},
    "onslaught": {"states": ["self|condition|Onslaught"], "switch": {"buffOnslaught": True}},
    "tailwind": {"states": ["self|count|Tailwind"], "switch": {"multiplierTailwind": 10}, "cap": {"default": 10}},
    "mana_spent": {"states": ["self|count|ManaSpentRecently"], "switch": {"multiplierManaSpentRecently": DEFAULT_MANA_RECENT}, "spent": "mana"},
    "life_spent": {"states": ["self|count|LifeSpentRecently"], "switch": {"multiplierLifeSpentRecently": DEFAULT_MANA_RECENT}, "spent": "life"},
    "kill": {"states": ["self|condition|KilledRecently"], "switch": {"conditionKilledRecently": True}, "needs": "kills"},
    "presence": {"states": ["self|condition|EnemyInPresence"], "note": "computed from enemy distance; context, not a made state"},
    "es": {"states": ["self|condition|HaveEnergyShield", "self|condition|FullEnergyShield", "self|condition|LowEnergyShield"]},
    "stat:Str": {"states": ["self|stat|Str"]}, "stat:Dex": {"states": ["self|stat|Dex"]}, "stat:Int": {"states": ["self|stat|Int"]},
    "stat:Life": {"states": ["self|stat|Life"]}, "stat:Mana": {"states": ["self|stat|Mana"]}, "stat:Spirit": {"states": ["self|stat|Spirit"]},
    # mechanics PoB does not calculate
    "remnant": {"states": [], "verified": False}, "glory": {"states": [], "verified": False},
    "minion": {"states": []}, "deflect": {"states": []}, "evasion": {"states": []}, "armour": {"states": []},
    "trigger": {"states": [], "verified": False, "note": "meta-gem Energy is not calculated"},
}

_CACHE: dict | None = None


def load(path: str = PATH) -> dict[str, dict]:
    global _CACHE
    if _CACHE is None or path != PATH:
        rows = json.load(open(path))["states"] if os.path.exists(path) else []
        out = {r["key"]: r for r in rows}
        if path != PATH:
            return out
        _CACHE = out
    return _CACHE


def state(key: str) -> dict | None:
    return load().get(key)


def for_token(token: str) -> dict:
    """The registry's view of one graph token: its hand row plus the PoB rows of its states."""
    t = TOKENS.get(token) or {}
    reg = load()
    return {"token": token, "known": token in TOKENS, "verified": t.get("verified", True),
            "states": [reg.get(k) or {"key": k, "missing": True} for k in t.get("states", [])],
            **{k: t[k] for k in ("switch", "cap", "aliases", "needs", "spent", "note") if k in t}}


def switch_for(tokens: list[str], clearing: bool = False, spent_per_second: float | None = None) -> dict:
    """Config values that switch a chain's states on in PoB.

    clearing           credit kill-gated states too (False: a boss fight, where nothing dies)
    spent_per_second   the chain's mana (or life) cost per second, for the "spent Recently" counts
    """
    out: dict = {}
    for tok in tokens or []:
        t = TOKENS.get(tok)
        if not t or not t.get("switch") or (t.get("needs") == "kills" and not clearing):
            continue
        sw = dict(t["switch"])
        if t.get("spent") and spent_per_second:
            for k in sw:
                sw[k] = max(1, round(spent_per_second * MANA_WINDOW))
        out.update(sw)
    return out


def build(pob_states: dict, source: str = "") -> dict:
    """states.json from a PoB state extract ({"states": [{key, kind, owner, group, consumers, config, set_by, params}]})."""
    owner = {"player": "self", "enemy": "enemy", "minion": "minion", "player(parent)": "ally", "parent": "ally"}
    family = {"enemy_ailment": "ailment", "self_ailment_or_debuff": "ailment", "enemy_debuff": "debuff", "charges": "charge",
              "stack_resource": "resource", "buff": "buff", "recent_event": "recent", "resource_level": "level",
              "minion_totem_presence": "minion", "action_state": "action", "ground_or_position": "ground",
              "situational_enemy": "context", "situational_context": "context", "build_fact": "fact",
              "producer_capability": "can", "skill_specific": "skill", "calc_internal": "internal", "other": "other"}
    wanted = {k for t in TOKENS.values() for k in t["states"]}
    rows = []
    for s in pob_states["states"]:
        if s["kind"] in ("SkillType", "Flag") or s["owner"] not in owner:
            continue
        kind = "stat" if s["kind"] == "Stat" else "count" if "Multiplier" in s["kind"] else "condition"
        key = f"{owner[s['owner']]}|{kind}|{s['name']}"
        lines = s["consumers"]["modcache_atomic_lines"]
        if not (key in wanted or (s.get("live_in_poe2_data") and lines > 0)) or "{" in s["name"] or s["name"].endswith("*"):
            continue
        options = [{"var": c["var"], "type": c["type"], **({"gate": c["gates"]} if c.get("gates") else {})}
                   for c in s.get("config") or [] if not (c.get("gates") or {}).get("ifSkill")]
        options += [{"var": v, "type": "indirect"} for v in s.get("config_indirect") or []]
        row = {"key": key, "family": family.get(s.get("group"), "other"), "lines": lines}
        if options:
            row["options"] = options
        if "calc" in (s.get("set_by") or []) or "computed-stat" in (s.get("set_by") or []):
            row["auto"] = True
        if s.get("params"):
            row["params"] = s["params"]
        rows.append(row)
    rows.sort(key=lambda r: r["key"])
    return {"source": source, "states": rows}


def check(pob_root: str | None = None) -> list[str]:
    """Problems in the hand layer: states missing from the registry, and (with a PoB) config names PoB lacks."""
    reg, bad = load(), []
    for tok, t in TOKENS.items():
        bad += [f"{tok}: state {k} is not in the registry" for k in t["states"] if k not in reg]
    if pob_root:
        text = open(os.path.join(pob_root, "Modules", "ConfigOptions.lua"), encoding="utf-8", errors="replace").read()
        have = set(re.findall(r'var = "(\w+)"', text))
        bad += [f"{tok}: PoB has no config option {v}" for tok, t in TOKENS.items() for v in t.get("switch", {}) if v not in have]
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="state registry for the mechanics graph")
    ap.add_argument("--build", metavar="POB_STATES_JSON", help="rebuild data/states.json from a PoB state extract")
    ap.add_argument("--source", default="", help="note on where the extract came from")
    ap.add_argument("--check", action="store_true", help="check the hand layer against the registry (and PoB)")
    ap.add_argument("--pob-root", help="PoB src dir, to check the config option names")
    a = ap.parse_args(argv)
    if a.build:
        doc = build(json.load(open(a.build)), a.source)
        json.dump(doc, open(PATH, "w"), separators=(",", ":"), sort_keys=True)
        print(json.dumps({"states": len(doc["states"])}))
    bad = check(a.pob_root) if a.check else []
    for b in bad:
        print(b)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
