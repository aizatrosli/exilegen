"""Violation sizes: floors report a normalised shortfall and Context.better ranks infeasible builds by it."""
from exilegen.optimise.base import Context, violation_size
from exilegen.scoring import floors as F


def _fl(metric, op=">=", value=0.0, mode="absolute"):
    return F.Floor("x", metric, op, value, "hard", mode)


def test_shortfall_scales():
    assert F.shortfall(_fl("res.fire"), 0.0) == 0.0
    assert abs(F.shortfall(_fl("res.fire"), -37.5) - 0.5) < 1e-9          # half a cap below 75
    assert abs(F.shortfall(_fl("ehp", value=20000.0), 15000.0) - 0.25) < 1e-9
    assert abs(F.shortfall(_fl("attr_deficit", op="<="), 50.0) - 0.5) < 1e-9
    assert F.shortfall(_fl("ehp", value=20000.0), None) == 1.0


def test_shortfall_uses_nearer_threshold_with_baseline():
    fl = _fl("ehp", value=20000.0, mode="either")
    assert abs(F.shortfall(fl, 9000.0, b=10000.0) - 0.1) < 1e-9


def _r(*viol, obj=1.0):
    return {"feasible": False, "objective": obj, "violations": list(viol)}


def test_violation_size_dedupes_profiles_and_defaults():
    r = _r({"rule": "floor", "entry": "res_fire", "shortfall": 0.2, "profile": "a"},
           {"rule": "floor", "entry": "res_fire", "shortfall": 0.4, "profile": "b"},
           {"rule": "constraint", "entry": "one_button"})
    assert abs(violation_size(r) - 1.4) < 1e-9


def test_better_ranks_infeasible_by_count_then_objective():
    one_big = _r({"rule": "floor", "entry": "ehp", "shortfall": 0.6}, obj=5.0)
    two_small = _r({"rule": "floor", "entry": "res_fire", "shortfall": 0.05},
                   {"rule": "floor", "entry": "res_cold", "shortfall": 0.05}, obj=1.0)
    assert Context.better(one_big, two_small)
    assert violation_size(two_small) < violation_size(one_big)
    feasible = {"feasible": True, "objective": -10.0, "violations": []}
    assert Context.better(feasible, two_small)


def test_new_defence_metrics():
    from exilegen.scoring import BuildView
    doc = {"stats": {"Life": 2000, "EnergyShield": 500, "StunThreshold": 800, "EvadeChance": 45,
                     "EffectiveDeflectChance": 10, "PhysicalDamageReduction": 50, "EffectiveBlockChance": 0,
                     "FireMaximumHitTaken": 12000, "ColdMaximumHitTaken": 9000, "LightningMaximumHitTaken": 15000,
                     "ChaosMaximumHitTaken": 5000, "PowerChargesMax": 3},
           "keystones": ["Mind Over Matter"]}
    bv = BuildView(doc)
    M = F.METRICS
    assert M["max_hit.ele_min"](bv) == 9000
    assert M["max_hit.chaos"](bv) == 5000
    assert M["pool"](bv) == 2500
    assert abs(M["stun_threshold_ratio"](bv) - 0.4) < 1e-9
    assert M["mitigation_layers"](bv) == 3          # evade, PDR, MoM
    assert bv.num("charges_max.power", None) == 3
    ids = {r["id"]: r for r in F.check(F.DEFAULT_FLOORS, bv)}
    assert ids["max_hit_ele_target"]["passed"] is False and ids["max_hit_ele_target"]["tier"] == "target"
