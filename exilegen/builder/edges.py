"""Typed edges: every mechanics-graph link read again against structured game data.

    python3 -m exilegen.builder.edges            # summary of the typing over the whole graph

mechgraph reads a link from the words of a line, with one relation each way (produces, consumes). This module
gives each link a relation, an owner and a verdict from two structured sources, in this order:

  stat ids   data/stat_ids.json: the game's own stat ids of the line (uniques) or of the entity (skills,
             notables), read by statlinks.analyse
  PoB tags   the parsed mod's tag: a bare Condition is about you, an ActorCondition with actor "enemy" about the enemy

  relation   applies | generates | spends | while | scales | raises_cap | amplifies | forbids | converts
  owner      enemy | self: who holds the state the link is about
  verdict    confirmed     a structured source agrees with the text reading
             contradicted  structured data says the link is something else (a cap, an immunity, a state on you,
                           an amplifier of the state); `why` says what
             text          no structured source speaks about this state on this line: the text reading stands

`typed(graph)` -> {(entity key, "p"|"c", token): Edge}. `chain_doubts(graph, edges, chain)` -> sentences for a
chain whose only source or only payoff for a link is contradicted.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import asdict, dataclass, field

from . import catalog as catmod
from . import mechgraph
from . import statlinks

PATH = os.path.join(catmod.DATA_DIR, "stat_ids.json")
ENEMY_TAGS = {"ActorCondition"}
AMOUNT = set(mechgraph.WEAK_TOKENS) | {"crit", "block", "presence", "minion", "es", "totem"}
_IDS: dict | None = None


def load_ids(path: str = PATH) -> dict[str, dict]:
    global _IDS
    if _IDS is None:
        _IDS = json.load(open(path))["entities"] if os.path.exists(path) else {}
    return _IDS


@dataclass
class Edge:
    entity: str
    side: str                 # "p" (the text read a source) or "c" (the text read a payoff or use)
    token: str
    relation: str
    owner: str
    verdict: str
    why: str = ""
    stat_ids: list = field(default_factory=list)
    line: str = ""


def _owner_default(token: str) -> str:
    return "enemy" if token.startswith("enemy:") else "self"


def _ids_for(key: str, e: mechgraph.Entity, line: str, ids: dict) -> list[str]:
    rec = ids.get(key) or (ids.get("skill:" + e.meta["skill"]) if e.meta.get("skill") else None) or {}
    per_line = rec.get("lines") or {}
    if per_line:
        if line in per_line:
            return per_line[line]
        hit = [v for k, v in per_line.items() if k.startswith(line[:60]) or line.startswith(k[:60])]
        return hit[0] if hit else []
    return rec.get("ids") or []


def _tag_owner(cat: catmod.Catalog, line: str, token: str) -> str | None:
    """Who PoB's parsed tag says holds an `enemy:` state on this line: "self", "enemy" or None (no tag about it)."""
    if not token.startswith("enemy:"):
        return None
    name = token.split(":", 1)[1]
    seen = set()
    for m in (cat.parsed.get(line) or {}).get("mods", []):
        for t in m.get("tags") or []:
            var = str(t.get("var") or "")
            if var != name and not (name == "Stunned" and var in ("HeavyStunned", "Stunned")):
                continue
            seen.add("enemy" if t.get("type") in ENEMY_TAGS or t.get("actor") == "enemy" else "self")
    return "self" if seen == {"self"} else "enemy" if "enemy" in seen else None


AGREE = {"p": {"produces"}, "c": {"payoff_enemy", "payoff_self", "consumes"}}
OTHER = {"cap": ("raises_cap", "it raises the maximum, it does not make any"),
         "blocks": ("forbids", "it prevents the state or makes you immune to it"),
         "scales": ("amplifies", "it changes how strong or how long the state is, it neither makes nor uses it"),
         "consumes": ("spends", "it spends or removes the state"),
         "payoff_enemy": ("while", "it pays off on the state, it does not make it"),
         "payoff_self": ("while", "it pays off on the state, it does not make it"),
         "produces": ("applies", "it makes the state, it does not pay off on it")}


