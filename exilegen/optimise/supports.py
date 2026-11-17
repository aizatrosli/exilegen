"""Support gem optimiser step.

    from exilegen.optimise.supports import SupportStep
    res = SupportStep(groups="main+triggered").run(ctx)

For each target socket group the step replaces support gems with legal alternatives:

1. Candidates come from the engine (`valid_supports`): PoB's can-support test, gem tier against the
   character level, not socketed in another enabled group (PoE2 one-copy rule, which also covers lineage
   supports), not already in the group. A candidate is legal for slot k when its only conflicts (same gem,
   gem family, plus-version lineage) sit in slot k. Supports that do not support the group's active skill
   (a meta gem's energy supports, say) are protected and never touched. Candidates whose registry entry
   blocks under any scorer profile (e.g. Hexblast without a curse) are dropped before evaluation.
2. Greedy: the slots are ordered by their drop-one marginal (weakest first). For the weakest slot every
   legal candidate is evaluated in one `whatif_many` batch and scored with the Scorer; the best improving
   one is committed, otherwise the next weakest slot is tried. Repeat until no slot improves.
3. Local search: the full single-swap neighbourhood plus pairwise swaps of any two slots with ordered
   pairs from the `top_k` candidates (ranked by their best single swap), until nothing improves.
4. After every commit the build is re-scored from scratch (support effects multiply).

Judging: candidates are judged only through the Scorer (feasibility, then objective). Screening batches
are cheap (no drop-one calcs); the support-condition discounts then use marginals estimated from the
committed build (the replaced slot's drop-one value is exact, the others are scaled). The best
`confirm_n` screened moves are re-evaluated with real engine marginals before a commit.

Multi-profile scorers: the batch helper switches profiles itself (one `whatif_many` per profile) and
restores the first profile, the same way `Context._stats_for` does. It also passes the scorer a skills
view that contains the candidate gem, so the condition registry sees what is being evaluated.
"""
from __future__ import annotations

import copy
import dataclasses
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .base import Context, StepResult

CHUNK = 96                       # variants per whatif_many request
_ROMAN = re.compile(r"\s+(I|II|III|IV|V)$")


def _base_name(name: str) -> str:
    return _ROMAN.sub("", name or "").strip().lower()


def _key(res: dict | None) -> tuple:
    """Sort key of a score result: feasible first, then objective."""
    if not res:
        return (False, float("-inf"))
    return (bool(res.get("feasible")), res.get("objective") if res.get("objective") is not None else float("-inf"))


def _unverified(flags) -> set:
    return {f for f in flags or [] if "unverified" in f}


@dataclass
class _State:
    stats: dict                  # profile -> stats doc (with marginals)
    view: dict                   # engine skills() view
    res: dict                    # score result


