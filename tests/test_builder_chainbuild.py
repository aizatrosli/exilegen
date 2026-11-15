"""Building one chain: the parts that need no engine (level filter, exclusions, alternatives, lab arguments)."""
from __future__ import annotations

import json
import os

import pytest

from exilegen import lab
from exilegen.builder import catalog as catmod
from exilegen.builder import chainbuild as cb
from exilegen.builder import supportgems

CHAIN = {"id": "c1", "kind": "enabler", "main_skill": "SparkPlayer", "main_name": "Spark", "tokens": ["x"], "thesis": "t",
         "uniques": ["Early Ring", "Late Helm"], "keystones": ["Blood Magic"], "asc_nodes": [["Lich", "Price of Power"]],
         "util_skills": ["CurseA"],
         "leveling": {"steps": [
             {"kind": "skill", "name": "Spark", "level": 3, "gate": "gem", "main": True, "known": True},
             {"kind": "unique", "name": "Early Ring", "level": 8, "gate": "drop"},
             {"kind": "spirit", "name": "30 Spirit", "level": 11, "gate": "spirit"},
             {"kind": "skill", "name": "Curse A", "level": 22, "gate": "gem", "main": False},
             {"kind": "asc", "name": "Price of Power", "level": 22, "gate": "ascendancy"},
             {"kind": "keystone", "name": "Blood Magic", "level": 40, "gate": "passives"},
             {"kind": "unique", "name": "Late Helm", "level": 65, "gate": "drop"}]}}
NAMES = {"CurseA": "Curse A", "SparkPlayer": "Spark"}


def _restrict(level):
    parts = cb.parts_at(CHAIN, level)
    return parts, cb.restricted(CHAIN, parts, level, NAMES)


def test_parts_above_the_level_are_left_out():
    parts, ch = _restrict(21)
    assert [(p["name"], p["included"]) for p in parts] == [
        ("Spark", True), ("Early Ring", True), ("30 Spirit", True), ("Curse A", False), ("Price of Power", False),
        ("Blood Magic", False), ("Late Helm", False)]
    assert set(parts[1]) == {"kind", "name", "level", "gate", "included"}
    assert (ch["uniques"], ch["keystones"], ch["asc_nodes"], ch["util_skills"]) == (["Early Ring"], [], [], [])
    assert CHAIN["uniques"] == ["Early Ring", "Late Helm"] and CHAIN["util_skills"] == ["CurseA"]   # the original is kept
    parts, ch = _restrict(22)                      # a part at exactly the level is in
    assert (ch["asc_nodes"], ch["util_skills"], ch["keystones"]) == ([["Lich", "Price of Power"]], ["CurseA"], [])
    parts, ch = _restrict(cb.DEFAULT_LEVEL)
    assert all(p["included"] for p in parts) and ch["uniques"] == CHAIN["uniques"] and ch["keystones"] == ["Blood Magic"]


def test_level_below_the_main_skill_is_an_error():
    with pytest.raises(cb.ChainError, match="Spark is not available at level 2: its gem needs level 3"):
        _restrict(2)
    assert _restrict(3)[1]["uniques"] == []


def test_chain_without_leveling_data_keeps_every_part():
    ch = {k: v for k, v in CHAIN.items() if k != "leveling"}
    parts = cb.parts_at(ch, 5)
    assert all(p["included"] and p["level"] is None for p in parts) and len(parts) == 5
    assert cb.restricted(ch, parts, 5, NAMES)["uniques"] == CHAIN["uniques"]


def test_level_and_exclusion_arguments():
    assert cb.check_level(None) == cb.DEFAULT_LEVEL and cb.check_level(15) == 15
    for bad in (0, 101, -3, True, "15", 1.5):
        with pytest.raises(cb.ChainError, match="level must be"):
            cb.check_level(bad)
    assert cb.clean_names([" Fork ", "fork", "", "Rakiata's Flow"]) == ["Fork", "Rakiata's Flow"]
    assert cb.clean_names(None) == []
    with pytest.raises(cb.ChainError, match="unknown chain 'zz'"):
        cb.find_chain("zz", [CHAIN])
    assert cb.find_chain("c1", [CHAIN]) is CHAIN


def test_ascendancy_points_follow_the_trials():
    assert [cb.asc_points(l) for l in (1, 21, 22, 33, 59, 60, 75, 100)] == [0, 0, 2, 4, 4, 6, 8, 8]
    assert cb.asc_points(30, {"trial_levels": [28, 40]}) == 2


def test_alternatives():
    base = ["Rakiata's Flow", "Extraction", "Fork", "Deliberation"]
    got = cb.alternatives(["Rakiata's Flow", "fork", "Chain"], ["Fire Attunement", "Extraction", "Scattershot", "Deliberation"], base, 35000.0)
    assert got == [
        {"excluded": "Rakiata's Flow", "was_used": True, "replaced_by": "Fire Attunement", "dps_without_exclusion": 35000.0},
        {"excluded": "fork", "was_used": True, "replaced_by": "Scattershot", "dps_without_exclusion": 35000.0},
        {"excluded": "Chain", "was_used": False, "replaced_by": None, "dps_without_exclusion": 35000.0}]
    # the socket stayed empty, or the order changed: the support only the restricted build has
    assert cb.alternatives(["Fork"], ["Extraction", "Deliberation", "Rakiata's Flow"], base)[0]["replaced_by"] is None
    assert cb.alternatives(["Fork"], ["Scattershot", "Extraction", "Deliberation", "Rakiata's Flow"], base)[0]["replaced_by"] == "Scattershot"
    # the unrestricted build is not known
    assert cb.alternatives(["Fork"], ["Extraction"], None) == [
        {"excluded": "Fork", "was_used": None, "replaced_by": None, "dps_without_exclusion": None}]


