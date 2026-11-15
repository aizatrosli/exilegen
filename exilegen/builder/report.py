"""Markdown + CSV report of a builder run directory.

    python3 -m exilegen build-report --run-dir runs/ob1        -> runs/ob1/report.md, runs/ob1/builds.csv
    from exilegen.builder.report import write_report; write_report("runs/ob1")

Sections: run summary and coverage; top builds overall (verified, then unverified-but-feasible);
per constraint (standard constraints re-checked on the stored skill view); per ascendancy; per defining
unique; "most surprising" (best builds in cells whose (main skill, ascendancy) is rare or unseen in the
poe.ninja corpus); source success rates. Every build lists its skeleton thesis/source, honest DPS, EHP,
lowest max hit, flags and the PoB code.
"""
from __future__ import annotations

import csv
import json
import math
import os
import time

from .archive import AXES, Archive, Entry, primary_doc

REPORT_CONSTRAINTS = ["one_button", "one_button:strict", "no_cast", "no_cast:strict", "no_minions"]
SURPRISE_BUCKETS = ("unseen", "rare", "niche")


def _f(v, nd=0) -> str:
    if v is None:
        return "?"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isinf(v) or math.isnan(v):
        return "inf"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.2f}M"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:.{nd}f}" if nd else f"{v:.2f}"


def _flags(e: Entry, limit: int = 6) -> list[str]:
    fl = [str(x) for x in ((e.rec.get("score") or {}).get("flags") or [])]
    unv = [x for x in fl if "unverified" in x]
    other = [x for x in fl if "unverified" not in x]
    return (unv + other)[:limit] + ([f"... +{len(fl) - limit} more"] if len(fl) > limit else [])


def constraint_ok(e: Entry, constraint: str) -> bool | None:
    """Re-check a constraint on the stored skill view (None when the record has no skill data)."""
    try:
        from exilegen.scoring import BuildView
        from exilegen.scoring import constraints as C
    except ImportError:
        return None
    doc = primary_doc(e.rec.get("stats"))
    skills = e.rec.get("skills")
    if not doc and not skills:
        return None
    bv = BuildView(doc or {}, skills)
    if not bv.groups():
        return None
    viol, flags = C.evaluate([constraint], bv)
    return not viol


def _status(e: Entry) -> str:
    return {0: "feasible, verified", 1: "feasible, UNVERIFIED", 2: "INFEASIBLE"}[e.tier]


def build_block(e: Entry, n: int | None = None, code: bool = True) -> list[str]:
    sk = e.skeleton
    d = e.rec.get("descriptor") or {}
    s = e.rec.get("score") or {}
    head = f"{n}. " if n is not None else ""
    L = [f"{head}**{sk.get('ascendancy')} - {d.get('main_skill') or sk.get('main_skill')}**"
         f"  (objective {_f(e.objective, 2)}, {_status(e)})",
         f"   - honest DPS **{_f(e.dps_honest)}** (raw {_f(s.get('dps_raw'))}), EHP **{_f(e.ehp)}**,"
         f" max hit {_f(e.rec.get('max_hit'))}, binding profile {s.get('binding_profile') or s.get('profile')}",
         f"   - cell `{e.cell}`; corpus share {_f((d.get('corpus_share') or 0) * 100, 2)}%",
         f"   - source `{sk.get('source') or '?'}`" + (f", parent `{sk.get('parent')}`" if sk.get("parent") else "")
         + f", skeleton `{sk.get('id')}`"]
    if sk.get("thesis"):
        L.append(f"   - thesis: {sk['thesis']}")
    bits = []
    if sk.get("supports"):
        bits.append("supports " + ", ".join(sk["supports"]))
    if sk.get("trigger"):
        bits.append("trigger " + json.dumps(sk["trigger"]))
    if sk.get("uniques"):
        bits.append("uniques " + ", ".join(u.get("name", "?") if isinstance(u, dict) else str(u) for u in sk["uniques"]))
    if sk.get("keystones"):
        bits.append("keystones " + ", ".join(sk["keystones"]))
    if sk.get("weapon"):
        bits.append(f"weapon {sk['weapon']}")
    if bits:
        L.append("   - skeleton: " + "; ".join(bits))
    fl = _flags(e)
    if fl:
        L.append("   - flags: " + "; ".join(f"`{x}`" for x in fl))
    for v in (s.get("violations") or [])[:3]:
        L.append(f"   - violation: {v.get('rule')} {v.get('entry')}: {v.get('detail')}")
    if code and e.rec.get("code"):
        L.append(f"   <details><summary>PoB code</summary>\n\n   ```\n   {e.rec['code']}\n   ```\n   </details>")
    return L


