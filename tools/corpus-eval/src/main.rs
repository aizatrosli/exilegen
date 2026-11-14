//! corpus-eval: batch-evaluate PoE2 PoB share codes with pob-engine.
//!
//! Reads a JSONL of builds (`code` + metadata), evaluates each under two config
//! profiles on a pool of worker engines, and appends one record per build to
//! evaluated.jsonl (failures to errors.jsonl). Resumable: builds whose code hash
//! is already in either output are skipped. `--follow` tails the input until the
//! producer goes quiet.

use pob_engine::{EngineConfig, EngineHandle};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender, SyncSender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const EXTRACT_LUA: &str = include_str!("extract.lua");
const CONFIG_LUA: &str = include_str!("config_snapshot.lua");
const LOCK_LUA: &str = include_str!("lock_config.lua");

#[derive(Clone)]
struct Args {
    pob_root: PathBuf,
    input: PathBuf,
    out_dir: PathBuf,
    state_dir: PathBuf,
    workers: usize,
    follow: bool,
    idle_min: u64,
    limit: usize,
    recycle_every: usize,
    timeout_s: u64,
}

fn parse_args() -> Args {
    let mut a = Args {
        pob_root: PathBuf::new(),
        input: PathBuf::from("data/corpus/poe2/builds.jsonl"),
        out_dir: PathBuf::from("data/corpus/poe2/"),
        state_dir: PathBuf::from("."),
        workers: 3,
        follow: false,
        idle_min: 10,
        limit: usize::MAX,
        recycle_every: 300,
        timeout_s: 120,
    };
    let v: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < v.len() {
        let next = |i: usize| v.get(i + 1).cloned().unwrap_or_else(|| panic!("{} needs a value", v[i]));
        match v[i].as_str() {
            "--pob-root" => { a.pob_root = next(i).into(); i += 1 }
            "--input" => { a.input = next(i).into(); i += 1 }
            "--out-dir" => { a.out_dir = next(i).into(); i += 1 }
            "--state-dir" => { a.state_dir = next(i).into(); i += 1 }
            "--workers" => { a.workers = next(i).parse().unwrap(); i += 1 }
            "--idle-min" => { a.idle_min = next(i).parse().unwrap(); i += 1 }
            "--limit" => { a.limit = next(i).parse().unwrap(); i += 1 }
            "--recycle-every" => { a.recycle_every = next(i).parse().unwrap(); i += 1 }
            "--timeout" => { a.timeout_s = next(i).parse().unwrap(); i += 1 }
            "--follow" => a.follow = true,
            other => panic!("unknown arg {other}"),
        }
        i += 1;
    }
    assert!(a.pob_root.join("Launch.lua").is_file(), "--pob-root must contain Launch.lua");
    a
}

fn now_s() -> f64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64() }

fn code_hash(code: &str) -> String {
    let h = Sha256::digest(code.trim().as_bytes());
    h.iter().take(16).map(|b| format!("{b:02x}")).collect()
}

struct Job { hash: String, line: usize, code: String, meta: Value }
enum Outcome { Ok(Value), Err(Value) }

fn log(msg: &str) {
    let t = chrono_like();
    eprintln!("[{t}] {msg}");
}
fn chrono_like() -> String {
    let s = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs();
    format!("{:02}:{:02}:{:02}Z", (s / 3600) % 24, (s / 60) % 60, s % 60)
}

// ------------------------------------------------------------------ engine wrapper

struct Worker { id: usize, cfg: EngineConfig, h: EngineHandle, used: usize, timeout: Duration, version: Value }

impl Worker {
    fn boot(id: usize, cfg: EngineConfig, timeout: Duration) -> Worker {
        let h = EngineHandle::spawn(cfg.clone());
        if let Err(e) = h.wait_ready() { panic!("worker {id}: engine failed to boot: {e}"); }
        let version = h.call("version", Value::Null).map(|o| o.result).unwrap_or(Value::Null);
        Worker { id, cfg, h, used: 0, timeout, version }
    }
    fn restart(&mut self, why: &str) {
        log(&format!("worker {}: restarting engine ({why}) after {} builds", self.id, self.used));
        self.h.shutdown();
        *self = Worker::boot(self.id, self.cfg.clone(), self.timeout);
    }
    /// A call with a deadline. A Lua call cannot be interrupted, so on timeout the engine is abandoned.
    fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        let h = self.h.clone();
        let m = method.to_string();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || { let _ = tx.send(h.call(&m, params).map(|o| o.result).map_err(|e| e.to_string())); });
        match rx.recv_timeout(self.timeout) {
            Ok(r) => r,
            Err(_) => Err(format!("TIMEOUT in {method}")),
        }
    }
    fn eval_json(&self, code: &'static str, what: &str) -> Result<Value, String> {
        let h = self.h.clone();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || { let _ = tx.send(h.eval(code).map_err(|e| e.to_string())); });
        let v = match rx.recv_timeout(self.timeout) {
            Ok(r) => r?,
            Err(_) => return Err(format!("TIMEOUT in {what}")),
        };
        match v {
            Value::String(s) => serde_json::from_str(&s).map_err(|e| format!("{what}: bad json: {e}")),
            other => Ok(other),
        }
    }
}

