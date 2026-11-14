"""Minimal reader for the PoB-PoE2 Lua data the scoring layer needs (no Lua runtime).

Reads Data/Skills/*.lua (granted effects), Data/Gems.lua (which effects are real gems),
TreeData/<ver>/tree.lua (keystones) and Data/Uniques/*.lua (unique mod text).
Regex based: good enough for the generated, regularly formatted PoB data files.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field

_SKILL_HDR = re.compile(r'^skills\["([^"]+)"\]\s*=\s*\{', re.M)
_STR = r'"((?:[^"\\]|\\.)*)"'
_STAT_ID = re.compile(r'"([a-z][a-z0-9_]*(?:_[a-z0-9_+%]+)+)"')


@dataclass
class SkillData:
    id: str
    name: str
    description: str = ""
    support: bool = False
    hidden: bool = False
    cast_time: float | None = None
    skill_types: list[str] = field(default_factory=list)
    stat_ids: list[str] = field(default_factory=list)
    constant_stats: dict[str, float] = field(default_factory=dict)
    gem_name: str | None = None
    gem_tags: list[str] = field(default_factory=list)
    file: str = ""


def _unescape(s: str) -> str:
    return s.replace('\\n', ' ').replace('\\"', '"').replace("\\'", "'")


def read_skills(pob_root: str) -> dict[str, SkillData]:
    out: dict[str, SkillData] = {}
    for path in sorted(glob.glob(os.path.join(pob_root, "Data", "Skills", "*.lua"))):
        text = open(path, encoding="utf-8", errors="replace").read()
        heads = list(_SKILL_HDR.finditer(text))
        for i, m in enumerate(heads):
            body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
            name = re.search(r'^\tname = ' + _STR, body, re.M)
            desc = re.search(r'^\tdescription = ' + _STR, body, re.M)
            ct = re.search(r'^\tcastTime = ([0-9.]+)', body, re.M)
            st = re.search(r'^\tskillTypes = \{(.*?)\},?\s*$', body, re.M)
            cs = {}
            for cm in re.finditer(r'\{ "([^"]+)", (-?[0-9.]+) \}', body):
                cs.setdefault(cm.group(1), float(cm.group(2)))
            stat_ids = sorted({x for x in _STAT_ID.findall(body) if "_" in x})
            out[m.group(1)] = SkillData(
                id=m.group(1),
                name=_unescape(name.group(1)) if name else m.group(1),
                description=_unescape(desc.group(1)) if desc else "",
                support=bool(re.search(r'^\tsupport = true', body, re.M)),
                hidden=bool(re.search(r'^\thidden = true', body, re.M)),
                cast_time=float(ct.group(1)) if ct else None,
                skill_types=re.findall(r'SkillType\.(\w+)', st.group(1)) if st else [],
                stat_ids=stat_ids,
                constant_stats=cs,
                file=os.path.basename(path),
            )
    # attach gem info
    gems_path = os.path.join(pob_root, "Data", "Gems.lua")
    if os.path.exists(gems_path):
        text = open(gems_path, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r'\n\t\["[^"]+"\] = \{(.*?)\n\t\},', text, re.S):
            b = m.group(1)
            ge = re.search(r'grantedEffectId = "([^"]+)"', b)
            nm = re.search(r'\tname = ' + _STR, b)
            if not ge or ge.group(1) not in out:
                continue
            sd = out[ge.group(1)]
            if sd.gem_name is None:
                sd.gem_name = _unescape(nm.group(1)) if nm else sd.name
                tags = re.search(r'tags = \{(.*?)\}', b, re.S)
                sd.gem_tags = re.findall(r'(\w+) = true', tags.group(1)) if tags else []
    return out


@dataclass
class Keystone:
    id: int
    name: str
    stats: list[str]


def read_keystones(pob_root: str, tree_version: str = "0_5") -> list[Keystone]:
    path = os.path.join(pob_root, "TreeData", tree_version, "tree.lua")
    if not os.path.exists(path):
        return []
    text = open(path, encoding="utf-8", errors="replace").read()
    out = []
    for m in re.finditer(r'\n\t\t\[(\d+)\]=\{\n(.*?)\n\t\t\}', text, re.S):
        body = m.group(2)
        if "isKeystone=true" not in body or "ascendancyName" in body:
            continue
        nm = re.search(r'\bname="([^"]*)"', body)
        stats = re.findall(r'\[\d+\]="((?:[^"\\]|\\.)*)"', body.split("stats={", 1)[1]) if "stats={" in body else []
        out.append(Keystone(int(m.group(1)), nm.group(1) if nm else "?", [_unescape(s) for s in stats]))
    # de-duplicate by name (some keystones appear in several node slots)
    seen, uniq = set(), []
    for k in out:
        if k.name not in seen:
            seen.add(k.name)
            uniq.append(k)
    return uniq


@dataclass
class Unique:
    name: str
    base: str
    lines: list[str]
    file: str


def read_uniques(pob_root: str) -> list[Unique]:
    out = []
    for path in sorted(glob.glob(os.path.join(pob_root, "Data", "Uniques", "*.lua"))):
        text = open(path, encoding="utf-8", errors="replace").read()
        for block in re.findall(r'\[\[\n(.*?)\]\]', text, re.S):
            rows = [r for r in block.split("\n") if r.strip()]
            if len(rows) < 2:
                continue
            # keep current-variant lines only when variants exist
            variants = [r for r in rows if r.startswith("Variant:")]
            cur = None
            for i, v in enumerate(variants, 1):
                if "Current" in v:
                    cur = i
            lines = []
            for r in rows[2:]:
                if re.match(r'^(Variant|Implicits|Requires|League|Source|Selected Variant|LevelReq|Has Alt Variant)', r):
                    continue
                vm = re.match(r'^\{variant:([0-9,]+)\}', r)
                if vm and cur is not None and str(cur) not in vm.group(1).split(","):
                    continue
                lines.append(re.sub(r'\{[^}]*\}', '', r).strip())
            out.append(Unique(rows[0].strip(), rows[1].strip(), lines, os.path.basename(path)))
    return out
