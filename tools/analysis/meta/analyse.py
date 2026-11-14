"""PoE2 ladder corpus analysis (poe.ninja Forbidden Rites, PoB-PoE2 0.23.1).
Run: python3 -I analyse.py <data_dir> <out_dir>
Writes CSVs + results.json to out_dir."""
import csv, json, math, re, sys, collections, statistics as st

DATA, OUT = sys.argv[1], sys.argv[2]
rows = [json.loads(l) for l in open(f"{DATA}/evaluated.jsonl")]
ov = json.load(open(f"{DATA}/overview.json"))
TOTAL = ov["totalCharacters"]
R = {}  # results for the report


def q(xs, p):
    xs = sorted(x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x)))
    if not xs:
        return None
    k = (len(xs) - 1) * p
    f = math.floor(k); c = math.ceil(k)
    return xs[f] if f == c else xs[f] + (xs[c] - xs[f]) * (k - f)


def rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs); i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a, b):
    ra, rb = rank(a), rank(b)
    ma, mb = st.mean(ra), st.mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else float("nan")


def wcsv(name, header, data):
    with open(f"{OUT}/{name}", "w", newline="") as f:
        w = csv.writer(f); w.writerow(header); w.writerows(data)


ovdim = {k: dict((n, c) for n, c in v["counts"]) for k, v in ov["dimensions"].items()}
ELEM = ["Fire", "Cold", "Lightning", "Chaos"]
DTYPES = ["Physical", "Fire", "Cold", "Lightning", "Chaos"]

# ---------------------------------------------------------------- per-build table
B = []
for r in rows:
    t, s = r["tags"], r["pinnacle_locked"]["stats"]
    ms = r["pinnacle_locked"]["mainSkill"] or {}
    mins = r["pinnacle_locked"]["minionStats"]
    ks = {k["name"] for k in t["keystones"]}
    pdps = s.get("CombinedDPS") or 0
    mdps = (mins or {}).get("CombinedDPS") or 0
    minion = mins is not None
    dps = max(pdps, mdps)
    nonhit = (mins or s).get("NonHitDPS") or 0 if minion and mdps >= pdps else s.get("NonHitDPS") or 0
    mg = next((g for g in t["socketGroups"] if g.get("main")), None)
    mgem = None
    if mg:
        mgem = next((x for x in mg["gems"] if not x.get("support") and x.get("name") == ms.get("name")), None) \
            or next((x for x in mg["gems"] if not x.get("support") and x.get("damaging")), None)
    types = set(ms.get("types") or [])
    proxy = set((mgem or {}).get("proxyKind") or [])
    act = (mgem or {}).get("activation")
    if minion or "Minion" in types or "minion" in proxy:
        arch = "minion"
    elif "Totem" in types or "totem" in proxy:
        arch = "totem"
    elif dps > 0 and nonhit / dps > 0.5:
        arch = "DoT"
    elif act == "triggered":
        arch = "triggered " + ("spell" if "Spell" in types else "attack")
    elif "Attack" in types:
        arch = "attack"
    elif "Spell" in types:
        arch = "spell"
    else:
        arch = "other/non-damaging"
    if dps == 0 and arch not in ("minion",):
        arch = "other/non-damaging"
    life, es, mana = s.get("Life", 0) or 0, s.get("EnergyShield", 0) or 0, s.get("Mana", 0) or 0
    ci = "Chaos Inoculation" in ks
    mom = "Mind Over Matter" in ks
    pool = (life if life > 1 else 0) + es
    if ci or (es > 0 and life <= 1):
        pooltype = "CI / ES-only"
    elif es >= 0.3 * (life + es) and life >= 0.3 * (life + es):
        pooltype = "hybrid life+ES"
    elif es > life:
        pooltype = "ES-primary (life kept)"
    else:
        pooltype = "life"
    maxhits = {d: s.get(f"{d}MaximumHitTaken") for d in DTYPES}
    mh_vals = [v for d, v in maxhits.items() if v is not None and not (d == "Chaos" and ci)]
    minmh = min(mh_vals) if mh_vals else None
    layers = []
    if (s.get("PhysicalDamageReduction") or 0) >= 40: layers.append("armour40+")
    if (s.get("EvadeChance") or 0) >= 40: layers.append("evade40+")
    if (s.get("EffectiveBlockChance") or 0) >= 30: layers.append("block30+")
    if (s.get("DeflectChance") or 0) >= 20: layers.append("deflect20+")
    if mom: layers.append("MoM")
    if "Eldritch Battery" in ks: layers.append("EB")
    B.append(dict(
        idx=r["line"], asc=t["ascendancy"] or "None", cls=t["className"], level=t["level"],
        meta_skill=r["meta"]["main_skill"], pob_skill=ms.get("name") or "(none)",
        match=(ms.get("name") == r["meta"]["main_skill"]),
        dps_skill=ms.get("name") or "(none)", arch=arch, dps=dps, minion=minion,
        hit_dps=s.get("TotalDPS") or 0, nonhit=nonhit,
        ehp=s.get("TotalEHP") or 0, minmh=minmh, maxhits=maxhits, life=life, es=es, mana=mana,
        pool=pool, pooltype=pooltype, ci=ci, mom=mom, layers=layers,
        res={e: s.get(f"{e}Resist") for e in ELEM}, armour=s.get("Armour") or 0, evasion=s.get("Evasion") or 0,
        pdr=s.get("PhysicalDamageReduction") or 0, evade=s.get("EvadeChance") or 0,
        block=s.get("EffectiveBlockChance") or 0, deflect=s.get("DeflectChance") or 0,
        spirit=s.get("Spirit") or 0, crit=s.get("CritChance") or 0,
        ob=t["oneButton"], obi=t["oneButtonInclItemSkills"], nocast=t["noCast"],
        manual=t["manualDamagingSkillNames"], manualn=t["manualDamagingSkills"],
        itemman=t["itemGrantedManualDamagingSkills"], granted=t["grantedSkills"],
        uniques=t["uniques"], groups=t["socketGroups"], ks=sorted(ks),
        unsup=t["unsupportedModLines"], unsup_s=t["unsupportedModSample"],
    ))
