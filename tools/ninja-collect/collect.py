import sys, os, json, time, hashlib, base64, zlib, re, urllib.parse as up, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ninja
SCR = os.environ.get("EXILEGEN_CACHE", ".cache/ninja/")  # raw responses, plan (not committed)
DICTS = SCR + "dl/dicts"; os.makedirs(DICTS, exist_ok=True)
OUT = os.environ.get("EXILEGEN_CORPUS", "data/corpus/poe2/")
VERSION = "1430-20261113-58818"; OVERVIEW = "forbidden-rites"; LEAGUE = "Forbidden Rites"
SEARCH = f"/poe2/api/builds/{VERSION}/search?overview={OVERVIEW}&type=exp"
N_SKILLS, ASC_PER_SKILL, MIN_COMBO, PER_COMBO = 40, 6, 10, 6
TOP_OVERALL, PER_CLASS_TOP, TARGET = 100, 5, 1500
PLAN = SCR + "plan.json"
def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)
def sha(s): return hashlib.sha256(s.encode()).hexdigest()
def search(**f):
    q = SEARCH + "".join(f"&{k}={up.quote(v)}" for k, v in f.items())
    cp = SCR + "dl/cache/" + hashlib.sha1(q.encode()).hexdigest() + ".bin"
    os.makedirs(SCR + "dl/cache", exist_ok=True)
    if os.path.exists(cp): b = open(cp, "rb").read()
    else:
        st, b = ninja.get(q)
        if st != 200: raise RuntimeError(f"search {f} -> {st}")
        open(cp, "wb").write(b)
    return ninja.decode_search(b, DICTS)
def rows(r, filt_skill=None):
    c = r["columns"]; n = len(c.get("name", []))
    dps = c.get(f"dps-{filt_skill}.skill") if filt_skill else c.get("dps.skill")
    out = []
    for i in range(n):
        out.append({"account": c["account"][i], "name": c["name"][i], "class": c["class"][i], "level": c["level"][i],
                    "main_skills": (c.get("skills") or [])[i] if i < len(c.get("skills") or []) else [], "keystones": (c.get("keypassives") or [])[i] if i < len(c.get("keypassives") or []) else [],
                    "dps_skill": (dps[i] if dps and i < len(dps) and isinstance(dps[i], str) else None) or None, "rank": i})
    return out
def spread(lst, k):
    if len(lst) <= k: return lst
    step = len(lst) / k
    return [lst[int(j * step)] for j in range(k)]
def write_json(path, obj):
    with open(path, "w") as f: json.dump(obj, f, indent=1, ensure_ascii=False); f.flush(); os.fsync(f.fileno())
def plan():
    log("overview search")
    top = search()
    ov = {"league": LEAGUE, "version": VERSION, "overview": OVERVIEW, "type": "exp", "fetchedUtc": datetime.datetime.utcnow().isoformat() + "Z",
          "totalCharacters": top["total"], "dictionaries": top["dicts"],
          "dimensions": {k: {"kind": v["kind"], "counts": sorted([[n, c] for n, c in v["counts"] if c], key=lambda x: -x[1])} for k, v in top["dims"].items()},
          "integerRanges": top["ints"]}
    skills = [n for n, c in ov["dimensions"]["skills"]["counts"]][:N_SKILLS]
    classes = [n for n, c in ov["dimensions"]["class"]["counts"]]
    picks = []  # (stratum, row)
    for r in spread(rows(top), TOP_OVERALL): picks.append(("top-overall", None, r))
    matrix = {}
    for s in skills:
        log("skill", s)
        rs = search(skills=s)
        cc = sorted([[n, c] for n, c in rs["dims"]["class"]["counts"] if c], key=lambda x: -x[1])
        matrix[s] = {"total": rs["total"], "byAscendancy": cc}
        chosen = [n for n, c in cc if c >= MIN_COMBO][:ASC_PER_SKILL]
        for i, a in enumerate(chosen):
            if i == 0:
                rr = [x for x in rows(rs, s) if x["class"] == a]
            else:
                log("  combo", s, a); rr = rows(search(skills=s, **{"class": a}), s)
            for r in spread(rr, PER_COMBO):
                r["dps_skill"] = r["dps_skill"] or s; picks.append((f"skill:{s}|asc:{a}", s, r))
    ov["skillByAscendancy"] = matrix
    for a in classes:
        log("class top", a)
        rc = search(**{"class": a})
        for r in rows(rc)[:PER_CLASS_TOP]: picks.append((f"top-asc:{a}", None, r))
    seen = set(); plan = []
    for st, s, r in picks:
        k = (r["account"], r["name"])
        if k in seen: continue
        seen.add(k); r["stratum"] = st; r["filter_skill"] = s; plan.append(r)
    write_json(OUT + "overview.json", ov)
    json.dump(plan, open(PLAN, "w"))
    log("plan size", len(plan))
    return plan
