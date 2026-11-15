"""Spirit gems for a chain: the catalogue, the budget, scoring and picking (catalogue cache only, no engine)."""
from __future__ import annotations

import os
import re

import pytest

from exilegen.builder import catalog as catmod
from exilegen.builder import chainbuild as cb
from exilegen.builder import graphx
from exilegen.builder import mechgraph
from exilegen.builder import spiritx

pytestmark = pytest.mark.skipif(not os.path.exists(os.path.join(catmod.DATA_DIR, "skills.json")),
                                reason="builder catalogue cache missing (python -m exilegen.builder.catalog)")
POB = os.environ.get("EXG_POB_ROOT") or "/home/user/pobdata"


@pytest.fixture(scope="module")
def cat():
    return catmod.load()


@pytest.fixture(scope="module")
def scorer(cat):
    return spiritx.Scorer(mechgraph.MechGraph(cat), cat)


@pytest.fixture(scope="module")
def pool(cat):
    req, _ = graphx.Requirements.from_dict({"top": 2000})
    return graphx.mechanics(cat, req)["chains"]


def _cand(name, spirit, score, role="feeds", job=None, level=10):
    return {"name": name, "skill": name + "Player", "spirit": spirit, "role": role, "why": "w", "level": level,
            "score": score, "tier": spiritx.ROLE_TIER[role], "job": job or f"{role}:{name}"}


# ------------------------------------------------------------------------------------------------ catalogue
def test_description_reader_splits_needs_from_gives():
    needs, gives = spiritx.read_description(
        "While active, grants you a Frenzy Charge when you Freeze, Electrocute or Pin an enemy. This can only occur once every few seconds.")
    assert set(needs) == {"enemy:Frozen", "enemy:Electrocuted", "enemy:Pinned"} and set(gives) == {"charge:Frenzy"}
    needs, gives = spiritx.read_description(
        "While active, Shattering an enemy with a non-Herald Attack Hit will cause an icy explosion that deals Attack damage to surrounding enemies.")
    assert set(needs) == {"enemy:Frozen"} and not gives
    needs, gives = spiritx.read_description("While active, emits an Aura that periodically Withers enemies in your Presence.")
    assert not needs and set(gives) == {"enemy:Withered"}
    needs, gives = spiritx.read_description("While active, periodically invokes divine blessings to heal you and remove Curses and Elemental Ailments from you.")
    assert not needs and not gives


def test_catalogue_facts(scorer):
    gems = {g["name"]: g for g in scorer.gems}
    assert len(gems) > 50
    ice = gems["Herald of Ice"]
    assert ice["spirit"] == 30 and ice["level"] == 10 and ice["kind"] == "herald" and "enemy:Frozen" in ice["needs"]
    assert ice["auto"] and "Shattering" in ice["needs"]["enemy:Frozen"]
    assert gems["Combat Frenzy"]["gives"]["charge:Frenzy"]["w"] == 0.5      # once every few seconds
    assert gems["Trinity"]["scales"]["tags"] == ["Elemental"] and gems["Trinity"]["spirit"] == 100
    # meta gems, item-granted skills: known, flagged, never suggested
    assert gems["Cast on Critical"]["kind"] == "meta" and not gems["Cast on Critical"]["auto"] and gems["Cast on Critical"]["flags"]
    assert gems["Leylines"]["level"] is None and not gems["Leylines"]["auto"]
    assert any("per minion" in f for f in gems["Skeletal Arsonist"]["flags"])
    assert scorer.find("herald of ice") is ice and scorer.find("HeraldOfIce") is ice and scorer.find("nope") is None