N = len(B)
R["N"] = N
R["pob_meta_match"] = sum(b["match"] for b in B)
R["minion_dps_builds"] = sum(b["minion"] for b in B)
R["zero_dps"] = sum(b["dps"] == 0 for b in B)

wcsv("builds_flat.csv",
     ["line", "ascendancy", "class", "level", "meta_main_skill", "pob_main_skill", "archetype", "pinnacle_dps",
      "hit_dps", "nonhit_dps", "minion", "total_ehp", "min_max_hit", "life", "es", "mana", "pool_type",
      "fire_res", "cold_res", "light_res", "chaos_res", "armour", "evasion", "phys_dr", "evade", "block",
      "deflect", "one_button", "one_button_incl_item", "no_cast", "manual_damaging", "unsupported_lines"],
     [[b["idx"], b["asc"], b["cls"], b["level"], b["meta_skill"], b["pob_skill"], b["arch"], round(b["dps"]),
       round(b["hit_dps"]), round(b["nonhit"]), b["minion"], round(b["ehp"]), b["minmh"], b["life"], b["es"],
       b["mana"], b["pooltype"], b["res"]["Fire"], b["res"]["Cold"], b["res"]["Lightning"], b["res"]["Chaos"],
       b["armour"], b["evasion"], b["pdr"], b["evade"], b["block"], b["deflect"], b["ob"], b["obi"],
       b["nocast"], "|".join(b["manual"]), b["unsup"]] for b in B])

# ---------------------------------------------------------------- 1. meta map
cnt_asc = collections.Counter(b["asc"] for b in B)
ovasc = ovdim["class"]
meta_asc = []
for a, c in sorted(ovasc.items(), key=lambda x: -x[1]):
    meta_asc.append([a, c, round(100 * c / TOTAL, 2), cnt_asc.get(a, 0), round(100 * cnt_asc.get(a, 0) / N, 2)])
for a in cnt_asc:
    if a not in ovasc:
        meta_asc.append([a, 0, 0, cnt_asc[a], round(100 * cnt_asc[a] / N, 2)])
wcsv("meta_ascendancy.csv", ["ascendancy", "ninja_chars", "ninja_pct", "corpus_n", "corpus_pct"], meta_asc)
R["meta_asc"] = meta_asc

