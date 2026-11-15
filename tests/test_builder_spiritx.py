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
    assert gems["Combat Frenzy"]["gives"]["charge:Frenzy"]["w"] > 0.5
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
    assert [p["name"] for p in r["picks"]] == ["A", "C"] and r["used"] == 90         # A+C (1.6) beats A+D (1.55)
    more = {m["name"]: m for m in r["more"]}
    assert more["B"]["blocked"] == "weaker" and more["B"]["same_job_as"] == "A"
    assert more["D"]["blocked"] == "budget" and more["E"]["blocked"] == "budget" and "same_job_as" not in more["D"]
    assert all("tier" not in p and "job" not in p for p in r["picks"] + r["more"])
    r = spiritx.pick(cands, 160)
    assert [p["name"] for p in r["picks"]] == ["A", "C", "D", "F"] and r["used"] == 150   # links first, then scaling
    r = spiritx.pick(cands, 290)
    assert [p["name"] for p in r["picks"]][-1] == "G" and r["used"] == 280            # one utility gem, the linked one
    assert {m["name"]: m["blocked"] for m in r["more"]}["H"] == "weaker"
    assert spiritx.pick(cands, 20) == {"picks": [], "more": spiritx.pick(cands, 20)["more"], "used": 0}
    assert spiritx.pick(cands, -40)["picks"] == []


def test_pick_respects_the_unlock_level():
    cands = [_cand("Late", 30, 0.9, level=58), _cand("Early", 30, 0.6, level=10)]
    r = spiritx.pick(cands, 60, level=33)
    assert [p["name"] for p in r["picks"]] == ["Early"] and r["more"][0]["blocked"] == "level"
    assert [p["name"] for p in spiritx.pick(cands, 60, level=None)["picks"]] == ["Late", "Early"]
    assert len(spiritx.pick([_cand(str(i), 30, 0.5 - i / 100, role="utility", job="utility") for i in range(10)], 0)["more"]) == spiritx.MORE_MAX


# ------------------------------------------------------------------------------------------------ chains
def _first(pool, pred):
    return next(c for c in pool if pred(c))


def test_a_herald_pays_off_the_state_an_attack_applies(scorer, pool, cat):
    ch = _first(pool, lambda c: c["main_name"] == "Storm Lance")
    sug = scorer.suggest(ch, 60, 33)
    top = sug["picks"][0]
    assert (top["name"], top["role"], top["token"], top["spirit"], top["level"]) == \
        ("Herald of Thunder", "pays_off", "enemy:Shocked", 30, 10)
    assert "killing Shocked enemies" in top["why"] and "Storm Lance" in top["why"]
    assert sug["budget"] == 60 and sug["free"] == 60 - sug["own"] and sug["used"] <= max(sug["free"], 0)
    # a spell cannot use an Attack herald, whatever it applies
    spell = _first(pool, lambda c: c["main_name"] == "Arc")
    assert not [c for c in scorer.candidates(spell) if c["name"].startswith("Herald of")]


def test_a_gem_feeds_the_payoff_part(scorer, pool):
    ch = _first(pool, lambda c: "charge:Power" in c["tokens"] and "Price of Power" in [n for _, n in c["asc_nodes"]])
    c = next(c for c in scorer.candidates(ch) if c["name"] == "Lingering Illusion")
    assert c["role"] == "feeds" and c["token"] == "charge:Power" and "Price of Power" in c["why"] and "Power Charge" in c["why"]
    # Charge Regulation would spend the very charges the payoff spends: not offered as a payoff here
    assert not [c for c in scorer.candidates(ch) if c["name"] == "Charge Regulation" and c.get("token") == "charge:Power"]


def test_generic_gems_are_listed_not_picked(scorer, pool):
    n = picked = 0
    for ch in pool[:300]:
        sug = scorer.suggest(ch, 100, 90)
        n += 1
        assert sug["used"] == sum(p["spirit"] for p in sug["picks"]) <= max(sug["free"], 0)
        assert len(sug["more"]) <= spiritx.MORE_MAX and all(m["blocked"] in ("budget", "weaker", "level") for m in sug["more"])
        assert len([p for p in sug["picks"] if p["role"] == "utility"]) <= 1
        picked += any(p["name"] in ("Attrition", "War Banner") for p in sug["picks"])
        roles = [spiritx.ROLE_TIER[p["role"]] for p in sug["picks"]]
        assert all(p["score"] >= spiritx.MIN_SCORE and p["why"].endswith(".") for p in sug["picks"])
        assert roles == sorted(roles)                           # state links, then scaling, then utility
    assert n and picked == 0


