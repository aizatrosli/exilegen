"""Tests for exilegen.scoring. Run: python3 -m unittest tests.test_scoring  (or pytest tests/test_scoring.py)

Fixtures in tests/fixtures/scoring/ are built from the corpus and POC outputs by make_fixtures.py.
Tests that need the full corpus (/mnt/project-files) skip when it is not mounted.
"""
import copy
import csv
import json
import math
import os
import unittest

from exilegen.scoring import BuildView, Registry, Scorer, dominates, front
from exilegen.scoring import energy as E
from exilegen.scoring import floors as F
from exilegen.scoring import constraints as C
from exilegen.scoring.adapter import doc_from_poc, energy_from_poc
from exilegen.scoring.profiles import Profiles

FX = os.path.join(os.path.dirname(__file__), "fixtures", "scoring")
CORPUS = "/mnt/project-files/corpus/poe2/evaluated.jsonl"


def load(name):
    return json.load(open(os.path.join(FX, name)))


OB = load("one_button_poc.json")
SO = load("support_opt_poc.json")
CORP = {r["line"]: r for r in load("corpus_sample.json")}
REG = Registry.load()
PINNACLE20 = Profiles({"pinnacle_v1": {"enemy_power": 20}, "mapping_v1": {"enemy_power": 10}})


def run(rid):
    return next(r for r in OB["runs"] if r["id"] == rid)["r"]


def gem(name, gid, support=False, types=(), activation=None, damaging=False, spell=False, proxy=()):
    return {"name": name, "id": gid, "support": support, "types": list(types), "activation": activation,
            "damaging": damaging, "spell": spell, "proxyKind": list(proxy), "enabled": True}


def synthetic(main_active, supports, stats=None, extra_groups=(), keystones=()):
    """One main group (manual damaging skill + supports) and optional extra groups."""
    g = {"index": 1, "enabled": True, "main": True, "metaGroup": False,
         "gems": [main_active] + [gem(n, i, support=True) for n, i in supports]}
    st = {"CombinedDPS": 1_000_000.0, "TotalEHP": 30000, "Life": 3000, "EnergyShield": 0, "Speed": 2.0,
          "FireResist": 75, "ColdResist": 75, "LightningResist": 75, "ChaosResist": 20,
          "PhysicalMaximumHitTaken": 6000, "FireMaximumHitTaken": 20000, "ColdMaximumHitTaken": 20000,
          "LightningMaximumHitTaken": 20000, "ChaosMaximumHitTaken": 9000, "SpiritUnreserved": 10,
          "ManaPerSecondCost": 50, "ManaRegenRecovery": 100, "LifePerSecondCost": 0, "LifeRegenRecovery": 100,
          "Str": 100, "Dex": 100, "Int": 100, "ReqStr": 50, "ReqDex": 50, "ReqInt": 50, "MovementSpeedMod": 1.2}
    st.update(stats or {})
    return {"stats": st, "mainSkill": {"name": main_active["name"], "id": main_active["id"]},
            "socketGroups": [g] + list(extra_groups), "keystones": list(keystones), "uniques": []}


ICE_SHOT = gem("Ice Shot", "IceShotPlayer", types=["Attack", "Projectile", "Damage"], activation="manual", damaging=True)


from exilegen.scoring.adapter import normalise_energy_group


class TestRegistry(unittest.TestCase):
    def test_size_and_curated_offenders(self):
        self.assertGreater(len(REG), 400)
        names = {e["name"] for e in REG.entries if e["curated"]}
        for n in ("Overextend", "Inexorable Critical", "Atziri's Impatience", "Atalui's Bloodletting", "Minion Pact",
                  "Zerphi's Infamy", "Hit and Run", "Deliberation", "Hexblast", "Detonate Dead", "Spell Cascade"):
            self.assertIn(n, names)
        for e in REG.entries:
            self.assertIn("verified", e)
            self.assertTrue(e.get("source"), e["id"])

    def test_curated_overrides_auto(self):
        hits = REG.lookup("HexblastPlayer", "Hexblast")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]["curated"])
        # Minion Pact I and II share one curated entry
        self.assertEqual(REG.lookup("SupportMinionPactPlayerTwo")[0]["id"], "support.minion_pact")

    def test_auto_scan_found_payoffs(self):
        auto_high = {e["name"] for e in REG.entries if not e["curated"] and e.get("risk") == "high"}
        for n in ("Volatile Dead", "Elemental Sundering", "Fragmentation Rounds"):
            self.assertIn(n, auto_high)