ovsk = ovdim["skills"]
cnt_ds = collections.Counter(b["dps_skill"] for b in B)
cnt_ms = collections.Counter(b["meta_skill"] for b in B)
meta_sk = []
for sname, c in sorted(ovsk.items(), key=lambda x: -x[1])[:60]:
    meta_sk.append([sname, c, round(100 * c / TOTAL, 2), cnt_ms.get(sname, 0), cnt_ds.get(sname, 0)])
wcsv("meta_skill.csv", ["skill", "ninja_chars", "ninja_pct_of_chars", "corpus_meta_main", "corpus_pob_main"], meta_sk)
R["meta_skill_top"] = meta_sk[:20]

R["arch"] = collections.Counter(b["arch"] for b in B).most_common()
R["pooltype"] = collections.Counter(b["pooltype"] for b in B).most_common()
R["layers"] = collections.Counter(l for b in B for l in b["layers"]).most_common()
R["nolayer"] = sum(not [l for l in b["layers"] if l not in ("MoM", "EB")] for b in B)
ovkp = ovdim["keypassives"]
R["ks_compare"] = [[k, round(100 * ovkp.get(k, 0) / TOTAL, 1), round(100 * sum(k in b["ks"] for b in B) / N, 1)]
                   for k in ["Chaos Inoculation", "Mind Over Matter", "Eldritch Battery", "Blackflame Covenant",
                             "Resolute Technique", "Iron Reflexes", "Ghost Dance", "Dance with Death",
                             "Vaal Pact", "Avatar of Fire", "Pain Attunement", "Giant's Blood",
                             "Acrobatics", "Unwavering Stance", "Conduit", "Bulwark"]]
R["traits"] = ov["dimensions"]["traits"]["counts"]
wcsv("meta_archetype_defence.csv", ["kind", "name", "n", "pct"],
     [["archetype", a, c, round(100 * c / N, 1)] for a, c in R["arch"]] +
     [["pool", a, c, round(100 * c / N, 1)] for a, c in R["pooltype"]] +
     [["layer", a, c, round(100 * c / N, 1)] for a, c in R["layers"]] +
     [["keystone(ninja% vs corpus%)", k, a, b] for k, a, b in R["ks_compare"]])

# ---------------------------------------------------------------- 2. power vs popularity
dps_all = [b["dps"] for b in B if b["dps"] > 0]
ehp_all = [b["ehp"] for b in B]
mh_all = [b["minmh"] for b in B if b["minmh"]]
R["corpus_dps"] = [q(dps_all, p) for p in (.1, .25, .5, .75, .9)]
R["corpus_ehp"] = [q(ehp_all, p) for p in (.1, .25, .5, .75, .9)]
R["corpus_mh"] = [q(mh_all, p) for p in (.1, .25, .5, .75, .9)]


def grp(key, popmap, label):
    g = collections.defaultdict(list)
    for b in B:
        if b["dps"] > 0:
            g[b[key]].append(b)
    out = []
    for k, bs in g.items():
        d = [b["dps"] for b in bs]; e = [b["ehp"] for b in bs]; m = [b["minmh"] for b in bs if b["minmh"]]
        pop = popmap.get(k, 0)
        out.append(dict(name=k, n=len(bs), small=len(bs) < 5, med_dps=q(d, .5), p90_dps=q(d, .9),
                        med_ehp=q(e, .5), p90_ehp=q(e, .9), med_mh=q(m, .5), p10_mh=q(m, .1),
                        pop=pop, pop_pct=100 * pop / TOTAL,
                        arch=collections.Counter(b["arch"] for b in bs).most_common(1)[0][0]))
    big = [x for x in out if not x["small"]]
    # percentile ranks among n>=5 groups
    for fld in ("p90_dps", "med_dps", "med_ehp", "med_mh", "pop"):
        vals = sorted(x[fld] or 0 for x in big)
        for x in out:
            v = x[fld] or 0
            x[fld + "_pr"] = sum(1 for y in vals if y <= v) / len(vals)
    for x in out:
        flags = []
        if not x["small"] and popmap:
            if x["p90_dps_pr"] >= .6 and x["pop_pr"] <= .5:
                flags.append("STRONG-RARE" if (x["med_ehp_pr"] >= .4 or x["med_mh_pr"] >= .5) else "HIGH-DPS-RARE(glass)")
            if x["pop_pr"] >= .6 and x["med_dps_pr"] <= .35:
                flags.append("POPULAR-LOW-DPS")
            if x["pop_pr"] >= .6 and x["med_ehp_pr"] <= .25:
                flags.append("POPULAR-FRAGILE")
        if (x["med_dps"] or 0) < 10000:
            flags.append("DPS-SUSPECT(main is setup/support?)")
        x["flag"] = ";".join(flags)
    out.sort(key=lambda x: (x["small"], -(x["p90_dps"] or 0)))
    wcsv(f"power_by_{label}.csv",
         ["name", "n", "small_n", "archetype", "median_dps", "p90_dps", "median_ehp", "p90_ehp",
          "median_min_maxhit", "p10_min_maxhit", "ninja_chars", "ninja_pct", "flag"],
         [[x["name"], x["n"], x["small"], x["arch"], round(x["med_dps"]), round(x["p90_dps"]), round(x["med_ehp"]),
           round(x["p90_ehp"]), round(x["med_mh"] or 0), round(x["p10_mh"] or 0), x["pop"],
           round(x["pop_pct"], 2), x["flag"]] for x in out])
    return out


