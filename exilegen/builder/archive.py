"""MAP-Elites archive for the from-scratch meta builder (docs/build-search-design.md sections 3, 4, 6, 7).

    arch = Archive.open("runs/one_button_a")          # replays runs/<name>/archive.jsonl when resuming
    status = arch.add(result)                          # BuildResult -> "new_cell" | "improved" | "front" | ...
    arch.feedback()                                    # -> generator feedback dict
    arch.stats()                                       # coverage, QD-score, per-source success rates
    arch.checkpoint()                                  # writes index.json (cells -> record ids, counters)

Behaviour descriptor (describe(result)), one cell per distinct tuple:
    damage      dominant damage type(s) of the main skill: "lightning", "cold+fire", "mixed", with a
                "dot:" prefix when damage over time (ignite/poison/bleed/dot) is most of the DPS
    activation  manual | triggered | persistent | minion | totem | trap | mine  (main DPS source)
    defence     pool x mitigation: life|es|hybrid|ci|mom  x  armour|evasion|block|none   ("es/evasion")
    unique      the defining unique: first unique the skeleton forces, else "none"
    ascendancy  skeleton ascendancy
    corpus      rarity of (main skill, ascendancy) in the poe.ninja corpus overview:
                meta (>=10% of that ascendancy) | common (>=2%) | niche (>=0.2%) | rare (>0) | unseen

Each cell keeps a small Pareto set (honest DPS, EHP, -cost tier) per quality tier:
    tier 0 feasible + verified, tier 1 feasible + unverified (stored, marked, ranked below tier 0),
    tier 2 infeasible (kept only while the cell has no feasible entry: shows what almost worked).
The cell elite is the best-objective entry of the lowest non-empty tier.

Persistence (run directory):
    archive.jsonl   append-only, one record per evaluated skeleton (also failures): the source of truth
    index.json      snapshot: cells -> record uids per tier, counters, source stats (rewritten by checkpoint)
Re-opening a run directory replays archive.jsonl, so a run is resumable after a crash or a stop.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from .contracts import BuildResult, Skeleton, main_skill_matches

CORPUS_OVERVIEW = "/mnt/project-files/corpus/poe2/overview.json"
FINE_AXES = ("damage", "activation", "defence", "unique", "ascendancy", "corpus")
# Coarse cells (the default): few enough that builds of one archetype meet in the same cell and compete,
# instead of each unique/ascendancy/corpus combination getting a cell of its own (fine axes: 157 cells for 213
# records, 43 with a second record). damage_family folds "dot:fire" -> "dot", "cold+fire" -> "cold";
# pool is the defence pool without the mitigation layer; class is the base class.
COARSE_AXES = ("damage_family", "activation", "pool", "class")
AXES_PRESETS = {"coarse": COARSE_AXES, "fine": FINE_AXES}
DEFAULT_AXES = COARSE_AXES
AXES = FINE_AXES + ("damage_family", "pool", "class")      # every descriptor axis (reports, coverage, feedback)
# Quarantine. A build whose measured main skill is not the skeleton's (wrong_main) is ranked with the unverified
# builds in tier 1.
# A "feasible" build with no honest damage (its main skill is a buff, herald or utility that PoB
# gives 0 DPS) passes the defence floors on EHP alone. It is kept in tier 2 (like an infeasible build) so it
# cannot hold a cell, count as a feasible cell or add to the QD score.
QUARANTINE_MIN_DPS = 0.0
PARETO_KEYS = ("dps_honest", "ehp", "neg_cost")
COST_TIER = {"low": 0, "mid": 1, "high": 2}
MAX_FRONT = {0: 6, 1: 3, 2: 2}          # entries kept per tier per cell
DAMAGE_TYPES = ("Physical", "Fire", "Cold", "Lightning", "Chaos")
KNOWN_PROFILES = {"pinnacle_v1", "mapping_v1", "as_exported", "pinnacle_locked"}
CORPUS_BUCKETS = ("meta", "common", "niche", "rare", "unseen")


# ---------------------------------------------------------------------------------------------- helpers
def _num(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)) and v == v and v not in (float("inf"), float("-inf")):
        return float(v)
    return None


def norm_name(s: str | None) -> str:
    """'CometPlayer' / 'Comet' / 'comet' -> 'comet'; 'Summon Infernal Hound' -> 'summoninfernalhound'."""
    s = str(s or "")
    s = re.sub(r"(Player|PlayerTwo|Minion)$", "", s)
    return re.sub(r"[^a-z0-9]", "", s.lower())


def primary_doc(stats, prefer: str = "pinnacle_v1") -> dict:
    """One engine stats doc from BuildResult.stats: a doc, or {profile: doc}."""
    if not isinstance(stats, dict):
        return {}
    if stats and all(k in KNOWN_PROFILES for k in stats) and all(isinstance(v, dict) for v in stats.values()):
        return stats.get(prefer) or next(iter(stats.values()))
    return stats


def _skills_groups(skills) -> list[dict]:
    if isinstance(skills, dict):
        if isinstance(skills.get("groups"), list):
            return skills["groups"]
        if skills and all(k in KNOWN_PROFILES for k in skills):   # per-profile skills views
            return _skills_groups(next(iter(skills.values())))
    return []


def _jsonable(o):
    if is_dataclass(o):
        return asdict(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    if isinstance(o, float) and (math.isinf(o) or math.isnan(o)):
        return None
    return str(o)


# ---------------------------------------------------------------------------------------------- corpus
class Corpus:
    """Popularity of (main skill, ascendancy) from the poe.ninja overview aggregate."""

    def __init__(self, path: str | None = CORPUS_OVERVIEW):
        self.ok = False
        self.by_skill: dict[str, dict[str, int]] = {}
        self.skill_total: dict[str, int] = {}
        self.asc_total: dict[str, int] = {}
        self.item_total: dict[str, int] = {}
        self.total = 0
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError, TypeError):
            return
        self.total = int(d.get("totalCharacters") or 0)
        dims = d.get("dimensions") or {}
        self.asc_total = {norm_name(n): int(c) for n, c in (dims.get("class") or {}).get("counts") or []}
        self.skill_total = {norm_name(n): int(c) for n, c in (dims.get("skills") or {}).get("counts") or []}
        self.item_total = {norm_name(n): int(c) for n, c in (dims.get("items") or {}).get("counts") or []}
        for name, v in (d.get("skillByAscendancy") or {}).items():
            self.by_skill[norm_name(name)] = {norm_name(a): int(c) for a, c in v.get("byAscendancy") or []}
        self.total = self.total or sum(self.asc_total.values())
        self.ok = bool(self.asc_total)

    def share(self, skill: str | None, ascendancy: str | None) -> float | None:
        """Fraction of the ascendancy's characters that use the skill as a main skill (None: no corpus)."""
        if not self.ok:
            return None
        s, a = norm_name(skill), norm_name(ascendancy)
        at = self.asc_total.get(a)
        if not at:
            # unknown ascendancy (or a base class): fall back to the skill's global share
            return self.skill_total.get(s, 0) / max(1, self.total)
        if s in self.by_skill:
            return self.by_skill[s].get(a, 0) / at
        st = self.skill_total.get(s, 0)
        # not in the top-75 table: independence estimate, capped at the "niche" ceiling
        return min(0.019, st * at / max(1, self.total) / at) if st else 0.0

    def bucket(self, skill, ascendancy) -> str:
        sh = self.share(skill, ascendancy)
        if sh is None:
            return "unknown"
        if sh >= 0.10:
            return "meta"
        if sh >= 0.02:
            return "common"
        if sh >= 0.002:
            return "niche"
        if sh > 0:
            return "rare"
        return "unseen"

    def unique_share(self, name) -> float | None:
        if not self.ok:
            return None
        return self.item_total.get(norm_name(name), 0) / max(1, self.total)