class _Engine:
    def valid_supports(self, group, skill=None, include_used=None):
        return {"slots": [2, 3], "supports": [{"id": "a", "name": "Fork"}, {"id": "b", "name": "Rakiata's Flow"},
                                              {"id": "c", "name": "Extraction", "socketed": True}, {"id": "d", "name": "Chain"}]}


def test_banned_supports_are_not_offered():
    eng = _Engine()
    names = lambda: [s["name"] for s in eng.valid_supports(1)["supports"]]
    with supportgems.banned(eng, ["fork ", "Extraction"]) as e:
        assert e is eng and eng.valid_supports(1)["slots"] == [2, 3]
        assert names() == ["Rakiata's Flow", "Extraction", "Chain"]      # a socketed one stays visible
    assert names() == ["Fork", "Rakiata's Flow", "Extraction", "Chain"] and "valid_supports" not in vars(eng)
    with supportgems.banned(eng, []):
        assert "valid_supports" not in vars(eng) and len(names()) == 4
    with pytest.raises(RuntimeError):
        with supportgems.banned(eng, ["Fork"]):
            raise RuntimeError("x")
    assert len(names()) == 4                                              # restored after a failure too


def test_forced_supports_are_dropped_and_over_level_uniques_reported():
    from exilegen.builder.contracts import Skeleton
    sk = Skeleton(id="s", cls="Witch", ascendancy="Lich", main_skill="SparkPlayer", supports=["Fork", "Chain"],
                  trigger={"meta": "m", "supports": ["fork"], "feeder_supports": ["Extraction"]},
                  uniques=[{"name": "Late Helm", "role": "grants_main"}, {"name": "Early Ring"}])
    cb.drop_forced(sk, ["FORK"])
    assert sk.supports == ["Chain"] and sk.trigger["supports"] == [] and sk.trigger["feeder_supports"] == ["Extraction"]
    data = {"uniques": {"Late Helm": {"level": 65}, "Early Ring": {"level": 8}}}
    assert cb.over_level(sk, 15, data) == [{"kind": "unique", "name": "Late Helm", "level": 65, "role": "grants_main"}]
    assert cb.over_level(sk, 65, data) == []


@pytest.mark.skipif(not os.path.exists(supportgems.PATH), reason="support gem list missing (python -m exilegen.builder.supportgems)")
def test_support_gem_list():
    names = supportgems.names()
    assert names == sorted(names) and len(names) > 100 and "Fork" in names
    lin = supportgems.lineage_names()
    assert {"Rakiata's Flow", "Uul-Netol's Embrace", "Dialla's Desire"} <= set(lin) and "Fork" not in lin
    assert supportgems.unknown(["fork", "Nope", "Rakiata's Flow"]) == ["Nope"]


def test_support_gems_from_pob_gem_table(tmp_path):
    (tmp_path / "Data").mkdir()
    (tmp_path / "Data" / "Gems.lua").write_text(
        'return {\n\t["a"] = {\n\t\tname = "Spark",\n\t\ttags = {\n\t\t\tspell = true,\n\t\t},\n\t},'
        '\n\t["b"] = {\n\t\tname = "Fork",\n\t\ttags = {\n\t\t\tsupport = true,\n\t\t\tprojectile = true,\n\t\t},\n\t},'
        '\n\t["c"] = {\n\t\tname = "Rakiata\'s Flow",\n\t\ttags = {\n\t\t\tsupport = true,\n\t\t\tlineage = true,\n\t\t},\n\t},\n}\n')
    assert supportgems.parse(str(tmp_path)) == [{"name": "Fork", "lineage": False}, {"name": "Rakiata's Flow", "lineage": True}]


@pytest.mark.skipif(not os.path.exists(os.path.join(catmod.DATA_DIR, "skills.json")), reason="builder catalogue cache missing")
def test_lab_chain_arguments(capsys):
    a = lab.parser().parse_args(["chain", "--chain", "abc"])
    assert (a.level, a.exclude_support, a.compare, a.budget, a.seed) == (None, [], False, 6000, 7)
    a = lab.parser().parse_args(["chain", "--chain", "abc", "--level", "45", "--exclude-support", "Fork",
                                 "--exclude-support", "Rakiata's Flow", "--compare"])
    assert (a.level, a.exclude_support, a.compare) == (45, ["Fork", "Rakiata's Flow"], True)
    assert lab.main(["chain", "--chain", "no-such-chain"]) == 2            # errors before any engine starts
    assert "unknown chain" in json.loads(capsys.readouterr().out)["error"]
    assert lab.main(["chain", "--chain", "no-such-chain", "--level", "0"]) == 2
    assert "level must be" in json.loads(capsys.readouterr().out)["error"]


def test_only_support_gems_are_reported():
    gems = ["Cast on Block", "Fiery Death", "Rakiata's Flow"]
    assert cb.only_supports(gems, ["Fiery Death", "Rakiata's Flow", "Fork"]) == ["Fiery Death", "Rakiata's Flow"]
    assert cb.only_supports(gems, []) == gems
