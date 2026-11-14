"""Passive tree solver POC: progressive linearised budgeted-PCST growth + connected local search,
with PoB (pob-redux engine) as the evaluator. See docs/poc-results.md."""
import heapq, math, time
from collections import defaultdict, deque

LAMBDA = 0.3          # weight on log(EHP)
EHP_FLOOR = 0.95


class Objective:
    def __init__(self, base):
        self.b = base
        self.capped = [k for k in ("fr", "cr", "lr") if base[k] >= base[k + "m"] - 0.5]
        self.def0 = base["deficit"]

    def pen(self, m):
        p = 10.0 * max(0.0, 1.0 - m["ehp"] / (EHP_FLOOR * self.b["ehp"]))
        for k in self.capped:
            p += 0.03 * max(0.0, m[k + "m"] - m[k])
        p += 0.01 * max(0.0, m["deficit"] - self.def0)
        return p

    def f(self, m):
        if m is None or "err" in m:
            return -1e9
        return math.log(m["dps"] + 1.0) + LAMBDA * math.log(m["ehp"] + 1.0) - self.pen(m)

    def feasible(self, m):
        return (m["ehp"] >= EHP_FLOOR * self.b["ehp"] - 1e-6 and all(m[k] >= m[k + "m"] - 0.5 for k in self.capped)
                and m["deficit"] <= self.def0 + 1e-6)