_CORPUS: Corpus | None = None
_CORPUS_LOCK = threading.Lock()


def corpus(path: str | None = None) -> Corpus:
    global _CORPUS
    with _CORPUS_LOCK:
        if path is not None:
            return Corpus(path)
        if _CORPUS is None:
            _CORPUS = Corpus(os.environ.get("EXG_CORPUS_OVERVIEW") or CORPUS_OVERVIEW)
        return _CORPUS


# ---------------------------------------------------------------------------------------------- descriptor
def damage_bucket(doc: dict) -> str:
    st = (doc or {}).get("stats") or {}
    dps = (doc or {}).get("dps") or {}
    ms = (doc or {}).get("minionStats")
    if isinstance(ms, dict) and (_num(dps.get("minion")) or 0) > (_num(dps.get("player")) or 0):
        st = ms.get("stats") if isinstance(ms.get("stats"), dict) else ms     # minion build: the minion's damage
    by = {t: _num((st.get("HitDamageByType") or {}).get(t)) or 0.0 for t in DAMAGE_TYPES}
    hit_total = sum(by.values())
    total_dps = _num(st.get("CombinedDPS")) or 0.0
    dot = {"Fire": _num(st.get("IgniteDPS")) or 0.0, "Chaos": (_num(st.get("PoisonDPS")) or 0.0),
           "Physical": _num(st.get("BleedDPS")) or 0.0}
    dot_total = sum(dot.values())
    non_hit = _num(st.get("NonHitDPS"))
    if non_hit is None and total_dps and _num(st.get("TotalDPS")) is not None:
        non_hit = max(0.0, total_dps - st["TotalDPS"])
    non_hit = non_hit or 0.0
    hit_dps = max(0.0, total_dps - non_hit) if total_dps else hit_total
    # weight each type by its DPS share: hit share of the hit DPS + its DoT DPS
    mix = {}
    for t in DAMAGE_TYPES:
        mix[t] = (by[t] / hit_total * hit_dps if hit_total > 0 else 0.0) + dot.get(t, 0.0)
    if non_hit > dot_total and non_hit > 0 and hit_total == 0 and dot_total == 0:
        return "dot:unknown"
    tot = sum(mix.values())
    if tot <= 0:
        return "unknown"
    prefix = "dot:" if non_hit > 0.5 * max(total_dps, 1e-9) and total_dps > 0 else ""
    ranked = sorted(mix.items(), key=lambda kv: -kv[1])
    (t1, v1), (t2, v2) = ranked[0], ranked[1]
    if v1 / tot >= 0.6:
        return prefix + t1.lower()
    if (v1 + v2) / tot >= 0.8:
        return prefix + "+".join(sorted((t1.lower(), t2.lower())))
    return prefix + "mixed"