R["pow_skill"] = grp("dps_skill", ovsk, "skill")
R["pow_asc"] = grp("asc", ovasc, "ascendancy")
R["pow_arch"] = grp("arch", {}, "archetype")
# matched subset sanity: does PoB main == ninja main change medians?
R["dps_matched_vs_not"] = [q([b["dps"] for b in B if b["match"] and b["dps"] > 0], .5),
                           q([b["dps"] for b in B if not b["match"] and b["dps"] > 0], .5)]

# ---------------------------------------------------------------- 3. constraint builds
def gems_by(b, pred):
    out = []
    for g in b["groups"]:
        if not g.get("enabled", True):
            continue
        for x in g["gems"]:
            if not x.get("support") and x.get("enabled", True) and pred(g, x):
                out.append(x["name"])
    return out


def trig(b): return gems_by(b, lambda g, x: x.get("activation") == "triggered" and x.get("damaging"))
def metas(b): return sorted({g["active"] for g in b["groups"] if g.get("metaGroup") and g.get("enabled", True)})
def persist_dmg(b): return gems_by(b, lambda g, x: x.get("activation") == "persistent" and x.get("damaging"))


seg = {"oneButton": [b for b in B if b["ob"]], "noCast": [b for b in B if b["nocast"]],
       "both": [b for b in B if b["ob"] and b["nocast"]], "rest(neither)": [b for b in B if not b["ob"] and not b["nocast"]],
       "oneButtonInclItem": [b for b in B if b["obi"]], "all": B}
R["seg"] = []
for k, bs in seg.items():
    d = [b["dps"] for b in bs if b["dps"] > 0]
    R["seg"].append([k, len(bs), q(d, .5), q(d, .9), q([b["ehp"] for b in bs], .5),
                     q([b["minmh"] for b in bs if b["minmh"]], .5), q([b["unsup"] for b in bs], .5)])
wcsv("constraint_segments.csv", ["segment", "n", "median_dps", "p90_dps", "median_ehp", "median_min_maxhit",
                                 "median_unsupported"], R["seg"])
ob = seg["oneButton"]; rest = [b for b in B if not b["ob"]]


def lift(field_fn, A, Bset, minA=5):
    ca = collections.Counter(x for b in A for x in set(field_fn(b)))
    cb = collections.Counter(x for b in Bset for x in set(field_fn(b)))
    out = []
    for x, c in ca.items():
        if c >= minA:
            pa, pb = c / len(A), cb.get(x, 0) / len(Bset)
            out.append([x, c, round(100 * pa, 1), round(100 * pb, 1), round(pa / pb, 1) if pb else float("inf")])
    out.sort(key=lambda r: (-r[1]))
    return out


