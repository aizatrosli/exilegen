// pobrepl: one pob engine driven over stdin/stdout JSON lines.
// in:  {"lua": "<chunk returning a JSON string>"} | {"m": "<bridge method>", "p": {...}}
// out: {"ok": true, "r": <value>} | {"ok": false, "err": "..."}
use pob_engine::{EngineConfig, EngineHandle};
use serde_json::{json, Value};
use std::io::{BufRead, Write};

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let (pob, state) = (a[1].clone(), a[2].clone());
    let cfg = EngineConfig { pob_root: pob.into(), user_dir: format!("{state}/user").into(), cache_dir: Some(format!("{state}/cache").into()) };
    let h = EngineHandle::spawn(cfg); h.wait_ready().unwrap();
    let stdin = std::io::stdin(); let mut out = std::io::stdout();
    writeln!(out, "{}", json!({"ok": true, "r": "ready"})).unwrap(); out.flush().unwrap();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() { continue }
        let req: Value = match serde_json::from_str(&line) { Ok(v) => v, Err(e) => { writeln!(out, "{}", json!({"ok": false, "err": e.to_string()})).unwrap(); out.flush().unwrap(); continue } };
        let res: Result<Value, String> = if let Some(code) = req["lua"].as_str() {
            h.eval(code).map_err(|e| e.to_string()).and_then(|v| match v { Value::String(s) => serde_json::from_str(&s).or(Ok(Value::String(s))), o => Ok(o) })
        } else {
            h.call(req["m"].as_str().unwrap_or(""), req["p"].clone()).map(|o| o.result).map_err(|e| e.to_string())
        };
        let o = match res { Ok(v) => json!({"ok": true, "r": v}), Err(e) => json!({"ok": false, "err": e}) };
        writeln!(out, "{o}").unwrap(); out.flush().unwrap();
    }
    h.shutdown();
}
