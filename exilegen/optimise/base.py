"""Shared contract for optimiser steps.

Every step (supports, tree, triggers/one-button, ...) works on ONE loaded build in ONE Engine and
judges candidates ONLY through the Scorer (never raw PoB DPS), so condition discounts, the Energy
correction, floors and constraints apply everywhere.

A step:
  * reads the current build state from ctx.engine (already loaded, profile set);
  * proposes changes, evaluates them with engine.whatif / whatif_many (no mutation) and
    ctx.evaluate(...) / ctx.score_stats(...);
  * commits accepted changes with ctx.commit(change) (engine.apply + rescore + history entry);
  * stops when no improvement or ctx.budget_left() <= 0;
  * returns StepResult.

Multi-profile scoring: the Scorer may hold several profiles (e.g. ["pinnacle_v1", "mapping_v1"]).
ctx.evaluate handles switching: it scores a change under every profile and returns the Scorer's
combined result (min over profiles). Steps must not call engine.set_profile themselves.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

# Engine "core" field group as a list, so a step can add groups (e.g. CORE_FIELDS + ["energy"]).
CORE_FIELDS = ["mainSkill", "stats", "minionStats", "main", "dps", "offence", "sustain", "movementSpeed",
               "ailmentThreshold", "spirit", "maxHit", "requirements", "keystones", "uniques"]
# Builds with Meta+Triggers groups need the energy facts, or triggered DPS is never energy-corrected.
TRIGGER_FIELDS = CORE_FIELDS + ["energy"]


CHANGE_KEYS = ("add_nodes", "remove_nodes", "attr_choices", "gem_changes", "group_enabled")


def change_only(change: dict | None) -> dict:
    """The engine change part of a change dict (steps may keep labels etc. alongside)."""
    return {k: v for k, v in (change or {}).items() if k in CHANGE_KEYS and v}


def has_meta_groups(skills: dict | None) -> bool:
    """True if the skills() view has an enabled Meta+Triggers group with a socketed spell (not item-granted)."""
    for g in (skills or {}).get("groups") or []:
        if g.get("enabled") is False or not g.get("metaGroup"):
            continue
        if any(not gm.get("support") and not gm.get("meta") and gm.get("enabled", True) is not False
               for gm in g.get("gems") or []):
            return True
    return False


def fields_for(skills: dict | None):
    """Stats fields a build needs for honest scoring: core, plus energy facts for trigger builds."""
    return list(TRIGGER_FIELDS) if has_meta_groups(skills) else "core"


@dataclass
class StepResult:
    name: str
    improved: bool
    start_objective: float
    end_objective: float
    changes: list = field(default_factory=list)   # list of change dicts actually committed
    evals: int = 0
    seconds: float = 0.0
    notes: list = field(default_factory=list)


class Step(Protocol):
    name: str

    def run(self, ctx: "Context") -> StepResult: ...


def _activation_guess(info, group_is_meta: bool) -> str:
    t = set(getattr(info, "types", None) or [])
    if info is not None and info.meta:
        return "persistent"
    if group_is_meta or "Triggered" in t or "InbuiltTrigger" in t:
        return "triggered"
    if "Persistent" in t or "HasReservation" in t:
        return "persistent"
    if t & {"Minion", "CreatesMinion", "SummonsTotem", "Trapped", "RemoteMined"}:
        return "proxy"
    return "manual"


def patch_skills(view: dict | None, change: dict | None, catalog=None) -> dict | None:
    """Approximate engine skills() view after `change` (gem_changes / group_enabled), for scoring a what-if.

    Engine what-ifs never change the committed skills view, so without this the Scorer's constraints and
    condition registry would judge a candidate by the gems it replaces. Facts of a replaced active gem
    (activation, damaging, spell) come from the catalogue (types) and the group (meta -> triggered).
    The exact view is only known after engine.apply; Context.try_commit verifies with it.
    """
    if not view or not change or not (change.get("gem_changes") or change.get("group_enabled")):
        return view
    v = copy.deepcopy(view)
    groups = v.setdefault("groups", [])
    by_idx = {g.get("index"): g for g in groups}
    for gi, en in (change.get("group_enabled") or {}).items():
        g = by_idx.get(int(gi))
        if g is not None:
            g["enabled"] = bool(en)
    removals = []
    for gc in change.get("gem_changes") or []:
        gi, slot = int(gc["group"]), int(gc["slot"])
        g = by_idx.get(gi)
        if g is None:
            g = {"index": gi, "label": "", "enabled": True, "main": False, "metaGroup": False, "grantedBy": None,
                 "gems": [], "skills": []}
            groups.append(g)
            by_idx[gi] = g
        gems = g.setdefault("gems", [])
        cur = next((x for x in gems if x.get("slot") == slot), None)
        if "gem" in gc and gc["gem"] is None:
            removals.append((g, slot))
            continue
        if "gem" not in gc:
            if cur is not None and "enabled" in gc:
                cur["enabled"] = bool(gc["enabled"])
            continue
        info = catalog.get(gc["gem"]) if catalog is not None else None
        entry = {"slot": slot, "id": info.gem_id if info else str(gc["gem"]),
                 "skillId": info.skill_id if info else str(gc["gem"]),
                 "name": info.name if info else str(gc["gem"]), "support": bool(info.support) if info else False,
                 "enabled": gc.get("enabled", True) is not False, "gemTypes": list(info.types) if info else [],
                 "meta": bool(info.meta) if info else False, "level": gc.get("level"), "patched": True}
        if cur is not None:
            gems[gems.index(cur)] = entry
        else:
            gems.append(entry)
        g["skills"] = [f for f in g.get("skills") or [] if f.get("gemSlot") != slot]
        if entry["meta"]:
            g["metaGroup"] = True
    for g, slot in removals:
        g["gems"] = [x for x in g.get("gems") or [] if x.get("slot") != slot]
        g["skills"] = [f for f in g.get("skills") or [] if f.get("gemSlot") != slot]
    # skill facts for patched actives (after metaGroup is final)
    for g in groups:
        if any(x.get("patched") for x in g.get("gems") or []):
            g["metaGroup"] = any(x.get("meta") and x.get("enabled", True) is not False for x in g.get("gems") or [])
        for x in g.get("gems") or []:
            if not x.get("patched") or x.get("support"):
                continue
            info = catalog.get(x["skillId"]) if catalog is not None else None
            t = list(info.types) if info else []
            g.setdefault("skills", []).append({
                "group": g["index"], "name": x["name"], "id": x["skillId"], "gemSlot": x["slot"], "types": t,
                "activation": _activation_guess(info, bool(g.get("metaGroup")) and not x.get("meta")),
                "damaging": bool(info.damaging) if info else False, "spell": bool(info.spell) if info else False,
                "meta": bool(x.get("meta")), "enabled": x.get("enabled", True), "patched": True})
    return v


def _nviol(r: dict) -> int:
    """Distinct violations (rule, entry), counted once across profiles."""
    return len({(v.get("rule"), v.get("entry")) for v in r.get("violations") or []})


def violation_size(r: dict) -> float:
    """Total size of a result's violations: each distinct (rule, entry) counts its worst normalised
    shortfall across profiles (floors carry one; a constraint or a floor without one counts 1.0).
    A build 5 resistance short is nearly feasible; one with no chaos res and no EHP is not."""
    worst: dict = {}
    for v in r.get("violations") or []:
        k = (v.get("rule"), v.get("entry"))
        sf = v.get("shortfall")
        sf = 1.0 if sf is None else max(float(sf), 1e-3)     # a failed floor is never "free"
        worst[k] = max(worst.get(k, 0.0), sf)
    return sum(worst.values())


@dataclass
class Context:
    engine: Any                      # exilegen.engine.Engine with the build loaded
    scorer: Any                      # exilegen.scoring.Scorer
    baseline_stats: Any = None       # stats doc(s) of the starting build (floors in relative mode)
    budget: int = 20000              # max engine evaluations for the whole run
    fields: Any = "core"             # stats field group for whatif (pipeline: fields_for(skills))
    evals: int = 0
    history: list = field(default_factory=list)
    current: dict | None = None      # latest score result of the committed build
    current_stats: Any = None
    log_fn: Any = print
    catalog: Any = None              # exilegen.optimise.gems.Catalog (lazy)
    seed: int | None = None
    stats_filter: Any = None         # optional fn(stats_doc, profile) -> stats_doc applied before scoring
    _skills: Any = field(default=None, repr=False)

    # ------------------------------------------------------------ helpers
    def log(self, *a):
        if self.log_fn:
            self.log_fn(*a)

    def budget_left(self) -> int:
        return self.budget - self.evals

    def gems(self):
        """Gem catalogue (PoB data of the engine's PoB root)."""
        if self.catalog is None:
            from .gems import catalog
            self.catalog = catalog(getattr(self.engine, "pob_root", None))
        return self.catalog

    def _profiles(self) -> list[str]:
        return list(getattr(self.scorer, "profile_list", None) or [self.scorer.profile])

    def _use_profile(self, prof: str) -> None:
        """Switch the engine profile only when needed (a switch costs ~0.1-0.2 s)."""
        if len(self._profiles()) > 1 and getattr(self.engine, "_profile", None) != prof:
            self.engine.set_profile(prof)

    def _stats_for(self, change: dict | None, profiles: list[str] | None = None) -> dict:
        """Stats for `change` under every scorer profile (or `profiles`), keyed by profile name.
        The engine is left on the first scorer profile."""
        out = {}
        profs = self._profiles()
        todo = list(profiles or profs)
        # current profile first saves a switch
        cur = getattr(self.engine, "_profile", None)
        todo.sort(key=lambda p: p != cur)
        for prof in todo:
            self._use_profile(prof)
            if change and change_only(change):
                out[prof] = self.engine.whatif(fields=self.fields, **change_only(change))
            else:
                out[prof] = self.engine.stats(fields=self.fields)
            self.evals += 1
        self._use_profile(profs[0])
        return {p: out[p] for p in (profiles or profs)}

    def _keyed(self, stats) -> dict:
        """{profile: stats}; a bare stats doc is the first profile's."""
        profs = self._profiles()
        if isinstance(stats, dict) and stats and all(k in profs for k in stats) \
                and all(isinstance(v, dict) for v in stats.values()):
            return stats
        return {profs[0]: stats}

    def score_stats(self, stats_by_profile: dict, skills=None) -> dict:
        """Score {profile: stats} (or one bare stats doc = first profile). With fewer profiles than the
        scorer holds (a cheap screen), only those are scored (one profile -> that profile's result)."""
        sp = self._keyed(stats_by_profile)
        profs = self._profiles()
        base = self.baseline_stats
        if base is not None and (len(profs) == 1 or self.stats_filter):
            base = self._keyed(base)
        if self.stats_filter:
            sp = {p: self.stats_filter(d, p) for p, d in sp.items()}
            if base is not None:
                base = {p: self.stats_filter(d, p) for p, d in base.items()}
        if len(profs) > 1 and len(sp) == 1:
            prof, stats = next(iter(sp.items()))
            b1 = base.get(prof) if isinstance(base, dict) and prof in base else None
            return self.scorer.score_one(stats, prof, skills, b1)
        # The Scorer reads a dict `skills` as {profile: view}; a bare skills() view is a dict too.
        sk = {p: skills for p in sp} if skills is not None else None
        return self.scorer.score(sp, sk, baseline=base)

    def evaluate_many(self, changes: list[dict], profiles: list[str] | None = None, skills=None,
                      chunk: int = 64) -> list[dict | None]:
        """Score many candidate changes: one profile switch per profile, whatif_many in chunks.
        Gem / group changes get a patched skills view each. A failed variant scores None."""
        profs = list(profiles or self._profiles())
        cur = getattr(self.engine, "_profile", None)
        order = sorted(profs, key=lambda p: p != cur)
        stats: list[dict] = [dict() for _ in changes]
        for prof in order:
            self._use_profile(prof)
            for i in range(0, len(changes), chunk):
                part = [change_only(c) for c in changes[i:i + chunk]]
                try:
                    rs = self.engine.whatif_many(part, fields=self.fields)
                except Exception:
                    rs = []
                    for c in part:
                        try:
                            rs.append(self.engine.whatif(fields=self.fields, **c))
                        except Exception as ex:
                            rs.append({"err": str(ex)})
                for j, r in enumerate(rs):
                    stats[i + j][prof] = r
            self.evals += len(changes)
        self._use_profile(self._profiles()[0])
        base_view = skills if skills is not None else self.skills()
        out = []
        for c, sp in zip(changes, stats):
            if any(not isinstance(x, dict) or "err" in x for x in sp.values()) or len(sp) != len(profs):
                out.append(None)
                continue
            view = base_view
            if skills is None and c and (c.get("gem_changes") or c.get("group_enabled")):
                view = patch_skills(base_view, c, self.gems())
            try:
                out.append(self.score_stats({p: sp[p] for p in profs}, view))
            except Exception:
                out.append(None)
        return out

    def evaluate(self, change: dict | None = None, skills=None, profiles: list[str] | None = None) -> dict:
        """Score a candidate change without committing it. change uses Engine.whatif kwargs.
        Gem / group changes are scored against a patched skills view (patch_skills). `profiles` restricts
        the evaluation to some profiles (a cheap screen); the result is then not comparable with
        ctx.current unless compared per profile (see current_for)."""
        if skills is None:
            skills = self.skills()
            if change and (change.get("gem_changes") or change.get("group_enabled")):
                skills = patch_skills(skills, change, self.gems())
        return self.score_stats(self._stats_for(change, profiles), skills)

    def current_for(self, profile: str) -> dict | None:
        """The committed build's score under one profile."""
        if self.current is None:
            return None
        per = self.current.get("profiles")
        return per.get(profile) if per else self.current

    def skills(self):
        if self._skills is None:
            self._skills = self.engine.skills()
        return self._skills

    def refresh(self) -> dict:
        self._skills = None
        if self.fields == "core" and has_meta_groups(self.skills()):
            self.fields = list(TRIGGER_FIELDS)     # trigger builds: energy facts or no energy correction
        self.current_stats = self._stats_for(None)
        self.current = self.score_stats(self.current_stats, self.skills())
        return self.current

    def commit(self, change: dict, label: str = "", result: dict | None = None) -> dict:
        self.engine.apply(fields=False, **change_only(change))
        self._skills = None
        self.evals += 1
        res = self.refresh()
        self.history.append({"label": label, "change": change, "objective": res.get("objective"),
                             "dps_honest": res.get("dps_honest"), "feasible": res.get("feasible"), "t": time.time()})
        return res

    def try_commit(self, change: dict, label: str = "", accept=None, min_gain: float = 1e-9) -> tuple[bool, dict]:
        """Apply `change`, score the exact result (real skills view, every profile) and keep it only if
        accept(new, old) (default: Context.better with min_gain). A rejected change is rolled back with
        engine snapshot/restore. -> (kept, score result of the build as it now stands)."""
        prev, prev_stats, prev_skills = self.current, self.current_stats, self._skills
        tok = self.engine.snapshot()
        try:
            res = self.commit(change, label)
        except Exception:
            self.engine.restore(tok)
            self.current, self.current_stats, self._skills = prev, prev_stats, prev_skills
            self._drop(tok)
            raise
        ok = accept(res, prev) if accept else self.better(res, prev, min_gain)
        if not ok:
            self.engine.restore(tok)
            self.history.pop()
            self.current, self.current_stats, self._skills = prev, prev_stats, prev_skills
        self._drop(tok)
        return ok, self.current

    def _drop(self, tok):
        try:
            self.engine.op("drop_snapshot", token=tok)
        except Exception:
            pass

    @staticmethod
    def better(a: dict | None, b: dict | None, eps: float = 1e-9) -> bool:
        """True if score result a beats b: feasibility first, then objective."""
        if a is None:
            return False
        if b is None:
            return True
        fa, fb = bool(a.get("feasible")), bool(b.get("feasible"))
        if fa != fb:
            return fa
        if not fa:
            # both infeasible: smaller total violation first (sum of normalised shortfalls), so a move
            # that closes most of a large gap counts as progress and repair can climb towards feasibility
            va, vb = violation_size(a), violation_size(b)
            if abs(va - vb) > 1e-6:
                return va < vb
        return (a.get("objective") or float("-inf")) > (b.get("objective") or float("-inf")) + eps
