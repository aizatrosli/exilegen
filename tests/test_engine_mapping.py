"""exg op -> pobctl serve request mapping (no engine needed)."""
from exilegen.engine import _pobctl_request


def test_pobctl_request_mapping():
    req = {"op": "whatif", "gem_changes": [{"group": 2, "slot": 1, "gem": None}], "timeout": 5}
    out = _pobctl_request(req)
    assert out == {"timeout": 5, "m": "search_whatif", "p": {"gem_changes": [{"group": 2, "slot": 1, "gem": False}]}}
    assert req["gem_changes"][0]["gem"] is None  # caller's request untouched
    many = _pobctl_request({"op": "whatif_many", "variants": [{"gem_changes": [{"group": 1, "slot": 1, "gem": None}]}]})
    assert many["p"]["variants"][0]["gem_changes"][0]["gem"] is False
    assert _pobctl_request({"op": "lua", "chunk": "return 1"}) == {"lua": "return 1"}
    assert _pobctl_request({"op": "quit"}) == {"ctl": "shutdown"}
    assert _pobctl_request({"m": "get_stats", "p": {}}) == {"m": "get_stats", "p": {}}
