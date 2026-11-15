"""Ladder comparison tests: a small synthetic reference and measured dict, no engine, catalogue or run needed."""
from __future__ import annotations

import json

from exilegen.builder import laddercmp as lc


def _chain(cid, skill, score, uniques=(), keystones=(), asc=()):
    return {"id": cid, "kind": "enabler", "main_name": skill, "score": score, "uniques": list(uniques),
            "keystones": list(keystones), "asc_nodes": [list(a) for a in asc], "tokens": []}


CHAINS = [
    _chain("a", "Spark", 0.9, uniques=["Indigon"]),
    _chain("b", "Spark", 0.8, keystones=["Blood Magic"]),
    _chain("c", "Spark", 0.7, asc=[("Stormweaver", "Tempest Caller")]),
    _chain("d", "Odd Skill", 0.6, uniques=["Indigon"]),
    _chain("e", "Rare Skill", 0.5, uniques=["Indigon"]),
    _chain("f", "Fireball", 0.4, uniques=["Nothing"]),
    _chain("g", "Fireball", 0.3, keystones=["Blood Magic"]),
]

REF = {
    "version": 1,
    "source": {"builds": 100, "characters": 1000, "dps_metric": "test", "comparable_to_chainscore": True, "note": ""},
    "skills": {
        "Spark": {"n": 40, "share": 0.30, "dps": {"median": 1000, "p90": 4000, "max": 9000},
                  "ehp": {"median": 10000, "p90": 20000}, "ascendancies": [["Stormweaver", 0.6]],
                  "supports": [["Rapid Casting II", 0.8], ["Pierce", 0.7], ["Lightning Mastery", 0.6], ["Overcharge", 0.5],
                               ["Acceleration", 0.3], ["Zenith", 0.1]],
                  "uniques": [["Indigon", 0.4]], "keystones": [["Elemental Equilibrium", 0.2]]},
        "Ice Nova": {"n": 20, "share": 0.20, "dps": {"median": 500, "p90": 900, "max": 1000},
                     "ehp": {"median": 1, "p90": 2}, "ascendancies": [], "supports": [], "uniques": [], "keystones": []},
        "Fireball": {"n": 10, "share": 0.10, "dps": None, "ehp": None, "ascendancies": None, "supports": None,
                     "uniques": None, "keystones": None},
        "Rare Skill": {"n": 1, "share": 0.001, "dps": {"median": 100, "p90": 100, "max": 100},
                       "ehp": {"median": 100, "p90": 100}, "ascendancies": [], "supports": [], "uniques": [],
                       "keystones": []},
    },
    "combos": {"Spark||Stormweaver": {"n": 24, "share": 0.18, "dps": {"median": 2000, "p90": 5000, "max": 9000},
                                      "ehp": {"median": 8000, "p90": 16000}}},
    "pairs": {"unique_skill": {"Indigon||Spark": 16}, "keystone_skill": {"Blood Magic||Spark": 1},
              "asc_node_skill": {"Stormweaver:Tempest Caller||Spark": 12}},
}

MEASURED = {
    "a": {"built": True, "feasible": True, "dps": 2000, "ehp": 5000, "base_dps": 1000, "base_key": "k", "gain_credited": 2.0,
          "supports": ["Rapid Casting I", "Pierce", "Fork"], "ascendancy": "Stormweaver"},
    "b": {"built": True, "feasible": True, "dps": 500, "ehp": 30000, "base_dps": 1000, "base_key": "k", "gain_credited": 0.5,
          "supports": []},
    "c": {"built": True, "feasible": True, "dps": 9000, "ehp": None, "wrong_main": "Arc", "supports": ["Fork"]},
    "d": {"built": True, "feasible": True, "dps": 700, "ehp": 100, "base_dps": 100, "gain_credited": 7.0,
          "supports": ["Fork"]},
    "f": {"built": False, "feasible": False, "dps": 0, "ehp": None, "error": "boom"},
    "zz": {"built": True, "feasible": True, "dps": 1, "ehp": 1, "gain_credited": 1.0},
}