def pob_meta(code):
    try:
        x = zlib.decompress(base64.urlsafe_b64decode(code + "=" * (-len(code) % 4)))[:4000].decode("utf-8", "replace")
        m = re.search(r"<Build [^>]*", x); t = m.group(0) if m else ""
        g = lambda k: (re.search(k + r'="([^"]*)"', t) or [None, None])[1]
        return {"className": g("className"), "ascendClassName": g("ascendClassName"), "pobLevel": g("level"), "mainSocketGroup": g("mainSocketGroup")}
    except Exception as e:
        return {"decodeError": str(e)[:80]}
def fetch(plan):
    target = len(plan)
    done = set()
    bj = OUT + "builds.jsonl"
    if os.path.exists(bj):
        for line in open(bj):
            try: d = json.loads(line); done.add((d["account_sha256"], d["character_sha256"]))
            except Exception: pass
    fetched = len(done); failed = 0; consecutive_fail = 0
    f = open(bj, "a"); pending = 0
    def progress(state="running"):
        write_json(OUT + "progress.json", {"fetched": fetched, "failed": failed, "target": target, "state": state, "updatedUtc": datetime.datetime.utcnow().isoformat() + "Z"})
    progress()
    try:
        for r in plan:
            ah, ch = sha(r["account"]), sha(r["name"])
            if (ah, ch) in done: continue
            path = f"/poe2/api/builds/{VERSION}/character?account={up.quote(r['account'])}&name={up.quote(r['name'])}&overview={OVERVIEW}&timeMachine="
            try:
                st, b = ninja.get(path)
                j = json.loads(b) if st == 200 else {}
                code = (j.get("pathOfBuildingExport") or "").strip()
            except ninja.Blocked:
                raise
            except Exception as e:
                st, code, j = str(e)[:60], "", {}
            if not code:
                failed += 1; consecutive_fail += 1
                log("fail", st, r["stratum"])
                if consecutive_fail >= 25: log("too many consecutive failures; stopping"); progress("stopped-failures"); return
                continue
            consecutive_fail = 0
            rec = {"account_sha256": ah, "character_sha256": ch, "league": LEAGUE, "snapshot": VERSION,
                   "class": j.get("class") or r["class"], "baseClass": j.get("baseClass"), "level": j.get("level") or r["level"],
                   "main_skill": r["filter_skill"] or r["dps_skill"] or (r["main_skills"][0] if r["main_skills"] else None),
                   "main_skills": r["main_skills"], "keystones": r["keystones"], "stratum": r["stratum"], "rank_in_query": r["rank"],
                   "updatedUtc": j.get("updatedUtc"), "pob": pob_meta(code), "code": code}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fetched += 1; pending += 1; done.add((ah, ch))
            if pending >= 50:
                f.flush(); os.fsync(f.fileno()); pending = 0; progress(); log("progress", fetched, failed, target)
        f.flush(); os.fsync(f.fileno()); progress("done"); log("DONE", fetched, failed)
    except ninja.Blocked as e:
        f.flush(); progress("stopped-blocked"); log("BLOCKED, stopping", e)
    finally:
        f.close()
if __name__ == "__main__":
    p = json.load(open(PLAN)) if os.path.exists(PLAN) else plan()
    fetch(p)
