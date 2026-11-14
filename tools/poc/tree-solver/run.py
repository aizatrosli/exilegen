"""Run the tree-solver POC over selected corpus builds. One PoB engine at a time.
usage: python3 run.py OUTDIR [hash ...]"""
import csv, hashlib, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pob import Pob
from solver import Solver, Objective, LAMBDA

CORPUS = os.environ.get("EXILEGEN_CORPUS", "data/corpus/poe2/")
DEFAULT = ["c79c6d56729fadd0e819ca70d91c3122", "b0d3f0cb3a6c9f088a42881acd6b97ec", "465abc3a9f95568af11a24c0c49a276a",
           "8f42bdea6d49182e694ba60c4d5352bd", "d8657a846a793e8dd00a5f6c103a12e7", "496c971d80847bc787d78aff7c6b3350",
           "bd483adf4d3ae614052b7cdd69c94681", "00e2984317c34fe0d4d4fad59416f64f", "64c096f22b96d510b91e05d296f0d648",
           "48113d658d496452072014953a5367ea"]
GROW_STEP = int(os.environ.get("TS_STEP", 12))
LS_EVALS = int(os.environ.get("TS_LS_EVALS", 1200))


def lua_list(ids):
    return "{" + ",".join(str(i) for i in ids) + "}"


def set_tree(p, ids, attrs):
    return p.lua("local d=require('dkjson'); return TS.enc(TS.setTree(d.decode(%s), d.decode(%s)))"
                 % (json.dumps(json.dumps(sorted(ids))), json.dumps(json.dumps(attrs or {"0": 0}))))