def test_per_chain_classes_parts_and_power():
    pc = lc.per_chain(CHAINS, MEASURED, REF)
    assert set(pc) == {c["id"] for c in CHAINS}
    a = pc["a"]
    assert a["cls"] == "meta" and a["in_ref"] and a["ladder"] == {"n": 40, "share": 0.3, "rank": 1}
    assert a["parts"][0]["pair_n"] == 16 and a["parts"][0]["frac"] == 0.4 and a["parts"][0]["paired"]
    assert a["power"]["skill"]["dps_vs_median"] == 2.0 and a["power"]["skill"]["dps_vs_p90"] == 0.5
    assert a["power"]["skill"]["ehp_vs_median"] == 0.5 and a["power"]["skill"]["base_dps_vs_median"] == 1.0
    assert a["power"]["combo"]["dps_vs_median"] == 1.0
    # one sampled build out of 40 holding the keystone is not a common pairing
    assert pc["b"]["cls"] == "known skill, new pairing" and not pc["b"]["parts"][0]["paired"]
    assert pc["b"]["power"]["combo"] is None
    assert pc["c"]["parts"][0]["paired"] and pc["c"]["ascendancy"] == "Stormweaver"
    assert not pc["c"]["comparable"] and pc["c"]["power"]["skill"]["dps_vs_median"] is None
    # absent from the reference, too rare on it, and never measured
    assert pc["d"]["cls"] == "off-meta skill" and not pc["d"]["in_ref"] and pc["d"]["power"] is None
    assert pc["e"]["cls"] == "off-meta skill" and pc["e"]["in_ref"] and not pc["e"]["measured"]
    assert pc["g"]["cls"] == "known skill, new pairing" and pc["g"]["supports"] is None
    assert pc["f"]["power"]["skill"]["dps_vs_median"] is None and pc["f"]["supports"] is None


def test_supports_are_compared_by_family():
    pc = lc.per_chain(CHAINS, MEASURED, REF)
    s = pc["a"]["supports"]["vs_ladder"]
    assert s["top5"]["shared"] == 2 and s["top5"]["jaccard"] == round(2 / 6, 4)
    assert s["top5"]["ours_in_ladder"] == round(2 / 3, 4) and s["top5"]["ladder_in_ours"] == 0.4
    assert s["top12"]["ladder"] == 6
    assert pc["b"]["supports"]["n"] == 0 and pc["b"]["supports"]["vs_ladder"] is None
    assert pc["d"]["supports"]["vs_ladder"] is None
    assert lc.support_family("Rapid Attacks II") == lc.support_family("rapid attacks i") == "rapid attacks"
    assert lc.support_family("Xoph's Pyre") == "xoph's pyre"


def test_ascendancy_falls_back_to_how_often_the_skill_is_played_there():
    ref = json.loads(json.dumps(REF))
    del ref["pairs"]["asc_node_skill"]
    p = lc.per_chain(CHAINS, MEASURED, ref)["c"]["parts"][0]
    assert p["evidence"] == "ascendancy" and p["frac"] == 0.6 and p["paired"] and p["pair_n"] is None