@pytest.mark.skipif(not os.path.exists(os.path.join(POB, "Data", "ModItem.lua")), reason="PoB data not here")
def test_gear_presets_match_pob_data():
    p = {x["id"]: x["spirit"] for x in spiritx.GEAR_PRESETS}
    sceptres = re.findall(r"^\tspirit = (\d+)", open(os.path.join(POB, "Data", "Bases", "sceptre.lua")).read(), re.M)
    assert sceptres and {int(x) for x in sceptres} == {p["sceptre"]}
    mods = open(os.path.join(POB, "Data", "ModItem.lua")).read()
    rolls = re.findall(r'\["IncreasedSpirit\d"\] = \{[^\n]*?"\+\((\d+)-(\d+)\) to Spirit"[^\n]*?weightKey = \{([^}]*)\}', mods)
    assert max(int(hi) for _, hi, keys in rolls if "body_armour" in keys) == p["body"]
    assert max(int(hi) for _, hi, keys in rolls if "amulet" in keys) == p["amulet"]
    assert f"+(20-{p['body_mantle']}) to Spirit" in open(os.path.join(POB, "Data", "Bases", "body.lua")).read()
    assert f"+(10-{p['solar_amulet']}) to Spirit" in open(os.path.join(POB, "Data", "Bases", "amulet.lua")).read()


# ------------------------------------------------------------------------------------------------ budget
def test_budget_fixed_and_by_level():
    R = lambda **k: graphx.Requirements.from_dict(k)[0]
    assert spiritx.budget(R()) == {"budget": 200.0, "source": "fixed"}
    assert spiritx.budget(R(spirit=120, spirit_gear=50)) == {"budget": 120.0, "source": "fixed"}
    for level, quest in ((10, 0), (11, 30), (32, 30), (33, 60), (54, 60), (55, 100), (0, 100)):
        b = spiritx.budget(R(spirit_source="level", spirit_level=level, spirit_gear=40))
        assert (b["quest"], b["budget"], b["gear"], b["source"]) == (quest, quest + 40, 40, "level")
        assert b["level"] == (level or spiritx.FINAL_LEVEL)
    cfg = {"quest_spirit": {"5": 10, "20": 50}}
    assert spiritx.budget(R(spirit_source="level", spirit_level=20), cfg)["budget"] == 50
    assert not spiritx.check(R())
    assert len(spiritx.check(R(spirit_source="gear", spirit_level=101, spirit_gear=-1, spirit_gems="yes"))) == 4


def test_preflight_validates_spirit_fields(cat):
    doc = graphx.run({"spirit_source": "sometimes", "spirit_level": -3}, check_only=True, cat=cat)
    c = next(c for c in doc["preflight"] if c["id"] == "req.spirit")
    assert c["status"] == "fail" and "spirit_source" in c["detail"] and "spirit_level" in c["detail"] and not doc["passed"]
    doc = graphx.run({"spirit_source": "level", "spirit_level": 33, "spirit_gear": 40}, check_only=True, cat=cat)
    c = next(c for c in doc["preflight"] if c["id"] == "req.spirit")
    assert c["status"] == "ok" and c["detail"].startswith("100 Spirit") and doc["passed"]


# ------------------------------------------------------------------------------------------------ picking
def test_pick_fits_the_budget_and_does_no_job_twice():
    cands = [_cand("A", 30, 0.9, job="feeds:x"), _cand("B", 30, 0.8, job="feeds:x"), _cand("C", 60, 0.7),
             _cand("D", 30, 0.65), _cand("E", 100, 0.5, role="scales"), _cand("F", 30, 0.45, role="scales"),
             _cand("G", 30, 0.33, role="utility", job="utility"), _cand("H", 30, 0.25, role="utility", job="utility")]
    r = spiritx.pick(cands, 100)
    assert [p["name"] for p in r["picks"]] == ["A", "D", "F"] and r["used"] == 90    # three small gems beat A + C
    more = {m["name"]: m for m in r["more"]}
    assert more["B"]["blocked"] == "weaker" and more["B"]["same_job_as"] == "A"
    assert more["C"]["blocked"] == "budget" and more["E"]["blocked"] == "budget" and "same_job_as" not in more["C"]
    assert all("tier" not in p and "job" not in p for p in r["picks"] + r["more"])
    r = spiritx.pick(cands, 160)
    assert [p["name"] for p in r["picks"]] == ["A", "C", "D", "F"] and r["used"] == 150
    r = spiritx.pick(cands, 290)
    assert len(r["picks"]) == spiritx.MAX_PICKS and r["used"] == 220                  # A C D E: never more than four gems
    assert {m["name"]: m["blocked"] for m in r["more"]}["H"] == "weaker"
    assert spiritx.pick(cands, 20) == {"picks": [], "more": spiritx.pick(cands, 20)["more"], "used": 0}
    assert spiritx.pick(cands, -40)["picks"] == []


