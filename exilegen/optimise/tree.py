"""Passive tree step: warm-start local search or from-scratch prize-guided growth, judged only by the
Scorer (honest DPS, EHP, max hit, movement, sustain, ailment terms; floors; constraints).

    step = TreeStep(mode="warm")                 # or "scratch"
    res = step.run(ctx)                          # ctx: optimise.base.Context with the build loaded

Method (lessons from tools/poc/tree-solver, docs/poc-results.md):
  * State = {node: mode} over main-tree nodes (mode 0 normal, 1/2 weapon set) + attribute choices.
    Budgets are kept per mode (main-tree points and each weapon set's points separately).
  * Fixed nodes: socketed jewel sockets, allocated nodes that are granted (Sinister sockets ...),
    path-free nodes (From Nothing / Intuitive-Leap-like), user pins, and the nodes of a weapon set
    that does not change the score. Jewel-granted, unallocated nodes are banned from the graph.
  * Linearisation: one what-if per distinct stat key within depth D (path + node), attribute nodes
    under each of the three options, and every removable branch of size <= 6 (its loss).
  * Moves (all evaluated exactly, in batches): path moves (whole path to a node within depth D),
    point-neutral swaps (remove a branch, add a path of equal length), add-only moves while
    points are unspent, keystone + up to k nearby notables as one move, keystone removal with its
    branch + greedy refill, attribute flips (1 and 3 nodes).
  * Perturbation: drop a low-value branch of 3-6 nodes, refill, local search again; keep the best.
  * Scratch: class start + pinned nodes (shortest paths), then rounds of prize-guided growth
    (prize = lam * marginal + mu * log P(node), corpus prior hook, default mu = 0) to the same
    budgets, then the warm local search.
  * The final tree is committed with ctx.commit (a real apply) and compared with the start; the
    step falls back to the start if the applied tree is worse.

Throughput: candidates are scored in batches through ctx.evaluate_many (one profile switch per
profile per batch); without it the step batches whatif_many itself and also rejects candidates the
engine prunes unexpectedly. Every engine calc counts in ctx.evals.

Path-free nodes (From Nothing / Intuitive-Leap-like radius nodes, Sinister sockets granted by
jewels): the engine's resolveAlloc (exg.lua) prunes them on ANY tree what-if or apply. Until that is
fixed, the step (on_free_pruned="readd", default) forces the same pruning in every what-if (a no-op
re-add), so candidates are compared on equal footing, and after the final apply re-allocates them
through PoB's own undo state (engine `lua` op), rescoring before the keep/fallback decision.
on_free_pruned="skip" leaves such builds untouched. If the engine stops pruning them (probe at
start), they are simply fixed free nodes.
"""
from __future__ import annotations

import math
import random
import time
from collections import Counter, defaultdict
from typing import Any, Callable

from .base import Context, StepResult
from .tree_graph import TreeGraph

NEG = -1e18


