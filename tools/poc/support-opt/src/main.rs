// supportopt: support-gem optimiser POC. ONE pob engine, sequential jobs.
// usage: supportopt <pob_root> <state_dir> <jobs.jsonl> <out.jsonl>
// job: {id, hash, code, g, skill}. Env DUMP=<lua file>: eval it on the first job's build, print, exit.
use pob_engine::{EngineConfig, EngineHandle};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::time::{Duration, Instant};
const STATS: &str = include_str!("stats.lua");
const LOCK: &str = include_str!("lock_config.lua");
const OPT: &str = include_str!("opt.lua");
const APPLY: &str = include_str!("apply.lua");

fn ev(h: &EngineHandle, code: &str) -> Result<Value, String> {
    match h.eval(code).map_err(|e| e.to_string())? { Value::String(s) => serde_json::from_str(&s).map_err(|e| e.to_string()), o => Ok(o) }
}
fn call(h: &EngineHandle, m: &str, p: Value) -> Result<Value, String> { h.call(m, p).map(|o| o.result).map_err(|e| e.to_string()) }
fn with_params(src: &str, p: &Value) -> String { src.replace("__PARAMS__", &p.to_string()) }

fn load(h: &EngineHandle, code: &str, g: &Value, lock: bool) -> Result<(), String> {
    call(h, "load_build_code", json!({"code": code.trim(), "name": "x"}))?;
    if lock {
        ev(h, LOCK)?;
        call(h, "set_config", json!({"var": "enemyIsBoss", "value": "Pinnacle"}))?;
        if !g.is_null() { call(h, "set_main_skill", json!({"index": g}))?; }
    }
    Ok(())
}

fn run(h: &EngineHandle, job: &Value) -> Result<Value, String> {
    let t0 = Instant::now();
    let code = job["code"].as_str().unwrap_or("");
    load(h, code, &job["g"], true)?;
    let base = ev(h, STATS)?;
    let opt = ev(h, &with_params(OPT, job))?;
    if opt["err"].is_string() { return Ok(json!({"base": base, "opt": opt, "secs": t0.elapsed().as_secs_f64()})); }
    let mut exports = json!({});
    for variant in ["best", "strict"] {
        let v = &opt[variant];
        if v.is_null() { continue; }
        let ap = ev(h, &with_params(APPLY, &json!({"g": job["g"], "slots": v["slots"], "ids": v["ids"], "mas": opt["mas"]})))?;
        call(h, "refresh", json!({}))?;
        let st = ev(h, STATS)?;
        let c = call(h, "save_build_code", json!({}))?;
        exports[variant] = json!({"apply": ap, "stats": st, "code": c["code"]});
    }
    // verify: re-import the strict export as a user would (no extra locking) and recompute
    let mut verify = Value::Null;
    if let Some(c) = exports["strict"]["code"].as_str() {
        let c = c.to_string();
        verify = match load(h, &c, &Value::Null, false).and_then(|_| ev(h, STATS)) { Ok(v) => v, Err(e) => json!({"err": e}) };
    }
    Ok(json!({"base": base, "opt": opt, "exports": exports, "verify": verify, "secs": t0.elapsed().as_secs_f64()}))
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let (pob, state, input, output) = (a[1].clone(), a[2].clone(), a[3].clone(), a[4].clone());
    let cfg = EngineConfig { pob_root: pob.into(), user_dir: format!("{state}/user").into(), cache_dir: Some(format!("{state}/cache").into()) };
    let jobs: Vec<Value> = BufReader::new(std::fs::File::open(&input).unwrap()).lines().map_while(Result::ok)
        .filter(|l| !l.trim().is_empty()).map(|l| serde_json::from_str(&l).unwrap()).collect();
    let mut h = EngineHandle::spawn(cfg.clone()); h.wait_ready().unwrap();
    if let Ok(f) = std::env::var("DUMP") {
        let j = &jobs[0];
        load(&h, j["code"].as_str().unwrap(), &j["g"], true).unwrap();
        let src = std::fs::read_to_string(f).unwrap();
        println!("{}", match ev(&h, &with_params(&src, j)) { Ok(v) => v.to_string(), Err(e) => e });
        h.shutdown(); return;
    }
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&output).unwrap();
    let t0 = Instant::now();
    for (k, j) in jobs.iter().enumerate() {
        let (hh, jj) = (h.clone(), j.clone());
        let (t2, r2) = std::sync::mpsc::channel();
        std::thread::spawn(move || { let _ = t2.send(run(&hh, &jj)); });
        let r = match r2.recv_timeout(Duration::from_secs(600)) {
            Ok(Ok(v)) => v,
            Ok(Err(e)) => json!({"err": e}),
            Err(_) => { h.shutdown(); h = EngineHandle::spawn(cfg.clone()); h.wait_ready().unwrap(); json!({"err": "TIMEOUT"}) }
        };
        let mut r = r; r["id"] = j["id"].clone(); r["hash"] = j["hash"].clone(); r["skill"] = j["skill"].clone(); r["g"] = j["g"].clone();
        writeln!(f, "{r}").unwrap(); f.flush().ok();
        eprintln!("{}/{} {} {:.0}s err={}", k + 1, jobs.len(), j["skill"], t0.elapsed().as_secs_f64(), r["err"]);
    }
    h.shutdown();
}
