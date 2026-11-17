"""Ladder support combinations against the builder's (builder/supportgaps.py)."""
from exilegen.builder import supportgaps as sg


def test_packaged_ladder_file_reads_back():
    lad = sg.load_ladder()
    assert sum(v["n"] for v in lad.values()) > 1000 and lad["Ice Shot"]["freq"]["elemental armament"] > 0.5
    assert all(k == k.lower() for k in lad["Ice Shot"]["freq"])          # families: lower case, no tier


def test_gaps_name_missed_staples_odd_picks_and_pairs():
    lad = {"Spark": sg._stats([["a", "b", "c"]] * 8 + [["a", "d"]] * 2)}
    our = {"Spark": sg._stats([["c", "x"]] * 4)}
    g = sg.gaps(lad, our)
    assert {r["support"] for r in g["missed_staples"]} == {"a", "b"}
    assert [r["support"] for r in g["odd_picks"]] == ["x"]
    assert any(r["pair"] == "a + b" for r in g["pairs_missed"])
    assert g["skills"]["Spark"]["overlap"] == 0.25 and g["never_socketed"][0]["support"] == "a"
    assert "Staples the builder misses" in sg.markdown(g)
