"""How far are the graph's chains from what ladder players actually run?

Compares a chainscore run (chains built for real in PoB) with the ladder reference (`data/ladder_ref.json`,
written by `exilegen.builder.ladderref`). No engine needed: a run directory and the reference file are enough.

    A  coverage   do the ladder's most played skills have a chain, one that builds, one that helps
    B  realism    are the best chains "meta", "known skill, new pairing" or "off-meta skill"
    C  power      chain DPS and EHP against the ladder's median and p90 for the same skill
    D  supports   do the support gems our search sockets agree with the ladder's
    E  summary    one paragraph on what this says about the graph

    per_chain(chains, measured, ref) -> {chain id: {...}}     the per-chain comparison, for the graph export
    compare(chains, measured, ref, top=50) -> dict            everything above
    load_run(run_dir) -> measured dict with "ascendancy" and "base_ehp" added from the run's files

A skill counts as known when the ladder plays it at KNOWN_SHARE or more; a part counts as paired with the skill
when at least PAIRED_MIN sampled builds hold both and they are PAIRED_FRAC or more of that skill's builds.
Support gems are compared by family: "Rapid Attacks II" and "Rapid Attacks I" are the same support.

CLI:  python3 -m exilegen.builder.laddercmp --scores RUN_DIR [--ref data/ladder_ref.json] [--out DIR] [--top 50]
      writes ladder_compare.json and ladder_compare.md to --out (default: the run dir), prints a summary JSON
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

REF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ladder_ref.json")
KNOWN_SHARE = 0.005     # a skill at or above this share of ladder characters is one players know
KNOWN_MIN_N = 3         # ... and it needs this many sampled builds to say anything about it
PAIRED_FRAC = 0.05      # a part held by this fraction of the skill's builds is commonly paired with it
PAIRED_MIN = 2          # ... in at least this many sampled builds
STAPLE_FRAC = 0.4       # a support on this fraction of a skill's ladder builds is a staple there
RARE_FRAC = 0.05        # a support below this fraction is one ladder players rarely use on the skill
TOPS = (10, 25, 50)
CLASSES = ("meta", "known skill, new pairing", "off-meta skill")

_TIER = re.compile(r"\s+(?:I|II|III|IV|V)$", re.I)


def _norm(name) -> str:
    return str(name or "").strip().lower()


def support_family(name) -> str:
    """Support gem name without its tier, lower case."""
    return _norm(_TIER.sub("", str(name or "").strip()))


def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 else None


def _ratio(a, b) -> float | None:
    a, b = _num(a), _num(b)
    return round(a / b, 4) if a and b else None


def _quant(v: list[float], q: float) -> float | None:
    if not v:
        return None
    v = sorted(v)
    i = q * (len(v) - 1)
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (i - lo), 4)


def dist(v: list) -> dict:
    """n, quartiles and the share at or above 1 of a list of ratios (None entries dropped)."""
    v = [x for x in v if x is not None]
    return {"n": len(v), "q25": _quant(v, 0.25), "median": _quant(v, 0.5), "q75": _quant(v, 0.75),
            "share_ge_1": round(sum(1 for x in v if x >= 1.0) / len(v), 4) if v else None}


class Ref:
    """The ladder reference with lookups that ignore case and never raise on missing parts."""

    def __init__(self, ref: dict | None):
        ref = ref or {}
        self.source = ref.get("source") or {}
        self.comparable = bool(self.source.get("comparable_to_chainscore"))
        self.skills = {k: v for k, v in (ref.get("skills") or {}).items() if isinstance(v, dict)}
        self._skill = {_norm(k): k for k in self.skills}
        self.combos = {_norm(k): v for k, v in (ref.get("combos") or {}).items() if isinstance(v, dict)}
        pairs = ref.get("pairs") or {}
        self.pairs = {kind: {_norm(k): v for k, v in (pairs.get(kind) or {}).items()}
                      for kind in ("unique_skill", "keystone_skill", "asc_node_skill")}
        self.has_asc_pairs = "asc_node_skill" in pairs
        builds = _num(self.source.get("builds")) or sum((_num(s.get("n")) or 0) for s in self.skills.values()) or 0

        any_share = any(_num(s.get("share")) is not None for s in self.skills.values())

        def weight(name):
            # a missing share next to known ones means too rare to be listed; the sample size only stands in
            # for share when the reference has no shares at all
            s = self.skills[name]
            sh = _num(s.get("share"))
            if sh is not None or any_share:
                return sh or 0.0
            return (_num(s.get("n")) or 0) / builds if builds else 0.0
        self.share = {k: weight(k) for k in self.skills}
        self.ranked = sorted(self.skills, key=lambda k: (-self.share[k], -(_num(self.skills[k].get("n")) or 0), k))
        self.rank = {k: i + 1 for i, k in enumerate(self.ranked)}

    def skill_name(self, name) -> str | None:
        return self._skill.get(_norm(name))

    def skill(self, name) -> dict | None:
        k = self.skill_name(name)
        return self.skills[k] if k else None

    def combo(self, skill, ascendancy) -> dict | None:
        return self.combos.get(_norm(skill) + "||" + _norm(ascendancy)) if skill and ascendancy else None

    def frac(self, skill, field: str, name) -> float | None:
        """Fraction of the skill's ladder builds listing `name` under supports / uniques / keystones / ascendancies."""
        s = self.skill(skill)
        if not s:
            return None
        want = support_family(name) if field == "supports" else _norm(name)
        tot = 0.0
        hit = False
        for row in s.get(field) or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            got = support_family(row[0]) if field == "supports" else _norm(row[0])
            if got == want:
                tot += _num(row[1]) or 0.0
                hit = True
        return round(min(tot, 1.0), 4) if hit else None

    def top_supports(self, skill, k: int) -> list[str]:
        """The skill's top k ladder supports by fraction, as families (tiers merged, best fraction first)."""
        s = self.skill(skill) or {}
        best: dict[str, float] = {}
        for row in s.get("supports") or []:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                f = support_family(row[0])
                best[f] = best.get(f, 0.0) + (_num(row[1]) or 0.0)
        return [f for f, _ in sorted(best.items(), key=lambda x: (-x[1], x[0]))[:k]]

    def support_fracs(self, skill) -> dict[str, float]:
        s = self.skill(skill) or {}
        best: dict[str, float] = {}
        for row in s.get("supports") or []:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                f = support_family(row[0])
                best[f] = min(1.0, best.get(f, 0.0) + (_num(row[1]) or 0.0))
        return best


