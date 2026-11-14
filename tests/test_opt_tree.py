"""TreeStep tests. The graph tests are pure; the engine test runs one POC build in warm mode with a
small budget and is skipped without the exg-engine binary, PoB data or the build file.

Env: EXG_ENGINE_BIN, EXG_POB_ROOT, EXG_TREE_BUILDS (default /mnt/project-files/poc/tree-solver/builds.jsonl).
"""
import json
import os

import pytest

from exilegen.engine import find_binary, find_pob_root
from exilegen.optimise.tree import TreeStep, _floor_short
from exilegen.optimise.tree_graph import TreeGraph

BUILDS = os.environ.get("EXG_TREE_BUILDS", "/mnt/project-files/poc/tree-solver/builds.jsonl")
HASH = "b0d3f0cb3a6c9f088a42881acd6b97ec"     # Amazon / Ice Shot: no weapon-set or path-free nodes


def _view():
    #   start(1) - 2 - 3 - 4(notable)        5 hangs off 2; 6 is a mastery off 3 with 7 behind it
    links = {1: [2], 2: [1, 3, 5], 3: [2, 4, 6], 4: [3], 5: [2], 6: [3, 7], 7: [6], 9: [10], 10: [9]}
    kinds = {1: "start", 4: "notable", 6: "mastery", 9: "start"}
    nodes = {str(i): {"id": i, "name": f"n{i}", "kind": kinds.get(i, "small"), "sd": ["x"], "linked": l}
             for i, l in links.items()}
    return {"startNode": 1, "nodes": nodes}


def test_graph_connectivity_and_branches():
    G = TreeGraph(_view())
    assert 9 not in G.adj                       # other class start removed
    S = {2: 0, 3: 0, 4: 0, 5: 0}
    assert G.connected(S)
    assert G.unreached({3: 0, 4: 0}) == {3, 4}
    br = G.branches(S, fixed=set(), free=set())
    assert br[3] == frozenset({3, 4}) and br[5] == frozenset({5})
    assert br[2] == frozenset({2, 3, 4, 5})
    assert 3 not in G.branches(S, fixed={4}, free=set())     # a branch may not hold a fixed node
    # mastery: no path out of it
    assert G.unreached({2: 0, 3: 0, 6: 0, 7: 0}) == {7}
    # weapon-set mode: a ws-1 node reaches through mode-0 nodes, a mode-0 node not through ws-1
    assert G.connected({2: 0, 3: 1})
    assert G.unreached({2: 1, 3: 0}) == {3}
    p = G.paths({1, 2}, {2}, None, 4)
    assert p[4] == [3, 4] and p[5] == [5]


def test_floor_shortfall():
    f = {"metric": "res.fire", "value": -10.0, "threshold": 0.0, "baseline": 0.0, "mode": "relative"}
    assert _floor_short(f) == pytest.approx(1.1)
    f = {"metric": "ehp", "value": 9000.0, "threshold": 15800.0, "baseline": 10000.0, "mode": "either"}
    assert _floor_short(f) == pytest.approx(0.2)


@pytest.mark.skipif(not find_binary() or not find_pob_root(), reason="exg-engine binary or PoB data missing")
def test_tree_warm_small_budget():
    if not os.path.isfile(BUILDS):
        pytest.skip("POC build file missing")
    code = None
    with open(BUILDS) as f:
        for line in f:
            b = json.loads(line)
            if b["hash"] == HASH and b["variant"] == "player":
                code = b["code"]
                break
    if not code:
        pytest.skip("build not found")
    from exilegen.engine import Engine
    from exilegen.optimise.base import Context
    from exilegen.scoring import Scorer

    profs = ["pinnacle_v1", "mapping_v1"]
    with Engine() as eng:
        eng.load(code=code, profile=profs[0])
        base = {}
        for p in profs:
            eng.set_profile(p)
            base[p] = eng.stats(fields="core")
        eng.set_profile(profs[0])
        ctx = Context(engine=eng, scorer=Scorer(profile=profs, floor_mode="relative"), baseline_stats=base,
                      budget=100000, log_fn=None)
        r0 = ctx.refresh()
        n0 = len(eng.tree(frontier=False)["allocated"])
        step = TreeStep(mode="warm", max_evals=700, restarts=0, inner_rounds=2, max_keys=60, verbose=False)
        res = step.run(ctx)
        assert res.evals <= 900
        assert res.end_objective >= r0["objective"] - 1e-9          # never worse: fallback otherwise
        assert ctx.current["feasible"]
        tv = eng.tree(frontier=False)
        assert len(tv["allocated"]) <= n0                            # main-tree budget kept
        assert set(step.report["nodes"]) == set(tv["allocated"])
        if res.improved:
            assert res.end_objective > r0["objective"]
            assert res.changes and ctx.history
        assert eng._profile == profs[0]                             # profile restored
