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
