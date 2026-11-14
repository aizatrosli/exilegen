"""1-button / trigger optimisation: TriggerStep (tune Meta+Triggers setups) and ConvertStep (manual caster ->
1-button build). Port of the one-button POC (tools/poc/one-button/lib.lua) onto the engine + Scorer.

Every candidate is judged by the Scorer (energy-corrected, condition-discounted honest DPS, floors,
constraints): raw PoB DPS picks the wrong trigger spells (POC: Hexblast x3.5 raw, x1.67 honest).

TriggerStep moves, per enabled, player-socketed meta group with a modelled meta (Cast on Critical,
Cast on Elemental Ailment):
  spell      replace each socketed spell with another triggerable damaging spell
  meta       swap the meta gem for the other modelled meta
  supports   replace meta-group supports (spell supports + energy supports such as Boundless Energy)
  feeder     replace the feeder skill (the engine's energy feeder) with a fast-hitting spell
  feeder supports  replace / fill the feeder's supports (they drive crits / ailments -> energy)
Spirit-aware: a candidate whose only violation is the Spirit floor is retried together with disabling
a persistent group, cheapest (smallest objective loss) first; other floors must still pass.

Search shape (POC trySlot): screen every candidate with a what-if on the first profile, verify the
best few with an exact apply (real skills view, every profile) and keep the first that beats the
current build (Context.try_commit). Large support lists are pre-screened with the group's skill as the
main skill (one calc instead of one per damaging skill).

ConvertStep: when a `one_button` constraint is violated, socket a modelled meta gem with the main
spell, add a feeder (an existing manual spell or a new Spark group), disable the other manual damaging
groups, free Spirit by disabling persistent groups, fill the feeder's supports; keep the best combo
that is feasible (constraints + floors), otherwise leave the build unchanged.
"""
from __future__ import annotations

import time

from exilegen.engine import EngineError

from .base import TRIGGER_FIELDS, Context, StepResult, change_only

PRESCREEN_PER_EVAL = 4  # a pre-screen what-if is one calc (~1/5 of an energy evaluation): 4 count as 1
MAX_GEMS = 6           # gems per socket group (active + 5 supports)
EPS = 1e-6


# ---------------------------------------------------------------------------------------------- helpers
def _p0(ctx: Context) -> str:
    """Screening profile: the binding one (lowest objective) of the committed build, else the first."""
    profs = ctx._profiles()
    b = (ctx.current or {}).get("binding_profile")
    return b if b in profs else profs[0]


def _ensure_energy(ctx: Context) -> None:
    f = ctx.fields
    if f == "all" or (isinstance(f, (list, tuple)) and "energy" in f):
        return
    ctx.fields = list(TRIGGER_FIELDS)
    ctx.refresh()


def _stats0(ctx: Context) -> dict:
    st = ctx.current_stats or {}
    return st.get(_p0(ctx)) or next(iter(st.values()), {})


def _groups(ctx: Context) -> dict:
    return {g["index"]: g for g in ctx.skills().get("groups") or []}


def _meta_gem(g: dict) -> dict | None:
    return next((x for x in g.get("gems") or [] if x.get("meta") and x.get("enabled", True) is not False), None)


def _supports(g: dict) -> list[dict]:
    return [x for x in g.get("gems") or [] if x.get("support")]


def _spirit_only(res: dict) -> bool:
    v = res.get("violations") or []
    return bool(v) and all(x.get("rule") == "floor" and x.get("entry") in ("spirit",) for x in v)


def _short(res: dict | None) -> str:
    if not res:
        return "-"
    bad = "" if res.get("feasible") else " INFEASIBLE(" + ", ".join(sorted({
        str(v.get("entry")) + (":" + str(v.get("detail"))[:60] if v.get("rule") == "constraint" else "")
        for v in res.get("violations") or []})) + ")"
    return f"obj {res.get('objective', 0):.3f} honest {res.get('dps_honest', 0):,.0f}{bad}"