def activation_class(doc: dict, skills, skeleton: Skeleton | None = None) -> str:
    dps = (doc or {}).get("dps") or {}
    if (_num(dps.get("minion")) or 0) > (_num(dps.get("player")) or 0) and (_num(dps.get("minion")) or 0) > 0:
        return "minion"
    ms = (doc or {}).get("mainSkill") or {}
    main = (doc or {}).get("main") or {}
    gi, si = main.get("group"), main.get("skill")
    types = set(ms.get("types") or [])
    fact = None
    for g in _skills_groups(skills):
        if gi is not None and g.get("index") != gi:
            continue
        if gi is None and not g.get("main"):
            continue
        cands = g.get("skills") or []
        for f in cands:
            if (si is not None and f.get("index") == si) or (ms.get("id") and f.get("id") == ms.get("id")):
                fact = f
                break
        if fact is None:
            dmg = [f for f in cands if f.get("damaging")]
            fact = dmg[0] if dmg else (cands[0] if cands else None)
        break
    if fact is not None:
        types |= set(fact.get("types") or [])
        act = fact.get("activation")
        pk = set(fact.get("proxyKind") or [])
        if act == "proxy" or pk:
            for k in ("minion", "totem", "trap", "mine"):
                if k in pk:
                    return k
            if "SummonsTotem" in types:
                return "totem"
            return "minion"
        if act in ("manual", "triggered", "persistent"):
            return act
    # no skills view: infer from the main skill's types, then from the skeleton hints
    if "SummonsTotem" in types:
        return "totem"
    if types & {"Minion", "CreatesMinion"}:
        return "minion"
    if types & {"Triggered", "InbuiltTrigger"}:
        return "triggered"
    if types & {"Persistent"}:
        return "persistent"
    if types:
        return "manual"
    if skeleton is not None:
        hint = (skeleton.tags or {}).get("activation")
        if hint:
            return str(hint)
        if skeleton.trigger:
            return "triggered"
    return "manual"


def defence_bucket(doc: dict, skeleton: Skeleton | None = None) -> str:
    st = (doc or {}).get("stats") or {}
    ks = {norm_name(k.get("name") if isinstance(k, dict) else k) for k in (doc or {}).get("keystones") or []}
    if skeleton is not None:
        ks |= {norm_name(k) for k in skeleton.keystones}
    life = _num(st.get("Life")) or 0.0
    es = _num(st.get("EnergyShield")) or 0.0
    if not st and skeleton is not None and skeleton.defence:
        return skeleton.defence
    if "chaosinoculation" in ks:
        pool = "ci"
    elif "mindovermatter" in ks:
        pool = "mom"
    elif es > 2 * life:
        pool = "es"
    elif life > 2 * es:
        pool = "life"
    else:
        pool = "hybrid"
    block = _num(st.get("EffectiveBlockChance")) or _num(st.get("BlockChance")) or 0.0
    ar, ev = _num(st.get("Armour")) or 0.0, _num(st.get("Evasion")) or 0.0
    if block >= 40:
        mit = "block"
    elif max(ar, ev) < 1500:
        mit = "none"
    elif ar >= ev:
        mit = "armour"
    else:
        mit = "evasion"
    return f"{pool}/{mit}"


def defining_unique(skeleton: Skeleton | None) -> str:
    """The generator's tags.defining_unique, else the first unique the skeleton forces, else "none"."""
    du = ((skeleton.tags or {}).get("defining_unique") if skeleton else None)
    if du:
        return str(du.get("name") if isinstance(du, dict) else du)
    for u in (skeleton.uniques if skeleton else None) or []:
        n = u.get("name") if isinstance(u, dict) else u
        if n:
            return str(n)
    return "none"