def test_compare_answers_each_question():
    doc = lc.compare(CHAINS, MEASURED, REF, top=2)
    cov = doc["coverage"]
    assert cov["pool_chains"] == 7 and cov["run_chains"] == 5 and doc["run_chains_not_in_pool"] == 1
    t = cov["tops"]["top10"]
    assert t["skills"] == 4 and t["with_chain"] == 3 and t["with_feasible_chain"] == 1 and t["with_helping_chain"] == 1
    assert t["no_chain"] == ["Ice Nova"]
    assert [b["skill"] for b in cov["blind_spots"]] == ["Ice Nova"]
    spark = cov["skills"][0]
    assert spark["skill"] == "Spark" and spark["chains"] == 3 and spark["measured"] == 3 and spark["feasible"] == 3
    assert spark["best_chain"] == "a" and spark["best_gain_rank"] == 2 and spark["best_chain_score_rank"] == 1
    assert cov["chains_on_skills_absent_from_ladder"] == 1
    real = doc["realism"]
    assert [r["id"] for r in real["top_chains"]] == ["d", "a"]
    assert real["top"]["off-meta skill"]["n"] == 1 and real["top"]["meta"]["n"] == 1
    assert real["compared"]["n"] == 3 and real["compared"]["known skill, new pairing"]["n"] == 1
    pw = doc["power"]
    assert pw["indicative"] is False and pw["compared_chains"] == 2
    d = pw["same_skill"]["chains"]["dps_vs_median"]
    assert d["n"] == 2 and d["median"] == 1.25 and d["share_reaching"] == 0.5
    assert pw["same_skill"]["chains"]["dps_vs_p90"]["share_reaching"] == 0.0
    assert pw["same_skill"]["baselines"]["dps_vs_median"]["n"] == 1
    sup = doc["supports"]
    assert sup["compared"] == 1 and sup["zero_supports"] == 1 and sup["built"] == 4
    assert [x["support"] for x in sup["ours_often_ladder_rarely"]] == ["Fork"]
    assert {x["support"] for x in sup["ladder_staples_we_never_pick"]} == {"Lightning Mastery", "Overcharge"}
    assert set(doc["chains"]) == {"a", "b", "c", "d", "f"}
    json.dumps(doc)
    md = lc.markdown(doc, "run")
    assert "Ice Nova" in md and "indicative only" not in md
    assert lc.summary(doc)["A_blind_spots"] == ["Ice Nova"]


def test_power_is_marked_indicative_when_the_ladder_metric_differs():
    ref = json.loads(json.dumps(REF))
    ref["source"]["comparable_to_chainscore"] = False
    doc = lc.compare(CHAINS, MEASURED, ref)
    assert doc["indicative_power"] and doc["power"]["indicative"] and lc.summary(doc)["C_power_indicative"]
    assert lc.markdown(doc).splitlines()[2].startswith("> **Power figures are indicative only.**")
    assert "indicative only" in doc["summary_text"]


def test_missing_data_does_not_crash():
    for ref in (None, {}, {"skills": {}, "pairs": {}}, {"skills": {"Spark": {}}, "combos": None, "pairs": None}):
        for meas in ({}, None, MEASURED):
            doc = lc.compare(CHAINS, meas, ref)
            assert lc.markdown(doc) and lc.summary(doc)["pool_chains"] == 7
    doc = lc.compare([], {}, REF)
    assert doc["coverage"]["tops"]["top10"]["with_chain"] == 0 and lc.markdown(doc)


def test_cli_writes_both_files(tmp_path, monkeypatch, capsys):
    from exilegen.builder import chainscore
    run = tmp_path / "run"
    run.mkdir()
    (run / "chain_scores.jsonl").write_text(json.dumps(
        {"id": "a", "main_name": "Spark", "ok": True, "feasible": True, "dps_honest": 2000.0, "ehp": 5000.0,
         "base_key": "k", "ascendancy": "Stormweaver", "pinnacle_v1.main": "Spark"}) + "\n")
    (run / "baselines.jsonl").write_text(json.dumps(
        {"base_key": "k", "main_name": "Spark", "ok": True, "feasible": True, "dps_honest": 1000.0, "ehp": 4000.0}) + "\n")
    ref = tmp_path / "ref.json"
    ref.write_text(json.dumps(REF))
    monkeypatch.setattr(chainscore, "pool_chains", lambda top=100000: CHAINS)
    assert lc.main(["--scores", str(run), "--ref", str(ref), "--out", str(tmp_path / "out"), "--top", "5"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run_chains"] == 1 and out["C_dps_vs_ladder_median"]["median"] == 2.0
    doc = json.loads((tmp_path / "out" / "ladder_compare.json").read_text())
    assert doc["chains"]["a"]["ascendancy"] == "Stormweaver" and doc["chains"]["a"]["power"]["base_ehp"] == 4000
    assert (tmp_path / "out" / "ladder_compare.md").read_text().startswith("# Graph chains against the ladder")
    assert lc.main(["--scores", str(run), "--ref", str(tmp_path / "missing.json")]) == 2