def _table(rows: list[list], header: list[str]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "/") for x in r) + " |")
    return out


def render(archive: Archive, extra: dict | None = None, top: int = 15, per_group: int = 3) -> str:
    st = archive.stats()
    runner = (extra or {}).get("runner") or (archive.meta or {}).get("runner") or {}
    feasible = archive.all_entries((0, 1))
    verified = [e for e in feasible if e.tier == 0]
    unverified = [e for e in feasible if e.tier == 1]
    L = [f"# Builder run report: `{os.path.basename(os.path.normpath(archive.run_dir or '.'))}`", "",
         f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')}. Constraints: `{runner.get('constraints') or []}`;"
         f" profiles: `{runner.get('profiles') or 'default'}`.", ""]
    secs = runner.get("seconds_total") or 0
    jobs = (runner.get("counts") or {}).get("jobs")
    L += ["## Summary", ""]
    L += _table([
        ["records (evaluated skeletons)", st["records"]],
        ["run time (all sessions)", f"{secs / 60:.1f} min" if secs else "?"],
        ["jobs this session / per hour", f"{jobs} / {jobs / max(1e-9, (runner.get('seconds_total') or 1) / 3600):.0f}" if jobs else "?"],
        ["cells touched", st["cells"]],
        ["cells with a feasible build", st["cells_feasible"]],
        ["cells with a feasible verified build", st["cells_verified"]],
        ["quarantined (feasible but no honest damage)", st.get("quarantined", 0)],
        ["QD-score (sum of feasible elite objectives)", _f(st["qd_score"], 1)],
        ["QD-score verified only", _f(st["qd_score_verified"], 1)],
        ["axis coverage (distinct values among feasible cells)",
         ", ".join(f"{a} {n}" for a, n in st["axis_coverage"].items())],
        ["outcomes", ", ".join(f"{k} {v}" for k, v in sorted(((runner.get("counts") or {}).get("status") or {}).items()))],
    ], ["metric", "value"])
    L.append("")
    if st["sources"]:
        L += ["### Skeleton sources", ""]
        L += _table([[k, v["n"], v["ok"], v["feasible"], v["verified"], v["new_cell"], v["improved"],
                      f"{v['success_rate']:.0%}", f"{v['seconds'] / max(1, v['n']):.0f}s"]
                     for k, v in sorted(st["sources"].items(), key=lambda kv: -kv[1]["n"])],
                    ["source", "jobs", "ok", "feasible", "verified", "new cells", "improved", "success", "avg time"])
        L.append("")
    # coverage per axis
    L += ["### Coverage per axis (feasible cells)", "", f"Cells are keyed on: {', '.join(archive.axes)}.", ""]
    for a in archive.axes:
        cnt: dict[str, int] = {}
        for c in archive.cells.values():
            if c.best_tier() <= 1:
                cnt[str(c.desc.get(a))] = cnt.get(str(c.desc.get(a)), 0) + 1
        if cnt:
            L.append(f"- **{a}**: " + ", ".join(f"{k} {v}" for k, v in sorted(cnt.items(), key=lambda kv: -kv[1])[:20]))
    L.append("")

    L += ["## Top builds overall (feasible, verified)", ""]
    if not verified:
        L.append("_none yet_")
    for i, e in enumerate(_dedupe_cells(verified)[:top], 1):
        L += build_block(e, i)
    L.append("")
    L += ["## Top feasible but UNVERIFIED builds", "",
          "Ranked below verified builds: their DPS rests on something PoB does not model (e.g. Energy-gated"
          " triggers). Human review needed.", ""]
    if not unverified:
        L.append("_none_")
    for i, e in enumerate(_dedupe_cells(unverified)[:max(5, top // 2)], 1):
        L += build_block(e, i)
    L.append("")

    L += ["## Most surprising", "",
          "Best feasible builds whose (main skill, ascendancy) is rare or unseen in the ladder corpus.", ""]
    sur = [e for e in feasible if (e.rec.get("descriptor") or {}).get("corpus") in SURPRISE_BUCKETS]
    sur.sort(key=lambda e: (e.tier, -(e.objective or -1e9)))
    if not sur:
        L.append("_none_")
    for i, e in enumerate(_dedupe_cells(sur)[:10], 1):
        L += build_block(e, i)
    L.append("")

    L += ["## Per constraint", "",
          "Standard constraints re-checked on each build's stored skill view (the run's own constraints are"
          " already enforced by the scorer).", ""]
    for c in REPORT_CONSTRAINTS:
        ok = [e for e in feasible if constraint_ok(e, c)]
        L.append(f"### `{c}` ({len(ok)} feasible builds pass)")
        L.append("")
        for i, e in enumerate(_dedupe_cells(ok)[:per_group], 1):
            L += build_block(e, i, code=False)
        L.append("")

    L += ["## Per ascendancy", "", "_PoB codes: in the lists above or in builds.csv (by skeleton id)._", ""]
    by_asc: dict[str, list[Entry]] = {}
    for e in feasible:
        by_asc.setdefault(str((e.rec.get("descriptor") or {}).get("ascendancy")), []).append(e)
    for asc, es in sorted(by_asc.items(), key=lambda kv: kv[1][0].rank()):
        L.append(f"### {asc} ({len(es)})")
        L.append("")
        for i, e in enumerate(_dedupe_cells(es)[:per_group], 1):
            L += build_block(e, i, code=False)
        L.append("")

    L += ["## Per defining unique", ""]
    by_u: dict[str, list[Entry]] = {}
    for e in feasible:
        u = str((e.rec.get("descriptor") or {}).get("unique"))
        if u != "none":
            by_u.setdefault(u, []).append(e)
    if not by_u:
        L.append("_no unique-defined builds yet_")
    for u, es in sorted(by_u.items(), key=lambda kv: kv[1][0].rank()):
        L.append(f"### {u} ({len(es)})")
        L.append("")
        for i, e in enumerate(_dedupe_cells(es)[:per_group], 1):
            L += build_block(e, i, code=False)
        L.append("")

    fails = [r for r in archive.records.values() if r.get("status") == "failed"]
    if fails:
        L += ["## Failures", ""]
        errs: dict[str, int] = {}
        for r in fails:
            k = (r.get("error") or "no result").split("\n")[0][:120]
            errs[k] = errs.get(k, 0) + 1
        L += _table([[v, k] for k, v in sorted(errs.items(), key=lambda kv: -kv[1])[:15]], ["count", "error"])
        L.append("")
    return "\n".join(L)


def _dedupe_cells(es: list[Entry]) -> list[Entry]:
    """Sorted by rank, at most one entry per cell (the cell's best), so lists show distinct ideas."""
    seen, out = set(), []
    for e in sorted(es, key=Entry.rank):
        if e.cell in seen:
            continue
        seen.add(e.cell)
        out.append(e)
    return out


CSV_COLS = ["uid", "status", "tier", "feasible", "verified", "objective", "dps_honest", "dps_raw", "ehp", "max_hit",
            "cls", "ascendancy", "main_skill", *[f"d_{a}" for a in AXES], "corpus_share", "source", "parent",
            "thesis", "flags", "violations", "evals", "seconds", "error", "code"]


def write_csv(archive: Archive, path: str) -> str:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        in_cells = {e.uid: e for e in archive.all_entries()}
        for uid, r in archive.records.items():
            sk = r.get("skeleton") or {}
            s = r.get("score") or {}
            d = r.get("descriptor") or {}
            e = in_cells.get(uid)
            w.writerow([uid, r.get("status"), e.tier if e else "", s.get("feasible"), s.get("verified"),
                        s.get("objective"), s.get("dps_honest"), s.get("dps_raw"), s.get("ehp"), r.get("max_hit"),
                        sk.get("cls"), sk.get("ascendancy"), d.get("main_skill") or sk.get("main_skill"),
                        *[d.get(a) for a in AXES], d.get("corpus_share"), sk.get("source"), sk.get("parent"),
                        sk.get("thesis"), "; ".join(map(str, s.get("flags") or [])),
                        "; ".join(f"{v.get('rule')}:{v.get('entry')}" for v in s.get("violations") or []),
                        r.get("evals"), round(float(r.get("seconds") or 0), 1), (r.get("error") or "")[:300],
                        r.get("code") or ""])
    return path


def write_report(run_dir: str, archive: Archive | None = None, extra: dict | None = None,
                 out_dir: str | None = None, redescribe: bool = False, axes=None) -> dict:
    """Write report.md + builds.csv (into out_dir, default the run directory). -> {md, csv}."""
    arch = archive or Archive.open(run_dir, redescribe=redescribe, axes=axes)
    out_dir = out_dir or run_dir
    os.makedirs(out_dir, exist_ok=True)
    md = render(arch, extra)
    p_md = os.path.join(out_dir, "report.md")
    tmp = p_md + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(md)
    os.replace(tmp, p_md)
    p_csv = write_csv(arch, os.path.join(out_dir, "builds.csv.tmp"))
    os.replace(p_csv, os.path.join(out_dir, "builds.csv"))
    return {"md": p_md, "csv": os.path.join(out_dir, "builds.csv")}
