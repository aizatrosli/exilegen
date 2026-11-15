"""Archive, runner and report of the from-scratch builder, with stub skeletons / synth (no engine)."""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time

import pytest

from exilegen.builder import archive as A
from exilegen.builder.archive import Archive, Cell, Entry, cell_key, describe
from exilegen.builder.contracts import BuildResult, Skeleton
from exilegen.builder.report import render, write_report
from exilegen.builder.runner import Bandit, Runner, RunDirLocked


# ---------------------------------------------------------------------------------------------- stubs
def sk(main="Spark", asc="Stormweaver", cls="Sorceress", source="corpus_prior", uniques=None, n=0, **kw) -> Skeleton:
    body = json.dumps([main, asc, cls, source, uniques, n, sorted(kw.items())], default=str)
    return Skeleton(id=hashlib.sha1(body.encode()).hexdigest()[:16], cls=cls, ascendancy=asc, main_skill=main,
                    uniques=uniques or [], source=source, thesis=f"stub {main} {n}", **kw)


def stats_doc(main="Spark", hit=None, life=3000, es=500, armour=0, evasion=5000, block=0, keystones=(),
              dps=100000.0, ignite=0.0, minion=0.0, types=("Spell", "Lightning", "Damage")):
    hit = hit or {"Lightning": 1000.0}
    return {"mainSkill": {"name": main, "id": main.replace(" ", "") + "Player", "types": list(types)},
            "main": {"group": 1, "skill": 1, "name": main},
            "stats": {"CombinedDPS": dps + ignite, "TotalDPS": dps, "NonHitDPS": ignite, "IgniteDPS": ignite,
                      "HitDamageByType": hit, "Life": life, "EnergyShield": es, "Armour": armour,
                      "Evasion": evasion, "EffectiveBlockChance": block},
            "dps": {"player": dps + ignite, "minion": minion, "best": max(dps + ignite, minion)},
            "maxHit": {"min": 6000.0}, "keystones": [{"name": k} for k in keystones], "uniques": []}


def skills_view(main="Spark", activation="manual", proxy=()):
    return {"groups": [{"index": 1, "enabled": True, "main": True, "metaGroup": activation == "triggered",
                        "gems": [{"slot": 1, "name": main, "support": False, "skillId": main + "Player"}],
                        "skills": [{"index": 1, "name": main, "id": main.replace(" ", "") + "Player", "gemSlot": 1,
                                    "activation": activation, "proxyKind": list(proxy), "damaging": True,
                                    "spell": True, "types": ["Spell"]}]}]}


def score(dps, ehp, feasible=True, verified=True, flags=None):
    flags = list(flags or [])
    if not verified and not any("unverified" in f for f in flags):
        flags.append("unverified:triggered_dps_without_energy")
    import math
    return {"feasible": feasible, "verified": verified, "violations": [] if feasible else
            [{"rule": "constraint", "entry": "one_button", "detail": "2 manual damaging skills"}],
            "objective": math.log(dps + 1) + 0.3 * math.log(ehp + 1), "dps_raw": dps, "dps_honest": dps,
            "ehp": ehp, "flags": flags, "profile": "pinnacle_v1", "explain": "stub"}


def result(s: Skeleton, dps=1e5, ehp=2e4, feasible=True, verified=True, ok=True, **doc_kw) -> BuildResult:
    if not ok:
        return BuildResult(skeleton=s, ok=False, error="synth failed")
    return BuildResult(skeleton=s, ok=True, code="CODE-" + s.id, score=score(dps, ehp, feasible, verified),
                       stats={"pinnacle_v1": stats_doc(s.main_skill, dps=dps, **doc_kw)},
                       skills=skills_view(s.main_skill), evals=100, seconds=1.0)