def _ascendancy(chain: dict, m: dict) -> str | None:
    if m.get("ascendancy"):
        return m["ascendancy"]
    asc = {a for a, _ in chain.get("asc_nodes") or []}
    return next(iter(asc)) if len(asc) == 1 else None


def _parts(chain: dict, skill: str, ref: Ref, n_skill: float | None) -> list[dict]:
    out = []

    def add(kind, name, pair_kind, pair_key, field, field_name, level=None):
        pn = ref.pairs[pair_kind].get(_norm(pair_key) + "||" + _norm(skill))
        fr = ref.frac(skill, field, field_name) if field else None
        pair_frac = round(pn / n_skill, 4) if isinstance(pn, (int, float)) and n_skill else None
        best = max([x for x in (fr, pair_frac) if x is not None], default=None)
        count = pn if isinstance(pn, (int, float)) else (round(fr * n_skill) if fr is not None and n_skill else 0)
        p = {"type": kind, "name": name, "pair_n": pn if isinstance(pn, (int, float)) else None, "frac": best,
             "paired": bool(best is not None and best >= PAIRED_FRAC and count >= PAIRED_MIN)}
        if level:
            p["evidence"] = level
        out.append(p)

    for u in chain.get("uniques") or []:
        add("unique", u, "unique_skill", u, "uniques", u)
    for k in chain.get("keystones") or []:
        add("keystone", k, "keystone_skill", k, "keystones", k)
    for row in chain.get("asc_nodes") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        a, n = row[0], row[1]
        # without notable-level pairs the best evidence is how often the skill is played on that ascendancy
        if ref.has_asc_pairs:
            add("asc_node", f"{a}: {n}", "asc_node_skill", f"{a}:{n}", None, None)
        else:
            add("asc_node", f"{a}: {n}", "asc_node_skill", f"{a}:{n}", "ascendancies", a, level="ascendancy")
    return out


def _supports_cmp(ours: list[str], skill: str, ref: Ref) -> dict | None:
    mine = {support_family(s) for s in ours if s}
    out = {}
    for k in (5, 12):
        top = set(ref.top_supports(skill, k))
        if not top:
            return None
        inter = mine & top
        out[f"top{k}"] = {"ladder": len(top), "shared": len(inter),
                          "jaccard": round(len(inter) / len(mine | top), 4),
                          "ours_in_ladder": round(len(inter) / len(mine), 4) if mine else None,
                          "ladder_in_ours": round(len(inter) / len(top), 4)}
    return out


