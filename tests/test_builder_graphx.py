"""Graph export tests (catalogue cache only: no PoB data, engine or corpus mount needed)."""
from __future__ import annotations

import os

import pytest

from exilegen.builder import catalog as catmod
from exilegen.builder import graphx

pytestmark = pytest.mark.skipif(not os.path.exists(os.path.join(catmod.DATA_DIR, "skills.json")),
                                reason="builder catalogue cache missing (python -m exilegen.builder.catalog)")


@pytest.fixture(scope="module")
def cat():
    return catmod.load()


def test_failed_requirements_stop_the_analysis(cat):
    doc = graphx.run({"constraints": ["bogus:x"], "ascendancy": "Lich", "cls": "Ranger",
                      "uniques": [{"name": "Nope"}], "zzz": 1}, cat=cat)
    failed = {c["id"] for c in doc["preflight"] if c["status"] == "fail"}
    assert {"req.keys", "req.constraints", "req.ascendancy", "req.uniques"} <= failed
    assert not doc["passed"] and "mechanics" not in doc


def test_check_only_never_analyses(cat):
    doc = graphx.run({}, check_only=True, cat=cat)
    assert doc["passed"] and "mechanics" not in doc


def test_export_respects_requirements(cat):
    doc = graphx.run({"ascendancy": "Lich", "damage_types": ["Chaos"], "constraints": ["no_minions"], "top": 40}, cat=cat)
    m = doc["mechanics"]
    assert doc["passed"] and 0 < len(m["chains"]) <= 40
    ids = {n["id"] for n in m["nodes"]}
    for c in m["chains"]:
        assert {a for a, _ in c["asc_nodes"]} <= {"Lich"}
        assert "Chaos" in cat.skills[c["main_skill"]]["damage_types"]
        assert set(c["path"]) | set(c["path_tokens"]) <= ids
    assert all(e["s"] in ids and e["t"] in ids for e in m["edges"])


def test_chains_are_weighted_by_tempo(cat):
    doc = graphx.run({"top": 200}, cat=cat)
    chains = doc["mechanics"]["chains"]
    assert chains == sorted(chains, key=lambda c: (-c["score"], c["id"]))
    for c in chains:
        t = c["tempo"]
        assert 0 < t["tempo"] <= 1 and abs(c["score"] - c["score_raw"] * t["tempo"]) < 1e-3
        assert set(t["factors"]) == {"resource", "spirit", "cooldown", "time"}
    assert any(c["tempo"]["tempo"] < 1 for c in chains)


def test_tight_budget_lowers_scores(cat):
    loose = {c["id"]: c["score"] for c in graphx.run({"top": 2000}, cat=cat)["mechanics"]["chains"]}
    tight = {c["id"]: c["score"] for c in graphx.run({"top": 2000, "mana_per_second": 40, "spirit": 30}, cat=cat)["mechanics"]["chains"]}
    both = set(loose) & set(tight)
    assert both and all(tight[i] <= loose[i] + 1e-9 for i in both) and any(tight[i] < loose[i] for i in both)
    assert not graphx.run({"mana_per_second": 0}, cat=cat)["passed"]


def test_leveling_path_and_ranking(cat):
    from exilegen.builder import leveling
    if not leveling.load():
        pytest.skip("leveling data missing")
    plain = graphx.run({"top": 400}, cat=cat)["mechanics"]["chains"]
    assert all(c["leveling"]["online_level"] == max(s["level"] for s in c["leveling"]["steps"]) for c in plain)
    assert all([s["level"] for s in c["leveling"]["steps"]] == sorted(s["level"] for s in c["leveling"]["steps"]) for c in plain)
    by_id = {c["id"]: c for c in plain}
    ranked = graphx.run({"top": 400, "leveling": True}, cat=cat)["mechanics"]["chains"]
    assert not any(c["leveling"]["unobtainable"] for c in ranked)
    for c in ranked:
        if c["id"] in by_id:
            assert c["score"] == pytest.approx(by_id[c["id"]]["score"] * c["leveling"]["factor"], abs=2e-4)
    early = graphx.run({"top": 400, "online_by": 30}, cat=cat)["mechanics"]["chains"]
    assert early and all(c["leveling"]["online_level"] <= 30 for c in early)