class TreeStep:
    def __init__(self, mode: str = "warm", max_evals: int | None = None, max_seconds: float | None = None,
                 path_depth: int = 4, keystone_depth: int = 8, keystone_k: int = 3, keystones_per_round: int = 6,
                 restarts: int = 3, grow_step: int = 12, grow_depth: int = 6, moves_per_batch: int = 40,
                 inner_rounds: int = 8, max_keys: int = 220, branch_max: int = 6, lam: float = 1.0, mu: float = 0.0,
                 prior: Callable[[int, dict], float] | None = None, pins=(), extra_points: int = 0,
                 on_free_pruned: str = "readd", seed: int = 0, verbose: bool = True,
                 seed_tree: dict | None = None):
        if mode not in ("warm", "scratch"):
            raise ValueError("mode must be warm or scratch")
        self.mode = mode
        self.name = f"tree_{mode}"
        self.max_evals = max_evals if max_evals is not None else (4000 if mode == "warm" else 12000)
        self.max_seconds = max_seconds
        self.D = path_depth
        self.Dk = keystone_depth
        self.k = keystone_k
        self.ks_per_round = keystones_per_round
        self.restarts = restarts
        self.grow_step = grow_step
        self.grow_depth = grow_depth
        self.M = moves_per_batch
        self.inner = inner_rounds
        self.max_keys = max_keys
        self.branch_max = branch_max
        self.lam, self.mu, self.prior = lam, mu, prior
        self.pins = set(int(p) for p in pins)
        # scratch only: a tree to start from (e.g. a mutated build's parent) as {"nodes": [...],
        # "attrs": {node: choice}}. Its main-tree nodes are added after the pins, nearest to the class
        # start first, while points last; growth and local search then repair it.
        self.seed_tree = seed_tree
        self.extra_points = extra_points
        self.on_free_pruned = on_free_pruned
        self.rng = random.Random(seed)
        self.verbose = verbose
        self.report: dict = {}
        self._applied = None
        self._noop = None

    # ================================================================== run
    def run(self, ctx: Context) -> StepResult:
        t0 = time.time()
        self._t0, self._ev0, self.ctx = t0, ctx.evals, ctx
        notes: list[str] = []
        start_res = ctx.current or ctx.refresh()
        res = StepResult(self.name, False, start_res.get("objective"), start_res.get("objective"))
        eng = ctx.engine
        self._profs = ctx._profiles()
        self._prof = self._profs[0]
        self._cache: dict = {}
        self._br_cache: dict = {}
        self._skills = ctx.skills()

        view = eng.tree(frontier=False, graph=True)
        alloc = {int(i): 0 for i in view["allocated"]}
        for k, m in (view.get("weaponSetNodes") or {}).items():
            if int(k) in alloc:
                alloc[int(k)] = int(m)
        attrs = {int(k): int(v) for k, v in (view.get("attributeChoices") or {}).items() if v}
        granted = {int(i) for i in view.get("grantedNodes") or ()}
        G = self.G = TreeGraph(view, banned=granted - set(alloc))
        jewels = {int(j["id"]) for j in view.get("jewelSockets") or () if j.get("item")}
        free = G.unreached(alloc)
        jewels = {int(j["id"]) for j in view.get("jewelSockets") or () if j.get("item")}
        self.E, self.EA = dict(alloc), dict(attrs)
        self.player = set(alloc)
        common = Counter(attrs.values()).most_common(1)
        self.ac = common[0][0] if common else 1

        # path-free nodes: does the engine keep them across a tree what-if?
        self.prune_ok: set = set()
        if free:
            pruned = self._probe_pruned(alloc, attrs, free)
            names = ", ".join(G.name(i) for i in sorted(free))
            if pruned:
                msg = (f"engine prunes {len(pruned)} path-free node(s) on any tree change ({names}); "
                       "needs resolveAlloc to keep Intuitive-Leap/From-Nothing/granted nodes")
                notes.append(msg)
                self._log(msg)
                if self.on_free_pruned == "skip" or self.mode == "scratch":
                    # scratch cannot keep the (unexposed) jewel-radius anchors of these nodes
                    notes.append("skipped: path-free nodes cannot be rebuilt from scratch until the engine "
                                 "models them")
                    return self._finish(res, start_res, notes, skipped="free_nodes_pruned")
                # every what-if prunes them the same way (forced with a no-op re-add), so candidates are
                # compared on equal footing; the final apply re-adds them through PoB (see _readd_free)
                self.prune_ok = set(pruned)
                self._noop = next((v for v in sorted(alloc) if v in jewels and v not in free),
                                  next((v for v in sorted(alloc) if v not in free), None))
            else:
                notes.append(f"{len(free)} path-free node(s) kept fixed: {names}")
        self.free = set(free)
        fixed = (jewels & set(alloc)) | (granted & set(alloc)) | set(free) | (self.pins & set(alloc))

        # budgets per mode
        cnt = Counter(alloc.values())
        self.B = {0: cnt.get(0, 0) + self.extra_points, 1: cnt.get(1, 0), 2: cnt.get(2, 0)}

        val0, r0 = self._evals([(alloc, attrs)])[0]
        self.val_start = val0
        if r0 is None:
            notes.append("start state failed to evaluate")
            return self._finish(res, start_res, notes, skipped="eval_failed")

        # weapon sets that change the score
        self.active = {0}
        probes = [k for k in (1, 2) if cnt.get(k)]
        if probes:
            outs = self._evals([({v: m for v, m in alloc.items() if m != k}, attrs) for k in probes])
            for k, (v, _) in zip(probes, outs):
                if abs(v - val0) > 1e-6:
                    self.active.add(k)
            inactive = [k for k in probes if k not in self.active]
            if inactive:
                notes.append(f"weapon set(s) {inactive} do not change the score: nodes kept as they are")
        self._log(f"[{self.name}] start val={val0:.4f} obj={start_res.get('objective'):.4f} N={dict(self.B)} "
                  f"fixed={len(fixed)} free={len(free)} active_ws={sorted(self.active - {0})}")

        if self.mode == "warm":
            for v, m in alloc.items():
                if m and m not in self.active:
                    fixed.add(v)
            self.fixed = fixed
            S, A, cur = dict(alloc), dict(attrs), val0
        else:
            self.fixed = {v for v in fixed}
            S, A = self._scratch_start(alloc, attrs, fixed)
            S, A = self._grow(S, A)
            cur = self._evals([(S, A)])[0][0]
            self._log(f"  grown: val={cur:.4f} N={dict(Counter(S.values()))} evals={self._used()}")

        S, A, cur = self._local_search(S, A, cur)
        best = (S, A, cur)
        for r in range(self.restarts):
            if not self._ok(reserve=200):
                break
            P = self._perturb(*best)
            if P is None:
                break
            S2, A2, c2 = self._local_search(*P)
            self._log(f"  restart {r + 1}: val={c2:.4f} (best {best[2]:.4f}) evals={self._used()}")
            if c2 > best[2] + 1e-9:
                best = (S2, A2, c2)
        S, A, cur = best

        # ---------------------------------------------------------- commit + verify
        self._reset_profile()
        if cur <= val0 + 1e-9 and self.mode == "warm":
            notes.append("no improving tree found")
            return self._finish(res, start_res, notes, S=self.E, A=self.EA, predicted=val0)
        change = self._diff(S, A)
        if not change:
            return self._finish(res, start_res, notes, S=S, A=A, predicted=cur)
        token = eng.snapshot()
        new = ctx.commit(change, label=self.name)
        if self.prune_ok:
            kept = self._readd_free(S, A)
            new = ctx.refresh()
            if ctx.history:
                ctx.history[-1].update(objective=new.get("objective"), feasible=new.get("feasible"))
            notes.append(f"re-added {kept}/{len(self.prune_ok)} path-free node(s) after the apply")
        self._applied = new
        if not Context.better(new, start_res):
            eng.restore(token)
            if ctx.history and ctx.history[-1].get("label") == self.name:
                ctx.history.pop()
            ctx.refresh()
            notes.append(f"applied tree scored {new.get('objective'):.4f} (what-if predicted {cur:.4f}) "
                         f"<= start {start_res.get('objective'):.4f}: fell back to the start tree")
            try:
                eng.op("drop_snapshot", token=token)
            except Exception:
                pass
            self._finish(res, start_res, notes, S=S, A=A, predicted=cur, fallback=True)
            cand = self.report
            self._finish(res, start_res, notes, S=self.E, A=self.EA, predicted=cur, fallback=True)
            self.report["candidate"] = {k: cand.get(k) for k in ("nodes", "attrs", "jaccard", "keystones", "points")}
            return res
        try:
            eng.op("drop_snapshot", token=token)
        except Exception:
            pass
        if abs((new.get("objective") or 0) - cur) > 1e-3 and new.get("feasible"):
            notes.append(f"applied objective {new.get('objective'):.4f} differs from what-if {cur:.4f}")
        res.changes.append(change)
        res.improved = True
        return self._finish(res, ctx.current, notes, S=S, A=A, predicted=cur, start_res=start_res)

    # ================================================================== bookkeeping
    def _log(self, *a):
        if self.verbose:
            self.ctx.log(*a)

    def _used(self):
        return self.ctx.evals - self._ev0

    def _ok(self, reserve: int = 0) -> bool:
        if self._used() + reserve >= self.max_evals or self.ctx.budget_left() <= reserve:
            return False
        if self.max_seconds is not None and time.time() - self._t0 > self.max_seconds:
            return False
        return True

    def _reset_profile(self):
        if len(self._profs) > 1 and self._prof != self._profs[0]:
            self.ctx.engine.set_profile(self._profs[0])
            self._prof = self._profs[0]

    def _finish(self, res: StepResult, end_res, notes, S=None, A=None, predicted=None, fallback=False,
                skipped=None, start_res=None):
        self._reset_profile()
        res.end_objective = (end_res or {}).get("objective")
        res.evals = self._used()
        res.seconds = time.time() - self._t0
        res.notes = notes
        rep = {"mode": self.mode, "skipped": skipped, "fallback": fallback, "predicted": predicted,
               "evals": res.evals, "seconds": res.seconds, "applied": self._applied}
        if S is not None:
            nodes = set(S)
            rep["nodes"] = sorted(nodes)
            rep["attrs"] = {str(k): v for k, v in (A or {}).items() if k in nodes}
            rep["jaccard"] = len(nodes & self.player) / max(1, len(nodes | self.player))
            rep["keystones"] = sorted(self.G.name(i) for i in nodes if self.G.kind(i) == "keystone")
            rep["player_keystones"] = sorted(self.G.name(i) for i in self.player if self.G.kind(i) == "keystone")
            rep["points"] = dict(Counter(S.values()))
        self.report = rep
        return res

    # ================================================================== evaluation
    def _skey(self, S, A):
        return (frozenset(S.items()), frozenset((k, v) for k, v in A.items() if k in S and self.G.is_attr(k)))

    def _diff(self, S, A) -> dict:
        add, rem, ac = [], [], {}
        for v, m in S.items():
            if self.E.get(v) != m:
                add.append({"id": v, "ws": m})
        for v in self.E:
            if v not in S:
                rem.append(v)
        for v in S:
            if self.G.is_attr(v):
                want = A.get(v)
                if want is None:
                    if v in self.E:
                        continue
                    want = self.ac
                if v not in self.E or self.EA.get(v) != want:
                    ac[v] = want
        ch = {}
        if self.prune_ok and self._noop is not None and self._noop in S and not any(
                isinstance(a, dict) and a["id"] == self._noop for a in add):
            add.append({"id": self._noop, "ws": S[self._noop]})    # forces the engine's pruning
        if add:
            ch["add_nodes"] = add
        if rem:
            ch["remove_nodes"] = sorted(rem)
        if ac:
            ch["attr_choices"] = ac
        return ch

    def _batch_stats(self, changes: list[dict]) -> list[dict]:
        """Stats per profile for each change: one profile switch per profile per batch."""
        ctx, eng = self.ctx, self.ctx.engine
        profs = self._profs
        order = profs if self._prof == profs[0] else [self._prof] + [p for p in profs if p != self._prof]
        out = [dict() for _ in changes]
        for p in order:
            if len(profs) > 1 and self._prof != p:
                eng.set_profile(p)
                self._prof = p
            for s in range(0, len(changes), 64):
                chunk = changes[s:s + 64]
                rs = eng.whatif_many(chunk, fields=ctx.fields)
                for j, r in enumerate(rs):
                    out[s + j][p] = r
            ctx.evals += len(changes)
        return out

    def _evals(self, states: list) -> list:
        """[(S, A)] -> [(val, score result | None)] (cached by state)."""
        ctx = self.ctx
        keys = [self._skey(S, A) for S, A in states]
        todo, seen = [], set()
        for (S, A), k in zip(states, keys):
            if k not in self._cache and k not in seen:
                seen.add(k)
                todo.append((k, S, A))
        cap = max(0, (self.max_evals - self._used())) // max(1, len(self._profs))
        if len(todo) > cap and self._used() > 0:
            todo = todo[:cap]
        if todo:
            many = getattr(ctx, "evaluate_many", None)
            if callable(many):
                scored = many([self._diff(S, A) for _, S, A in todo], skills=self._skills)
                for (k, S, A), r in zip(todo, scored):
                    self._cache[k] = (self._val(r), r)
            else:
                stats = self._batch_stats([self._diff(S, A) for _, S, A in todo])
                for (k, S, A), sp in zip(todo, stats):
                    bad = any(not isinstance(x, dict) or "err" in x for x in sp.values())
                    pruned = set()
                    for x in sp.values():
                        if isinstance(x, dict):
                            pruned |= {int(i) for i in (x.get("pruned") or ())}
                    pruned -= self.prune_ok
                    if bad or pruned:
                        self._cache[k] = (NEG, None)
                        if pruned and self.verbose:
                            self._log(f"  what-if pruned {sorted(pruned)[:6]}: candidate dropped")
                        continue
                    sp = {p: x for p, x in sp.items()}
                    r = ctx.score_stats(sp if len(sp) > 1 else next(iter(sp.values())), self._skills)
                    self._cache[k] = (self._val(r), r)
        return [self._cache.get(k, (NEG, None)) for k in keys]

    def _val(self, r: dict | None) -> float:
        if not r or r.get("objective") is None:
            return NEG
        o = float(r["objective"])
        if r.get("feasible"):
            return o
        return o - 5.0 - 5.0 * _shortfall(r)

    def _readd_free(self, S, A) -> int:
        """Workaround until the engine's resolveAlloc keeps path-free nodes: re-allocate them in PoB's
        own spec (RestoreUndoState keeps Intuitive-Leap-like nodes). Returns how many are allocated."""
        ids = sorted(self.prune_ok)
        modes = [int(S.get(i, self.E.get(i, 0)) or 0) for i in ids]
        ch = [int((A.get(i) or self.EA.get(i) or 0)) if self.G.is_attr(i) else 0 for i in ids]
        chunk = (
            "local b = launch.main.modes['BUILD']; local spec = b.spec\n"
            f"local ids, modes, ch = {{{','.join(map(str, ids))}}}, {{{','.join(map(str, modes))}}}, "
            f"{{{','.join(map(str, ch))}}}\n"
            "local st = spec:CreateUndoState(); local have = {}\n"
            "for _, id in ipairs(st.hashList) do have[id] = true end\n"
            "for i, id in ipairs(ids) do\n"
            "  if not have[id] then st.hashList[#st.hashList + 1] = id end\n"
            "  if modes[i] ~= 0 then st.weaponSets[id] = modes[i] end\n"
            "  local tn = spec.tree.nodes[id]\n"
            "  if ch[i] > 0 and tn and tn.isAttribute and tn.options and tn.options[ch[i]] then\n"
            "    local nn = copyTableSafe(tn, false, true); spec:ReplaceNode(nn, nn.options[ch[i]])\n"
            "    spec.tree:ProcessStats(nn); st.hashOverrides[id] = nn\n"
            "  end\n"
            "end\n"
            "EXG.restoreTree(st); EXG.state.dirty = true\n"
            "local n = 0; for _, id in ipairs(ids) do if spec.allocNodes[id] then n = n + 1 end end\n"
            "return n\n")
        try:
            return int(self.ctx.engine.lua(chunk) or 0)
        except Exception as e:
            self._log(f"  re-add of path-free nodes failed: {e}")
            return 0

    def _probe_pruned(self, alloc, attrs, free) -> set:
        """Ask the engine whether a no-op tree change keeps the path-free nodes."""
        eng = self.ctx.engine
        a = next((v for v in alloc if v in attrs and v not in free and attrs[v]), None)
        if a is not None:
            ch = {"attr_choices": {a: attrs[a] % 3 + 1}}
        else:
            leaf = next((v for v in alloc if v not in free), None)
            if leaf is None:
                return set(free)
            ch = {"add_nodes": [{"id": leaf, "ws": alloc[leaf]}]}
        r = eng.whatif(fields=["dps"], **ch)
        self.ctx.evals += 1
        return {int(i) for i in (r.get("pruned") or ())} & set(free)

    # ================================================================== state helpers
    def _branches(self, S, max_size):
        k = frozenset(S.items())
        br = self._br_cache.get(k)
        if br is None:
            if len(self._br_cache) > 64:
                self._br_cache.clear()
            br = self._br_cache[k] = self.G.branches(S, self.fixed, self.free, max_size=12)
        return {u: b for u, b in br.items() if len(b) <= max_size}

    def _counts(self, S):
        c = Counter(S.values())
        return {m: c.get(m, 0) for m in (0, 1, 2)}

    def _within_budget(self, S):
        c = self._counts(S)
        return all(c[m] <= self.B[m] for m in (0, 1, 2))

    def _sources(self, S, m):
        return {self.G.start} | {v for v, mm in S.items() if mm == 0 or (m and mm == m)}

    def _valid(self, S):
        # keystones and jewel sockets cannot be weapon-set specific (wiki Weapon set)
        if any(m and self.G.kind(v) in ("keystone", "jewel") and v not in self.fixed for v, m in S.items()):
            return False
        return self._within_budget(S) and self.fixed <= set(S) and self.G.connected(S, self.free)

    def _with(self, S, A, adds=(), removes=(), mode=0):
        S2, A2 = dict(S), dict(A)
        for u in removes:
            S2.pop(u, None)
            A2.pop(u, None)
        for v in adds:
            S2[v] = mode
            if self.G.is_attr(v) and v not in A2:
                A2[v] = self.ac
        return S2, A2

    def _prize(self, gain: dict) -> dict:
        if not self.mu or self.prior is None:
            return {v: self.lam * g for v, g in gain.items()}
        out = {}
        for v, g in gain.items():
            try:
                lp = float(self.prior(v, self.G.facts.get(v, {})))
            except Exception:
                lp = 0.0
            out[v] = self.lam * g + self.mu * lp
        return out

    # ================================================================== linearisation
    def _linearise(self, S, A, cur, depth, with_branches=True, max_keys=None, prev=None):
        """-> gain {node: est. marginal}, loss {branch frozenset: measured loss}."""
        G = self.G
        blocked = set(S)
        paths = G.paths(self._sources(S, 0), blocked, None, depth, alpha=0.0)
        groups = defaultdict(list)
        for v, p in paths.items():
            k = G.kind(v)
            if k in ("start", "mastery", "jewel") or v in self.G.banned:
                continue
            groups[G.key(v)].append(v)
        reps = []
        for key, vs in groups.items():
            rep = min(vs, key=lambda v: (len(paths[v]), v))
            reps.append((key, rep))
        prio = {"keystone": 0, "notable": 1}
        reps.sort(key=lambda kr: (len(paths[kr[1]]) > 1, prio.get(G.kind(kr[1]), 2), len(paths[kr[1]])))
        if prev:
            # refresh: notables/keystones and the keys never measured first, the rest keeps old gains
            reps.sort(key=lambda kr: (kr[1] in prev and G.kind(kr[1]) not in ("notable", "keystone"),
                                      len(paths[kr[1]]) > 1, len(paths[kr[1]])))
        reps = reps[:max_keys or self.max_keys]
        states, tags = [], []
        for key, rep in reps:
            p = paths[rep]
            if key == ("attr",):
                for c in (1, 2, 3):
                    S2, A2 = self._with(S, A, p)
                    A2[rep] = c
                    states.append((S2, A2))
                    tags.append((key, rep, c))
            else:
                states.append(self._with(S, A, p))
                tags.append((key, rep, None))
        loss: dict = {}
        bstates, btags = [], []
        if with_branches:
            br = self._branches(S, max(self.branch_max, 12))
            for u, b in br.items():
                if len(b) <= self.branch_max or any(G.kind(x) == "keystone" for x in b):
                    if b in loss:
                        continue
                    loss[b] = None
                    bstates.append(self._with(S, A, removes=b))
                    btags.append(b)
        outs = self._evals(states + bstates) if self._ok() else [(NEG, None)] * (len(states) + len(bstates))
        kg, ag = {}, {}
        first = {}
        for (key, rep, c), (v, _) in zip(tags, outs[:len(states)]):
            if v <= NEG / 2:
                continue
            if key == ("attr",):
                ag[c] = v - cur
                first[key] = rep
            else:
                kg[key] = (v - cur, rep)
        best_c = max(ag, key=ag.get) if ag else self.ac
        if ag:
            self.ac = best_c
        attr_gain = ag.get(best_c, 0.0)
        # subtract the path's estimated gains for deep representatives
        gain_key = {}
        for key, (g, rep) in kg.items():
            p = paths[rep]
            sub = 0.0
            for x in p[:-1]:
                kx = G.key(x)
                if kx == ("attr",):
                    sub += attr_gain
                elif kx in kg and len(paths[kg[kx][1]]) == 1:
                    sub += kg[kx][0]
            gain_key[key] = g - sub
        gain = {v: g for v, g in (prev or {}).items() if v not in blocked}
        for key, vs in groups.items():
            gk = attr_gain if key == ("attr",) else gain_key.get(key)
            if gk is None:
                continue
            for v in vs:
                gain[v] = gk
        for b, (v, _) in zip(btags, outs[len(states):]):
            loss[b] = cur - v if v > NEG / 2 else 1e9
        loss = {b: l for b, l in loss.items() if l is not None}
        self._log(f"  linearise: {len(groups)} keys ({len(reps)} evaluated, {len(states)} states), "
                  f"{len(bstates)} branches, depth {depth}, evals={self._used()}")
        return gain, loss

    # ================================================================== moves
    def _branch_loss(self, b, loss, gain):
        if b in loss:
            return loss[b]
        # estimate from the node keys (stale after a move)
        return sum(max(0.0, gain.get(x, 0.0)) for x in b)

    def _path_moves(self, S, A, cur, gain, loss, n=None):
        G = self.G
        moves = []
        prize = self._prize(gain)
        branches = self._branches(S, self.branch_max)
        by_size = defaultdict(list)
        for b in set(branches.values()):
            by_size[len(b)].append(b)
        for m in sorted(self.active):
            slack = self.B[m] - self._counts(S)[m]
            srcs = self._sources(S, m)
            paths = G.paths(srcs, set(S), prize, self.D)
            for t, p in paths.items():
                if G.kind(t) in ("jewel", "mastery", "start"):
                    continue
                g = sum(prize.get(x, 0.0) for x in p)
                if g <= 0:
                    continue
                L = len(p)
                if L <= slack:
                    moves.append((g, "add", p, (), m))
                need = L - max(0, slack)
                if need <= 0:
                    continue
                for b in by_size.get(need, ()):
                    if any(S.get(x) != m for x in b):
                        continue
                    if not (G.adj[p[0]] & (srcs - b)):
                        continue
                    moves.append((g - self._branch_loss(b, loss, gain), "swap", p, tuple(b), m))
        # remove-only: branches whose measured loss is negative (points may stay unspent)
        for b, l in loss.items():
            if l < 0 and b <= set(S):
                moves.append((-l, "drop", (), tuple(b), 0))
        moves.sort(key=lambda x: -x[0])
        out, keys = [], set()
        for est, kind, p, b, m in moves:
            if len(out) >= (n or self.M):
                break
            S2, A2 = self._with(S, A, p, b, m)
            if not self._valid(S2):
                continue
            k = self._skey(S2, A2)
            if k in keys:
                continue
            keys.add(k)
            tgt = G.name(p[-1]) if p else G.name(min(b))
            out.append((S2, A2, f"{kind}:{tgt}/{len(p)}-{len(b)}"))
        return out

    def _free_points(self, S, need, loss, gain, protect, m=0):
        """Greedy disjoint branches (lowest loss per point) freeing >= need points of mode m."""
        if need <= 0:
            return set()
        br = set(self._branches(S, self.branch_max).values())
        cands = sorted((b for b in br if not (b & protect) and all(S.get(x) == m for x in b)),
                       key=lambda b: self._branch_loss(b, loss, gain) / len(b))
        out = set()
        for b in cands:
            if b & out:
                continue
            out |= b
            if len(out) >= need:
                return out
        return None

    def _keystone_moves(self, S, A, gain, loss):
        G = self.G
        prize = self._prize(gain)
        srcs = self._sources(S, 0)
        paths = G.paths(srcs, set(S), prize, self.Dk)
        kss = [v for v in paths if G.kind(v) == "keystone"]
        kss.sort(key=lambda v: (-gain.get(v, 0.0), len(paths[v])))
        out = []
        slack = self.B[0] - self._counts(S)[0]
        for K in kss[:self.ks_per_round]:
            T = list(paths[K])
            near = G.hop_dist(set(T), set(S), 3)
            nots = [n for n in near if G.kind(n) == "notable" and gain.get(n, 0.0) > 0]
            nots.sort(key=lambda n: -gain.get(n, 0.0) / near[n])
            variants = [list(T)]
            for n in nots[:self.k]:
                if n in T:
                    continue
                pp = G.paths(srcs | set(T), set(S) | set(T), prize, 4).get(n)
                if not pp:
                    continue
                T = T + pp
                variants.append(list(T))
            for T in variants:
                rem = self._free_points(S, len(T) - slack, loss, gain, protect=set())
                if rem is None:
                    continue
                S2, A2 = self._with(S, A, T, rem)
                if self._valid(S2):
                    out.append((S2, A2, f"ks+{len(T)}:{G.name(K)}"))
        # keystone removal with its branch, refilled greedily
        br = self._branches(S, 12)
        for K in [v for v in S if G.kind(v) == "keystone" and v not in self.fixed]:
            bs = sorted({b for b in br.values() if K in b}, key=len)
            for b in ({bs[0], bs[-1]} if bs else ()):
                S2, A2 = self._with(S, A, removes=b)
                S2, A2 = self._fill(S2, A2, gain, exclude=set(b))
                if self._valid(S2):
                    out.append((S2, A2, f"ks-{len(b)}:{G.name(K)}"))
        return out

    def _fill(self, S, A, gain, exclude=(), force=False, limit=None, modes=None):
        """Spend unspent points on the best paths (prize per point), greedy."""
        G = self.G
        prize = self._prize(gain)
        for x in exclude:
            prize[x] = 0.0
        spent = 0
        for m in (modes or sorted(self.active)):
            while True:
                slack = self.B[m] - self._counts(S)[m]
                if limit is not None:
                    slack = min(slack, limit - spent)
                if slack <= 0:
                    break
                paths = G.paths(self._sources(S, m), set(S) | set(exclude), prize, min(slack, max(self.D, 1) + 2))
                best, bs = None, NEG
                for t, p in paths.items():
                    if G.kind(t) in ("jewel", "mastery", "start"):
                        continue
                    s = sum(max(prize.get(x, 0.0), 0.0) for x in p) / len(p)
                    if s > bs or (s == bs and best is not None and len(p) < len(best)):
                        best, bs = p, s
                if best is None or (bs <= 0 and not force):
                    break
                S, A = self._with(S, A, best, mode=m)
                for x in best:
                    prize[x] = 0.0
                spent += len(best)
        return S, A

    def _flips(self, S, A, cur):
        improved = False
        for _ in range(10):
            if not self._ok():
                break
            byc = defaultdict(list)
            for v in S:
                if self.G.is_attr(v):
                    byc[A.get(v) or self.ac].append(v)
            cands = []
            for c0, vs in byc.items():
                for c in (1, 2, 3):
                    if c == c0:
                        continue
                    for n in sorted({1, min(3, len(vs))}):
                        A2 = dict(A)
                        for v in vs[:n]:
                            A2[v] = c
                        cands.append((S, A2))
            if not cands:
                break
            outs = self._evals(cands)
            i = max(range(len(cands)), key=lambda j: outs[j][0])
            if outs[i][0] > cur + 1e-9:
                A, cur, improved = cands[i][1], outs[i][0], True
            else:
                break
        return A, cur, improved

    # ================================================================== search
    def _local_search(self, S, A, cur):
        it = 0
        while self._ok(reserve=50):
            it += 1
            gain, loss = self._linearise(S, A, cur, self.D, max_keys=None if it == 1 else self.max_keys // 2,
                                         prev=getattr(self, "_last_gain", None) if it > 1 else None)
            self._last_gain = gain
            improved = False
            page = 1
            for inner in range(self.inner * 3):
                if not self._ok():
                    break
                cands = self._path_moves(S, A, cur, gain, loss, n=self.M * page)
                if inner == 0:
                    cands += self._keystone_moves(S, A, gain, loss)
                if self._counts(S)[0] < self.B[0]:
                    S2, A2 = self._fill(S, A, gain)
                    if S2 != S:
                        cands.append((S2, A2, "fill"))
                if not cands:
                    break
                outs = self._evals([(s, a) for s, a, _ in cands])
                i = max(range(len(cands)), key=lambda j: outs[j][0])
                if outs[i][0] <= cur + 1e-9:
                    if page >= 3:
                        break
                    page += 1          # next page of moves (evaluated ones are cached)
                    continue
                page = 1
                S, A, label = cands[i]
                self._log(f"  LS {it}.{inner}: {label} val {cur:.4f} -> {outs[i][0]:.4f} "
                          f"({len(cands)} moves) evals={self._used()}")
                cur = outs[i][0]
                improved = True
                for x in S:
                    gain[x] = 0.0
            A, cur, fl = self._flips(S, A, cur)
            if not (improved or fl):
                break
        return S, A, cur

    def _perturb(self, S, A, cur):
        br = {b for b in self._branches(S, 6).values() if 3 <= len(b) <= 6}
        if not br:
            return None
        gain, loss = self._linearise(S, A, cur, self.D, with_branches=False)
        bl = sorted(br, key=lambda b: sorted(b))
        outs = self._evals([self._with(S, A, removes=b) for b in bl])
        ranked = sorted(zip(bl, outs), key=lambda x: -x[1][0])[:3]
        b = self.rng.choice(ranked)[0]
        S2, A2 = self._with(S, A, removes=b)
        S2, A2 = self._fill(S2, A2, gain, exclude=set(b), force=True)
        if not self._valid(S2):
            return None
        v = self._evals([(S2, A2)])[0][0]
        self._log(f"  perturb: dropped {len(b)} ({', '.join(self.G.name(x) for x in sorted(b))}) val {v:.4f}")
        return S2, A2, v

    # ================================================================== scratch
    def _scratch_start(self, alloc, attrs, fixed):
        G = self.G
        S: dict = {v: alloc[v] for v in self.free if v in alloc}
        A: dict = {}
        for k in (1, 2):
            if k not in self.active:
                self.B[k] = 0
        d0 = G.paths({G.start}, set(), None, 60, 0.0)
        pins = sorted(fixed - set(S), key=lambda v: len(d0.get(v, [0] * 99)))
        for v in pins:
            if v in S:
                continue
            p = G.paths(self._sources(S, 0), set(S), None, 60, alpha=0.0).get(v)
            if p is None:
                self._log(f"  scratch: pin {G.name(v)} unreachable")
                continue
            S, A = self._with(S, A, p, mode=0)
            if v in attrs:
                A[v] = attrs[v]
        for v in S:
            if G.is_attr(v) and v in attrs:
                A[v] = attrs[v]
        if self.seed_tree and self.seed_tree.get("nodes"):
            S, A = self._seed_start(S, A, d0)
        self.fixed = set(fixed) & set(S)
        self._br_cache.clear()
        self._log(f"  scratch start: {len(S)} nodes (pins + paths)")
        return S, A

    def _seed_start(self, S, A, d0):
        """Add the seed tree's main-tree nodes to S, nearest to the class start first, each by the
        shortest path from what S already holds, while main-tree points last."""
        G = self.G
        sattrs = {int(k): int(v) for k, v in (self.seed_tree.get("attrs") or {}).items() if v}
        want = [int(v) for v in self.seed_tree["nodes"] if int(v) in d0 and int(v) not in S]
        want.sort(key=lambda v: len(d0.get(v, [0] * 99)))
        added = skipped = 0
        for v in want:
            if v in S:
                continue
            p = G.paths(self._sources(S, 0), set(S), None, 60, alpha=0.0).get(v)
            if p is None:
                skipped += 1
                continue
            if self._counts(S)[0] + len([u for u in p if u not in S]) > self.B[0]:
                skipped += 1
                continue
            S, A = self._with(S, A, p, mode=0)
            added += 1
        for v in S:
            if G.is_attr(v) and v in sattrs:
                A[v] = sattrs[v]
        self.report["seed"] = {"nodes": len(want), "added": added, "skipped": skipped}
        self._log(f"  scratch seed: {added} of {len(want)} seed nodes added ({skipped} skipped)")
        return S, A

    def _grow(self, S, A):
        rnd = 0
        done: set = set()
        while self._ok(reserve=500):
            c = self._counts(S)
            todo = [m for m in sorted(self.active) if c[m] < self.B[m] and m not in done]
            if not todo:
                break
            rnd += 1
            cur = self._evals([(S, A)])[0][0]
            gain, _ = self._linearise(S, A, cur, self.grow_depth, with_branches=False)
            m = todo[0]
            # main-tree points are always spent; weapon-set points only while they pay
            S2, A2 = self._fill(S, A, gain, force=(m == 0), limit=self.grow_step, modes=[m])
            if S2 == S:
                done.add(m)
                continue
            if m != 0 and self._evals([(S2, A2)])[0][0] <= cur:
                done.add(m)
                continue
            S, A = S2, A2
            self._log(f"  grow {rnd}: N={dict(Counter(S.values()))} val0={cur:.4f} evals={self._used()}")
        # out of budget: fill the rest from the last gains without evaluating
        if self._counts(S)[0] < self.B[0]:
            S, A = self._fill(S, A, {}, force=True, modes=[0])
        return S, A