def per_chain(chains: list[dict], measured: dict[str, dict], ref: dict | Ref | None) -> dict[str, dict]:
    """One comparison record per chain (every chain given, measured or not).

    in_ref / ladder      is the main skill on the ladder: n, share, rank by share
    parts / cls          each payoff part with how often ladder builds of the skill hold it; the class
    power                chain (and plain build) DPS and EHP over the ladder's median and p90, skill and skill+ascendancy;
                         uses measured["pinnacle_dps"/"pinnacle_ehp"] (see load_run) when present, else dps/ehp
    supports             overlap of the build's supports with the ladder's top 5 and top 12 for the skill
    comparable           the chain has a damage figure of its own (feasible, right main skill, DPS above 0)
    """
    ref = ref if isinstance(ref, Ref) else Ref(ref)
    out: dict[str, dict] = {}
    for c in chains:
        cid, skill = c.get("id"), c.get("main_name")
        if cid is None:
            continue
        m = (measured or {}).get(cid) or {}
        s = ref.skill(skill)
        key = ref.skill_name(skill)
        n_skill = _num(s.get("n")) if s else None
        asc = _ascendancy(c, m)
        rec = {"id": cid, "kind": c.get("kind"), "main_name": skill, "ascendancy": asc, "score": c.get("score"),
               "measured": bool(m), "built": bool(m.get("built")), "feasible": bool(m.get("feasible")),
               "wrong_main": m.get("wrong_main"), "gain_credited": m.get("gain_credited"),
               "in_ref": s is not None,
               "ladder": ({"n": s.get("n"), "share": round(ref.share[key], 5), "rank": ref.rank[key]} if s else None)}
        rec["comparable"] = bool(m.get("feasible") and not m.get("wrong_main") and (m.get("dps") or 0) > 0)
        parts = _parts(c, skill, ref, n_skill) if s else []
        rec["parts"] = parts
        known = bool(s and ref.share[key] >= KNOWN_SHARE and (n_skill or 0) >= KNOWN_MIN_N)
        rec["skill_known"] = known
        rec["parts_paired"] = sum(1 for p in parts if p["paired"])
        if not known:
            rec["cls"] = "off-meta skill"
        elif parts and all(p["paired"] for p in parts):
            rec["cls"] = "meta"
        else:
            rec["cls"] = "known skill, new pairing"
        power = None
        if s and m:
            combo = ref.combo(skill, asc)
            # the run's plain dps/ehp are the binding profile's (pinnacle or mapping); the ladder is pinnacle_v1
            pin = m.get("pinnacle_dps") is not None
            dps, ehp = (m.get("pinnacle_dps") if pin else m.get("dps")), (m.get("pinnacle_ehp") if pin else m.get("ehp"))
            bdps = m.get("base_pinnacle_dps") if m.get("base_pinnacle_dps") is not None else m.get("base_dps")
            behp = m.get("base_pinnacle_ehp") if m.get("base_pinnacle_ehp") is not None else m.get("base_ehp")
            power = {"dps": dps or None, "ehp": ehp, "base_dps": bdps, "base_ehp": behp,
                     "profile": "pinnacle_v1" if pin else "binding", "base_key": m.get("base_key")}
            for label, src in (("skill", s), ("combo", combo)):
                d, e = (src or {}).get("dps") or {}, (src or {}).get("ehp") or {}
                usable = rec["comparable"]
                power[label] = None if src is None else {
                    "n": src.get("n"), "ladder_dps_median": d.get("median"), "ladder_dps_p90": d.get("p90"),
                    "ladder_ehp_median": e.get("median"), "ladder_ehp_p90": e.get("p90"),
                    "dps_vs_median": _ratio(dps, d.get("median")) if usable else None,
                    "dps_vs_p90": _ratio(dps, d.get("p90")) if usable else None,
                    "ehp_vs_median": _ratio(ehp, e.get("median")) if usable else None,
                    "ehp_vs_p90": _ratio(ehp, e.get("p90")) if usable else None,
                    "base_dps_vs_median": _ratio(bdps, d.get("median")),
                    "base_dps_vs_p90": _ratio(bdps, d.get("p90")),
                    "base_ehp_vs_median": _ratio(behp, e.get("median")),
                    "base_ehp_vs_p90": _ratio(behp, e.get("p90"))}
        rec["power"] = power
        sup = m.get("supports")
        rec["supports"] = None
        if m and m.get("built"):
            rec["supports"] = {"ours": list(sup or []), "n": len(sup or []), "main_granted": bool(m.get("main_granted")),
                               "vs_ladder": (_supports_cmp(sup, skill, ref)
                                             if sup and s and not m.get("wrong_main") else None)}
        out[cid] = rec
    return out


def _rank_map(pairs: list[tuple[str, float]]) -> dict[str, int]:
    """1-based rank, best first; ties share the better rank."""
    out, prev, rank = {}, None, 0
    for i, (cid, v) in enumerate(sorted(pairs, key=lambda x: -x[1])):
        if v != prev:
            rank, prev = i + 1, v
        out[cid] = rank
    return out


