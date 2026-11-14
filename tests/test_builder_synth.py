"""Synthesizer: one skeleton from nothing with a small budget (engine tests skip without the binary),
plus engine-free checks of the gear helpers.

Env: EXG_ENGINE_BIN, EXG_POB_ROOT.
"""
import pytest

from exilegen.builder.contracts import BuildResult, Skeleton
from exilegen.builder.gear import defence_type, tier_params, weapon_spec
from exilegen.builder.synth import _objective_weights, make_scorer, support_slots
from exilegen.engine import Engine, find_binary, find_pob_root

needs_engine = pytest.mark.skipif(not find_binary() or not find_pob_root(), reason="exg-engine binary or PoB data missing")


def test_gear_helpers():
    assert weapon_spec("Bow")["type"] == "Bow" and weapon_spec("Bow")["offhand"] == "Quiver"
    assert weapon_spec("Quarterstaff")["type"] == "Staff: Warstaff"
    assert weapon_spec("One Hand Mace, Two Hand Mace")["type"] == "One Hand Mace"
    assert weapon_spec("Nonsense") is None
    assert defence_type("es", "Warrior") == "Energy Shield"
    assert defence_type(None, "Ranger") == "Evasion"
    assert tier_params("high")["itemLevel"] >= tier_params("low")["itemLevel"]
    assert [support_slots(x) for x in (10, 30, 50, 90)] == [2, 3, 4, 5]


def test_scorer_wrapping():
    sc = make_scorer()
    assert sc.profile_list == ["pinnacle_v1", "mapping_v1"] and sc.stats_filter is not None
    w = _objective_weights(sc)
    assert w["dps"] == 1.0 and 0 < w["ehp"] < 1


@pytest.fixture(scope="module")
def eng():
    e = Engine()
    yield e
    e.close()


@needs_engine
def test_synthesize_arc(eng):
    from exilegen.builder.synth import synthesize
    sk = Skeleton(id="test-arc", cls="Sorceress", ascendancy="Stormweaver", main_skill="Arc", level=70,
                  defence="es", budget_tier="low")
    r = synthesize(sk, eng, None, budget=400, gear_optimise=False, polish=())
    assert isinstance(r, BuildResult)
    assert r.ok, (r.error, r.notes)
    assert r.code and r.evals > 0 and r.seconds > 0
    st = r.stats["pinnacle_v1"]
    assert st["mainSkill"]["name"] == "Arc"                     # pinned, not "auto"
    assert r.score["dps_honest"] > 0
    main = next(g for g in r.skills["groups"] if g["index"] == st["main"]["group"])
    assert sum(1 for g in main["gems"] if g.get("support")) >= 2   # support sockets filled
    assert any("tree scratch" in n for n in r.notes)


@needs_engine
def test_synthesize_bad_skeleton(eng):
    from exilegen.builder.synth import synthesize
    sk = Skeleton(id="bad", cls="Sorceress", ascendancy="Deadeye", main_skill="Arc")
    r = synthesize(sk, eng, None, budget=50)
    assert not r.ok and "Deadeye" in r.error