class TestConditionsOnPOC(unittest.TestCase):
    def setUp(self):
        self.s = Scorer(profile="pinnacle_v1", floors=[], registry=REG, profiles=PINNACLE20)

    def test_hexblast_without_curse_blocked(self):
        r = self.s.score(doc_from_poc(run("opt:237"), "final"))      # no curse in the build
        self.assertFalse(r["feasible"])
        self.assertTrue(any(v["entry"] == "gem.hexblast" for v in r["violations"]))
        self.assertLess(r["dps_honest"], 0.1 * r["dps_raw"])

    def test_hexblast_with_curse_discounted(self):
        r = self.s.score(doc_from_poc(run("opt:197"), "final"))      # Blasphemy + Temporal Chains
        h = [c for c in r["conditions"] if c["entry_id"] == "gem.hexblast"][0]
        self.assertEqual(h["action"], "discount")
        self.assertAlmostEqual(h["credit"], 0.5)
        self.assertFalse(any(v["entry"] == "gem.hexblast" for v in r["violations"]))

    def test_hexblast_allowed_as_exported(self):
        s = Scorer(profile="as_exported", floors=[], registry=REG, profiles=PINNACLE20)
        r = s.score(doc_from_poc(run("opt:237"), "final"))
        self.assertTrue(r["feasible"])

    def test_detonate_dead_blocked_on_pinnacle_not_mapping(self):
        doc = doc_from_poc(run("opt:360"), "final")
        s = Scorer(profile=["pinnacle_v1", "mapping_v1"], floors=[], registry=REG, profiles=PINNACLE20)
        r = s.score({"pinnacle_v1": doc, "mapping_v1": doc})
        self.assertFalse(r["profiles"]["pinnacle_v1"]["feasible"])
        self.assertTrue(r["profiles"]["mapping_v1"]["feasible"])
        self.assertFalse(r["feasible"])
        self.assertEqual(r["binding_profile"], "pinnacle_v1")
        self.assertEqual(r["objective"], min(p["objective"] for p in r["profiles"].values()))

    def test_detonate_dead_allowed_with_corpse_source(self):
        doc = doc_from_poc(run("opt:360"), "final")
        doc["socketGroups"].append({"index": 99, "enabled": True, "main": False, "metaGroup": False, "gems": [
            gem("Sacrifice", "SacrificePlayer", types=["Persistent"], activation="persistent"),
            gem("Skeletal Warrior", "SummonSkeletalWarriorsPlayer", types=["Minion", "CreatesMinion"],
                activation="proxy", proxy=["minion"])]})
        r = self.s.score(doc)
        self.assertFalse(any(v["entry"] == "gem.detonate_dead" for v in r["violations"]))

    def test_snap_blocked_when_feeder_cannot_ailment(self):
        # 1231: the optimiser added Elemental Focus to the Spark feeder -> 0% shock; Snap consumes ailments
        r = self.s.score(doc_from_poc(run("opt:1231"), "final"))
        self.assertTrue(any(v["entry"] == "gem.snap" for v in r["violations"]))
        r2 = self.s.score(doc_from_poc(run("opt:223"), "final"))       # 93% shock feeder
        self.assertFalse(any(v["entry"] == "gem.snap" for v in r2["violations"]))

    def test_spell_cascade_group_rule_covers_whole_meta_group(self):
        r = self.s.score(doc_from_poc(run("opt:350"), "final"))
        sc = [c for c in r["conditions"] if c["entry_id"] == "rule.spell_cascade_group" and c["parts"]]
        labels = {k for c in sc for k in c["parts"]}
        self.assertIn("trig:8:Comet", labels)
        self.assertIn("trig:8:Living Bomb", labels)      # every spell socketed with it
        self.assertIn("trig:13:Arc", labels)


