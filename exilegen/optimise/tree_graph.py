"""Passive-tree graph model for the tree step: adjacency, PoB-style connectivity with weapon-set
modes, cut branches, and prize-guided shortest paths.

A tree state is a dict {node id: mode} over main-tree nodes (no class start, no ascendancy), where
mode 0 is a normal node and 1/2 a weapon-set node. Connectivity mirrors the engine's resolveAlloc
(exg.lua): a mode-0 node must reach the class start through mode-0 nodes, a mode-k node through
mode-0 and mode-k nodes, and paths never leave a Mastery node. Nodes in `free` (path-free nodes
kept by jewel effects) are exempt.
"""
from __future__ import annotations

import heapq
from collections import deque


class TreeGraph:
    def __init__(self, view: dict, banned: set | None = None):
        """view: engine tree op result with graph=True."""
        self.start = int(view["startNode"])
        self.facts = {int(k): v for k, v in view["nodes"].items()}
        banned = set(banned or ())
        self.banned = banned
        ok = set()
        for i, f in self.facts.items():
            if f.get("asc") or f.get("kind") in ("ascendancy", "ascendancy_notable", "ascendancy_start"):
                continue
            if f.get("kind") == "start" and i != self.start:
                continue
            if i in banned:
                continue
            ok.add(i)
        self.adj: dict[int, set] = {i: set() for i in ok}
        for i in ok:
            for j in self.facts[i].get("linked") or ():
                j = int(j)
                if j in ok:
                    self.adj[i].add(j)
                    self.adj[j].add(i)
        self.mastery = {i for i in ok if self.facts[i].get("kind") == "mastery"}

    # ------------------------------------------------------------ facts
    def kind(self, i):
        return self.facts.get(i, {}).get("kind")

    def name(self, i):
        return self.facts.get(i, {}).get("name") or str(i)

    def is_attr(self, i):
        f = self.facts.get(i, {})
        return bool(f.get("attribute")) or f.get("kind") == "attribute"

    def key(self, i):
        """Stat key: nodes with the same key have (nearly) the same effect."""
        f = self.facts.get(i, {})
        if self.is_attr(i):
            return ("attr",)
        k = f.get("kind")
        if k in ("notable", "keystone", "jewel"):
            return (k, i)
        return (k, tuple(f.get("sd") or ()))

    # ------------------------------------------------------------ connectivity
    def reached(self, S: dict) -> set:
        """Nodes of S that keep their path to the class start (engine resolveAlloc rules)."""
        kept: set = set()
        for m in (0, 1, 2):
            if m and not any(v == m for v in S.values()):
                continue
            seen = {self.start}
            q = deque([self.start])
            while q:
                u = q.popleft()
                if u in self.mastery:
                    continue
                for v in self.adj.get(u, ()):
                    if v in seen:
                        continue
                    mv = S.get(v)
                    if mv is None:
                        continue
                    if mv == 0 or mv == m:
                        seen.add(v)
                        q.append(v)
            for v in seen:
                if v != self.start and S.get(v) == m:
                    kept.add(v)
        return kept

    def unreached(self, S: dict, free: set = frozenset()) -> set:
        return set(S) - self.reached(S) - set(free)

    def connected(self, S: dict, free: set = frozenset()) -> bool:
        return not self.unreached(S, free)

    def branches(self, S: dict, fixed: set, free: set, max_size: int = 12) -> dict:
        """{u: frozenset(branch)} for every movable u: the nodes cut off when u is removed (u
        included). Branches that contain a fixed node or exceed max_size are left out."""
        out = {}
        for u in S:
            if u in fixed:
                continue
            S2 = dict(S)
            del S2[u]
            cut = self.unreached(S2, free)
            if len(cut) + 1 > max_size or cut & fixed:
                continue
            out[u] = frozenset(cut | {u})
        return out

    # ------------------------------------------------------------ paths
    def paths(self, sources, blocked, prize: dict | None = None, maxlen: int = 6, alpha: float = 0.5) -> dict:
        """Multi-source Dijkstra from `sources` over nodes not in `blocked`; node cost
        1 - alpha*clip(prize/pmax). Returns {v: [new nodes, source side first]} for paths of at most
        maxlen nodes. Masteries are never traversed out of."""
        prize = prize or {}
        pos = [p for p in prize.values() if p > 0]
        pmax = max(pos) if pos else 1.0

        def cost(w):
            return 1.0 - alpha * min(max(prize.get(w, 0.0) / pmax, 0.0), 1.0)

        dist, prev, hops = {}, {}, {}
        pq = []
        for u in sources:
            if u in self.mastery and u != self.start:
                continue
            for w in self.adj.get(u, ()):
                if w in blocked or w in sources:
                    continue
                c = cost(w)
                if c < dist.get(w, 1e18):
                    dist[w], prev[w], hops[w] = c, None, 1
                    heapq.heappush(pq, (c, w))
        done = set()
        while pq:
            c, u = heapq.heappop(pq)
            if u in done or c > dist[u] + 1e-12:
                continue
            done.add(u)
            if hops[u] >= maxlen or u in self.mastery:
                continue
            for w in self.adj.get(u, ()):
                if w in blocked or w in sources or w in done:
                    continue
                cw = c + cost(w)
                if cw < dist.get(w, 1e18) - 1e-12:
                    dist[w], prev[w], hops[w] = cw, u, hops[u] + 1
                    heapq.heappush(pq, (cw, w))
        out = {}
        for v in done:
            p = [v]
            u = prev[v]
            while u is not None:
                p.append(u)
                u = prev[u]
            out[v] = p[::-1]
        return out

    def hop_dist(self, sources, blocked, maxd: int) -> dict:
        d = {}
        q = deque()
        for u in sources:
            for w in self.adj.get(u, ()):
                if w not in blocked and w not in sources and w not in d:
                    d[w] = 1
                    q.append(w)
        while q:
            u = q.popleft()
            if d[u] >= maxd or u in self.mastery:
                continue
            for w in self.adj.get(u, ()):
                if w not in blocked and w not in sources and w not in d:
                    d[w] = d[u] + 1
                    q.append(w)
        return d
