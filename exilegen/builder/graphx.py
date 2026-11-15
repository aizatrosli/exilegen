"""Graph export for the pob-redux Graph view, gated by a requirements check.

    python3 -m exilegen.builder.graphx --requirements req.json --out graph.json [--corpus-dir DIR] [--check]

Nothing is analysed until the requirements pass. The check has two halves:

  data          catalog present, mod-parse coverage, corpus summary, ladder corpus (optional), networkx (optional)
  requirements  the user's request is well formed: constraints parse, profiles / ascendancy / uniques /
                keystones exist, class matches ascendancy, nothing is both required and forbidden

A failed check exits 2 and writes no graph (with --out it still writes the check, so the app can show why).
The requirements object uses the builder's own names (contracts.Skeleton, scoring.constraints, profiles.json):

  {"constraints": ["one_button"], "profiles": ["pinnacle_v1", "mapping_v1"], "budget_tier": "mid",
   "cls": null, "ascendancy": "Lich", "damage_types": ["Chaos"], "uniques": [{"name": "Liminal Coil"}],
   "forbid_uniques": [], "keystones": [], "chain_kinds": [], "min_novelty": 0.0, "top": 150,
   "exclude_supports": ["Rakiata's Flow"]}

`exclude_supports` are support gems the user does not want socketed. They change no score and drop no chain: a
chain whose measured build uses one stays in the list (the view marks it and offers a rebuild with
`python3 -m exilegen.lab chain --exclude-support ...`). `mechanics.info.support_names` lists every support gem
for a picker and `lineage_supports` the lineage gems among them. `--codes` (with `--scores`) attaches each measured
chain build's PoB code as `measured.code`.

The exported document ("format": "exilegen-graph", "version": 1) holds the requirements, the check, the
mechanics graph restricted to the kept chains, the chains, and (with --corpus-dir) the ladder co-occurrence
graph with its kits and untried-but-predicted pairs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, asdict

from . import catalog as catmod
from . import mechgraph
from . import chainfit
from . import chainresults
from . import leveling as levelmod
from . import supportgems
from . import tempo as tempomod

FORMAT, VERSION = "exilegen-graph", 1
BUDGET_TIERS = ("low", "mid", "high")
CHAIN_KINDS = ("self_feed", "enabler", "graft", "bridge")
CONSTRAINT_TYPES = ("one_button", "no_cast", "no_minions", "max_manual_skills", "require_gem", "forbid_gem",
                    "require_unique", "forbid_unique", "require_keystone", "forbid_keystone")
DMG = ("Physical", "Fire", "Cold", "Lightning", "Chaos")
MIN_PARSE_COVERAGE = 0.7
MIN_CORPUS_BUILDS = 500
PROFILES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "profiles.json")


@dataclass
class Requirements:
    constraints: list = field(default_factory=list)
    profiles: list = field(default_factory=lambda: ["pinnacle_v1", "mapping_v1"])
    budget_tier: str = "mid"
    cls: str | None = None
    ascendancy: str | None = None
    damage_types: list = field(default_factory=list)
    uniques: list = field(default_factory=list)          # [{"name": ...}] chains must touch one of these
    forbid_uniques: list = field(default_factory=list)   # names
    keystones: list = field(default_factory=list)        # chains must touch one of these
    chain_kinds: list = field(default_factory=list)      # empty = all
    min_novelty: float = 0.0
    top: int = 150
    mana_per_second: float = tempomod.DEFAULT_BUDGET["mana_per_second"]   # sustainable resource spend
    spirit: float = tempomod.DEFAULT_BUDGET["spirit"]                     # Spirit available for reservations
    leveling: bool = False        # rank by how early the chain works while leveling and what must be farmed for it
    online_by: int = 0            # drop chains that are not complete by this character level (0 = no limit)
    rank_by: str = "formula"      # "formula", "measured" (PoB gain, needs --scores) or "fitted" (score fitted to PoB runs)
    leveling_config: dict = field(default_factory=dict)   # overrides of leveling.DEFAULT_CONFIG (trial levels, ...)
    exclude_supports: list = field(default_factory=list)  # support gem names not to socket (no effect on scores)

    @classmethod
    def from_dict(cls, d: dict) -> tuple["Requirements", list[str]]:
        known = {f for f in cls.__dataclass_fields__}
        unknown = sorted(set(d) - known)
        r = cls(**{k: v for k, v in d.items() if k in known and v is not None})
        r.uniques = [u if isinstance(u, dict) else {"name": str(u)} for u in r.uniques or []]
        return r, unknown

    def unique_names(self) -> list[str]:
        return [u.get("name", "") for u in self.uniques]


def _check(cid: str, group: str, status: str, label: str, detail: str = "") -> dict:
    return {"id": cid, "group": group, "status": status, "label": label, "detail": detail}


def preflight(cat: catmod.Catalog, req: Requirements, unknown_keys: list[str] | None = None,
              corpus_dir: str | None = None, scores: dict | None = None) -> list[dict]:
    """-> checks [{id, group: data|requirements, status: ok|warn|fail, label, detail}]."""
    out = []
    # ---- data
    counts = {"skills": len(cat.skills), "uniques": len(cat.uniques), "keystones": len(cat.keystones),
              "ascendancies": len(cat.ascendancies)}
    missing = [k for k, v in counts.items() if not v]
    out.append(_check("data.catalog", "data", "fail" if missing else "ok", "Game data catalog",
                      ("missing: " + ", ".join(missing) + " (run python3 -m exilegen.builder.catalog)") if missing else
                      ", ".join(f"{v} {k}" for k, v in counts.items())))
    n = len(cat.parsed or {})
    cov = sum(1 for v in cat.parsed.values() if v.get("mods")) / n if n else 0.0
    out.append(_check("data.parse_coverage", "data", "fail" if not n else "warn" if cov < MIN_PARSE_COVERAGE else "ok",
                      "Mod lines PoB can parse",
                      f"{cov:.0%} of {n} lines; links through the rest fall back to text matching"))
    sample = (cat.corpus or {}).get("sample_n") or 0
    out.append(_check("data.corpus_summary", "data", "ok" if sample >= MIN_CORPUS_BUILDS else "warn",
                      "Ladder summary for novelty",
                      f"{sample} builds, {(cat.corpus or {}).get('total_characters', 0)} ladder characters" if sample else
                      "none: every chain will count as fully novel"))
    ev = os.path.join(corpus_dir, "evaluated.jsonl") if corpus_dir else None
    if ev and os.path.exists(ev):
        age = (time.time() - os.path.getmtime(ev)) / 86400
        out.append(_check("data.ladder_corpus", "data", "warn" if age > 45 else "ok", "Ladder corpus for kits",
                          f"{ev} ({age:.0f} days old)"))
    else:
        out.append(_check("data.ladder_corpus", "data", "warn", "Ladder corpus for kits",
                          "not given (--corpus-dir): ladder kits and untried pairs are skipped"))
    try:
        import networkx  # noqa: F401
        import numpy  # noqa: F401
        out.append(_check("data.graph_libs", "data", "ok", "numpy and networkx", "installed"))
    except ImportError as e:
        out.append(_check("data.graph_libs", "data", "warn", "numpy and networkx", f"{e}: ladder kits are skipped"))
    # ---- requirements
    if unknown_keys:
        out.append(_check("req.keys", "requirements", "fail", "Known fields only", "unknown: " + ", ".join(unknown_keys)))
    bad = []
    try:
        from ..scoring.constraints import Constraint
        for c in req.constraints:
            try:
                if Constraint.parse(c).type not in CONSTRAINT_TYPES:
                    bad.append(f"{c} (unknown constraint)")
            except Exception as e:  # noqa: BLE001
                bad.append(f"{c} ({e})")
    except ImportError:
        pass
    out.append(_check("req.constraints", "requirements", "fail" if bad else "ok", "Constraints",
                      "; ".join(bad) if bad else (", ".join(map(str, req.constraints)) or "none")))
    try:
        profs = set(json.load(open(PROFILES_PATH))["profiles"])
    except Exception:  # noqa: BLE001
        profs = set()
    badp = [p for p in req.profiles if profs and p not in profs]
    out.append(_check("req.profiles", "requirements", "fail" if badp or not req.profiles else "ok", "Scoring profiles",
                      ("unknown: " + ", ".join(badp)) if badp else (", ".join(req.profiles) or "pick at least one")))
    out.append(_check("req.budget_tier", "requirements", "ok" if req.budget_tier in BUDGET_TIERS else "fail", "Budget tier",
                      req.budget_tier if req.budget_tier in BUDGET_TIERS else f"{req.budget_tier!r} is not one of {BUDGET_TIERS}"))
    asc = cat.ascendancies.get(req.ascendancy) if req.ascendancy else None
    classes = {a["class"] for a in cat.ascendancies.values()}
    if req.ascendancy and not asc:
        out.append(_check("req.ascendancy", "requirements", "fail", "Class and ascendancy", f"unknown ascendancy {req.ascendancy!r}"))
    elif req.cls and req.cls not in classes:
        out.append(_check("req.ascendancy", "requirements", "fail", "Class and ascendancy", f"unknown class {req.cls!r}"))
    elif asc and req.cls and asc["class"] != req.cls:
        out.append(_check("req.ascendancy", "requirements", "fail", "Class and ascendancy",
                          f"{req.ascendancy} belongs to {asc['class']}, not {req.cls}"))
    else:
        out.append(_check("req.ascendancy", "requirements", "ok", "Class and ascendancy",
                          " / ".join(x for x in (req.cls, req.ascendancy) if x) or "any"))
    badd = [d for d in req.damage_types if d not in DMG]
    out.append(_check("req.damage_types", "requirements", "fail" if badd else "ok", "Damage types",
                      ("unknown: " + ", ".join(badd)) if badd else (", ".join(req.damage_types) or "any")))
    unames = {u["name"] for u in cat.uniques.values()}
    badu = [u for u in req.unique_names() + list(req.forbid_uniques) if u not in unames]
    both = sorted(set(req.unique_names()) & set(req.forbid_uniques))
    detail = "; ".join(x for x in (("unknown: " + ", ".join(badu)) if badu else "",
                                   ("required and forbidden: " + ", ".join(both)) if both else "") if x)
    out.append(_check("req.uniques", "requirements", "fail" if detail else "ok", "Uniques",
                      detail or (", ".join(req.unique_names()) or "any")))
    knames = {k["name"] for k in cat.keystones.values()}
    badk = [k for k in req.keystones if k not in knames]
    out.append(_check("req.keystones", "requirements", "fail" if badk else "ok", "Keystones",
                      ("unknown: " + ", ".join(badk)) if badk else (", ".join(req.keystones) or "any")))
    ex = req.exclude_supports
    if not isinstance(ex, list) or not all(isinstance(x, str) for x in ex):
        out.append(_check("req.exclude_supports", "requirements", "fail", "Excluded support gems",
                          "exclude_supports must be a list of support gem names"))
    else:
        known = supportgems.names()
        badx = supportgems.unknown(ex) if known else []
        out.append(_check("req.exclude_supports", "requirements", "warn" if badx or (ex and not known) else "ok",
                          "Excluded support gems",
                          ("not a known support gem: " + ", ".join(badx)) if badx else
                          "no support gem list (python3 -m exilegen.builder.supportgems): names are not checked"
                          if ex and not known else (", ".join(ex) or "none")))
    badc = [k for k in req.chain_kinds if k not in CHAIN_KINDS]
    has_tempo = bool(tempomod.load())
    ok_budget = float(req.mana_per_second) > 0 and float(req.spirit) >= 0
    out.insert(5, _check("data.tempo", "data", "ok" if has_tempo else "warn", "Skill cost, cast time and cooldown",
                         "from PoB skill data at gem level 20" if has_tempo else
                         "missing (python3 -m exilegen.builder.tempo): chains are ranked as if skills were free"))
    has_lvl = bool(levelmod.load())
    out.insert(6, _check("data.leveling", "data", "ok" if has_lvl else ("fail" if req.leveling or req.online_by else "warn"),
                         "Level and drop data",
                         "gem, item, passive and ascendancy gates from PoB data; trial, quest-point and Spirit levels from the PoE2 Codex campaign guide"
                         if has_lvl else "missing (python3 -m exilegen.builder.leveling): chains have no leveling path"))
    lvl_cfg, cfg_errors = levelmod.config(req.leveling_config if isinstance(req.leveling_config, dict) else {"": 0})
    out.append(_check("req.leveling_config", "requirements", "fail" if cfg_errors else "ok", "Leveling settings",
                      "; ".join(cfg_errors) if cfg_errors else
                      "trials at levels {}; boss drops at {}; full score to level {}, x{} at {}{}".format(
                          ", ".join(f"{x:g}" for x in lvl_cfg["trial_levels"]), lvl_cfg["boss_level"], lvl_cfg["early_level"],
                          lvl_cfg["late_factor"], lvl_cfg["late_level"],
                          " (changed: " + ", ".join(sorted(req.leveling_config)) + ")" if req.leveling_config else "")))
    if req.rank_by not in ("formula", "measured", "fitted"):
        out.append(_check("req.rank_by", "requirements", "fail", "Ranking", f"{req.rank_by!r} is not 'formula', 'measured' or 'fitted'"))
    elif req.rank_by == "measured":
        n = len(scores or {})
        out.append(_check("req.rank_by", "requirements", "ok" if n else "fail", "Ranking",
                          f"by PoB-measured gain ({n} chains measured)" if n else
                          "rank_by 'measured' needs --scores with a chainscore run directory that has results"))
    elif req.rank_by == "fitted":
        m = chainfit.load()
        out.append(_check("req.rank_by", "requirements", "ok" if m else "fail", "Ranking",
                          f"by the score fitted to {m['n']} PoB-measured chains" if m else
                          "rank_by 'fitted' needs a fitted model (python3 -m exilegen.builder.chainfit --scores <run dir>)"))
    ok_lvl = isinstance(req.online_by, int) and 0 <= req.online_by <= 100
    out.append(_check("req.leveling", "requirements", "ok" if ok_lvl else "fail", "Leveling",
                      (("ranked for leveling" if req.leveling else "off") +
                       (f", complete by level {req.online_by}" if req.online_by else "")) if ok_lvl else
                      "online_by must be a whole level from 0 to 100"))
    out.append(_check("req.budget", "requirements", "ok" if ok_budget else "fail", "Resource and Spirit budget",
                      f"{req.mana_per_second:g} mana per second, {req.spirit:g} Spirit" if ok_budget else
                      "mana_per_second must be above 0 and spirit 0 or more"))
    ok_num = 0.0 <= float(req.min_novelty) <= 1.0 and 1 <= int(req.top) <= 2000
    out.append(_check("req.scope", "requirements", "fail" if badc or not ok_num else "ok", "Chain kinds, novelty and size",
                      ("unknown kinds: " + ", ".join(badc)) if badc else
                      "min_novelty must be 0..1 and top 1..2000" if not ok_num else
                      f"{', '.join(req.chain_kinds) or 'all kinds'}; novelty >= {req.min_novelty}; top {req.top}"))
    return out


def passed(checks: list[dict]) -> bool:
    return not any(c["status"] == "fail" for c in checks)


# ---------------------------------------------------------------------------------------------- mechanics
def _chain_ok(ch: dict, req: Requirements, cat: catmod.Catalog) -> bool:
    s = cat.skills.get(ch["main_skill"]) or {}
    types = set(s.get("types") or [])
    ascs = {a for a, _ in ch["asc_nodes"]} | {a for a, _ in s.get("asc_granted") or []}
    if req.ascendancy and ascs and req.ascendancy not in ascs:
        return False
    if req.cls and not req.ascendancy and ascs and not any(cat.class_of(a) == req.cls for a in ascs):
        return False
    if req.damage_types and not set(req.damage_types) & set(s.get("damage_types") or []):
        return False
    if req.chain_kinds and ch["kind"] not in req.chain_kinds:
        return False
    if ch["novelty"] < req.min_novelty:
        return False
    if set(ch["uniques"]) & set(req.forbid_uniques):
        return False
    want_u, want_k = set(req.unique_names()), set(req.keystones)
    if (want_u or want_k) and not (want_u & set(ch["uniques"]) or want_k & set(ch["keystones"])):
        return False
    for c in map(str, req.constraints):
        if c == "no_cast:strict" and "Spell" in types:
            return False
        if c == "no_minions" and ({"CreatesMinion", "Minion"} & types):
            return False
        if c.startswith("forbid_unique:") and c.split(":", 1)[1] in ch["uniques"]:
            return False
        if c.startswith("forbid_keystone:") and c.split(":", 1)[1] in ch["keystones"]:
            return False
        if c.startswith("forbid_gem:") and c.split(":", 1)[1] in (ch["main_skill"], ch["main_name"]):
            return False
    return True


def mechanics(cat: catmod.Catalog, req: Requirements, scores: dict | None = None) -> dict:
    g = mechgraph.MechGraph(cat)
    pool = g.chains(top=max(4 * req.top, 1200), per_token=10 ** 6, per_main=24, per_entity=24)
    tempo = tempomod.load()
    budget = {"mana_per_second": float(req.mana_per_second), "spirit": float(req.spirit)}
    lvl = levelmod.load()
    lvl_cfg, _ = levelmod.config(req.leveling_config)
    for c in pool:
        main = cat.skills[c["main_skill"]]
        helpers = [cat.skills[g.entities[k].meta["skill"]] for k in c["path"]
                   if g.entities[k].kind in ("skill", "util") and g.entities[k].meta.get("skill") != c["main_skill"]]
        c["tempo"] = tempomod.weigh(main, helpers, tempo, budget)
        c["score_raw"] = c["score"]
        c["score"] = round(c["score_raw"] * c["tempo"]["tempo"], 4)
        if lvl:
            ascs = [a for a, _ in c["asc_nodes"]] + [a for a, _ in main.get("asc_granted") or []]
            cls = req.cls or (cat.class_of(req.ascendancy) if req.ascendancy else None) or (cat.class_of(ascs[0]) if ascs else None)
            path_skills = [g.entities[k].meta.get("skill") for k in c["path"] if g.entities[k].kind in ("skill", "util")]
            c["leveling"] = levelmod.plan(c, lvl, cls, cat.skills, path_skills, c["tempo"].get("spirit_total", 0), lvl_cfg)
            if req.leveling:
                c["score"] = round(c["score"] * c["leveling"]["factor"], 4)
    if lvl and (req.leveling or req.online_by):
        pool = [c for c in pool if not (req.leveling and c["leveling"]["unobtainable"])
                and not (req.online_by and c["leveling"]["online_level"] > req.online_by)]
    pool.sort(key=lambda c: (-c["score"], c["id"]))
    model = chainfit.load()
    ladder = _ladder(pool, scores)
    for rank, c in enumerate(pool):
        if c["id"] in ladder:
            c["ladder"] = ladder[c["id"]]
        c["formula_rank"] = rank
        sk = cat.skills.get(c["main_skill"]) or {}
        c["main_damage_types"] = list(sk.get("damage_types") or [])
        c["main_spell"] = "Spell" in (sk.get("types") or [])
        c["main_minion"] = bool({"CreatesMinion", "Minion"} & set(sk.get("types") or []))
        if model:
            c["predicted"] = chainfit.predict(model, c)
        if scores and c["id"] in scores:
            c["measured"] = scores[c["id"]]
    if scores and req.rank_by == "measured":
        # measured gain first (credited with the chain's own states), then built ones without a baseline, then the rest
        def key(c):
            m = c.get("measured")
            if m and m.get("gain_credited") is not None:
                return (0, -m["gain_credited"])
            return (1, -(m or {}).get("dps", 0)) if m and m.get("built") else (2 if not m else 3, -c["score"])
        pool.sort(key=key)
    elif model and req.rank_by == "fitted":
        # the score fitted to PoB measurements: expected damage multiple of adding the chain
        pool.sort(key=lambda c: (-c["predicted"]["score"], c["id"]))
    chains = [c for c in pool if _chain_ok(c, req, cat)][: req.top]
    ents = {k for c in chains for k in c["path"]}
    toks = {t for c in chains for t in c["tokens"]}
    nodes, edges = [], []
    for k in sorted(ents):
        e = g.entities[k]
        meta = {m: e.meta[m] for m in ("ascendancy", "class", "slot") if e.meta.get(m)}
        if e.kind in ("skill", "util") and e.meta.get("skill") in cat.skills:
            meta["tempo"] = tempomod.facts(cat.skills[e.meta["skill"]], tempo.get(e.meta["skill"], {}))
        nodes.append({"id": k, "kind": e.kind, "name": e.name, **meta})
        for side, links in (("produces", e.produces), ("consumes", e.consumes)):
            for tok, l in links.items():
                if tok in toks:
                    edges.append({"s": k if side == "produces" else "token:" + tok,
                                  "t": "token:" + tok if side == "produces" else k,
                                  "type": side, "w": round(l.weight if side == "produces" else max(l.payoff, 0.2), 2),
                                  "line": l.line[:200]})
    for t in sorted(toks):
        nodes.append({"id": "token:" + t, "kind": "token", "name": t.split(":", 1)[-1], "group": t.split(":", 1)[0],
                      "spec": round(g.spec.get(t, 0), 3)})
    for c in chains:
        c["path_tokens"] = ["token:" + t for t in c["tokens"]]
    return {"nodes": nodes, "edges": edges, "chains": chains,
            "info": {**g.size(), "raw_chains": g.n_raw_chains, "pool": len(pool), "kept": len(chains),
                     **({"measured": chainresults.summary({k: v for k, v in scores.items() if k in {c["id"] for c in pool}}, len(pool))}
                        if scores else {}),
                     **({"fitted": {"chains": model["n"], **model.get("held_out", {})}} if model else {}),
                     "by_kind": dict(Counter(c["kind"] for c in chains)),
                     "support_names": supportgems.names(), "lineage_supports": supportgems.lineage_names()}}


def _ladder(pool: list[dict], scores: dict | None) -> dict[str, dict]:
    """Per chain, how it sits against the poe.ninja ladder reference (laddercmp), trimmed for the graph document."""
    from . import laddercmp, ladderref
    try:
        ref = ladderref.load()
    except OSError:
        return {}
    if not ref or not ref.get("skills"):
        return {}
    out = {}
    for cid, r in laddercmp.per_chain(pool, scores or {}, ref).items():
        sk = ref["skills"].get(r["main_name"]) or {}
        d = {"cls": r["cls"], "known": bool(r["in_ref"])}
        if r["ladder"]:
            d.update(share=r["ladder"]["share"], rank=r["ladder"]["rank"], n=r["ladder"]["n"])
            d["supports"] = [n for n, _ in (sk.get("supports") or [])[:5]]
            d["uniques"] = [n for n, _ in (sk.get("uniques") or [])[:5]]
            if (sk.get("dps") or {}).get("median"):
                d["dps_median"], d["dps_p90"] = sk["dps"]["median"], sk["dps"].get("p90")
        pw = (r.get("power") or {}).get("skill")
        if pw:
            d["thin"] = bool(pw["thin"])
            if pw.get("dps_vs_median") is not None:
                d["dps_vs_median"] = pw["dps_vs_median"]
        if r["parts"]:
            d["paired"] = [p["name"] for p in r["parts"] if p.get("paired")]
        out[cid] = d
    return out


# ---------------------------------------------------------------------------------------------- ladder
def ladder(corpus_dir: str, max_nodes: int = 260, max_edges: int = 1600, max_gaps: int = 150) -> dict | None:
    """Co-occurrence graph of the ladder corpus: kits (Louvain) and untried-but-predicted pairs."""
    try:
        import numpy as np
        import networkx as nx
    except ImportError:
        return None
    ev = os.path.join(corpus_dir, "evaluated.jsonl")
    bm = os.path.join(corpus_dir, "analysis-synergy", "builds_main_skill_dps.json")
    if not (os.path.exists(ev) and os.path.exists(bm)):
        return None
    bms = {b["hash"]: b for b in json.load(open(bm))}
    builds = []
    for line in open(ev):
        r = json.loads(line)
        b, t = bms.get(r["hash"]), r["tags"]
        if not b:
            continue
        comps = {"M:" + b["skill"], "A:" + (t.get("ascendancy") or "none")}
        comps |= {"S:" + s for s in b["sup"]} | {"U:" + u for u in t.get("uniques") or []}
        comps |= {"K:" + k["name"] for k in t.get("keystones") or []}
        comps |= {"AN:" + x["name"] for x in t.get("ascendancyNodes") or [] if x.get("type") == "Notable"}
        comps |= {"N:" + x["name"] for x in t.get("notables") or []}
        for grp in t.get("socketGroups") or []:
            if grp.get("enabled", True):
                comps |= {"G:" + x["name"] for x in grp.get("gems") or []
                          if not x.get("support") and x.get("name") and x["name"] != b["skill"] and not x.get("weaponDefault")}
        builds.append((b["skill"], b.get("resid"), comps))
    cnt = Counter(c for _, _, cs in builds for c in cs)
    vocab = sorted(c for c, k in cnt.items() if k >= 3)
    idx = {c: i for i, c in enumerate(vocab)}
    nb = len(builds)
    X = np.zeros((nb, len(vocab)), dtype=np.float32)
    for i, (_, _, cs) in enumerate(builds):
        for c in cs:
            if c in idx:
                X[i, idx[c]] = 1
    C = X.T @ X
    n = np.diag(C).copy()
    with np.errstate(divide="ignore", invalid="ignore"):
        pmi = np.log((C * nb) / np.outer(n, n))
        npmi = pmi / -np.log(C / nb)
    pmi[C == 0] = 0
    npmi[C == 0] = 0
    np.fill_diagonal(pmi, 0)
    np.fill_diagonal(npmi, 0)
    kind = np.array([c.split(":", 1)[0] for c in vocab])
    keep = kind != "N"          # passive notables encode tree region, not a kit
    G = nx.Graph()
    ii, jj = np.where(np.triu((C >= 3) & (npmi > 0.15) & np.outer(keep, keep), 1))
    for a, b in zip(ii, jj):
        G.add_edge(vocab[a], vocab[b], weight=float(npmi[a, b]))
    comms = sorted(nx.community.louvain_communities(G, weight="weight", resolution=2.0, seed=7), key=len, reverse=True)
    memb = {c: k for k, cm in enumerate(comms) for c in cm}
    resid = np.array([r if r is not None else np.nan for _, r, _ in builds])
    skill_of = np.array([s for s, _, _ in builds])
    kits = []
    for k, cm in enumerate(comms):
        sub = G.subgraph(cm)
        top = sorted(cm, key=lambda c: -sub.degree(c, weight="weight"))
        core = [idx[c] for c in top[:8]]
        inb = X[:, core].sum(1) >= min(4, max(2, len(core) // 2))
        sk = Counter(skill_of[inb].tolist()).most_common(3)
        asc = next((c[2:] for c in top if c.startswith("A:")), None)
        main = next((c[2:] for c in top if c.startswith("M:")), None)
        kits.append({"id": k, "label": " / ".join(x for x in (asc, main) if x) or top[0].split(":", 1)[1],
                     "size": len(cm), "builds": int(inb.sum()),
                     "mean_resid": round(float(np.nanmean(resid[inb])), 3) if inb.sum() else None,
                     "skills": [{"name": s, "n": c} for s, c in sk], "core": top[:12]})
    # the drawn graph: the strongest members of each kit, and the strongest edges between drawn nodes
    order = sorted(G.nodes, key=lambda c: -G.degree(c, weight="weight"))
    per_kit, chosen = Counter(), []
    cap = max(6, max_nodes // max(1, len(comms)) * 2)
    for c in order:
        if per_kit[memb[c]] < cap and len(chosen) < max_nodes:
            per_kit[memb[c]] += 1
            chosen.append(c)
    cs = set(chosen)
    es = sorted(((d["weight"], a, b) for a, b, d in G.edges(data=True) if a in cs and b in cs), reverse=True)[:max_edges]
    nodes = [{"id": c, "kind": c.split(":", 1)[0], "name": c.split(":", 1)[1], "kit": memb[c], "n": int(n[idx[c]])}
             for c in chosen]
    edges = [{"s": a, "t": b, "type": "cooc", "w": round(w, 3), "n": int(C[idx[a], idx[b]])} for w, a, b in es]
    # untried-but-predicted (main skill, support | unique)
    gaps = []
    for s, nsk in Counter(skill_of.tolist()).items():
        if nsk < 8:
            continue
        prof = X[skill_of == s].mean(0)
        ctx = np.where(prof >= 0.25)[0]
        for tk in ("S", "U"):
            cand = np.where((kind == tk) & (prof == 0) & (n >= 5))[0]
            if not len(cand):
                continue
            P = np.clip(pmi[np.ix_(ctx, cand)], 0, None) * prof[ctx][:, None]
            sc = P.sum(0)
            for j in np.argsort(-sc)[:4]:
                if sc[j] > 0:
                    gaps.append({"skill": s, "skill_builds": int(nsk), "kind": tk, "candidate": vocab[cand[j]][2:],
                                 "candidate_builds": int(n[cand[j]]), "score": round(float(sc[j]), 2),
                                 "driven_by": [vocab[ctx[k]] for k in np.argsort(-P[:, j])[:3] if P[k, j] > 0]})
    gaps.sort(key=lambda r: -r["score"])
    return {"nodes": nodes, "edges": edges, "kits": kits, "gaps": gaps[:max_gaps],
            "info": {"builds": nb, "components": len(vocab), "graph_nodes": G.number_of_nodes(),
                     "graph_edges": G.number_of_edges(), "kits": len(comms),
                     "modularity": round(nx.community.modularity(G, comms, weight="weight"), 3)}}


# ---------------------------------------------------------------------------------------------- export
def run(req_dict: dict, corpus_dir: str | None = None, check_only: bool = False, cat: catmod.Catalog | None = None,
        scores_dir: str | None = None, codes: bool = False) -> dict:
    cat = cat or catmod.load()
    req, unknown = Requirements.from_dict(req_dict or {})
    scores = chainresults.measured(scores_dir, codes=codes) if scores_dir else None
    if scores:
        from . import laddercmp   # imported here: laddercmp reaches back to this module through chainscore
        for cid, m in laddercmp.load_run(scores_dir).items():   # pinnacle-profile figures for the ladder comparison
            for k in ("ascendancy", "pinnacle_dps", "pinnacle_ehp", "base_pinnacle_dps"):
                if k in m and cid in scores:
                    scores[cid][k] = m[k]
    checks = preflight(cat, req, unknown, corpus_dir, scores)
    doc = {"format": FORMAT, "version": VERSION, "game": "poe2", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "requirements": asdict(req), "preflight": checks, "passed": passed(checks)}
    if check_only or not doc["passed"]:
        return doc
    doc["catalog"] = {"ascendancies": sorted(({"name": a["name"], "class": a["class"]} for a in cat.ascendancies.values()),
                                              key=lambda a: (a["class"], a["name"]))}
    doc["mechanics"] = mechanics(cat, req, scores)
    lad = ladder(corpus_dir) if corpus_dir else None
    if lad:
        doc["ladder"] = lad
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="requirements-gated graph export for the pob-redux Graph view")
    ap.add_argument("--requirements", help="JSON file (default: no restrictions)")
    ap.add_argument("--corpus-dir", help="ladder corpus directory (evaluated.jsonl + analysis-synergy/)")
    ap.add_argument("--out", help="write the graph document here")
    ap.add_argument("--scores", help="chainscore run directory: attach PoB-measured results to the chains")
    ap.add_argument("--codes", action="store_true", help="with --scores: attach each measured build's PoB code (measured.code)")
    ap.add_argument("--check", action="store_true", help="run the requirements check only")
    a = ap.parse_args(argv)
    req = json.load(open(a.requirements)) if a.requirements else {}
    doc = run(req, a.corpus_dir, a.check, scores_dir=a.scores, codes=a.codes)
    for c in doc["preflight"]:
        print(f"[{c['status']:4}] {c['label']}: {c['detail']}", file=sys.stderr)
    if a.out:
        json.dump(doc, open(a.out, "w"), separators=(",", ":"))
    if not doc["passed"]:
        print("requirements not met: nothing analysed", file=sys.stderr)
        return 2
    if "mechanics" in doc:
        info = {k: v for k, v in doc["mechanics"]["info"].items() if k not in ("support_names", "lineage_supports")}
        print(json.dumps({"mechanics": info, "ladder": (doc.get("ladder") or {}).get("info")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