def main_skill_name(doc: dict, skeleton: Skeleton | None) -> str:
    ms = (doc or {}).get("mainSkill") or {}
    return ms.get("name") or (skeleton.main_skill if skeleton else "") or "?"


def describe(result: BuildResult, corpus_obj: Corpus | None = None) -> dict:
    """Behaviour descriptor of a BuildResult (also stored on result.descriptor)."""
    sk = result.skeleton
    doc = primary_doc(result.stats)
    cp = corpus_obj or corpus()
    skill = main_skill_name(doc, sk)
    # rarity of the idea: the skeleton's intended main skill (the build's DPS skill may differ)
    rarity_skill = sk.main_skill or skill
    d = {
        "damage": damage_bucket(doc) if doc else "unknown",
        "activation": activation_class(doc, result.skills, sk),
        "defence": defence_bucket(doc, sk),
        "unique": defining_unique(sk),
        "ascendancy": sk.ascendancy or sk.cls or "?",
        "corpus": cp.bucket(rarity_skill, sk.ascendancy),
    }
    if d["damage"] in ("unknown", "dot:unknown"):     # no typed damage in PoB: the skeleton's intent, else "none"
        hint = (sk.tags or {}).get("damage") or getattr(sk, "primary_damage", None)
        d["damage"] = str(hint).lower() if hint else ("unknown" if not doc else "none")
    derive(d, sk.cls)
    sh = cp.share(rarity_skill, sk.ascendancy)
    d["main_skill"] = skill
    d["corpus_share"] = sh
    return d


def derive(d: dict, cls: str | None = None) -> dict:
    """Fill the coarse axes from the fine ones (in place; also for records written before they existed)."""
    dm = str(d.get("damage") or "unknown")
    d.setdefault("damage_family", "dot" if dm.startswith("dot:") else dm.split("+")[0])
    d.setdefault("pool", str(d.get("defence") or "?").split("/")[0])
    if cls or "class" not in d:
        d.setdefault("class", cls or "?")
    return d


def resolve_axes(spec=None) -> tuple:
    """Axes from a preset name ("coarse", "fine"), a comma list or a sequence of descriptor axes."""
    if spec is None or spec == "":
        return DEFAULT_AXES
    if isinstance(spec, str):
        if spec in AXES_PRESETS:
            return AXES_PRESETS[spec]
        spec = [x.strip() for x in spec.split(",") if x.strip()]
    out = tuple(spec)
    bad = [a for a in out if a not in AXES]
    if bad or not out:
        raise ValueError(f"unknown archive axes {bad or spec}; choose from {', '.join(AXES)} or "
                         f"{'/'.join(AXES_PRESETS)}")
    return out


def cell_key(desc: dict, axes=DEFAULT_AXES) -> str:
    return "|".join(str(desc.get(a, "?")) for a in axes)


# ---------------------------------------------------------------------------------------------- records
def slim_score(score: dict | None) -> dict | None:
    if not score:
        return score
    keep = ("profile", "feasible", "violations", "objective", "dps_raw", "dps_energy", "dps_honest", "ehp",
            "factors", "terms", "below_target", "flags", "verified", "binding_profile", "explain", "enemy_power")
    out = {k: score.get(k) for k in keep if k in score}
    if isinstance(score.get("profiles"), dict):
        out["profiles"] = {p: {k: r.get(k) for k in keep if k in r and k != "explain"}
                           for p, r in score["profiles"].items()}
    return out


def slim_stats(stats) -> Any:
    if not isinstance(stats, dict):
        return stats
    if stats and all(k in KNOWN_PROFILES for k in stats):
        return {p: slim_stats(v) for p, v in stats.items()}
    return {k: v for k, v in stats.items() if k not in ("scoring", "marginals", "unsupported_sample")}


def slim_skills(skills) -> Any:
    groups = _skills_groups(skills)
    if not groups:
        return skills if isinstance(skills, (list, type(None))) else None
    out = []
    for g in groups:
        out.append({
            "index": g.get("index"), "enabled": g.get("enabled", True), "main": g.get("main"),
            "metaGroup": g.get("metaGroup"), "grantedBy": g.get("grantedBy"),
            "gems": [{k: gm.get(k) for k in ("slot", "skillId", "id", "name", "support", "enabled", "level")}
                     for gm in g.get("gems") or []],
            "skills": [{k: f.get(k) for k in ("index", "name", "id", "gemSlot", "types", "activation", "proxyKind",
                                               "damaging", "spell", "attack", "movement", "utility", "meta",
                                               "itemGranted", "weaponDefault", "enabled", "triggeredBy")
                        if k in f} for f in g.get("skills") or []]})
    return {"groups": out}


