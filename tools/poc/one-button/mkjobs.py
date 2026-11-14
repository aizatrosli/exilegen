#!/usr/bin/env python3
"""Build job JSONL for the onebutton driver. usage: mkjobs.py out.jsonl mode line[,line..] [json-params]"""
import json, sys
out, mode, lines = sys.argv[1], sys.argv[2], [int(x) for x in sys.argv[3].split(',')]
extra = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
B = open(__import__('os').environ.get('EXILEGEN_CORPUS','data/corpus/poe2/')+'builds.jsonl').read().splitlines()
with open(out, 'w') as f:
    for ln in lines:
        b = json.loads(B[ln - 1])
        f.write(json.dumps({"id": f"{mode}:{ln}", "code": b['code'], "params": {"mode": mode, **extra}, "lua": "return OB_main()"}) + "\n")