@pytest.fixture
def fake_corpus(tmp_path, monkeypatch):
    ov = {"totalCharacters": 1000,
          "dimensions": {"class": {"counts": [["Stormweaver", 500], ["Infernalist", 300], ["Titan", 200]]},
                         "skills": {"counts": [["Spark", 400], ["Comet", 50], ["Boneshatter", 3]]},
                         "items": {"counts": [["Headhunter", 100]]}},
          "skillByAscendancy": {"Spark": {"total": 400, "byAscendancy": [["Stormweaver", 300], ["Infernalist", 100]]},
                                "Comet": {"total": 50, "byAscendancy": [["Stormweaver", 2], ["Infernalist", 48]]}}}
    p = tmp_path / "overview.json"
    p.write_text(json.dumps(ov))
    c = A.Corpus(str(p))
    monkeypatch.setattr(A, "_CORPUS", c)
    return c


# ---------------------------------------------------------------------------------------------- descriptor
def test_corpus_buckets(fake_corpus):
    assert fake_corpus.bucket("Spark", "Stormweaver") == "meta"          # 300/500
    assert fake_corpus.bucket("SparkPlayer", "Stormweaver") == "meta"    # gem id spelling
    assert fake_corpus.bucket("Comet", "Stormweaver") == "niche"         # 2/500 = 0.4%
    assert fake_corpus.bucket("Comet", "Titan") == "unseen"
    assert fake_corpus.bucket("Boneshatter", "Titan") in ("niche", "rare")   # not in table: estimate
    assert fake_corpus.bucket("Nonexistent", "Titan") == "unseen"
    assert A.Corpus("/nonexistent/overview.json").bucket("Spark", "Stormweaver") == "unknown"


def test_describe_axes(fake_corpus):
    s = sk("Spark", "Stormweaver", uniques=[{"name": "Rathpith Globe"}])
    d = describe(result(s))
    assert d["damage"] == "lightning"
    assert d["activation"] == "manual"
    assert d["defence"] == "life/evasion"
    assert d["unique"] == "Rathpith Globe"
    assert d["ascendancy"] == "Stormweaver"
    assert d["corpus"] == "meta"
    # mixed hit + ignite DoT, CI, block, triggered, no unique
    s2 = sk("Comet", "Infernalist")
    r2 = BuildResult(skeleton=s2, ok=True, score=score(1e5, 1e4),
                     stats=stats_doc("Comet", hit={"Cold": 500, "Fire": 500}, dps=1e5, ignite=3e5, life=1, es=0,
                                     block=60, keystones=["Chaos Inoculation"]),
                     skills=skills_view("Comet", "triggered"))
    d2 = describe(r2)
    assert d2["damage"] == "dot:fire"
    assert d2["activation"] == "triggered"
    assert d2["defence"] == "ci/block"
    assert d2["unique"] == "none"
    # two-type mix and minion
    r3 = BuildResult(skeleton=sk("Skeletal Warrior", "Infernalist"), ok=True, score=score(1, 1),
                     stats=stats_doc("Skeletal Warrior", hit={"Cold": 500, "Physical": 450, "Fire": 50}, dps=10,
                                     minion=1e5, es=9000, life=1000, armour=8000, evasion=100),
                     skills=skills_view("Skeletal Warrior", "proxy", ["minion"]))
    d3 = describe(r3)
    assert d3["damage"] == "cold+physical"
    # minion build whose damage is only in minionStats
    doc4 = stats_doc("Raise Zombie", hit={}, dps=0, minion=5e4)
    doc4["minionStats"] = {"CombinedDPS": 5e4, "TotalDPS": 5e4, "HitDamageByType": {"Physical": 900, "Chaos": 50}}
    r4 = BuildResult(skeleton=sk("Raise Zombie", "Infernalist"), ok=True, score=score(1, 1), stats=doc4,
                     skills=skills_view("Raise Zombie", "proxy", ["minion"]))
    assert describe(r4)["damage"] == "physical" and describe(r4)["activation"] == "minion"
    assert d3["activation"] == "minion"
    assert d3["defence"] == "es/armour"
    assert cell_key(d3).count("|") == len(A.DEFAULT_AXES) - 1
    assert cell_key(d3) == "cold|minion|es|Sorceress"
    assert cell_key(d3, A.FINE_AXES).count("|") == len(A.FINE_AXES) - 1


def test_describe_without_stats_uses_skeleton_hints(fake_corpus):
    s = sk("Spark", "Stormweaver", trigger={"meta": "MetaCastOnCritPlayer"}, defence="es")
    d = describe(BuildResult(skeleton=s, ok=False))
    assert d["activation"] == "triggered" and d["defence"] == "es" and d["damage"] == "unknown"


