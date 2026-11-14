import json,hashlib,sys
codes={}
for l in open('data/builds.jsonl'):
    b=json.loads(l); c=b.get('code') or ''
    if c.strip(): codes[hashlib.sha256(c.strip().encode()).hexdigest()[:32]]=c
ev=[json.loads(l) for l in open('data/evaluated.jsonl')]
n=int(sys.argv[1]) if len(sys.argv)>1 else 10**9
miss=0
with open('jobs_scan.jsonl','w') as f:
    for r in ev[:n]:
        c=codes.get(r['hash'])
        if not c: miss+=1; continue
        f.write(json.dumps({'id':r['hash'],'code':c,'ops':[{'op':'stats','label':'base'},{'op':'scan_groups','label':'scan'}]})+'\n')
print('missing',miss)