def test_leveling_factor_and_levels():
    from exilegen.builder import leveling
    cfg = leveling.DEFAULT_CONFIG
    assert leveling.factor(20, 0, False) == 1.0
    assert leveling.factor(90, 0, False) == cfg["late_factor"]
    assert leveling.factor(20, 1, False) == cfg["boss_factor"]
    assert leveling.factor(20, 0, True) == 0.0
    assert leveling.level_for_passives(1) <= leveling.level_for_passives(40) <= leveling.level_for_passives(100)
    assert [leveling.level_for_ascendancy(p) for p in (1, 2, 3, 8)] == [22, 22, 33, 75]
    assert leveling.level_for_passives(12) == 11 and leveling.level_for_passives(14) == 12
    assert leveling.level_for_spirit(30) == (11, False) and leveling.level_for_spirit(60) == (33, False)
    assert leveling.level_for_spirit(130) == (55, True)


def test_leveling_config_overrides(cat):
    from exilegen.builder import leveling
    if not leveling.load():
        pytest.skip("leveling data missing")
    cfg, errors = leveling.config({"trial_levels": [28, 40, 65, 85], "boss_level": 80})
    assert not errors and leveling.level_for_ascendancy(2, cfg) == 28 and leveling.level_for_ascendancy(8, cfg) == 85
    assert leveling.config({"trial_levels": [30, 20, 60, 75]})[1] and leveling.config({"nope": 1})[1]
    bad = graphx.run({"leveling_config": {"late_factor": 2}}, cat=cat)
    assert not bad["passed"] and "req.leveling_config" in {c["id"] for c in bad["preflight"] if c["status"] == "fail"}
    base = {c["id"]: c for c in graphx.run({"top": 400}, cat=cat)["mechanics"]["chains"]}
    late = graphx.run({"top": 400, "leveling_config": {"trial_levels": [28, 40, 65, 85], "boss_level": 80}}, cat=cat)["mechanics"]["chains"]
    moved = 0
    for c in late:
        for s in c["leveling"]["steps"]:
            if s["gate"] == "ascendancy" and s["points"] is not None:
                assert s["level"] in (28, 40, 65, 85)
                moved += 1
            if s["gate"] == "boss":
                assert s["level"] >= 80
        assert c["leveling"]["online_level"] >= base[c["id"]]["leveling"]["online_level"]
    assert moved