fn evaluate(w: &Worker, job: &Job) -> Result<Value, (String, String)> {
    let t0 = Instant::now();
    let loaded = w.call("load_build_code", json!({"code": job.code.trim(), "name": "corpus"})).map_err(|e| ("load".to_string(), e))?;
    let t_load = t0.elapsed().as_secs_f64() * 1000.0;
    let config = w.eval_json(CONFIG_LUA, "config_snapshot").map_err(|e| ("config_snapshot".into(), e))?;
    let a = w.eval_json(EXTRACT_LUA, "extract").map_err(|e| ("extract_as_exported".into(), e))?;
    w.eval_json(LOCK_LUA, "lock_config").map_err(|e| ("lock_config".into(), e))?;
    w.call("set_config", json!({"var": "enemyIsBoss", "value": "Pinnacle"})).map_err(|e| ("set_config".into(), e))?;
    let b = w.eval_json(EXTRACT_LUA, "extract").map_err(|e| ("extract_pinnacle".into(), e))?;
    let ms = t0.elapsed().as_secs_f64() * 1000.0;

    let mut rec = Map::new();
    rec.insert("hash".into(), json!(job.hash));
    rec.insert("line".into(), json!(job.line));
    rec.insert("meta".into(), job.meta.clone());
    rec.insert("evaluatedAt".into(), json!(now_s()));
    rec.insert("pob".into(), json!({"version": w.version, "treeVersion": a["treeVersion"], "targetVersion": a["targetVersion"], "buildTargetVersion": loaded["targetVersion"]}));
    rec.insert("timingMs".into(), json!({"load": (t_load as i64), "total": (ms as i64)}));
    // Tags: config independent, from the as-exported calc.
    let tag_keys = ["className", "ascendancy", "level", "keystones", "notables", "ascendancyNodes", "allocatedNodeCount",
        "socketGroups", "activationCounts", "manualDamagingSkills", "manualDamagingSkillNames", "oneButton", "itemGrantedManualDamagingSkills", "oneButtonInclItemSkills", "anyManualSpell",
        "noCast", "grantedSkills", "uniques", "items", "unsupportedModLines", "unsupportedModSample"];
    let mut tags = Map::new();
    for k in tag_keys { tags.insert(k.into(), a.get(k).cloned().unwrap_or(Value::Null)); }
    rec.insert("tags".into(), Value::Object(tags));
    rec.insert("playerConfig".into(), config);
    let prof = |v: &Value| json!({"mainSkill": v["mainSkill"], "stats": v["stats"], "minionStats": v["minionStats"]});
    rec.insert("as_exported".into(), prof(&a));
    rec.insert("pinnacle_locked".into(), prof(&b));
    Ok(Value::Object(rec))
}

fn worker_loop(id: usize, args: Args, cfg: EngineConfig, jobs: Arc<Mutex<Receiver<Job>>>, out: Sender<Outcome>) {
    let mut w = Worker::boot(id, cfg, Duration::from_secs(args.timeout_s));
    log(&format!("worker {id}: ready (boot {:?} ms)", w.h.status().boot_ms));
    loop {
        let job = { let rx = jobs.lock().unwrap(); rx.recv() };
        let Ok(job) = job else { break };
        if w.used >= args.recycle_every { w.restart("recycle"); }
        w.used += 1;
        let t0 = Instant::now();
        match evaluate(&w, &job) {
            Ok(rec) => { let _ = out.send(Outcome::Ok(rec)); }
            Err((stage, err)) => {
                let fatal = err.starts_with("TIMEOUT") || err.contains("not running") || err.contains("engine thread");
                let _ = out.send(Outcome::Err(json!({
                    "hash": job.hash, "line": job.line, "stage": stage, "error": err.chars().take(2000).collect::<String>(),
                    "meta": job.meta, "ms": t0.elapsed().as_millis() as u64, "at": now_s(),
                })));
                if fatal { w.restart(&format!("{stage}: {}", err.chars().take(80).collect::<String>())); }
            }
        }
    }
    w.h.shutdown();
}