# ---------------------------------------------------------------------------------------------- cells
def test_cell_pareto_and_tiers(fake_corpus):
    arch = Archive()
    base = dict(main="Spark", asc="Stormweaver")
    st = arch.add(result(sk(n=1, **base), dps=1e5, ehp=2e4))
    assert st == "new_cell"
    # dominated (less of both)
    assert arch.add(result(sk(n=2, **base), dps=5e4, ehp=1e4)) == "dominated"
    # trade-off: more EHP, less DPS -> joins the front, elite unchanged (lower objective)
    assert arch.add(result(sk(n=3, **base), dps=6e4, ehp=9e4)) == "front"
    # dominates everything -> improved, evicts the others
    assert arch.add(result(sk(n=4, **base), dps=2e5, ehp=1e5)) == "improved"
    cell = next(iter(arch.cells.values()))
    assert [e.uid for e in cell.tiers[0]] == [sk(n=4, **base).id]
    # an unverified build with a huge DPS is stored in tier 1, never displaces the verified elite
    stu = arch.add(result(sk(n=5, **base), dps=1e7, ehp=1e6, verified=False))
    assert stu == "front"
    assert cell.elite().uid == sk(n=4, **base).id
    assert len(cell.tiers[1]) == 1 and cell.tiers[1][0].tier == 1
    # infeasible: dropped once the cell has feasible entries
    assert arch.add(result(sk(n=6, **base), dps=1e9, ehp=1e9, feasible=False)) == "infeasible_dropped"
    # duplicate skeleton id
    assert arch.add(result(sk(n=4, **base))) == "duplicate"
    # failure is recorded but makes no cell
    assert arch.add(result(sk(n=7, **base), ok=False)) == "failed"
    assert arch.n == 7 and len(arch.cells) == 1


def test_infeasible_placeholder_replaced_by_feasible(fake_corpus):
    arch = Archive()
    assert arch.add(result(sk(n=1), feasible=False)) == "new_cell_infeasible"
    c = next(iter(arch.cells.values()))
    assert c.best_tier() == 2
    assert arch.add(result(sk(n=2), dps=10, ehp=10, verified=False)) == "improved"
    assert c.best_tier() == 1 and not c.tiers[2]
    assert arch.add(result(sk(n=3), dps=5, ehp=5)) == "improved"     # verified beats unverified, any DPS
    assert c.elite().tier == 0


def test_front_size_capped_and_keeps_extremes(fake_corpus):
    arch = Archive()
    for i in range(20):        # a pure trade-off line: every point is non-dominated
        arch.add(result(sk(n=i), dps=1000.0 * (i + 1), ehp=1000.0 * (20 - i)))
    c = next(iter(arch.cells.values()))
    assert len(c.tiers[0]) == A.MAX_FRONT[0]
    dps = [e.dps_honest for e in c.tiers[0]]
    ehp = [e.ehp for e in c.tiers[0]]
    assert max(dps) == 20000 and max(ehp) == 20000