def _coverage(chains, measured, ref: Ref, pc, predicted: dict | None) -> dict:
    by_skill: dict[str, list[dict]] = {}
    for c in chains:
        by_skill.setdefault(_norm(c.get("main_name")), []).append(c)
    gain_rank = _rank_map([(cid, r["gain_credited"]) for cid, r in pc.items()
                           if r["comparable"] and r["gain_credited"] is not None])
    score_rank = _rank_map([(c["id"], float(c.get("score") or 0)) for c in chains if c.get("id") is not None])
    pred_rank = _rank_map(list(predicted.items())) if predicted else {}
    rows = []
    for name in ref.ranked:
        cs = by_skill.get(_norm(name), [])
        ids = [c["id"] for c in cs]
        meas = [i for i in ids if i in measured]
        feas = [i for i in meas if pc[i]["feasible"] and pc[i]["built"]]
        comp = [i for i in meas if pc[i]["comparable"] and pc[i]["gain_credited"] is not None]
        best = max(comp, key=lambda i: pc[i]["gain_credited"], default=None)
        top_score = min(ids, key=lambda i: score_rank[i], default=None)
        rows.append({"skill": name, "ladder_rank": ref.rank[name], "share": round(ref.share[name], 5),
                     "n": ref.skills[name].get("n"), "chains": len(ids), "measured": len(meas),
                     "feasible": len(feas), "helping": sum(1 for i in comp if pc[i]["gain_credited"] >= 1.0),
                     "best_chain": best, "best_gain": pc[best]["gain_credited"] if best else None,
                     "best_gain_rank": gain_rank.get(best) if best else None,
                     "best_chain_score_rank": score_rank.get(best) if best else None,
                     "best_chain_predicted_rank": pred_rank.get(best) if best else None,
                     "top_score_rank": score_rank.get(top_score) if top_score else None})
    tops = {}
    for k in TOPS:
        sel = rows[:k]
        if not sel:
            continue
        with_meas = [r for r in sel if r["measured"]]
        gr = [r["best_gain_rank"] for r in sel if r["best_gain_rank"]]
        sr = [r["top_score_rank"] for r in sel if r["top_score_rank"]]
        tops[f"top{k}"] = {
            "skills": len(sel), "ladder_share": round(sum(r["share"] for r in sel), 4),
            "with_chain": sum(1 for r in sel if r["chains"]),
            "share_covered": round(sum(r["share"] for r in sel if r["chains"]), 4),
            "with_measured_chain": len(with_meas),
            "with_feasible_chain": sum(1 for r in sel if r["feasible"]),
            "with_helping_chain": sum(1 for r in sel if r["helping"]),
            "median_best_gain_rank": _quant(gr, 0.5), "median_top_score_rank": _quant(sr, 0.5),
            "no_chain": [r["skill"] for r in sel if not r["chains"]],
            "chain_not_in_run": [r["skill"] for r in sel if r["chains"] and not r["measured"]]}
    known = [r for r in rows if r["share"] >= KNOWN_SHARE]
    ladder_names = set(ref._skill)
    off = Counter(c.get("main_name") for c in chains if _norm(c.get("main_name")) not in ladder_names)
    return {"ladder_skills": len(rows), "pool_chains": len(chains), "run_chains": len(measured),
            "ranked_by_gain": len(gain_rank), "tops": tops, "skills": rows[:max(TOPS)],
            "blind_spots": [{"skill": r["skill"], "ladder_rank": r["ladder_rank"], "share": r["share"]}
                            for r in known if not r["chains"]],
            "chains_on_skills_absent_from_ladder": sum(off.values()),
            "pool_skills": len(by_skill), "pool_skills_absent_from_ladder": len(off)}


def _realism(pc, top: int) -> dict:
    comp = sorted((r for r in pc.values() if r["comparable"] and r["gain_credited"] is not None),
                  key=lambda r: (-r["gain_credited"], r["id"]))

    def split(rs):
        n = len(rs)
        cnt = Counter(r["cls"] for r in rs)
        return {"n": n, **{c: {"n": cnt.get(c, 0), "share": round(cnt.get(c, 0) / n, 4) if n else None}
                           for c in CLASSES}}
    best = comp[:top]
    return {"compared": split(comp), "top": split(best), "top_n": len(best),
            "pool": split(list(pc.values())),
            "top_skill_share": dist([(r["ladder"] or {}).get("share") or 0.0 for r in best]) | {"share_ge_1": None},
            "top_not_in_ref": sum(1 for r in best if not r["in_ref"]),
            "top_chains": [{"id": r["id"], "main_name": r["main_name"], "ascendancy": r["ascendancy"],
                            "kind": r["kind"], "gain_credited": r["gain_credited"], "cls": r["cls"],
                            "ladder_share": (r["ladder"] or {}).get("share"),
                            "ladder_rank": (r["ladder"] or {}).get("rank"), "parts": r["parts"]} for r in best]}


def _power(pc, ref: Ref) -> dict:
    comp = [r for r in pc.values() if r["comparable"] and r["power"]]
    prof = Counter(r["power"]["profile"] for r in comp)

    def block(label):
        rows = [r["power"][label] for r in comp if r["power"].get(label)]
        # one plain build serves several chains: count each once
        seen, base = set(), []
        for r in pc.values():
            p = (r["power"] or {}).get(label)
            pw = r["power"] or {}
            k = pw.get("base_key") or (r["main_name"], r["ascendancy"], pw.get("base_dps"))
            if p and pw.get("base_dps") and k not in seen:
                seen.add(k)
                base.append(p)

        def one(rows, pre):
            d = {}
            for stat in ("dps", "ehp"):
                for mark in ("median", "p90"):
                    x = dist([p.get(f"{pre}{stat}_vs_{mark}") for p in rows])
                    d[f"{stat}_vs_{mark}"] = {"n": x["n"], "q25": x["q25"], "median": x["median"], "q75": x["q75"],
                                              "share_reaching": x["share_ge_1"]}
            return d
        return {"chains": one(rows, ""), "baselines": one(base, "base_")}
    return {"indicative": not ref.comparable, "dps_metric": ref.source.get("dps_metric"),
            "note": ref.source.get("note"), "compared_chains": len(comp),
            "chain_profile": dict(prof),
            "comparable_without_ladder_skill": sum(1 for r in pc.values() if r["comparable"] and not r["in_ref"]),
            "same_skill": block("skill"), "same_skill_and_ascendancy": block("combo")}


