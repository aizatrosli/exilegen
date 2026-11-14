"""Fit a chain score to PoB measurements (a chainscore run), replacing the hand-set formula for ranking.

Two ridge models over features that are known before a chain is built:

    works   chance the chain turns into a build with damage that can be compared (0..1)
    gain    log of gain_credited (chain DPS over the plain build, credited with its own states)

    predicted = works x exp(gain)      the expected damage multiple of adding the chain

Features: chain kind, part counts, the formula's own terms, main skill traits, state tokens, and one indicator
per part (unique, keystone, ascendancy node, helper skill) seen at least MIN_SEEN times. The per-part weights
are the learned link weights: how much a part moves measured gain on average, shrunk towards zero.

Honest by construction: `evaluate` holds out whole main skills (grouped folds), so a chain is never predicted
from another chain on the same skill.

CLI:  python3 -m exilegen.builder.chainfit --scores <run dir> [--out exilegen/builder/data/chain_model.json]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter

MIN_SEEN = 4
GAIN_CLIP = (math.log(0.05), math.log(20.0))
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "chain_model.json")


def features(c: dict) -> dict[str, float]:
    t = c.get("tempo") or {}
    f = {"bias": 1.0, "kind:" + c["kind"]: 1.0, "length": float(c.get("length", 2)),
         "n_uniques": float(len(c.get("uniques") or [])), "n_keystones": float(len(c.get("keystones") or [])),
         "n_asc": float(len(c.get("asc_nodes") or [])), "n_util": float(len(c.get("util_skills") or [])),
         "fit": float(c.get("fit", 0)), "novelty": float(c.get("novelty", 0)),
         "tempo": float(t.get("tempo", 1.0) if isinstance(t, dict) else t)}
    if c.get("main_spell"):
        f["main:spell"] = 1.0
    if c.get("main_minion"):
        f["main:minion"] = 1.0
    for d in c.get("main_damage_types") or []:
        f["dmg:" + d] = 1.0
    for tok in c.get("tokens") or []:
        f["token:" + tok] = 1.0
        f["group:" + tok.split(":", 1)[0]] = 1.0
    for u in c.get("uniques") or []:
        f["unique:" + u] = 1.0
    for k in c.get("keystones") or []:
        f["keystone:" + k] = 1.0
    for a, n in c.get("asc_nodes") or []:
        f["asc:" + a + ":" + n] = 1.0
        f["ascendancy:" + a] = 1.0
    for s in c.get("util_skills") or []:
        f["util:" + (s if isinstance(s, str) else str(s))] = 1.0
    return f


_DENSE = ("bias", "length", "n_uniques", "n_keystones", "n_asc", "n_util", "fit", "novelty", "tempo")


def _vocab(rows: list[dict[str, float]]) -> list[str]:
    seen = Counter(k for r in rows for k in r)
    return sorted(k for k, n in seen.items() if k in _DENSE or n >= MIN_SEEN)


def _ridge(X, y, lam: float):
    import numpy as np
    reg = lam * np.eye(X.shape[1])
    reg[0, 0] = 0.0   # the bias is not shrunk
    return np.linalg.solve(X.T @ X + reg, X.T @ y)


def _matrix(rows, vocab):
    import numpy as np
    ix = {k: i for i, k in enumerate(vocab)}
    X = np.zeros((len(rows), len(vocab)))
    for i, r in enumerate(rows):
        for k, v in r.items():
            if k in ix:
                X[i, ix[k]] = v
    return X


def targets(chains: list[dict], measured: dict[str, dict]):
    """(chain, works 0/1, log gain or None) for every chain the run scored."""
    out = []
    for c in chains:
        m = measured.get(c["id"])
        if not m:
            continue
        g = m.get("gain_credited")
        out.append((c, 1.0 if g is not None else 0.0,
                    min(max(math.log(g), GAIN_CLIP[0]), GAIN_CLIP[1]) if g else None))
    return out


def fit(chains: list[dict], measured: dict[str, dict], lam: float = 3.0) -> dict:
    import numpy as np
    data = targets(chains, measured)
    rows = [features(c) for c, _, _ in data]
    vocab = _vocab(rows)
    X = _matrix(rows, vocab)
    w_works = _ridge(X, np.array([w for _, w, _ in data]), lam)
    has = [i for i, (_, _, g) in enumerate(data) if g is not None]
    w_gain = _ridge(X[has], np.array([data[i][2] for i in has]), lam)
    return {"version": 1, "lam": lam, "n": len(data), "n_gain": len(has), "vocab": vocab,
            "works": [round(float(x), 5) for x in w_works], "gain": [round(float(x), 5) for x in w_gain]}


def predict(model: dict, c: dict) -> dict:
    f = features(c)
    works = gain = 0.0
    why = []
    for k, ww, wg in zip(model["vocab"], model["works"], model["gain"]):
        v = f.get(k)
        if v:
            works += ww * v
            gain += wg * v
            if k not in _DENSE:
                why.append((wg * v, k))
    works = min(max(works, 0.0), 1.0)
    gain = min(max(gain, GAIN_CLIP[0]), GAIN_CLIP[1])
    why.sort(key=lambda x: -abs(x[0]))
    return {"works": round(works, 3), "gain": round(math.exp(gain), 3), "score": round(works * math.exp(gain), 4),
            "why": [{"part": k, "effect": round(math.exp(e), 3)} for e, k in why[:4]]}


def _spearman(a, b) -> float:
    import numpy as np
    if len(a) < 3:
        return float("nan")
    ra, rb = np.argsort(np.argsort(a)).astype(float), np.argsort(np.argsort(b)).astype(float)
    return float(np.corrcoef(ra, rb)[0, 1]) if ra.std() and rb.std() else float("nan")


def evaluate(chains: list[dict], measured: dict[str, dict], lam: float = 3.0, folds: int = 5) -> dict:
    """Grouped cross-validation: every main skill is held out as a whole."""
    import numpy as np
    data = targets(chains, measured)
    skills = sorted({c["main_skill"] for c, _, _ in data})
    fold_of = {s: i % folds for i, s in enumerate(skills)}
    pred: dict[str, dict] = {}
    for k in range(folds):
        train = [c for c, _, _ in data if fold_of[c["main_skill"]] != k]
        model = fit(train, measured, lam)
        for c, _, _ in data:
            if fold_of[c["main_skill"]] == k:
                pred[c["id"]] = predict(model, c)
    g = [(pred[c["id"]]["gain"], math.exp(y), c) for c, _, y in data if y is not None]
    allr = [(pred[c["id"]]["score"], (math.exp(y) if y is not None else 0.0), c) for c, _, y in data]
    works = [(pred[c["id"]]["works"], w) for c, w, _ in data]
    order = sorted(allr, key=lambda x: -x[0])
    top = order[: max(1, len(order) // 10)]
    base_rate = sum(1 for _, y, _ in allr if y >= 1.1) / max(1, len(allr))
    return {"n": len(data), "n_gain": len(g), "lam": lam, "folds": folds,
            "gain_spearman": round(_spearman([p for p, _, _ in g], [y for _, y, _ in g]), 3),
            "formula_spearman": round(_spearman([c["score"] for _, _, c in g], [y for _, y, _ in g]), 3),
            "score_spearman_all": round(_spearman([p for p, _, _ in allr], [y for _, y, _ in allr]), 3),
            "formula_spearman_all": round(_spearman([c["score"] for _, _, c in allr], [y for _, y, _ in allr]), 3),
            "works_auc": round(_auc(works), 3),
            "top_decile_winners": round(sum(1 for _, y, _ in top if y >= 1.1) / len(top), 3),
            "formula_top_decile_winners": round(sum(1 for _, y, _ in sorted(allr, key=lambda x: -x[2]["score"])[: len(top)] if y >= 1.1) / len(top), 3),
            "winner_base_rate": round(base_rate, 3)}


def _auc(pairs) -> float:
    pos = [p for p, w in pairs if w]
    neg = [p for p, w in pairs if not w]
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def load(path: str | None = None) -> dict | None:
    path = path or MODEL_PATH
    return json.load(open(path)) if os.path.exists(path) else None


def weights(model: dict, prefix: tuple[str, ...] = ("unique:", "keystone:", "asc:", "util:", "token:")) -> list[dict]:
    """Learned link weights: the gain multiple each part or state adds on average."""
    out = [{"part": k, "gain": round(math.exp(g), 3), "works": round(w, 3)}
           for k, w, g in zip(model["vocab"], model["works"], model["gain"]) if k.startswith(prefix)]
    return sorted(out, key=lambda x: -x["gain"])


def main(argv=None) -> int:
    from . import chainresults, chainscore
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scores", required=True, help="chainscore run directory")
    ap.add_argument("--out", default=MODEL_PATH)
    ap.add_argument("--lam", type=float, default=None, help="ridge strength (default: best of 1, 3, 10, 30 by held-out rank correlation)")
    a = ap.parse_args(argv)
    chains = chainscore.pool_chains()
    res = chainresults.measured(a.scores)
    lams = [a.lam] if a.lam else [1.0, 3.0, 10.0, 30.0]
    evals = [evaluate(chains, res, l) for l in lams]
    best = max(evals, key=lambda e: e["score_spearman_all"])
    model = fit(chains, res, best["lam"])
    model["held_out"] = best
    with open(a.out, "w") as fh:
        json.dump(model, fh, separators=(",", ":"))
    print(json.dumps({"tried": evals, "chosen": best, "features": len(model["vocab"])}, indent=1))
    w = weights(model)
    print("strongest parts:", json.dumps(w[:12]))
    print("weakest parts:", json.dumps(w[-12:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
