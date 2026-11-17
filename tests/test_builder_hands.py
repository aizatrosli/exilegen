"""Chains whose parts need weapons that one weapon set cannot hold (builder/hands.py)."""
import pytest

from exilegen.builder import catalog as catmod
from exilegen.builder import chainbuild, graphx, hands


@pytest.fixture(scope="module")
def cat():
    return catmod.load()


@pytest.fixture(scope="module")
def pool(cat):
    return graphx.run({"top": 2000}, cat=cat)["mechanics"]["chains"]


def test_what_one_weapon_set_holds():
    wand, sceptre, mace, axe, staff, spear = ({"item": n, "class": c} for n, c in (
        ("w", "Wand"), ("s", "Sceptre"), ("m", "One Hand Mace"), ("a", "One Hand Axe"), ("st", "Staff"), ("sp", "Spear")))
    assert not hands._together(wand, dict(wand, item="w2"))          # wands are never dual wielded
    assert hands._together(wand, sceptre) and hands._together(mace, sceptre)
    assert hands._together(mace, axe) and not hands._together(mace, spear)
    assert not hands._together(staff, sceptre) and not hands._together(wand, mace)


def _info(cat):
    return graphx.run({"top": 2000}, cat=cat)["mechanics"]["info"]


def test_two_main_hand_weapons_are_not_a_chain(cat, pool):
    # weapon swap exists, but the swapped-out weapon's stats and buffs are gone: such a chain is dropped
    rx = lambda c: c.get("reaction") or {}
    assert not any(rx(c).get("meta_name") == "Spellslinger" and "Liminal Coil" in c["uniques"] for c in pool)
    assert not any(c.get("weapon_swap") and c["weapon_swap"]["lost"] for c in pool)
    one = next(c for c in pool if rx(c).get("meta_name") == "Spellslinger" and not c["uniques"]
               and cat.skills[c["main_skill"]].get("tier") != 0)
    two = dict(one, uniques=["Liminal Coil"])
    got = hands.check(two, cat)
    assert got["lost"] and "cannot be in hand together" in got["why"]
    items = [w["items"] for w in got["weapons"]]
    assert ["Liminal Coil"] in items and ["Dueling Wand"] in items
    assert hands.check(one, cat) is None                              # a gem skill on the Dueling Wand: one weapon
    with pytest.raises(chainbuild.ChainError, match="in hand together"):
        chainbuild.build(two["id"], chains=[dict(two, weapon_swap=got)])


def test_a_skill_only_one_weapon_grants_counts_as_that_weapon(cat, pool):
    assert 50 < _info(cat)["weapon_swap"]["dropped"] < 0.15 * len(pool)
    plain = next(c for c in pool if c["main_name"] == "Spark" and not c["uniques"])
    assert hands.check(plain, cat) is None and "weapon_swap" not in plain


def test_lab_score_reads_and_forgets(tmp_path):
    import json
    from exilegen import lab
    out = tmp_path / "scores"
    a = lab.parser().parse_args(["score", "--out", str(out)])
    assert lab.cmd_score(a) == {"lab": lab.LAB_VERSION, "asked": [], "scored": 0, "measured": {}, "missing": []}
    out.mkdir()
    rows = [{"id": "aaa", "ok": True, "feasible": True, "dps_honest": 100.0, "main_name": "Spark", "base_key": "k"},
            {"id": "bbb", "ok": False, "error": "no skeleton"}]
    (out / "chain_scores.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out / "baselines.jsonl").write_text(json.dumps({"base_key": "k", "main_name": "Spark", "dps_honest": 50.0, "feasible": True}) + "\n")
    got = lab.cmd_score(a)
    assert got["scored"] == 2 and got["measured"]["aaa"]["gain"] == 2.0 and not got["measured"]["bbb"]["built"]
    lab._forget(str(out), ["aaa"])
    assert set(lab.cmd_score(a)["measured"]) == {"bbb"}
