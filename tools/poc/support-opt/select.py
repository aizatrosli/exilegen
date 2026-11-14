# Pick 20 POC builds across skills (median-DPS build per skill, >=3 supports) -> jobs.jsonl
import json, sys, statistics
C = __import__('os').environ.get('EXILEGEN_CORPUS', 'data/corpus/poe2/')
SKILLS = ["Comet", "Ice Shot", "Twister", "Flameblast", "Frostbolt", "Grim Pillars", "Spark", "Dark Consequences",
          "Companion: Zekoa, the Headcrusher", "Falling Thunder", "Flicker Strike", "Oil Barrage", "Shattering Palm",
          "Rend", "Essence Drain", "Skeletal Storm Mage Minion", "Poisonburst Arrow", "Wolf Pack", "Ember Fusillade",
          "Detonate Dead"]
m = json.load(open(C + 'analysis-synergy/builds_main_skill_dps.json'))
line = {}
for l in open(C + 'evaluated.jsonl'):
    d = json.loads(l); line[d['hash']] = d['line']
codes = open(C + 'builds.jsonl').readlines()
out = open(sys.argv[1], 'w')
for i, sk in enumerate(SKILLS):
    xs = [x for x in m if x['skill'] == sk and len(x.get('sup') or []) >= 3 and x.get('dps') and x['hash'] in line]
    if not xs: print('none', sk); continue
    med = statistics.median(x['dps'] for x in xs)
    x = min(xs, key=lambda x: abs(x['dps'] - med))
    code = json.loads(codes[line[x['hash']] - 1])['code']
    out.write(json.dumps({"id": i, "hash": x['hash'], "code": code, "g": x['g'], "skill": sk, "n_skill_builds": len(xs),
                          "corpus_dps": x['dps'], "asc": x['asc']}) + "\n")
    print(i, sk, len(xs), x['g'], round(x['dps']), x['sup'])
