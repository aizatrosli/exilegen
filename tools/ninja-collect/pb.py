import sys
def varint(b,i):
    r=0;s=0
    while True:
        c=b[i];i+=1;r|=(c&0x7f)<<s;s+=7
        if c<0x80:return r,i
def parse(b):
    i=0;out=[]
    while i<len(b):
        k,i=varint(b,i);f=k>>3;t=k&7
        if f==0: raise ValueError
        if t==0:v,i=varint(b,i)
        elif t==1:v=b[i:i+8];i+=8
        elif t==5:v=b[i:i+4];i+=4
        elif t==2:
            l,i=varint(b,i);v=b[i:i+l];i+=l
            if i>len(b): raise ValueError
        else: raise ValueError
        out.append((f,t,v))
    return out
def tryparse(v):
    try:
        p=parse(v)
        return p if p else None
    except Exception: return None
def show(b,ind=0,maxrep=3,depth=8):
    p=parse(b);cnt={}
    for f,t,v in p:
        cnt[f]=cnt.get(f,0)+1
        if cnt[f]>maxrep:
            if cnt[f]==maxrep+1: print(' '*ind+f'{f}: ... ({sum(1 for x in p if x[0]==f)} total)')
            continue
        if t==2:
            s=None
            try:
                s=v.decode('utf-8')
                if not s.isprintable(): s=None
            except: pass
            sub=tryparse(v) if depth>0 else None
            if s is not None and (sub is None or len(s)<40 and s.isprintable()):
                print(' '*ind+f'{f}: "{s[:80]}"')
            elif sub:
                print(' '*ind+f'{f}: {{ ({len(v)}B)');show(v,ind+2,maxrep,depth-1);print(' '*ind+'}')
            else: print(' '*ind+f'{f}: bytes{len(v)} {v[:20].hex()}')
        else: print(' '*ind+f'{f}[{t}]: {v if t==0 else v.hex()}')
if __name__=='__main__':
    show(open(sys.argv[1],'rb').read(),maxrep=int(sys.argv[2]) if len(sys.argv)>2 else 3)