def screen(ctx: Context, changes: list[tuple[str, dict]], profile: str | None = None) -> list[tuple[dict, str, dict]]:
    """What-if + score every (label, change) on one profile. -> [(result, label, change)] best first."""
    prof = profile or _p0(ctx)
    changes = changes[:max(0, ctx.budget_left())]
    if not changes:
        return []
    rs = ctx.evaluate_many([c for _, c in changes], profiles=[prof])
    out = [(r, l, c) for r, (l, c) in zip(rs, changes) if r is not None]
    out.sort(key=lambda t: (bool(t[0].get("feasible")), t[0].get("objective") or float("-inf")), reverse=True)
    return out


def prescreen_main(ctx: Context, main, changes: list[tuple[str, dict]], metric) -> list[tuple[float, str, dict]]:
    """Cheap screen: make `main` ({group, skill}) the main skill and rank changes by metric(whatif core
    stats). One calc per change instead of one per damaging skill. State is restored afterwards."""
    eng = ctx.engine
    prev_main = getattr(eng, "_main", None)
    tok = eng.snapshot()
    out = []
    try:
        eng.select_main(main)
        changes = changes[:max(0, ctx.budget_left())]
        for i in range(0, len(changes), 64):
            part = changes[i:i + 64]
            try:
                ws = eng.whatif_many([change_only(c) for _, c in part], fields=["dps", "offence", "main", "spirit"],
                                     skip_ehp=True)
            except EngineError:
                continue
            ctx.evals += (len(part) + PRESCREEN_PER_EVAL - 1) // PRESCREEN_PER_EVAL
            for w, (label, ch) in zip(ws, part):
                if not isinstance(w, dict) or "err" in w:
                    continue
                try:
                    out.append((float(metric(w)), label, ch))
                except Exception:
                    continue
    finally:
        eng.restore(tok)
        eng._main = prev_main
        ctx._drop(tok)
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def persistent_groups(ctx: Context, protect: set) -> list[int]:
    """Enabled, player-socketed, non-meta groups whose skills are all persistent (auras, buffs, heralds...)."""
    out = []
    for gi, g in _groups(ctx).items():
        if gi in protect or g.get("enabled") is False or g.get("grantedBy") or g.get("metaGroup"):
            continue
        sk = [s for s in g.get("skills") or [] if s.get("enabled", True) is not False]
        # persistent (reserving) skills; a reservation skill with a manual non-damaging part (Blink) counts
        if sk and any(s.get("activation") == "persistent" for s in sk) and \
                not any(s.get("activation") == "manual" and s.get("damaging") for s in sk):
            out.append(gi)
    return out


def spirit_relief_options(ctx: Context, change: dict, protect: set, profile: str | None = None, limit: int = 4):
    """change + disabling one persistent group, screened; feasible ones first (cheapest = best objective)."""
    opts = []
    for gi in persistent_groups(ctx, protect):
        ch = dict(change)
        ge = dict(ch.get("group_enabled") or {})
        ge[gi] = False
        ch["group_enabled"] = ge
        opts.append((f"disable g{gi}", ch))
    return screen(ctx, opts[:max(limit, 0) or None], profile)


def relieve_spirit(ctx: Context, protect: set, max_disable: int = 3, notes: list | None = None) -> list[dict]:
    """While the committed build's only problem is a Spirit floor violation, disable the persistent group
    whose loss costs least (never breaking another floor). -> committed changes."""
    done = []
    for _ in range(max_disable):
        cur = ctx.current or {}
        if cur.get("feasible") or not any(v.get("entry") == "spirit" for v in cur.get("violations") or []):
            break
        opts = []
        for gi in persistent_groups(ctx, protect):
            opts.append((f"disable g{gi}", {"group_enabled": {gi: False}}))
        ranked = _screen_all(ctx, opts)
        sp0 = _spirit(cur)
        pick = None
        for r, label, ch in ranked:
            other = [v for v in r.get("violations") or [] if v.get("entry") != "spirit" and v.get("rule") == "floor"]
            sp = _spirit(r)
            if other or sp is None or sp0 is None or sp <= sp0:
                continue        # must free Spirit and keep every other floor
            fixed = not any(v.get("entry") == "spirit" for v in r.get("violations") or [])
            key = (bool(r.get("feasible")), fixed, r.get("objective") or float("-inf"))
            if pick is None or key > pick[0]:
                pick = (key, label, ch)
        if pick is None:
            break
        name = _group_name(ctx, next(iter(pick[2]["group_enabled"])))
        ctx.commit(pick[2], f"spirit: disable {name}")
        done.append(pick[2])
        if notes is not None:
            notes.append(f"disabled {name} for Spirit")
    return done


