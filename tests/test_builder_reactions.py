"""Chain reactions: a meta gem casting a second spell from an event of the main skill (builder/reactions.py)."""
import json
import random

import pytest

from exilegen.builder import catalog as catmod
from exilegen.builder import chainbuild, edges, graphx, mechgraph, reactions


@pytest.fixture(scope="module")
def cat():
    return catmod.load()


@pytest.fixture(scope="module")
def pool(cat):
    return graphx.run({"top": 2000}, cat=cat)["mechanics"]


def _rx(pool):
    return [c for c in pool["chains"] if c["kind"] == "reaction"]


def test_meta_gem_data_is_whole(cat):
    ms = reactions.metas()
    assert {"Cast on Critical", "Cast on Elemental Ailment", "Cast on Dodge", "Cast on Minion Death"} <= {m["name"] for m in ms}
    for m in ms:
        assert m["id"] in cat.skills and m["events"] and m["max"] in ("cast_time", 500)
        for ev in m["events"]:
            assert 0 <= ev["boss"] <= 1 and 0 <= ev["clear"] <= 1 and ev["energy"] > 0
        assert reactions.hosts(m, cat), m["name"]
    assert "cannot gain Energy" in reactions.load()["rules"]["no_relay"]


def test_only_triggerable_spells_are_hosted_and_never_by_another_meta(cat):
    coc = reactions.meta("Cast on Critical")
    hosted = reactions.hosts(coc, cat)
    assert all({"Spell", "Triggerable"} <= set(s["types"]) and "Meta" not in s["types"] for s in hosted)
    assert any(s["name"] == "Comet" for s in hosted)
    mjolner = reactions.meta("Thundergod's Wrath")
    assert all("Lightning" in s["types"] for s in reactions.hosts(mjolner, cat))


def test_energy_arithmetic_is_from_the_gem_stats(cat):
    cea = reactions.meta("Cast on Elemental Ailment")
    comet = next(s for s in cat.skills.values() if s["name"] == "Comet")
    shock = next(e for e in cea["events"] if e["token"] == "enemy:Shocked")
    freeze = next(e for e in cea["events"] if e["token"] == "enemy:Frozen")
    en = reactions.energy(cea, shock, comet)
    assert en["per_event"] == 31.4 and en["per_cast"] == 100 * comet["cast_time"] and en["estimate"]
    assert reactions.energy(cea, freeze, comet)["per_event"] == 314.0        # a Freeze is worth ten Shocks


def test_reactions_join_the_pool_with_their_own_kind(pool):
    rx = _rx(pool)
    assert len(rx) >= 100 and pool["info"]["by_kind"]["reaction"] == len(rx)
    assert len({c["id"] for c in pool["chains"]}) == len(pool["chains"])
    metas = {c["reaction"]["meta_name"] for c in rx}
    assert {"Cast on Critical", "Cast on Elemental Ailment", "Cast on Dodge", "Cast on Minion Death"} <= metas
    for c in rx:
        r = c["reaction"]
        assert c["path"][0] == "skill:" + c["main_skill"]
        assert "util:" + r["meta"] in c["path"] and "util:" + r["spells"][0] in c["path"]
        assert c["tokens"][0] == r["event"] and c["tokens"][1] == "cast:" + r["meta_name"]
        assert r["spells"][0] != c["main_skill"]                          # a socketed skill cannot feed its own gem
        assert r["payoff"] in ("state", "loop", "damage") and (r["payoff"] == "damage") == (len(c["tokens"]) == 2)
        assert c["leveling"]["online_level"] >= 22 and r["meta_name"] in c["thesis"]
        if r["source"]["kind"] == "gem" and r["spirit"]:
            assert c["tempo"]["spirit_total"] >= r["spirit"]              # the meta gem's Spirit is counted
        if r["source"]["kind"] == "unique":
            assert r["source"]["name"] in c["uniques"] and not r["buildable"]
    nodes = {n["id"]: n for n in pool["nodes"]}
    assert nodes["util:MetaCastOnCritPlayer"]["meta_gem"] and nodes["token:cast:Cast on Critical"]["group"] == "cast"


def test_ladder_staples_are_found(pool):
    rx = _rx(pool)
    assert any(c["reaction"]["payoff"] == "loop" and c["reaction"]["state"] == "infusion"
               and c["reaction"]["spell_names"][0] in ("Snap", "Living Bomb") for c in rx)
    assert any(c["reaction"]["payoff"] == "damage" and c["reaction"]["spell_names"] == ["Comet"] for c in rx)