def max_hit(doc: dict) -> float | None:
    mh = (doc or {}).get("maxHit") or {}
    v = _num(mh.get("min"))
    if v is not None:
        return v
    vals = [_num(mh.get(t)) for t in DAMAGE_TYPES]
    vals = [x for x in vals if x is not None]
    if vals:
        return min(vals)
    st = (doc or {}).get("stats") or {}
    vals = [_num(st.get(f"{t}MaximumHitTaken")) for t in DAMAGE_TYPES]
    vals = [x for x in vals if x is not None]
    return min(vals) if vals else None


def measures_main(rec: dict) -> bool:
    """Does PoB measure the skeleton's main skill? A trigger setup or a gear change can hand the damage to
    another skill (13% of archived builds before synth guarded against it); such a build is not the idea it
    is filed under, so it ranks below honest builds."""
    skd = rec.get("skeleton") or {}
    names = {skd.get("main_skill"), (skd.get("tags") or {}).get("main_skill_name")} - {None, ""}
    return main_skill_matches(names, primary_doc(rec.get("stats")).get("mainSkill"))


class Entry:
    """One archived build (a line of archive.jsonl), with the derived ranking keys."""
    __slots__ = ("rec", "uid", "cell", "tier", "objective", "feasible", "verified", "dps_honest", "ehp", "neg_cost",
                 "quarantine")

    def __init__(self, rec: dict):
        self.rec = rec
        self.uid = rec["uid"]
        self.cell = rec.get("cell")
        s = rec.get("score") or {}
        self.feasible = bool(s.get("feasible"))
        self.verified = bool(s.get("verified", True)) and not any(
            str(f).startswith("unverified") or ":unverified" in str(f) for f in s.get("flags") or [])
        self.objective = _num(s.get("objective"))
        self.dps_honest = _num(s.get("dps_honest"))
        self.quarantine = ("no_damage" if self.feasible and self.dps_honest is not None
                           and self.dps_honest <= QUARANTINE_MIN_DPS else None)
        if self.quarantine is None and self.feasible and not measures_main(rec):
            self.quarantine = "wrong_main"     # the damage is another skill's: kept, ranked like an unverified build
            self.verified = False
        self.tier = (2 if self.quarantine == "no_damage" else 0 if self.feasible and self.verified
                     else 1 if self.feasible else 2)
        self.ehp = _num(s.get("ehp"))
        self.neg_cost = -float(COST_TIER.get((rec.get("skeleton") or {}).get("budget_tier", "mid"), 1))

    def key(self, k):
        v = getattr(self, k)
        return float("-inf") if v is None else v

    def rank(self) -> tuple:
        """Smaller is better: tier, then -objective."""
        return (self.tier, -(self.objective if self.objective is not None else float("-inf")))

    @property
    def skeleton(self) -> dict:
        return self.rec.get("skeleton") or {}


def dominates(a: Entry, b: Entry, keys=PARETO_KEYS) -> bool:
    """a >= b on every key and > on one (all maximised; missing = -inf)."""
    gt = False
    for k in keys:
        x, y = a.key(k), b.key(k)
        if x < y:
            return False
        if x > y:
            gt = True
    return gt


class Cell:
    def __init__(self, key: str, desc: dict):
        self.key = key
        self.desc = desc
        self.tiers: dict[int, list[Entry]] = {0: [], 1: [], 2: []}
        self.attempts = 0

    def entries(self) -> list[Entry]:
        return sorted([e for t in (0, 1, 2) for e in self.tiers[t]], key=Entry.rank)

    def elite(self) -> Entry | None:
        for t in (0, 1, 2):
            if self.tiers[t]:
                return min(self.tiers[t], key=Entry.rank)
        return None

    def best_tier(self) -> int:
        for t in (0, 1, 2):
            if self.tiers[t]:
                return t
        return 3

    def offer(self, e: Entry) -> str:
        """Try to insert e. -> 'new_cell' | 'improved' | 'front' | 'dominated' | 'infeasible_dropped'."""
        was_empty = self.best_tier() == 3
        old = self.elite()
        if e.tier == 2 and (self.tiers[0] or self.tiers[1]):
            return "infeasible_dropped"
        front = self.tiers[e.tier]
        if any(dominates(o, e) or (o.key("dps_honest") == e.key("dps_honest") and o.key("ehp") == e.key("ehp")
                                   and o.key("neg_cost") == e.key("neg_cost") and o.rank() <= e.rank())
               for o in front):
            return "dominated"
        front[:] = [o for o in front if not dominates(e, o)] + [e]
        if len(front) > MAX_FRONT[e.tier]:
            front.sort(key=Entry.rank)
            # keep the extremes of each Pareto key, then the best-objective entries
            keep = [max(front, key=lambda x: x.key(k)) for k in ("dps_honest", "ehp")]
            for x in front:
                if len({id(y) for y in keep}) >= MAX_FRONT[e.tier]:
                    break
                keep.append(x)
            ids = {id(y) for y in keep}
            front[:] = [x for x in front if id(x) in ids]
        if e.tier < 2:   # a feasible entry evicts the infeasible placeholders
            self.tiers[2] = []
        if was_empty:
            return "new_cell"
        new = self.elite()
        if new is e and (old is None or e.rank() < old.rank()):
            return "improved"
        return "front" if any(x is e for x in front) else "dominated"