def _supports(pc, ref: Ref) -> dict:
    built = [r for r in pc.values() if r["supports"] is not None]
    zero = [r for r in built if r["supports"]["n"] == 0]
    cmpd = [r for r in built if r["supports"]["vs_ladder"]]
    out = {"built": len(built), "zero_supports": len(zero),
           "zero_supports_skill_from_item": sum(1 for r in zero if r["supports"]["main_granted"]),
           "zero_supports_ids": [r["id"] for r in zero if not r["supports"]["main_granted"]][:50],
           "with_supports_skill_absent_from_ladder": sum(1 for r in built if r["supports"]["n"] and not r["in_ref"]),
           "with_supports_wrong_main": sum(1 for r in built if r["supports"]["n"] and r["wrong_main"]),
           "compared": len(cmpd), "supports_per_build": dist([float(r["supports"]["n"]) for r in cmpd])["median"]}
    for k in ("top5", "top12"):
        rows = [r["supports"]["vs_ladder"][k] for r in cmpd]
        j = dist([x["jaccard"] for x in rows])
        out[k] = {"jaccard": {q: j[q] for q in ("n", "q25", "median", "q75")},
                  "ours_in_ladder_mean": round(sum(x["ours_in_ladder"] for x in rows) / len(rows), 4) if rows else None,
                  "ladder_in_ours_mean": round(sum(x["ladder_in_ours"] for x in rows) / len(rows), 4) if rows else None,
                  "no_overlap": sum(1 for x in rows if not x["shared"])}
    picks: dict[str, list[float]] = {}
    label: dict[str, str] = {}
    by_skill: dict[str, set] = {}
    ours_all = set()
    for r in built:
        for s in r["supports"]["ours"]:
            ours_all.add(support_family(s))
    for r in cmpd:
        fr = ref.support_fracs(r["main_name"])
        mine = {support_family(s): s for s in r["supports"]["ours"]}
        by_skill.setdefault(ref.skill_name(r["main_name"]), set()).update(mine)
        for f, s in mine.items():
            picks.setdefault(f, []).append(fr.get(f, 0.0))
            label.setdefault(f, _TIER.sub("", s))
    ours_rare = [{"support": label[f], "picked": len(v), "ladder_frac_mean": round(sum(v) / len(v), 4),
                  "never_on_ladder_for_skill": sum(1 for x in v if x == 0.0)}
                 for f, v in picks.items() if sum(v) / len(v) < RARE_FRAC]
    out["ours_often_ladder_rarely"] = sorted(ours_rare, key=lambda x: (-x["picked"], x["support"]))[:20]
    agree = [{"support": label[f], "picked": len(v), "ladder_frac_mean": round(sum(v) / len(v), 4)}
             for f, v in picks.items() if sum(v) / len(v) >= RARE_FRAC]
    out["ours_and_ladder"] = sorted(agree, key=lambda x: (-x["picked"], x["support"]))[:20]
    miss: dict[str, dict] = {}
    for skill, mine in by_skill.items():
        s = ref.skills[skill]
        names = {support_family(row[0]): _TIER.sub("", str(row[0])) for row in s.get("supports") or []
                 if isinstance(row, (list, tuple)) and row}
        for f, frac in ref.support_fracs(skill).items():
            if frac >= STAPLE_FRAC and f not in mine:
                d = miss.setdefault(f, {"support": names.get(f, f), "skills": [], "frac": []})
                d["skills"].append(skill)
                d["frac"].append(frac)
    out["ladder_staples_we_never_pick"] = sorted(
        ({"support": d["support"], "skills": len(d["skills"]), "ladder_frac_mean": round(sum(d["frac"]) / len(d["frac"]), 4),
          "picked_anywhere_in_run": f in ours_all, "on": sorted(d["skills"])[:6]} for f, d in miss.items()),
        key=lambda x: (-x["skills"], -x["ladder_frac_mean"], x["support"]))[:20]
    out["skills_compared"] = len(by_skill)
    return out


def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def _x(x) -> str:
    return "n/a" if x is None else (f"{x:.2f}x" if x < 10 else f"{x:.0f}x")