def _spirit(res: dict | None) -> float | None:
    """Lowest unreserved Spirit over the profiles of a score result (from its floor checks)."""
    if not res:
        return None
    per = res.get("profiles") or {None: res}
    vals = [f.get("value") for r in per.values() for f in r.get("floors") or [] if f.get("id") == "spirit"]
    vals = [v for v in vals if isinstance(v, (int, float))]
    return min(vals) if vals else None


def _screen_all(ctx: Context, changes):
    changes = changes[:max(0, ctx.budget_left())]
    rs = ctx.evaluate_many([c for _, c in changes]) if changes else []
    out = [(r, l, c) for r, (l, c) in zip(rs, changes) if r is not None]
    out.sort(key=lambda t: (bool(t[0].get("feasible")), t[0].get("objective") or float("-inf")), reverse=True)
    return out


def _group_name(ctx: Context, gi) -> str:
    g = _groups(ctx).get(int(gi)) or {}
    act = next((x.get("name") for x in g.get("gems") or [] if not x.get("support")), None)
    return f"{act or '?'} (g{gi})"


def _support_candidates(ctx: Context, gi: int, slot: int | None, extra_energy: bool) -> list[dict]:
    """Legal supports for group gi as a replacement for `slot` (None = an added slot)."""
    try:
        vs = ctx.engine.valid_supports(gi)
    except EngineError:
        return []
    g = _groups(ctx).get(gi) or {}
    present = {x.get("skillId") for x in g.get("gems") or []}
    out = []
    for s in vs.get("supports") or []:
        if s.get("socketed") or s.get("skillId") in present:
            continue
        conf = {c.get("slot") for c in s.get("conflicts") or []}
        if conf and (slot is None or conf != {slot}):
            continue
        out.append({"gem": s.get("skillId") or s.get("id"), "name": s.get("name")})
    if extra_energy:
        have = {c["gem"] for c in out}
        used = _used_supports(ctx)
        stems = {_stem(x.get("name")) for x in g.get("gems") or [] if x.get("support")}
        for e in ctx.gems().energy_supports():
            if e.skill_id in have or e.skill_id in present or e.skill_id in used:
                continue
            if _stem(e.name) in stems and (slot is None or _stem(e.name) != _stem(_slot_name(g, slot))):
                continue
            out.append({"gem": e.skill_id, "name": e.name, "energy": True})
    return out


def _level_quality(g: dict, slot) -> dict:
    """Keep the replaced active gem's level and quality (a new gem object would get PoB defaults)."""
    cur = next((x for x in g.get("gems") or [] if x.get("slot") == slot), None) or {}
    return {k: cur[k] for k in ("level", "quality") if cur.get(k) is not None}


def _stem(name) -> str:
    """Support family stem: 'Boundless Energy II' -> 'boundless energy'."""
    parts = (name or "").lower().split()
    while parts and parts[-1] in ("i", "ii", "iii", "iv", "v"):
        parts.pop()
    return " ".join(parts)


def _slot_name(g: dict, slot) -> str | None:
    return next((x.get("name") for x in g.get("gems") or [] if x.get("slot") == slot), None)


def _used_supports(ctx: Context) -> set:
    return {x.get("skillId") for g in ctx.skills().get("groups") or [] if g.get("enabled") is not False
            for x in g.get("gems") or [] if x.get("support") and x.get("enabled", True) is not False}


# ---------------------------------------------------------------------------------------------- cost correction
COST_KEYS = ("manaUsePerSec", "manaPerSecondCost", "lifeUsePerSec", "lifePerSecondCost")


