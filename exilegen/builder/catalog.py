"""Catalogue for the skeleton generator: active skills, uniques, ascendancies, keystones + corpus priors.

    python3 -m exilegen.builder.catalog --pob <PoB-PoE2 root> [--corpus /mnt/project-files/corpus/poe2/] [--no-engine]

Regenerates the cached JSON under exilegen/builder/data/ (skills.json, uniques.json, tree.json, corpus.json).
The generator and the tests only read the cache (no PoB data, no engine, no corpus needed at run time).

Mod lines (unique current variants, keystone and ascendancy-notable stats) are parsed with PoB's own
ModParser through the engine `lua` op (modLib.parseMod) when an engine is available; each parsed mod is
reduced to {name, type, value, flags[], kw[], tags[]} with flags / keyword flags decoded to names.
Without an engine (--no-engine) mods are left unparsed and mechgraph falls back to its regex tagger.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DEFAULT_CORPUS = "/mnt/project-files/corpus/poe2/"

DAMAGE_TYPES = ("Physical", "Fire", "Cold", "Lightning", "Chaos")
SLOT_OF_TYPE = {
    "Amulet": "Amulet", "Belt": "Belt", "Body Armour": "Body Armour", "Boots": "Boots", "Gloves": "Gloves",
    "Helmet": "Helmet", "Ring": "Ring", "Jewel": "Jewel", "Flask": "Flask", "Charm": "Charm",
    "Shield": "Weapon 2", "Focus": "Weapon 2", "Quiver": "Weapon 2",
}
TWO_HANDED = {"Bow", "Crossbow", "Staff", "Quarterstaff", "Two Hand Axe", "Two Hand Mace", "Two Hand Sword", "Spear2H",
              "Talisman"}        # a talisman takes both hands (PoB PoE2 ItemsTab)
# weapon classes that occupy only the main hand (an off-hand: shield / focus / quiver / second 1h weapon is allowed)
ONE_HANDED = {"Claw", "Dagger", "Flail", "One Hand Axe", "One Hand Mace", "One Hand Sword", "Spear", "Wand",
              "Sceptre"}
# PoB skill weaponTypes key -> our weapon class (quarterstaff attacks are "Staff" in PoB skill data)
SKILL_WEAPON_ALIAS = {"Staff": "Quarterstaff"}
# activation-relevant skill types
_META_EXCLUDE = {"Meta", "Persistent", "HasReservation", "Minion", "CreatesMinion", "InbuiltTrigger", "Channel", "Triggered"}

# skills that are damaging in PoB data but are utility / defensive / movement in practice
UTILITY_SKILLS = {"Raise Shield", "Parry", "Rhoa Mount", "Disengage", "Escape Shot", "Fortifying Cry", "Arctic Armour",
                  "Moment of Vulnerability", "Bone Offering", "Pain Offering", "Soul Offering", "Leap Slam",
                  "Lightning Warp", "Profane Ritual", "Voltaic Barrier", "Resonating Shield", "Shield Wall", "Wind Blast",
                  "Flash Grenade", "Oil Grenade"}
_RANGE = re.compile(r"\((-?[0-9.]+)-(-?[0-9.]+)\)")
_TAGS = re.compile(r"\{[^}]*\}")
_META_ROW = re.compile(r"^(Variant|Implicits|Requires|League|Source|Selected Variant|LevelReq|Has Alt Variant|Sockets|"
                       r"Radius|Limited to|Item Level|Quality|Corrupted|Unreleased|Rune:|Prefix|Suffix|Crafted)")


# ---------------------------------------------------------------------------------------------- PoB readers
def _read_bases(pob_root: str) -> dict[str, dict]:
    out = {}
    for path in glob.glob(os.path.join(pob_root, "Data", "Bases", "*.lua")):
        text = open(path, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r'itemBases\["([^"]+)"\] = \{(.*?)\n\}', text, re.S):
            b = m.group(2)
            t = re.search(r'\ttype = "([^"]+)"', b)
            st = re.search(r'\tsubType = "([^"]+)"', b)
            tags = re.search(r'tags = \{(.*?)\}', b)
            out[m.group(1)] = {"type": t.group(1) if t else None, "subType": st.group(1) if st else None,
                               "tags": re.findall(r'(\w+) = true', tags.group(1)) if tags else []}
    return out


def _weapon_class(base: dict | None) -> str | None:
    if not base:
        return None
    t, st, tags = base.get("type"), base.get("subType"), base.get("tags") or []
    if t == "Staff" and (st == "Warstaff" or "warstaff" in tags):
        return "Quarterstaff"
    if t == "Spear" and "two_hand_weapon" in tags:
        return "Spear2H"
    return t


def _read_skills(pob_root: str) -> list[dict]:
    from exilegen.scoring import pobdata
    raw = pobdata.read_skills(pob_root)
    weapon = {}
    for path in sorted(glob.glob(os.path.join(pob_root, "Data", "Skills", "*.lua"))):
        text = open(path, encoding="utf-8", errors="replace").read()
        heads = list(re.finditer(r'^skills\["([^"]+)"\]\s*=\s*\{', text, re.M))
        for i, m in enumerate(heads):
            body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
            wt = re.search(r'^\tweaponTypes = \{(.*?)^\t\},', body, re.M | re.S)
            if wt:
                weapon[m.group(1)] = sorted({SKILL_WEAPON_ALIAS.get(x, x) for x in re.findall(r'\["([^"]+)"\] = true', wt.group(1))})
    # gem tiers
    tiers = {}
    gtext = open(os.path.join(pob_root, "Data", "Gems.lua"), encoding="utf-8", errors="replace").read()
    for m in re.finditer(r'\n\t\["([^"]+)"\] = \{(.*?)\n\t\},', gtext, re.S):
        ge = re.search(r'grantedEffectId = "([^"]+)"', m.group(2))
        tier = re.search(r'\tTier = (\d+)', m.group(2))
        if ge:
            tiers.setdefault(ge.group(1), (m.group(1), int(tier.group(1)) if tier else None))
    out = []
    for sid, sd in raw.items():
        if sd.support:
            continue
        t = set(sd.skill_types)
        gem_id, tier = tiers.get(sid, (None, None))
        out.append({
            "id": sid, "name": sd.gem_name or sd.name, "gem_id": gem_id, "tier": tier, "is_gem": gem_id is not None,
            "hidden": sd.hidden, "types": sd.skill_types, "gem_tags": sd.gem_tags, "cast_time": sd.cast_time,
            "weapon_types": weapon.get(sid, []), "description": sd.description[:400], "file": sd.file,
            "stat_ids": [s for s in sd.stat_ids if not s.startswith("display_")][:60],
            "damage_types": [d for d in DAMAGE_TYPES if d in t],
        })
    return out


def _resolve_ranges(line: str) -> str:
    return _RANGE.sub(lambda m: m.group(2), line)


def _read_uniques(pob_root: str, bases: dict) -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(pob_root, "Data", "Uniques", "*.lua"))):
        fname = os.path.basename(path)
        text = open(path, encoding="utf-8", errors="replace").read()
        for block in re.findall(r'\[\[\n(.*?)\]\]', text, re.S):
            rows = [r.rstrip() for r in block.split("\n") if r.strip()]
            if len(rows) < 2:
                continue
            name = rows[0].strip()
            base_name = next((_TAGS.sub("", r).strip() for r in rows[1:]
                              if _TAGS.sub("", r).strip() in bases), _TAGS.sub("", rows[1]).strip())
            variants = [r for r in rows if r.startswith("Variant:")]
            cur = None
            for i, v in enumerate(variants, 1):
                if "Current" in v:
                    cur = i
            if cur is None and variants:
                cur = len(variants)    # no "Current" label: newest variant (PoB lists oldest first)
            n_impl = 0
            mi = next((r for r in rows if r.startswith("Implicits:")), None)
            if mi:
                n_impl = int(re.sub(r"\D", "", mi) or 0)
            lines, implicit_lines = [], []
            body_rows = [r for r in rows[2:] if not _META_ROW.match(r)]
            for j, r in enumerate(body_rows):
                vm = re.match(r'^\{variant:([0-9,]+)\}', r)
                if vm and cur is not None and str(cur) not in vm.group(1).split(","):
                    continue
                clean = _TAGS.sub("", r).strip()
                if not clean:
                    continue
                (implicit_lines if j < n_impl else lines).append(clean)
            base = bases.get(base_name)
            wclass = _weapon_class(base)
            btype = base.get("type") if base else None
            slot = SLOT_OF_TYPE.get(btype or "", None)
            if slot is None and wclass:
                slot = "Weapon 1"
            granted = []
            for ln in implicit_lines + lines:
                gm = re.match(r"Grants Skill: (?:Level \S+ )?(.+)$", ln)
                if gm:
                    granted.append(gm.group(1).strip())
            out.append({
                "name": name, "base": base_name, "base_type": btype, "weapon_class": wclass if slot == "Weapon 1" else None,
                "slot": slot, "two_handed": (wclass in TWO_HANDED) if slot == "Weapon 1" else False,
                "file": fname, "implicits": implicit_lines, "lines": lines, "granted_skills": granted,
                "base_tags": (base or {}).get("tags", []), "base_subtype": (base or {}).get("subType"),
            })
    # dedupe by name (keep the first; PoB has a handful of duplicates across files)
    seen, uniq = set(), []
    for u in out:
        if u["name"] not in seen:
            seen.add(u["name"])
            uniq.append(u)
    return uniq


def _read_tree(pob_root: str, version: str = "0_5") -> dict:
    t = json.load(open(os.path.join(pob_root, "TreeData", version, "tree.json"), encoding="utf-8"))
    classes = {c["name"]: [a["id"] for a in c.get("ascendancies", [])] for c in t["classes"]}
    keystones, asc_nodes = {}, defaultdict(list)
    for nid, n in t["nodes"].items():
        if not isinstance(n, dict):
            continue
        stats = [s.replace("\n", " ") for s in n.get("stats", [])]
        if n.get("isKeystone") and not n.get("ascendancyName"):
            keystones.setdefault(n["name"], {"name": n["name"], "id": int(nid) if str(nid).isdigit() else nid, "stats": stats})
        elif n.get("ascendancyName") and n.get("isNotable"):
            asc_nodes[n["ascendancyName"]].append({"name": n["name"], "id": nid, "stats": stats})
    asc = {}
    for cls, ascs in classes.items():
        for a in ascs:
            asc[a] = {"name": a, "class": cls, "notables": sorted(asc_nodes.get(a, []), key=lambda x: x["name"])}
    return {"classes": classes, "ascendancies": asc, "keystones": sorted(keystones.values(), key=lambda k: k["name"])}


# ---------------------------------------------------------------------------------------------- ModParser via engine
_LUA_PARSE = r'''
local dk = require("dkjson")
local function clean(v, d)
  if d > 6 then return nil end
  local t = type(v)
  if t == "table" then local o = {} for k, x in pairs(v) do o[tostring(k)] = clean(x, d + 1) end return o
  elseif t == "function" then return "<fn>" else return v end
end
local res = {}
for i, line in ipairs(LINES) do
  local ok, m, extra = pcall(modLib.parseMod, line)
  if ok then res[i] = {mods = clean(m or {}, 0), extra = extra} else res[i] = {err = tostring(m)} end
end
return dk.encode({res = res, mf = ModFlag, kf = KeywordFlag, st = SkillType})
'''


def _bits(v: int, table: dict) -> list[str]:
    if not v:
        return []
    return sorted(k for k, b in table.items() if b and b & (b - 1) == 0 and v & b and k not in ("MatchAll",))


def _compact(mod: dict, mf: dict, kf: dict, st_rev: dict) -> dict:
    o = {"name": mod.get("name"), "type": mod.get("type")}
    v = mod.get("value")
    if isinstance(v, (int, float, bool)):
        o["value"] = v
    elif isinstance(v, dict):
        if "mod" in v and isinstance(v["mod"], dict):
            o["inner"] = _compact(v["mod"], mf, kf, st_rev)
        elif "skillId" in v:
            o["skill"] = v.get("skillId")
        else:
            o["value"] = {k: x for k, x in v.items() if isinstance(x, (str, int, float, bool))}
    f, k = _bits(int(mod.get("flags") or 0), mf), _bits(int(mod.get("keywordFlags") or 0), kf)
    if f:
        o["flags"] = f
    if k:
        o["kw"] = k
    tags = []
    i = 1
    while str(i) in mod:
        tg = mod[str(i)]
        if isinstance(tg, dict):
            c = {x: tg[x] for x in ("type", "var", "stat", "actor", "skillName", "neg", "div", "limit", "varList")
                 if x in tg and not isinstance(tg[x], dict)}
            if "skillType" in tg:
                c["skillType"] = st_rev.get(tg["skillType"], tg["skillType"])
            if isinstance(tg.get("varList"), dict):
                c["varList"] = list(tg["varList"].values())
            tags.append(c)
        i += 1
    if tags:
        o["tags"] = tags
    return o


def parse_lines(lines: list[str], engine, chunk: int = 400) -> dict[str, dict]:
    """line -> {"mods": [compact mod...], "unparsed": extra-text-or-None}."""
    out: dict[str, dict] = {}
    todo = sorted(set(lines))
    for s in range(0, len(todo), chunk):
        part = todo[s:s + chunk]
        code = "LINES={" + ",".join(json.dumps(x) for x in part) + "}\n" + _LUA_PARSE
        r = engine.lua(code)
        mf, kf, st = r["mf"], r["kf"], r["st"]
        st_rev = {v: k for k, v in st.items()}
        res = r["res"]
        items = res.items() if isinstance(res, dict) else enumerate(res, 1)
        for idx, x in items:
            line = part[int(idx) - 1]
            mods = x.get("mods") or {}
            ml = [mods[k] for k in sorted(mods, key=lambda z: int(z))] if isinstance(mods, dict) else list(mods)
            out[line] = {"mods": [_compact(m, mf, kf, st_rev) for m in ml if isinstance(m, dict)],
                         "unparsed": (x.get("extra") or None) if not x.get("err") else x["err"][:200]}
    return out


# ---------------------------------------------------------------------------------------------- corpus priors
def _f(x, d=0.0):
    try:
        v = float(x)
        return d if math.isnan(v) else v
    except (TypeError, ValueError):
        return d


def build_corpus(corpus: str) -> dict:
    """Popularity + power priors and co-occurrence counts from the poe.ninja corpus."""
    c = corpus.rstrip("/") + "/"
    ov = json.load(open(c + "overview.json"))
    dims = ov["dimensions"]
    out: dict = {"total_characters": ov.get("totalCharacters"),
                 "ninja": {k: dict(dims[k]["counts"]) for k in ("class", "skills", "items", "keypassives", "allskills")},
                 "skill_by_asc": {k: dict(v["byAscendancy"]) for k, v in (ov.get("skillByAscendancy") or {}).items()}}
    # power per skill / ascendancy (pinnacle-locked PoB numbers)
    for key, fn in (("power_skill", "analysis-meta/power_by_skill.csv"), ("power_asc", "analysis-meta/power_by_ascendancy.csv")):
        rows = {}
        for r in csv.DictReader(open(c + fn)):
            rows[r["name"]] = {"n": int(_f(r["n"])), "median_dps": _f(r["median_dps"]), "p90_dps": _f(r["p90_dps"]),
                               "median_ehp": _f(r["median_ehp"]), "ninja_pct": _f(r["ninja_pct"]), "flag": r.get("flag") or ""}
        out[key] = rows
    # per-build co-occurrence (sample) from evaluated.jsonl tags
    flat = {int(r["line"]): r for r in csv.DictReader(open(c + "analysis-meta/builds_flat.csv"))}
    pair = defaultdict(Counter)   # kind -> Counter["a||b"]
    single = defaultdict(Counter)
    combo_power = defaultdict(list)  # "skill||asc" -> [dps]
    n = 0
    for line in open(c + "evaluated.jsonl"):
        d = json.loads(line)
        tg = d.get("tags") or {}
        meta = d.get("meta") or {}
        fr = flat.get(d.get("line"))
        skill = (fr or {}).get("pob_main_skill") or meta.get("main_skill")
        asc = tg.get("ascendancy") or meta.get("class")
        uniques = sorted({i["name"] for i in tg.get("items") or [] if i.get("rarity") == "UNIQUE"})
        keys = sorted({k["name"] for k in tg.get("keystones") or []})
        n += 1
        single["skill"][skill] += 1
        single["asc"][asc] += 1
        for u in uniques:
            single["unique"][u] += 1
            pair["unique_skill"][f"{u}||{skill}"] += 1
            pair["unique_asc"][f"{u}||{asc}"] += 1
            for u2 in uniques:
                if u < u2:
                    pair["unique_unique"][f"{u}||{u2}"] += 1
        for k in keys:
            single["keystone"][k] += 1
            pair["keystone_skill"][f"{k}||{skill}"] += 1
        pair["skill_asc"][f"{skill}||{asc}"] += 1
        if fr:
            combo_power[f"{skill}||{asc}"].append(_f(fr.get("pinnacle_dps")))
    out["sample_n"] = n
    out["single"] = {k: dict(v) for k, v in single.items()}
    out["pair"] = {k: dict(v) for k, v in pair.items()}
    out["combo_p90"] = {k: sorted(v)[int(0.9 * (len(v) - 1))] for k, v in combo_power.items() if v}
    # analysis tables used as seeds
    out["antimeta"] = [r for r in csv.DictReader(open(c + "analysis-synergy/antimeta_candidates.csv"))][:600]
    out["uniques_buildaround"] = [r for r in csv.DictReader(open(c + "analysis-synergy/uniques.csv"))]
    out["interactions"] = [r for r in csv.DictReader(open(c + "analysis-synergy/interaction_candidates.csv"))]
    out["enablers"] = [r for r in csv.DictReader(open(c + "analysis-meta/constraint_enablers.csv"))]
    return out


# ---------------------------------------------------------------------------------------------- build / load
def build(pob_root: str, corpus: str | None = DEFAULT_CORPUS, engine=None, out_dir: str = DATA_DIR) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    bases = _read_bases(pob_root)
    skills = _read_skills(pob_root)
    uniques = _read_uniques(pob_root, bases)
    tree = _read_tree(pob_root)
    parsed = {}
    if engine is not None:
        lines = [ln for u in uniques for ln in u["implicits"] + u["lines"]]
        lines += [s for k in tree["keystones"] for s in k["stats"]]
        lines += [s for a in tree["ascendancies"].values() for nd in a["notables"] for s in nd["stats"]]
        parsed = parse_lines(lines, engine)
        # resolve range text "(10-20)%" to the top roll before parsing, keep the original line as the key
        rl = {ln: _resolve_ranges(ln) for ln in lines if _RANGE.search(ln)}
        if rl:
            p2 = parse_lines(list(set(rl.values())), engine)
            for orig, res in rl.items():
                parsed[orig] = p2.get(res, parsed.get(orig))
    # map unique-granted skill names to skill ids (ExtraSkill mods give the exact id)
    by_name = defaultdict(list)
    for s in skills:
        by_name[s["name"].lower()].append(s["id"])
    for u in uniques:
        gids = []
        for ln in u["implicits"] + u["lines"]:
            for m in (parsed.get(ln) or {}).get("mods", []):
                if m.get("name") == "ExtraSkill" and m.get("skill"):
                    gids.append(m["skill"])
        if not gids:
            for g in u["granted_skills"]:
                ids = by_name.get(g.lower(), [])
                gids.append(next((i for i in ids if i.startswith("Unique") or "Unique" in i), ids[0] if ids else g))
        u["granted_skill_ids"] = gids
    granted_by = defaultdict(list)
    for u in uniques:
        for gid in u["granted_skill_ids"]:
            granted_by[gid].append(u["name"])
    asc_granted = defaultdict(list)
    for a in tree["ascendancies"].values():
        for nd in a["notables"]:
            for st in nd["stats"]:
                gm = re.match(r"Grants Skill: (.+)$", st)
                if gm:
                    asc_granted[gm.group(1).strip().lower()].append([a["name"], nd["name"]])
    for s in skills:
        s["granted_by"] = granted_by.get(s["id"], [])
        s["asc_granted"] = asc_granted.get(s["name"].lower(), []) if s["is_gem"] else []
    json.dump({"skills": skills}, open(os.path.join(out_dir, "skills.json"), "w"), indent=0)
    json.dump({"uniques": uniques}, open(os.path.join(out_dir, "uniques.json"), "w"), indent=0)
    json.dump(tree, open(os.path.join(out_dir, "tree.json"), "w"), indent=0)
    json.dump(parsed, open(os.path.join(out_dir, "parsed_mods.json"), "w"), separators=(",", ":"))
    info = {"skills": len(skills), "uniques": len(uniques), "ascendancies": len(tree["ascendancies"]),
            "keystones": len(tree["keystones"]), "parsed_lines": len(parsed)}
    if corpus and os.path.exists(corpus.rstrip("/") + "/overview.json"):
        cp = build_corpus(corpus)
        json.dump(cp, open(os.path.join(out_dir, "corpus.json"), "w"), separators=(",", ":"))
        info["corpus_builds"] = cp["sample_n"]
    return info


@dataclass
class Catalog:
    skills: dict[str, dict]
    uniques: dict[str, dict]
    classes: dict[str, list[str]]
    ascendancies: dict[str, dict]
    keystones: dict[str, dict]
    parsed: dict[str, dict]
    corpus: dict = field(default_factory=dict)

    # ---- activation potential (what a skill can be in a build) -----------------------------------------
    @staticmethod
    def activations(s: dict) -> list[str]:
        t = set(s["types"])
        dmg = bool({"Damage", "Attack", "DamageOverTime", "DegenOnlySpellDamage"} & t)
        acts = []
        if ("CreatesMinion" in t or "Companion" in t) and not ("Spell" in t and dmg and "Persistent" not in t):
            acts.append("minion")
        if "Persistent" in t and dmg and "Meta" not in t:
            acts.append("persistent")
        if "InbuiltTrigger" in t or "Triggered" in t:
            acts.append("inbuilt_trigger")
        if dmg and "Spell" in t and "Triggerable" in t and not (t & _META_EXCLUDE):
            acts.append("meta_trigger")
        if dmg and "Totemable" in t:
            acts.append("totem")
        if dmg and not ({"Persistent", "Triggered", "InbuiltTrigger", "Meta"} & t) and ("CreatesMinion" not in t or "Spell" in t):
            acts.append("manual")
        return acts

    @staticmethod
    def is_damaging(s: dict) -> bool:
        return bool({"Damage", "Attack", "DamageOverTime", "DegenOnlySpellDamage"} & set(s["types"]))

    def main_skill_candidates(self) -> list[dict]:
        """Skills that can be a build's main skill: a real gem or unique-granted, damaging or minion-creating.
        One entry per (name, granted-by) — weapon-variant duplicates collapse to the most general variant."""
        if getattr(self, "_mains", None) is not None:
            return self._mains
        out = []
        ninja = self.corpus.get("ninja", {}).get("allskills", {})
        for s in sorted(self.skills.values(), key=lambda x: (x["name"], -len(x["weapon_types"]), x["id"])):
            if s["name"] in UTILITY_SKILLS:
                continue
            t = set(s["types"])
            if s["hidden"] and not s["granted_by"]:
                continue
            if not (self.is_damaging(s) or "CreatesMinion" in t or "Companion" in t):
                continue
            if "Meta" in t or "Aura" in t and not self.is_damaging(s):
                continue
            avail = bool(s["granted_by"]) or (s["is_gem"] and ((s["tier"] or 0) > 0 or s["name"] in ninja))
            if not avail or s["id"].startswith("WeaponGranted") or s["id"].endswith("Monster"):
                continue
            acts = self.activations(s)
            if not acts or (acts == ["inbuilt_trigger"] and not s["granted_by"] and not s.get("asc_granted")):
                continue
            if any(o["name"] == s["name"] and bool(o["granted_by"]) == bool(s["granted_by"]) for o in out[-3:]):
                continue
            out.append(s)
        self._mains = out
        return out

    def skill(self, ref: str) -> dict | None:
        if ref in self.skills:
            return self.skills[ref]
        r = ref.lower()
        best = None
        for s in self.skills.values():
            if s["name"].lower() == r:
                if s["is_gem"] or s["granted_by"]:
                    return s
                best = best or s
        return best

    def class_of(self, asc: str) -> str | None:
        a = self.ascendancies.get(asc)
        return a["class"] if a else None

    def unique_mods(self, name: str) -> list[tuple[str, list[dict]]]:
        u = self.uniques[name]
        return [(ln, (self.parsed.get(ln) or {}).get("mods", [])) for ln in u["implicits"] + u["lines"]]


_LOADED: dict[str, Catalog] = {}


def load(data_dir: str = DATA_DIR) -> Catalog:
    if data_dir in _LOADED:
        return _LOADED[data_dir]
    sk = json.load(open(os.path.join(data_dir, "skills.json")))["skills"]
    un = json.load(open(os.path.join(data_dir, "uniques.json")))["uniques"]
    tr = json.load(open(os.path.join(data_dir, "tree.json")))
    pp = os.path.join(data_dir, "parsed_mods.json")
    cp = os.path.join(data_dir, "corpus.json")
    cat = Catalog(skills={s["id"]: s for s in sk}, uniques={u["name"]: u for u in un}, classes=tr["classes"],
                  ascendancies=tr["ascendancies"], keystones={k["name"]: k for k in tr["keystones"]},
                  parsed=json.load(open(pp)) if os.path.exists(pp) else {},
                  corpus=json.load(open(cp)) if os.path.exists(cp) else {})
    _LOADED[data_dir] = cat
    return cat


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pob", default=os.environ.get("EXG_POB_ROOT"))
    ap.add_argument("--corpus", default=DEFAULT_CORPUS)
    ap.add_argument("--out", default=DATA_DIR)
    ap.add_argument("--no-engine", action="store_true")
    a = ap.parse_args(argv)
    eng = None
    if not a.no_engine:
        from exilegen.engine import Engine
        eng = Engine(pob_root=a.pob)
    try:
        info = build(a.pob, a.corpus, eng, a.out)
    finally:
        if eng is not None:
            eng.close()
    print(json.dumps(info))


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------------------------- legality helpers
SLOT_LIMIT = {"Ring": 2, "Jewel": 3, "Flask": 2, "Charm": 3}


def slot_conflicts(uniques: list[dict]) -> list[str]:
    """Problems with a set of unique items (dicts with slot / two_handed / weapon_class / base_type).
    Max one per slot (rings x2), a two-handed weapon blocks an off-hand except bow + quiver,
    a quiver needs a bow, at most one weapon unique (we do not dual-wield uniques)."""
    out = []
    cnt = Counter(u.get("slot") for u in uniques)
    for slot, n in cnt.items():
        if slot is None:
            out.append("unique without slot")
        elif n > SLOT_LIMIT.get(slot, 1):
            out.append(f"{n} uniques in {slot}")
    names = [u.get("name") for u in uniques]
    if len(set(names)) != len(names):
        out.append("duplicate unique")
    w1 = next((u for u in uniques if u.get("slot") == "Weapon 1"), None)
    w2 = next((u for u in uniques if u.get("slot") == "Weapon 2"), None)
    if w1 and w2 and w1.get("two_handed"):
        if not (w2.get("base_type") == "Quiver" and w1.get("weapon_class") == "Bow"):
            out.append(f"two-handed {w1.get('name')} with off-hand {w2.get('name')}")
    if w2 and w2.get("base_type") == "Quiver" and not (w1 and w1.get("weapon_class") == "Bow"):
        if w1 is not None:
            out.append("quiver without bow")
    return out


def weapon_ok(skill: dict, weapon_class: str | None) -> bool:
    """Can `skill` be used with a main-hand weapon of `weapon_class` (None = no weapon decided yet)?"""
    t = set(skill.get("types") or [])
    wt = skill.get("weapon_types") or []
    if not wt or "Attack" not in t or "NonWeaponAttack" in t:
        return True
    if weapon_class is None:
        return True
    return weapon_class in wt or (weapon_class == "Spear2H" and "Spear" in wt)