def _summary_text(doc: dict) -> str:
    a, b, c, d = doc["coverage"], doc["realism"], doc["power"], doc["supports"]
    s = []
    t = a["tops"].get("top25") or next(iter(a["tops"].values()), None)
    if t:
        s.append(f"Coverage: {t['with_chain']} of the ladder's {t['skills']} most played skills "
                 f"({_pct(t['share_covered'] / t['ladder_share'] if t['ladder_share'] else None)} of their combined "
                 f"play share) have at least one chain; in this run {t['with_feasible_chain']} have one that built feasibly and "
                 f"{t['with_helping_chain']} one that helps (credited gain of 1.0 or more).")
        if a["run_chains"] < a["pool_chains"]:
            s.append(f"The run covers {a['run_chains']} of {a['pool_chains']} chains, so the built and helping counts "
                     f"are lower bounds.")
    if a["blind_spots"]:
        s.append(f"The graph is blind to {len(a['blind_spots'])} skills that players do run "
                 f"(most played: {', '.join(x['skill'] for x in a['blind_spots'][:5])}).")
    else:
        s.append("No skill above the known-skill share is without a chain.")
    if a["pool_chains"]:
        s.append(f"In the other direction, {a['chains_on_skills_absent_from_ladder']} of {a['pool_chains']} chains "
                 f"({_pct(a['chains_on_skills_absent_from_ladder'] / a['pool_chains'])}) sit on skills the ladder sample "
                 f"does not contain at all.")
    tp = b["top"]
    if tp["n"]:
        s.append(f"Of the {tp['n']} chains with the highest measured gain, {tp['meta']['n']} count as meta, "
                 f"{tp['known skill, new pairing']['n']} put a new part on a skill players know, and "
                 f"{tp['off-meta skill']['n']} sit on off-meta skills: the graph's best picks are mostly "
                 + ("what players already do." if tp["meta"]["n"] * 2 > tp["n"] else
                    "its own ideas, not copies of the ladder."))
    p = c["same_skill"]["chains"]["dps_vs_median"]
    if p["n"]:
        e = c["same_skill"]["chains"]["ehp_vs_median"]
        word = "weaker than" if p["median"] < 0.8 else ("stronger than" if p["median"] > 1.25 else "about level with")
        s.append(f"Power{' (indicative only: the ladder DPS is not the same measure as ours)' if c['indicative'] else ''}: "
                 f"on the {p['n']} chains whose skill is on the ladder, the typical chain deals {_x(p['median'])} the "
                 f"ladder median for its skill and has {_x(e['median'])} its EHP; {_pct(p['share_reaching'])} reach the "
                 f"median and {_pct(c['same_skill']['chains']['dps_vs_p90']['share_reaching'])} the p90, so the chains "
                 f"are {word} what players run.")
    else:
        s.append("Power: no built chain shares a skill with the ladder sample, so nothing can be said about it.")
    if d["compared"]:
        s.append(f"Supports: {d['compared']} chain builds can be compared; the median overlap with the ladder's top 5 "
                 f"is {d['top5']['jaccard']['median']:.2f} (Jaccard) and on average {_pct(d['top12']['ours_in_ladder_mean'])} "
                 f"of our supports are in the ladder's top 12 for that skill.")
    else:
        s.append("Supports: no chain build with supports is on a ladder skill, so support agreement is unknown.")
    if d["zero_supports"]:
        s.append(f"{d['zero_supports']} of {d['built']} built chains carry no support gems at all "
                 f"({d['zero_supports_skill_from_item']} because an item grants the skill).")
    return " ".join(s)


def compare(chains: list[dict], measured: dict[str, dict], ref: dict | None, top: int = 50,
            predicted: dict[str, float] | None = None) -> dict:
    r = Ref(ref)
    measured = measured or {}
    pc = per_chain(chains, measured, r)
    known_ids = set(pc)
    doc = {"version": 1, "source": r.source, "indicative_power": not r.comparable,
           "thresholds": {"known_share": KNOWN_SHARE, "known_min_n": KNOWN_MIN_N, "paired_frac": PAIRED_FRAC,
                          "paired_min": PAIRED_MIN, "staple_frac": STAPLE_FRAC, "rare_frac": RARE_FRAC},
           "asc_evidence": "notable" if r.has_asc_pairs else "ascendancy",
           "run_chains_not_in_pool": sum(1 for i in measured if i not in known_ids),
           "coverage": _coverage(chains, {i: m for i, m in measured.items() if i in known_ids}, r, pc, predicted),
           "realism": _realism(pc, top), "power": _power(pc, r), "supports": _supports(pc, r)}
    doc["summary_text"] = _summary_text(doc)
    doc["chains"] = {i: rec for i, rec in pc.items() if rec["measured"]}
    return doc


def summary(doc: dict) -> dict:
    """The compact figures printed by the CLI."""
    a, b, c, d = doc["coverage"], doc["realism"], doc["power"], doc["supports"]
    keep = ("skills", "with_chain", "share_covered", "with_feasible_chain", "with_helping_chain",
            "median_best_gain_rank", "median_top_score_rank")
    ch = c["same_skill"]["chains"]
    return {"ladder": {"builds": doc["source"].get("builds"), "skills": a["ladder_skills"],
                       "comparable_to_chainscore": not doc["indicative_power"]},
            "pool_chains": a["pool_chains"], "run_chains": a["run_chains"],
            "A_coverage": {k: {x: v[x] for x in keep} for k, v in a["tops"].items()},
            "A_blind_spots": [x["skill"] for x in a["blind_spots"]][:15], "A_blind_spot_count": len(a["blind_spots"]),
            "A_chains_on_skills_absent_from_ladder": a["chains_on_skills_absent_from_ladder"],
            "B_classes_compared": {k: (v["n"] if isinstance(v, dict) else v) for k, v in b["compared"].items()},
            "B_classes_top": {k: (v["n"] if isinstance(v, dict) else v) for k, v in b["top"].items()},
            "C_power_indicative": c["indicative"],
            "C_dps_vs_ladder_median": ch["dps_vs_median"], "C_dps_vs_ladder_p90": ch["dps_vs_p90"],
            "C_ehp_vs_ladder_median": ch["ehp_vs_median"],
            "C_baseline_dps_vs_ladder_median": c["same_skill"]["baselines"]["dps_vs_median"],
            "D_supports": {"compared": d["compared"], "zero_supports": d["zero_supports"],
                           "top5_jaccard_median": d["top5"]["jaccard"]["median"],
                           "top12_jaccard_median": d["top12"]["jaccard"]["median"],
                           "ours_in_ladder_top12_mean": d["top12"]["ours_in_ladder_mean"],
                           "ladder_top5_in_ours_mean": d["top5"]["ladder_in_ours_mean"]},
            "E_summary": doc["summary_text"]}


def _table(head: list[str], rows: list[list]) -> list[str]:
    def cell(x):
        return "" if x is None else str(x).replace("|", "/")
    return ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"] + \
           ["| " + " | ".join(cell(x) for x in r) + " |" for r in rows] + [""]


