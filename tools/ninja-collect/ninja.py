import sys, time, struct, json, urllib.request, urllib.error, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pb import parse, varint
UA = "PoB Redux research client"
HOST = "https://poe.ninja"
_last = [0.0]
class Blocked(Exception): pass
def get(path, tries=4):
    for a in range(tries):
        wait = 1.1 - (time.time() - _last[0])
        if wait > 0: time.sleep(wait)
        _last[0] = time.time()
        req = urllib.request.Request(HOST + path, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500 or e.code == 403:
                ra = e.headers.get("Retry-After")
                delay = int(ra) if ra and ra.isdigit() else 10 * (2 ** a)
                print(f"HTTP {e.code} on {path[:90]}; sleeping {delay}s", flush=True)
                time.sleep(min(delay, 300))
                continue
            return e.code, b""
        except Exception as e:
            print(f"ERR {e} on {path[:90]}", flush=True); time.sleep(5 * (a + 1))
    raise Blocked(path)
def read_dict(d):
    assert d[:4] == b"NDIC", d[:8]
    n = struct.unpack("<I", d[12:16])[0]
    nb = struct.unpack("<I", d[28:32])[0]
    i = 36 + 8 * nb; lens = []
    for _ in range(n):
        l, i = varint(d, i); lens.append(l)
    out = []
    for l in lens:
        out.append(d[i:i+l].decode("utf-8")); i += l
    return out
_dcache = {}
def dictionary(h, cache_dir):
    if h in _dcache: return _dcache[h]
    p = os.path.join(cache_dir, h + ".bin")
    if os.path.exists(p): d = open(p, "rb").read()
    else:
        st, d = get(f"/poe2/api/builds/dictionary/{h}")
        if st != 200: raise RuntimeError(f"dict {h} {st}")
        open(p, "wb").write(d)
    _dcache[h] = read_dict(d); return _dcache[h]
def packed(v):
    out=[];i=0
    while i<len(v):
        x,i=varint(v,i); out.append(x)
    return out
def decode_search(raw, cache_dir):
    m = parse(parse(raw)[0][2])
    res = {"total": None, "dims": {}, "ints": {}, "columns": {}, "dicts": {}}
    dicts = {}
    for f,t,v in m:
        if f==1: res["total"]=v
        elif f==6:
            p=[x[2].decode() for x in parse(v)]
            dicts[p[0]] = p[1:]
    res["dicts"] = dicts
    def dname(kind, idx):
        h = dicts.get(kind)
        if not h: return idx
        lst = dictionary(h[0], cache_dir)
        return lst[idx] if idx < len(lst) else idx
    for f,t,v in m:
        if f==2:
            p=parse(v); name=p[0][2].decode(); kind=p[1][2].decode(); counts=[]
            for g,_,w in p:
                if g==3:
                    q={a:b for a,_,b in parse(w)} if w else {}
                    counts.append((dname(kind,q.get(1,0)), q.get(2,0)))
            res["dims"][name]={"kind":kind,"counts":counts}
        elif f==3:
            q={}
            for a,_,b in parse(v): q[a]=b
            res["ints"][q[1].decode()]={"min":q.get(2,0) if q.get(2,0)<2**63 else q[2]-2**64,"max":q.get(3,0)}
        elif f==12:
            p=parse(v); name=p[0][2].decode(); col={"raw":p}
            strs=[b.decode() for a,_,b in p if a==7]
            kind=next((b.decode() for a,_,b in p if a==11),None)
            nums=next((packed(b) for a,_,b in p if a==6),None)
            lists=[b for a,_,b in p if a==9]
            if strs: res["columns"][name]=strs
            elif lists:
                out=[]
                for b in lists:
                    q=parse(b) if b else []
                    idx=packed(q[0][2]) if q else []
                    out.append([dname(kind,x) for x in idx] if kind else idx)
                res["columns"][name]=out
            elif nums is not None:
                res["columns"][name]=[dname(kind,x) for x in nums] if kind else nums
            else:
                res["columns"][name]=p
    return res
