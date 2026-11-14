//! exg-engine: one Path of Building (PoE2) engine served over stdin/stdout JSON lines.
//!
//! On start it boots PoB (pob-engine crate), loads `exilegen/lua/exg.lua` once (from `--lua`
//! or the copy embedded at build time) with the profile file, prints one ready line and then
//! answers one JSON line per request line. See `engine/API.md` for the op list.
//!
//! Requests:
//!   {"op": "<name>", ...params, "id"?: any, "timeout"?: seconds}   exilegen op (exg.lua)
//!   {"lua": "<chunk>"}                                                raw Lua (debug)
//!   {"m": "<bridge method>", "p": {...}}                              raw pob-redux bridge call
//! Replies:
//!   {"ok": true,  "r": <result>, "id": .., "ms": <wall ms>}
//!   {"ok": false, "err": "...",  "id": .., "ms": .., "fatal"?: true}
//!
//! A Lua call cannot be interrupted, so when a request runs past its timeout the engine
//! replies with `"fatal": true, "restart": true` and exits with status 3. A client then starts
//! a new engine and reloads its build. An engine that died (Lua panic, thread gone) exits 4.

use pob_engine::{EngineConfig, EngineHandle};
use serde_json::{json, Map, Value};
use std::io::{BufRead, Write};
use std::path::PathBuf;
use std::sync::mpsc;
use std::time::{Duration, Instant};

const EXG_LUA: &str = include_str!("../../exilegen/lua/exg.lua");
const PROFILES_JSON: &str = include_str!("../../exilegen/data/profiles.json");

struct Args {
    pob_root: PathBuf,
    state: PathBuf,
    lua: Option<PathBuf>,
    profiles: Option<PathBuf>,
    timeout: f64,
}

fn usage() -> ! {
    eprintln!("usage: exg-engine --pob-root DIR [--state DIR] [--lua exg.lua] [--profiles profiles.json] [--timeout SECONDS]");
    eprintln!("       env fallbacks: EXG_POB_ROOT or POB_ROOT, EXG_STATE");
    std::process::exit(2)
}

fn parse_args() -> Args {
    let env = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
    let mut a = Args {
        pob_root: env("EXG_POB_ROOT").or_else(|| env("POB_ROOT")).map(PathBuf::from).unwrap_or_default(),
        state: env("EXG_STATE").map(PathBuf::from).unwrap_or_else(|| std::env::temp_dir().join("exg-engine")),
        lua: None,
        profiles: None,
        timeout: 120.0,
    };
    let v: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < v.len() {
        let val = |i: usize| v.get(i + 1).cloned().unwrap_or_else(|| usage());
        match v[i].as_str() {
            "--pob-root" => { a.pob_root = val(i).into(); i += 1 }
            "--state" => { a.state = val(i).into(); i += 1 }
            "--lua" => { a.lua = Some(val(i).into()); i += 1 }
            "--profiles" => { a.profiles = Some(val(i).into()); i += 1 }
            "--timeout" => { a.timeout = val(i).parse().unwrap_or_else(|_| usage()); i += 1 }
            "-h" | "--help" => usage(),
            other => { eprintln!("unknown argument {other}"); usage() }
        }
        i += 1;
    }
    if !a.pob_root.join("Launch.lua").is_file() {
        eprintln!("--pob-root {:?} does not contain Launch.lua", a.pob_root);
        usage()
    }
    a
}

/// A Lua long-bracket literal that contains `s` verbatim.
fn lua_long_string(s: &str) -> String {
    let mut n = 0;
    loop {
        let close = format!("]{}]", "=".repeat(n));
        if !s.contains(&close) {
            let eq = "=".repeat(n);
            return format!("[{eq}[\n{s}]{eq}]");
        }
        n += 1;
    }
}

enum Fail {
    Soft(String),
    Timeout(String),
    Dead(String),
}

struct Server {
    h: EngineHandle,
}