def energy_cost_filter(scorer):
    """Stats filter (Context.stats_filter): when the main skill is a spell triggered by a modelled meta gem,
    PoB prices its cost at PoB's own trigger rate; scale the main skill's cost per second by the Energy
    factor (scoring.energy model, the profile's enemy Power). Without it the relative/absolute mana floors
    reject every faster-casting triggered spell (cost at PoB's 2-3 casts/s that Energy never allows)."""
    from exilegen.scoring import energy as E
    from exilegen.scoring.adapter import BuildView

    def f(doc, profile):
        if not isinstance(doc, dict) or not doc.get("energy") or not doc.get("sustain"):
            return doc
        main = doc.get("main") or {}
        eg = next((x for x in doc["energy"] if x.get("group") == main.get("group")), None)
        if not eg:
            return doc
        trig = next((t for t in eg.get("triggered") or [] if t.get("index") == main.get("skill")), None)
        if not trig or not trig.get("pobRate"):
            return doc
        try:
            power, _ = scorer.profiles.param(profile, "enemy_power")
            groups = BuildView({"energy": [eg]}, None, profile).energy_groups() or []
            g = groups[0]
            eps = E.energy_per_sec(g, float(power or 20))
            mx = E.max_energy(g)
        except Exception:
            return doc
        if eps is None or not mx:
            return doc
        factor = min(1.0, (eps / mx) / float(trig["pobRate"]))
        if factor >= 1.0:
            return doc
        sus = dict(doc["sustain"])
        for k in COST_KEYS:
            if isinstance(sus.get(k), (int, float)):
                sus[k] = sus[k] * factor
        sus["energyCostFactor"] = factor
        return dict(doc, sustain=sus)
    return f


