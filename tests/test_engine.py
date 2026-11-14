"""Engine service tests. Skipped when the exg-engine binary, PoB data or the corpus are missing.

Env: EXG_ENGINE_BIN (or CARGO_TARGET_DIR / engine/target), EXG_POB_ROOT, EXG_CORPUS (default
/mnt/project-files/corpus/poe2/).
"""
import json
import os

import pytest

from exilegen.engine import Engine, EngineCrash, code_hash, find_binary, find_pob_root

CORPUS = os.environ.get("EXG_CORPUS", "/mnt/project-files/corpus/poe2/")
N_BUILDS = 5

pytestmark = pytest.mark.skipif(not find_binary() or not find_pob_root(), reason="exg-engine binary or PoB data missing")


def _corpus(n):
    ev_path, b_path = os.path.join(CORPUS, "evaluated.jsonl"), os.path.join(CORPUS, "builds.jsonl")
    if not (os.path.isfile(ev_path) and os.path.isfile(b_path)):
        pytest.skip("corpus missing")
    refs = {}
    with open(ev_path) as f:
        for line in f:
            r = json.loads(line)
            refs[r["hash"]] = r
            if len(refs) >= 200:
                break
    out = []
    with open(b_path) as f:
        for line in f:
            code = json.loads(line)["code"]
            h = code_hash(code)
            if h in refs:
                out.append((code, refs[h]))
                if len(out) >= n:
                    break
    return out


@pytest.fixture(scope="module")
def eng():
    e = Engine()
    yield e
    e.close()


@pytest.fixture(scope="module")
def builds():
    return _corpus(N_BUILDS)


def close(a, b, rel=0.01):
    return abs((a or 0) - (b or 0)) <= rel * max(abs(b or 0), 1.0)


def test_stats_match_corpus_pinnacle(eng, builds):
    for code, ref in builds:
        eng.load(code=code, profile="pinnacle_v1", main="pob")
        s = eng.stats(fields=["core"])
        want = ref["pinnacle_locked"]["stats"]
        assert close(s["stats"].get("CombinedDPS"), want.get("CombinedDPS")), (ref["hash"], s["stats"].get("CombinedDPS"), want.get("CombinedDPS"))
        assert close(s["stats"].get("TotalEHP"), want.get("TotalEHP")), (ref["hash"], s["stats"].get("TotalEHP"), want.get("TotalEHP"))
        assert s["stats"].get("CharmLimit") == want.get("CharmLimit")
        # same schema as corpus-eval profile sections
        assert s["mainSkill"]["name"] == ref["pinnacle_locked"]["mainSkill"]["name"]
        for k in want:
            if k in ("FullDPS",):
                continue
            assert k in s["stats"], k


def test_profile_switch_back_to_as_exported(eng, builds):
    code, ref = builds[0]
    eng.load(code=code, profile="pinnacle_v1", main="pob")
    eng.set_profile("as_exported", main="pob")
    s = eng.stats(fields=["stats"])
    assert close(s["stats"].get("CombinedDPS"), ref["as_exported"]["stats"].get("CombinedDPS"))
    assert s["scoring"]["scenario"] == "as_exported"


def _frontier_change(eng):
    tr = eng.tree(max_dist=3)
    nodes = tr["nodes"]
    fr = [f for f in tr["frontier"] if not nodes[str(f["id"])].get("asc")]
    notable = next(f for f in fr if nodes[str(f["id"])]["kind"] == "notable")
    attrs = list(tr["attributeChoices"].items())[:2]
    return tr, dict(add_nodes=notable["path"], attr_choices={int(k): (v % 3) + 1 for k, v in attrs})


