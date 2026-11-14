"""Rebuild the scoring test fixtures (run by hand; tests only read the JSON it writes).

    python3 tests/fixtures/scoring/make_fixtures.py --pob <PoB-PoE2 root> [--project /mnt/project-files]

Writes, without PoB codes or account/character hashes:
  corpus_sample.json   a few evaluated.jsonl records (tags + both profiles' stats)
  one_button_poc.json  the one-button POC driver output (baseline/final energy facts, groups enriched with
                       gem ids/types from PoB data) + results.csv corrected factors
  support_opt_poc.json support-opt POC per-build supports before/strict/best and DPS
"""
import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "..")))
from exilegen.scoring import pobdata  # noqa: E402

CORPUS_LINES = [3, 4, 11, 33, 219, 563, 852]


def gem_index(pob):
    idx = {}
    for sid, sd in pobdata.read_skills(pob).items():
        if sd.gem_name and not sd.hidden and sd.gem_name not in idx:
            idx[sd.gem_name] = {"id": sid, "support": sd.support, "types": sd.skill_types, "cast_time": sd.cast_time}
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pob", required=True)
    ap.add_argument("--project", default="/mnt/project-files")
    a = ap.parse_args()
    gi = gem_index(a.pob)

    recs = []
    for line in open(os.path.join(a.project, "corpus/poe2/evaluated.jsonl")):
        d = json.loads(line)
        if d.get("line") in CORPUS_LINES:
            recs.append({"line": d["line"], "tags": d["tags"], "pinnacle_locked": d.get("pinnacle_locked"),
                         "as_exported": d.get("as_exported")})
    for r in recs:  # drop free-text samples
        r["tags"].pop("unsupportedModSample", None)
    json.dump(recs, open(os.path.join(HERE, "corpus_sample.json"), "w"), indent=0)

    ob = []
    for line in open(os.path.join(a.project, "poc/one-button/raw_driver_output.jsonl")):
        d = json.loads(line)
        r = d["r"]
        r.pop("code", None)
        r.pop("log", None)
        for g in r.get("groups") or []:
            g["gem_info"] = [gi.get(n, {"id": None}) for n in g["gems"]]
        ob.append({"id": d["id"], "r": r})
    rows = list(csv.DictReader(open(os.path.join(a.project, "poc/one-button/results.csv"))))
    json.dump({"runs": ob, "results": rows, "gem_index_subset": {
        n: gi[n] for n in ("Hexblast", "Detonate Dead", "Comet", "Snap", "Arc", "Frost Wall", "Spark", "Cast on Critical",
                           "Cast on Elemental Ailment", "Living Bomb", "Shattering Palm", "Blasphemy", "Temporal Chains")}},
              open(os.path.join(HERE, "one_button_poc.json"), "w"))

    so = []
    for line in open(os.path.join(a.project, "poc/support-opt/builds.jsonl")):
        d = json.loads(line)
        so.append({k: d.get(k) for k in ("id", "skill", "asc", "base_dps", "strict_dps", "best_dps", "supports_before",
                                          "supports_after_strict", "supports_after_best", "best_case_conditional_new",
                                          "removal_loss_pct", "ehp_before", "ehp_after_strict")})
    for s in so:
        s["support_ids"] = {n: (gi.get(n) or {}).get("id") for n in
                            set(s["supports_before"] + s["supports_after_strict"] + s["supports_after_best"])}
    json.dump(so, open(os.path.join(HERE, "support_opt_poc.json"), "w"), indent=0)
    print("ok", len(recs), len(ob), len(so))


if __name__ == "__main__":
    main()