// ------------------------------------------------------------------ input tailing

fn load_done(paths: &[PathBuf]) -> HashSet<String> {
    let mut s = HashSet::new();
    for p in paths {
        let Ok(f) = File::open(p) else { continue };
        for line in BufReader::new(f).lines().map_while(Result::ok) {
            if let Ok(v) = serde_json::from_str::<Value>(&line) {
                if let Some(h) = v["hash"].as_str() { s.insert(h.to_string()); }
            }
        }
    }
    s
}

/// What the producer says about itself: (fingerprint of progress, finished?).
fn progress_state(progress: &Path, input_len: u64) -> (String, bool) {
    let txt = std::fs::read_to_string(progress).unwrap_or_default();
    let mut done = false;
    if let Ok(v) = serde_json::from_str::<Value>(&txt) {
        let st = v["status"].as_str().or(v["state"].as_str()).unwrap_or("").to_lowercase();
        if v["done"].as_bool() == Some(true) || v["finished"].as_bool() == Some(true) || v["complete"].as_bool() == Some(true)
            || ["done", "finished", "complete", "completed"].contains(&st.as_str()) { done = true; }
        let fetched = v["fetched"].as_f64().or(v["fetched_count"].as_f64()).or(v["count"].as_f64());
        let target = v["target"].as_f64().or(v["target_count"].as_f64());
        if let (Some(f), Some(t)) = (fetched, target) { if t > 0.0 && f >= t { done = true; } }
        let fp = format!("{}|{}", fetched.map(|f| f.to_string()).unwrap_or_default(), input_len);
        return (fp, done);
    }
    (format!("|{input_len}"), done)
}

fn reader(args: Args, mut done: HashSet<String>, tx: SyncSender<Job>, queued: Arc<AtomicUsize>, skipped: Arc<AtomicUsize>, bad: Sender<Outcome>) {
    let progress = args.input.with_file_name("progress.json");
    let mut offset: u64 = 0;
    let mut line_no = 0usize;
    let mut sent = 0usize;
    let mut last_fp = String::new();
    let mut last_change = Instant::now();
    let mut buf = Vec::new();
    loop {
        let len = std::fs::metadata(&args.input).map(|m| m.len()).unwrap_or(0);
        if len > offset {
            if let Ok(mut f) = File::open(&args.input) {
                f.seek(SeekFrom::Start(offset)).ok();
                buf.clear();
                f.take(len - offset).read_to_end(&mut buf).ok();
                // only complete lines
                let end = buf.iter().rposition(|&b| b == b'\n').map(|p| p + 1).unwrap_or(0);
                offset += end as u64;
                for raw in buf[..end].split(|&b| b == b'\n') {
                    if raw.iter().all(|b| b.is_ascii_whitespace()) { continue; }
                    line_no += 1;
                    let v: Value = match serde_json::from_slice(raw) {
                        Ok(v) => v,
                        Err(e) => { let _ = bad.send(Outcome::Err(json!({"hash": null, "line": line_no, "stage": "input_json", "error": e.to_string(), "at": now_s()}))); continue; }
                    };
                    let Some(code) = v["code"].as_str().filter(|c| !c.trim().is_empty()) else {
                        let h = format!("nocode-line{line_no}");
                        if done.insert(h.clone()) {
                            let _ = bad.send(Outcome::Err(json!({"hash": h, "line": line_no, "stage": "input", "error": "no code field", "at": now_s()})));
                        }
                        continue;
                    };
                    let hash = code_hash(code);
                    if !done.insert(hash.clone()) { skipped.fetch_add(1, Ordering::Relaxed); continue; }
                    let mut meta = v.clone();
                    if let Some(o) = meta.as_object_mut() { o.remove("code"); }
                    if sent >= args.limit { return; }
                    sent += 1;
                    queued.fetch_add(1, Ordering::Relaxed);
                    if tx.send(Job { hash, line: line_no, code: code.to_string(), meta }).is_err() { return; }
                }
            }
        }
        if !args.follow { return; }
        let (fp, finished) = progress_state(&progress, len);
        let len_now = std::fs::metadata(&args.input).map(|m| m.len()).unwrap_or(0);
        if finished && len_now <= offset { log("producer reports finished; input drained"); return; }
        if fp != last_fp { last_fp = fp; last_change = Instant::now(); }
        // Producer has not started yet: wait for it rather than counting idle time.
        if len_now == 0 && !progress.exists() { last_change = Instant::now(); }
        if last_change.elapsed() > Duration::from_secs(args.idle_min * 60) && len_now <= offset {
            log(&format!("no producer progress for {} min; stopping follow", args.idle_min));
            return;
        }
        std::thread::sleep(Duration::from_secs(20));
    }
}

