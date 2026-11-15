"""ladderref: the reference file from small synthetic corpus rows (no engine)."""
import json

from exilegen.builder import ladderref as L


def rec(key, skill, asc, dps, ehp=10000, supports=(), uniques=(), keystones=(), nodes=(), minion=None):
    return {"meta": {"character_sha256": key * 16, "class": asc, "main_skill": "Herald of Ice"},
            "pinnacle_locked": {"mainSkill": {"name": skill}, "stats": {"CombinedDPS": dps, "TotalEHP": ehp},
                                "minionStats": {"CombinedDPS": minion} if minion else None},
            "tags": {"ascendancy": asc,
                     "socketGroups": [{"main": False, "supports": ["Wrong Group"]},
                                      {"main": True, "active": skill, "supports": list(supports)}],
                     "items": [{"name": u, "rarity": "UNIQUE"} for u in uniques] + [{"name": "Doom Loop", "rarity": "RARE"}],
                     "keystones": [{"id": 1, "name": k} for k in keystones],
                     "ascendancyNodes": [{"name": n, "type": "Notable"} for n in nodes] + [{"name": "Spirit", "type": "Normal"}]}}


OVERVIEW = {"totalCharacters": 1000,
            "dimensions": {"skills": {"counts": [["Spark", 250], ["Herald of Ice", 400]]}},
            "skillByAscendancy": {"Spark": {"total": 250, "byAscendancy": [["Stormweaver", 100], ["Oracle", 20]]}}}
RECS = [rec("a", "Spark", "Stormweaver", 100, 1000, ["Pierce I", "Acceleration"], ["Indigon"], ["Mind Over Matter"], ["Tempest Caller"]),
        rec("b", "Spark", "Stormweaver", 300, 3000, ["Pierce I"], ["Indigon", "Atziri's Rule"]),
        rec("c", "Spark", "Oracle", 0, 2000, ["Pierce I"]),
        rec("d", "Skeletal Brute Minion", "Lich", 5, 500, minion=900)]


def write(tmp_path, honest=None):
    (tmp_path / "overview.json").write_text(json.dumps(OVERVIEW))
    (tmp_path / "evaluated.jsonl").write_text("\n".join(json.dumps(r) for r in RECS) + "\n")
    hp = tmp_path / "honest.jsonl"
    if honest is not None:
        hp.write_text("\n".join(json.dumps(r) for r in honest) + "\n")
    return str(hp)


def test_quantile():
    assert L.quantile([], 0.5) is None
    assert L.quantile([4], 0.9) == 4
    assert L.quantile([1, 3], 0.5) == 2
    assert abs(L.quantile([0, 10, 20, 30, 40], 0.9) - 36) < 1e-9


def test_row_reads_main_group_and_tags():
    r = L.build_row(RECS[0])
    assert r["skill"] == "Spark" and r["asc"] == "Stormweaver" and r["dps"] == 100
    assert r["supports"] == ["Acceleration", "Pierce I"]          # the main group's, not another group's
    assert r["uniques"] == ["Indigon"] and r["keystones"] == ["Mind Over Matter"] and r["asc_nodes"] == ["Tempest Caller"]
    m = L.build_row(RECS[3])
    assert m["skill"] == "Skeletal Brute" and m["dps"] == 900     # minion DPS when larger, gem name
    assert L.build_row({"pinnacle_locked": {"mainSkill": None}}) is None


def test_reference_without_honest_is_pob_dps_and_not_comparable(tmp_path):
    ref = L.build(str(tmp_path), write(tmp_path))
    src = ref["source"]
    assert ref["version"] == 1 and src["builds"] == 4 and src["characters"] == 1000
    assert src["comparable_to_chainscore"] is False and "CombinedDPS" in src["dps_metric"]
    s = ref["skills"]["Spark"]
    assert s["n"] == 3 and s["share"] == 0.25
    assert s["dps"] == {"median": 200, "p90": 280, "max": 300}    # the build with no DPS is left out
    assert s["ehp"] == {"median": 2000, "p90": 2800}
    assert s["ascendancies"] == [["Stormweaver", 0.667], ["Oracle", 0.333]]
    assert s["supports"] == [["Pierce I", 1.0], ["Acceleration", 0.333]]
    assert s["uniques"][0] == ["Indigon", 0.667] and s["keystones"] == [["Mind Over Matter", 0.333]]
    assert ref["skills"]["Skeletal Brute"]["share"] is None       # poe.ninja does not list it
    assert ref["combos"]["Spark||Stormweaver"] == {"n": 2, "share": 0.1, "dps": {"median": 200, "p90": 280, "max": 300},
                                                   "ehp": {"median": 2000, "p90": 2800}}
    assert ref["combos"]["Spark||Oracle"]["dps"] is None and ref["combos"]["Spark||Oracle"]["share"] == 0.02
    assert ref["combos"]["Skeletal Brute||Lich"]["share"] is None
    assert ref["pairs"]["unique_skill"] == {"Atziri's Rule||Spark": 1, "Indigon||Spark": 2}
    assert ref["pairs"]["keystone_skill"] == {"Mind Over Matter||Spark": 1}
    assert ref["pairs"]["asc_node_skill"] == {"Stormweaver:Tempest Caller||Spark": 1}


def test_reference_with_honest_rows(tmp_path):
    honest = [{"k": "a" * 16, "main": "Spark", "h": 250, "e": 1100},
              {"k": "b" * 16, "main": "Comet", "h": 50, "e": 3000},        # the engine measured another skill
              {"k": "c" * 16, "error": "boom"}]                            # failed: in n, not in DPS; d is missing
    hp = write(tmp_path, honest)
    ref = L.build(str(tmp_path), hp)
    assert ref["source"]["comparable_to_chainscore"] is True and "honest" in ref["source"]["dps_metric"]
    assert ref["skills"]["Spark"]["n"] == 2 and ref["skills"]["Spark"]["dps"] == {"median": 250, "p90": 250, "max": 250}
    assert ref["skills"]["Spark"]["ehp"]["median"] == 1550
    assert ref["skills"]["Comet"]["dps"]["max"] == 50
    assert ref["skills"]["Skeletal Brute"]["dps"] is None


def test_cli_writes_one_line_and_load_reads_it(tmp_path):
    hp = write(tmp_path)
    out = tmp_path / "ref.json"
    assert L.main(["--corpus-dir", str(tmp_path), "--out", str(out), "--honest", hp]) == 0
    text = out.read_text()
    assert text.count("\n") == 1 and '": ' not in text          # one compact line
    assert L.load(str(out))["skills"]["Spark"]["n"] == 3