def test_ascendancy_that_removes_spirit(scorer, cat):
    ch = {"id": "x", "main_skill": "ArcPlayer", "main_name": "Arc", "tokens": ["enemy:Shocked"], "path": ["skill:ArcPlayer"],
          "asc_nodes": [["Acolyte of Chayula", "Embrace the Darkness"]], "tempo": {"spirit_total": 0}}
    sug = scorer.suggest(ch, 100, 90)
    assert sug["budget"] == 0 and not sug["picks"] and "no Spirit" in sug["notes"][0]
    ch["asc_nodes"] = [["Invoker", "Lead me through Grace..."]]
    assert scorer.suggest(ch, 140, 90, gear=40)["budget"] == 100
    ch["asc_nodes"] = [["Invoker", "Some Other Node"]]
    sug = scorer.suggest(ch, 140, 90, gear=40)
    assert sug["budget"] == 140 and "only if that notable is taken" in sug["notes"][0]


# ------------------------------------------------------------------------------------------------ graph document
def test_fixed_source_leaves_scores_and_ranking_alone(cat):
    base = graphx.run({"top": 60, "spirit_gems": False}, cat=cat)["mechanics"]
    doc = graphx.run({"top": 60}, cat=cat)["mechanics"]
    assert [(c["id"], c["score"], c["tempo"]) for c in doc["chains"]] == [(c["id"], c["score"], c["tempo"]) for c in base["chains"]]
    assert all("spirit" not in c for c in base["chains"]) and all("spirit" in c for c in doc["chains"])
    assert doc["info"]["spirit"]["budget"] == 200 and doc["info"]["spirit"]["source"] == "fixed"
    assert "level" not in doc["info"]["spirit"] and base["info"]["spirit"]["gems"] == doc["info"]["spirit"]["gems"] > 50


def test_level_source_in_the_graph_document(cat):
    doc = graphx.run({"top": 50, "spirit_source": "level", "spirit_level": 33, "spirit_gear": 40}, cat=cat)["mechanics"]
    info = doc["info"]["spirit"]
    assert (info["budget"], info["source"], info["level"], info["quest"], info["gear"]) == (100, "level", 33, 60, 40)
    assert {p["id"] for p in info["gear_presets"]} >= {"sceptre", "body", "amulet"}
    assert all(set(p) == {"id", "label", "spirit", "note"} for p in info["gear_presets"])
    for c in doc["chains"]:
        sp = c["spirit"]
        assert sp["budget"] == 100 and sp["free"] == 100 - sp["own"] == 100 - c["tempo"]["spirit_total"]
        assert all(p["level"] <= 33 for p in sp["picks"])
        assert all(set(p) >= {"name", "skill", "spirit", "role", "why", "level", "score"} for p in sp["picks"] + sp["more"])
        assert all(("token" in p) == (p["role"] in ("feeds", "pays_off")) for p in sp["picks"] + sp["more"])
    assert any(c["spirit"]["picks"] for c in doc["chains"])
    assert graphx.run({"top": 50, "spirit_source": "level"}, cat=cat)["mechanics"]["info"]["spirit"]["level"] == spiritx.FINAL_LEVEL


# ------------------------------------------------------------------------------------------------ the build
def test_spirit_request_arguments():
    assert cb.spirit_request(None) == ("none", 0.0) and cb.spirit_request("none", 0) == ("none", 0.0)
    assert cb.spirit_request(" Auto ", 100) == ("auto", 100.0)
    assert cb.spirit_request("Herald of Ice, trinity,,herald of ice") == (["Herald of Ice", "trinity"], 0.0)
    assert cb.spirit_request(["Attrition"], 40.5) == (["Attrition"], 40.5)
    with pytest.raises(cb.ChainError):
        cb.spirit_request("auto", -1)
    assert [cb.quests_at(l) for l in (1, 11, 33, 54, 55, 90)] == [0, 1, 2, 2, 3, 3]


def test_spirit_plan_and_report(pool, cat):
    ch = _first(pool, lambda c: c["main_name"] == "Storm Lance")
    parts = cb.parts_at(ch, 40)
    plan = cb.spirit_plan(ch, ch, parts, 40, "auto", 0.0, cat)
    assert plan["budget"] == 60 and plan["quests"] == 2 and plan["gems"][0]["name"] == "Herald of Thunder"
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
