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