def test_pick_is_one_choice_over_all_roles():
    # a strong generic gem beats a weak state link when only one fits
    cands = [_cand("Weak link", 30, 0.4), _cand("Strong generic", 30, 0.6, role="scales")]
    assert [p["name"] for p in spiritx.pick(cands, 30)["picks"]] == ["Strong generic"]
    # a gem under PICK_MIN is listed, never picked; a further gem must add more than GEM_COST
    r = spiritx.pick([_cand("True but small", 30, spiritx.PICK_MIN - 0.02)], 100)
    assert not r["picks"] and r["more"][0]["blocked"] == "weaker"
    # at most one gem that only works while clearing packs
    heralds = [dict(_cand(n, 30, 0.5), clear_only=True) for n in ("H1", "H2", "H3")]
    r = spiritx.pick(heralds, 200)
    assert len(r["picks"]) == 1 and r["picks"][0]["clear_only"] and all(m["blocked"] == "weaker" for m in r["more"])


def test_pick_respects_the_unlock_level():
    cands = [_cand("Late", 30, 0.9, level=58), _cand("Early", 30, 0.6, level=10)]
    r = spiritx.pick(cands, 60, level=33)
    assert [p["name"] for p in r["picks"]] == ["Early"] and r["more"][0]["blocked"] == "level"
    assert [p["name"] for p in spiritx.pick(cands, 60, level=None)["picks"]] == ["Late", "Early"]
    assert len(spiritx.pick([_cand(str(i), 30, 0.5 - i / 100, role="utility", job="utility") for i in range(10)], 0)["more"]) == spiritx.MORE_MAX


# ------------------------------------------------------------------------------------------------ chains
def _first(pool, pred):
    return next(c for c in pool if pred(c))


def test_a_herald_is_a_clearing_tool_for_attacks_with_a_martial_weapon(scorer, pool, cat):
    ch = _first(pool, lambda c: c["main_name"] == "Storm Lance")
    c = next(c for c in scorer.candidates(ch, level=33) if c["name"] == "Herald of Thunder")
    assert (c["role"], c["token"], c["spirit"], c["level"], c.get("clear_only")) == ("pays_off", "enemy:Shocked", 30, 10, True)
    assert "kills a Shocked enemy" in c["why"] and "Storm Lance" in c["why"] and "lone boss" in c["why"]
    assert c["score"] < 0.5                                         # an explosion of its own on a kill, not the chain's damage
    sug = scorer.suggest(ch, 60, 33)
    assert sug["budget"] == 60 and sug["free"] == 60 - sug["own"] and sug["used"] <= max(sug["free"], 0)
    # no chain is ever offered an Attack herald for a spell, a minion, a warcry or an unarmed skill
    heralds = {"Herald of Ice", "Herald of Thunder", "Herald of Ash"}
    martial = set(cat.skills["HeraldOfIcePlayer"]["weapon_types"])
    assert "Wand" not in martial and "Sceptre" not in martial and "Staff" not in martial
    seen = 0
    for ch in pool:
        main = cat.skills.get(ch["main_skill"]) or {}
        got = [c for c in scorer.candidates(ch) if c["name"].startswith("Herald of")]
        seen += bool(got)
        if "Attack" not in (main.get("types") or []) or "Herald" in (main.get("types") or []):
            assert not [c for c in got if c["name"] in heralds], (ch["id"], main.get("name"))
        held = {(cat.uniques.get(u) or {}).get("weapon_class") for u in ch.get("uniques") or []} - {None}
        if got:                                                     # every herald needs a martial weapon in hand
            assert not held - martial, (ch["id"], held)
            assert set(main.get("weapon_types") or []) & martial or held or main.get("granted_by"), ch["id"]
        assert sum(1 for p in scorer.suggest(ch, 200)["picks"] if p.get("clear_only")) <= 1
    assert seen > 20


