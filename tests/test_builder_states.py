"""State registry: the graph's tokens tied to PoB's states and config switches."""
import os

import pytest

from exilegen.builder import chainscore, mechgraph, states


def test_every_graph_token_is_in_the_hand_layer():
    known = set(states.TOKENS)
    for tok in mechgraph.STATE_COMPILED:
        assert tok in known or tok.startswith(("dmg:", "skill:")), tok


def test_hand_layer_points_at_registry_rows():
    assert states.check() == []
    reg = states.load()
    assert reg["enemy|condition|Shocked"]["family"] == "ailment"
    # the same word on you is another state
    assert "self|condition|Shocked" in reg and reg["self|condition|Shocked"]["lines"] < reg["enemy|condition|Shocked"]["lines"]


def test_switches():
    sw = states.switch_for(["enemy:Stunned", "charge:Power", "dmg:Fire", "glory"])
    assert sw == {"conditionEnemyHeavyStunned": True, "conditionEnemyStunned": True, "usePowerCharges": True}
    # kill-gated states are not credited in a boss fight
    assert states.switch_for(["kill"]) == {} and states.switch_for(["kill"], clearing=True) == {"conditionKilledRecently": True}
    # mana spent Recently is the chain's own cost over the 4 second window
    assert states.switch_for(["mana_spent"], spent_per_second=120.0) == {"multiplierManaSpentRecently": 480}
    assert states.switch_for(["mana_spent"]) == {"multiplierManaSpentRecently": states.DEFAULT_MANA_RECENT}
    assert chainscore.state_vars(["mana_spent"], {"tempo": {"cost_per_second": 50}}) == {"multiplierManaSpentRecently": 200}


def test_token_view():
    v = states.for_token("charge:Power")
    assert v["cap"]["default"] == 3 and v["states"][0]["key"] == "self|count|PowerCharge" and v["verified"]
    assert states.for_token("glory")["verified"] is False and not states.for_token("glory").get("switch")
    assert states.for_token("nonsense")["known"] is False


def test_config_names_exist_in_pob():
    root = os.environ.get("EXG_POB_ROOT")
    if not root or not os.path.exists(os.path.join(root, "Modules", "ConfigOptions.lua")):
        pytest.skip("no PoB source")
    assert states.check(root) == []