# ---------------------------------------------------------------------------------------------- TriggerStep
class TriggerStep:
    name = "triggers"

    def __init__(self, spells: bool = True, metas: bool = True, meta_supports: bool = True, feeder: bool = True,
                 feeder_supports: bool = True, spirit: bool = True, top_k: int = 3, prescreen_k: int = 6,
                 passes: int = 1, min_gain: float = 1e-4):
        self.spells, self.metas, self.meta_supports = spells, metas, meta_supports
        self.feeder, self.feeder_supports, self.spirit = feeder, feeder_supports, spirit
        self.top_k, self.prescreen_k, self.passes, self.min_gain = top_k, prescreen_k, passes, min_gain

    @staticmethod
    def applicable(ctx: Context) -> bool:
        from .base import has_meta_groups
        return has_meta_groups(ctx.skills())

    # -------------------------------------------------------------- facts
    def _energy(self, ctx) -> list[dict]:
        return [e for e in (_stats0(ctx).get("energy") or [])]

    def _meta_groups(self, ctx) -> list[dict]:
        """Energy entries of groups we may edit (player-socketed, enabled, modelled meta)."""
        groups = _groups(ctx)
        cat = ctx.gems()
        out = []
        for e in self._energy(ctx):
            g = groups.get(e.get("group"))
            if not g or g.get("grantedBy") or g.get("enabled") is False:
                continue
            if e.get("metaSkillId") not in cat_modelled(cat):
                continue
            out.append(e)
        return out

    # -------------------------------------------------------------- verification
    def _verify(self, ctx, ranked, label_prefix, res: StepResult, protect: set) -> bool:
        """Try the best screened candidates exactly; spirit-only failures are retried with a disable."""
        cur0 = ctx.current_for(_p0(ctx))
        if ranked:
            r, label, _ = ranked[0]
            ctx.log(f"  {label_prefix}: {len(ranked)} screened; best {label}: {_short(r)} vs current {_short(cur0)}")
        tried = 0
        spirit_retry = []
        for r, label, ch in ranked:
            if tried >= self.top_k or ctx.budget_left() <= 0:
                break
            if not r.get("feasible"):
                if self.spirit and _spirit_only(r) and (r.get("objective") or 0) > (cur0.get("objective") or 0) + self.min_gain:
                    spirit_retry.append((r, label, ch))
                continue
            if not Context.better(r, cur0, self.min_gain):
                break
            tried += 1
            ok, now = ctx.try_commit(ch, f"{label_prefix} {label}", min_gain=self.min_gain)
            res.evals += 1
            if ok:
                res.changes.append(dict(ch, label=f"{label_prefix} {label}"))
                ctx.log(f"  + {label_prefix} {label}: {_short(now)}")
                return True
        for r, label, ch in spirit_retry[:2]:
            for r2, l2, ch2 in spirit_relief_options(ctx, ch, protect):
                if not r2.get("feasible") or not Context.better(r2, cur0, self.min_gain):
                    continue
                ok, now = ctx.try_commit(ch2, f"{label_prefix} {label} + {l2}", min_gain=self.min_gain)
                if ok:
                    res.changes.append(dict(ch2, label=f"{label_prefix} {label} + {l2} (Spirit)"))
                    ctx.log(f"  + {label_prefix} {label} + {l2}: {_short(now)}")
                    return True
                break
        return False

    # -------------------------------------------------------------- moves
    def _spell_moves(self, ctx, e, res, protect) -> bool:
        gi = e["group"]
        improved = False
        cat = ctx.gems()
        for sp in list(e.get("spells") or []):
            slot = sp.get("slot")
            if slot is None or ctx.budget_left() <= 0:
                continue
            cur_id = sp.get("id")
            lq = _level_quality(_groups(ctx).get(gi) or {}, slot)
            cands = [(f"g{gi}:{slot} {sp.get('name')} -> {c.name}",
                      {"gem_changes": [dict({"group": gi, "slot": slot, "gem": c.skill_id}, **lq)]})
                     for c in cat.trigger_spells() if c.skill_id != cur_id]
            ranked = screen(ctx, cands)
            if self._verify(ctx, ranked, "spell", res, protect):
                improved = True
        return improved

    def _meta_moves(self, ctx, e, res, protect) -> bool:
        gi = e["group"]
        g = _groups(ctx).get(gi) or {}
        mg = _meta_gem(g)
        if not mg:
            return False
        lq = _level_quality(g, mg["slot"])
        cands = [(f"g{gi} {mg.get('name')} -> {m.name}",
                  {"gem_changes": [dict({"group": gi, "slot": mg["slot"], "gem": m.skill_id}, **lq)]})
                 for m in ctx.gems().metas(True) if m.skill_id != mg.get("skillId")]
        return self._verify(ctx, screen(ctx, cands), "meta", res, protect)

    def _support_moves(self, ctx, gi, main, res, protect, label, energy: bool, metric) -> bool:
        """Replace each support of group gi, or fill an empty socket. Candidates are pre-screened with
        `main` as the main skill using `metric`, then screened honestly."""
        improved = False
        g = _groups(ctx).get(gi) or {}
        slots = [x["slot"] for x in _supports(g)]
        n = len(g.get("gems") or [])
        targets = [(s, False) for s in slots]
        if n < MAX_GEMS:
            targets.append((max([x["slot"] for x in g.get("gems") or []] or [0]) + 1, True))
        for slot, add in targets:
            if ctx.budget_left() <= 0:
                break
            g = _groups(ctx).get(gi) or {}
            cur = next((x for x in g.get("gems") or [] if x.get("slot") == slot), None)
            cands = _support_candidates(ctx, gi, None if add else slot, energy)
            changes = [(f"g{gi}:{slot} {(cur or {}).get('name', '+')} -> {c['name']}",
                        {"gem_changes": [{"group": gi, "slot": slot, "gem": c["gem"]}]}) for c in cands]
            energy_changes = [x for x, c in zip(changes, cands) if c.get("energy")]
            other = [x for x, c in zip(changes, cands) if not c.get("energy")]
            pre = prescreen_main(ctx, main, other, metric) if main is not None else [(0.0, l, c) for l, c in other]
            short = [(l, c) for _, l, c in pre[:self.prescreen_k]] + energy_changes
            if not add and cur is not None:
                short.append((f"g{gi}:{slot} drop {cur.get('name')}",
                              {"gem_changes": [{"group": gi, "slot": slot, "gem": None}]}))
            ranked = screen(ctx, short)
            if self._verify(ctx, ranked, label, res, protect):
                improved = True
        return improved

    def _feeder(self, ctx) -> dict | None:
        for e in self._energy(ctx):
            f = e.get("feeder")
            if f and f.get("group") is not None:
                return f
        return None

    def _feeder_slot(self, ctx, f) -> int | None:
        g = _groups(ctx).get(f["group"]) or {}
        if g.get("grantedBy"):
            return None
        for s in g.get("skills") or []:
            if s.get("index") == f.get("index") and s.get("gemSlot") is not None:
                return s["gemSlot"]
        for x in g.get("gems") or []:
            if x.get("skillId") == f.get("id"):
                return x["slot"]
        return None

    def _feeder_metric(self, ctx):
        kinds = set()
        for e in self._meta_groups(ctx):
            kinds |= set((e.get("perEvent") or {}).keys())

        def metric(w):
            o = w.get("offence") or {}
            hps = o.get("hitsPerSec") or 0.0
            ev = 0.0
            if "crit" in kinds:
                ev += hps * min(100.0, o.get("critChance") or 0.0) / 100.0
            if kinds & {"shock", "ignite"}:
                ev += hps * (min(100.0, o.get("shockChance") or 0.0) + min(100.0, o.get("igniteChance") or 0.0)) / 100.0
            d = (w.get("dps") or {}).get("best") or 0.0
            return ev * 1e9 + d          # energy events first, the feeder's own DPS breaks ties
        return metric

    def _feeder_moves(self, ctx, res, protect) -> bool:
        f = self._feeder(ctx)
        if not f:
            return False
        slot = self._feeder_slot(ctx, f)
        if slot is None:
            return False
        lq = _level_quality(_groups(ctx).get(f["group"]) or {}, slot)
        cands = [(f"g{f['group']}:{slot} {f.get('name')} -> {c.name}",
                  {"gem_changes": [dict({"group": f["group"], "slot": slot, "gem": c.skill_id}, **lq)]})
                 for c in ctx.gems().feeder_spells() if c.skill_id != f.get("id")]
        pre = prescreen_main(ctx, {"group": f["group"], "skill": f.get("index") or 1}, cands, self._feeder_metric(ctx))
        short = [(l, c) for _, l, c in pre[:self.prescreen_k]]
        return self._verify(ctx, screen(ctx, short), "feeder", res, protect)

    # -------------------------------------------------------------- run
    def run(self, ctx: Context) -> StepResult:
        t0, ev0 = time.time(), ctx.evals
        _ensure_energy(ctx)
        if ctx.current is None:
            ctx.refresh()
        res = StepResult(self.name, False, ctx.current.get("objective"), ctx.current.get("objective"))
        metas = self._meta_groups(ctx)
        if not metas:
            unmod = [e.get("metaName") for e in self._energy(ctx) if e.get("spells")]
            res.notes.append("no editable modelled meta group" + (f" (unmodelled: {unmod})" if unmod else ""))
            res.seconds = time.time() - t0
            return res
        for p in range(self.passes):
            improved = False
            for e in self._meta_groups(ctx):
                gi = e["group"]
                f = self._feeder(ctx)
                protect = {gi} | ({f["group"]} if f else set())
                if self.spells:
                    improved |= self._spell_moves(ctx, e, res, protect)
                if self.metas and ctx.budget_left() > 0:
                    improved |= self._meta_moves(ctx, e, res, protect)
                if self.meta_supports and ctx.budget_left() > 0:
                    e2 = next((x for x in self._meta_groups(ctx) if x["group"] == gi), e)
                    trig = (e2.get("triggered") or [{}])[0]
                    main = {"group": gi, "skill": trig.get("index")} if trig.get("index") else None
                    improved |= self._support_moves(ctx, gi, main, res, protect, "metasupport", True,
                                                    lambda w: (w.get("dps") or {}).get("best") or 0.0)
            f = self._feeder(ctx)
            if f:
                protect = {e["group"] for e in self._meta_groups(ctx)} | {f["group"]}
                if self.feeder and ctx.budget_left() > 0:
                    improved |= self._feeder_moves(ctx, res, protect)
                f = self._feeder(ctx)
                if self.feeder_supports and f and ctx.budget_left() > 0 and not (_groups(ctx).get(f["group"]) or {}).get("grantedBy"):
                    improved |= self._support_moves(ctx, f["group"], {"group": f["group"], "skill": f.get("index") or 1},
                                                    res, protect, "feedersupport", False, self._feeder_metric(ctx))
            if not improved or ctx.budget_left() <= 0:
                break
        res.end_objective = ctx.current.get("objective")
        res.improved = bool(res.changes) and (res.end_objective or 0) > (res.start_objective or 0) + EPS
        res.evals = ctx.evals - ev0
        res.seconds = time.time() - t0
        if ctx.budget_left() <= 0:
            res.notes.append("budget exhausted")
        return res


