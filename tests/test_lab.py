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
