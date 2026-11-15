"""Synthesizer: Skeleton -> complete, legal, scored PoB build from nothing (contracts.Synthesizer).

    from exilegen.builder.synth import synthesize
    res = synthesize(skeleton, engine, scorer, budget=6000)      # -> contracts.BuildResult

Steps (each logged into BuildResult.notes; an impossible step ends with ok=False + error):
  1. class   new_build, class + ascendancy, level.
  2. skills  main group (main gem + forced supports), trigger setup (meta gem + spells, feeder group),
             extra skills (auras / heralds / persistent / minions) in their own groups; every gem at the
             level the character allows, quality 20. Item-granted groups that the skeleton does not name
             are disabled. The main skill is pinned explicitly ({group, skill}), never "auto".
  3. gear    requested uniques (PoB item DB) in their slots, a weapon for the main skill (hint, else the
             skill's weapon requirement, else Staff / Sceptre / mace by skill kind) with its off-hand,
             template rares (builder/data/gear_templates.json, budget tier) in every other slot.
     -> the build is exported and loaded into the engine with the scorer's first profile and the pinned
        main skill; from here every judgement goes through the Scorer (honest DPS, floors, constraints).
  4. sockets empty support sockets of the main / meta / feeder groups (count by level) are filled
             greedily: every legal support is screened on one profile, the best few are verified with an
             exact apply on every profile. Extra skills are disabled (last first) while Spirit is over.
  5. ascend  forced ascendancy notables, then greedy by score gain per point.
  6. tree    forced keystones are allocated and pinned; TreeStep(mode="scratch") spends the level's points
             (attribute choices included: attribute requirements are a hard floor).
  7. gear    bridge optimise_gear (greedy affixes per slot, uniques kept) with weights derived from the
             scorer's objective; every proposal is equipped, kept only if the Scorer agrees.
  8. polish  SupportStep -> TriggerStep (if Meta+Triggers groups) -> TreeStep warm -> SupportStep, each
             rolled back if it ends worse than it started, within the remaining budget. The cycle repeats
             (up to POLISH_CYCLES) until one gains < 1%.
  9. final   score (absolute floors: no baseline), export code, stats, skills, evals, seconds.

Budget: engine evaluations (Context.evals), split over sockets / ascendancy / tree / polish.
Floors: with no baseline every "either" floor is absolute; the default scorer uses DEFAULT_FLOORS (mana
sustain is relative and therefore not judged without a baseline, as PoB ignores flasks and leech).
"""
from __future__ import annotations

import dataclasses
import math
import time
import traceback
from typing import Any

from exilegen.optimise.base import Context, StepResult, fields_for, has_meta_groups
from exilegen.scoring import Scorer
from exilegen.scoring import floors as F

from .contracts import BuildResult, Skeleton, main_skill_matches
from .gear import GearPlanner, weapon_spec

DEFAULT_PROFILES = ["pinnacle_v1", "mapping_v1"]
POLISH = ("supports", "triggers", "tree_warm", "supports")
# Budget weight of each polish step within a cycle (share = remaining budget x weight / remaining weights).
# Measured on 105 builds (2 runs): tree_warm gains 0.195 objective per 1k evals and improves 99% of the time;
# the first supports pass 0.335; the closing supports pass (same step again, after tree_warm) only 0.022 while
# taking as many evals. A step that already ran in the cycle gets REPEAT_WEIGHT.
STEP_WEIGHTS = {"tree_warm": 2.0}
REPEAT_WEIGHT = 0.3
POLISH_CYCLES = 3                # repeat POLISH until a cycle gains < 1% (pipeline.CYCLE_MIN_GAIN)
# share of the evaluation budget per phase (the rest goes to the polish steps)
SHARE = {"sockets": 0.15, "ascendancy": 0.04, "tree": 0.36}


class SynthError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------------- helpers
def support_slots(level: int) -> int:
    """Support sockets a main skill has at this character level (Jeweller's Orbs by campaign stage)."""
    if level >= 65:
        return 5
    if level >= 45:
        return 4
    if level >= 20:
        return 3
    return 2


def make_scorer(scorer=None, profiles=None, constraints=None, floors=None):
    """The caller's Scorer wrapped so trigger costs are energy-aware (as the pipeline does); a default
    two-profile Scorer when none is given."""
    from exilegen.optimise.pipeline import FilteredScorer
    from exilegen.optimise.triggers import energy_cost_filter
    if scorer is None:
        sc = FilteredScorer(profile=list(profiles or DEFAULT_PROFILES), constraints=list(constraints or []),
                            floors=floors if floors is not None else list(F.DEFAULT_FLOORS))
    elif isinstance(scorer, FilteredScorer):
        sc = scorer
    else:
        kw = {f.name: getattr(scorer, f.name) for f in dataclasses.fields(Scorer) if f.init and f.name != "_cache"}
        sc = FilteredScorer(**kw)
    if getattr(sc, "stats_filter", None) is None:
        sc.stats_filter = energy_cost_filter(sc)
    return sc


def _objective_weights(scorer) -> dict:
    """optimise_gear weights from the Scorer's objective: log DPS vs log EHP (+ max hit); a little pool
    weight keeps life/ES lines alive when EHP barely moves."""
    w = getattr(scorer, "weights", None) or {}

    def num(x):
        if isinstance(x, dict):
            vals = [v for v in x.values() if isinstance(v, (int, float))]
            return max(vals) if vals else 0.0
        return float(x or 0.0)
    ehp = num(w.get("ehp", 0.3)) + num(w.get("max_hit", 0.1))
    return {"dps": 1.0, "ehp": round(ehp, 3), "life": round(ehp / 2, 3)}


def _short(r: dict | None) -> str:
    if not r:
        return "-"
    v = sorted({str(x.get("entry")) for x in r.get("violations") or []})
    return (f"obj {r.get('objective', 0):.3f} honest {r.get('dps_honest') or 0:,.0f} ehp {r.get('ehp') or 0:,.0f}"
            + ("" if r.get("feasible") else f" INFEASIBLE({', '.join(v)})"))