class TestSupportConditions(unittest.TestCase):
    def setUp(self):
        self.s = Scorer(profile="pinnacle_v1", registry=REG, profiles=PINNACLE20)

    def score(self, doc):
        return self.s.score(doc)

    def test_overextend_flagged_and_discounted(self):
        r = self.score(synthetic(ICE_SHOT, [("Overextend", "SupportOverextendPlayer")]))
        h = [c for c in r["conditions"] if c["entry_id"] == "support.overextend"][0]
        self.assertEqual(h["action"], "discount")
        self.assertAlmostEqual(r["dps_honest"], 1_000_000 * (1 - 0.5 * 0.2))
        self.assertIn("no_marginal:support.overextend", r["flags"])

    def test_marginal_used_when_engine_supplies_drop_one(self):
        doc = synthetic(ICE_SHOT, [("Overextend", "SupportOverextendPlayer")])
        doc["marginals"] = [{"group": 1, "gem_id": "SupportOverextendPlayer", "dps_without": 800_000}]
        r = self.score(doc)
        self.assertAlmostEqual(r["dps_honest"], 800_000 + 0.5 * 200_000)
        self.assertNotIn("no_marginal:support.overextend", r["flags"])

    def test_minion_pact_needs_a_minion(self):
        r = self.score(synthetic(ICE_SHOT, [("Minion Pact II", "SupportMinionPactPlayerTwo")]))
        self.assertAlmostEqual(r["dps_honest"], 1_000_000 * (1 - 0.26))
        minion = {"index": 2, "enabled": True, "main": False, "metaGroup": False,
                  "gems": [gem("Skeletal Warrior", "SummonSkeletalWarriorsPlayer", types=["Minion", "CreatesMinion"],
                               activation="proxy", proxy=["minion"])]}
        r2 = self.score(synthetic(ICE_SHOT, [("Minion Pact II", "SupportMinionPactPlayerTwo")], extra_groups=[minion]))
        self.assertAlmostEqual(r2["dps_honest"], 1_000_000)

    def test_zerphi_glory_gate(self):
        r = self.score(synthetic(ICE_SHOT, [("Zerphi's Infamy", "SupportZerphisLegacyPlayer")]))
        self.assertAlmostEqual(r["dps_honest"], 0.0)          # no life spent -> no Glory -> skill unusable
        r2 = self.score(synthetic(ICE_SHOT, [("Zerphi's Infamy", "SupportZerphisLegacyPlayer")],
                                  {"LifePerSecondCost": 1000, "LifeRegenRecovery": 2000, "Speed": 2.0}))
        # 5% of 1000 life/s = 50 Glory/s; 2 uses/s need 100 -> 50%
        self.assertAlmostEqual(r2["dps_honest"], 500_000)

    def test_hit_and_run_partial(self):
        r = self.score(synthetic(ICE_SHOT, [("Hit and Run", "SupportHitAndRunPlayer")]))
        self.assertAlmostEqual(r["dps_honest"], 1_000_000 * (1 - 0.5 * 0.6))

    def test_atziri_life_loss_check(self):
        doc = synthetic(ICE_SHOT, [("Atziri's Impatience", "SupportAtzirisImpatiencePlayer")],
                        {"Speed": 1.0, "Life": 3000, "LifeRegenRecovery": 100})
        r = self.score(doc)
        self.assertTrue(any(v["entry"] == "support.atziris_impatience" for v in r["violations"]))
        doc["stats"].update({"Speed": 0.05, "LifeRegenRecovery": 500})     # 0.25*3000*0.05 = 37.5/s < 500
        self.assertFalse(any(v["entry"] == "support.atziris_impatience" for v in self.score(doc)["violations"]))

    def test_atalui_life_cost_sustain(self):
        doc = synthetic(ICE_SHOT, [("Atalui's Bloodletting", "SupportAtaluiBloodlettingPlayer")],
                        {"LifePerSecondCost": 600, "LifeRegenRecovery": 100, "LifeUnreserved": 3000})
        r = self.score(doc)
        self.assertAlmostEqual(r["dps_honest"], 1_000_000 * (100 + 3000 / 60) / 600)
        doc["stats"]["LifePerSecondCost"] = 120
        self.assertAlmostEqual(self.score(doc)["dps_honest"], 1_000_000)

    def test_deliberation_is_utility_only(self):
        r = self.score(synthetic(ICE_SHOT, [("Deliberation", "SupportDeliberationPlayer")]))
        self.assertAlmostEqual(r["dps_honest"], 1_000_000)
        self.assertAlmostEqual(r["utility"]["movement_speed_mult"], 0.95)

    def test_uhtreds_omen_counts_group_supports(self):
        one = self.score(synthetic(ICE_SHOT, [("Uhtred's Omen", "SupportUhtredOmenPlayer"), ("Overextend", "SupportOverextendPlayer")]))
        self.assertEqual([c for c in one["conditions"] if c["entry_id"] == "support.uhtreds_omen"][0]["action"], "allow")
        two = self.score(synthetic(ICE_SHOT, [("Uhtred's Omen", "SupportUhtredOmenPlayer"), ("Overextend", "SupportOverextendPlayer"),
                                               ("Boundless Energy II", "SupportBoundlessEnergyPlayerTwo")]))
        self.assertEqual([c for c in two["conditions"] if c["entry_id"] == "support.uhtreds_omen"][0]["credit"], 0.0)

    def test_support_poc_strict_picks_are_caught(self):
        """support-opt POC: 11/20 strict results use a caveated support PoB does not penalise."""
        caught = 0
        for b in SO:
            sups = [(n, b["support_ids"].get(n)) for n in b["supports_after_strict"]]
            if b["skill"] == "Comet":
                # POC README caveat: this Comet sits in a Cast-on-Crit group whose meta-only supports
                # (Atziri's Communion, Boundless Energy) PoB ignores when it counts Uhtred's Omen's "one other support"
                sups += [("Atziri's Communion", None), ("Boundless Energy II", "SupportBoundlessEnergyPlayerTwo")]
            r = self.score(synthetic(gem(b["skill"], None, activation="manual", damaging=True), sups,
                                     {"CombinedDPS": b["strict_dps"]}))
            if any(c["action"] in ("discount", "block", "flag") and c["entry_id"] in (
                    "support.overextend", "support.inexorable_critical", "support.atziris_impatience",
                    "support.atalui_bloodletting", "support.uhtreds_omen") for c in r["conditions"]):
                caught += 1
        self.assertGreaterEqual(caught, 11)