// ------------------------------------------------------------------ main

fn main() {
    let args = parse_args();
    std::fs::create_dir_all(&args.out_dir).ok();
    std::fs::create_dir_all(&args.state_dir).ok();
    let ev_path = args.out_dir.join("evaluated.jsonl");
    let er_path = args.out_dir.join("errors.jsonl");
    let done = load_done(&[ev_path.clone(), er_path.clone()]);
    log(&format!("resume: {} hashes already done; input {}; {} workers", done.len(), args.input.display(), args.workers));

    let cfg = EngineConfig {
        pob_root: args.pob_root.clone(),
        user_dir: args.state_dir.join("user"),
        cache_dir: Some(args.state_dir.join("cache")),
    };
    // Warm the compiled-Lua cache with one engine before the others boot in parallel.
    { let h = EngineHandle::spawn(cfg.clone()); h.wait_ready().expect("boot"); h.shutdown(); }

    let (jtx, jrx) = mpsc::sync_channel::<Job>(args.workers * 2);
    let jrx = Arc::new(Mutex::new(jrx));
    let (otx, orx) = mpsc::channel::<Outcome>();
    let queued = Arc::new(AtomicUsize::new(0));
    let skipped = Arc::new(AtomicUsize::new(0));

    let mut threads = Vec::new();
    for id in 0..args.workers {
        let (a, c, r, o) = (args.clone(), cfg.clone(), jrx.clone(), otx.clone());
        threads.push(std::thread::Builder::new().name(format!("w{id}")).spawn(move || worker_loop(id, a, c, r, o)).unwrap());
    }
    {
        let (a, q, s, o) = (args.clone(), queued.clone(), skipped.clone(), otx.clone());
        std::thread::spawn(move || reader(a, done, jtx, q, s, o));
    }
    drop(otx);

    // A run killed mid-write leaves a partial last line: terminate it so the next append starts clean.
    for p in [&ev_path, &er_path] {
        if let Ok(b) = std::fs::read(p) { if !b.is_empty() && *b.last().unwrap() != b'\n' {
            OpenOptions::new().append(true).open(p).unwrap().write_all(b"\n").unwrap();
        } }
    }
    let mut ev = OpenOptions::new().create(true).append(true).open(&ev_path).expect("open evaluated.jsonl");
    let mut er = OpenOptions::new().create(true).append(true).open(&er_path).expect("open errors.jsonl");
    let start = Instant::now();
    let (mut ok, mut err) = (0usize, 0usize);
    let mut last_log = Instant::now();
    let mut window: Vec<Instant> = Vec::new();
    let report = |ok: usize, err: usize, window: &Vec<Instant>| {
        let mins = start.elapsed().as_secs_f64() / 60.0;
        let recent = window.iter().filter(|t| t.elapsed() < Duration::from_secs(300)).count() as f64 / (mins.min(5.0).max(1e-9));
        log(&format!("ok={ok} err={err} queued={} skipped(dup/done)={} | {:.1} builds/min overall, {:.1}/min last 5 min",
            queued.load(Ordering::Relaxed), skipped.load(Ordering::Relaxed), (ok + err) as f64 / mins.max(1e-9), recent));
    };
    loop {
        match orx.recv_timeout(Duration::from_secs(5)) {
            Ok(Outcome::Ok(v)) => { ev.write_all(format!("{v}\n").as_bytes()).unwrap(); ev.flush().ok(); ok += 1; window.push(Instant::now()); }
            Ok(Outcome::Err(v)) => { er.write_all(format!("{v}\n").as_bytes()).unwrap(); er.flush().ok(); err += 1; window.push(Instant::now()); }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }
        if last_log.elapsed() > Duration::from_secs(60) {
            window.retain(|t| t.elapsed() < Duration::from_secs(300));
            report(ok, err, &window); last_log = Instant::now();
        }
    }
    report(ok, err, &window);
    log("done");
    for t in threads { let _ = t.join(); }
}