class Solver:
    def __init__(self, pob, graph, obj, pins, log=print):
        self.pob, self.obj, self.log = pob, obj, log
        self.start = graph["start"]
        nodes = {x["id"]: x for x in graph["nodes"]}
        bad = {i for i, x in nodes.items() if x["granted"] or x["ascStart"] or (x["t"] == "ClassStart" and i != self.start)}
        self.adj = defaultdict(set)
        for i, x in nodes.items():
            if i in bad:
                continue
            for j in x["linked"]:
                if j in nodes and j not in bad:
                    self.adj[i].add(j); self.adj[j].add(i)
        # restrict to the root's component
        seen = {self.start}; dq = deque([self.start])
        while dq:
            u = dq.popleft()
            for v in self.adj[u]:
                if v not in seen:
                    seen.add(v); dq.append(v)
        self.V = seen
        self.info = nodes
        self.pins = set(p for p in pins if p in seen)
        self.attr_rep = None
        self.free = set()   # nodes allowed in T without a path (player nodes reached via jewel/leap effects)
        self.evals = 0
        self.cache = {}

    # ---------- evaluation ----------
    def key(self, v, attrs):
        x = self.info[v]
        if x["attr"]:
            return "ATTR%d" % attrs.get(str(v), 0)
        return x["mk"] or ("EMPTY%d" % v if x["jewel"] else "")

    def evaluate(self, sets):
        """sets: list of (ids, attrs) -> list of metric dicts (true PoB evals)"""
        out = [None] * len(sets)
        jobs, idx = [], []
        for i, (ids, attrs) in enumerate(sets):
            k = (frozenset(ids), frozenset((a, c) for a, c in attrs.items() if int(a) in ids))
            if k in self.cache:
                out[i] = self.cache[k]
            else:
                jobs.append({"add": sorted(ids), "attrs": {a: c for a, c in attrs.items() if int(a) in ids} or {"0": 0}}); idx.append((i, k))
        for s in range(0, len(jobs), 40):
            res = self.pob.evalb(jobs[s:s + 40])
            for (i, k), r in zip(idx[s:s + 40], res):
                out[i] = r; self.cache[k] = r
        self.evals += len(jobs)
        return out

    def fval(self, T, attrs):
        return self.obj.f(self.evaluate([(T, attrs)])[0])

    def linearize(self, T, attrs, cand):
        """marginal gain of adding each candidate node (one eval per distinct mod key; attribute nodes: best choice)."""
        f0 = self.fval(T, attrs)
        groups = defaultdict(list)
        for v in cand:
            x = self.info[v]
            if x["attr"]:
                groups["ATTR"].append(v)
            elif x["mk"]:
                groups[x["mk"]].append(v)
        sets, keys = [], []
        for k, vs in groups.items():
            if k == "ATTR":
                for c in (1, 2, 3):
                    a2 = dict(attrs); a2[str(vs[0])] = c
                    sets.append((T | {vs[0]}, a2)); keys.append(("ATTR", c))
            else:
                sets.append((T | {vs[0]}, attrs)); keys.append((k, None))
        res = self.evaluate(sets)
        gain, kg, ag = {}, {}, {}
        for (k, c), r in zip(keys, res):
            g = self.obj.f(r) - f0
            if k == "ATTR":
                ag[c] = g
            else:
                kg[k] = g
        best_attr = max(ag.items(), key=lambda x: x[1]) if ag else (1, 0.0)
        for k, vs in groups.items():
            for v in vs:
                gain[v] = best_attr[1] if k == "ATTR" else kg[k]
        return f0, gain, best_attr[0]

    # ---------- graph helpers ----------
    def dist_from(self, T, maxd):
        d = {v: 0 for v in T}; dq = deque(T)
        while dq:
            u = dq.popleft()
            if d[u] >= maxd:
                continue
            for w in self.adj[u]:
                if w not in d:
                    d[w] = d[u] + 1; dq.append(w)
        return d

    def best_paths(self, T, prize, maxlen):
        """multi-source Dijkstra from T over non-T nodes; node cost 1 - 0.5*clip(prize/pmax).
        returns dict v -> path (list of new nodes, root side first)."""
        pmax = max([p for p in prize.values() if p > 0] or [1.0])
        dist, prev, hops = {}, {}, {}
        pq = []
        for u in T:
            for w in self.adj[u]:
                if w not in T:
                    c = 1 - 0.5 * min(max(prize.get(w, 0) / pmax, 0), 1)
                    if c < dist.get(w, 1e9):
                        dist[w] = c; prev[w] = None; hops[w] = 1
                        heapq.heappush(pq, (c, w))
        done = set()
        while pq:
            c, u = heapq.heappop(pq)
            if u in done or c > dist[u] + 1e-12:
                continue
            done.add(u)
            if hops[u] >= maxlen:
                continue
            for w in self.adj[u]:
                if w in T or w in done:
                    continue
                cw = c + 1 - 0.5 * min(max(prize.get(w, 0) / pmax, 0), 1)
                if cw < dist.get(w, 1e9) - 1e-12:
                    dist[w] = cw; prev[w] = u; hops[w] = hops[u] + 1
                    heapq.heappush(pq, (cw, w))
        paths = {}
        for v in done:
            p = [v]; u = prev[v]
            while u is not None:
                p.append(u); u = prev[u]
            paths[v] = p[::-1]
        return paths

    def shortest_path(self, T, target):
        prev = {v: None for v in T}; dq = deque(T)
        while dq:
            u = dq.popleft()
            if u == target:
                break
            for w in self.adj[u]:
                if w not in prev:
                    prev[w] = u; dq.append(w)
        p = []; u = target
        while u not in T:
            p.append(u); u = prev[u]
        return p[::-1]

    def articulation(self, T):
        """articulation points of the induced subgraph on T (iterative Tarjan), root-connected."""
        disc, low, ap = {}, {}, set()
        t = 0
        root = self.start
        stack = [(root, None, iter(self.adj[root] & T))]
        disc[root] = low[root] = 0; children = 0
        while stack:
            u, parent, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                stack.pop()
                if parent is not None:
                    low[parent] = min(low[parent], low[u])
                    if parent != root and low[u] >= disc[parent]:
                        ap.add(parent)
                continue
            if nxt == parent:
                continue
            if nxt in disc:
                low[u] = min(low[u], disc[nxt])
            else:
                t += 1; disc[nxt] = low[nxt] = t
                if u == root:
                    children += 1
                stack.append((nxt, u, iter(self.adj[nxt] & T)))
        if children > 1:
            ap.add(root)
        return ap

    def connected(self, T):
        seen = {self.start}; dq = deque([self.start])
        while dq:
            u = dq.popleft()
            for w in self.adj[u] & T:
                if w not in seen:
                    seen.add(w); dq.append(w)
        return len(seen | (self.free & T)) == len(T)

    # ---------- phase 1: progressive linearised PCST growth ----------
    def grow(self, N, step=12, maxd=10):
        T = {self.start}; attrs = {}
        for p in sorted(self.pins, key=lambda p: len(self.shortest_path(T, p))):
            for v in self.shortest_path(T, p):
                T.add(v)
                if self.info[v]["attr"]:
                    attrs[str(v)] = 1
        rounds = 0
        while len(T) - 1 < N:
            rounds += 1
            remaining = N - (len(T) - 1)
            d = self.dist_from(T, maxd)
            cand = [v for v in d if v not in T]
            f0, gain, ac = self.linearize(T, attrs, cand)
            prize = dict(gain)
            spent = 0
            while spent < min(step, remaining) and len(T) - 1 < N:
                rem = N - (len(T) - 1)
                paths = self.best_paths(T, prize, min(rem, maxd))
                best, bscore = None, -1e18
                for v, p in paths.items():
                    s = sum(max(prize.get(u, 0), 0) for u in p) / len(p)
                    if s > bscore:
                        best, bscore = p, s
                if best is None:
                    break
                for u in best:
                    T.add(u)
                    if self.info[u]["attr"]:
                        attrs[str(u)] = ac
                    prize[u] = 0
                spent += len(best)
            self.log(f"  grow round {rounds}: |T|-1={len(T)-1} f0={f0:.3f} cand={len(cand)} evals={self.evals}")
        return T, attrs

    # ---------- phase 2: local search ----------
    def removable(self, T):
        ap = self.articulation(T)
        return [v for v in T if v != self.start and v not in self.pins and v not in ap and v not in self.free]

    def local_search(self, T, attrs, max_evals=1500, log_every=True):
        T = set(T); attrs = dict(attrs)
        e0 = self.evals
        cur = self.fval(T, attrs)
        it = 0
        while self.evals - e0 < max_evals:
            it += 1
            improved = False
            # linearise: removal losses and frontier gains (frontier up to distance 3 for path moves)
            rem = self.removable(T)
            rsets = [(T - {u}, attrs) for u in rem]
            loss = {u: cur - self.obj.f(r) for u, r in zip(rem, self.evaluate(rsets))}
            d = self.dist_from(T, 3)
            cand = [v for v in d if v not in T]
            _, gain, ac = self.linearize(T, attrs, cand)
            # 1-swaps
            adds = sorted([v for v in cand if d[v] == 1], key=lambda v: -gain.get(v, 0))[:12]
            rems = sorted(rem, key=lambda u: loss[u])[:12]
            moves = []
            for v in adds:
                for u in rems:
                    if u == v:
                        continue
                    if not (self.adj[v] & (T - {u})):
                        continue
                    moves.append((gain.get(v, 0) - loss[u], [u], [v]))
            # twig/path moves: remove a leaf chain of length k, add best path of length k
            leaves = [u for u in rem if len(self.adj[u] & T) == 1]
            chains = []
            for lf in leaves:
                ch = [lf]; u = lf
                while len(ch) < 4:
                    nb = list(self.adj[u] & T - set(ch))
                    if len(nb) != 1:
                        break
                    w = nb[0]
                    if w == self.start or w in self.pins or len(self.adj[w] & T) != 2:
                        break
                    ch.append(w); u = w
                for k in range(2, len(ch) + 1):
                    chains.append(ch[:k])
            paths = self.best_paths(T, gain, 4)
            plist = sorted(paths.values(), key=lambda p: -sum(gain.get(x, 0) for x in p))[:40]
            for ch in sorted(chains, key=lambda c: sum(loss.get(x, 0) for x in c))[:20]:
                lc = sum(loss.get(x, 0) for x in ch)
                for p in plist:
                    if len(p) == len(ch) and not (set(p) & set(ch)) and (self.adj[p[0]] & (T - set(ch))):
                        moves.append((sum(gain.get(x, 0) for x in p) - lc, ch, p))
            moves.sort(key=lambda m: -m[0])
            tried = 0
            for est, out, inn in moves:
                if tried >= 40 or self.evals - e0 >= max_evals:
                    break
                if not all(u in T for u in out) or any(v in T for v in inn):
                    continue
                T2 = (T - set(out)) | set(inn)
                if not self.connected(T2):
                    continue
                a2 = dict(attrs)
                for v in inn:
                    if self.info[v]["attr"]:
                        a2[str(v)] = ac
                tried += 1
                f2 = self.fval(T2, a2)
                if f2 > cur + 1e-6:
                    T, attrs, cur = T2, a2, f2; improved = True
            # attribute flips: one representative node per (from -> to) pair, greedy while improving
            for _ in range(30):
                if self.evals - e0 >= max_evals:
                    break
                byc = defaultdict(list)
                for u in T:
                    if self.info[u]["attr"]:
                        byc[attrs.get(str(u), 0)].append(u)
                best = None
                for c0, vs in byc.items():
                    for c in (1, 2, 3):
                        if c == c0:
                            continue
                        a2 = dict(attrs); a2[str(vs[0])] = c
                        f2 = self.fval(T, a2)
                        if f2 > cur + 1e-6 and (best is None or f2 > best[0]):
                            best = (f2, a2)
                if not best:
                    break
                cur, attrs = best; improved = True
            if log_every:
                self.log(f"  LS iter {it}: f={cur:.4f} moves={len(moves)} tried={tried} evals={self.evals}")
            if not improved:
                break
        return T, attrs, cur