impl Server {
    /// Runs one engine job with a deadline. A job that overruns cannot be stopped.
    fn run<T: Send + 'static>(&self, timeout: f64, what: &str, f: impl FnOnce(EngineHandle) -> pob_engine::Result<T> + Send + 'static) -> Result<T, Fail> {
        let h = self.h.clone();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || { let _ = tx.send(f(h)); });
        match rx.recv_timeout(Duration::from_secs_f64(timeout.max(0.001))) {
            Ok(Ok(v)) => Ok(v),
            Ok(Err(e)) => {
                let s = e.to_string();
                if matches!(e, pob_engine::Error::NotRunning(_)) { Err(Fail::Dead(s)) } else { Err(Fail::Soft(s)) }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => Err(Fail::Timeout(format!("timeout after {timeout}s in {what}"))),
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(Fail::Dead(format!("engine thread died in {what}"))),
        }
    }

    /// Evaluates a chunk; a string result holding JSON is decoded.
    fn eval_json(&self, timeout: f64, what: &str, code: String) -> Result<Value, Fail> {
        let v = self.run(timeout, what, move |h| h.eval(&code))?;
        Ok(match v {
            Value::String(s) => serde_json::from_str(&s).unwrap_or(Value::String(s)),
            o => o,
        })
    }

    fn op(&self, timeout: f64, name: &str, params: &Value) -> Result<Value, Fail> {
        let req = json!({"op": name, "p": params}).to_string();
        self.eval_json(timeout, name, format!("return EXG.call({})", lua_long_string(&req)))
    }

    fn handle(&self, req: &Map<String, Value>, default_timeout: f64) -> Result<Value, Fail> {
        let timeout = req.get("timeout").and_then(Value::as_f64).unwrap_or(default_timeout);
        if let Some(code) = req.get("lua").and_then(Value::as_str) {
            return self.eval_json(timeout, "lua", code.to_string());
        }
        if let Some(m) = req.get("m").and_then(Value::as_str) {
            let (m, p) = (m.to_string(), req.get("p").cloned().unwrap_or(Value::Null));
            return self.run(timeout, &m.clone(), move |h| h.call(&m, p).map(|o| o.result));
        }
        let Some(op) = req.get("op").and_then(Value::as_str) else {
            return Err(Fail::Soft("request needs \"op\", \"lua\" or \"m\"".into()));
        };
        let mut params = req.clone();
        params.remove("op");
        params.remove("id");
        params.remove("timeout");
        let params = Value::Object(params);
        match op {
            "load" => {
                // Free the previous build before PoB builds the next one.
                let gc = params.get("gc").and_then(Value::as_str).unwrap_or("auto").replace(['"', '\\'], "");
                self.run(timeout, "gc", move |h| h.eval(&format!("EXG.before_load(); return EXG.gc_between_loads(\"{gc}\")")))?;
                let name = params.get("name").cloned().unwrap_or(json!("exg"));
                let (m, p) = if let Some(code) = params.get("code").and_then(Value::as_str) {
                    ("load_build_code", json!({"code": code.trim(), "name": name}))
                } else if let Some(xml) = params.get("xml").and_then(Value::as_str) {
                    ("load_build_xml", json!({"xml": xml, "name": name}))
                } else {
                    return Err(Fail::Soft("load needs code or xml".into()));
                };
                self.run(timeout, m, move |h| h.call(m, p).map(|o| o.result))?;
                self.op(timeout, "_after_load", &params)
            }
            _ => self.op(timeout, op, &params),
        }
    }
}

fn main() {
    let args = parse_args();
    let t0 = Instant::now();
    let cfg = EngineConfig {
        pob_root: args.pob_root.clone(),
        user_dir: args.state.join("user"),
        cache_dir: Some(args.state.join("cache")),
    };
    let h = EngineHandle::spawn(cfg);
    let mut out = std::io::stdout();
    let reply = |out: &mut std::io::Stdout, v: Value| {
        let _ = writeln!(out, "{v}");
        let _ = out.flush();
    };
    if let Err(e) = h.wait_ready() {
        reply(&mut out, json!({"ok": false, "err": format!("engine failed to boot: {e}"), "fatal": true}));
        std::process::exit(4);
    }
    let srv = Server { h: h.clone() };

    let lua_src = match &args.lua {
        Some(p) => std::fs::read_to_string(p).unwrap_or_else(|e| { eprintln!("cannot read {p:?}: {e}"); std::process::exit(2) }),
        None => EXG_LUA.to_string(),
    };
    let profiles = match &args.profiles {
        Some(p) => std::fs::read_to_string(p).unwrap_or_else(|e| { eprintln!("cannot read {p:?}: {e}"); std::process::exit(2) }),
        None => PROFILES_JSON.to_string(),
    };
    let init = format!(
        "local f = assert(loadstring({}, 'exg.lua')); f(); return EXG.init({})",
        lua_long_string(&lua_src),
        lua_long_string(&profiles)
    );
    let info = match srv.eval_json(args.timeout, "init", init) {
        Ok(v) => v,
        Err(Fail::Soft(e)) | Err(Fail::Timeout(e)) | Err(Fail::Dead(e)) => {
            reply(&mut out, json!({"ok": false, "err": format!("exg.lua failed to load: {e}"), "fatal": true}));
            std::process::exit(4);
        }
    };
    let version = h.call("version", Value::Null).map(|o| o.result).unwrap_or(Value::Null);
    reply(&mut out, json!({"ok": true, "r": {"ready": true, "boot_ms": t0.elapsed().as_millis() as u64, "exg": info,
        "pob": version, "pid": std::process::id()}}));

    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() { continue }
        let t = Instant::now();
        let req: Map<String, Value> = match serde_json::from_str::<Value>(&line) {
            Ok(Value::Object(m)) => m,
            Ok(_) => { reply(&mut out, json!({"ok": false, "err": "request must be a JSON object"})); continue }
            Err(e) => { reply(&mut out, json!({"ok": false, "err": format!("bad json: {e}")})); continue }
        };
        let id = req.get("id").cloned().unwrap_or(Value::Null);
        if req.get("op").and_then(Value::as_str) == Some("quit") {
            reply(&mut out, json!({"ok": true, "r": "bye", "id": id}));
            break;
        }
        let res = srv.handle(&req, args.timeout);
        let ms = (t.elapsed().as_secs_f64() * 1000.0 * 1000.0).round() / 1000.0;
        match res {
            Ok(v) => reply(&mut out, json!({"ok": true, "r": v, "id": id, "ms": ms})),
            Err(Fail::Soft(e)) => reply(&mut out, json!({"ok": false, "err": e, "id": id, "ms": ms})),
            Err(Fail::Timeout(e)) => {
                reply(&mut out, json!({"ok": false, "err": e, "id": id, "ms": ms, "fatal": true, "restart": true}));
                std::process::exit(3);
            }
            Err(Fail::Dead(e)) => {
                reply(&mut out, json!({"ok": false, "err": e, "id": id, "ms": ms, "fatal": true, "restart": true}));
                std::process::exit(4);
            }
        }
    }
    h.shutdown();
}