def _type(key: str, e: mechgraph.Entity, side: str, link: mechgraph.Link, cat: catmod.Catalog, ids: dict) -> Edge:
    tok = link.token
    default = _owner_default(tok)
    base_rel = ("applies" if default == "enemy" else "generates") if side == "p" else ("scales" if link.payoff else "while")
    edge = Edge(key, side, tok, base_rel, default, "text", line=link.line)
    sids = _ids_for(key, e, link.line, ids)
    per_line = bool((ids.get(key) or {}).get("lines"))
    if tok.startswith("dmg:"):
        conv = [(s, r) for s in sids for t, r in statlinks.conversions(s) if t == tok]
        if conv:
            want = "produces" if side == "p" else "consumes"
            edge.stat_ids = sorted({s for s, _ in conv})
            edge.relation = "converts"
            edge.verdict = "confirmed" if any(r == want for _, r in conv) else "text"
        return edge
    found = [(s, a) for s in sids for a in statlinks.analyse(s) if a["state"] == tok and a["role"] != "mention"]
    tag = _tag_owner(cat, link.line, tok)
    if found:
        edge.stat_ids = sorted({s for s, _ in found})
        agree = [a for _, a in found if a["role"] in AGREE[side]]
        on_self = [a for a in (agree or [a for _, a in found]) if a["target"] == "self"]
        if default == "enemy" and on_self and len(on_self) == len(agree or found):
            edge.owner, edge.verdict = "self", "contradicted"
            edge.why = "the state is on you, not on the enemy"
            edge.relation = "while" if side == "c" else "applies"
        elif agree:
            edge.verdict = "confirmed"
            role = agree[0]["role"]
            edge.relation = "spends" if role == "consumes" else base_rel
        else:
            role = Counter(a["role"] for _, a in found).most_common(1)[0][0]
            edge.relation, edge.why = OTHER[role]
            # Not every disagreement is a contradiction:
            #   amount tokens (armour, evasion, crit, block, attributes): a cap or an increase is what having the stat means
            #   whole-entity ids (skills, notables): the ids are not this line's, so only a hard "never" speaks against a source
            #   an immunity or "never" says nothing against a payoff
            amount = tok in AMOUNT or tok.startswith("stat:")
            soft = (amount and role in ("scales", "cap")) or (not per_line and role != "blocks") or (side == "c" and role == "blocks")
            edge.verdict = "text" if soft else "contradicted"
            if role == "blocks" and e.kind in ("skill", "util"):
                edge.why = "the game data says this skill can never inflict it"
            if soft:
                edge.relation, edge.why = base_rel, ""
    if tag == "self" and edge.verdict != "contradicted" and default == "enemy":
        edge.owner, edge.verdict, edge.why = "self", "contradicted", "PoB reads the condition as yours, not the enemy's"
    elif tag == "enemy" and edge.verdict == "text":
        edge.verdict = "confirmed"
    return edge


def typed(g: mechgraph.MechGraph, ids: dict | None = None) -> dict[tuple, Edge]:
    ids = load_ids() if ids is None else ids
    out = {}
    for key, e in g.entities.items():
        for side, links in (("p", e.produces), ("c", e.consumes)):
            for tok, link in links.items():
                if tok.startswith("skill:"):
                    continue
                out[(key, side, tok)] = _type(key, e, side, link, g.cat, ids)
    return out


def chain_doubts(g: mechgraph.MechGraph, edges: dict[tuple, Edge], chain: dict) -> list[str]:
    """A chain link is in doubt when every part that should make its state, or every part that should pay off on
    it, is contradicted by the game data."""
    out = []
    for tok in chain.get("tokens") or []:
        for side, what in (("p", "source"), ("c", "payoff")):
            es = [edges[(k, side, tok)] for k in chain["path"] if (k, side, tok) in edges]
            if es and all(x.verdict == "contradicted" for x in es):
                x = es[0]
                name = g.entities[x.entity].name
                out.append(f"Game data disagrees with the {what} for {tok.split(':', 1)[-1]}: {name}: {x.why} "
                           f"(\"{x.line[:110]}\"; {', '.join(x.stat_ids[:2]) or 'PoB tag'}).")
    return out


def summary(g: mechgraph.MechGraph, edges: dict[tuple, Edge]) -> dict:
    by = Counter((x.side, x.verdict) for x in edges.values())
    strong = [x for x in edges.values() if x.token not in mechgraph.WEAK_TOKENS and not x.token.startswith("dmg:")]
    return {"edges": len(edges), "by_side_verdict": {f"{s}:{v}": n for (s, v), n in sorted(by.items())},
            "strong_tokens": dict(Counter(x.verdict for x in strong)),
            "contradicted_why": dict(Counter(x.why for x in edges.values() if x.verdict == "contradicted")),
            "by_kind": {k: dict(Counter(x.verdict for x in edges.values() if g.entities[x.entity].kind == k))
                        for k in ("unique", "keystone", "asc", "skill", "util")}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="type the mechanics graph's links against stat ids and PoB tags")
    ap.add_argument("--list", metavar="VERDICT", help="print the edges with this verdict")
    a = ap.parse_args(argv)
    g = mechgraph.MechGraph()
    edges = typed(g)
    if a.list:
        for x in edges.values():
            if x.verdict == a.list:
                print(json.dumps(asdict(x)))
        return 0
    print(json.dumps(summary(g, edges), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