class TestEnergy(unittest.TestCase):
    def test_reproduces_poc_factors(self):
        n = 0
        for rr in OB["runs"]:
            r = rr["r"]
            for sec in ("baseline", "final"):
                s = r.get(sec)
                if not s or not s.get("triggered"):
                    continue
                bv = BuildView(energy_from_poc(s, r.get("groups") if sec == "final" else None))
                res = E.correct(bv.energy_groups(), 20)
                for sp, t in zip(res.spells, s["triggered"]):
                    self.assertAlmostEqual(sp.factor, t["factor"], delta=0.05 * t["factor"])
                    if sec == "final":
                        self.assertAlmostEqual(sp.energy_rate, t["energyRate"], delta=0.05 * t["energyRate"])
                    n += 1
        self.assertGreaterEqual(n, 30)

    def _final_corr(self, power):
        out = {}
        s = Scorer(profile="pinnacle_v1", floors=[], registry=Registry({}, None),
                   profiles=Profiles({"pinnacle_v1": {"enemy_power": power}}))
        for row in OB["results"]:
            if row["status"] != "ok":
                continue
            r = run(f"{row['mode']}:{row['line']}")
            out[row["line"]] = (s.score(doc_from_poc(r, "final"))["dps_energy"], row)
        return out

    def test_reproduces_results_csv_corrected_dps(self):
        for line, (dps, row) in self._final_corr(20).items():
            self.assertAlmostEqual(dps, float(row["final_corr"]), delta=0.05 * float(row["final_corr"]), msg=line)

    def test_enemy_power_from_profile(self):
        for line, (dps, row) in self._final_corr(10).items():
            self.assertAlmostEqual(dps, float(row["final_corr_p10"]), delta=0.05 * float(row["final_corr_p10"]), msg=line)

    def test_half_energy_sensitivity(self):
        bv = BuildView(doc_from_poc(run("opt:237"), "final"))
        res = E.correct(bv.energy_groups(), 20)
        hx = res.spells[0]
        self.assertAlmostEqual(hx.factor_half_energy, hx.factor / 2)     # energy-limited
        bv = BuildView(doc_from_poc(run("opt:523"), "final"))
        res = E.correct(bv.energy_groups(), 20)
        self.assertEqual(res.spells[2].factor_half_energy, 1.0)          # 1.86/s vs 0.68/s: not limited
        self.assertLessEqual(res.sensitivity, 1.0)

    def test_unmodelled_meta_flagged(self):
        doc = {"stats": {"CombinedDPS": 1000, "TotalEHP": 20000}, "energy": [
            {"group": 3, "meta_id": "MetaCastOnMinionDeathPlayer", "meta_name": "Cast on Minion Death",
             "spells": [{"name": "Comet", "pob_rate": 1.0, "pob_dps": 5000}]}]}
        r = Scorer(profile="pinnacle_v1", floors=[], registry=REG, profiles=PINNACLE20).score(doc)
        self.assertFalse(r["verified"])
        self.assertTrue(any("energy_unmodelled" in f for f in r["flags"]))
        self.assertAlmostEqual(r["dps_energy"], 6000)

    def test_item_granted_metas_are_estimated_not_counted_in_full(self):
        def one(meta, spell, main):
            g = normalise_energy_group({"group": 1, "metaSkillId": meta, "meta_name": meta, "genBonus": 0, "spells": [spell]})
            return E.correct([g], 20, main=main)
        warp = {"name": "Lightning Warp", "pobRate": 3.3, "dps": 5000.0, "castTime": 0.6}
        r = one("MetaSpellslingerPlayer", warp, {})
        self.assertEqual(r.spells[0].kind, "estimated")                 # 20 Energy/s against 60 per cast: 0.33/s
        self.assertAlmostEqual(r.spells[0].factor, (20 / 60) / 3.3)
        self.assertTrue(any(f.startswith("unverified:energy_estimated") for f in r.flags))
        snap = {"name": "Snap", "pobRate": 0.25, "dps": 1000.0, "castTime": 0.5}
        self.assertEqual(one("MetaSpellslingerPlayer", snap, {}).spells[0].factor, 1.0)
        self.assertEqual(one("MetaCastOnBlockPlayer", snap, {"block": None}).spells[0].kind, "unmodelled")
        self.assertAlmostEqual(one("MetaCastOnBlockPlayer", snap, {"block": 0.3}).spells[0].factor, (7.5 / 50) / 0.25)
        self.assertEqual(one("MetaCastLightningSpellOnHitPlayer", snap, {"hits_per_sec": 2.0}).spells[0].factor, 1.0)
        self.assertEqual(one("MetaSpellslingerPlayer", snap, None).spells[0].kind, "unmodelled")

    def test_freeze_energy_ignored_unless_reported(self):
        g = {"group": 1, "meta_id": "MetaCastOnElementalAilmentPlayer", "max_energy": 100, "gen_pct": 0,
             "feeder": {"hits_per_sec": 2, "shock": 0, "ignite": 0, "freeze": 0.5},
             "spells": [{"name": "Comet", "pob_rate": 1.0, "pob_dps": 1000}]}
        res = E.correct(BuildView({"energy": [g]}).energy_groups(), 20)
        self.assertEqual(res.spells[0].factor, 0.0)
        self.assertIn("energy:freeze_ignored:1", res.flags)
        g["feeder"]["freeze_events_per_sec"] = 0.5
        res = E.correct(BuildView({"energy": [g]}).energy_groups(), 20)
        self.assertAlmostEqual(res.spells[0].energy_rate, 0.5 * 10 * 20 / 100)


