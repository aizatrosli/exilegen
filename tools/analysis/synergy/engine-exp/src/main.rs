// synexp: run experiment jobs (JSONL: {id, code, ops:[...]}) on a pool of pob engines; pinnacle-locked config.
use pob_engine::{EngineConfig, EngineHandle};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant};
const STATS: &str = include_str!("stats.lua");
const GROUPS: &str = include_str!("groups.lua");
const LOCK: &str = include_str!("lock_config.lua");

fn ev(h: &EngineHandle, code: &str) -> Result<Value, String> {
    match h.eval(code).map_err(|e| e.to_string())? { Value::String(s) => serde_json::from_str(&s).map_err(|e| e.to_string()), o => Ok(o) }
}
fn call(h: &EngineHandle, m: &str, p: Value) -> Result<Value, String> { h.call(m, p).map(|o| o.result).map_err(|e| e.to_string()) }

fn run(h: &EngineHandle, job: &Value) -> Value {
    let mut res = vec![];
    let r = (|| -> Result<(), String> {
        call(h, "load_build_code", json!({"code": job["code"].as_str().unwrap_or("").trim(), "name": "x"}))?;
        ev(h, LOCK)?;
        call(h, "set_config", json!({"var": "enemyIsBoss", "value": "Pinnacle"}))?;
        for op in job["ops"].as_array().cloned().unwrap_or_default() {
            let label = op["label"].clone();
            let t0 = Instant::now();
            let out: Result<Value, String> = match op["op"].as_str().unwrap_or("") {
                "stats" => ev(h, STATS),
                "call" => call(h, op["m"].as_str().unwrap(), op["p"].clone()).map(|v| if op["keep"].as_bool() == Some(true) { v } else { Value::Null }),
                "callstats" => call(h, op["m"].as_str().unwrap(), op["p"].clone()).and_then(|_| ev(h, STATS)),
                "eval" => ev(h, op["lua"].as_str().unwrap()),
                "scan_groups" => (|| {
                    let g = ev(h, GROUPS)?;
                    let mut rows = vec![];
                    for it in g["list"].as_array().cloned().unwrap_or_default() {
                        let r = call(h, "set_main_skill", json!({"index": it["g"]}))
                            .and_then(|_| call(h, "set_main_skill_options", json!({"groupIndex": it["g"], "mainActiveSkill": it["s"]})))
                            .and_then(|_| ev(h, STATS));
                        rows.push(json!({"g": it["g"], "s": it["s"], "name": it["name"], "id": it["id"], "r": r.unwrap_or_else(|e| json!({"err": e}))}));
                    }
                    let _ = call(h, "set_main_skill", json!({"index": g["main"]}));
                    let _ = call(h, "set_main_skill_options", json!({"groupIndex": g["main"], "mainActiveSkill": g["mas"]}));
                    Ok(json!({"main": g["main"], "rows": rows}))
                })(),
                o => Err(format!("bad op {o}")),
            };
            let ok = out.is_ok();
            res.push(json!({"label": label, "ok": ok, "ms": t0.elapsed().as_millis() as u64, "r": out.unwrap_or_else(|e| json!({"err": e}))}));
            if !ok && op["fatal"].as_bool() == Some(true) { break; }
        }
        Ok(())
    })();
    json!({"id": job["id"], "err": r.err(), "res": res})
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let (pob, state, input, output, workers) = (a[1].clone(), a[2].clone(), a[3].clone(), a[4].clone(), a[5].parse::<usize>().unwrap());
    let cfg = EngineConfig { pob_root: pob.into(), user_dir: format!("{state}/user").into(), cache_dir: Some(format!("{state}/cache").into()) };
    { let h = EngineHandle::spawn(cfg.clone()); h.wait_ready().unwrap(); h.shutdown(); }
    let jobs: Vec<Value> = BufReader::new(std::fs::File::open(&input).unwrap()).lines().map_while(Result::ok)
        .filter(|l| !l.trim().is_empty()).map(|l| serde_json::from_str(&l).unwrap()).collect();
    let n = jobs.len();
    let q = Arc::new(Mutex::new(jobs.into_iter()));
    let (tx, rx) = mpsc::channel::<Value>();
    for _ in 0..workers {
        let (q, tx, cfg) = (q.clone(), tx.clone(), cfg.clone());
        std::thread::spawn(move || {
            let mut h = EngineHandle::spawn(cfg.clone()); h.wait_ready().unwrap();
            let mut used = 0;
            loop {
                let j = { q.lock().unwrap().next() };
                let Some(j) = j else { break };
                if used >= 150 { h.shutdown(); h = EngineHandle::spawn(cfg.clone()); h.wait_ready().unwrap(); used = 0; }
                used += 1;
                let hh = h.clone(); let jj = j.clone();
                let (t2, r2) = mpsc::channel();
                std::thread::spawn(move || { let _ = t2.send(run(&hh, &jj)); });
                match r2.recv_timeout(Duration::from_secs(300)) {
                    Ok(v) => { let _ = tx.send(v); }
                    Err(_) => { let _ = tx.send(json!({"id": j["id"], "err": "TIMEOUT"})); h.shutdown(); h = EngineHandle::spawn(cfg.clone()); h.wait_ready().unwrap(); used = 0; }
                }
            }
            h.shutdown();
        });
    }
    drop(tx);
    let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&output).unwrap();
    let t0 = Instant::now(); let mut k = 0;
    for v in rx { writeln!(f, "{v}").unwrap(); f.flush().ok(); k += 1; if k % 25 == 0 { eprintln!("{k}/{n} {:.0}s", t0.elapsed().as_secs_f64()); } }
    eprintln!("done {k}/{n} {:.0}s", t0.elapsed().as_secs_f64());
}