class _Synth:
    def __init__(self, sk: Skeleton, engine, scorer, budget: int, log_fn=None, gear_optimise: bool = True,
                 polish=POLISH, seed: int = 0, polish_cycles: int = POLISH_CYCLES, asc_points: int | None = None,
                 unique_trial: str = "on", seed_code: str | None = None):
        self.sk, self.e, self.budget, self.seed = sk, engine, int(budget), seed
        self.scorer = make_scorer(scorer, constraints=list(getattr(sk, "constraints", None) or []))
        self.asc_points = asc_points           # cap on ascendancy points spent (None = all the engine gives)
        self.profiles = list(self.scorer.profile_list)
        self.log_fn = log_fn
        self.gear_optimise = gear_optimise
        self.polish = list(polish or ())
        self.polish_cycles = polish_cycles
        self.unique_trial = unique_trial       # "on" | "off"
        self.seed_code = seed_code             # a related build (a mutation's parent) whose tree seeds ours
        self.trials: list[dict] = []
        self.notes: list[str] = []
        self.ctx: Context | None = None
        self.main_ref: dict | None = None      # {group, skill}
        self.main_group: int | None = None
        self.meta_group: int | None = None
        self.feeder_group: int | None = None
        self.extra_groups: list[int] = []
        self.n_groups = 0
        self.timing: dict[str, float] = {}
        from exilegen.optimise.gems import catalog
        self.cat = catalog(getattr(engine, "pob_root", None))

    # ------------------------------------------------------------------ logging
    def note(self, msg: str) -> None:
        self.notes.append(msg)
        if self.log_fn:
            self.log_fn(f"[synth] {msg}")

    def gem(self, ref, what: str):
        g = self.cat.get(ref)
        if g is None:
            # tolerate "Arc" vs "ArcPlayer" and missing "Player" suffixes
            g = self.cat.get(f"{ref}Player") or next((x for x in self.cat.by_gem.values()
                                                      if x.name.lower() == str(ref).lower().strip()), None)
        if g is None:
            raise SynthError(f"{what} {ref!r}: unknown gem")
        return g

    # ------------------------------------------------------------------ 1. class
    def make_class(self):
        sk = self.sk
        self.e.raw("new_build", {"name": f"synth {sk.id}"[:60]})
        classes = self.e.raw("list_classes", {}).get("classes") or []
        cl = next((c for c in classes if c["name"].lower() == sk.cls.lower()), None)
        asc = None
        for c in classes:
            a = next((a for a in c["ascendancies"] if a["name"].lower() == (sk.ascendancy or "").lower()), None)
            if a and (cl is None or c is cl):
                cl, asc = c, a
                break
        if cl is None:
            raise SynthError(f"unknown class {sk.cls!r}")
        if sk.ascendancy and asc is None:
            raise SynthError(f"ascendancy {sk.ascendancy!r} does not belong to {cl['name']}")
        p = {"classId": cl["id"]}
        if asc:
            p["ascendClassId"] = asc["id"]
        self.e.raw("select_class", p)
        self.e.raw("set_level", {"level": int(sk.level)})
        self.cls = cl["name"]
        self.note(f"class {cl['name']} / {asc['name'] if asc else '-'} level {sk.level}")

    # ------------------------------------------------------------------ 2. skills (raw)
    def _add_group(self, label: str) -> int:
        r = self.e.raw("add_socket_group", {"label": label})
        self.n_groups = int(r["groupIndex"])
        return self.n_groups

    def _add_gem(self, gi: int, info, quality: int = 20) -> None:
        p = {"groupIndex": gi, "gemId": info.gem_id}
        if not info.support:
            p["quality"] = quality
        self.e.raw("add_gem", p)

    def _granted_ids(self) -> set:
        """Skill ids the skeleton's uniques grant (catalog `grants`), plus a built-in trigger's spells."""
        out = set()
        for u in self.sk.uniques or []:
            if isinstance(u, dict):
                out |= {str(x) for x in u.get("grants") or []}
        trig = self.sk.trigger or {}
        if trig and not trig.get("meta") and trig.get("source"):
            out |= {str(x) for x in trig.get("spells") or []}
        return out

    def _main_names(self) -> set:
        sk, m = self.sk, self.main_info
        names = {str(sk.main_skill).lower(), (m.name or "").lower(), (m.skill_id or "").lower()}
        if (sk.tags or {}).get("main_skill_name"):
            names.add(str(sk.tags["main_skill_name"]).lower())
        return {n for n in names if n}

    def make_skills(self):
        from exilegen.optimise.gems import GemInfo
        sk = self.sk
        granted = self._granted_ids()
        self.granted_main = str(sk.main_skill) in granted
        try:
            main = self.gem(sk.main_skill, "main skill")
        except SynthError:
            if not self.granted_main:
                raise
            name = (sk.tags or {}).get("main_skill_name") or str(sk.main_skill)
            main = GemInfo(gem_id="", skill_id=str(sk.main_skill), name=name,
                           types=["Spell"] if (sk.tags or {}).get("spell") else [])
        self.main_info = main
        trig = dict(sk.trigger or {})
        meta = None
        if trig.get("meta"):
            try:
                meta = self.gem(trig["meta"], "meta gem")
            except SynthError as ex:
                self.note(f"trigger: {ex}; no meta group")
        spells = []
        for x in trig.get("spells") or []:
            if str(x) in granted:
                continue                      # item-granted: comes with the unique, cannot be socketed
            try:
                spells.append(self.gem(x, "trigger spell"))
            except SynthError as ex:
                self.note(f"trigger: {ex}")
        feeder = None
        if trig.get("feeder"):
            try:
                feeder = self.gem(trig["feeder"], "feeder")
            except SynthError as ex:
                self.note(f"trigger: {ex}")
        if meta is not None and not spells and not self.granted_main and main.gem_id:
            spells = [main]
        if meta is not None and not spells:
            self.note(f"trigger: no socketable spell for {meta.name}; meta group skipped")
            meta = None
        main_in_meta = meta is not None and main.skill_id in {s.skill_id for s in spells}
        forced = [self.gem(x, "support") for x in sk.supports or []]
        if meta is not None:
            gi = self._add_group("meta")
            self._add_gem(gi, meta)
            for x in spells:
                self._add_gem(gi, x)
            for x in [self.gem(y, "meta support") for y in trig.get("supports") or []]:
                self._add_gem(gi, x)
            if main_in_meta:
                for x in forced:
                    self._add_gem(gi, x)
                self.main_group = gi
            self.meta_group = gi
            self.note(f"trigger group {gi}: {meta.name} + {', '.join(x.name for x in spells)}")
        if not main_in_meta and not self.granted_main:
            gi = self._add_group("main")
            self._add_gem(gi, main)
            for x in forced:
                self._add_gem(gi, x)
            self.main_group = gi
            self.note(f"main group {gi}: {main.name}" + (f" + {', '.join(x.name for x in forced)}" if forced else ""))
        elif self.granted_main:
            self.note(f"main skill {main.name} comes from a unique (no gem)")
        if feeder is not None and feeder.skill_id != main.skill_id:
            gi = self._add_group("feeder")
            self._add_gem(gi, feeder)
            for x in [self.gem(y, "feeder support") for y in trig.get("feeder_supports") or []]:
                self._add_gem(gi, x)
            self.feeder_group = gi
            self.note(f"feeder group {gi}: {feeder.name}" + (f" ({trig.get('source')})" if trig.get("source") else ""))
        for x in sk.extra_skills or []:
            try:
                info = self.gem(x, "extra skill")
            except SynthError as ex:
                self.note(f"skip extra skill: {ex}")
                continue
            if info.support:
                self.note(f"skip extra skill {info.name}: a support")
                continue
            gi = self._add_group(info.name)
            self._add_gem(gi, info)
            self.extra_groups.append(gi)
        if self.extra_groups:
            self.note(f"extra skills: {', '.join(str(g) for g in self.extra_groups)}")

    # ------------------------------------------------------------------ 3. gear (raw)
    def uniques(self):
        sk = self.sk
        self.gp = GearPlanner(self.e, level=sk.level, tier=sk.budget_tier, defence=sk.defence, cls=self.cls,
                              notes=self.notes)
        for u in sk.uniques or []:
            u = {"name": u} if isinstance(u, str) else dict(u)
            got = self.gp.equip_unique(u["name"], u.get("slot"), u.get("variant"))
            if got is None:
                raise SynthError(f"unique {u['name']!r} could not be equipped")

    def make_gear(self):
        sk = self.sk
        spec = weapon_spec(sk.weapon) if sk.weapon else None
        if spec is None:
            req = self._weapon_requirement()
            spec = weapon_spec(req) if req else None
        if spec is None:
            t = set(self.main_info.types)
            kind = "minion" if t & {"Minion", "CreatesMinion"} else "spell" if "Spell" in t else "attack"
            spec = weapon_spec(__import__("exilegen.builder.gear", fromlist=["templates"]).templates()
                               ["default_weapon"][kind])
        if sk.weapon and spec is None:
            self.note(f"weapon hint {sk.weapon!r} unknown; using the skill's requirement")
        off = self.sk.offhand or (self.sk.trigger or {}).get("offhand")
        if spec is not None and off:
            spec = dict(spec, offhand=off)
            if spec.get("hands") == 2:
                self.note(f"off-hand {off} wanted but {spec['type']} is two-handed")
        self.weapon = spec
        self.gp.equip_weapons(spec)
        self.gp.fill_templates()
        taken = ", ".join(f"{s}={v.split(':', 1)[-1]}" for s, v in sorted(self.gp.taken.items()))
        self.note(f"gear ({sk.budget_tier}, {self.gp.dtype}): {taken}")

    def _weapon_requirement(self) -> str | None:
        try:
            rows = self.e.raw("list_gems", {"query": self.main_info.name, "limit": 10, "maxLevel": 0}).get("gems") or []
        except Exception:
            return None
        row = next((r for r in rows if r.get("gemId") == self.main_info.gem_id), None) or \
            next((r for r in rows if (r.get("name") or "").lower() == self.main_info.name.lower()), None)
        w = (row or {}).get("weapon")
        return w if isinstance(w, str) and w else None

    # ------------------------------------------------------------------ main skill pin + load
    def main_ok(self, stats=None) -> bool:
        """Is the skill PoB measures the skeleton's main skill? (current stats by default)"""
        stats = self.ctx.current_stats if stats is None and self.ctx is not None else stats
        doc = (next(iter(stats.values()), None) if stats and all(isinstance(v, dict) for v in stats.values())
               else stats) or {}
        return main_skill_matches(self._main_names(), doc.get("mainSkill"))

    def _repin(self, code: str) -> bool:
        """After a reload moved the main skill to another group (item-granted groups come and go with gear),
        find it again by skill id and re-pin. False when no group holds it any more."""
        names = self._main_names()
        for g in self.e.skills().get("groups") or []:
            if g.get("grantedBy") in ("Explode", "Thorns") or g.get("enabled") is False:
                continue
            for sk_ in g.get("skills") or []:
                if (sk_.get("id") or "").lower() in names or (sk_.get("name") or "").lower() in names:
                    if (g["index"], sk_["index"]) == (self.main_ref["group"], self.main_ref["skill"]):
                        return False
                    self.main_ref = {"group": int(g["index"]), "skill": int(sk_["index"])}
                    self.main_group = int(g["index"])
                    self.e.load(code=code, profile=self.profiles[0], main=dict(self.main_ref))
                    self.note(f"main skill re-pinned: group {g['index']} skill {sk_['index']}")
                    return True
        return False

    def pin_and_load(self):
        """Load the raw build, find the main skill's {group, skill} in the engine's skills view (skill ids,
        item-granted groups included), disable item-granted groups the skeleton does not use, pin."""
        code = self.e.raw("save_build_code", {})
        code = code.get("code") if isinstance(code, dict) else code
        self.e.load(code=code, profile=self.profiles[0], main="pob")
        view = self.e.skills()
        main_names = self._main_names()
        trig = self.sk.trigger or {}
        wanted = set(main_names) | {str(x).lower() for x in self.sk.extra_skills or []}
        wanted |= {str(x).lower() for x in [trig.get("feeder"), trig.get("feeder_name")] + list(trig.get("spells") or []) if x}

        def hit(sk_, names):
            return (sk_.get("id") or "").lower() in names or (sk_.get("name") or "").lower() in names
        found = None
        groups = [g for g in view.get("groups") or [] if g.get("grantedBy") not in ("Explode", "Thorns")]
        order = sorted(groups, key=lambda g: (g.get("index") != self.main_group, bool(g.get("grantedBy"))))
        for g in order:
            for sk_ in g.get("skills") or []:
                if hit(sk_, main_names):
                    found = (g["index"], sk_["index"], g.get("grantedBy"))
                    break
            if found:
                break
        if found is None:
            raise SynthError(f"main skill {self.main_info.name} ({self.sk.main_skill}) has no socket group"
                             " (granted by a unique that is missing or not equipped?)")
        off = {}
        for g in groups:
            if not g.get("grantedBy") or g["index"] == found[0] or g.get("enabled") is False:
                continue
            if not any(hit(x, wanted) for x in g.get("skills") or []):
                off[str(g["index"])] = False
        if off:
            try:
                self.e.apply(group_enabled=off, fields=False)
                self.note(f"disabled unused item-granted groups {', '.join(off)}")
            except Exception as ex:
                self.note(f"could not disable item-granted groups: {str(ex)[:100]}")
        self.granted_main = bool(found[2])
        self.main_group = found[0]
        self.main_ref = {"group": int(found[0]), "skill": int(found[1])}
        self.note(f"main skill pinned: group {found[0]} skill {found[1]}"
                  + (f" (granted by {found[2]})" if found[2] else ""))
        self.one_button()
        self.checkpoint(first=True, code=self.e.export())

    def one_button(self):
        """Under a one_button constraint keep a single manual damaging skill: the feeder when there is one,
        else the main skill; other enabled manual damaging groups (extra skills, item-granted attacks) are
        disabled."""
        cons = [str(c) for c in (getattr(self.scorer, "constraints", None) or [])]
        if not any(c.startswith("one_button") for c in cons):
            return
        keep = self.feeder_group or self.main_ref["group"]
        off = {}
        for g in self.e.skills().get("groups") or []:
            if g.get("enabled") is False or g["index"] in (keep, self.meta_group):
                continue
            manual = [x for x in g.get("skills") or [] if x.get("activation") == "manual" and x.get("damaging")
                      and x.get("enabled", True) is not False]
            if manual and g["index"] != self.main_ref["group"]:
                off[str(g["index"])] = False
        main_g = next((g for g in self.e.skills().get("groups") or [] if g["index"] == self.main_ref["group"]), {})
        main_manual = any(x.get("activation") == "manual" and x.get("damaging") for x in main_g.get("skills") or []
                          if x.get("index") == self.main_ref["skill"])
        if main_manual and self.feeder_group and self.feeder_group != self.main_ref["group"]:
            self.note("one_button: the main skill and the feeder are both manual; the build will violate it")
        if off:
            try:
                self.e.apply(group_enabled=off, fields=False)
                self.extra_groups = [g for g in self.extra_groups if str(g) not in off]
                self.note(f"one_button: disabled manual damaging groups {', '.join(off)}")
            except Exception as ex:
                self.note(f"one_button: could not disable {', '.join(off)}: {str(ex)[:100]}")

    def checkpoint(self, first: bool = False, code: str | None = None):
        """Export the live build and reload it (profile + pinned main): a crash-recovery point that also
        covers raw item changes, which engine snapshots do not."""
        if code is None:
            code = self.e.raw("save_build_code", {})
            code = code.get("code") if isinstance(code, dict) else code
        self.e.load(code=code, profile=self.profiles[0], main=dict(self.main_ref))
        if first:
            ctx = Context(self.e, self.scorer, budget=self.budget, log_fn=self.log_fn, seed=self.seed)
            ctx.baseline_stats = None
            self.ctx = ctx
        ctx = self.ctx
        ctx._skills = None
        ctx.fields = fields_for(ctx.skills())
        ctx.refresh()
        if not self.main_ok() and self._repin(code):
            ctx._skills = None
            ctx.fields = fields_for(ctx.skills())
            ctx.refresh()
        main = (next(iter(ctx.current_stats.values())) or {}).get("main") or {}
        if main.get("group") != self.main_ref["group"]:
            self.note(f"warning: engine main is {main} not {self.main_ref}")

    # ------------------------------------------------------------------ 4. support sockets
    def _group_view(self, gi):
        return next((g for g in self.ctx.skills().get("groups") or [] if g.get("index") == gi), None)

    BEAM_WIDTH = 4          # partial support sets kept per depth
    BEAM_POOL = 16          # single-support screen survivors the beam combines

    def fill_supports(self, gi: int, n_supports: int, budget: int, label: str):
        """Fill the empty support sockets of group gi with a beam search over support sets.

        1. Screen every legal support alone in the next socket (one batched what-if per candidate).
        2. Combine the best BEAM_POOL of them: at each depth extend every kept set by one support, score
           the whole set with what-if, keep the best BEAM_WIDTH sets. Sets are scored together, so a
           support that only pays off with another one (more projectiles + a projectile support, a
           conversion + its damage type) is found, which one-at-a-time greedy filling misses.
        3. Commit the best set of any size, accepted only if it raises the objective without adding a
           violation: feasibility-first acceptance would take a support that wrecks DPS to pass a floor
           (Zarokh's Revolt slowing casts under a life-cost floor), which later steps repair far better.
        Item-granted groups take supports from slot 2 on unless the grant says noSupports."""
        ctx = self.ctx

        def nviol(r):
            return len({(v.get("rule"), v.get("entry")) for v in (r or {}).get("violations") or []})

        def accept(new, old):
            if old is None:
                return True
            return nviol(new) <= nviol(old) and (new.get("objective") or -1e18) > (old.get("objective") or -1e18) + 1e-4
        g = self._group_view(gi)
        if g is None or g.get("noSupports"):
            return
        have = sum(1 for x in g.get("gems") or [] if x.get("support"))
        want = max(0, n_supports - have)
        if want <= 0:
            return
        stop = ctx.evals + budget
        b = (ctx.current or {}).get("binding_profile")
        p0 = b if b in self.profiles else self.profiles[0]
        nslot = len(g.get("gems") or []) + 1
        try:
            vs = self.e.valid_supports(gi)
        except Exception as ex:
            self.note(f"{label}: valid_supports failed: {ex}")
            return
        cands = [x for x in vs.get("supports") or [] if not x.get("socketed") and not x.get("conflicts")]
        if not cands:
            return
        names = {x["id"]: x["name"] for x in cands}

        def change(ids):
            return {"gem_changes": [{"group": gi, "slot": nslot + i, "gem": sid} for i, sid in enumerate(ids)]}

        def obj(r):
            return (r or {}).get("objective") if r is not None else None
        base_v = nviol(ctx.current)
        # 1. single-support screen
        singles = [(sid,) for sid in names][:max(1, stop - ctx.evals)]
        scored = ctx.evaluate_many([change(c) for c in singles], profiles=[p0])
        ranked = sorted(((r, c) for r, c in zip(scored, singles) if r is not None and obj(r) is not None
                         and nviol(r) <= base_v), key=lambda rc: rc[0]["objective"], reverse=True)
        if not ranked:
            self.note(f"{label}: no support screened without a new violation")
            return
        pool = [c[0] for _, c in ranked[:self.BEAM_POOL]]
        beam = [(r, c) for r, c in ranked[:self.BEAM_WIDTH]]
        best_by_size = {1: ranked[0]}
        # 2. beam over sets drawn from the pool
        from exilegen.optimise.supports import _base_name
        info = {x["id"]: (set(x.get("family") or []), _base_name(x["name"])) for x in cands}

        def clash(c, sid):          # same gem family or the I/II/III lineage of one support
            f, n = info[sid]
            return any(info[o][1] == n or info[o][0] & f for o in c)
        for depth in range(2, want + 1):
            if ctx.evals >= stop:
                break
            seen, exts = set(), []
            for _, c in beam:
                for sid in pool:
                    if sid in c or clash(c, sid):
                        continue
                    key = tuple(sorted(c + (sid,)))
                    if key in seen:
                        continue
                    seen.add(key)
                    exts.append(c + (sid,))
            exts = exts[:max(0, stop - ctx.evals)]
            if not exts:
                break
            scored = ctx.evaluate_many([change(c) for c in exts], profiles=[p0])
            nxt = sorted(((r, c) for r, c in zip(scored, exts) if r is not None and obj(r) is not None
                          and nviol(r) <= base_v), key=lambda rc: rc[0]["objective"], reverse=True)
            if not nxt:
                break
            beam = nxt[:self.BEAM_WIDTH]
            best_by_size[depth] = nxt[0]
        # 3. commit the best set (largest on a tie: an empty socket is never better than a neutral support)
        order = sorted(best_by_size.items(), key=lambda kv: (kv[1][0]["objective"], kv[0]), reverse=True)
        for size, (r, c) in order[:3]:
            ok, _ = ctx.try_commit(change(c), label=f"socket {', '.join(names[x] for x in c)}", accept=accept)
            if ok:
                self.note(f"{label} (group {gi}) supports: {', '.join(names[x] for x in c)} -> {_short(ctx.current)}")
                return
        top = order[0][1][0] if order else None
        self.note(f"{label}: no support kept (best screened {_short(top)} vs {_short(ctx.current)})")

    def relaxed(self):
        """The scorer without the gear/tree-dependent hard floors (resistances, EHP, max hit, attributes):
        a naked build fails them all, so early choices (supports, ascendancy) are judged on the objective,
        while cost floors (life/mana sustain, Spirit) and constraints still apply."""
        drop = ("res.", "ehp", "max_hit", "attr_deficit")
        fl = [f for f in (self.scorer.floors or []) if f.tier != "hard" or not f.metric.startswith(drop)]
        return dataclasses.replace(self.scorer, floors=fl, _cache={})

    def sockets(self, final: bool = False):
        ctx = self.ctx
        full = ctx.scorer
        if not final:
            ctx.scorer = self.relaxed()
            ctx.refresh()
        try:
            self._sockets(final)
        finally:
            if ctx.scorer is not full:
                ctx.scorer = full
                ctx.refresh()

    def _sockets(self, final: bool):
        n = support_slots(self.sk.level)
        b = int(self.budget * SHARE["sockets"])
        targets = []
        if self.main_group:
            targets.append((self.main_group, "main"))
        if self.meta_group and self.meta_group != self.main_group:
            targets.append((self.meta_group, "meta"))
        if self.feeder_group:
            targets.append((self.feeder_group, "feeder"))
        for i, (gi, lab) in enumerate(targets):
            self.fill_supports(gi, n, max(50, b // len(targets)) if not final else 10 ** 9, lab)
        self.spirit()

    def spirit(self):
        """Disable extra skills (last first) while unreserved Spirit is negative."""
        ctx = self.ctx
        for gi in reversed(self.extra_groups):
            st = next(iter(ctx.current_stats.values())) or {}
            un = (st.get("spirit") or {}).get("unreserved")
            if un is None or un >= 0:
                return
            g = self._group_view(gi)
            if g is None or g.get("enabled") is False:
                continue
            ctx.commit({"group_enabled": {str(gi): False}}, label=f"spirit: disable {gi}")
            self.note(f"spirit over ({un}): disabled extra group {gi} ({(g.get('label') or '')})")

    # ------------------------------------------------------------------ 5. ascendancy
    def ascendancy(self):
        ctx = self.ctx
        full = ctx.scorer
        ctx.scorer = self.relaxed()
        ctx.refresh()
        try:
            self._ascendancy()
        finally:
            ctx.scorer = full
            ctx.refresh()

    def _ascendancy(self):
        ctx, e = self.ctx, self.e
        summ = e.summary().get("points") or {}
        left = int(summ.get("ascMax") or 0) - int(summ.get("ascUsed") or 0)
        if self.asc_points is not None:
            left = min(left, int(self.asc_points) - int(summ.get("ascUsed") or 0))
        stop = ctx.evals + max(30, int(self.budget * SHARE["ascendancy"]))
        forced = [x.lower() for x in self.sk.ascendancy_nodes or []]
        taken = []
        while left > 0 and ctx.evals < stop:
            tv = e.tree(frontier=True, max_dist=left)
            nodes = tv.get("nodes") or {}
            fr = [f for f in tv.get("frontier") or [] if (nodes.get(str(f["id"])) or {}).get("asc")
                  and f["dist"] <= left]
            if not fr:
                break
            pick = None
            for name in list(forced):
                hit = next((f for f in fr if (nodes[str(f["id"])].get("name") or "").lower() == name), None)
                if hit:
                    pick = hit
                    forced.remove(name)
                    break
            if pick is None:
                cands = [f for f in fr if nodes[str(f["id"])].get("kind") == "ascendancy_notable"] or fr
                changes = [{"add_nodes": list(f["path"])} for f in cands]
                scored = ctx.evaluate_many(changes)
                cur = ctx.current or {}

                def key(rf):
                    r, f = rf
                    if r is None:
                        return (False, -1e18)
                    d = (r.get("objective") or -1e18) - (cur.get("objective") or 0)
                    return (bool(r.get("feasible")) or not cur.get("feasible"), d / max(1, f["dist"]))
                best = max(zip(scored, cands), key=key)
                pick = best[1]
            ctx.commit({"add_nodes": list(pick["path"])}, label="ascendancy")
            taken.append(nodes[str(pick["id"])].get("name"))
            left -= int(pick["dist"])
        for name in forced:
            self.note(f"forced ascendancy node {name!r} not reachable")
        self.note(f"ascendancy: {', '.join(t for t in taken if t)} -> {_short(ctx.current)}")

    # ------------------------------------------------------------------ 6. tree
    def tree(self):
        from exilegen.optimise.tree import TreeStep
        ctx, e = self.ctx, self.e
        pins = []
        if self.sk.keystones:
            tv = e.tree(frontier=True, max_dist=60)
            nodes = tv.get("nodes") or {}
            byname = {}
            for f in tv.get("frontier") or []:
                n = nodes.get(str(f["id"])) or {}
                if n.get("kind") == "keystone":
                    byname.setdefault((n.get("name") or "").lower(), f)
            for k in self.sk.keystones:
                f = byname.get(k.lower())
                if f is None:
                    self.note(f"keystone {k!r} not found on the tree")
                    continue
                ctx.commit({"add_nodes": list(f["path"])}, label=f"keystone {k}")
                pins.append(int(f["id"]))
            if pins:
                self.note(f"keystones pinned: {', '.join(self.sk.keystones)} -> {_short(ctx.current)}")
        pts = e.summary().get("points") or {}
        avail = int(pts.get("available") or 0)
        alloc = e.tree(frontier=False).get("allocated") or []
        share = max(300, int(self.budget * SHARE["tree"]))
        seed_tree = self._seed_tree()
        if seed_tree:
            share = max(300, share // 2)       # repairing a parent's tree needs less than growing one
        step = TreeStep(mode="scratch", max_evals=share, pins=pins, extra_points=max(0, avail - len(alloc)),
                        seed=self.seed, verbose=False, seed_tree=seed_tree)
        r = self._run(step, share)
        rep = getattr(step, "report", None) or {}
        cand = rep.get("candidate") or {}
        if rep.get("fallback") and cand.get("nodes"):
            # TreeStep compares the applied tree with its start (feasibility first); from scratch the start is
            # an empty tree, which must never win: commit the grown tree anyway, local search continues later
            S = {int(v): 0 for v in cand["nodes"]}
            A = {int(k): int(v) for k, v in (cand.get("attrs") or {}).items()}
            change = step._diff(S, A)
            if change:
                ctx.commit(change, label="tree_scratch(forced)")
                self.note(f"tree scratch fell back to the empty start tree; committed its tree anyway "
                          f"({len(S)} nodes)")
        self.note(f"tree scratch: {avail} points, {r.evals} evals, {r.seconds:.0f}s -> {_short(ctx.current)}"
                  + (f" [{'; '.join(str(n) for n in r.notes[:2])}]" if r.notes and not r.improved else ""))

    def _seed_tree(self) -> dict | None:
        """Main-tree nodes and attribute choices of seed_code (the caller passes one of the same class),
        read by loading it and then restoring the current build."""
        if not self.seed_code:
            return None
        e, tv = self.e, None
        c = e.raw("save_build_code", {})
        cur = c.get("code") if isinstance(c, dict) else c
        try:
            e.load(code=self.seed_code, profile=self.profiles[0])
            tv = e.tree(frontier=False, ascendancy=False)
        except Exception as ex:          # noqa: BLE001 - no seed: grow from scratch
            self.note(f"seed tree: could not read the parent build: {str(ex)[:120]}")
        finally:
            e.load(code=cur, profile=self.profiles[0], main=dict(self.main_ref))
            self.ctx._skills = None
            self.ctx.refresh()
        nodes = [int(v) for v in (tv or {}).get("allocated") or []]
        if not nodes:
            return None
        self.note(f"seed tree: starting from the parent's {len(nodes)} nodes")
        return {"nodes": nodes, "attrs": tv.get("attributeChoices") or {}}

    # ------------------------------------------------------------------ 7. gear optimise
    def gear(self, final: bool = True):
        """optimise_gear with objective-derived weights; on the final pass also the 'balanced' and 'defence'
        presets (a build short of the EHP / max-hit floors needs defence-weighted affixes). Each variant
        starts from the same gear; the best by the Scorer (feasibility first) is kept, or the input gear if
        none beats it."""
        ctx = self.ctx
        code0 = self.e.raw("save_build_code", {})
        code0 = code0.get("code") if isinstance(code0, dict) else code0
        best = (ctx.current, code0, "input")
        best_stats = ctx.current_stats
        variants = [("objective", _objective_weights(self.scorer), None)]
        if final:
            variants += [("balanced", None, "balanced"), ("defence", None, "defence")]
        for i, (name, w, preset) in enumerate(variants):
            if i:
                self.e.load(code=code0, profile=self.profiles[0], main=dict(self.main_ref))
            try:
                out = self.gp.optimise(weights=w, preset=preset)
            except Exception as ex:
                self.note(f"optimise_gear {name} failed: {str(ex)[:160]}")
                continue
            ctx._skills = None
            ctx.refresh()
            self.note(f"optimise_gear {name}: {len(out['equipped'])} slots -> {_short(ctx.current)}")
            if not self.main_ok() and self.main_ok(best_stats):
                self.note(f"optimise_gear {name}: main skill lost, variant skipped")
                continue
            if Context.better(ctx.current, best[0], 1e-6):
                c = self.e.raw("save_build_code", {})
                best = (ctx.current, c.get("code") if isinstance(c, dict) else c, name)
            if ctx.current.get("feasible") and name == "objective" and best[2] == name:
                break          # feasible with the scorer's own weighting: no defence pass needed
        self.e.load(code=best[1], profile=self.profiles[0], main=dict(self.main_ref))
        self.note(f"gear kept: {best[2]}")
        self.checkpoint()

    # ------------------------------------------------------------------ 7b. unique trials
    def unique_trials(self):
        """Make each forced unique earn its slot: swap in a template rare, tune it with optimise_gear and
        compare (feasibility first, then objective) with the unique.

        Every trial is recorded (BuildResult.unique_trials: unique, slot, role, gain = DPS-weighted objective
        ratio rare/unique, feasible either way, action). A unique the skeleton cannot do without (it grants
        the main skill or the trigger, or a constraint requires it) is never tried. One that defines the
        skeleton's thesis (build_around, chain, anti_meta) is measured but kept, so the archive still holds
        that idea and its cost is visible. Any other (mutation swaps) is replaced when the rare wins.
        Weapon slots are skipped: a different weapon base changes what the main skill can use."""
        from .gear import ARMOUR_SLOTS, JEWELLERY_SLOTS
        if self.unique_trial == "off":
            return
        ctx, gp = self.ctx, self.gp
        trig = (self.sk.trigger or {}).get("source")
        from exilegen.scoring.constraints import Constraint
        required = set()
        for c in getattr(self.sk, "constraints", None) or []:
            try:
                cc = Constraint.parse(c)
            except Exception:          # noqa: BLE001
                continue
            if cc.type == "require_unique" and cc.arg:
                required.add(cc.arg)
        keep_roles = {"build_around", "chain", "anti_meta"}
        weights = _objective_weights(self.scorer)
        roles, grants = {}, {}
        for u in self.sk.uniques or []:
            d = {"name": u} if isinstance(u, str) else u
            roles[d["name"]] = d.get("role", "")
            grants[d["name"]] = {str(x) for x in d.get("grants") or []}
        used = {str(self.sk.main_skill), str(self.main_info.skill_id or "")} | {str(x) for x in self.sk.extra_skills or []} \
            | {str(x) for x in (self.sk.trigger or {}).get("spells") or []}
        for slot, val in sorted(gp.taken.items()):
            if not str(val).startswith("unique:") or slot.startswith("Weapon"):
                continue
            name = str(val).split(":", 1)[1]
            role = roles.get(name, "")
            if role == "grants_main" or name == trig or name in required or grants.get(name, set()) & used:
                continue                     # the build needs it: it grants a used skill or is required
            before = ctx.current
            c0 = self.e.raw("save_build_code", {})
            code0 = c0.get("code") if isinstance(c0, dict) else c0
            trial = {"unique": name, "slot": slot, "role": role}
            try:
                if slot in ARMOUR_SLOTS:
                    tb = gp.armour_base(slot)
                    made = tb and gp.craft(slot, tb[0], tb[1], "armour", title=f"Trial {slot}")
                elif slot in JEWELLERY_SLOTS:
                    tb = gp.jewellery_base(slot)
                    made = tb and gp.craft(slot, tb[0], tb[1], "Ring" if slot.startswith("Ring") else slot,
                                           title=f"Trial {slot}")
                else:
                    made = None
                if not made:
                    raise SynthError("no template base")
                gp.optimise(weights=weights, slots=[slot])
                ctx._skills = None
                ctx.refresh()
                after = ctx.current
            except Exception as ex:          # noqa: BLE001 - a failed trial keeps the unique
                after = None
                trial["error"] = str(ex)[:120]
            o0, o1 = (before or {}).get("objective"), (after or {}).get("objective")
            trial["gain"] = round(math.exp(max(-50.0, min(50.0, o1 - o0))), 4) if o0 is not None and o1 is not None else None
            trial["feasible_unique"] = bool((before or {}).get("feasible"))
            trial["feasible_rare"] = bool((after or {}).get("feasible")) if after else None
            rare_wins = after is not None and Context.better(after, before, 1e-4)
            if rare_wins and not self.main_ok():
                rare_wins = False
                trial["main_lost"] = True        # the unique grants (or enables) the main skill
            if rare_wins and role not in keep_roles:
                trial["action"] = "replaced"
                self.note(f"unique trial: {name} ({slot}) replaced by a tuned rare, objective x{trial['gain']}")
                self.checkpoint()
            else:
                trial["action"] = "kept" + (" (thesis)" if rare_wins else "")
                self.e.load(code=code0, profile=self.profiles[0], main=dict(self.main_ref))
                gp.taken[slot] = val
                ctx._skills = None
                ctx.refresh()
                self.note(f"unique trial: {name} ({slot}) kept; a tuned rare scores x{trial['gain']}"
                          + (" (rare is better, kept as the skeleton's thesis)" if rare_wins else ""))
            self.trials.append(trial)

    # ------------------------------------------------------------------ 8. polish
    def _run(self, step, share: int) -> StepResult:
        """Run one optimiser step with a budget share; roll it back if it ends worse (pipeline guard)."""
        ctx, e = self.ctx, self.e
        cap = ctx.budget
        ctx.budget = min(cap, ctx.evals + max(1, share))
        ev0, ts = ctx.evals, time.time()
        before, before_stats, n_hist = ctx.current, ctx.current_stats, len(ctx.history)
        obj0 = (before or {}).get("objective")
        tok = e.snapshot()
        try:
            r = step.run(ctx)
        except Exception as ex:          # noqa: BLE001
            self.note(f"step {getattr(step, 'name', step)} failed: {type(ex).__name__}: {ex}")
            if self.log_fn:
                self.log_fn(traceback.format_exc())
            try:
                ctx.refresh()
            except Exception:
                pass
            r = StepResult(getattr(step, "name", "?"), False, obj0, (ctx.current or {}).get("objective"),
                           notes=[f"failed: {ex}"])
        finally:
            ctx.budget = cap
        try:
            ctx._skills = None
            ctx.refresh()
            lost = self.main_ok(before_stats) and not self.main_ok()
            if lost or Context.better(before, ctx.current, 1e-6):
                e.restore(tok)
                ctx.current, ctx.current_stats, ctx._skills = before, before_stats, None
                del ctx.history[n_hist:]
                r.notes.append("rolled back: " + ("the damage moved to another skill" if lost
                                                  else "ended worse than it started"))
                r.improved, r.changes = False, []
        finally:
            ctx._drop(tok)
        want = fields_for(ctx.skills())
        if want != "core" and ctx.fields == "core":
            ctx.fields = want
            ctx.refresh()
        r.evals = r.evals or (ctx.evals - ev0)
        r.seconds = r.seconds or (time.time() - ts)
        return r

    def polish_steps(self):
        """Run the polish steps, then repeat them on whatever budget the steps left unused while a cycle
        still gains >= CYCLE_MIN_GAIN, so a tree change can unlock a better support and vice versa. The
        first cycle splits the whole polish budget as before: reserving budget for later cycles starved
        the first one and cost ~25% DPS on a 16-skeleton A/B."""
        from exilegen.optimise.pipeline import CYCLE_MIN_GAIN, _gain, make_step
        ctx = self.ctx
        for cyc in range(max(1, self.polish_cycles)):
            if ctx.budget_left() <= 50:
                if cyc == 0:
                    self.note("budget exhausted before polish")
                break
            c0 = ctx.current
            todo = [s for s in self.polish if s != "triggers" or has_meta_groups(ctx.skills())]
            wts = [REPEAT_WEIGHT if s in todo[:j] else STEP_WEIGHTS.get(s, 1.0) for j, s in enumerate(todo)]
            for i, name in enumerate(todo):
                left = ctx.budget_left()
                if left <= 50:
                    if cyc == 0:
                        self.note(f"budget exhausted before {name}")
                    break
                share = max(50, int(left * wts[i] / max(1e-9, sum(wts[i:]))))
                step = make_step(name, None if self.seed is None else self.seed + cyc, share if name.startswith("tree") else None, self.notes)
                if step is None:
                    continue
                if hasattr(step, "verbose"):
                    step.verbose = False
                r = self._run(step, share)
                rb = any("rolled back" in str(n) for n in r.notes)
                self.note(f"{name}: {'rolled back' if rb else ('improved' if r.improved else 'no gain')}, "
                          f"{r.evals} evals, {r.seconds:.0f}s -> {_short(ctx.current)}")
            g = _gain(c0, ctx.current)
            self.note(f"polish cycle {cyc + 1}: gain {g:.2%}")
            if g < CYCLE_MIN_GAIN:
                break

    # ------------------------------------------------------------------ run
    def run(self) -> BuildResult:
        t0 = time.time()
        res = BuildResult(skeleton=self.sk, ok=False, notes=self.notes)
        phases = [("class", self.make_class), ("uniques", self.uniques), ("skills", self.make_skills), ("gear", self.make_gear),
                  ("load", self.pin_and_load), ("sockets", self.sockets), ("ascendancy", self.ascendancy),
                  ("tree", self.tree)]
        if self.gear_optimise:
            phases.insert(phases.index(("tree", self.tree)), ("gear_pre", lambda: self.gear(final=False)))
            phases.append(("gear_opt", self.gear))
            phases.append(("unique_trials", self.unique_trials))
        phases.append(("sockets2", lambda: self.sockets(final=True)))
        phases.append(("polish", self.polish_steps))
        phase = None
        try:
            for phase, fn in phases:
                ts = time.time()
                fn()
                self.timing[phase] = round(time.time() - ts, 1)
            ctx = self.ctx
            ctx._skills = None
            ctx.refresh()
        except Exception as ex:          # noqa: BLE001 - a failed skeleton is a result, not a crash
            res.error = f"{phase}: {type(ex).__name__}: {ex}"
            self.note(f"FAILED in {phase}: {type(ex).__name__}: {ex}")
            if self.log_fn and not isinstance(ex, SynthError):
                self.log_fn(traceback.format_exc())
        ctx = self.ctx
        if ctx is not None and ctx.current is not None:
            try:
                res.score = ctx.current
                res.stats = ctx.current_stats
                res.skills = ctx.skills()
                if res.error:
                    ctx._skills = None
                    ctx.refresh()
                    res.score, res.stats, res.skills = ctx.current, ctx.current_stats, ctx.skills()
                res.code = self.e.export()
                res.ok = bool(res.code)          # loadable and scored, even if a later phase failed
            except Exception as ex:      # noqa: BLE001
                self.note(f"final export failed: {ex}")
                res.ok = False
                res.error = res.error or f"export: {ex}"
            res.evals = ctx.evals
        self.note("timing: " + ", ".join(f"{k} {v:.0f}s" for k, v in self.timing.items()))
        res.seconds = time.time() - t0
        res.unique_trials = list(self.trials)
        return res


def synthesize(skeleton: Skeleton, engine: Any, scorer: Any = None, budget: int = 6000, log_fn=None,
               gear_optimise: bool = True, polish=POLISH, seed: int = 0,
               polish_cycles: int = POLISH_CYCLES, exclude_supports=None,
               asc_points: int | None = None, unique_trial: str = "on",
               seed_code: str | None = None) -> BuildResult:
    """contracts.Synthesizer: build `skeleton` from nothing in `engine`, judged by `scorer` (default:
    pinnacle_v1 + mapping_v1, DEFAULT_FLOORS, no constraints). Never raises for a bad skeleton: the result
    has ok=False and error set. `budget` is in engine evaluations. `exclude_supports` are support gem display
    names no search step may socket (supportgems.banned); `asc_points` caps the ascendancy points spent, for
    a character below the last trial. Both default to no restriction."""
    from .supportgems import banned
    with banned(engine, exclude_supports):
        return _Synth(skeleton, engine, scorer, budget, log_fn=log_fn, gear_optimise=gear_optimise,
                      polish=polish, seed=seed, polish_cycles=polish_cycles, asc_points=asc_points,
                      unique_trial=unique_trial, seed_code=seed_code).run()


def summary_row(res: BuildResult) -> dict:
    """Flat facts of a result for tables: honest/raw DPS and EHP per profile, feasibility, violations."""
    s = res.score or {}
    per = s.get("profiles") or ({s.get("profile"): s} if s else {})
    row = {"id": res.skeleton.id, "ok": res.ok, "feasible": s.get("feasible"), "objective": s.get("objective"),
           "dps_honest": s.get("dps_honest"), "ehp": s.get("ehp"), "evals": res.evals,
           "seconds": round(res.seconds, 1), "error": res.error,
           "violations": ";".join(sorted({f"{v.get('profile', '')}:{v.get('entry')}" for v in s.get("violations") or []}))}
    for p, r in per.items():
        st = (res.stats or {}).get(p) if isinstance(res.stats, dict) else None
        row[f"{p}.dps_honest"] = r.get("dps_honest")
        row[f"{p}.dps_raw"] = r.get("dps_raw")
        row[f"{p}.ehp"] = r.get("ehp")
        if st:
            row[f"{p}.main"] = (st.get("mainSkill") or {}).get("name")
            row[f"{p}.pob_dps"] = (st.get("dps") or {}).get("best")
    return row


def _finite(x):
    return x if isinstance(x, (int, float)) and math.isfinite(x) else None
