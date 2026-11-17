"""Support gem combinations: what ladder players socket on a skill against what the builder's search sockets.

The ladder side is the poe.ninja corpus (evaluated.jsonl: every sampled build's socket groups); the builder side
is a chainscore run (the supports of each chain build, read from its PoB code). No engine needed.

    ladder(corpus_dir)            -> {skill: {"n", "freq": {support: fraction}, "sets": Counter, "pairs": Counter, "size": [..]}}
    ours(run_dir)                 -> the same shape over the run's chain builds and their plain baselines
    gaps(ladder, ours, pool=None) -> the comparison (see the keys below)

Supports are compared by family ("Rapid Attacks II" = "Rapid Attacks I"). A skill is compared when the ladder
sample holds MIN_LADDER builds of it and the run MIN_OURS.

What the comparison holds:
    skills            per skill: the ladder's staples and how often we socket each, what we socket that the ladder
                      does not, the ladder's most common combination, our most common, and their overlap
    missed_staples    (skill, support) the ladder runs on STAPLE or more of its builds and we on MISS or less
    odd_picks         (skill, support) we socket on ODD_OURS or more and the ladder on RARE or less
    never_socketed    supports common on the ladder (over all skills) that no build of the run holds
    overused          supports far more common in the run than on the ladder (over all skills)
    pairs_missed      support pairs that go together on the ladder and never in our builds of the skill
    overlap           how much of the ladder's typical set our builds hold (mean over chains, by skill)

CLI:  python3 -m exilegen.builder.supportgaps --scores RUN_DIR [--corpus DIR] [--out DIR]
      python3 -m exilegen.builder.supportgaps --corpus DIR --save-ladder      (refresh data/ladder_supports.json)
      writes support_gaps.json and support_gaps.md to --out (default: the run dir), prints a summary JSON
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from itertools import combinations

from . import chainresults
from .laddercmp import support_family

MIN_LADDER = 8        # sampled ladder builds of a skill before its supports say anything
MIN_OURS = 3          # builds of the skill in the run
STAPLE = 0.4          # on this fraction of a skill's ladder builds: a staple there
MISS = 0.1            # we socket a staple on this fraction or less: missed
ODD_OURS = 0.4        # we socket it this often ...
RARE = 0.05           # ... and the ladder this rarely: an odd pick
PAIR_MIN = 0.3        # a pair held together by this fraction of the skill's ladder builds


def _stats(rows: list[list[str]]) -> dict:
    """Frequencies over a list of support sets (already families)."""
    n = len(rows)
    freq, sets, pairs = Counter(), Counter(), Counter()
    for r in rows:
        s = sorted(set(r))
        freq.update(s)
        sets[" + ".join(s)] += 1
        pairs.update(" + ".join(p) for p in combinations(s, 2))
    return {"n": n, "freq": {k: round(v / n, 3) for k, v in freq.most_common()}, "sets": sets, "pairs": pairs,
            "size": sorted(len(set(r)) for r in rows)}


def ladder(corpus_dir: str) -> dict[str, dict]:
    """Per measured main skill: the supports on the main socket group of every sampled build."""
    by: dict[str, list[list[str]]] = {}
    with open(os.path.join(corpus_dir, "evaluated.jsonl"), encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            skill = ((rec.get("pinnacle_locked") or {}).get("mainSkill") or {}).get("name")
            main = next((g for g in (rec.get("tags") or {}).get("socketGroups") or [] if g.get("main")), None)
            if not skill or not main:
                continue
            if skill.endswith(" Minion"):
                skill = skill[:-len(" Minion")]
            by.setdefault(skill, []).append([support_family(s) for s in main.get("supports") or [] if s])
    return {k: _stats(v) for k, v in by.items()}


REF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ladder_supports.json")


def save_ladder(corpus_dir: str, path: str = REF_PATH) -> dict:
    """The ladder side as one small file ({skill: {set: count}}), so a comparison needs no corpus."""
    lad = ladder(corpus_dir)
    snap = None
    try:
        with open(os.path.join(corpus_dir, "overview.json"), encoding="utf-8") as f:
            ov = json.load(f)
        snap = {"league": ov.get("league"), "version": ov.get("version"), "fetched": ov.get("fetchedUtc")}
    except OSError:
        pass
    doc = {"version": 1, "source": snap, "builds": sum(v["n"] for v in lad.values()),
           "note": "Support gem families on the main socket group of each sampled poe.ninja build, by the skill PoB measured.",
           "skills": {k: dict(v["sets"].most_common()) for k, v in sorted(lad.items())}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=0, ensure_ascii=False, sort_keys=False)
    return doc


def load_ladder(path: str = REF_PATH) -> dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return {k: _stats([[x for x in key.split(" + ") if x] for key, c in sets.items() for _ in range(c)])
            for k, sets in doc["skills"].items()}


def ours(run_dir: str) -> dict[str, dict]:
    """Per main skill: the supports of the run's chain builds that deal damage, and of their plain baselines."""
    by: dict[str, list[list[str]]] = {}
    for name in ("chain_scores.jsonl", "baselines.jsonl"):
        for r in chainresults._read(os.path.join(run_dir, name)):
            if not r.get("ok") or not (r.get("dps_honest") or 0) > 0:
                continue
            sup = chainresults.supports(r.get("code"), r.get("main_name"))
            if sup is None or sup[1]:
                continue                  # no code, or the skill comes from an item (it gets no support gems)
            by.setdefault(r["main_name"], []).append([support_family(s) for s in sup[0]])
    return {k: _stats(v) for k, v in by.items()}