def cat_modelled(cat) -> set:
    return {m.skill_id for m in cat.metas(True)}


# ---------------------------------------------------------------------------------------------- ConvertStep
def manual_damaging(ctx: Context) -> list[dict]:
    """Counted manual damaging skills (constraints.py counting rules) from the skills view."""
    out = []
    main_g = (_stats0(ctx).get("main") or {}).get("group")
    for g in ctx.skills().get("groups") or []:
        if g.get("enabled") is False:
            continue
        nsup = len([x for x in g.get("gems") or [] if x.get("support") and x.get("enabled", True) is not False])
        for s in g.get("skills") or []:
            if s.get("enabled") is False or s.get("activation") != "manual" or not s.get("damaging"):
                continue
            if s.get("weaponDefault"):
                continue
            if (s.get("itemGranted") or g.get("grantedBy")) and not (nsup or g["index"] == main_g):
                continue
            out.append(dict(s, group=g["index"]))
    return out


def needs_conversion(ctx: Context) -> bool:
    cons = [str(c) for c in (getattr(ctx.scorer, "constraints", None) or [])]
    if not any(c.split(":")[0] == "one_button" for c in cons):
        return False
    cur = ctx.current or ctx.refresh()
    return any(v.get("rule") == "constraint" and str(v.get("entry", "")).startswith("one_button")
               for v in cur.get("violations") or [])


