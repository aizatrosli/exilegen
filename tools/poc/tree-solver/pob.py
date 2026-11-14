"""Thin client for the pobrepl binary (one PoB engine over stdin/stdout JSON lines)."""
import json, os, subprocess, time
HERE = os.path.dirname(os.path.abspath(__file__))
SCR = os.environ.get("EXG_SCRATCH", ".cache")  # CARGO_TARGET_DIR parent and engine state
BIN = os.environ.get("POBREPL", f"{SCR}/exg-target/release/pobrepl")
POB = os.environ.get("POB_ROOT", f"{SCR}/pob2")
STATE = os.environ.get("TS_STATE", f"{SCR}/ts-state")

class Pob:
    def __init__(self):
        os.makedirs(STATE, exist_ok=True)
        self.p = subprocess.Popen([BIN, POB, STATE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        r = json.loads(self.p.stdout.readline()); assert r["ok"], r
        self.n_eval = 0
    def _req(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n"); self.p.stdin.flush()
        r = json.loads(self.p.stdout.readline())
        if not r["ok"]: raise RuntimeError(r["err"][:2000])
        return r["r"]
    def lua(self, code): return self._req({"lua": code})
    def call(self, m, p=None): return self._req({"m": m, "p": p or {}})
    def ts(self, expr, arg=None):
        """call TS.<expr>(decoded arg) and return decoded result"""
        a = json.dumps(arg if arg is not None else {})
        return self.lua(f"local d=require('dkjson'); local a=d.decode({json.dumps(a)}); return TS.enc({expr}(a))")
    def load(self, code, group=None):
        self.call("load_build_code", {"code": code.strip(), "name": "x"})
        self.lua(open(os.path.join(HERE, "lock_config.lua")).read())
        self.call("set_config", {"var": "enemyIsBoss", "value": "Pinnacle"})
        if group is not None:
            try: self.call("set_main_skill", {"index": group})
            except Exception as e: print("set_main_skill failed", e)
        self.lua(open(os.path.join(HERE, "ts.lua")).read())
        self.lua("TS.refresh(); return TS.enc({})")
    def evalb(self, jobs):
        self.n_eval += len(jobs)
        return self.lua("local d=require('dkjson'); return TS.enc(TS.evalBatch(d.decode(%s)))" % json.dumps(json.dumps(jobs)))
    def close(self):
        try: self.p.stdin.close(); self.p.wait(30)
        except Exception: self.p.kill()