@dataclass
class SupportStep:
    name: str = "supports"
    groups: Any = "main"         # "main" | "main+triggered" | [group indices]
    max_rounds: int = 3          # passes over all target groups
    top_k: int = 8               # candidates paired in the pairwise local search
    allow_unverified: bool = False   # True: unverified registry discounts are not applied (credit 1)
    pairwise: bool = True
    min_gain: float = 5e-4       # minimum objective gain (log units, ~0.05% DPS) to commit a move
    confirm_n: int = 3           # screened moves re-checked with real marginals before a commit
    max_moves_per_group: int = 30
    notes: list = field(default_factory=list)

    # ================================================================== entry point
    def run(self, ctx: Context) -> StepResult:
        t0, ev0 = time.time(), ctx.evals
        self.ctx, self.eng = ctx, ctx.engine
        self.notes = []
        self.scorer = self._make_scorer(ctx.scorer)
        self.profs = ctx._profiles()
        self.registry_blocked: dict[int, list[str]] = {}
        view = self.eng.skills()
        self.fields = self._fields(ctx.fields, view)
        cur = self._state(view)
        # Judge floors exactly as the caller's Context does: its baseline when it has one (relative
        # floors), otherwise absolute floors. Baselining on this step's own start relaxed EHP/max-hit/res
        # floors to "no worse than start", so synth's absolute guard rolled most support moves back.
        self.base_stats = ctx.baseline_stats
        cur = _State(cur.stats, cur.view, self._score(cur.stats, cur.view))
        self.cur = cur
        start = cur.res
        targets = self._targets(cur)
        ctx.log(f"[supports] start objective {start['objective']:.4f} honest {start['dps_honest']:,.0f}"
                f" feasible={start['feasible']} targets={targets}")
        changes: list[dict] = []
        for rnd in range(max(1, self.max_rounds)):
            n0 = len(changes)
            for g in targets:
                if ctx.budget_left() <= 0:
                    break
                try:
                    changes += self._optimise_group(g, rnd)
                except Exception as ex:          # noqa: BLE001 - one bad group must not end the step
                    self.notes.append(f"group {g}: {type(ex).__name__}: {ex}")
                    ctx.log(f"[supports] group {g} failed: {ex}")
            if len(changes) == n0 or ctx.budget_left() <= 0:
                break
        end = self.cur.res
        if ctx.budget_left() <= 0:
            self.notes.append("budget exhausted")
        for g, names in self.registry_blocked.items():
            if names:
                self.notes.append(f"group {g}: registry-blocked candidates skipped: {', '.join(sorted(set(names)))}")
        new_unv = sorted(_unverified(end.get("flags")) - _unverified(start.get("flags")))
        if new_unv:
            self.notes.append("new unverified flags: " + ", ".join(new_unv))
        return StepResult(self.name, improved=bool(changes), start_objective=start["objective"],
                          end_objective=end["objective"], changes=changes, evals=ctx.evals - ev0,
                          seconds=time.time() - t0,
                          notes=self.notes + [{"start": _summary(start), "end": _summary(end)}])

    # ================================================================== setup helpers
    def _make_scorer(self, scorer):
        if not self.allow_unverified:
            return scorer
        reg = copy.deepcopy(scorer.registry)          # identity between entries/by_id/by_name is kept
        profs = list(getattr(scorer, "profile_list", None) or [scorer.profile])
        regp = {scorer.profiles.registry_profile(p) for p in profs}
        for e in reg.entries:
            if not e.get("verified") and not e.get("requires"):
                tr = dict(e.get("treatment") or {})
                for p in regp:
                    tr[p] = {"action": "allow"}
                e["treatment"] = tr
        return dataclasses.replace(scorer, registry=reg, _cache={})

    @staticmethod
    def _fields(fields, view) -> list:
        if fields is None or fields == "all":
            out = ["all"]
        elif isinstance(fields, str):
            out = [fields]
        else:
            out = list(fields)
        if "all" not in out:
            if not any(f == "core" for f in out):
                out.append("core")
            if any(g.get("enabled") is not False and g.get("metaGroup") for g in view.get("groups", [])):
                out.append("energy")      # the Scorer's energy correction needs the meta facts
        return [f for f in out if f != "marginals"]

    def _targets(self, cur: _State) -> list[int]:
        doc = next(iter(cur.stats.values()))
        main = (doc.get("main") or {}).get("group")
        gs = {g["index"]: g for g in cur.view.get("groups", [])}
        if isinstance(self.groups, (list, tuple)):
            cand = [int(x) for x in self.groups]
        else:
            cand = [main]
            if self.groups in ("main+triggered", "all", "triggered"):
                for e in doc.get("energy") or []:
                    cand.append(e.get("group"))
                    cand.append((e.get("feeder") or {}).get("group"))
        out = []
        for g in cand:
            gg = gs.get(g)
            if g is None or g in out or not gg or gg.get("enabled") is False or gg.get("noSupports"):
                continue
            out.append(g)
        return out

    # ================================================================== evaluation
    def _whatif_many(self, changes: list[list[dict]], marginals: bool, fields: list | None = None) -> list[dict | None]:
        """Stats per profile for each gem-change list: [{profile: doc} | None]."""
        out: list[dict | None] = [{} for _ in changes]
        fields = (fields or self.fields) + (["marginals"] if marginals else [])
        multi = len(self.profs) > 1
        for prof in self.profs:
            if multi:
                self.eng.set_profile(prof)
            for i in range(0, len(changes), CHUNK):
                part = changes[i:i + CHUNK]
                res = self.eng.whatif_many([{"gem_changes": gc} for gc in part], fields=fields)
                for j, r in enumerate(res):
                    k = i + j
                    self.ctx.evals += 1 + len((r or {}).get("marginals") or [])
                    if out[k] is None or not isinstance(r, dict) or r.get("err"):
                        out[k] = None
                    else:
                        out[k][prof] = r
        if multi:
            self.eng.set_profile(self.profs[0])
        return out

    def _state(self, view=None) -> _State:
        stats = {}
        multi = len(self.profs) > 1
        for prof in self.profs:
            if multi:
                self.eng.set_profile(prof)
            d = self.eng.stats(fields=self.fields, marginals=True)
            self.ctx.evals += 1 + len(d.get("marginals") or [])
            stats[prof] = d
        if multi:
            self.eng.set_profile(self.profs[0])
        view = view or self.eng.skills()
        return _State(stats, view, None)

    def _score(self, stats: dict, view: dict) -> dict:
        return self.scorer.score(stats, {p: view for p in stats}, baseline=self.base_stats)

    @staticmethod
    def _view_with(view: dict, group: int, swaps: list[tuple[int, dict]]) -> dict:
        """Copy of the skills view with candidate supports in `group`'s slots."""
        v = dict(view)
        groups = []
        for g in view.get("groups", []):
            if g.get("index") == group:
                g = dict(g)
                gems = [dict(x) for x in g.get("gems", [])]
                for slot, c in swaps:
                    for i, x in enumerate(gems):
                        if x.get("slot") == slot:
                            gems[i] = {"slot": slot, "id": c["id"], "skillId": c.get("skillId"), "name": c["name"],
                                       "support": True, "enabled": True, "tags": c.get("tags", []),
                                       "family": c.get("family", [])}
                g["gems"] = gems
            groups.append(g)
        v["groups"] = groups
        return v

    def _approx_marginals(self, doc: dict, prof: str, group: int, swaps: list[tuple[int, dict]]) -> list:
        """Drop-one marginals for a screened candidate, estimated from the committed build: dropping a
        replaced slot gives exactly the committed build's drop-one value of that slot; other slots are scaled
        by the candidate's DPS ratio (supports multiply)."""
        base = self.cur.stats.get(prof) or {}
        ms = base.get("marginals") or []
        if not ms:
            return []
        d0 = ((base.get("dps") or {}).get("best") or 0.0)
        d1 = ((doc.get("dps") or {}).get("best") or 0.0)
        ratio = d1 / d0 if d0 > 0 else 1.0
        sw = {s: c for s, c in swaps}
        out = []
        for m in ms:
            m2 = dict(m)
            if m.get("group") == group and m.get("slot") in sw and len(swaps) == 1:
                c = sw[m["slot"]]
                m2.update(gem_id=c.get("skillId"), gemId=c["id"], name=c["name"], dps_base=d1)
            else:
                if m.get("group") == group and m.get("slot") in sw:
                    c = sw[m["slot"]]
                    m2.update(gem_id=c.get("skillId"), gemId=c["id"], name=c["name"])
                for k in ("dps_without", "group_dps_without"):
                    if isinstance(m.get(k), (int, float)):
                        m2[k] = m[k] * ratio
                m2["dps_base"] = d1
            out.append(m2)
        return out

    def _can_synth_energy(self, group: int) -> bool:
        """Screening can skip the engine's per-skill energy calcs when the changed group is the main group:
        then its triggered spell DPS and the feeder facts (when the main skill is the feeder) follow from the
        main-skill fields of a core calc. Changes to other meta groups or to a separate feeder need the
        real facts."""
        for prof, doc in self.cur.stats.items():
            main = (doc.get("main") or {}).get("group")
            if group != main:
                return False
            for e in doc.get("energy") or []:
                fg = (e.get("feeder") or {}).get("group")
                if e.get("group") == group:
                    tr = e.get("triggered") or []
                    ms = (doc.get("mainSkill") or {}).get("id")
                    if len(tr) != 1 or tr[0].get("id") != ms:
                        return False
        return True

    def _approx_energy(self, doc: dict, prof: str, group: int) -> list:
        base = self.cur.stats.get(prof) or {}
        d0 = ((base.get("dps") or {}).get("best") or 0.0)
        d1 = ((doc.get("dps") or {}).get("best") or 0.0)
        off = doc.get("offence") or {}
        out = []
        for e in base.get("energy") or []:
            e2 = dict(e)
            if e.get("group") == group:
                e2["triggered"] = [dict(t, dps=d1) for t in e.get("triggered") or []]
            f = e.get("feeder") or {}
            if f.get("group") == group and d0 > 0:
                f2 = dict(f, dps=d1)
                for k in ("hitsPerSec", "speed", "critChance", "shockChance", "igniteChance", "freezeChance",
                          "chillChance", "electrocuteChance"):
                    if isinstance(off.get(k), (int, float)):
                        f2[k] = off[k]
                hps = f2.get("hitsPerSec") or 0.0
                for ev, ch in (("critsPerSec", "critChance"), ("shocksPerSec", "shockChance"),
                               ("ignitesPerSec", "igniteChance"), ("freezesPerSec", "freezeChance")):
                    if ev in f:
                        f2[ev] = hps * (f2.get(ch) or 0.0) / 100.0
                e2["feeder"] = f2
            out.append(e2)
        return out

    def _evaluate(self, group: int, moves: list[list[tuple[int, dict]]], exact: bool) -> list[dict | None]:
        """Score each move (list of (slot, candidate)); exact=True asks the engine for marginals."""
        if not moves:
            return []
        left = self.ctx.budget_left()
        per = len(self.profs) * (1 + (8 if exact else 0))
        if not exact:
            # keep enough for _pick's exact confirm: a screen that spends the whole budget finds the better
            # support and then cannot commit it
            left -= self.confirm_n * len(self.profs) * 9
        if left < per * len(moves):
            moves = moves[:max(0, left // per)]
            if not moves:
                return []
        gcs = [[{"group": group, "slot": s, "gem": c["id"]} for s, c in mv] for mv in moves]
        fields = self.fields
        synth = not exact and "energy" in fields and self._can_synth_energy(group)
        if synth:
            fields = [f for f in fields if f != "energy"]
        stats = self._whatif_many(gcs, marginals=exact, fields=fields)
        out = []
        for mv, st in zip(moves, stats):
            if not st:
                out.append(None)
                continue
            if not exact:
                st = {p: dict(d, marginals=self._approx_marginals(d, p, group, mv)) for p, d in st.items()}
                if synth:
                    st = {p: dict(d, energy=self._approx_energy(d, p, group)) for p, d in st.items()}
            view = self._view_with(self.cur.view, group, mv)
            try:
                out.append(self._score(st, view))
            except Exception as ex:      # noqa: BLE001
                self.notes.append(f"score error: {ex}")
                out.append(None)
        return out

    # ================================================================== candidates
    def _candidates(self, group: int):
        vs = self.eng.valid_supports(group)
        gems = {}
        for g in self.cur.view.get("groups", []):
            if g.get("index") == group:
                gems = {x.get("slot"): x for x in g.get("gems", [])}
        sups = vs.get("supports") or []
        supportable = {s["id"] for s in sups if s.get("socketed")}
        # a support that does not support the target skill is protected when it may serve another active
        # gem of the group (meta gem, second active); in a one-active group it is dead weight and replaceable
        n_active = sum(1 for x in gems.values() if not x.get("support") and x.get("enabled") is not False)
        slots = [s for s in vs.get("slots") or []
                 if (gems.get(s) or {}).get("id") in supportable or n_active <= 1]
        blocked = self._blocked_names(sups)
        self.registry_blocked.setdefault(group, []).extend(blocked)
        cands = [s for s in sups if not s.get("socketed") and s["name"] not in blocked]
        return slots, cands, gems, vs

    def _blocked_names(self, sups: list[dict]) -> set:
        """Supports whose registry entry blocks under any profile for the current build."""
        from exilegen.scoring.adapter import BuildView
        from exilegen.scoring.conditions import Signals
        reg = self.scorer.registry
        out = set()
        for prof, doc in self.cur.stats.items():
            rp = self.scorer.profiles.registry_profile(prof)
            reg.overrides[rp] = self.scorer.profiles.condition_overrides(prof)
            sig = Signals(BuildView(doc, self.cur.view, prof))
            for s in sups:
                for e in reg.lookup(s.get("skillId"), s.get("name")):
                    if e.get("kind") not in (None, "support", "rule", "gem"):
                        continue
                    try:
                        action = reg.entry_credit(e, rp, sig)[0]
                    except Exception:    # noqa: BLE001
                        continue
                    if action == "block":
                        out.add(s["name"])
        return out

    @staticmethod
    def _legal(cands_slots: list[tuple[int, dict]]) -> bool:
        slots = {s for s, _ in cands_slots}
        for s, c in cands_slots:
            if any(cf.get("slot") not in slots for cf in c.get("conflicts") or []):
                return False
            if len(cands_slots) == 1 and any(cf.get("slot") != s for cf in c.get("conflicts") or []):
                return False
        cs = [c for _, c in cands_slots]
        for i in range(len(cs)):
            for j in range(i + 1, len(cs)):
                a, b = cs[i], cs[j]
                if a["id"] == b["id"] or set(a.get("family") or []) & set(b.get("family") or []) \
                        or _base_name(a["name"]) == _base_name(b["name"]):
                    return False
        return True

    # ================================================================== search
    def _pick(self, group: int, moves: list, screened: list) -> tuple | None:
        """Confirm the best screened moves with real marginals; return (move, result) of the best one that
        beats the committed build by min_gain."""
        cur = self.cur.res
        ranked = sorted([(r, m) for r, m in zip(screened, moves) if r], key=lambda x: _key(x[0]), reverse=True)
        top = [m for r, m in ranked if self._beats(r, cur)][:self.confirm_n]
        if not top:
            return None
        exact = self._evaluate(group, top, exact=True)
        best = None
        for m, r in zip(top, exact):
            if r and self._beats(r, cur) and (best is None or _key(r) > _key(best[1])):
                best = (m, r)
        return best

    def _beats(self, a: dict, b: dict) -> bool:
        if not a:
            return False
        fa, fb = bool(a.get("feasible")), bool(b.get("feasible"))
        if fa != fb:
            return fa
        return (a.get("objective") or float("-inf")) > (b.get("objective") or float("-inf")) + self.min_gain

    def _commit(self, group: int, move: list, res: dict, gems: dict, phase: str) -> dict:
        before = self.cur.res
        change = {"gem_changes": [{"group": group, "slot": s, "gem": c["id"]} for s, c in move]}
        label = "supports: " + "; ".join(f"g{group}s{s} {(gems.get(s) or {}).get('name')} -> {c['name']}" for s, c in move)
        self.ctx.commit(change, label=label, result=res)
        self.cur = self._state()
        self.cur.res = self._score(self.cur.stats, self.cur.view)
        after = self.cur.res
        ids = {e["id"] for _, c in move for e in self.scorer.registry.lookup(c.get("skillId"), c["name"])}
        new_hits = [h for h in after.get("conditions") or [] if h.get("entry_id") in ids]
        rec = {
            "group": group, "phase": phase,
            "slot": [s for s, _ in move] if len(move) > 1 else move[0][0],
            "old": [(gems.get(s) or {}).get("name") for s, _ in move] if len(move) > 1 else (gems.get(move[0][0]) or {}).get("name"),
            "new": [c["name"] for _, c in move] if len(move) > 1 else move[0][1]["name"],
            "new_ids": [c["id"] for _, c in move],
            "objective_before": before["objective"], "objective_after": after["objective"],
            "objective_delta": after["objective"] - before["objective"],
            "dps_honest_before": before["dps_honest"], "dps_honest_after": after["dps_honest"],
            "dps_honest_delta": after["dps_honest"] - before["dps_honest"],
            "dps_raw_before": before["dps_raw"], "dps_raw_after": after["dps_raw"],
            "feasible": after["feasible"],
            "screen_objective": res.get("objective"),
            "flags": sorted(set(after.get("flags") or []) - set(before.get("flags") or [])),
            "conditions": [{"name": h.get("name"), "action": h.get("action"), "credit": h.get("credit"),
                            "reason": h.get("reason"), "verified": h.get("verified")} for h in new_hits],
        }
        self.ctx.log(f"[supports] {phase} {label}: obj {before['objective']:.4f} -> {after['objective']:.4f}"
                     f", honest {before['dps_honest']:,.0f} -> {after['dps_honest']:,.0f}")
        return rec

    def _optimise_group(self, group: int, rnd: int) -> list[dict]:
        changes = []
        # ---- greedy: weakest slot first
        for _ in range(self.max_moves_per_group):
            if self.ctx.budget_left() <= 0:
                return changes
            slots, cands, gems, _vs = self._candidates(group)
            if not slots or not cands:
                return changes
            loss = self._slot_loss(group)
            order = sorted(slots, key=lambda s: loss.get(s, 0.0))
            committed = False
            for s in order:
                moves = [[(s, c)] for c in cands if self._legal([(s, c)])]
                screened = self._evaluate(group, moves, exact=False)
                pick = self._pick(group, moves[:len(screened)], screened)
                if pick:
                    changes.append(self._commit(group, pick[0], pick[1], gems, f"greedy.r{rnd}"))
                    committed = True
                    break
                if self.ctx.budget_left() <= 0:
                    return changes
            if not committed:
                break
        if not self.pairwise:
            return changes
        # ---- local search: full single neighbourhood + pairs over the top_k candidates
        for _ in range(self.max_moves_per_group):
            if self.ctx.budget_left() <= 0:
                return changes
            slots, cands, gems, _vs = self._candidates(group)
            if not slots or not cands:
                return changes
            singles = [[(s, c)] for s in slots for c in cands if self._legal([(s, c)])]
            sres = self._evaluate(group, singles, exact=False)
            best_per: dict[str, tuple] = {}
            for mv, r in zip(singles, sres):
                if r:
                    cid = mv[0][1]["id"]
                    if cid not in best_per or _key(r) > best_per[cid][0]:
                        best_per[cid] = (_key(r), mv[0][1])
            top = [c for _, c in sorted(best_per.values(), key=lambda x: x[0], reverse=True)[:self.top_k]]
            pairs = []
            if len(slots) >= 2:
                for i, s1 in enumerate(slots):
                    for s2 in slots[i + 1:]:
                        for c1 in top:
                            for c2 in top:
                                if c1["id"] != c2["id"] and self._legal([(s1, c1), (s2, c2)]):
                                    pairs.append([(s1, c1), (s2, c2)])
            pres = self._evaluate(group, pairs, exact=False)
            moves = singles[:len(sres)] + pairs[:len(pres)]
            pick = self._pick(group, moves, list(sres) + list(pres))
            if not pick:
                break
            changes.append(self._commit(group, pick[0], pick[1], gems, f"local.r{rnd}"))
        return changes

    def _slot_loss(self, group: int) -> dict[int, float]:
        """slot -> relative DPS loss when the support is dropped (committed build, first profile)."""
        doc = self.cur.stats.get(self.profs[0]) or {}
        out = {}
        for m in doc.get("marginals") or []:
            if m.get("group") != group:
                continue
            base, wo = m.get("group_dps_base"), m.get("group_dps_without")
            if not isinstance(base, (int, float)) or not isinstance(wo, (int, float)):
                base, wo = m.get("dps_base"), m.get("dps_without")
            if isinstance(base, (int, float)) and isinstance(wo, (int, float)) and base > 0:
                out[m.get("slot")] = (base - wo) / base
        return out


def _summary(r: dict) -> dict:
    return {k: r.get(k) for k in ("feasible", "objective", "dps_raw", "dps_honest", "ehp", "verified")} | \
        {"violations": [v.get("entry") for v in r.get("violations") or []]}