def markdown(doc: dict, run: str = "") -> str:
    a, b, c, d = doc["coverage"], doc["realism"], doc["power"], doc["supports"]
    src = doc["source"]
    L = ["# Graph chains against the ladder", ""]
    if c["indicative"]:
        L += ["> **Power figures are indicative only.** The ladder's DPS is not the same measure as the chain builds' "
              "(`comparable_to_chainscore` is false in the reference). Sections A, B and D are not affected.", ""]
    L += [f"Run: `{run or 'n/a'}` with {a['run_chains']} of {a['pool_chains']} chains measured. "
          f"Ladder reference: {src.get('builds', 'n/a')} sampled builds, {src.get('characters', 'n/a')} characters, "
          f"{a['ladder_skills']} main skills. Ladder DPS: {src.get('dps_metric') or 'not stated'}."
          + (f" Note from the reference: {src['note']}" if src.get("note") else ""), "",
          "## Summary (E): what this says about the graph", "", doc["summary_text"], "", "## A. Does the graph find the meta?", ""]
    L += _table(["Ladder skills", "summed share", "has a chain", "summed share covered", "chain in this run",
                 "built feasibly", "chain helps (gain 1.0+)", "median rank of best chain by gain",
                 "median best rank by formula"],
                [[k, _pct(t["ladder_share"]), f"{t['with_chain']}/{t['skills']}", _pct(t["share_covered"]),
                  t["with_measured_chain"], t["with_feasible_chain"], t["with_helping_chain"],
                  t["median_best_gain_rank"], t["median_top_score_rank"]] for k, t in a["tops"].items()])
    L += ["Shares are each skill's share of ladder characters; a character can count towards several skills, so the "
          "sums can pass 100%.", ""]
    L += [f"Ranks by gain are among the {a['ranked_by_gain']} chains with a measured gain; ranks by formula are among "
          f"all {a['pool_chains']} chains.", ""]
    L += _table(["#", "Ladder skill", "share", "chains", "in run", "feasible", "helping", "best gain", "rank by gain",
                 "that chain's formula rank", "skill's best formula rank"],
                [[r["ladder_rank"], r["skill"], f"{100 * r['share']:.1f}%", r["chains"], r["measured"], r["feasible"],
                  r["helping"], r["best_gain"], r["best_gain_rank"], r["best_chain_score_rank"], r["top_score_rank"]]
                 for r in a["skills"][:25]])
    if a["blind_spots"]:
        L += [f"**Blind spots** ({len(a['blind_spots'])} skills at {100 * KNOWN_SHARE:.1f}% share or more with no chain): "
              + ", ".join(f"{x['skill']} ({100 * x['share']:.1f}%)" for x in a["blind_spots"][:40]) + ".", ""]
    else:
        L += [f"No ladder skill at {100 * KNOWN_SHARE:.1f}% share or more is without a chain.", ""]
    L += [f"{a['chains_on_skills_absent_from_ladder']} of {a['pool_chains']} chains "
          f"({a['pool_skills_absent_from_ladder']} of {a['pool_skills']} main skills) are on skills absent from the ladder sample.", "",
          "## B. Are the recommendations real or exotic?", "",
          f"A skill is known at {100 * KNOWN_SHARE:.1f}% share; a part is paired when {100 * PAIRED_FRAC:.0f}% of the "
          f"skill's ladder builds (at least {PAIRED_MIN}) hold it."
          + (" Ascendancy notables are judged by how often the skill is played on that ascendancy, because the "
             "reference has no notable-level pairs." if doc["asc_evidence"] == "ascendancy" else ""), ""]
    L += _table(["Chains", "n"] + list(CLASSES),
                [[name, x["n"]] + [f"{x[k]['n']} ({_pct(x[k]['share'])})" for k in CLASSES]
                 for name, x in ((f"top {b['top_n']} by gain", b["top"]), ("all compared", b["compared"]),
                                 ("whole pool", b["pool"]))])
    L += _table(["Chain", "skill", "gain", "ladder share", "parts (share of the skill's ladder builds)", "class"],
                [[r["id"], r["main_name"], r["gain_credited"],
                  "absent" if r["ladder_share"] is None else f"{100 * r['ladder_share']:.2f}%",
                  "; ".join(f"{p['name']} ({'never' if not p['frac'] else _pct(p['frac'])})" for p in r["parts"]) or
                  ("skill absent from ladder" if r["ladder_share"] is None else "no parts"), r["cls"]]
                 for r in b["top_chains"][:25]])
    L += ["## C. How far in power?", ""]
    if c["indicative"]:
        L += ["Indicative only, see the note at the top.", ""]
    for title, key in (("Same skill", "same_skill"), ("Same skill and ascendancy", "same_skill_and_ascendancy")):
        rows = []
        for who in ("chains", "baselines"):
            for stat, name in (("dps_vs_median", "DPS / ladder median"), ("dps_vs_p90", "DPS / ladder p90"),
                               ("ehp_vs_median", "EHP / ladder median"), ("ehp_vs_p90", "EHP / ladder p90")):
                x = c[key][who][stat]
                rows.append([who if who == "chains" else "plain builds", name, x["n"], x["q25"], x["median"], x["q75"],
                             _pct(x["share_reaching"])])
        L += [f"{title}:", ""] + _table(["Builds", "ratio", "n", "q25", "median", "q75", "share reaching it"], rows)
    if c["chain_profile"].get("binding"):
        L += [f"{c['chain_profile']['binding']} chains have no pinnacle figure in the run and use the run's own DPS, "
              f"which can be the mapping profile's.", ""]
    L += [f"{c['compared_chains']} chains compared; {c['comparable_without_ladder_skill']} more have damage but no "
          f"ladder skill to compare with.", "", "## D. Do the support gems agree?", "",
          f"{d['compared']} chain builds on {d['skills_compared']} ladder skills have supports to compare "
          f"(median {d['supports_per_build']} supports each). {d['zero_supports']} of {d['built']} built chains have "
          f"no supports ({d['zero_supports_skill_from_item']} because an item grants the skill) and are left out; "
          f"{d['with_supports_skill_absent_from_ladder']} have supports but a skill the ladder does not have, and "
          f"{d['with_supports_wrong_main']} deal their damage with another skill.", ""]
    L += _table(["Against the ladder's", "Jaccard q25", "median", "q75", "our supports found there",
                 "ladder's found in ours", "builds with no overlap"],
                [[k, d[k]["jaccard"]["q25"], d[k]["jaccard"]["median"], d[k]["jaccard"]["q75"],
                  _pct(d[k]["ours_in_ladder_mean"]), _pct(d[k]["ladder_in_ours_mean"]), d[k]["no_overlap"]]
                 for k in ("top5", "top12")])
    if d["ours_often_ladder_rarely"]:
        L += ["Supports our search picks that ladder players rarely use on the same skill:", ""]
        L += _table(["Support", "times picked", "mean ladder share on those skills", "picks where the ladder never has it"],
                    [[x["support"], x["picked"], _pct(x["ladder_frac_mean"]), x["never_on_ladder_for_skill"]]
                     for x in d["ours_often_ladder_rarely"][:12]])
    if d["ladder_staples_we_never_pick"]:
        L += [f"Ladder staples ({100 * STAPLE_FRAC:.0f}% or more of a skill's builds) that no chain on that skill picked:", ""]
        L += _table(["Support", "skills", "mean ladder share", "picked anywhere in the run", "on"],
                    [[x["support"], x["skills"], _pct(x["ladder_frac_mean"]),
                      "yes" if x["picked_anywhere_in_run"] else "no", ", ".join(x["on"])]
                     for x in d["ladder_staples_we_never_pick"][:12]])
    return "\n".join(L)