def test_measured_results_rank_and_attach(cat, tmp_path):
    import json
    ids = [c["id"] for c in graphx.run({"top": 30}, cat=cat)["mechanics"]["chains"]]
    rows = [{"id": ids[20], "ok": True, "feasible": True, "dps_honest": 3000.0, "ehp": 20000, "base_key": "a"},
            {"id": ids[3], "ok": True, "feasible": True, "dps_honest": 900.0, "ehp": 20000, "base_key": "a"},
            {"id": ids[5], "ok": False, "error": "no skeleton for this chain"}]
    (tmp_path / "chain_scores.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "baselines.jsonl").write_text(json.dumps({"base_key": "a", "ok": True, "dps_honest": 1000.0}) + "\n")
    (tmp_path / "states.jsonl").write_text(json.dumps({"id": ids[3], "vars": ["usePowerCharges"], "chain_off": 900.0,
                                                       "chain_on": 1800.0, "base_off": 1000.0, "base_on": 1000.0}) + "\n")
    doc = graphx.run({"top": 30}, cat=cat, scores_dir=str(tmp_path))
    by = {c["id"]: c for c in doc["mechanics"]["chains"]}
    assert by[ids[20]]["measured"]["gain"] == 3.0 and by[ids[3]]["measured"]["gain_credited"] == 1.8
    assert by[ids[3]]["measured"]["payoff"] == 2.0 and not by[ids[5]]["measured"]["built"]
    assert doc["mechanics"]["info"]["measured"]["scored"] == 3 and [c["id"] for c in doc["mechanics"]["chains"]] == ids
    ranked = graphx.run({"top": 30, "rank_by": "measured"}, cat=cat, scores_dir=str(tmp_path))["mechanics"]["chains"]
    assert [c["id"] for c in ranked[:2]] == [ids[20], ids[3]]
    assert not graphx.run({"rank_by": "measured"}, cat=cat)["passed"]


def test_chainfit_bias_is_not_shrunk_and_unseen_columns_do_not_break_the_fit():
    from exilegen.builder import chainfit
    chains = [{"id": f"c{i}", "kind": "enabler" if i % 2 else "graft", "main_skill": f"s{i % 7}", "length": 2,
               "asc_nodes": [["A", "x"]] if i < 5 else [], "fit": 0.5, "novelty": 0.5, "score": 0.3} for i in range(40)]
    # the first vocabulary entry ("asc:A:x" sorts before "bias") only occurs on chains with no gain
    measured = {c["id"]: ({"built": False} if i < 5 else {"built": True, "gain_credited": 2.0 if i % 2 else 0.5})
                for i, c in enumerate(chains)}
    model = chainfit.fit(chains, measured, lam=1.0)
    assert model["vocab"][0] != "bias"
    hi = chainfit.predict(model, chains[7])["gain"]
    lo = chainfit.predict(model, chains[8])["gain"]
    assert hi > 1.5 and lo < 0.7


def test_exclude_supports_is_checked_and_changes_no_ranking(cat):
    from exilegen.builder import supportgems
    base = graphx.run({"top": 30}, cat=cat)
    assert base["requirements"]["exclude_supports"] == []
    doc = graphx.run({"top": 30, "exclude_supports": ["Rakiata's Flow", "No Such Gem"]}, cat=cat)
    chk = {c["id"]: c for c in doc["preflight"]}["req.exclude_supports"]
    assert doc["passed"] and chk["status"] == "warn" and "No Such Gem" in chk["detail"] and "Rakiata" not in chk["detail"]
    assert [c["id"] for c in doc["mechanics"]["chains"]] == [c["id"] for c in base["mechanics"]["chains"]]
    assert {c["id"]: c for c in base["preflight"]}["req.exclude_supports"]["status"] == "ok"
    for bad in ("Fork", [1, "Fork"], {"Fork": True}):
        d = graphx.run({"exclude_supports": bad}, cat=cat)
        assert not d["passed"] and "mechanics" not in d
        assert {c["id"]: c for c in d["preflight"]}["req.exclude_supports"]["status"] == "fail"
    info = doc["mechanics"]["info"]
    assert info["support_names"] == sorted(info["support_names"]) == supportgems.names()
    assert "Rakiata's Flow" in info["support_names"] and "Fork" in info["support_names"]
    assert "Rakiata's Flow" in info["lineage_supports"] and "Fork" not in info["lineage_supports"]
    assert set(info["lineage_supports"]) <= set(info["support_names"])


def test_measured_code_only_with_codes(cat, tmp_path):
    import json
    cid = graphx.run({"top": 5}, cat=cat)["mechanics"]["chains"][0]["id"]
    (tmp_path / "chain_scores.jsonl").write_text(json.dumps(
        {"id": cid, "ok": True, "feasible": True, "dps_honest": 3000.0, "ehp": 20000, "code": "eNabc"}) + "\n")
    plain = graphx.run({"top": 5}, cat=cat, scores_dir=str(tmp_path))["mechanics"]["chains"][0]
    coded = graphx.run({"top": 5}, cat=cat, scores_dir=str(tmp_path), codes=True)["mechanics"]["chains"][0]
    assert plain["measured"]["built"] and "code" not in plain["measured"]
    assert coded["measured"]["code"] == "eNabc"
