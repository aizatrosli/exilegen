"""exilegen.lab: the JSON sidecar commands (no engine needed)."""
import json
import math

import pytest

from exilegen import lab


def test_skeleton_from_user_json():
    sk = lab._skeleton({"cls": "Witch", "ascendancy": "Lich", "main_skill": "SparkPlayer", "uniques": ["Liminal Coil"]})
    assert sk.uniques == [{"name": "Liminal Coil"}]
    assert sk.id and sk.source == "user"
    with pytest.raises(ValueError, match="unknown skeleton fields"):
        lab._skeleton({"cls": "Witch", "ascendancy": "Lich", "main_skill": "SparkPlayer", "bogus": 1})
    with pytest.raises(ValueError, match="needs main_skill"):
        lab._skeleton({"cls": "Witch", "ascendancy": "Lich"})


def test_clean_is_json_safe():
    out = lab._clean({"a": math.nan, "b": [math.inf, 1.5], 3: {"c": object()}})
    assert out["a"] is None and out["b"] == [None, 1.5] and isinstance(out["3"]["c"], str)
    json.dumps(out, allow_nan=False)


def test_status_and_errors(tmp_path, capsys):
    assert lab.main(["status", "--run-dir", str(tmp_path)]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["exists"] and st["progress"] is None and not st["stopping"]
    assert lab.main(["synth", "--skeleton", '{"cls": "Witch"}']) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_graph_lists_chains_in_short_form(capsys):
    assert lab.main(["graph", "--top", "3", "--requirements", '{"ascendancy": "Lich"}']) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["chains"]) == 3 and out["total"] >= 3
    c = out["chains"][0]
    assert {"id", "kind", "main", "parts", "links", "thesis", "score", "spirit_gems", "online_level"} <= set(c)
    assert all(isinstance(p, str) and ":" not in p for p in c["parts"])       # names, not graph keys
    assert "evidence" not in c and "leveling" not in c

    # one chain in full, by the start of its id
    assert lab.main(["graph", "--chain", c["id"][:6]]) == 0
    one = json.loads(capsys.readouterr().out)["chain"]
    assert one["id"] == c["id"] and one["evidence"] and one["leveling"]["steps"] and "more" in one["spirit"]

    # one main skill's chains
    assert lab.main(["graph", "--main", c["main"].lower()[:6], "--top", "50"]) == 0
    mine = json.loads(capsys.readouterr().out)
    assert mine["chains"] and all(c["main"][:6].lower() in x["main"].lower() for x in mine["chains"])
    assert mine["total"] >= len(mine["chains"])


def test_graph_errors_are_objects(capsys):
    assert lab.main(["graph", "--requirements", '{"cls": "Nope"}']) == 2
    assert "unknown class" in json.loads(capsys.readouterr().out)["error"]
    assert lab.main(["graph", "--chain", "zzzz"]) == 2
    assert "chain not found" in json.loads(capsys.readouterr().out)["error"]