def test_links_are_read_again_before_a_gem_hangs_on_them(scorer, pool, cat):
    rm = spiritx.really_makes
    assert not rm("enemy:Shocked", "A number of enemies hit by the wave will release a shockwave.")
    assert rm("enemy:Shocked", "Hits Shock enemies as though dealing more damage.")
    assert not rm("enemy:Shocked", "Shocks you when you reach maximum Power Charges")
    assert not rm("enemy:Poisoned", "+25% chance to be Poisoned")
    assert not rm("charge:Endurance", "The cooldown can be bypassed by expending an Endurance Charge.")
    assert not rm("charge:Power", "+1 to maximum Power Charges") and not rm("charge:Frenzy", "Gain Power Charges instead of Frenzy Charges")
    assert not rm("enemy:Bleeding", "Elemental Damage also Contributes to Bleeding Magnitude")
    assert not rm("enemy:Frozen", "Build Glory by inflicting Freeze, Shock or Ignite on enemies.")      # a condition, not a source
    assert not rm("enemy:Shocked", "The first two attacks conjure a spirit if they Hit a Shocked enemy and refresh Shock duration.")
    assert rm("rage", "Regenerate 6% of your maximum Rage per second") and rm("enemy:Cursed", "Curses you inflict have infinite Duration")
    assert rm("enemy:Chilled", "Physical damage from Hits Contributes to Chill Magnitude and Freeze Buildup")
    assert rm("enemy:Frozen", "Lightning Damage from Hits Contributes to Freeze Buildup instead of Shock Chance")
    assert not rm("enemy:Shocked", "Lightning Damage from Hits Contributes to Freeze Buildup instead of Shock Chance")
    assert rm("charge:Power", "Gain a Power Charge on Critical Hit") and rm("enemy:Shocked", "Storm Lance hits with Lightning damage")
    assert spiritx.DRAWBACK_RX.search("Fireflower: Take 100 Fire Damage when you Ignite an Enemy")
    assert spiritx.DRAWBACK_RX.search("All Damage from Hits with this Weapon Contributes to Chill Magnitude")
    assert not spiritx.DRAWBACK_RX.search("All Damage from Hits against Poisoned targets Contributes to Chill Magnitude")
    assert rm("charge:Frenzy", "+1 to Maximum Frenzy Charges", caps_ok=True) and not rm("charge:Frenzy", "+1 to Maximum Frenzy Charges")
    # Sunder releases a shockwave: it does not Shock, so Herald of Thunder has nothing to wait for
    for name in ("Sunder", "Wing Blast"):
        for ch in [c for c in pool if c["main_name"] == name and not c.get("uniques")][:3]:
            assert not [c for c in scorer.candidates(ch) if c["name"] in ("Herald of Thunder", "Mana Remnants") and c.get("token") == "enemy:Shocked"]
    # a skill that consumes Shock does not feed a herald that waits for a Shocked kill
    for ch in [c for c in pool if c["main_name"] in ("Siphoning Strike", "Primal Strikes", "Elemental Sundering")]:
        assert not [c for c in scorer.candidates(ch) if c["name"] == "Herald of Thunder"], ch["id"]
    # The Three Dragons: Lightning damage freezes instead of shocking
    ch = _first(pool, lambda c: "The Three Dragons" in (c.get("uniques") or []) and "Lightning" in (cat.skills[c["main_skill"]].get("damage_types") or []))
    side = scorer._sides(ch)
    assert "enemy:Frozen" in side["made"] and "Three Dragons" in side["made"]["enemy:Frozen"].line


