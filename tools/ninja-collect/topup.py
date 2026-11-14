import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect as C
ov = json.load(open(C.OUT + "overview.json"))
plan = json.load(open(C.PLAN))
seen = {(r["account"], r["name"]) for r in plan}
skills = [n for n, c in ov["dimensions"]["skills"]["counts"]][C.N_SKILLS:C.N_SKILLS + 35]
extra = []
for s in skills:
    C.log("topup skill", s)
    rs = C.search(skills=s)
    cc = sorted([[n, c] for n, c in rs["dims"]["class"]["counts"] if c], key=lambda x: -x[1])
    ov["skillByAscendancy"][s] = {"total": rs["total"], "byAscendancy": cc}
    for i, (a, c) in enumerate([x for x in cc if x[1] >= C.MIN_COMBO][:2]):
        rr = [x for x in C.rows(rs, s) if x["class"] == a] if i == 0 else C.rows(C.search(skills=s, **{"class": a}), s)
        for r in C.spread(rr, 4):
            if (r["account"], r["name"]) in seen: continue
            seen.add((r["account"], r["name"])); r["stratum"] = f"skill-tail:{s}|asc:{a}"; r["filter_skill"] = s; extra.append(r)
C.write_json(C.OUT + "overview.json", ov)
plan += extra
json.dump(plan, open(C.PLAN, "w"))
C.log("plan size", len(plan))
C.fetch(plan)