class ConvertStep:
    name = "convert"

    def __init__(self, metas=("MetaCastOnCritPlayer", "MetaCastOnElementalAilmentPlayer"), feeders=("SparkPlayer",),
                 feeder_supports: int = 4, max_disable: int = 3, prescreen_k: int = 4):
        self.metas, self.feeders = list(metas), list(feeders)
        self.feeder_supports, self.max_disable, self.prescreen_k = feeder_supports, max_disable, prescreen_k

    applicable = staticmethod(needs_conversion)

    def _target(self, ctx, manual) -> dict | None:
        """The spell to put under the meta gem: the main skill if it is a manual triggerable spell, else the
        first such manual spell."""
        cat = ctx.gems()
        trig = {g.skill_id for g in cat.trigger_spells()}
        main_g = (_stats0(ctx).get("main") or {}).get("group")
        groups = _groups(ctx)
        cands = [m for m in manual if m.get("id") in trig and m.get("gemSlot") is not None
                 and not groups.get(m["group"], {}).get("grantedBy")]
        cands.sort(key=lambda m: (m["group"] != main_g,))
        return cands[0] if cands else None

    def run(self, ctx: Context) -> StepResult:
        t0, ev0 = time.time(), ctx.evals
        _ensure_energy(ctx)
        if ctx.current is None:
            ctx.refresh()
        start = ctx.current
        res = StepResult(self.name, False, start.get("objective"), start.get("objective"))
        manual = manual_damaging(ctx)
        target = self._target(ctx, manual)
        if target is None:
            res.notes.append(f"no manual triggerable spell to convert (manual damaging: {[m.get('name') for m in manual]})")
            res.seconds = time.time() - t0
            return res
        tg = target["group"]
        groups = _groups(ctx)
        cat = ctx.gems()
        trig_ids = {g.skill_id for g in cat.trigger_spells()}
        feeder_ids = {g.skill_id for g in cat.feeder_spells()}
        # feeder options: an existing manual fast spell elsewhere, or a new group per configured feeder
        feeders = []
        for m in manual:
            if m["group"] != tg and m.get("id") in feeder_ids and not groups.get(m["group"], {}).get("grantedBy"):
                feeders.append(("existing", m))
        for fid in self.feeders:
            feeders.append(("new", {"id": fid, "name": (cat.get(fid).name if cat.get(fid) else fid)}))
        n_groups = max(groups) if groups else 0
        tgroup = groups[tg]
        root = ctx.engine.snapshot()
        root_state = (ctx.current, ctx.current_stats, ctx._skills, len(ctx.history))
        best = None
        tried = []
        try:
            for meta in self.metas:
                for kind, fd in feeders:
                    if ctx.budget_left() <= 0:
                        break
                    seq, notes = self._combo(ctx, tg, tgroup, meta, kind, fd, manual, n_groups, trig_ids)
                    cur = ctx.current
                    label = f"{cat.get(meta).name if cat.get(meta) else meta} + {fd.get('name')} ({kind})"
                    tried.append(f"{label}: {_short(cur)}" + (f" [{'; '.join(notes)}]" if notes else ""))
                    ctx.log(f"  convert {label}: {_short(cur)}")
                    if cur.get("feasible") and (best is None or Context.better(cur, best[0])):
                        best = (cur, seq, label, notes)
                    # back to the original build
                    ctx.engine.restore(root)
                    ctx.current, ctx.current_stats, ctx._skills = root_state[:3]
                    del ctx.history[root_state[3]:]
        finally:
            pass
        res.notes += tried
        if best is None:
            ctx._drop(root)
            res.notes.append("no conversion met constraints and floors; build unchanged")
            res.evals = ctx.evals - ev0
            res.seconds = time.time() - t0
            return res
        for ch in best[1]:
            ctx.commit(ch, f"convert: {ch.get('label', '')}")
            res.changes.append(ch)
        ctx._drop(root)
        res.notes.append(f"converted to {best[2]}")
        res.end_objective = ctx.current.get("objective")
        res.improved = Context.better(ctx.current, start)
        res.evals = ctx.evals - ev0
        res.seconds = time.time() - t0
        return res

    def _combo(self, ctx, tg, tgroup, meta, kind, fd, manual, n_groups, trig_ids):
        """Apply one conversion combo (committed; caller restores). -> (changes, notes)."""
        seq, notes = [], []
        gems = tgroup.get("gems") or []
        # 1. meta gem into the target group (append, or replace the least useful support when full)
        if len(gems) < MAX_GEMS:
            meta_changes = [("append", [{"group": tg, "slot": max(x["slot"] for x in gems) + 1, "gem": meta}])]
        else:
            meta_changes = [(f"replace {s.get('name')}", [{"group": tg, "slot": s["slot"], "gem": meta}])
                            for s in _supports(tgroup)]
        disable = {m["group"]: False for m in manual if m["group"] != tg and not (kind == "existing" and m["group"] == fd["group"])}
        if kind == "new":
            fg = n_groups + 1
            feeder_gc = [{"group": fg, "slot": 1, "gem": fd["id"]}]
        else:
            fg = fd["group"]
            feeder_gc = []
        opts = []
        for lab, mc in meta_changes:
            ch = {"gem_changes": mc + feeder_gc}
            if disable:
                ch["group_enabled"] = dict(disable)
            opts.append((lab, ch))
        ranked = screen(ctx, opts) if len(opts) > 1 else [(None, opts[0][0], opts[0][1])]
        lab, ch = ranked[0][1], ranked[0][2]
        ch = dict(ch, label=f"meta {meta} ({lab}), feeder {fd.get('name')}")
        ctx.commit(ch, "convert")
        seq.append(ch)
        # 2. Spirit
        protect = {tg, fg}
        for c in relieve_spirit(ctx, protect, self.max_disable, notes):
            seq.append(dict(c, label="spirit"))
        # 3. feeder supports (new feeder group only)
        if kind == "new":
            fmetric = TriggerStep()._feeder_metric(ctx)
            for k in range(self.feeder_supports):
                if ctx.budget_left() <= 0:
                    break
                g = _groups(ctx).get(fg) or {}
                if len(g.get("gems") or []) >= MAX_GEMS:
                    break
                slot = max(x["slot"] for x in g.get("gems") or []) + 1
                cands = _support_candidates(ctx, fg, None, False)
                changes = [(c["name"], {"gem_changes": [{"group": fg, "slot": slot, "gem": c["gem"]}]}) for c in cands]
                fi = next((s.get("index") for s in g.get("skills") or [] if s.get("gemSlot") == 1), 1)
                pre = prescreen_main(ctx, {"group": fg, "skill": fi or 1}, changes, fmetric)
                ranked = screen(ctx, [(l, c) for _, l, c in pre[:self.prescreen_k]])
                done = False
                for r, l, c in ranked:
                    ok, _ = ctx.try_commit(c, f"convert feeder support {l}")
                    if ok:
                        seq.append(dict(c, label=f"feeder support {l}"))
                        done = True
                        break
                if not done:
                    break
        return seq, notes