class TestFloors(unittest.TestCase):
    def test_corpus_record(self):
        bv = BuildView.from_corpus(CORP[3], "pinnacle_locked")      # CI Oracle, cold/lightning 57
        res = {r["id"]: r for r in F.check(F.DEFAULT_FLOORS, bv)}
        self.assertFalse(res["res_cold"]["passed"])
        self.assertTrue(res["res_fire"]["passed"])
        self.assertTrue(res["res_chaos"]["passed"])               # CI: chaos ignored
        self.assertIsNone(res["attributes"]["passed"])            # corpus has no Req* fields -> unknown
        # relative mode: the build itself as baseline passes "not reduced"
        res2 = {r["id"]: r for r in F.check(F.DEFAULT_FLOORS, bv, bv)}
        self.assertTrue(res2["res_cold"]["passed"])
        self.assertTrue(res2["ehp"]["passed"])

    def test_relative_mode_blocks_regression(self):
        base = BuildView(synthetic(ICE_SHOT, [], {"ColdResist": 60}))
        worse = BuildView(synthetic(ICE_SHOT, [], {"ColdResist": 50}))
        res = {r["id"]: r for r in F.check(F.DEFAULT_FLOORS, worse, base)}
        self.assertFalse(res["res_cold"]["passed"])
        same = {r["id"]: r for r in F.check(F.DEFAULT_FLOORS, base, base)}
        self.assertTrue(same["res_cold"]["passed"])

    def test_spirit_attribute_and_sustain(self):
        s = Scorer(profile="pinnacle_v1", registry=REG, profiles=PINNACLE20)
        r = s.score(synthetic(ICE_SHOT, [], {"SpiritUnreserved": -5, "ReqInt": 200}))
        bad = {v["entry"] for v in r["violations"]}
        self.assertEqual(bad, {"attributes"})          # no baseline: Spirit is only a target (PoB unreliable)
        self.assertIn("spirit_target", r["below_target"])
        r = s.score(synthetic(ICE_SHOT, [], {"SpiritUnreserved": -5}), baseline=synthetic(ICE_SHOT, []))
        self.assertEqual({v["entry"] for v in r["violations"]}, {"spirit"})
        r = s.score(synthetic(ICE_SHOT, [], {"SpiritUnreserved": -50}),
                    baseline=synthetic(ICE_SHOT, [], {"SpiritUnreserved": -60}))
        self.assertNotIn("spirit", {v["entry"] for v in r["violations"]})   # min(0, baseline) rule
        base = synthetic(ICE_SHOT, [], {"ManaPerSecondCost": 150})
        worse = synthetic(ICE_SHOT, [], {"ManaPerSecondCost": 400})
        r = s.score(worse, baseline=base)
        self.assertIn("mana_sustain", {v["entry"] for v in r["violations"]})

    @unittest.skipUnless(os.path.exists(CORPUS), "corpus not mounted")
    def test_corpus_floor_rates(self):
        n = ok = 0
        with open(CORPUS) as fh:
            lines = list(fh)
        for line in lines:
            d = json.loads(line)
            if not d.get("pinnacle_locked"):
                continue
            bv = BuildView.from_corpus(d, "pinnacle_locked")
            r = {x["id"]: x for x in F.check(F.DEFAULT_FLOORS, bv)}
            if "Chaos Inoculation" in bv.names("keystones"):
                continue
            n += 1
            ok += bool(r["res_chaos"]["passed"])
        self.assertGreater(ok / n, 0.98)


