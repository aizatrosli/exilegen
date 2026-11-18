"""Game-rule fixes from the knowledge audit (17 Nov): builder data rules and new scoring checks."""
from __future__ import annotations

import random

from exilegen.builder import catalog as C
from exilegen.builder.gear import GearPlanner, weapon_spec
from exilegen.builder.skeletons import Generator
from test_scoring import ICE_SHOT, PINNACLE20, REG, Scorer, gem, synthetic


def test_talisman_is_two_handed_and_unreleased_weapons_marked():
    assert "Talisman" in C.TWO_HANDED and "Talisman" not in C.ONE_HANDED
    assert not {"Claw", "Dagger", "Flail", "One Hand Sword", "Two Hand Axe"} & C.RELEASED_WEAPONS


def test_removed_content_filtered():
    assert C.is_removed_gem("Overextend") and C.is_removed_gem("Shock Conduction II")
    assert not C.is_removed_gem("Spell Cascade")
    cat = C.load()
    assert "The Road Warrior" not in cat.uniques
    assert not any(s["name"] in C.removed()["gems"] for s in cat.skills.values())


def test_unarmed_skills_get_no_weapon():
    spec = weapon_spec("Unarmed")
    assert spec is not None and spec["type"] is None
    gp = GearPlanner(None)
    gp.equip_weapons(spec)
    assert "Weapon 1" not in gp.taken and "Weapon 2" not in gp.taken
    gen = Generator.__new__(Generator)
    s = {"types": ["Attack", "ProjectilesFromUser"], "weapon_types": ["None"]}
    assert gen._weapon_for(random.Random(1), s, []) == "Unarmed"
    s = {"types": ["Attack", "Melee"], "weapon_types": ["Claw", "Dagger", "One Hand Mace", "Spear"]}
    for i in range(20):
        assert gen._weapon_for(random.Random(i), s, []) in ("One Hand Mace", "Spear")


def test_templates_follow_defence_and_skill_kind():
    gp = GearPlanner(None, defence="ci")
    gp.skill_types = {"Attack", "Melee"}
    amu = gp.template_for("Amulet", "jewellery")
    flat = [rx for alts in amu["prefixes"] + amu["suffixes"] for rx in alts]
    assert not any("Life" in rx for rx in flat)
    assert "^GlobalIncreaseMeleeSkillGemLevel$" in flat and "^GlobalIncreaseSpellSkillGemLevel$" not in flat
    gp = GearPlanner(None, defence="life")
    gp.skill_types = {"Spell"}
    flat = [rx for alts in gp.template_for("Amulet", "x")["suffixes"] for rx in alts]
    assert "^GlobalIncreaseSpellSkillGemLevel$" in flat


def _scorer():
    return Scorer(profile="pinnacle_v1", registry=REG, profiles=PINNACLE20)


def test_demon_form_credit_follows_sustain():
    df = {"index": 2, "enabled": True, "main": False, "metaGroup": False,
          "gems": [gem("Demon Form", "DemonFormPlayer", types=["Buff", "Spell"], activation="manual")]}
    spark = gem("Spark", "SparkPlayer", types=["Spell", "Damage"], activation="manual", damaging=True, spell=True)
    ok = _scorer().score(synthetic(spark, [], {"NetLifeRegen": 50, "LifeRegenRecovery": 100}, extra_groups=[df]))
    assert abs(ok["dps_honest"] - 1_000_000) < 1
    bad = _scorer().score(synthetic(spark, [], {"NetLifeRegen": -100, "LifeRegenRecovery": 100}, extra_groups=[df]))
    # 200 life/s lost at 10 stacks, 100 recovered: holds 5 stacks -> half the bonus; share 0.25 -> x0.875
    assert abs(bad["dps_honest"] - 875_000) < 1


def test_detonate_dead_on_own_minion_corpse():
    dd = gem("Detonate Dead", "DetonateDeadPlayer", types=["Spell", "Damage"], activation="manual", damaging=True, spell=True)
    doc = synthetic(dd, [])
    doc["socketGroups"].append({"index": 9, "enabled": True, "main": False, "metaGroup": False, "gems": [
        gem("Sacrifice", "SacrificePlayer", types=["Persistent"], activation="persistent"),
        gem("Skeletal Warrior", "SummonSkeletalWarriorsPlayer", types=["Minion", "CreatesMinion"],
            activation="proxy", proxy=["minion"])]})
    doc["skills"] = [{"group": 1, "name": "Detonate Dead", "id": "DetonateDeadPlayer", "offence": {}, "corpseLife": 20000},
                     {"group": 9, "name": "Skeletal Warrior", "id": "SummonSkeletalWarriorsPlayer", "offence": {},
                      "minionLife": 5000}]
    assert abs(_scorer().score(doc)["dps_honest"] - 250_000) < 1


def test_cooldown_with_fixed_hit_rate_is_flagged():
    doc = synthetic(ICE_SHOT, [])
    doc["offence"] = {"hitSpeed": 8.0, "cooldown": 4.0}
    assert any(f.startswith("unverified:cooldown_ignored") for f in _scorer().score(doc)["flags"])
