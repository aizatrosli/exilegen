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


def test_spellslinger_beside_another_wand_needs_weapon_swap(cat, pool):
    c = next(c for c in pool if (c.get("reaction") or {}).get("meta_name") == "Spellslinger" and "Liminal Coil" in c["uniques"])
    assert c["weapon_swap"] and any("weapon swap" in d for d in c["doubts"])
    items = [w["items"] for w in c["weapon_swap"]["weapons"]]
    assert ["Liminal Coil"] in items and ["Dueling Wand"] in items
    with pytest.raises(chainbuild.ChainError, match="weapon swap"):
        chainbuild.build(c["id"], chains=pool)


def test_a_skill_only_one_weapon_grants_counts_as_that_weapon(cat, pool):
    marked = [c for c in pool if c.get("weapon_swap")]
    assert 50 < len(marked) < 0.15 * len(pool)
    assert any(c["main_name"] == "Chaotic Surge" for c in marked)    # Spire of Ire's skill beside another weapon unique
    plain = next(c for c in pool if c["main_name"] == "Spark" and not c["uniques"])
    assert hands.check(plain, cat) is None and "weapon_swap" not in plain
    one = next(c for c in pool if (c.get("reaction") or {}).get("meta_name") == "Spellslinger" and not c["uniques"]
               and (cat.skills[c["main_skill"]].get("tier") != 0))
    assert "weapon_swap" not in one                                   # a gem skill on the Dueling Wand: one weapon