R["ob_manual"] = collections.Counter(b["manual"][0] if b["manual"] else "(none - fully automated)" for b in ob).most_common(15)
R["ob_trig"] = lift(trig, ob, rest)[:15]
R["ob_meta"] = lift(metas, ob, rest)[:15]
R["ob_uniq"] = sorted(lift(lambda b: b["uniques"], ob, rest, 4), key=lambda r: -r[4] if r[4] != float("inf") else -99)[:15]
R["ob_granted"] = lift(lambda b: [g["name"] for g in b["granted"] if g["name"]], ob, rest, 3)[:12]
R["ob_persist"] = lift(persist_dmg, ob, rest)[:10]
R["ob_arch"] = collections.Counter(b["arch"] for b in ob).most_common()
R["nc_manual"] = collections.Counter(m for b in seg["noCast"] for m in b["manual"]).most_common(12)
R["item_breaks_ob"] = [(b["asc"], b["itemman"]) for b in B if b["ob"] and not b["obi"]]
ehp25, mh25 = q(ehp_all, .25), q(mh_all, .25)
sane = [b for b in ob if b["ehp"] >= ehp25 and (b["minmh"] or 0) >= mh25 and b["dps"] > 0]
sane.sort(key=lambda b: -b["dps"])
top = []
for b in sane[:10]:
    top.append([b["idx"], b["asc"], b["pob_skill"], b["arch"], round(b["dps"]), round(b["ehp"]), round(b["minmh"]),
                "|".join(b["manual"]) or "-", "|".join(dict.fromkeys(trig(b))) or "-", "|".join(metas(b)) or "-",
                "|".join(b["uniques"][:5]), b["nocast"], b["unsup"]])
wcsv("constraint_top10.csv", ["line", "ascendancy", "pob_main", "archetype", "pinnacle_dps", "ehp", "min_max_hit",
                              "manual_skill", "triggered_damaging", "meta_gems", "uniques", "noCast", "unsupported"], top)
R["ob_top"] = top
wcsv("constraint_enablers.csv", ["kind", "name", "n_onebutton", "pct_onebutton", "pct_rest", "lift"],
     [["triggered"] + r for r in R["ob_trig"]] + [["meta"] + r for r in R["ob_meta"]] +
     [["unique"] + r for r in R["ob_uniq"]] + [["granted"] + r for r in R["ob_granted"]] +
     [["persistent_dmg"] + r for r in R["ob_persist"]])

# ---------------------------------------------------------------- 4. defensive floors
P = (.05, .1, .25, .5, .75)
fl = []
for e in ELEM:
    v = [b["res"][e] for b in B if b["res"][e] is not None and not (e == "Chaos" and b["ci"])]
    fl.append([f"{e}Resist", len(v)] + [q(v, p) for p in P])
fl.append(["share ele res all >=75", N, round(100 * sum(all((b["res"][e] or 0) >= 75 for e in ELEM[:3]) for b in B) / N, 1)])
fl.append(["share chaos >=0 (non-CI)", N, round(100 * sum((b["res"]["Chaos"] or 0) >= 0 for b in B if not b["ci"]) / sum(not b["ci"] for b in B), 1)])
for name, sel in (("Life+ES (all)", B), ("Life (life-based)", [b for b in B if b["pooltype"] == "life"]),
                  ("ES (CI/ES-only)", [b for b in B if b["pooltype"] == "CI / ES-only"]),
                  ("Life+ES (hybrid)", [b for b in B if b["pooltype"] == "hybrid life+ES"])):
    fl.append([name, len(sel)] + [q([b["pool"] for b in sel], p) for p in P])
fl.append(["TotalEHP", N] + [q(ehp_all, p) for p in P])
fl.append(["min max-hit (excl chaos for CI)", len(mh_all)] + [q(mh_all, p) for p in P])
for d in DTYPES:
    v = [b["maxhits"][d] for b in B if b["maxhits"][d] is not None]
    fl.append([f"{d} max hit", len(v)] + [q(v, p) for p in P])
fl.append(["Spirit", N] + [q([b["spirit"] for b in B], p) for p in P])
fl.append(["Armour", N] + [q([b["armour"] for b in B], p) for p in P])
fl.append(["Evasion", N] + [q([b["evasion"] for b in B], p) for p in P])
wcsv("defence_floors.csv", ["metric", "n", "p05", "p10", "p25", "p50", "p75"],
     [[r[0], r[1]] + [round(x) if isinstance(x, float) and x > 10 else x for x in r[2:]] for r in fl])
R["floors"] = fl