def test_gems_need_what_their_text_asks_for(scorer, pool, cat):
    names = lambda ch, **kw: {c["name"] for c in scorer.candidates(ch, **kw)}
    for ch in pool:
        main = cat.skills.get(ch["main_skill"]) or {}
        types, got = set(main.get("types") or []), names(ch)
        if "is_resummoning_minion" not in (main.get("stat_ids") or []):
            assert not got & {"Grim Feast", "Sacrifice"}, ch["id"]             # "your Reviving Minions"
        if types & {"Triggered", "InbuiltTrigger"}:
            assert not got & {"Raging Spirits", "Archmage"}, ch["id"]          # "Spells you use yourself"
        if "Shapeshift" not in types:
            assert "Savage Fury" not in got
        if not {"Physical", "Chaos"} & set(main.get("damage_types") or []):
            assert not got & {"Plague Bearer", "Herald of Plague"}, ch["id"]   # Poison comes from Physical and Chaos damage
        if "Attack" not in types:
            assert not got & {"Berserk", "War Banner"}, ch["id"]
        if "Minion" in types:
            assert not got & {"Mana Remnants", "Combat Frenzy", "Siphon Elements"}, ch["id"]
    # Trinity is worth its Spirit for a skill with two elements, not for one
    one = _first(pool, lambda c: cat.skills[c["main_skill"]].get("damage_types") == ["Lightning"] and "Spell" in cat.skills[c["main_skill"]]["types"]
                 and "Damage" in cat.skills[c["main_skill"]]["types"])
    t = next(c for c in scorer.candidates(one) if c["name"] == "Trinity")
    assert t["score"] < spiritx.PICK_MIN and "only one" in t["why"]
    # Power Siphon only culls: nothing that scales its hit
    cull = _first(pool, lambda c: c["main_name"] == "Power Siphon")
    assert not [c for c in scorer.candidates(cull) if c["role"] == "scales"]
    # Siphon Elements gives the Infusion of the ailment: Ember Fusillade ignites and wants a Lightning Infusion
    ember = _first(pool, lambda c: c["main_name"] == "Ember Fusillade" and "infusion" in c["tokens"])
    assert not [c for c in scorer.candidates(ember) if c["name"] == "Siphon Elements" and c["role"] == "feeds"]
    # Archmage belongs in a Mana chain
    mana = _first(pool, lambda c: "Indigon" in (c.get("uniques") or []) and "mana_spent" in c["tokens"]
                  and not {"Triggered", "Channel"} & set(cat.skills[c["main_skill"]]["types"]) and "Spell" in cat.skills[c["main_skill"]]["types"])
    a = next(c for c in scorer.candidates(mana) if c["name"] == "Archmage")
    assert a["score"] >= 0.6 and "Indigon" in a["why"]


def test_skeleton_gems_cost_more_at_low_level(scorer):
    reaver = scorer.find("Skeletal Reaver")
    assert reaver["spirit"] == 30 and reaver["spirit_by_level"]
    cost = lambda lv: spiritx.cost_at(reaver["spirit"], reaver["spirit_by_level"], lv)
    assert cost(None) == 30 and cost(90) == 30 and cost(33) == 35 and cost(33) < cost(12)
    assert spiritx.cost_at(30, None, 15) == 30
    if os.path.isdir(POB):
        assert spiritx.parse_levels(POB) == spiritx.load_levels()