def _top_set(st: dict) -> tuple[list[str], float]:
    if not st["sets"]:
        return [], 0.0
    k, c = st["sets"].most_common(1)[0]
    return ([x for x in k.split(" + ") if x], round(c / st["n"], 3))


def _typical(st: dict, size: int = 5) -> list[str]:
    """The `size` most used supports of the skill: what a typical build of it holds."""
    return list(st["freq"])[:size]


def gaps(lad: dict[str, dict], our: dict[str, dict]) -> dict:
    skills, missed, odd, pairs_missed, overlaps = {}, [], [], [], []
    for skill in sorted(set(lad) & set(our), key=lambda s: -lad[s]["n"]):
        L, O = lad[skill], our[skill]
        if L["n"] < MIN_LADDER or O["n"] < MIN_OURS:
            continue
        staples = [(s, f) for s, f in L["freq"].items() if f >= STAPLE]
        typical = _typical(L)
        held = [len(set(k.split(" + ")) & set(typical)) * c for k, c in O["sets"].items()]
        overlap = round(sum(held) / (O["n"] * max(1, len(typical))), 3)
        overlaps.append((skill, overlap, O["n"]))
        for s, f in staples:
            of = O["freq"].get(s, 0.0)
            if of <= MISS:
                missed.append({"skill": skill, "support": s, "ladder": f, "ours": of, "ladder_n": L["n"], "ours_n": O["n"]})
        for s, of in O["freq"].items():
            if of >= ODD_OURS and L["freq"].get(s, 0.0) <= RARE:
                odd.append({"skill": skill, "support": s, "ours": of, "ladder": L["freq"].get(s, 0.0),
                            "ladder_n": L["n"], "ours_n": O["n"]})
        for p, c in L["pairs"].most_common(12):
            if c / L["n"] >= PAIR_MIN and not O["pairs"].get(p):
                pairs_missed.append({"skill": skill, "pair": p, "ladder": round(c / L["n"], 3), "ours_n": O["n"]})
        lt, lts = _top_set(L)
        ot, ots = _top_set(O)
        skills[skill] = {"ladder_n": L["n"], "ours_n": O["n"],
                         "ladder_staples": [[s, f, O["freq"].get(s, 0.0)] for s, f in staples],
                         "ladder_typical": typical, "ours_typical": _typical(O), "overlap": overlap,
                         "ladder_top_set": lt, "ladder_top_set_share": lts, "ours_top_set": ot, "ours_top_set_share": ots,
                         "ladder_median_supports": L["size"][len(L["size"]) // 2],
                         "ours_median_supports": O["size"][len(O["size"]) // 2]}
    # over all skills: how often a support is on a main skill at all
    ln, on = sum(v["n"] for v in lad.values()), sum(v["n"] for v in our.values())
    lall, oall = Counter(), Counter()
    for v in lad.values():
        for s, f in v["freq"].items():
            lall[s] += f * v["n"]
    for v in our.values():
        for s, f in v["freq"].items():
            oall[s] += f * v["n"]
    never = [{"support": s, "ladder": round(c / ln, 4), "ladder_builds": round(c)} for s, c in lall.most_common()
             if c / ln >= 0.01 and not oall.get(s)]
    over = sorted(({"support": s, "ours": round(c / on, 4), "ladder": round(lall.get(s, 0) / ln, 4),
                    "times": round((c / on) / max(lall.get(s, 0) / ln, 1 / ln), 1)} for s, c in oall.items()
                   if c / on >= 0.03 and (c / on) >= 4 * (lall.get(s, 0) / ln)), key=lambda r: -r["ours"])
    under = sorted(({"support": s, "ladder": round(c / ln, 4), "ours": round(oall.get(s, 0) / on, 4)} for s, c in lall.items()
                    if c / ln >= 0.03 and oall.get(s) and (c / ln) >= 4 * (oall[s] / on)), key=lambda r: -r["ladder"])
    w = sum(n for _, _, n in overlaps)
    return {"ladder_builds": ln, "our_builds": on, "skills_compared": len(skills), "skills": skills,
            "missed_staples": sorted(missed, key=lambda r: -r["ladder"]),
            "odd_picks": sorted(odd, key=lambda r: -r["ours"]),
            "pairs_missed": sorted(pairs_missed, key=lambda r: -r["ladder"]),
            "never_socketed": never, "underused": under, "overused": over,
            "overlap": {"mean": round(sum(o * n for _, o, n in overlaps) / w, 3) if w else None,
                        "worst": [[s, o] for s, o, _ in sorted(overlaps, key=lambda x: x[1])[:10]],
                        "best": [[s, o] for s, o, _ in sorted(overlaps, key=lambda x: -x[1])[:5]]},
            "median_supports": {"ladder": _median([x for v in lad.values() for x in v["size"]]),
                                "ours": _median([x for v in our.values() for x in v["size"]])}}


def _median(xs: list[int]):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def markdown(g: dict) -> str:
    pc = lambda x: f"{100 * x:.0f}%"
    out = ["# Support gems: ladder against the builder", "",
           f"{g['ladder_builds']} ladder builds, {g['our_builds']} builder builds, {g['skills_compared']} skills compared. "
           f"Our builds hold {pc(g['overlap']['mean'] or 0)} of the ladder's five most used supports for the skill on average.", "",
           "## Staples the builder misses", "", "| Skill | Support | Ladder | Ours |", "|---|---|---|---|"]
    out += [f"| {r['skill']} | {r['support']} | {pc(r['ladder'])} | {pc(r['ours'])} |" for r in g["missed_staples"][:60]]
    out += ["", "## Supports the ladder uses and the builder never sockets", "", "| Support | Ladder builds | Share |", "|---|---|---|"]
    out += [f"| {r['support']} | {r['ladder_builds']} | {pc(r['ladder'])} |" for r in g["never_socketed"][:40]]
    out += ["", "## Supports the builder underuses (4 times or more)", "", "| Support | Ladder | Ours |", "|---|---|---|"]
    out += [f"| {r['support']} | {pc(r['ladder'])} | {pc(r['ours'])} |" for r in g["underused"][:30]]
    out += ["", "## Supports the builder overuses (4 times or more)", "", "| Support | Ours | Ladder |", "|---|---|---|"]
    out += [f"| {r['support']} | {pc(r['ours'])} | {pc(r['ladder'])} |" for r in g["overused"][:30]]
    out += ["", "## Odd picks by skill", "", "| Skill | Support | Ours | Ladder |", "|---|---|---|---|"]
    out += [f"| {r['skill']} | {r['support']} | {pc(r['ours'])} | {pc(r['ladder'])} |" for r in g["odd_picks"][:50]]
    out += ["", "## Pairs the ladder runs together and the builder never does", "", "| Skill | Pair | Ladder |", "|---|---|---|"]
    out += [f"| {r['skill']} | {r['pair']} | {pc(r['ladder'])} |" for r in g["pairs_missed"][:50]]
    out += ["", "## By skill", "", "| Skill | Ladder n | Ours n | Overlap | Ladder's typical five | Ours |", "|---|---|---|---|---|---|"]
    out += [f"| {k} | {v['ladder_n']} | {v['ours_n']} | {pc(v['overlap'])} | {', '.join(v['ladder_typical'])} | {', '.join(v['ours_typical'])} |"
            for k, v in g["skills"].items()]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ladder support gem combinations against a chainscore run")
    ap.add_argument("--corpus", help="poe.ninja corpus directory (evaluated.jsonl); default: the packaged ladder file")
    ap.add_argument("--save-ladder", action="store_true", help="write the packaged ladder file from --corpus and stop")
    ap.add_argument("--scores", help="chainscore run directory")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    if a.save_ladder:
        doc = save_ladder(a.corpus)
        print(json.dumps({"builds": doc["builds"], "skills": len(doc["skills"]), "path": REF_PATH}))
        return 0
    if not a.scores:
        ap.error("--scores is required")
    g = gaps(ladder(a.corpus) if a.corpus else load_ladder(), ours(a.scores))
    out = a.out or a.scores
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "support_gaps.json"), "w", encoding="utf-8") as f:
        json.dump(g, f, indent=1)
    with open(os.path.join(out, "support_gaps.md"), "w", encoding="utf-8") as f:
        f.write(markdown(g))
    print(json.dumps({k: g[k] for k in ("ladder_builds", "our_builds", "skills_compared", "overlap", "median_supports")}
                     | {"missed_staples": len(g["missed_staples"]), "never_socketed": len(g["never_socketed"]),
                        "overused": len(g["overused"]), "underused": len(g["underused"]),
                        "odd_picks": len(g["odd_picks"]), "pairs_missed": len(g["pairs_missed"])}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
