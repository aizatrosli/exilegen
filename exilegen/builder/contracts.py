"""Contracts for the from-scratch meta builder (outer loop).

Flow:  skeletons.generate(...)  ->  Skeleton  ->  synth.synthesize(skeleton, ...)  ->  BuildResult
       ->  archive.Archive.add(result)   (MAP-Elites cells, Pareto per cell)
       ->  archive feedback (cell stats) -> skeletons.generate(..., feedback=...)

Three modules, three owners; this file is the shared contract. Keep it stable; extend with optional
fields only.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Protocol


@dataclass
class Skeleton:
    """The discrete "idea" of a build. Everything else (tree, supports, rare gear) is solved."""
    id: str                                   # stable hash of the content fields below
    cls: str                                  # PoB class name, e.g. "Sorceress"
    ascendancy: str                           # PoB ascendancy name, e.g. "Stormweaver"
    main_skill: str                           # gem id or name of the main damaging skill
    level: int = 90
    # optional skill setup hints (synth fills/optimises the rest)
    supports: list[str] = field(default_factory=list)         # forced supports for the main skill
    trigger: dict | None = None               # e.g. {"meta": "MetaCastOnCritPlayer", "feeder": "Spark", "spells": [...]}
    extra_skills: list[str] = field(default_factory=list)     # auras/heralds/persistent/minion/utility
    weapon: str | None = None                 # weapon class/base hint, e.g. "Staff", "Bow", "Quarterstaff"
    uniques: list[dict] = field(default_factory=list)         # [{"name": "Rathpith Globe", "slot": "Weapon 2"?}]
    keystones: list[str] = field(default_factory=list)        # forced keystones
    ascendancy_nodes: list[str] = field(default_factory=list) # optional forced ascendancy notables
    defence: str | None = None                # hint: "life" | "es" | "hybrid" | "ci" | "mom" | "armour" | "evasion"
    budget_tier: str = "mid"                  # gear budget for template rares: "low" | "mid" | "high"
    offhand: str | None = None                # off-hand hint, e.g. "Shield" (Cast on Block), "Focus", "Quiver"
    constraints: list = field(default_factory=list)  # constraints this skeleton was generated for
    # provenance (why this skeleton was proposed) — for the human report
    source: str = ""                          # "corpus_prior" | "anti_meta" | "mechanics_graph" | "mutation" | "llm" | ...
    thesis: str = ""                          # one line: why this might be strong
    parent: str | None = None                 # skeleton id it mutated from
    tags: dict = field(default_factory=dict)  # behaviour hints (damage type, activation class...)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BuildResult:
    skeleton: Skeleton
    ok: bool                                  # synthesis produced a loadable build
    code: str | None = None                   # exported PoB share code
    score: dict | None = None                 # Scorer.score(...) result (feasible, objective, dps_honest, ehp, flags...)
    stats: Any = None                         # engine stats doc(s) of the final build (core fields)
    skills: Any = None                        # engine skills() view of the final build
    descriptor: dict = field(default_factory=dict)  # behaviour descriptor values (filled by archive.describe)
    evals: int = 0
    seconds: float = 0.0
    notes: list = field(default_factory=list) # what synth did / why it failed
    error: str | None = None
    unique_trials: list = field(default_factory=list)  # each forced unique vs a tuned rare in its slot


class Synthesizer(Protocol):
    def __call__(self, skeleton: Skeleton, engine: Any, scorer: Any, budget: int = 6000) -> BuildResult: ...


class SkeletonGenerator(Protocol):
    def generate(self, n: int, constraints: list | None = None, feedback: dict | None = None,
                 seed: int | None = None) -> list[Skeleton]: ...

    def mutate(self, parent: Skeleton, n: int = 1, seed: int | None = None) -> list[Skeleton]: ...


def _skill_stem(s: str) -> str:
    s = str(s or "").lower().replace(" ", "").replace("'", "")
    return s[:-6] if s.endswith("player") else s


def main_skill_matches(names, measured: dict | None) -> bool:
    """True when the skill PoB measures (a stats doc's mainSkill {id, name}) is the intended main skill:
    an exact id/name match, or one stem extending the other (Requiem / Requiem Ammo). A trigger setup or a
    gear change that hands the damage to another skill (Ember Fusillade for Detonate Dead, a weapon's default
    attack for a skill granted by a unique that was swapped out) does not match. No measurement: True."""
    ms = measured or {}
    got = {_skill_stem(ms.get("id")), _skill_stem(ms.get("name"))} - {""}
    if not got:
        return True
    want = {_skill_stem(n) for n in names or []} - {""}
    if not want or got & want:
        return True
    return any(len(min(a, b, key=len)) >= 6 and (a.startswith(b) or b.startswith(a)) for a in got for b in want)