def load_run(run_dir: str) -> dict[str, dict]:
    """chainresults.measured plus, from the run's files: each chain's ascendancy, its plain build's EHP, and the
    pinnacle_v1 DPS and EHP of both builds (measured's own dps is the binding profile's, sometimes mapping_v1's)."""
    from . import chainresults
    res = chainresults.measured(run_dir)
    base = {r.get("base_key"): r for r in chainresults._read(os.path.join(run_dir, "baselines.jsonl"))}
    for r in chainresults._read(os.path.join(run_dir, "chain_scores.jsonl")):
        m = res.get(r.get("id"))
        if m is None:
            continue
        if r.get("ascendancy"):
            m["ascendancy"] = r["ascendancy"]
        b = base.get(r.get("base_key"))
        for pre, row in (("", r), ("base_", b)):
            if row and row.get("pinnacle_v1.dps_honest") is not None:
                m[pre + "pinnacle_dps"] = round(row["pinnacle_v1.dps_honest"])
                m[pre + "pinnacle_ehp"] = round(row["pinnacle_v1.ehp"]) if row.get("pinnacle_v1.ehp") else None
        if b:
            m["base_key"] = r["base_key"]
            if b.get("ehp"):
                m["base_ehp"] = round(b["ehp"])
    return res


def main(argv=None) -> int:
    from . import chainfit, chainscore
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scores", required=True, help="chainscore run directory")
    ap.add_argument("--ref", default=REF_PATH, help="ladder reference file")
    ap.add_argument("--out", default=None, help="output directory (default: the run directory)")
    ap.add_argument("--top", type=int, default=50, help="how many of the best chains by gain to classify")
    a = ap.parse_args(argv)
    if not os.path.exists(a.ref):
        print(f"no ladder reference at {a.ref} (python3 -m exilegen.builder.ladderref writes it)", file=sys.stderr)
        return 2
    ref = json.load(open(a.ref))
    chains = chainscore.pool_chains()
    res = load_run(a.scores)
    model = chainfit.load()
    predicted = {c["id"]: chainfit.predict(model, c)["score"] for c in chains} if model else None
    doc = compare(chains, res, ref, top=a.top, predicted=predicted)
    doc["run"] = os.path.abspath(a.scores)
    out = a.out or a.scores
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "ladder_compare.json"), "w") as fh:
        json.dump(doc, fh, indent=1)
    with open(os.path.join(out, "ladder_compare.md"), "w") as fh:
        fh.write(markdown(doc, a.scores))
    s = summary(doc)
    s["wrote"] = [os.path.join(out, "ladder_compare.json"), os.path.join(out, "ladder_compare.md")]
    print(json.dumps(s, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
