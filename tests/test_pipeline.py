"""Pipeline + CLI end-to-end on one POC 1-button build with a tiny budget. Skipped without the engine.

Env: EXG_ENGINE_BIN, EXG_POB_ROOT, EXG_CORPUS (default /mnt/project-files/corpus/poe2/).
"""
import json
import os

import pytest

from exilegen.engine import Engine, find_binary, find_pob_root

CORPUS = os.environ.get("EXG_CORPUS", "/mnt/project-files/corpus/poe2/")
pytestmark = pytest.mark.skipif(not find_binary() or not find_pob_root(), reason="exg-engine binary or PoB data missing")


def corpus_code(line: int) -> str:
    path = os.path.join(CORPUS, "builds.jsonl")
    if not os.path.isfile(path):
        pytest.skip("corpus missing")
    with open(path) as f:
        for i, row in enumerate(f, 1):
            if i == line:
                return json.loads(row)["code"]
    pytest.skip("corpus line missing")


@pytest.fixture(scope="module")
def eng():
    e = Engine()
    yield e
    e.close()


def test_plan_and_floors():
    from exilegen.optimise.pipeline import _plan, default_floors
    assert _plan("auto") is None
    assert _plan("tree,supports,triggers") == ["tree_warm", "supports", "triggers"]
    fl = {f.id: f for f in default_floors()}
    assert fl["mana_sustain"].mode == "either" and fl["ehp"].tier == "hard"


def test_optimise_end_to_end(eng, tmp_path):
    from exilegen.optimise.pipeline import format_result, optimise
    code = corpus_code(237)
    r = optimise(code, constraints=["one_button"], profiles=["pinnacle_v1"], steps="auto", budget=200,
                 engine=eng, seed=1)
    assert not r.get("error"), r.get("traceback")
    for k in ("start", "end", "steps", "history", "code", "explain", "changes", "evals"):
        assert k in r
    assert r["end"]["feasible"] and r["start"]["feasible"]
    assert r["end"]["objective"] >= r["start"]["objective"] - 1e-9
    assert r["evals"] <= 200 + 50
    assert r["steps"], "at least one step ran"
    json.dumps(r)                                  # serialisable
    text = format_result(r)
    assert "honest DPS" in text and r["code"] in text
    # the exported code loads and scores like the end state
    from exilegen.optimise.pipeline import score_code
    s = score_code(r["code"], constraints=["one_button"], profiles=["pinnacle_v1"], engine=eng)
    assert s["summary"]["feasible"]
    assert abs(s["summary"]["objective"] - r["end"]["objective"]) < 0.02


def test_cli_check_and_score(eng, tmp_path, capsys):
    from exilegen import cli
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"code": corpus_code(237)}))
    rc = cli.main(["check", "--file", str(f), "--constraint", "one_button", "--profile", "pinnacle_v1"])
    out = capsys.readouterr().out
    assert rc == 0 and "FEASIBLE" in out
    rc = cli.main(["score", "--file", str(f), "--profile", "pinnacle_v1", "--out", str(tmp_path / "s.json")])
    out = capsys.readouterr().out
    assert rc == 0 and "honest DPS" in out
    assert json.load(open(tmp_path / "s.json"))["summary"]["objective"] is not None
