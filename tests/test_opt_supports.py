"""SupportStep: unit checks (no engine) + one fast engine run on a POC build (skipped without the engine)."""
import json
import os
import unittest

from exilegen.engine import find_binary, find_pob_root
from exilegen.optimise.supports import SupportStep

BUILDS = "/mnt/project-files/poc/support-opt/builds.jsonl"


class TestLegality(unittest.TestCase):
    def c(self, id_, name, conflicts=(), family=()):
        return {"id": id_, "name": name, "conflicts": [{"slot": s, "why": "family"} for s in conflicts],
                "family": list(family)}

    def test_single(self):
        self.assertTrue(SupportStep._legal([(2, self.c("a", "A"))]))
        self.assertTrue(SupportStep._legal([(2, self.c("a", "A", conflicts=[2]))]))
        self.assertFalse(SupportStep._legal([(2, self.c("a", "A", conflicts=[3]))]))

    def test_pair(self):
        a, b = self.c("a", "Execute II", conflicts=[3]), self.c("b", "Pierce")
        self.assertTrue(SupportStep._legal([(2, b), (3, a)]))
        self.assertFalse(SupportStep._legal([(2, b), (4, a)]))
        # two tiers of one gem, or a shared family, never sit together
        self.assertFalse(SupportStep._legal([(2, self.c("x", "Execute II")), (3, self.c("y", "Execute III"))]))
        self.assertFalse(SupportStep._legal([(2, self.c("x", "A", family=["f"])), (3, self.c("y", "B", family=["f"]))]))

    def test_view_with(self):
        view = {"groups": [{"index": 1, "gems": [{"slot": 1, "id": "act", "name": "Spark", "support": False},
                                                {"slot": 2, "id": "old", "name": "Old", "support": True}]}]}
        v = SupportStep._view_with(view, 1, [(2, {"id": "new", "skillId": "SupportNew", "name": "New"})])
        self.assertEqual(v["groups"][0]["gems"][1]["name"], "New")
        self.assertEqual(view["groups"][0]["gems"][1]["name"], "Old")      # original untouched


@unittest.skipUnless(find_binary() and find_pob_root() and os.path.exists(BUILDS), "engine binary / PoB / POC builds missing")
class TestSupportStepEngine(unittest.TestCase):
    def test_one_build_small_budget(self):
        from exilegen.engine import Engine
        from exilegen.optimise.base import Context
        from exilegen.scoring import Scorer
        with open(BUILDS) as f:
            b = json.loads(f.readline())          # Comet (Cast on Critical), 2 optimisable supports
        with Engine() as e:
            e.load(code=b["code_before"], profile="pinnacle_v1", main=b["group"])
            ctx = Context(engine=e, scorer=Scorer(profile=["pinnacle_v1"], floor_mode="relative"), budget=400,
                          log_fn=None)
            r = SupportStep(groups="main", top_k=3, max_rounds=1).run(ctx)
            self.assertEqual(r.name, "supports")
            self.assertLessEqual(r.evals, 400 + 60)          # budget respected (one confirm batch of slack)
            self.assertGreaterEqual(r.end_objective, r.start_objective - 1e-9)
            for c in r.changes:
                self.assertGreater(c["objective_delta"], 0)
                for k in ("group", "slot", "old", "new", "dps_honest_delta", "flags"):
                    self.assertIn(k, c)
            # the engine state carries the committed supports
            if r.changes:
                names = {g["name"] for grp in e.skills()["groups"] if grp["index"] == b["group"] for g in grp["gems"]}
                last = r.changes[-1]["new"]
                for n in (last if isinstance(last, list) else [last]):
                    self.assertIn(n, names)


if __name__ == "__main__":
    unittest.main()