class TestConstraints(unittest.TestCase):
    def test_corpus_tags(self):
        bv = BuildView.from_corpus(CORP[3], "pinnacle_locked")      # Orb of Storms + Spark, manual spells
        v, _ = C.evaluate(["one_button", "no_cast", "no_cast:strict", "max_manual_skills:5",
                           "require_unique:Headhunter", "forbid_keystone:Chaos Inoculation", "no_minions"], bv)
        got = {x["constraint"] for x in v}
        self.assertEqual(got, {"one_button", "no_cast", "no_cast:strict", "forbid_keystone:Chaos Inoculation"})
        self.assertEqual(bool(CORP[3]["tags"]["oneButton"]), "one_button" not in got)

    def test_matches_corpus_one_button_tag(self):
        for line, rec in CORP.items():
            bv = BuildView.from_corpus(rec, "pinnacle_locked")
            v, _ = C.evaluate(["one_button"], bv)
            self.assertEqual(not v, bool(rec["tags"]["oneButton"]), line)

    def test_engine_skill_list_input(self):
        skills = [
            {"group": 1, "name": "Spark", "id": "SparkPlayer", "activation": "manual", "damaging": True, "spell": True, "main": True},
            {"group": 2, "name": "Cast on Critical", "id": "MetaCastOnCritPlayer", "activation": "persistent"},
            {"group": 2, "name": "Comet", "id": "CometPlayer", "activation": "triggered", "damaging": True, "spell": True},
            {"group": 3, "name": "Blink", "id": "BlinkPlayer", "activation": "manual"},
        ]
        bv = BuildView({"stats": {}}, skills)
        v, _ = C.evaluate(["one_button", "one_button:strict", "no_cast:lenient", "max_manual_skills:1"], bv)
        self.assertEqual({x["constraint"] for x in v}, {"no_cast:lenient", "max_manual_skills:1"})

    def test_missing_skill_data_is_unverified(self):
        v, flags = C.evaluate(["one_button"], BuildView({"stats": {}}))
        self.assertEqual(v, [])
        self.assertIn("unverified:constraints_no_skill_data", flags)