def test_a_gem_feeds_the_payoff_part(scorer, pool):
    ch = _first(pool, lambda c: "charge:Power" in c["tokens"] and "Price of Power" in [n for _, n in c["asc_nodes"]])
    c = next(c for c in scorer.candidates(ch) if c["name"] == "Lingering Illusion")
    assert c["role"] == "feeds" and c["token"] == "charge:Power" and "Price of Power" in c["why"] and "Power Charge" in c["why"]
    # Charge Regulation would spend the very charges the payoff spends: not offered as a payoff here
    assert not [c for c in scorer.candidates(ch) if c["name"] == "Charge Regulation" and c.get("token") == "charge:Power"]


def test_every_suggestion_fits_and_says_why(scorer, pool):
    n = 0
    for ch in pool[:300]:
        sug = scorer.suggest(ch, 100, 90)
        n += 1
        assert sug["used"] == sum(p["spirit"] for p in sug["picks"]) <= max(sug["free"], 0)
        assert len(sug["more"]) <= spiritx.MORE_MAX and all(m["blocked"] in ("budget", "weaker", "level") for m in sug["more"])
        assert len([p for p in sug["picks"] if p["role"] == "utility"]) <= 1 and len(sug["picks"]) <= spiritx.MAX_PICKS
        assert all(p["score"] >= spiritx.UTILITY_PICK_MIN and p["why"].endswith(".") for p in sug["picks"])
        assert all("Generic" in p["why"] for p in sug["picks"] if p["role"] == "scales")
    assert n


def test_quests():
    assert [cb.quests_at(l) for l in (1, 11, 33, 54, 55, 90)] == [0, 1, 2, 2, 3, 3]


def test_spirit_plan_and_report(pool, cat):
    ch = _first(pool, lambda c: c["main_name"] == "Storm Lance")
    parts = cb.parts_at(ch, 40)
    plan = cb.spirit_plan(ch, ch, parts, 40, "auto", 0.0, cat)
    assert plan["budget"] == 60 and plan["quests"] == 2 and plan["gems"]
    assert sum(g["spirit"] for g in plan["gems"]) <= plan["free"] and all(g["level"] <= 40 for g in plan["gems"])
    assert cb.spirit_plan(ch, ch, parts, 40, "auto", 100.0, cat)["budget"] == 160
    named = cb.spirit_plan(ch, ch, parts, 40, ["Herald of Thunder", "Trinity", "Elemental Conflux", "Blink", "Attrition", "Nope"], 20.0, cat)
    skip = {g["name"]: g.get("skip") for g in named["gems"]}
    assert skip == {"Herald of Thunder": None, "Trinity": "level", "Elemental Conflux": "level", "Blink": "spirit",
                    "Attrition": None, "Nope": "unknown"}                      # 80 free: 30, then 60 does not fit, then 30
    view = {"groups": [{"index": 1, "label": "main", "enabled": True, "skills": [{"id": "StormLancePlayer", "name": "Storm Lance"}]},
                       {"index": 2, "label": "Herald of Thunder", "enabled": True, "skills": []},
                       {"index": 3, "label": "Attrition", "enabled": False, "skills": []}]}
    rep = cb.spirit_report(named, {"spirit": {"total": 80, "reserved": 30, "unreserved": 50}}, view)
    assert (rep["budget"], rep["reserved"], rep["unreserved"]) == (80, 30, 50)
    got = {g["name"]: (g["enabled"], g.get("reason")) for g in rep["gems"]}
    assert got == {"Herald of Thunder": (True, None), "Trinity": (False, "level"), "Elemental Conflux": (False, "level"),
                   "Blink": (False, "spirit"), "Attrition": (False, "spirit"), "Nope": (False, "unknown")}
    assert "note" not in rep["planned"]
    assert "note" in cb.spirit_report(named, {"spirit": {"total": 190, "reserved": 30, "unreserved": 160}}, view)["planned"]


