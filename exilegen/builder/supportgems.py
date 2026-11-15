"""Support gem names, and a way to keep named supports out of a build.

    names()            every support gem's display name, sorted (for a picker)
    lineage_names()    the ones that are lineage gems (Rakiata's Flow, Uul-Netol's Embrace, ...)
    unknown(names)     the given names that are not a support gem (compared without regard to case)
    banned(engine, names)   context manager: while it is open the engine does not offer these supports

The names come from PoB's gem table (`python3 -m exilegen.builder.supportgems --pob-root <PathOfBuilding-PoE2>/src`
writes data/supports.json): gems tagged `support`, with the `lineage` tag kept.

Why `banned` and not a "forbid_gem:X" constraint: a constraint only marks the build as violating once the gem is
socketed. The support steps compare two infeasible builds by objective alone, so on a build that already misses a
floor a forbidden support can still be socketed. `banned` removes the gems from what the engine lists as legal
supports (`valid_supports`), which is where every support search takes its candidates from: the first fill
(synth.fill_supports), the support polish step and the trigger step's support moves.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re

from . import catalog as catmod

PATH = os.path.join(catmod.DATA_DIR, "supports.json")
_LOADED: dict[str, list[dict]] = {}


def parse(pob_root: str) -> list[dict]:
    """[{name, lineage}] for every support gem in <pob_root>/Data/Gems.lua, sorted by name."""
    text = open(os.path.join(pob_root, "Data", "Gems.lua"), encoding="utf-8", errors="replace").read()
    out: dict[str, dict] = {}
    for m in re.finditer(r'\n\t\["[^"]+"\] = \{(.*?)\n\t\},', text, re.S):
        body = m.group(1)
        name = re.search(r'\n\t\tname = "([^"]+)"', body)
        tags = re.search(r'\n\t\ttags = \{(.*?)\n\t\t\}', body, re.S)
        tagset = set(re.findall(r'(\w+) = true', tags.group(1))) if tags else set()
        if not name or "support" not in tagset:
            continue
        g = out.setdefault(name.group(1), {"name": name.group(1), "lineage": False})
        g["lineage"] = g["lineage"] or "lineage" in tagset
    return sorted(out.values(), key=lambda g: g["name"])


def load(path: str = PATH) -> list[dict]:
    if path not in _LOADED:
        _LOADED[path] = json.load(open(path))["supports"] if os.path.exists(path) else []
    return _LOADED[path]


def names(path: str = PATH) -> list[str]:
    return sorted(g["name"] for g in load(path))


def lineage_names(path: str = PATH) -> list[str]:
    return sorted(g["name"] for g in load(path) if g.get("lineage"))


def norm(names_) -> set[str]:
    """The names as a set for comparing: trimmed, lower case, empty ones dropped."""
    return {str(n).strip().lower() for n in names_ or [] if str(n).strip()}


def unknown(names_, path: str = PATH) -> list[str]:
    known = norm(names(path))
    return [n for n in names_ or [] if str(n).strip().lower() not in known]


def without(valid: dict, ban: set[str]) -> dict:
    """An engine `valid_supports` answer without the banned supports. One that is already socketed stays
    listed, so a search can still see it and replace it."""
    if not ban or not isinstance(valid, dict):
        return valid
    return dict(valid, supports=[s for s in valid.get("supports") or []
                                 if s.get("socketed") or str(s.get("name") or "").strip().lower() not in ban])


@contextlib.contextmanager
def banned(engine, names_):
    """While open, `engine.valid_supports` leaves out the named supports. Nothing changes for an empty list."""
    ban = norm(names_)
    if not ban:
        yield engine
        return
    inner = engine.valid_supports

    def valid_supports(*a, **kw):
        return without(inner(*a, **kw), ban)
    engine.valid_supports = valid_supports
    try:
        yield engine
    finally:
        try:
            del engine.valid_supports      # back to the class's method
        except AttributeError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract support gem names and the lineage flag from PoB data")
    ap.add_argument("--pob-root", required=True, help="PathOfBuilding-PoE2/src")
    ap.add_argument("--source", default="", help="label for the data revision")
    ap.add_argument("--out", default=PATH)
    a = ap.parse_args(argv)
    sup = parse(a.pob_root)
    with open(a.out, "w") as f:
        json.dump({"supports": sup, "source": a.source}, f, separators=(",", ":"))
    print(f"{len(sup)} support gems, {sum(1 for g in sup if g['lineage'])} lineage -> {a.out}")


if __name__ == "__main__":
    main()
