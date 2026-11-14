// onebutton: run optimiser jobs (JSONL {id, code, lua}) on ONE pob engine, pinnacle-locked config.
// Each job: load build -> lock config -> eval(prelude .. lib.lua .. job.lua) -> JSON result line.
// usage: onebutton <pob_root> <state_dir> <lib.lua> <jobs.jsonl> <out.jsonl> [timeout_s]
use pob_engine::{EngineConfig, EngineHandle};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::sync::mpsc;
use std::time::{Duration, Instant};
const LOCK: &str = include_str!("lock_config.lua");

fn ev(h: &EngineHandle, code: &str) -> Result<Value, String> {
    match h.eval(code).map_err(|e| e.to_string())? { Value::String(s) => serde_json::from_str(&s).map_err(|e| format!("{e}: {}", &s[..s.len().min(300)])), o => Ok(o) }
}
fn call(h: &EngineHandle, m: &str, p: Value) -> Result<Value, String> { h.call(m, p).map(|o| o.result).map_err(|e| e.to_string()) }

fn run(h: &EngineHandle, lib: &str, job: &Value) -> Value {
    let t0 = Instant::now();
    let r = (|| -> Result<Value, String> {
        call(h, "load_build_code", json!({"code": job["code"].as_str().unwrap_or("").trim(), "name": "x"}))?;
        ev(h, LOCK)?;
        call(h, "set_config", json!({"var": "enemyIsBoss", "value": "Pinnacle"}))?;
        let params = serde_json::to_string(&job["params"]).unwrap();
        let src = format!("local JOB_PARAMS = [==[{params}]==]\n{lib}\n{}", job["lua"].as_str().unwrap_or("return OB_main()"));
        ev(h, &src)
    })();
    let ms = t0.elapsed().as_millis() as u64;
    match r { Ok(v) => json!({"id": job["id"], "ms": ms, "r": v}), Err(e) => json!({"id": job["id"], "ms": ms, "err": e}) }
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let (pob, state, libp, input, output) = (a[1].clone(), a[2].clone(), a[3].clone(), a[4].clone(), a[5].clone());
    let tmo = a.get(6).and_then(|s| s.parse::<u64>().ok()).unwrap_or(1200);
    let lib = std::fs::read_to_string(&libp).unwrap();
    let cfg = EngineConfig { pob_root: pob.into(), user_dir: format!("{state}/user").into(), cache_dir: Some(format!("{state}/cache").into()) };
    let jobs: Vec<Value> = BufReader::new(std::fs::File::open(&input).unwrap()).lines().map_while(Result::ok)
        .filter(|l| !l.trim().is_empty()).map(|l| serde_json::from_str(&l).unwrap()).collect();
    let n = jobs.len();
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&output).unwrap();
    let mut h = EngineHandle::spawn(cfg.clone()); h.wait_ready().unwrap();
    for (k, j) in jobs.into_iter().enumerate() {
        let (hh, jj, ll) = (h.clone(), j.clone(), lib.clone());
        let (t2, r2) = mpsc::channel();
        std::thread::spawn(move || { let _ = t2.send(run(&hh, &ll, &jj)); });
        let v = match r2.recv_timeout(Duration::from_secs(tmo)) {
            Ok(v) => v,
            Err(_) => { h.shutdown(); h = EngineHandle::spawn(cfg.clone()); h.wait_ready().unwrap(); json!({"id": j["id"], "err": "TIMEOUT"}) }
        };
        eprintln!("[{}/{}] {} {}ms {}", k + 1, n, j["id"], v["ms"], if v["err"].is_null() { "ok" } else { "ERR" });
        writeln!(f, "{v}").unwrap(); f.flush().ok();
    }
    h.shutdown();
}
