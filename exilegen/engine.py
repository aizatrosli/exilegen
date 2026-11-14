"""Python client for exg-engine (one PoB-PoE2 engine over stdin/stdout JSON lines).

    from exilegen.engine import Engine, EnginePool
    with Engine() as e:
        e.load(code=share_code, profile="pinnacle_v1")
        base = e.stats()
        r = e.whatif(add_nodes=[12345])

The op contract is documented in engine/API.md.

Locating things (first match wins):
  binary    Engine(binary=...), $EXG_ENGINE_BIN, $CARGO_TARGET_DIR/release/exg-engine,
            <repo>/engine/target/release/exg-engine
  PoB data  Engine(pob_root=...), $EXG_POB_ROOT, $POB_ROOT
  state     Engine(state=...), $EXG_STATE, <tmp>/exg-engine   (PoB user dir + compiled Lua cache)
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Iterable

PKG = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(PKG)
LUA_PATH = os.path.join(PKG, "lua", "exg.lua")
PROFILES_PATH = os.path.join(PKG, "data", "profiles.json")

__all__ = ["Engine", "EnginePool", "EngineError", "EngineCrash", "find_binary", "find_pob_root", "code_hash"]


class EngineError(RuntimeError):
    """An op failed inside the engine (bad params, Lua error). The engine is still usable."""


class EngineCrash(EngineError):
    """The engine process died or timed out. It was restarted; mutated state was lost."""


def code_hash(code: str) -> str:
    """Same hash as corpus-eval: sha256 of the trimmed code, first 16 bytes hex."""
    return hashlib.sha256(code.strip().encode()).hexdigest()[:32]


def find_binary(explicit: str | None = None) -> str | None:
    cands = [explicit, os.environ.get("EXG_ENGINE_BIN")]
    if os.environ.get("CARGO_TARGET_DIR"):
        cands.append(os.path.join(os.environ["CARGO_TARGET_DIR"], "release", "exg-engine"))
    cands.append(os.path.join(REPO, "engine", "target", "release", "exg-engine"))
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def find_pob_root(explicit: str | None = None) -> str | None:
    for c in (explicit, os.environ.get("EXG_POB_ROOT"), os.environ.get("POB_ROOT")):
        if c and os.path.isfile(os.path.join(c, "Launch.lua")):
            return c
    return None


# ops that never change engine state: safe to retry after a crash
_READ_ONLY = {"stats", "skills", "valid_supports", "tree", "whatif", "whatif_many", "export", "export_xml",
              "summary", "profiles", "ping", "gc"}


class Engine:
    """One exg-engine process. Not thread-safe: use one Engine per thread (see EnginePool)."""

    def __init__(self, binary: str | None = None, pob_root: str | None = None, state: str | None = None,
                 timeout: float = 120.0, lua: str | None = LUA_PATH, profiles: str | None = PROFILES_PATH,
                 stderr: str | None = None, start: bool = True):
        self.binary = find_binary(binary)
        if not self.binary:
            raise FileNotFoundError("exg-engine binary not found (set EXG_ENGINE_BIN or build engine/)")
        self.pob_root = find_pob_root(pob_root)
        if not self.pob_root:
            raise FileNotFoundError("PoB data root not found (set EXG_POB_ROOT)")
        self.state = state or os.environ.get("EXG_STATE") or os.path.join(tempfile.gettempdir(), "exg-engine")
        self.timeout = timeout
        self.lua_path, self.profiles_path, self.stderr_path = lua, profiles, stderr
        self.proc: subprocess.Popen | None = None
        self.info: dict = {}
        self.restarts = 0
        self.n_ops = 0
        # what is loaded, for reload after a crash and for ensure_loaded
        self._load_params: dict | None = None
        self._load_key: str | None = None
        self._profile: str | None = None
        self._main: Any = None
        self._mutated = False
        self._base_token: str | None = None
        self._lines: queue.Queue = queue.Queue()
        if start:
            self.start()

    # ------------------------------------------------------------------ process
    def start(self) -> dict:
        os.makedirs(self.state, exist_ok=True)
        cmd = [self.binary, "--pob-root", self.pob_root, "--state", self.state, "--timeout", str(self.timeout)]
        if self.lua_path:
            cmd += ["--lua", self.lua_path]
        if self.profiles_path:
            cmd += ["--profiles", self.profiles_path]
        err = open(self.stderr_path, "ab") if self.stderr_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, bufsize=0)
        self._lines = queue.Queue()
        threading.Thread(target=self._reader, args=(self.proc, self._lines), daemon=True).start()
        r = self._read(self.timeout + 60)
        if not r.get("ok"):
            self.kill()
            raise EngineError(f"engine failed to start: {r.get('err')}")
        self.info = r["r"]
        self._load_key = None
        self._mutated = False
        self._base_token = None
        return self.info

    @staticmethod
    def _reader(proc: subprocess.Popen, q: queue.Queue) -> None:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("{"):
                q.put(line)
        q.put(None)

    def _read(self, timeout: float) -> dict:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self.kill()
            return {"ok": False, "err": f"no reply within {timeout}s", "fatal": True}
        if line is None:
            return {"ok": False, "err": "engine exited", "fatal": True}
        return json.loads(line)

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(10)
            except Exception:
                pass

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b'{"op":"quit"}\n')
                self.proc.stdin.flush()
                self.proc.wait(10)
            except Exception:
                self.kill()
        self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ------------------------------------------------------------------ requests
    def _send(self, req: dict, timeout: float | None) -> dict:
        if not self.alive():
            return {"ok": False, "err": "engine not running", "fatal": True}
        t = timeout or self.timeout
        if timeout:
            req = dict(req, timeout=timeout)
        try:
            self.proc.stdin.write((json.dumps(req) + "\n").encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            return {"ok": False, "err": f"engine pipe closed: {e}", "fatal": True}
        return self._read(t + 30)

    def _restart_and_reload(self) -> None:
        self.kill()
        self.restarts += 1
        self.start()
        if self._load_params is not None:
            r = self._send(dict(self._load_params, op="load"), None)
            if not r.get("ok"):
                raise EngineCrash(f"reload after restart failed: {r.get('err')}")
            self._load_key = self._key(self._load_params)
            if self._profile and self._profile != self._load_params.get("profile"):
                self._send({"op": "set_profile", "name": self._profile, "main": self._main}, None)

    def request(self, req: dict, timeout: float | None = None, _retry: bool = True) -> Any:
        self.n_ops += 1
        r = self._send(req, timeout)
        if r.get("ok"):
            return r["r"]
        if r.get("fatal"):
            op = req.get("op")
            was_mutated = self._mutated
            self._restart_and_reload()
            if _retry and op in _READ_ONLY and not was_mutated:
                return self.request(req, timeout, _retry=False)
            raise EngineCrash(f"{op}: {r.get('err')} (engine restarted, build reloaded{', mutations lost' if was_mutated else ''})")
        raise EngineError(r.get("err"))

    def op(self, _op: str, _timeout: float | None = None, **params) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        return self.request(dict(params, op=_op), _timeout)

    # ------------------------------------------------------------------ ops
    @staticmethod
    def _key(p: dict) -> str:
        src = p.get("code") or p.get("xml") or ""
        return f"{code_hash(src)}|{p.get('profile')}|{p.get('main')}"

    def load(self, code: str | None = None, xml: str | None = None, profile: str | None = None,
             main: Any = None, timeout: float | None = None, gc: str | None = None) -> dict:
        """Load a build (share code or XML). profile: as_exported|pinnacle_v1|mapping_v1|...;
        main: "auto" (default, highest-DPS skill) | "pob" (exported mark) | group index."""
        if not code and not xml:
            raise ValueError("load needs code or xml")
        p = {k: v for k, v in dict(code=code.strip() if code else None, xml=xml, profile=profile, main=main).items() if v is not None}
        r = self.request(dict(p, op="load", **({"gc": gc} if gc else {})), timeout)
        self._load_params, self._load_key = p, self._key(p)
        self._profile, self._main, self._mutated, self._base_token = profile, main, False, None
        return r

    def ensure_loaded(self, code: str | None = None, xml: str | None = None, profile: str | None = None,
                      main: Any = None) -> bool:
        """Load unless this build (same code hash, profile and main) is already loaded; a loaded
        build that was mutated is restored from its base snapshot instead. True if (re)loaded."""
        p = {k: v for k, v in dict(code=code.strip() if code else None, xml=xml, profile=profile, main=main).items() if v is not None}
        if self._load_key == self._key(p) and self.alive():
            if self._mutated:
                if self._base_token:
                    self.restore(self._base_token)
                    self._mutated = False
                else:
                    self.load(**p)
                    return True
            return False
        self.load(**p)
        self._base_token = self.snapshot()
        self._mutated = False
        return True

    def set_profile(self, name: str, main: Any = None) -> dict:
        r = self.op("set_profile", name=name, main=main)
        self._profile = name
        if main is not None:
            self._main = main
        return r

    def select_main(self, main: Any = "auto") -> dict:
        self._main = main
        return self.op("select_main", main=main)

    def summary(self) -> dict:
        return self.op("summary")

    def profiles(self) -> dict:
        return self.op("profiles")

    def stats(self, fields: Iterable[str] | None = None, skip_ehp: bool | None = None,
              marginals: bool | None = None) -> dict:
        """Stats record of the current state. fields: None = "all"; "core"; or a list of top-level
        keys (API.md). marginals=True adds drop-one support marginals (one calc per support)."""
        if isinstance(fields, str):
            fields = [fields]
        return self.op("stats", fields=list(fields) if fields else None, skip_ehp=skip_ehp, marginals=marginals)

    def skills(self) -> dict:
        return self.op("skills")

    def valid_supports(self, group: int, skill: int | None = None, include_used: bool | None = None) -> dict:
        return self.op("valid_supports", group=group, skill=skill, include_used=include_used)

    def tree(self, frontier: bool = True, max_dist: int | None = None, graph: bool = False,
             ascendancy: bool = True) -> dict:
        return self.op("tree", frontier=frontier, max_dist=max_dist, graph=graph, ascendancy=ascendancy)

    @staticmethod
    def _change(add_nodes=None, remove_nodes=None, attr_choices=None, gem_changes=None, group_enabled=None,
                fields=None, skip_ehp=None, marginals=None) -> dict:
        d = dict(add_nodes=add_nodes, remove_nodes=remove_nodes,
                 attr_choices={str(k): v for k, v in attr_choices.items()} if attr_choices else None,
                 gem_changes=gem_changes,
                 group_enabled={str(k): v for k, v in group_enabled.items()} if group_enabled else None,
                 fields=list(fields) if fields is not None and not isinstance(fields, (str, bool)) else fields,
                 skip_ehp=skip_ehp, marginals=marginals)
        return {k: v for k, v in d.items() if v is not None}

    def whatif(self, **change) -> dict:
        """Stats for a change without keeping it. change: add_nodes, remove_nodes, attr_choices,
        gem_changes, group_enabled, fields, skip_ehp (see API.md)."""
        return self.request(dict(self._change(**change), op="whatif"))

    def whatif_many(self, variants: list[dict], fields: Iterable[str] | None = None,
                    skip_ehp: bool | None = None) -> list[dict]:
        p = {"variants": [self._change(**v) for v in variants]}
        if fields is not None:
            p["fields"] = list(fields) if not isinstance(fields, str) else fields
        if skip_ehp is not None:
            p["skip_ehp"] = skip_ehp
        return self.request(dict(p, op="whatif_many"))

    def apply(self, **change) -> dict:
        """Make a change and return the new stats (fields=False returns nothing)."""
        self._mutated = True
        return self.request(dict(self._change(**change), op="apply"))

    def snapshot(self) -> str:
        return self.op("snapshot")["token"]

    def restore(self, token: str) -> bool:
        r = self.op("restore", token=token)["restored"]
        self._mutated = token != self._base_token
        return r

    def export(self) -> str:
        return self.op("export")["code"]

    def export_xml(self) -> str:
        return self.op("export_xml")["xml"]

    def lua(self, chunk: str) -> Any:
        """Run a Lua chunk (debug). A string result holding JSON is decoded."""
        self._mutated = True
        return self.op("lua", chunk=chunk)

    def raw(self, method: str, params: dict | None = None) -> Any:
        """Call a pob-redux bridge method directly (debug)."""
        self._mutated = True
        return self.request({"m": method, "p": params or {}})

    def gc(self) -> dict:
        return self.op("gc")


class EnginePool:
    """n persistent engines, each driven by its own thread.

        with EnginePool(2) as pool:
            out = pool.map(lambda eng, job: (eng.ensure_loaded(code=job["code"], profile="pinnacle_v1"),
                                             eng.stats(fields=["core"]))[1], jobs)

    fn(engine, job) runs on a worker; use engine.ensure_loaded() so each worker keeps its build
    cached by code hash between jobs. Results come back in job order.
    """

    def __init__(self, n: int = 2, **engine_kwargs):
        self.engines: list[Engine] = []
        errs: list[BaseException] = []

        def boot():
            try:
                e = Engine(**engine_kwargs)
                self.engines.append(e)
            except BaseException as ex:  # noqa: BLE001
                errs.append(ex)

        threads = [threading.Thread(target=boot) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errs:
            self.close()
            raise errs[0]

    def __len__(self):
        return len(self.engines)

    def map(self, fn: Callable[[Engine, Any], Any], jobs: Iterable[Any], return_exceptions: bool = False,
            progress: Callable[[int, int], None] | None = None) -> list[Any]:
        jobs = list(jobs)
        results: list[Any] = [None] * len(jobs)
        q: queue.Queue = queue.Queue()
        for i, j in enumerate(jobs):
            q.put((i, j))
        done = [0]
        lock = threading.Lock()
        first_err: list[BaseException] = []

        def work(eng: Engine):
            while True:
                try:
                    i, j = q.get_nowait()
                except queue.Empty:
                    return
                if first_err and not return_exceptions:
                    return
                try:
                    results[i] = fn(eng, j)
                except BaseException as ex:  # noqa: BLE001
                    if return_exceptions:
                        results[i] = ex
                    else:
                        first_err.append(ex)
                        return
                with lock:
                    done[0] += 1
                    if progress:
                        progress(done[0], len(jobs))

        threads = [threading.Thread(target=work, args=(e,)) for e in self.engines]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if first_err:
            raise first_err[0]
        return results

    def close(self):
        for e in self.engines:
            e.close()
        self.engines = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