# ---------------------------------------------------------------------------------------------- persistence
def test_persistence_resume_and_checkpoint(tmp_path, fake_corpus):
    d = str(tmp_path / "run")
    arch = Archive.open(d)
    arch.add(result(sk(n=1), dps=1e5, ehp=2e4))
    arch.add(result(sk("Comet", "Infernalist", n=2), dps=3e5, ehp=1e4,
                    hit={"Cold": 100}))
    arch.add(result(sk(n=3), ok=False))
    arch.add(result(sk(n=4), dps=1e6, ehp=1e5, verified=False))
    arch.checkpoint({"runner": {"bandit": {"succ": {"fresh": 2.0}}}})
    st1 = arch.stats()
    arch.close()
    # torn last line (crash mid-write) is ignored
    with open(os.path.join(d, "archive.jsonl"), "a") as f:
        f.write('{"uid": "trunc')
    idx = json.load(open(os.path.join(d, "index.json")))
    assert idx["n_records"] == 4 and len(idx["cells"]) == 2
    arch2 = Archive.open(d)
    assert arch2.n == 4
    assert arch2.seen == arch.seen
    st2 = arch2.stats()
    for k in ("cells", "cells_feasible", "cells_verified", "qd_score"):
        assert st1[k] == st2[k]
    assert arch2.meta["runner"]["bandit"]["succ"]["fresh"] == 2.0
    assert arch2.add(result(sk(n=1))) == "duplicate"
    # redescribe: a stale stored descriptor is recomputed on load
    lines = open(os.path.join(d, "archive.jsonl")).read().splitlines()[:4]
    recs = [json.loads(x) for x in lines]
    recs[0]["descriptor"]["damage"] = "stale"
    recs[0]["cell"] = "stale|cell"
    with open(os.path.join(d, "archive.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    assert "stale|cell" in Archive.open(d).cells
    assert "stale|cell" not in Archive.open(d, redescribe=True).cells


def test_quarantine_zero_damage(fake_corpus):
    arch = Archive()
    # a "feasible" build with no honest damage (a herald as main skill) is held in tier 2
    assert arch.add(result(sk(n=1), dps=0.0, ehp=9e4)) == "new_cell_infeasible"
    c = next(iter(arch.cells.values()))
    assert c.best_tier() == 2 and c.elite().quarantine == "no_damage"
    st = arch.stats()
    assert st["cells_feasible"] == 0 and st["qd_score"] == 0 and st["quarantined"] == 1
    # any build that does damage takes the cell
    assert arch.add(result(sk(n=2), dps=50.0, ehp=1e3)) == "improved"
    assert c.elite().tier == 0 and not c.tiers[2]
    assert arch.pick_parent(random.Random(0)).uid == sk(n=2).id


def test_axes_presets_and_rekey_on_resume(tmp_path, fake_corpus):
    assert A.resolve_axes(None) == A.COARSE_AXES and A.resolve_axes("fine") == A.FINE_AXES
    assert A.resolve_axes("damage_family,class") == ("damage_family", "class")
    with pytest.raises(ValueError):
        A.resolve_axes("damage,bogus")
    d = str(tmp_path / "run")
    arch = Archive.open(d, axes="fine")
    # two builds of one archetype that differ only in their unique: separate fine cells, one coarse cell
    arch.add(result(sk(n=1, uniques=[{"name": "Headhunter"}]), dps=1e5, ehp=2e4))
    arch.add(result(sk(n=2, uniques=[{"name": "Atziri's Acuity"}]), dps=2e5, ehp=2e4))
    assert len(arch.cells) == 2
    arch.checkpoint()
    arch.close()
    assert len(Archive.open(d).cells) == 2               # resume keeps the run's own axes (index.json)
    coarse = Archive.open(d, axes="coarse")
    assert len(coarse.cells) == 1
    cell = next(iter(coarse.cells.values()))
    assert cell.elite().uid == sk(n=2, uniques=[{"name": "Atziri's Acuity"}]).id and cell.desc == {"damage_family": "lightning", "activation": "manual",
                                                            "pool": "life", "class": "Sorceress"}
    # records written before the coarse axes existed (no "axes", no derived fields) are re-keyed
    lines = open(os.path.join(d, "archive.jsonl")).read().splitlines()
    recs = [json.loads(x) for x in lines]
    for r in recs:
        r.pop("axes")
        for k in ("damage_family", "pool", "class"):
            r["descriptor"].pop(k)
    d2 = str(tmp_path / "old")
    os.makedirs(d2)
    with open(os.path.join(d2, "archive.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    assert len(Archive.open(d2, axes="fine").cells) == 2
    assert len(Archive.open(d2).cells) == 1


def test_stats_and_feedback(fake_corpus):
    arch = Archive()
    arch.add(result(sk(n=1, source="corpus_prior"), dps=1e5, ehp=2e4))
    arch.add(result(sk(n=2, source="corpus_prior"), dps=1e4, ehp=1e4))           # dominated
    arch.add(result(sk("Comet", "Infernalist", n=3, source="anti_meta"), dps=2e5, ehp=3e4, hit={"Cold": 1}))
    arch.add(result(sk(n=4, source="anti_meta"), ok=False))
    st = arch.stats()
    assert st["cells_feasible"] == 2 and st["cells_verified"] == 2
    assert st["qd_score"] > 0
    assert st["sources"]["corpus_prior"]["success_rate"] == 0.5
    assert st["sources"]["anti_meta"]["success_rate"] == 0.5
    fb = arch.feedback()
    assert set(fb) >= {"cell_counts", "source_success", "elite_skeletons", "axis_counts", "elites", "seen_ids",
                       "skill_counts", "unique_counts", "elite_info"}
    assert all(isinstance(s, Skeleton) for s in fb["elites"])
    assert all(isinstance(s, Skeleton) for s in fb["elite_skeletons"])
    assert fb["elite_skeletons"][0].main_skill == "Comet"          # best objective first
    assert fb["axis_counts"]["ascendancy"] == {"Stormweaver": 1, "Infernalist": 1}
    assert sum(fb["cell_counts"].values()) == 3
    p = arch.pick_parent(random.Random(0))
    assert p is not None and p.tier == 0


# ---------------------------------------------------------------------------------------------- report
def test_report(tmp_path, fake_corpus):
    d = str(tmp_path / "run")
    arch = Archive.open(d)
    arch.add(result(sk(n=1, uniques=[{"name": "Headhunter"}]), dps=1e5, ehp=2e4))
    arch.add(result(sk("Comet", "Titan", n=2), dps=3e5, ehp=1e4, hit={"Cold": 100}))     # unseen -> surprising
    arch.add(result(sk(n=3), dps=1e6, ehp=1e5, verified=False))
    arch.close()
    out = write_report(d)
    md = open(out["md"]).read()
    assert "Top builds overall" in md and "Most surprising" in md and "Per ascendancy" in md
    assert "Headhunter" in md and "CODE-" in md and "UNVERIFIED" in md
    sur = md.split("## Most surprising")[1].split("## Per constraint")[0]
    assert "Comet" in sur and "Spark" not in sur
    assert "`one_button`" in md
    rows = open(out["csv"]).read().splitlines()
    assert len(rows) == 4 and rows[0].startswith("uid,")


# ---------------------------------------------------------------------------------------------- runner
class FakeGenerator:
    """Pool of skeletons over a few skills/ascendancies; mutate() perturbs the budget tier and a support."""

    def __init__(self):
        self.calls = {"generate": 0, "mutate": 0}
        self.feedback_seen = []
        self.lock = threading.Lock()

    def generate(self, n, constraints=None, feedback=None, seed=None):
        with self.lock:
            self.calls["generate"] += 1
            self.feedback_seen.append(feedback)
        rng = random.Random(seed)
        return [sk(rng.choice(["Spark", "Comet", "Arc"]), rng.choice(["Stormweaver", "Infernalist", "Titan"]),
                   source="fake_fresh", n=rng.randrange(40)) for _ in range(n)]

    def mutate(self, parent, n=1, seed=None):
        with self.lock:
            self.calls["mutate"] += 1
        rng = random.Random(seed)
        return [sk(parent.main_skill, parent.ascendancy, source="mutation", n=rng.randrange(10 ** 6),
                   parent=parent.id) for _ in range(n)]


def fake_synth(skeleton, engine, scorer, budget=6000):
    rng = random.Random(skeleton.id)
    if skeleton.main_skill == "Arc" and rng.random() < 0.3:
        raise RuntimeError("synth exploded")
    dps = rng.uniform(1e4, 1e6)
    return result(skeleton, dps=dps, ehp=rng.uniform(5e3, 5e4), verified=rng.random() > 0.2,
                  hit={"Lightning" if skeleton.main_skill != "Comet" else "Cold": 1.0})


def test_bandit_prefers_better_arm():
    b = Bandit()
    rng = random.Random(1)
    for _ in range(60):
        b.update("mutate", True)
        b.update("fresh", False)
    picks = [b.choose(rng) for _ in range(200)]
    assert picks.count("mutate") > 150
    assert b.choose(rng, ["fresh"]) == "fresh"


def test_runner_with_fake_synth(tmp_path, fake_corpus):
    d = str(tmp_path / "run")
    gen = FakeGenerator()
    r = Runner(d, generator=gen, synthesize=fake_synth, constraints=["one_button"], workers=2, minutes=1,
               max_jobs=40, engine_factory=lambda: None, scorer_factory=lambda: None, checkpoint_every=0.5,
               report_every=1.0)
    out = r.run()
    assert out["jobs"] >= 30
    assert out["stop_reason"].startswith("job budget")
    assert out["failed"] >= 1                         # Arc explosions were isolated, not fatal
    assert out["archive"]["cells_feasible"] >= 2
    assert gen.calls["generate"] >= 1
    assert gen.feedback_seen[0] is not None and "cell_counts" in gen.feedback_seen[0]
    for f in ("archive.jsonl", "index.json", "progress.json", "report.md", "builds.csv", "run.log"):
        assert os.path.exists(os.path.join(d, f)), f
    lines = open(os.path.join(d, "archive.jsonl")).read().splitlines()
    ids = [json.loads(x)["uid"] for x in lines]
    assert len(ids) == len(set(ids)) == out["jobs"]   # deduped by skeleton id
    # resume: second session continues the same archive and keeps the bandit
    r2 = Runner(d, generator=gen, synthesize=fake_synth, workers=2, minutes=1, max_jobs=10,
                engine_factory=lambda: None, scorer_factory=lambda: None)
    out2 = r2.run()
    assert r2.session == 2
    assert r2.archive.n == out["jobs"] + out2["jobs"]
    assert sum(r2.bandit.pulls.values()) >= sum(r.bandit.pulls.values())


def test_runner_mutates_elites(tmp_path, fake_corpus):
    gen = FakeGenerator()
    r = Runner(str(tmp_path / "run"), generator=gen, synthesize=fake_synth, workers=2, minutes=1, max_jobs=80,
               engine_factory=lambda: None, scorer_factory=lambda: None, report=False, batch=2)
    r.run()
    assert gen.calls["mutate"] >= 1
    assert any((rec["skeleton"].get("source") == "mutation") for rec in r.archive.records.values())


class FakeEngine:
    instances = []

    def __init__(self):
        self.dead = False
        self.closed = False
        FakeEngine.instances.append(self)

    def alive(self):
        return not self.dead

    def kill(self):
        self.dead = True

    def close(self):
        self.closed = True

    def request(self, *a, **k):
        if self.dead:
            raise RuntimeError("dead")
        return {}


def test_runner_timeout_crash_isolation_and_stop_file(tmp_path, fake_corpus):
    FakeEngine.instances = []
    d = str(tmp_path / "run")

    def synth(skeleton, engine, scorer, budget=6000):
        if skeleton.main_skill == "Comet":           # hangs until its engine is killed
            for _ in range(200):
                time.sleep(0.02)
                engine.request()
            return result(skeleton)
        if skeleton.main_skill == "Arc":             # crashes the engine process
            engine.dead = True
            raise RuntimeError("engine crashed")
        return result(skeleton)

    gen = FakeGenerator()
    r = Runner(d, generator=gen, synthesize=synth, workers=2, minutes=1, max_jobs=12, job_timeout=0.3,
               engine_factory=FakeEngine, scorer_factory=lambda: None, report=False)
    out = r.run()
    assert out["jobs"] >= 6
    assert out["timeouts"] >= 1 and out["engine_restarts"] >= 1
    recs = list(r.archive.records.values())
    assert any("timeout" in (x.get("error") or "") for x in recs)
    assert any(x.get("status") in ("new_cell", "improved", "front", "dominated") for x in recs)
    # STOP file ends a run early
    with open(os.path.join(d, "STOP"), "w"):
        pass
    r2 = Runner(d, generator=gen, synthesize=fake_synth, workers=1, minutes=5, engine_factory=lambda: None,
                scorer_factory=lambda: None, report=False)
    # run() removes a stale STOP at start; create it again shortly after start
    threading.Timer(0.5, lambda: open(os.path.join(d, "STOP"), "w").close()).start()
    t = time.time()
    out2 = r2.run()
    assert out2["stop_reason"] == "STOP file" and time.time() - t < 30


def test_run_dir_lock(tmp_path, fake_corpus):
    d = str(tmp_path / "run")
    r1 = Runner(d, generator=FakeGenerator(), synthesize=fake_synth, engine_factory=lambda: None,
                scorer_factory=lambda: None)
    r1._acquire()
    try:
        r2 = Runner(d, generator=FakeGenerator(), synthesize=fake_synth, engine_factory=lambda: None,
                    scorer_factory=lambda: None)
        with pytest.raises(RunDirLocked):
            r2._acquire()
    finally:
        r1._release()


def test_cli_build_with_stubs(tmp_path, fake_corpus, monkeypatch, capsys):
    """`build` and `build-report` subcommands wire through to Runner/report (stub generator + synth)."""
    import exilegen.builder.runner as R
    from exilegen import cli
    orig = R.Runner.__init__

    def init(self, run_dir, **kw):
        kw.update(generator=FakeGenerator(), synthesize=fake_synth, engine_factory=lambda: None,
                  scorer_factory=lambda: None, max_jobs=8)
        orig(self, run_dir, **kw)
    monkeypatch.setattr(R.Runner, "__init__", init)
    d = str(tmp_path / "cli")
    assert cli.main(["build", "--run-dir", d, "--constraint", "one_button", "--workers", "2", "--minutes", "1"]) == 0
    assert "jobs" in capsys.readouterr().out
    assert cli.main(["build-report", "--run-dir", d, "--out-dir", str(tmp_path / "rep")]) == 0
    assert os.path.exists(tmp_path / "rep" / "report.md")


def test_runner_successive_halving(tmp_path, fake_corpus):
    calls = []

    def synth(skeleton, engine, scorer, budget=6000):
        calls.append((skeleton.id, budget))
        r = fake_synth(skeleton, engine, scorer, budget)
        r.evals = budget
        return r
    r = Runner(str(tmp_path / "run"), generator=FakeGenerator(), synthesize=synth, workers=2, minutes=1,
               max_jobs=30, engine_factory=lambda: None, scorer_factory=lambda: None, report=False,
               budget=4000, halving=True, rung0_frac=0.25, eta=3)
    out = r.run()
    cheap = [i for i, b in calls if b == 1000]
    full = [i for i, b in calls if b == 4000]
    assert cheap and full
    assert set(full) <= set(cheap)                       # only skeletons that had a cheap run go full
    assert len(full) < len(cheap) * 0.7                  # most are not promoted
    assert r.counts["promoted"] == len(full) and r.counts["rung0"] == len(cheap)
    recs = [json.loads(x) for x in open(os.path.join(str(tmp_path / "run"), "archive.jsonl"))]
    assert len({x["uid"] for x in recs}) == len(recs)     # one record per skeleton
    full_recs = [x for x in recs if (x.get("job") or {}).get("rung") == 1 and x.get("ok")]
    assert all(x["evals"] == 5000 for x in full_recs)    # cheap + full cost


def test_wrong_main_ranked_unverified(fake_corpus):
    from exilegen.builder.contracts import main_skill_matches as m
    assert not m({"DetonateDeadPlayer"}, {"id": "EmberFusilladePlayer", "name": "Ember Fusillade"})
    assert m({"CrossbowRequiemAmmoPlayer"}, {"id": "CrossbowRequiemPlayer", "name": "Requiem"})
    assert not m({"ArcPlayer"}, {"id": "ArcticArmourPlayer"}) and m({"ArcPlayer"}, None)
    arch = Archive()
    honest = result(sk("Spark", n=1), dps=1e5, ehp=2e4)
    swapped = result(sk("Spark", n=2), dps=1e6, ehp=2e4)
    swapped.stats["pinnacle_v1"]["mainSkill"] = {"name": "Arc", "id": "ArcPlayer"}
    arch.add(honest)
    assert arch.add(swapped) == "front"
    c = next(iter(arch.cells.values()))
    assert c.elite().uid == sk("Spark", n=1).id
    assert c.tiers[1][0].quarantine == "wrong_main" and arch.stats()["wrong_main"] == 1
