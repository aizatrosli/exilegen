"""Typed edges: mechanics-graph links read again against stat ids and PoB tags."""
import pytest

from exilegen.builder import catalog, edges, mechgraph, statlinks


@pytest.fixture(scope="module")
def typed():
    g = mechgraph.MechGraph(catalog.load())
    return g, edges.typed(g)


def roles(stat_id):
    return {(a["state"], a["role"], a["target"]) for a in statlinks.analyse(stat_id)}


def test_stat_ids_say_who_and_what():
    assert ("enemy:Shocked", "scales", "self") in roles("base_self_shock_duration_-%")
    assert ("charge:Power", "cap", "self") in roles("max_power_charges")
    assert ("enemy:Shocked", "payoff_self", "self") in roles("critical_bonus_+%_final_while_shocked")
    assert ("enemy:Poisoned", "produces", "enemy") in roles("base_chance_to_poison_on_hit_%")
    assert ("rage", "produces", "self") in roles("base_rage_regeneration_per_minute")
    assert roles("shockwave_damage_+%") == set()                       # a shockwave is not Shock
    assert statlinks.conversions("physical_hit_and_dot_damage_%_taken_as_fire") == []   # defensive, not a damage bridge


def test_caps_and_on_you_states_are_not_sources(typed):
    g, ed = typed
    cap = ed[("unique:Darkray Vectors", "p", "charge:Frenzy")]
    assert cap.verdict == "contradicted" and cap.relation == "raises_cap" and "max_frenzy_charges" in cap.stat_ids
    you = ed[("unique:Bronzebeard", "p", "enemy:Shocked")]
    assert you.verdict == "contradicted" and you.owner == "self"
    berek = ed[("unique:Berek's Respite", "c", "enemy:Shocked")]         # "while Shocked": you are shocked
    assert berek.verdict == "contradicted" and berek.owner == "self"


def test_real_links_are_confirmed_and_amounts_left_alone(typed):
    g, ed = typed
    assert ed[("unique:Indigon", "c", "mana_spent")].verdict == "confirmed"
    assert sum(1 for x in ed.values() if x.verdict == "confirmed") > 400
    # an armour or energy shield amount is what the token means: never a contradiction
    assert not [x for x in ed.values() if x.verdict == "contradicted" and x.token in ("armour", "evasion", "es")
                and x.relation in ("raises_cap", "amplifies")]
    # keystones carry opaque ids: the text reading stands
    assert {x.verdict for x in ed.values() if g.entities[x.entity].kind == "keystone"} <= {"text", "confirmed", "contradicted"}


def test_a_skill_that_can_never_inflict_the_state(typed):
    g, ed = typed
    e = ed[("skill:SunderPlayer", "p", "enemy:ArmourBroken")]             # Sunder uses broken armour, it cannot break it
    assert e.verdict == "contradicted" and "never" in e.why


def test_chain_doubt_names_the_part_and_the_stat(typed):
    g, ed = typed
    chain = {"path": ["unique:Darkray Vectors", "skill:SpiralVolleyPlayer"], "tokens": ["charge:Frenzy"]}
    d = edges.chain_doubts(g, ed, chain)
    assert d and "Darkray Vectors" in d[0] and "max_frenzy_charges" in d[0]
    ok = {"path": ["unique:Indigon", "skill:EmberFusilladePlayer"], "tokens": ["mana_spent"]}
    assert edges.chain_doubts(g, ed, ok) == []