# ---------------------------------------------------------------- 5. reliability
pos = [b for b in B if b["dps"] > 0]
R["rho_unsup_dps"] = spearman([b["unsup"] for b in pos], [b["dps"] for b in pos])
R["rho_unsup_ehp"] = spearman([b["unsup"] for b in B], [b["ehp"] for b in B])
# within-skill: rank dps within dps_skill group (n>=5), correlate with within-group unsup rank
wa, wb = [], []
g = collections.defaultdict(list)
for b in pos: g[b["dps_skill"]].append(b)
for k, bs in g.items():
    if len(bs) >= 5:
        rd, ru = rank([b["dps"] for b in bs]), rank([b["unsup"] for b in bs])
        n = len(bs) - 1
        wa += [x / n for x in ru]; wb += [x / n for x in rd]
R["rho_within"] = spearman(wa, wb)
R["unsup_q"] = [q([b["unsup"] for b in B], p) for p in (.1, .25, .5, .75, .9)]
tert = sorted(pos, key=lambda b: b["unsup"])
k3 = len(tert) // 3
R["unsup_terciles"] = [(min(b["unsup"] for b in t_), max(b["unsup"] for b in t_), q([b["dps"] for b in t_], .5))
                       for t_ in (tert[:k3], tert[k3:2 * k3], tert[2 * k3:])]


def norm(line):
    l = line.strip()
    l = re.sub(r"\([\d.\-–]+\)", "#", l)
    l = re.sub(r"[+-]?\d+(\.\d+)?", "#", l)
    return l


pat = collections.Counter(); ex = {}
for b in B:
    for l in set(norm(x) for x in b["unsup_s"]):
        pat[l] += 1
# coarse mechanic buckets
BUCKETS = [
    ("Item meta (sockets / can-roll / augment)", r"Socket|Can roll|Augment|Corrupt|Sanctif|Fractur|Mirror"),
    ("Bonded: (rune/idol bonded mods)", r"^Bonded:"),
    ("Remnants / Infusion", r"Remnant|Infusion"),
    ("Shrine effects", r"Shrine"),
    ("Archon / Invocation / Energy", r"Archon|Invocat|Energy\b|gain .*Energy"),
    ("Recoup / recovery redirects", r"Recoup|Recover|Leech|regenerat"),
    ("Enemy Power / Rare-kill (Headhunter)", r"Power\b|Rare monster|Modifiers for"),
    ("Hinder / Maim / Blind / Daze", r"Hinder|Maim|Blind|Daze|Intimidat"),
    ("Unlucky / Lucky", r"Unlucky|Lucky"),
    ("Charges / Charms / Flasks", r"Charge|Charm|Flask"),
    ("Companion / Minion", r"Companion|Minion|Persistent Minion"),
    ("Exposure / Curses / Marks", r"Exposure|Curse|Mark\b"),
    ("Ailment interactions (Freeze/Shock/Ignite/Poison/Bleed)", r"Freez|Shock|Ignit|Poison|Bleed|Electrocut|Chill"),
    ("Grenade / Totem / Projectile", r"Grenade|Totem|Projectile"),
    ("Defences (block/deflect/evasion/armour/ES)", r"Block|Deflect|Evasion|Armour|Energy Shield"),
    ("Skill cost / reservation / spirit", r"Cost|Reserv|Spirit"),
]
bk = collections.Counter()
for b in B:
    hit = set()
    for l in b["unsup_s"]:
        for name, rx in BUCKETS:
            if re.search(rx, l, re.I):
                hit.add(name); break
        else:
            hit.add("(other)")
    bk.update(hit)
R["unsup_top"] = pat.most_common(25)
R["unsup_buckets"] = bk.most_common()
wcsv("unsupported_lines_top.csv", ["normalised_line", "n_builds", "pct_builds"],
     [[l, c, round(100 * c / N, 1)] for l, c in pat.most_common(100)])
wcsv("unsupported_buckets.csv", ["bucket", "n_builds", "pct_builds"],
     [[l, c, round(100 * c / N, 1)] for l, c in bk.most_common()])
R["unsup_trunc"] = sum(len(b["unsup_s"]) < b["unsup"] for b in B)

json.dump(R, open(f"{OUT}/results.json", "w"), indent=1, default=str)
print("ok", N)