def test_parts_left_out_at_a_level_do_not_count(scorer, pool):
    ch = _first(pool, lambda c: c.get("uniques") and (c.get("leveling") or {}).get("steps"))
    parts = cb.parts_at(ch, 1)
    gone = {p["name"] for p in parts if not p["included"] and p["kind"] in ("unique", "keystone", "asc")}
    path = cb.included_path(ch, parts, scorer.g.entities)
    assert gone and not {scorer.g.entities[k].name for k in path if k in scorer.g.entities} & gone
    assert cb.included_path(ch, cb.parts_at(ch, 100), scorer.g.entities) == ch["path"]


def test_off_attribute_gems_go_in_at_gem_level_one(cat):
    gems = [{"name": "Lingering Illusion", "skill": "LingeringIllusionPlayer"}, {"name": "Arctic Armour", "skill": "ArcticArmourPlayer"},
            {"name": "Attrition", "skill": "AttritionPlayer", "skip": "spirit"}, {"name": "Nope", "skill": None}]
    low = cb.low_level_gems(gems, "Witch", cat.skills.get("ArcPlayer"), cat.skills)
    assert list(low.values()) == ["Lingering Illusion"]              # Dex and Int on an Int class; Arctic Armour is Int
    assert all(k.startswith("Metadata/Items/Gems/") for k in low)
    assert not cb.low_level_gems(gems[:2], "Monk", None, cat.skills) and not cb.low_level_gems(gems, None, None, cat.skills)


def test_chains_with_a_misread_link_are_marked_and_ranked_last(scorer, pool, cat):
    sunder = _first(pool, lambda c: c["main_name"] == "Shockchain Arrow" and "enemy:Shocked" in c["tokens"]
                    and "Redflare Conduit" in (c.get("uniques") or []))
    assert any("Shocks you" in d for d in scorer.doubts(sunder))
    ash = _first(pool, lambda c: c["main_name"] == "Herald of Ash")
    assert any("persistent buff" in d for d in scorer.doubts(ash))
    clean = _first(pool, lambda c: c["main_name"] == "Glacial Lance" and not c.get("uniques"))
    assert scorer.doubts(clean) == []
    n = sum(1 for c in pool if scorer.doubts(c))
    assert 0.1 * len(pool) < n < 0.5 * len(pool)
    doc = graphx.run({"top": 2000}, cat=cat)["mechanics"]          # the whole pool, reaction chains included
    flags = [bool(c.get("doubts")) for c in doc["chains"]]
    assert flags == sorted(flags) and any(flags) and doc["info"]["doubted"] == sum(flags)


def test_parts_that_change_the_spirit_budget(scorer, pool, cat):
    r = spiritx._spirit_rule
    assert r("+(30-40) to Spirit") == ("add", 40.0) and r("50% less Spirit") == ("scale", 0.5) and r("8% increased Spirit") == ("scale", 1.08)
    assert r("You have no Spirit")[0] == "none" and r("Persistent Buffs have 50% less Reservation") == ("cheaper", 0.5)
    assert r("Totems reserve 75 Spirit each")[0] == "note" and r("(10-15)% increased Spell Damage per 100 Spirit") is None
    giver = next(u for u, d in cat.uniques.items() if any(spiritx.FLAT_RX.match(x) for x in d["lines"]))
    add = max(float(m.group(2) or m.group(1)) for m in (spiritx.FLAT_RX.match(x) for x in cat.uniques[giver]["lines"]) if m)
    ch = {"main_skill": "ArcPlayer", "main_name": "Arc", "path": ["skill:ArcPlayer"], "tokens": [], "uniques": [giver], "asc_nodes": []}
    sug = scorer.suggest(ch, 100)
    assert sug["budget"] == 100 + add and any(giver in t for t in sug["notes"])
    ch["keystones"] = ["Lord of the Wilds"]
    if "Lord of the Wilds" in cat.keystones:
        assert scorer.suggest(ch, 100)["budget"] == round((100 + add) * 0.5)