class TestScorer(unittest.TestCase):
    def test_empty_input_does_not_crash(self):
        r = Scorer(registry=REG, profiles=PINNACLE20).score({})
        self.assertFalse(r["verified"])
        self.assertEqual(r["dps_honest"], 0)
        self.assertIn("explain", r)

    def test_corpus_record_end_to_end(self):
        s = Scorer(profile="pinnacle_v1", constraints=["one_button"], registry=REG, profiles=PINNACLE20)
        r = s.score(BuildView.from_corpus(CORP[563], "pinnacle_locked"))     # Detonate Dead main skill
        self.assertTrue(any(v["entry"] == "gem.detonate_dead" for v in r["violations"]))
        self.assertTrue(any(f.startswith("unsupported_lines:") for f in r["flags"]))
        for k in ("feasible", "violations", "objective", "dps_raw", "dps_honest", "ehp", "factors", "flags", "explain"):
            self.assertIn(k, r)

    def test_utility_terms_move_objective(self):
        s = Scorer(registry=REG, profiles=PINNACLE20)
        a = s.score(synthetic(ICE_SHOT, [], {"MovementSpeedMod": 1.0}))
        b = s.score(synthetic(ICE_SHOT, [], {"MovementSpeedMod": 1.1}))
        self.assertAlmostEqual(b["objective"] - a["objective"], 0.5 * math.log(1.1), places=6)
        c = s.score(synthetic(ICE_SHOT, [], {"ManaPerSecondCost": 1000, "ManaRegenRecovery": 100, "ManaUnreserved": 0}))
        self.assertLess(c["objective"], a["objective"] + 0.5 * math.log(1.2) - 0.1)

    def test_pareto(self):
        a = {"dps_honest": 10, "ehp": 10, "feasible": True}
        b = {"dps_honest": 5, "ehp": 5, "feasible": True}
        c = {"dps_honest": 20, "ehp": 1, "feasible": True}
        d = {"dps_honest": 30, "ehp": 30, "feasible": False}
        self.assertTrue(dominates(a, b))
        self.assertFalse(dominates(a, c))
        self.assertEqual(front([a, b, c, d]), [a, c])
        self.assertEqual(front([a, b, c, d], feasible_only=False), [d])

    def test_profiles_json_missing_uses_defaults(self):
        p = Profiles({})
        self.assertEqual(p.param("pinnacle_v1", "enemy_power"), (20, True))
        p = Profiles({"pinnacle_v1": {"params": {"enemy_power": 15}}})
        self.assertEqual(p.param("pinnacle_v1", "enemy_power"), (15, False))


if __name__ == "__main__":
    unittest.main()
