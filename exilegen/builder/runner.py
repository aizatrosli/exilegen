"""Continuous MAP-Elites search loop: generator -> job queue -> N engine workers -> synth -> archive.

    from exilegen.builder.runner import Runner
    Runner("runs/ob1", constraints=["one_button"], workers=3, minutes=60).run()

    python3 -m exilegen build --constraint one_button --workers 3 --minutes 60 --run-dir runs/ob1

* Workers: one persistent engine each (an exilegen.engine.Engine driven from its own thread, like
  EnginePool), default min(3, cores - 1). A job = synthesize(skeleton, engine, scorer, budget) ->
  Archive.add. Each worker owns its Scorer (scorers keep per-call caches).
* Job queue: skeletons from the generator, either fresh (generate with archive feedback) or mutations
  of archive elites (parent picked by Archive.pick_parent). The fresh/mutate ratio is a two-armed
  Thompson-sampling bandit whose reward is "the job made a new cell or improved a cell elite".
* Dedupe by skeleton id (archive + in flight). Per-job timeout: the engine is killed, the job is
  recorded as failed, the engine restarted. Any exception in synth is recorded as a failed job; a dead
  engine is restarted (crash isolation).
* Budget: --minutes (wall clock of this session), --max-evals (sum of BuildResult.evals over the run),
  --max-jobs. Graceful stop: SIGINT/SIGTERM or a file named STOP in the run directory: no new jobs,
  in-flight jobs finish (up to the job timeout), then checkpoint + report. A second SIGINT abandons
  in-flight jobs.
* Periodic checkpoint (index.json, progress.json) and report (report.md, builds.csv).
* Resumable: re-running on the same run directory replays archive.jsonl and keeps the bandit state.
  A run directory is locked (flock) so two runs can never share one; run them in separate directories.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .archive import Archive
from .archive import cell_key as A_cell_key
from .contracts import BuildResult, Skeleton

try:
    import fcntl
except ImportError:  # pragma: no cover - non-posix
    fcntl = None

IMPROVED = ("new_cell", "improved")


def default_workers() -> int:
    return max(1, min(3, (os.cpu_count() or 2) - 1))


def make_scorer(constraints=(), profiles=None):
    """The optimiser's scorer: locked profiles, default floors (mana sustain in `either` mode) and the
    energy-aware cost filter, exactly like optimise.pipeline._context."""
    try:   # the synthesizer's own factory, so its floor choices hold (absolute floors, no baseline)
        from .synth import make_scorer as synth_scorer
        return synth_scorer(None, profiles=profiles, constraints=list(constraints or []))
    except ImportError:
        pass
    from exilegen.optimise.pipeline import DEFAULT_PROFILES, FilteredScorer, default_floors
    sc = FilteredScorer(profile=list(profiles or DEFAULT_PROFILES), constraints=list(constraints or []),
                        floors=default_floors())
    try:
        from exilegen.optimise.triggers import energy_cost_filter
        sc.stats_filter = energy_cost_filter(sc)
    except ImportError:
        pass
    return sc


def default_engine_factory():
    from exilegen.engine import Engine
    return Engine()


# ---------------------------------------------------------------------------------------------- bandit
@dataclass
class Bandit:
    """Two-armed Thompson sampling on a Beta posterior with exponential forgetting."""
    arms: tuple = ("fresh", "mutate")
    decay: float = 0.98
    succ: dict = field(default_factory=dict)
    fail: dict = field(default_factory=dict)
    pulls: dict = field(default_factory=dict)

    def __post_init__(self):
        for a in self.arms:
            self.succ.setdefault(a, 0.0)
            self.fail.setdefault(a, 0.0)
            self.pulls.setdefault(a, 0)

    def choose(self, rng: random.Random, allowed=None) -> str:
        allowed = [a for a in self.arms if allowed is None or a in allowed]
        if len(allowed) == 1:
            return allowed[0]
        draws = {a: rng.betavariate(1 + self.succ[a], 1 + self.fail[a]) for a in allowed}
        return max(draws, key=draws.get)

    def update(self, arm: str, success: bool) -> None:
        if arm not in self.succ:
            return
        for a in self.arms:
            self.succ[a] *= self.decay
            self.fail[a] *= self.decay
        self.pulls[arm] += 1
        if success:
            self.succ[arm] += 1
        else:
            self.fail[arm] += 1

    def rate(self, arm) -> float:
        return (1 + self.succ[arm]) / (2 + self.succ[arm] + self.fail[arm])

    def state(self) -> dict:
        return {"succ": self.succ, "fail": self.fail, "pulls": self.pulls}

    def load(self, st: dict | None) -> None:
        for k in ("succ", "fail", "pulls"):
            for a, v in ((st or {}).get(k) or {}).items():
                if a in self.arms:
                    getattr(self, k)[a] = v


# ---------------------------------------------------------------------------------------------- runner
class RunDirLocked(RuntimeError):
    pass


class Runner:
    def __init__(self, run_dir: str, generator: Any = None, synthesize: Callable | None = None,
                 constraints=(), profiles=None, workers: int | None = None, minutes: float | None = 60,
                 max_evals: int | None = None, max_jobs: int | None = None, budget: int = 6000,
                 job_timeout: float = 600.0, engine_factory: Callable | None = None,
                 scorer_factory: Callable | None = None, seed: int = 0, batch: int | None = None,
                 checkpoint_every: float = 60.0, report_every: float = 300.0, report: bool = True,
                 log: Callable | None = None, generator_kwargs: dict | None = None,
                 halving: bool = False, rung0_frac: float = 0.25, eta: int = 3, warm_start: bool = True):
        self.run_dir = run_dir
        os.makedirs(run_dir, exist_ok=True)
        self.constraints = list(constraints or [])
        self.profiles = list(profiles) if profiles else None
        self.workers = workers or default_workers()
        self.minutes, self.max_evals, self.max_jobs = minutes, max_evals, max_jobs
        self.budget, self.job_timeout = budget, job_timeout
        # Successive halving (asynchronous, ASHA-style): every skeleton first gets a cheap synth at
        # rung0_frac of the budget; it is promoted to a full-budget synth only if it ranks in the top
        # 1/eta of the recent cheap runs. Builds are ranked after tree, gear and supports have adapted,
        # which a pre-screen score cannot do, and the full budget goes where it pays.
        # Off by default. In a 25-minute A/B (2 workers, seed 3) it found a stronger best build (391k vs
        # 152k honest DPS) but covered far less: 27 vs 42 feasible cells, QD score 301 vs 501. A cheap
        # pass still took ~60% of a full synth's wall time (gear optimisation and trials do not scale
        # with the eval budget), and only 30% of cheap builds were feasible when archived unpromoted.
        self.halving, self.rung0_frac, self.eta = halving, rung0_frac, max(2, int(eta))
        self.warm_start = warm_start           # mutations start from their parent's passive tree
        self._rung0: list[tuple] = []          # recent cheap-run keys (feasible, objective)
        self._promo: Any = None                # deque of promoted jobs (created in _main)
        self.engine_factory = engine_factory if engine_factory is not None else default_engine_factory
        self.scorer_factory = scorer_factory or (lambda: make_scorer(self.constraints, self.profiles))
        self.seed = seed
        self.batch = batch or max(2, self.workers)
        self.checkpoint_every, self.report_every, self.do_report = checkpoint_every, report_every, report
        self._generator, self._generator_kwargs = generator, generator_kwargs or {}
        self._synth = synthesize
        self._log_fn = log
        self.rng = random.Random(seed)
        self.bandit = Bandit()
        self.archive: Archive | None = None
        self.stop_reason: str | None = None
        self._stop = threading.Event()
        self._abort = threading.Event()
        self.inflight: set[str] = set()
        self._engines: dict[int, Any] = {}
        self.counts = {"jobs": 0, "failed": 0, "timeouts": 0, "engine_restarts": 0, "duplicates_skipped": 0,
                       "generated": 0, "evals": 0, "status": {}, "rung0": 0, "promoted": 0}
        self._lockf = None
        self._logf = None
        self.t0 = time.time()
        self.prev_seconds = 0.0
        self.session = 0

    # -------------------------------------------------------------- plumbing
    def log(self, *a) -> None:
        msg = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
        if self._logf:
            self._logf.write(msg + "\n")
            self._logf.flush()
        if self._log_fn:
            self._log_fn(msg)

    @property
    def generator(self):
        if self._generator is None:
            from . import skeletons
            gen_cls = getattr(skeletons, "Generator")
            try:
                self._generator = gen_cls(**self._generator_kwargs)
            except TypeError:
                self._generator = gen_cls()
        return self._generator

    @property
    def synthesize(self):
        if self._synth is None:
            from .synth import synthesize
            self._synth = synthesize
        return self._synth

    def _acquire(self) -> None:
        self._lockf = open(os.path.join(self.run_dir, ".lock"), "a+")
        if fcntl is not None:
            try:
                fcntl.flock(self._lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._lockf.close()
                self._lockf = None
                raise RunDirLocked(f"run directory {self.run_dir} is in use by another run")
        self._lockf.seek(0)
        self._lockf.truncate()
        self._lockf.write(str(os.getpid()))
        self._lockf.flush()

    def _release(self) -> None:
        if self._lockf:
            try:
                if fcntl is not None:
                    fcntl.flock(self._lockf.fileno(), fcntl.LOCK_UN)
            finally:
                self._lockf.close()
                self._lockf = None

    def request_stop(self, reason: str = "stop requested") -> None:
        if not self._stop.is_set():
            self.stop_reason = reason
            self.log(f"[runner] stopping: {reason}")
        self._stop.set()

    def elapsed(self) -> float:
        return time.time() - self.t0

    def _check_budget(self) -> None:
        if self._stop.is_set():
            return
        if self.minutes is not None and self.elapsed() >= self.minutes * 60:
            self.request_stop(f"time budget {self.minutes} min")
        elif self.max_evals is not None and self.counts["evals"] >= self.max_evals:
            self.request_stop(f"eval budget {self.max_evals}")
        elif self.max_jobs is not None and self.counts["jobs"] >= self.max_jobs:
            self.request_stop(f"job budget {self.max_jobs}")
        elif os.path.exists(os.path.join(self.run_dir, "STOP")):
            self.request_stop("STOP file")

    def state(self) -> dict:
        return {"bandit": self.bandit.state(), "counts": self.counts, "constraints": self.constraints,
                "profiles": self.profiles, "workers": self.workers, "budget": self.budget,
                "seconds_total": self.prev_seconds + self.elapsed(), "session": self.session,
                "stop_reason": self.stop_reason, "seed": self.seed,
                "halving": {"on": self.halving, "rung0_frac": self.rung0_frac, "eta": self.eta}}

    def checkpoint(self, final: bool = False) -> None:
        st = self.state()
        self.archive.checkpoint({"runner": st})
        a = self.archive.stats()
        hrs = max(1e-9, self.elapsed() / 3600)
        prog = {"t": time.time(), "elapsed_s": round(self.elapsed(), 1), "final": final,
                "jobs": self.counts["jobs"], "jobs_per_hour": self.counts["jobs"] / hrs,
                "ok_per_hour": sum(v for k, v in self.counts["status"].items() if k != "failed") / hrs,
                "evals": self.counts["evals"], "cells_feasible": a["cells_feasible"],
                "cells_verified": a["cells_verified"], "qd_score": a["qd_score"], "best": a["best"],
                "bandit": {k: round(self.bandit.rate(k), 3) for k in self.bandit.arms},
                "counts": self.counts,
                "skeleton_rejects": dict(getattr(self.generator, "reject_counts", None) or {})}
        tmp = os.path.join(self.run_dir, "progress.json.tmp")
        with open(tmp, "w") as f:
            json.dump(prog, f, indent=1, default=str)
        os.replace(tmp, os.path.join(self.run_dir, "progress.json"))

    def write_report(self) -> None:
        if not self.do_report:
            return
        try:
            from .report import write_report
            write_report(self.run_dir, archive=self.archive, extra={"runner": self.state()})
        except Exception as ex:  # noqa: BLE001 - a report bug must not kill the search
            self.log(f"[runner] report failed: {type(ex).__name__}: {ex}")

    # -------------------------------------------------------------- jobs
    def _propose(self, batch_no: int) -> tuple[str, list[Skeleton], str | None]:
        """One batch of new skeletons (runs in a thread). -> (arm, skeletons, parent id)."""
        allowed = ["fresh"] + (["mutate"] if self.archive.elites() else [])
        arm = self.bandit.choose(self.rng, allowed)
        seed = self.seed * 100003 + batch_no
        parent_id = None
        if arm == "mutate":
            parent = self.archive.pick_parent(self.rng)
            if parent is None:
                arm = "fresh"
            else:
                parent_id = parent.uid
                sk = Skeleton(**parent.skeleton)
                try:
                    out = self.generator.mutate(sk, n=self.batch, seed=seed, constraints=self.constraints)
                except TypeError:
                    out = self.generator.mutate(sk, n=self.batch, seed=seed)
                return arm, list(out or []), parent_id
        fb = self.archive.feedback(skeleton_cell_key=self._skeleton_cell_key())
        out = self.generator.generate(self.batch, constraints=self.constraints, feedback=fb, seed=seed)
        return arm, list(out or []), parent_id

    def _parent_code(self, parent_id: str, child: Skeleton) -> str | None:
        """The archived build code of a mutation's parent, when it is a built build of the child's
        class (its passive tree then seeds the child's tree: a warm start instead of from scratch)."""
        rec = self.archive.records.get(parent_id) if self.archive else None
        if not rec or not rec.get("ok") or not rec.get("code"):
            return None
        if (rec.get("skeleton") or {}).get("cls") != child.cls:
            return None
        return rec["code"]

    def _skeleton_cell_key(self):
        """The generator module's own coarse cell function (skeletons.cell_key), if it has one."""
        mod = sys.modules.get(type(self.generator).__module__)
        fn = getattr(mod, "cell_key", None)
        return fn if callable(fn) and fn is not A_cell_key else None

    def _run_job(self, wid: int, engine: Any, scorer: Any, job: dict) -> BuildResult:
        sk: Skeleton = job["skeleton"]
        t = time.time()
        try:
            budget = int(job.get("budget") or self.budget)
            seed_code = job.get("seed_code")
            if seed_code:
                try:
                    r = self.synthesize(sk, engine, scorer, budget, seed_code=seed_code)
                except TypeError:            # a synthesize without warm starts
                    r = self.synthesize(sk, engine, scorer, budget)
            else:
                r = self.synthesize(sk, engine, scorer, budget)
            if not isinstance(r, BuildResult):
                raise TypeError(f"synthesize returned {type(r).__name__}, not BuildResult")
        except Exception as ex:  # noqa: BLE001 - crash isolation
            r = BuildResult(skeleton=sk, ok=False, error=f"{type(ex).__name__}: {ex}",
                            notes=[traceback.format_exc()[-2000:]])
        if not r.seconds:
            r.seconds = time.time() - t
        if r.skeleton is not sk and getattr(r.skeleton, "id", None) != sk.id:
            r.notes = list(r.notes or []) + [f"runner: synth returned skeleton {getattr(r.skeleton, 'id', None)}"]
            r.skeleton = sk
        return r

    @staticmethod
    def _engine_alive(engine) -> bool:
        if engine is None:
            return True
        alive = getattr(engine, "alive", None)
        return bool(alive()) if callable(alive) else True

    @staticmethod
    def _condemn(engine) -> None:
        """Kill an engine whose job overran and make sure the abandoned synth thread cannot restart it
        (Engine.request auto-restarts a dead process): the thread's next engine call raises."""
        if engine is None:
            return

        def _dead(*a, **k):
            raise RuntimeError("engine abandoned after a job timeout")
        try:
            engine.start = _dead
            engine.request = _dead
        except Exception:  # noqa: BLE001
            pass
        try:
            engine.kill()
        except Exception:  # noqa: BLE001
            pass

    async def _restart_engine(self, wid: int, engine, close_old: bool = True):
        self.counts["engine_restarts"] += 1
        if engine is not None and close_old:
            try:
                await asyncio.to_thread(engine.close)
            except Exception:  # noqa: BLE001
                try:
                    engine.kill()
                except Exception:  # noqa: BLE001
                    pass
        for attempt in range(3):
            try:
                return await asyncio.to_thread(self.engine_factory)
            except Exception as ex:  # noqa: BLE001
                self.log(f"[w{wid}] engine restart failed ({attempt + 1}/3): {ex}")
                await asyncio.sleep(2 + 3 * attempt)
        raise RuntimeError(f"worker {wid}: engine could not be restarted")

    async def _worker(self, wid: int, q: asyncio.Queue) -> None:
        engine = await asyncio.to_thread(self.engine_factory)
        self._engines[wid] = engine
        scorer = await asyncio.to_thread(self.scorer_factory)
        self.log(f"[w{wid}] ready")
        njobs = 0
        try:
            while True:
                if self._stop.is_set() and (q.empty() or self._abort.is_set()):
                    return
                if self._promo:
                    job = self._promo.popleft()          # promoted skeletons run before new ones
                else:
                    try:
                        job = await asyncio.wait_for(q.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        if self._producer_done and q.empty() and not self._promo:
                            return
                        continue
                if self._stop.is_set():
                    # stopping: queued (not started) jobs are dropped, not run
                    self.inflight.discard(job["skeleton"].id)
                    continue
                sk = job["skeleton"]
                fut = asyncio.ensure_future(asyncio.to_thread(self._run_job, wid, engine, scorer, job))
                try:
                    res = await asyncio.wait_for(asyncio.shield(fut), timeout=self.job_timeout)
                except asyncio.TimeoutError:
                    self.counts["timeouts"] += 1
                    self.log(f"[w{wid}] job {sk.id} timed out after {self.job_timeout:.0f}s; killing engine")
                    self._condemn(engine)
                    try:
                        await asyncio.wait_for(asyncio.shield(fut), timeout=30)
                    except Exception:  # noqa: BLE001
                        pass
                    res = BuildResult(skeleton=sk, ok=False, error=f"timeout after {self.job_timeout:.0f}s",
                                      seconds=self.job_timeout)
                    # the abandoned thread's result (if it ever ends) is dropped
                    engine = await self._restart_engine(wid, engine, close_old=False)
                self._engines[wid] = engine
                self._finish(wid, job, res)
                njobs += 1
                if not self._engine_alive(engine):
                    self.log(f"[w{wid}] engine dead after job {sk.id}; restarting")
                    engine = await self._restart_engine(wid, engine)
                    self._engines[wid] = engine
                elif engine is not None and njobs % 25 == 0 and hasattr(engine, "gc"):
                    try:
                        await asyncio.to_thread(engine.gc)
                    except Exception:  # noqa: BLE001
                        pass
                self._check_budget()
        finally:
            if engine is not None:
                try:
                    await asyncio.to_thread(engine.close)
                except Exception:  # noqa: BLE001
                    pass

    RUNG0_WINDOW = 60          # recent cheap runs a promotion is ranked against

    @staticmethod
    def _rank_key(res: BuildResult) -> tuple:
        sc = res.score or {}
        if not res.ok or sc.get("objective") is None:
            return (0, -1e18)
        return (1 if sc.get("feasible") else 0, float(sc["objective"]))

    def _promote(self, res: BuildResult) -> bool:
        """Rank a cheap (rung 0) result against the recent cheap runs: promote the top 1/eta. The first
        eta results are promoted when they built at all, so the bar has something to stand on."""
        key = self._rank_key(res)
        hist = self._rung0[-self.RUNG0_WINDOW:]
        self._rung0.append(key)
        if key[1] <= -1e17:
            return False
        if len(hist) < self.eta:
            return True
        better = sum(1 for k in hist if k > key)
        return better < (len(hist) + 1) / self.eta

    def _finish(self, wid: int, job: dict, res: BuildResult) -> None:
        sk = job["skeleton"]
        if self.halving and job.get("rung", 1) == 0:
            self.counts["rung0"] += 1
            self.counts["evals"] += int(res.evals or 0)
            if self._promote(res):
                self.counts["promoted"] += 1
                self._promo.append(dict(job, rung=1, budget=self.budget, rung0_evals=int(res.evals or 0),
                                        rung0_objective=(res.score or {}).get("objective")))
                sc = res.score or {}
                self.log(f"[w{wid}] promoted     {sk.id[:12]} {sk.ascendancy}/{sk.main_skill} "
                         f"dps={sc.get('dps_honest') or 0:,.0f} feas={sc.get('feasible')} {res.seconds:.0f}s")
                return
            res.notes = list(res.notes or []) + [f"halving: not promoted after a {job.get('budget')}-eval run"]
        else:
            self.counts["evals"] += int(res.evals or 0)
        if job.get("rung0_evals"):
            res.evals = int(res.evals or 0) + int(job["rung0_evals"])     # the skeleton's whole cost
        self.inflight.discard(sk.id)
        bar = self.archive.elite_quantile(0.5)
        status = self.archive.add(res, worker=wid, job={"arm": job["arm"], "parent": job.get("parent"),
                                                         "batch": job.get("batch"), "rung": job.get("rung")})
        self.counts["jobs"] += 1
        self.counts["status"][status] = self.counts["status"].get(status, 0) + 1
        if status == "failed":
            self.counts["failed"] += 1
        # bandit reward: an elite improved, or a new cell whose build reaches the median elite objective
        # (fine cells make "new cell" cheap; a weak build in a fresh niche is not progress)
        obj = (res.score or {}).get("objective")
        good = status == "improved" or (status == "new_cell" and (bar is None or (obj is not None and obj >= bar)))
        self.bandit.update(job["arm"], good)
        sc = res.score or {}
        self.log(f"[w{wid}] {status:<12} {sk.id[:12]} {sk.ascendancy}/{sk.main_skill} src={sk.source}"
                 f" arm={job['arm']} dps={sc.get('dps_honest') or 0:,.0f} ehp={sc.get('ehp') or 0:,.0f}"
                 f" feas={sc.get('feasible')} {res.seconds:.0f}s"
                 + (f" err={res.error[:120]}" if res.error else ""))

    async def _producer(self, q: asyncio.Queue) -> None:
        batch_no = self.counts.get("batches", 0)
        empty = 0
        try:
            while not self._stop.is_set():
                room = None if self.max_jobs is None else self.max_jobs - self.counts["jobs"] - len(self.inflight)
                if q.qsize() >= 2 * self.workers or (room is not None and room <= 0):
                    await asyncio.sleep(0.2)
                    continue
                batch_no += 1
                self.counts["batches"] = batch_no
                try:
                    arm, sks, parent = await asyncio.to_thread(self._propose, batch_no)
                except Exception as ex:  # noqa: BLE001
                    self.log(f"[producer] generator failed: {type(ex).__name__}: {ex}")
                    self.log(traceback.format_exc()[-1500:])
                    empty += 1
                    if empty >= 20:
                        self.request_stop("generator keeps failing")
                    await asyncio.sleep(min(10, empty))
                    continue
                new = 0
                for sk in sks:
                    if self.max_jobs is not None and self.counts["jobs"] + len(self.inflight) >= self.max_jobs:
                        break
                    if sk.id in self.archive.seen or sk.id in self.inflight:
                        self.counts["duplicates_skipped"] += 1
                        continue
                    self.inflight.add(sk.id)
                    self.counts["generated"] += 1
                    job = {"skeleton": sk, "arm": arm, "parent": parent, "batch": batch_no}
                    if self.warm_start and parent:
                        pc = self._parent_code(parent, sk)
                        if pc:
                            job["seed_code"] = pc
                    if self.halving:
                        job.update(rung=0, budget=max(300, int(self.budget * self.rung0_frac)))
                    await q.put(job)
                    new += 1
                if new == 0:
                    # nothing new: penalise the arm a little so the bandit tries the other one
                    self.bandit.update(arm, False)
                    empty += 1
                    if empty >= 50:
                        self.request_stop("generator exhausted (no new skeletons)")
                    await asyncio.sleep(min(2.0, 0.05 * empty))
                else:
                    empty = 0
        finally:
            self._producer_done = True

    async def _periodic(self) -> None:
        last_ck = last_rep = time.time()
        while not self._stop.is_set():
            await asyncio.sleep(1.0)
            self._check_budget()
            now = time.time()
            if now - last_ck >= self.checkpoint_every:
                last_ck = now
                await asyncio.to_thread(self.checkpoint)
            if self.do_report and now - last_rep >= self.report_every:
                last_rep = now
                await asyncio.to_thread(self.write_report)

    async def _main(self) -> dict:
        self._producer_done = False
        q: asyncio.Queue = asyncio.Queue()
        import collections
        self._promo = collections.deque()
        loop = asyncio.get_running_loop()
        installed = []
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self._on_signal, sig)
                    installed.append(sig)
                except (NotImplementedError, RuntimeError):
                    pass
        try:
            workers = [asyncio.ensure_future(self._worker(i, q)) for i in range(self.workers)]
            prod = asyncio.ensure_future(self._producer(q))
            per = asyncio.ensure_future(self._periodic())
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_EXCEPTION)
            for w in done:
                if w.exception() is not None:
                    self.log(f"[runner] worker died: {w.exception()!r}")
                    self.request_stop(f"worker died: {w.exception()}")
            await asyncio.gather(*workers, return_exceptions=True)
            self.request_stop(self.stop_reason or "workers finished")
            for t in (prod, per):
                t.cancel()
            await asyncio.gather(prod, per, return_exceptions=True)
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)
        return self.summary()

    def _on_signal(self, sig) -> None:
        if self._stop.is_set():
            self.log("[runner] second signal: abandoning in-flight jobs")
            self._abort.set()
            for e in list(self._engines.values()):
                self._condemn(e)
            return
        self.request_stop(f"signal {signal.Signals(sig).name}")

    def summary(self) -> dict:
        a = self.archive.stats()
        hrs = max(1e-9, self.elapsed() / 3600)
        return {"run_dir": self.run_dir, "stop_reason": self.stop_reason, "elapsed_s": self.elapsed(),
                "jobs": self.counts["jobs"], "jobs_per_hour": self.counts["jobs"] / hrs,
                "failed": self.counts["failed"], "timeouts": self.counts["timeouts"],
                "engine_restarts": self.counts["engine_restarts"], "evals": self.counts["evals"],
                "status": self.counts["status"], "archive": a,
                "bandit": {k: {"rate": self.bandit.rate(k), "pulls": self.bandit.pulls[k]} for k in self.bandit.arms}}

    # -------------------------------------------------------------- entry point
    def run(self) -> dict:
        self._acquire()
        self._logf = open(os.path.join(self.run_dir, "run.log"), "a")
        try:
            self.archive = Archive.open(self.run_dir)
            prev = (self.archive.meta or {}).get("runner") or {}
            self.bandit.load(prev.get("bandit"))
            self.prev_seconds = float(prev.get("seconds_total") or 0.0)
            self.session = int(prev.get("session") or 0) + 1
            if prev.get("counts"):
                self.counts["batches"] = int(prev["counts"].get("batches") or 0)
            try:
                os.remove(os.path.join(self.run_dir, "STOP"))
            except OSError:
                pass
            self.t0 = time.time()
            self.log(f"[runner] session {self.session} start: {self.workers} workers, constraints={self.constraints}"
                     f", minutes={self.minutes}, max_evals={self.max_evals}, resumed records={self.archive.n}")
            try:
                out = asyncio.run(self._main())
            except KeyboardInterrupt:
                self.request_stop("interrupted")
                out = self.summary()
            self.checkpoint(final=True)
            self.write_report()
            self.log(f"[runner] done: {json.dumps({k: out[k] for k in ('jobs', 'failed', 'stop_reason')}, default=str)}")
            return out
        finally:
            if self.archive:
                self.archive.close()
            if self._logf:
                self._logf.close()
                self._logf = None
            self._release()


def run(run_dir: str, **kw) -> dict:
    return Runner(run_dir, **kw).run()


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(run(sys.argv[1], minutes=float(sys.argv[2]) if len(sys.argv) > 2 else 5), indent=1, default=str))