def test_the_older_chains_keep_their_ids(cat):
    g = mechgraph.MechGraph(cat)
    before = {c["id"] for c in g.chains(top=1200, per_token=10 ** 6, per_main=24, per_entity=24)}
    doc = graphx.run({"top": 2000}, cat=cat)["mechanics"]
    gone = before - {c["id"] for c in doc["chains"]}                     # only two-weapon chains leave (hands.py)
    assert len(gone) <= doc["info"]["weapon_swap"]["dropped"] < 0.15 * len(before)
    assert reactions.extend(g, cat) is reactions.extend(g, cat)          # registering twice changes nothing


def test_a_reaction_becomes_a_meta_group_beside_the_main_skill(cat, pool):
    from exilegen.builder.skeletons import Generator
    c = next(c for c in _rx(pool) if c["reaction"]["buildable"] and not c["uniques"])
    gen = Generator(cat)
    sk = reactions.skeleton(gen, random.Random(7), c)
    assert sk is not None, gen.last_reject
    r = c["reaction"]
    assert sk.trigger["meta"] == r["meta"] and sk.trigger["spells"] == r["spells"] and sk.trigger["feeder"] == c["main_skill"]
    assert sk.main_skill == c["main_skill"] and not set(sk.extra_skills or []) & {r["meta"], *r["spells"]}
    json.dumps(sk.to_dict())
    item = next(c for c in _rx(pool) if not c["reaction"]["buildable"])
    assert reactions.skeleton(gen, random.Random(7), item) is None and "cannot socket" in gen.last_reject
    with pytest.raises(chainbuild.ChainError):
        chainbuild.build(item["id"], chains=pool["chains"])


def test_lab_shows_the_reaction(pool):
    from exilegen import lab
    c = _rx(pool)[0]
    v = lab._chain_view(c, {})
    assert v["kind"] == "reaction" and v["reaction"]["meta_gem"] == c["reaction"]["meta_name"]
    assert v["reaction"]["reliability"]["boss"] == c["reaction"]["boss"]


def test_only_skills_used_by_hand_feed_a_meta_gem(cat, pool):
    by = {s["name"]: s for s in cat.skills.values()}
    assert not reactions.feeds_by_hand(by["Fulmination"]) and not reactions.feeds_by_hand(by["Herald of Ash"])
    assert not reactions.feeds_by_hand(by["Frostflame Nova"])            # its text says it deals no damage
    assert reactions.feeds_by_hand(by["Spark"]) and reactions.feeds_by_hand(by["Ember Fusillade"])
    mains = {c["main_name"] for c in _rx(pool)}
    assert not mains & {"Fulmination", "Herald of Ash", "Frostflame Nova", "Briarpatch", "Mist Raven", "Volatile Dead"}
    assert not any("Concoction" in x for x in mains)
    assert not any("Frostflame Nova" in c["reaction"]["spell_names"] for c in _rx(pool))
    shock = next(c for c in _rx(pool) if c["reaction"]["event"] == "enemy:Shocked")
    assert any("rare against a lone boss" in d for d in shock.get("doubts") or [])


def test_supports_that_switch_the_event_off_stay_out(pool):
    crit = next(c for c in _rx(pool) if c["reaction"]["event"] == "crit")
    assert "Controlled Destruction" in reactions.event_killers(crit)
    shock = next(c for c in _rx(pool) if c["reaction"]["event"] == "enemy:Shocked")
    ks = reactions.event_killers(shock)
    assert "Elemental Focus" in ks and "Stormchain" in ks and "Controlled Destruction" not in ks
    snap = next((c for c in _rx(pool) if c["reaction"]["event"] == "crit" and c["reaction"].get("needs_state")), None)
    if snap:                                    # Snap consumes an ailment: nothing may stop the main skill's ailments
        assert {"Controlled Destruction", "Elemental Focus"} <= set(reactions.event_killers(snap))
    assert "Fiery Death" in ks and "Fiery Death" in reactions.event_killers({"reaction": {"event": "dodge"}})


def test_a_meta_gem_that_never_fires_is_flagged():
    from exilegen.scoring import energy
    from exilegen.scoring.adapter import normalise_energy_group
    g = normalise_energy_group({"group": 1, "meta": "MetaCastOnCritPlayer", "metaName": "Cast on Critical",
                                "feeder": {"hits_per_sec": 4.6, "crit": 0.0},
                                "spells": [{"name": "Snap", "dps": 1000.0, "pobRate": 0.25, "castTime": 0.5}]})
    r = energy.correct([g], 20.0)
    assert r.dps_corrected == 0 and any("energy_no_events" in f for f in r.flags)


def test_an_empty_meta_gem_is_not_flagged():
    from exilegen.scoring import energy
    from exilegen.scoring.adapter import normalise_energy_group
    g = normalise_energy_group({"group": 5, "meta": "MetaSpellslingerPlayer", "metaName": "Spellslinger", "spells": []})
    r = energy.correct([g], 20.0)
    assert r.flags == [] and r.spells == []
