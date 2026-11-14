"""Skeleton generator tests (catalogue cache only: no PoB data, engine or corpus mount needed)."""
from __future__ import annotations

import os

import pytest

from exilegen.builder import catalog as catmod
from exilegen.builder.contracts import Skeleton
from exilegen.builder.skeletons import SOURCES, Generator, skeleton_id

pytestmark = pytest.mark.skipif(not os.path.exists(os.path.join(catmod.DATA_DIR, "skills.json")),
                                reason="builder catalogue cache missing (python -m exilegen.builder.catalog)")


@pytest.fixture(scope="module")
def gen():
    return Generator()


def _uniques_ok(gen, sk):
    facts = [gen._ufacts(u["name"]) for u in sk.uniques]
    return not catmod.slot_conflicts(facts)


def test_catalog_sizes(gen):
    c = gen.cat
    assert len(c.main_skill_candidates()) > 150
    assert len(c.uniques) > 300
    assert len(c.ascendancies) >= 20 and len(c.keystones) >= 25
    choir = c.uniques["Choir of the Storm"]
    assert choir["slot"] == "Amulet" and "UniqueBreachLightningBoltPlayer" in choir["granted_skill_ids"]
    assert "TheStarsAnswerPlayer" in c.uniques["Runeseeker's Call"]["granted_skill_ids"]


def test_slot_conflicts():
    bow = {"name": "B", "slot": "Weapon 1", "two_handed": True, "weapon_class": "Bow"}
    quiver = {"name": "Q", "slot": "Weapon 2", "base_type": "Quiver"}
    shield = {"name": "S", "slot": "Weapon 2", "base_type": "Shield"}
    staff = {"name": "T", "slot": "Weapon 1", "two_handed": True, "weapon_class": "Staff"}
    ring = lambda n: {"name": n, "slot": "Ring"}
    assert not catmod.slot_conflicts([bow, quiver])
    assert catmod.slot_conflicts([staff, shield])
    assert catmod.slot_conflicts([staff, quiver])
    assert not catmod.slot_conflicts([ring("a"), ring("b")])
    assert catmod.slot_conflicts([ring("a"), ring("b"), ring("c")])


def test_deterministic(gen):
    a = [s.id for s in gen.generate(25, seed=11)]
    b = [s.id for s in Generator().generate(25, seed=11)]
    assert a == b
    assert a != [s.id for s in gen.generate(25, seed=12)]


def test_ids_stable_and_unique(gen):
    batch = gen.generate(50, seed=3)
    assert len(batch) == 50
    assert len({s.id for s in batch}) == 50
    for s in batch:
        assert s.id == skeleton_id(s)
        assert s.source in SOURCES and s.thesis


def test_sources_mixed(gen):
    batch = gen.generate(50, seed=5)
    assert len({s.source for s in batch}) >= 4


@pytest.mark.parametrize("cons", [[], ["one_button"], ["no_cast"], ["no_cast:strict"], ["one_button", "no_cast"],
                                  ["no_minions", "one_button"]])
def test_constraints_and_legality(gen, cons):
    batch = gen.generate(30, cons, seed=21)
    assert len(batch) >= 25
    for s in batch:
        assert gen.violations(s, cons) == [], (s.to_dict(), cons)
        assert gen.cat.class_of(s.ascendancy) == s.cls
        assert _uniques_ok(gen, s)
        assert len(s.uniques) <= 3
        main = gen.cat.skills[s.main_skill]
        act = s.tags["activation"]
        if "one_button" in cons:
            assert act not in ("manual", "totem")
        if "no_cast" in cons and "Spell" in main["types"]:
            assert act not in ("manual", "totem")
            fd = gen.cat.skills.get((s.trigger or {}).get("feeder") or "")
            assert fd is None or "Spell" not in fd["types"]
        if "no_cast:strict" in cons:
            assert "Spell" not in main["types"]
        if "no_minions" in cons:
            assert not s.tags["minion"]
        # attack skills match the weapon (unique weapon or hint)
        w1 = next((gen.cat.uniques[u["name"]] for u in s.uniques if gen.cat.uniques[u["name"]]["slot"] == "Weapon 1"), None)
        assert catmod.weapon_ok(main, w1["weapon_class"] if w1 else s.weapon)


def test_unique_granted_main_carries_unique(gen):
    sk = gen.assemble(__import__("random").Random(1), "UniqueBreachLightningBoltPlayer", source="build_around",
                      thesis="t", constraints=["one_button"])
    assert sk is not None
    assert {"Choir of the Storm", "Earthbound"} & {u["name"] for u in sk.uniques}
    assert sk.tags["activation"] == "inbuilt_trigger"


def test_mutate(gen):
    parents = gen.generate(12, seed=8)
    total = 0
    for p in parents:
        kids = gen.mutate(p, n=3, seed=4)
        for k in kids:
            total += 1
            assert k.parent == p.id and k.id != p.id and k.source == "mutation"
            assert gen.violations(k) == []
            assert gen.cat.class_of(k.ascendancy) == k.cls
            assert _uniques_ok(gen, k)
        assert [k.id for k in kids] == [k.id for k in gen.mutate(p, n=3, seed=4)]
    assert total >= 20


def test_mutate_respects_constraints(gen):
    for p in gen.generate(8, ["one_button"], seed=2):
        for k in gen.mutate(p, n=2, seed=1, constraints=["one_button"]):
            assert gen.violations(k, ["one_button"]) == []


def test_feedback_shifts_sources_and_cells(gen):
    fb = {"source_weights": {"corpus_prior": 0, "anti_meta": 0, "mutation": 0, "build_around": 1, "mechanics_graph": 0}}
    batch = gen.generate(20, feedback=fb, seed=9)
    assert {s.source for s in batch} == {"build_around"}
    # heavily explored cells are sampled less
    base = gen.generate(40, seed=13)
    from collections import Counter
    cells = Counter(s.tags["cell"] for s in base)
    top = cells.most_common(1)[0][0]
    again = gen.generate(40, feedback={"cell_counts": {top: 1000}}, seed=13)
    assert sum(s.tags["cell"] == top for s in again) < cells[top]
    # elites are mutated, seen ids skipped
    elites = [base[0].to_dict()]
    out = gen.generate(10, feedback={"elites": elites, "seen_ids": [s.id for s in base]}, seed=1)
    assert not {s.id for s in out} & {s.id for s in base}
    assert any(s.parent == base[0].id for s in out)


def test_skeleton_roundtrip(gen):
    s = gen.generate(1, seed=0)[0]
    s2 = Skeleton(**s.to_dict())
    assert skeleton_id(s2) == s.id