def test_whatif_equals_apply(eng, builds):
    for code, _ in builds[:3]:
        eng.load(code=code, profile="pinnacle_v1")
        tok = eng.snapshot()
        base = eng.stats(fields=["core"])
        tr, ch = _frontier_change(eng)
        w = eng.whatif(**ch)
        assert eng.stats(fields=["dps"])["dps"] == base["dps"], "whatif must not mutate"
        a = eng.apply(**ch)
        assert close(w["dps"]["best"], a["dps"]["best"], 1e-9)
        assert close(w["stats"]["TotalEHP"], a["stats"]["TotalEHP"], 1e-9)
        # node removal (PoB prunes disconnected nodes; whatif models the same pruning)
        eng.restore(tok)
        rem = dict(remove_nodes=[tr["allocated"][len(tr["allocated"]) // 2]])
        w, a = eng.whatif(**rem), eng.apply(**rem)
        assert close(w["dps"]["best"], a["dps"]["best"], 1e-9)
        assert w.get("pruned") == a.get("pruned")
        # gem change
        eng.restore(tok)
        sk = eng.skills()
        main = sk["main"]["group"]
        vs = eng.valid_supports(main)
        if vs["slots"]:
            slot = vs["slots"][0]
            cand = next((c for c in vs["supports"] if not c["socketed"] and all(x["slot"] == slot for x in c["conflicts"])), None)
            if cand:
                ch = dict(gem_changes=[{"group": main, "slot": slot, "gem": cand["id"]}])
                w = eng.whatif(**ch)
                assert eng.stats(fields=["dps"])["dps"] == base["dps"]
                a = eng.apply(**ch)
                assert close(w["dps"]["best"], a["dps"]["best"], 1e-9)
        eng.restore(tok)


def test_whatif_many_matches_single(eng, builds):
    code, _ = builds[1]
    eng.load(code=code, profile="pinnacle_v1")
    tr = eng.tree(max_dist=1)
    ids = [f["id"] for f in tr["frontier"]][:6]
    many = eng.whatif_many([{"add_nodes": [i]} for i in ids], fields=["dps"])
    for i, m in zip(ids, many):
        assert m["dps"] == eng.whatif(add_nodes=[i], fields=["dps"])["dps"]


def test_snapshot_restore_roundtrip(eng, builds):
    code, _ = builds[2]
    eng.load(code=code, profile="pinnacle_v1")
    base = eng.stats(fields=["core"])
    tree0 = eng.tree(frontier=False)
    groups0 = [[g["name"] for g in grp["gems"]] for grp in eng.skills()["groups"]]
    tok = eng.snapshot()
    tr, ch = _frontier_change(eng)
    eng.apply(**ch)
    eng.apply(remove_nodes=[tree0["allocated"][3]])
    sk = eng.skills()
    g = next(x for x in sk["groups"] if not x["grantedBy"] and x["gems"])
    eng.apply(gem_changes=[{"group": g["index"], "slot": 1, "gem": None}], group_enabled={1: False})
    assert eng.stats(fields=["dps", "stats"]) != base
    eng.restore(tok)
    after = eng.stats(fields=["core"])
    assert after["dps"] == base["dps"] and after["stats"]["TotalEHP"] == base["stats"]["TotalEHP"]
    tree1 = eng.tree(frontier=False)
    assert sorted(tree1["allocated"]) == sorted(tree0["allocated"])
    assert tree1["attributeChoices"] == tree0["attributeChoices"]
    assert tree1["weaponSetNodes"] == tree0["weaponSetNodes"]
    assert [[g["name"] for g in grp["gems"]] for grp in eng.skills()["groups"]] == groups0


def test_export_load_roundtrip(eng, builds):
    for code, _ in builds[:2]:
        eng.load(code=code, profile="pinnacle_v1")
        base = eng.stats(fields=["dps", "main"])
        tr, ch = _frontier_change(eng)
        changed = eng.apply(**ch)
        exported = eng.export()
        eng.load(code=exported, profile="pinnacle_v1", main=base["main"]["group"])
        again = eng.stats(fields=["dps"])
        assert close(again["dps"]["best"], changed["dps"]["best"], 1e-6)


def test_views_and_facts(eng, builds):
    code, _ = builds[0]
    eng.load(code=code, profile="pinnacle_v1")
    s = eng.stats(marginals=True)
    for k in ("mainSkill", "stats", "main", "dps", "offence", "sustain", "spirit", "maxHit", "requirements",
              "keystones", "uniques", "unsupported", "skills", "energy", "marginals", "scoring"):
        assert k in s, k
    assert s["scoring"]["enemy_power"] == 20
    tr = eng.tree(max_dist=2, graph=True)
    assert tr["points"]["used"] > 0 and tr["frontier"] and all("linked" in n for n in tr["nodes"].values())
    sk = eng.skills()
    assert all("activation" in x for g in sk["groups"] for x in g["skills"])


def test_timeout_restarts_and_reloads(builds):
    code, _ = builds[0]
    with Engine() as e:
        e.load(code=code, profile="pinnacle_v1")
        before = e.stats(fields=["dps"])["dps"]
        with pytest.raises(EngineCrash):
            e.request({"op": "lua", "chunk": "while true do end"}, timeout=2)
        assert e.restarts == 1
        assert e.stats(fields=["dps"])["dps"] == before