# ---------------------------------------------------------------------------------------------- archive
class Archive:
    def __init__(self, run_dir: str | None = None, corpus_path: str | None = None, axes=None):
        self.run_dir = run_dir
        self.axes = resolve_axes(axes)
        self.cells: dict[str, Cell] = {}
        self.records: dict[str, dict] = {}       # uid -> record (every evaluated skeleton)
        self.seen: set[str] = set()              # skeleton ids
        self.sources: dict[str, dict] = {}       # source -> counters
        self.n = 0
        self.lock = threading.RLock()
        self._corpus = corpus(corpus_path) if corpus_path else corpus()
        self._fh = None
        self.created = time.time()
        self.meta: dict = {}
        self.redescribe = False
        self.children: dict[str, int] = {}      # parent skeleton id -> mutations evaluated

    # -------------------------------------------------------------- persistence
    @property
    def jsonl_path(self) -> str | None:
        return os.path.join(self.run_dir, "archive.jsonl") if self.run_dir else None

    @classmethod
    def open(cls, run_dir: str, corpus_path: str | None = None, redescribe: bool = False, axes=None) -> "Archive":
        """Load a run directory. redescribe=True recomputes every stored descriptor with the current
        describe() (after a descriptor change; the jsonl itself is never rewritten). axes: the cell axes;
        default the axes the run was started with (index.json), else DEFAULT_AXES. Records stored under other
        axes are re-keyed from their descriptor."""
        os.makedirs(run_dir, exist_ok=True)
        meta = {}
        try:
            with open(os.path.join(run_dir, "index.json"), encoding="utf-8") as f:
                meta = (json.load(f) or {}).get("meta") or {}
        except (OSError, ValueError):
            pass
        a = cls(run_dir, corpus_path, axes=axes if axes is not None else meta.get("axes"))
        a.redescribe = redescribe
        p = a.jsonl_path
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue          # torn last line after a crash
                    a._ingest(rec)
        a.meta = meta
        a.meta["axes"] = list(a.axes)
        return a

    def _append(self, rec: dict) -> None:
        if not self.run_dir:
            return
        if self._fh is None:
            self._fh = open(self.jsonl_path, "a", encoding="utf-8")
        self._fh.write(json.dumps(rec, default=_jsonable) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def checkpoint(self, extra: dict | None = None) -> str | None:
        """Write index.json atomically (cells -> uids per tier, counters, stats)."""
        if not self.run_dir:
            return None
        with self.lock:
            if extra:
                self.meta.update(extra)
            self.meta["axes"] = list(self.axes)
            idx = {"version": 1, "written": time.time(), "n_records": self.n, "meta": self.meta,
                   "stats": self.stats(),
                   "cells": {k: {"desc": c.desc, "attempts": c.attempts,
                                 "tiers": {str(t): [e.uid for e in c.tiers[t]] for t in (0, 1, 2) if c.tiers[t]},
                                 "elite": (c.elite().uid if c.elite() else None)}
                             for k, c in self.cells.items()}}
        p = os.path.join(self.run_dir, "index.json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, default=_jsonable)
        os.replace(tmp, p)
        return p

    # -------------------------------------------------------------- adding
    def make_record(self, result: BuildResult, worker: Any = None, job: dict | None = None) -> dict:
        if not result.descriptor:
            result.descriptor = describe(result, self._corpus)
        sk = result.skeleton
        doc = primary_doc(result.stats)
        return {
            "uid": f"{sk.id}",
            "t": time.time(),
            "skeleton": sk.to_dict(),
            "ok": bool(result.ok),
            "code": result.code,
            "score": slim_score(result.score),
            "descriptor": result.descriptor,
            "cell": cell_key(result.descriptor, self.axes),
            "axes": list(self.axes),
            "max_hit": max_hit(doc),
            "stats": slim_stats(result.stats),
            "skills": slim_skills(result.skills),
            "evals": result.evals, "seconds": result.seconds,
            "notes": [str(n) for n in (result.notes or [])][:40],
            "unique_trials": list(getattr(result, "unique_trials", None) or []),
            "error": result.error,
            "worker": worker,
            "job": job or {},
        }

    def add(self, result: BuildResult, worker: Any = None, job: dict | None = None) -> str:
        """Archive one BuildResult. Returns the status:
        new_cell | improved | front | dominated | infeasible_dropped | failed | duplicate."""
        with self.lock:
            if result.skeleton.id in self.seen:
                return "duplicate"
            rec = self.make_record(result, worker, job)
            status = self._ingest(rec, fresh=True)
            rec["status"] = status
            self._append(rec)
            return status

    def _src(self, rec: dict) -> dict:
        src = (rec.get("skeleton") or {}).get("source") or "?"
        return self.sources.setdefault(src, {"n": 0, "ok": 0, "feasible": 0, "verified": 0, "new_cell": 0,
                                             "quarantined": 0, "wrong_main": 0, "improved": 0, "evals": 0, "seconds": 0.0})

    def _ingest(self, rec: dict, fresh: bool = False) -> str:
        """Insert a record (from add(), or replayed from archive.jsonl)."""
        uid = rec["uid"]
        if uid in self.records and not fresh:
            return "duplicate"
        if not fresh and getattr(self, "redescribe", False) and rec.get("ok") and rec.get("skeleton"):
            try:
                br = BuildResult(skeleton=Skeleton(**rec["skeleton"]), ok=True, stats=rec.get("stats"),
                                 skills=rec.get("skills"))
                rec["descriptor"] = describe(br, self._corpus)
                rec["cell"] = cell_key(rec["descriptor"], self.axes)
                rec["axes"] = list(self.axes)
            except Exception:  # noqa: BLE001 - keep the stored descriptor
                pass
        self.records[uid] = rec
        self.seen.add(uid)
        self.n += 1
        s = self._src(rec)
        s["n"] += 1
        par = (rec.get("skeleton") or {}).get("parent") or (rec.get("job") or {}).get("parent")
        if par:
            self.children[par] = self.children.get(par, 0) + 1
        s["evals"] += int(rec.get("evals") or 0)
        s["seconds"] += float(rec.get("seconds") or 0.0)
        if not rec.get("ok") or not rec.get("score"):
            status = "failed"
        else:
            s["ok"] += 1
            e = Entry(rec)
            s["feasible"] += int(e.feasible)
            s["verified"] += int(e.feasible and e.verified)
            s["quarantined"] += int(e.quarantine == "no_damage")
            s["wrong_main"] += int(e.quarantine == "wrong_main")
            desc = rec.get("descriptor") or {}
            if not rec.get("cell") or tuple(rec.get("axes") or FINE_AXES) != self.axes:
                derive(desc, (rec.get("skeleton") or {}).get("cls"))
                rec["descriptor"], rec["cell"], rec["axes"] = desc, cell_key(desc, self.axes), list(self.axes)
                e.cell = rec["cell"]
            key = rec["cell"]
            cell = self.cells.get(key)
            if cell is None:
                cell = self.cells[key] = Cell(key, {a: desc.get(a) for a in self.axes})
            cell.attempts += 1
            status = cell.offer(e)
            if status == "new_cell" and e.tier == 2:
                status = "new_cell_infeasible"
            if status in ("new_cell", "improved"):
                s[status] += 1
        if not fresh:
            rec.setdefault("status", status)
        return status

    # -------------------------------------------------------------- queries
    def elites(self, tiers=(0, 1)) -> list[Entry]:
        with self.lock:
            out = [c.elite() for c in self.cells.values()]
        return sorted([e for e in out if e is not None and e.tier in tiers], key=Entry.rank)

    def all_entries(self, tiers=(0, 1, 2)) -> list[Entry]:
        with self.lock:
            return sorted([e for c in self.cells.values() for t in tiers for e in c.tiers[t]], key=Entry.rank)

    def stats(self) -> dict:
        with self.lock:
            feas = [c for c in self.cells.values() if c.best_tier() <= 1]
            ver = [c for c in self.cells.values() if c.best_tier() == 0]
            qd = sum(max(0.0, c.elite().objective or 0.0) for c in feas)
            qd_v = sum(max(0.0, c.elite().objective or 0.0) for c in ver)
            axis_cov = {a: len({c.desc.get(a) for c in feas}) for a in self.axes}
            src = {}
            for k, v in self.sources.items():
                n = max(1, v["n"])
                src[k] = dict(v, ok_rate=v["ok"] / n, feasible_rate=v["feasible"] / n,
                              success_rate=(v["new_cell"] + v["improved"]) / n)
            best = min(feas, key=lambda c: c.elite().rank(), default=None)
            return {"records": self.n, "cells": len(self.cells), "cells_feasible": len(feas),
                    "cells_verified": len(ver), "qd_score": qd, "qd_score_verified": qd_v,
                    "axes": list(self.axes),
                    "quarantined": sum(v.get("quarantined", 0) for v in self.sources.values()),
                    "wrong_main": sum(v.get("wrong_main", 0) for v in self.sources.values()),
                    "axis_coverage": axis_cov, "sources": src,
                    "best": None if best is None else {"uid": best.elite().uid, "cell": best.key,
                                                       "objective": best.elite().objective,
                                                       "dps_honest": best.elite().dps_honest}}

    def feedback(self, n_elites: int = 30, skeleton_cell_key=None) -> dict:
        """Feedback for SkeletonGenerator.generate(feedback=...):
            cell_counts      {cell key: jobs}; keyed by skeleton_cell_key(Skeleton) when given (the generator's
                             own coarse cell, computable before evaluation), else by the archive cell key
            archive_cell_counts {archive cell key: attempts}
            axis_counts      {axis: {value: occupied feasible cells}}  (which niches are thin)
            skill_counts     {main skill (skeleton spelling): jobs}
            unique_counts    {unique name: jobs}
            source_success   {source: success rate = (new cells + improvements) / jobs}
            source_stats     {source: counters}
            elite_skeletons / elites  [Skeleton] best cell elites first (feasible; verified before unverified)
            elite_info       [{skeleton id, cell, descriptor, objective, dps_honest, ehp, verified}]
            seen_ids         skeleton ids already evaluated (do not propose again)
        """
        with self.lock:
            st = self.stats()
            axis_counts: dict[str, dict] = {a: {} for a in AXES}
            for c in self.cells.values():
                if c.best_tier() <= 1:
                    d = c.elite().rec.get("descriptor") or c.desc
                    for a in AXES:
                        v = str(d.get(a))
                        axis_counts[a][v] = axis_counts[a].get(v, 0) + 1
            cc: dict[str, int] = {}
            skill_counts: dict[str, int] = {}
            unique_counts: dict[str, int] = {}
            for rec in self.records.values():
                skd = rec.get("skeleton") or {}
                skill_counts[skd.get("main_skill")] = skill_counts.get(skd.get("main_skill"), 0) + 1
                for u in skd.get("uniques") or []:
                    n = u.get("name") if isinstance(u, dict) else str(u)
                    unique_counts[n] = unique_counts.get(n, 0) + 1
                if skeleton_cell_key is not None:
                    try:
                        k = skeleton_cell_key(Skeleton(**skd))
                    except Exception:  # noqa: BLE001
                        continue
                    cc[k] = cc.get(k, 0) + 1
            el = self.elites()[:n_elites]
            elite_sk = []
            for e in el:
                try:
                    elite_sk.append(Skeleton(**e.skeleton))
                except TypeError:
                    pass
            arch_cc = {k: c.attempts for k, c in self.cells.items()}
            return {
                "cell_counts": cc if skeleton_cell_key is not None else arch_cc,
                "archive_cell_counts": arch_cc,
                "axis_counts": axis_counts,
                "skill_counts": skill_counts,
                "unique_counts": unique_counts,
                "source_success": {k: v["success_rate"] for k, v in st["sources"].items()},
                "source_stats": st["sources"],
                "elite_skeletons": elite_sk,
                "elites": list(elite_sk),
                "elite_info": [{"id": e.uid, "cell": e.cell, "descriptor": e.rec.get("descriptor"),
                                "objective": e.objective, "dps_honest": e.dps_honest, "ehp": e.ehp,
                                "verified": e.tier == 0} for e in el],
                "seen_ids": set(self.seen),
            }

    def elite_quantile(self, q: float = 0.5) -> float | None:
        """Objective at quantile q of the feasible cell elites (None when there are none)."""
        objs = sorted(e.objective for e in self.elites() if e.objective is not None)
        if not objs:
            return None
        return objs[min(len(objs) - 1, int(q * len(objs)))]

    def pick_parent(self, rng) -> Entry | None:
        """Parent for a mutation: an elite, favouring good objective and rarely-attempted cells (curiosity)."""
        with self.lock:
            cand = [(c, c.elite()) for c in self.cells.values() if c.best_tier() <= 1]
        if not cand:
            return None
        cand.sort(key=lambda ce: ce[1].rank())
        n = len(cand)
        weights = []
        for i, (c, e) in enumerate(cand):
            w = (1.0 - 0.7 * i / max(1, n - 1)) / math.sqrt(1 + c.attempts) / (1 + self.children.get(e.uid, 0))
            if e.tier == 1:
                w *= 0.5
            weights.append(max(w, 1e-6))
        return rng.choices([e for _, e in cand], weights=weights, k=1)[0]
