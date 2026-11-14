"""TriggerStep / ConvertStep tests. Engine tests are skipped without the exg-engine binary, PoB data or
the corpus (POC 1-button build: corpus line 237, Comet on Cast on Critical fed by Spark).

Env: EXG_ENGINE_BIN, EXG_POB_ROOT, EXG_CORPUS (default /mnt/project-files/corpus/poe2/).
"""
import json
import os

import pytest

from exilegen.engine import Engine, find_binary, find_pob_root
from exilegen.optimise.base import Context, fields_for, has_meta_groups, patch_skills

CORPUS = os.environ.get("EXG_CORPUS", "/mnt/project-files/corpus/poe2/")
POC_LINE = 237

need_pob = pytest.mark.skipif(not find_pob_root(), reason="PoB data missing")
need_engine = pytest.mark.skipif(not find_binary() or not find_pob_root(), reason="exg-engine binary or PoB data missing")


def corpus_code(line: int) -> str:
    path = os.path.join(CORPUS, "builds.jsonl")
    if not os.path.isfile(path):
        pytest.skip("corpus missing")
    with open(path) as f:
        for i, row in enumerate(f, 1):
            if i == line:
                return json.loads(row)["code"]
    pytest.skip("corpus line missing")


VIEW = {"groups": [
    {"index": 1, "enabled": True, "metaGroup": True, "grantedBy": None,
     "gems": [{"slot": 1, "name": "Cast on Critical", "skillId": "MetaCastOnCritPlayer", "support": False, "meta": True},
              {"slot": 2, "name": "Comet", "skillId": "CometPlayer", "support": False, "meta": False, "level": 20},
              {"slot": 3, "name": "Zenith II", "skillId": "SupportZenithPlayerTwo", "support": True}],
     "skills": [{"name": "Cast on Critical", "id": "MetaCastOnCritPlayer", "gemSlot": 1, "activation": "persistent"},
                {"name": "Comet", "id": "CometPlayer", "gemSlot": 2, "activation": "triggered", "damaging": True,
                 "spell": True}]},
    {"index": 2, "enabled": True, "metaGroup": False, "grantedBy": None,
     "gems": [{"slot": 1, "name": "Spark", "skillId": "SparkPlayer", "support": False}],
     "skills": [{"name": "Spark", "id": "SparkPlayer", "gemSlot": 1, "activation": "manual", "damaging": True}]},
    {"index": 3, "enabled": True, "metaGroup": False, "grantedBy": None,
     "gems": [{"slot": 1, "name": "Archmage", "skillId": "ArchmagePlayer", "support": False}],
     "skills": [{"name": "Archmage", "id": "ArchmagePlayer", "gemSlot": 1, "activation": "persistent"}]},
]}


def test_has_meta_groups_and_fields():
    assert has_meta_groups(VIEW)
    assert "energy" in fields_for(VIEW)
    flat = {"groups": [VIEW["groups"][1]]}
    assert not has_meta_groups(flat) and fields_for(flat) == "core"


@need_pob
def test_patch_skills_spell_swap_and_disable():
    from exilegen.optimise.gems import catalog
    cat = catalog()
    v = patch_skills(VIEW, {"gem_changes": [{"group": 1, "slot": 2, "gem": "HexblastPlayer"}],
                            "group_enabled": {3: False}}, cat)
    g1 = v["groups"][0]
    gem = next(x for x in g1["gems"] if x["slot"] == 2)
    assert gem["name"] == "Hexblast" and gem["skillId"] == "HexblastPlayer"
    sk = next(s for s in g1["skills"] if s["gemSlot"] == 2)
    assert sk["activation"] == "triggered" and sk["damaging"] and sk["spell"]
    assert v["groups"][2]["enabled"] is False
    assert VIEW["groups"][0]["gems"][1]["name"] == "Comet"          # input untouched
    # a new group with a feeder is manual
    v2 = patch_skills(VIEW, {"gem_changes": [{"group": 4, "slot": 1, "gem": "SparkPlayer"}]}, cat)
    g4 = v2["groups"][-1]
    assert g4["index"] == 4 and g4["skills"][0]["activation"] == "manual"


@need_pob
def test_catalog_lists():
    from exilegen.optimise.gems import catalog
    cat = catalog()
    spells = {g.name for g in cat.trigger_spells()}
    assert {"Comet", "Hexblast", "Snap", "Arc"} <= spells
    assert "Cast on Critical" in {g.name for g in cat.metas()}
    assert "Boundless Energy II" in {g.name for g in cat.energy_supports()}
    assert cat.get("Metadata/Items/Gems/SkillGemSpark").skill_id == "SparkPlayer"


@pytest.fixture(scope="module")
def eng():
    if not find_binary() or not find_pob_root():
        pytest.skip("exg-engine binary or PoB data missing")
    e = Engine()
    yield e
    e.close()


@need_engine
def test_trigger_step_small_budget(eng):
    from exilegen.optimise.pipeline import default_floors
    from exilegen.optimise.triggers import TriggerStep
    from exilegen.scoring import Scorer
    eng.load(code=corpus_code(POC_LINE), profile="pinnacle_v1")
    ctx = Context(eng, Scorer(profile="pinnacle_v1", constraints=["one_button"], floors=default_floors()),
                  budget=260, log_fn=None)
    ctx.fields = fields_for(ctx.skills())
    assert "energy" in ctx.fields
    ctx.baseline_stats = ctx._stats_for(None)
    start = ctx.refresh()
    assert start["feasible"] and start["dps_honest"] > 0
    step = TriggerStep(meta_supports=False, feeder=False, feeder_supports=False, passes=1)
    res = step.run(ctx)
    assert res.name == "triggers"
    assert res.evals <= 260 + 10
    assert ctx.current["feasible"]
    assert ctx.current["objective"] >= start["objective"] - 1e-9
    # committed state == engine state: re-scoring from scratch gives the same objective
    obj = ctx.current["objective"]
    assert abs(ctx.refresh()["objective"] - obj) < 1e-6
    for ch in res.changes:
        assert ch.get("gem_changes")
    # Hexblast (no curse in this build) must never be picked: the registry blocks it
    names = {x.get("name") for g in ctx.skills()["groups"] for x in g.get("gems") or []}
    assert "Hexblast" not in names


@need_engine
def test_screen_matches_exact(eng):
    """A spell-swap screen (what-if + patched view) agrees with the exact apply + real skills view."""
    from exilegen.optimise.pipeline import default_floors
    from exilegen.scoring import Scorer
    eng.load(code=corpus_code(POC_LINE), profile="pinnacle_v1")
    ctx = Context(eng, Scorer(profile="pinnacle_v1", constraints=["one_button"], floors=default_floors()),
                  budget=100, log_fn=None)
    ctx.fields = fields_for(ctx.skills())
    ctx.baseline_stats = ctx._stats_for(None)
    ctx.refresh()
    e = ctx.current_stats["pinnacle_v1"]["energy"][0]
    slot = e["spells"][0]["slot"]
    ch = {"gem_changes": [{"group": e["group"], "slot": slot, "gem": "HexblastPlayer"}]}
    screened = ctx.evaluate_many([ch])[0]
    kept, now = ctx.try_commit(ch, "test", accept=lambda new, old: True)
    assert kept
    assert abs(now["dps_honest"] - screened["dps_honest"]) <= 1e-6 * max(1.0, now["dps_honest"])
    assert now["feasible"] == screened["feasible"] is False           # Hexblast without a curse is blocked
    assert any(v.get("entry") == "gem.hexblast" for v in now["violations"])