# ====================================================================== helpers
def _shortfall(r: dict) -> float:
    """Continuous size of the hard-floor / rule violations of a score result (0 when feasible)."""
    per = r.get("profiles") or {r.get("profile"): r}
    tot = 0.0
    for pr in per.values():
        for f in pr.get("floors") or ():
            if f.get("tier") != "hard" or f.get("passed") is not False:
                continue
            tot += _floor_short(f)
        tot += sum(1.0 for v in pr.get("violations") or () if v.get("rule") != "floor")
    return tot


def _floor_short(f: dict) -> float:
    v, thr, b = f.get("value"), f.get("threshold"), f.get("baseline")
    metric = f.get("metric") or ""
    if v is None or (isinstance(v, float) and math.isinf(v)):
        return 0.0
    le = metric == "attr_deficit"
    unit = metric.startswith("res") or metric in ("attr_deficit", "spirit_unreserved")

    def short(ref):
        if ref is None or (isinstance(ref, float) and math.isinf(ref)):
            return None
        d = (v - ref) if le else (ref - v)
        d = max(0.0, d)
        return d / 10.0 if unit else d / max(abs(ref), 1.0)

    a, rel = short(thr), short(b)
    mode = f.get("mode")
    if mode == "absolute":
        s = a
    elif mode == "relative":
        s = rel if rel is not None else 0.0
    else:
        s = min(x for x in (a, rel) if x is not None) if (a is not None or rel is not None) else 0.0
    return float(s or 0.0) + 0.1