def main():
    out = sys.argv[1]; hashes = sys.argv[2:] or DEFAULT
    os.makedirs(out, exist_ok=True)
    ms = {x["hash"]: x for x in json.load(open(CORPUS + "analysis-synergy/builds_main_skill_dps.json"))}
    codes = {}
    for l in open(CORPUS + "builds.jsonl"):
        b = json.loads(l); h = hashlib.sha256(b["code"].encode()).hexdigest()[:32]
        if h in hashes:
            codes[h] = b
    rows_f = open(os.path.join(out, "results.csv"), "a", newline="")
    w = csv.writer(rows_f)
    if rows_f.tell() == 0:
        w.writerow(["hash", "class", "ascendancy", "skill", "variant", "N", "points", "dps", "ehp", "fire", "cold", "light", "chaos",
                    "deficit", "dps_ratio", "ehp_ratio", "jaccard", "feasible", "f", "f_player", "evals", "wall_s",
                    "player_keystones", "solver_keystones", "weapon_set_nodes"])
    bj = open(os.path.join(out, "builds.jsonl"), "a")
    logf = open(os.path.join(out, "log.txt"), "a")

    def log(s):
        print(s, flush=True); logf.write(s + "\n"); logf.flush()

    for h in hashes:
        b = codes[h]; m = ms[h]
        log(f"=== {h} {b['class']} / {m['skill']}")
        p = Pob()
        try:
            p.load(b["code"], m["g"])
            base = p.lua("return TS.enc(TS.current())")
            g = p.ts("TS.graph"); tr = p.ts("TS.tree")
            info = {x["id"]: x for x in g["nodes"]}
            granted = [i for i in tr["ids"] if info.get(i, {}).get("granted")]
            P = set(i for i in tr["ids"] if i not in granted)
            N = len(P)
            jewels = [i for i in P if info[i]["jewel"] and info[i]["jewelItem"]]
            keystones = [i for i in P if info[i]["t"] == "Keystone"]
            log(f"baseline dps={base['dps']:.0f} ehp={base['ehp']:.0f} res={base['fr']},{base['cr']},{base['lr']},{base['chr']} "
                f"deficit={base['deficit']} N={N} jewels={len(jewels)} keystones={[info[k]['name'] for k in keystones]} wsets={len(tr['modes'])}")
            stripped = set_tree(p, granted, {})
            log(f"stripped: {stripped}")
            obj = Objective(base)
            variants = [("scratch", jewels, None), ("scratch_pinKS", jewels + keystones, None), ("warm", jewels, (P, tr["attrs"]))]
            # player tree evaluated in the same frame (all nodes main-tree)
            s0 = Solver(p, g, obj, jewels, log)
            mp = s0.evaluate([(P | {s0.start}, tr["attrs"])])[0]
            fp = obj.f(mp)
            log(f"player via addNodes: dps={mp['dps']:.0f} ehp={mp['ehp']:.0f} f={fp:.4f} feasible={obj.feasible(mp)}")
            only = os.environ.get("TS_VARIANTS")
            for name, pins, warm in variants:
                if only and name not in only.split(","):
                    continue
                t0 = time.time()
                s = Solver(p, g, obj, pins, log)
                s.cache = s0.cache
                ev0 = len(s0.cache)
                s.evals = 0
                if warm:
                    T, attrs = set(warm[0]) | {s.start}, dict(warm[1])
                    seen = {s.start}; st = [s.start]
                    while st:
                        u = st.pop()
                        for x in s.adj[u] & T:
                            if x not in seen: seen.add(x); st.append(x)
                    s.free = T - seen
                    if s.free: log(f"  warm: {len(s.free)} player nodes not path-connected (jewel/leap): {[info[i]['name'] for i in s.free]}")
                else:
                    T, attrs = s.grow(N, step=GROW_STEP)
                    log(f"  grown f={s.fval(T, attrs):.4f}")
                T, attrs, fT = s.local_search(T, attrs, max_evals=LS_EVALS)
                wall = time.time() - t0
                ids = T - {s.start}
                assert len(ids) == N, (len(ids), N)
                r = set_tree(p, list(ids) + granted + [s.start], attrs)
                mt = r["m"]
                code = p.call("save_build_code")["code"]
                set_tree(p, granted, {})
                jac = len(ids & P) / len(ids | P)
                sk = [info[k]["name"] for k in ids if info[k]["t"] == "Keystone"]
                feas = obj.feasible(mt)
                log(f"  {name}: dps={mt['dps']:.0f} ({mt['dps']/base['dps']:.3f}x) ehp={mt['ehp']:.0f} ({mt['ehp']/base['ehp']:.3f}x) "
                    f"res={mt['fr']},{mt['cr']},{mt['lr']} def={mt['deficit']} jac={jac:.2f} feas={feas} f={obj.f(mt):.4f} "
                    f"(calc f={fT:.4f}) evals={s.evals} wall={wall:.0f}s ks={sk} count={r['count']}")
                w.writerow([h, b["class"], m["asc"], m["skill"], name, N, r["count"] - len(granted), round(mt["dps"]), round(mt["ehp"]),
                            mt["fr"], mt["cr"], mt["lr"], mt["chr"], mt["deficit"], round(mt["dps"] / base["dps"], 4),
                            round(mt["ehp"] / base["ehp"], 4), round(jac, 3), feas, round(obj.f(mt), 4), round(fp, 4), s.evals,
                            round(wall, 1), "|".join(info[k]["name"] for k in keystones), "|".join(sk), len(tr["modes"])])
                rows_f.flush()
                bj.write(json.dumps({"hash": h, "class": b["class"], "skill": m["skill"], "variant": name, "N": N,
                                     "dps": mt["dps"], "ehp": mt["ehp"], "baseline": base, "metrics": mt, "nodes": sorted(ids),
                                     "attrs": attrs, "code": code}) + "\n"); bj.flush()
            bj.write(json.dumps({"hash": h, "class": b["class"], "skill": m["skill"], "variant": "player", "N": N,
                                 "dps": base["dps"], "ehp": base["ehp"], "baseline": base, "metrics": base,
                                 "nodes": sorted(P), "attrs": tr["attrs"], "code": b["code"].strip()}) + "\n"); bj.flush()
        except Exception as e:
            import traceback; log("FAILED " + traceback.format_exc())
        finally:
            p.close()


if __name__ == "__main__":
    main()
