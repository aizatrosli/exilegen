// Builds a few synthetic PoE2 builds (skill mixes) and prints JSONL with their codes.
use pob_engine::{EngineConfig, EngineHandle};
use serde_json::{json, Value};
fn main() {
    let a: Vec<String> = std::env::args().collect();
    let cfg = EngineConfig { pob_root: a[1].clone().into(), user_dir: format!("{}/user", a[2]).into(), cache_dir: Some(format!("{}/cache", a[2]).into()) };
    let e = EngineHandle::spawn(cfg); e.wait_ready().unwrap();
    let c = |m: &str, p: Value| -> Value { match e.call(m, p.clone()) { Ok(o) => o.result, Err(err) => { eprintln!("ERR {m} {p}: {err}"); Value::Null } } };
    let sets: Vec<(usize, Vec<Vec<&str>>)> = vec![
        (3, vec![vec!["Cast on Critical", "Fireball", "Frostbolt"], vec!["Spark"], vec!["Herald of Ice"]]),
        (4, vec![vec!["Raise Zombie"], vec!["Skeletal Warrior"], vec!["Contagion"]]),
        (1, vec![vec!["Artillery Ballista"], vec!["Explosive Grenade"], vec!["Herald of Ash"]]),
    ];
    let classes = c("list_classes", Value::Null);
    for (n, (ci, groups)) in sets.into_iter().enumerate() {
        c("new_build", json!({"name": format!("t{n}")}));
        c("set_level", json!({"level": 80}));
        let cls = &classes["classes"][ci];
        c("select_class", json!({"classId": cls["id"], "ascendClassId": cls["ascendancies"][0]["id"]}));
        for (gi, g) in groups.iter().enumerate() {
            c("add_socket_group", json!({}));
            for name in g {
                let gems = c("list_gems", json!({"query": name, "onlySupports": false, "maxLevel": 0}));
                let hit = gems["gems"].as_array().and_then(|v| v.iter().find(|x| x["name"].as_str() == Some(name)).or(v.first()).cloned()).unwrap_or(Value::Null);
                let id = hit["id"].as_str().or(hit["gemId"].as_str()).unwrap_or("").to_string();
                c("add_gem", json!({"groupIndex": gi + 1, "gemId": id}));
            }
        }
        let code = c("save_build_code", Value::Null)["code"].as_str().unwrap_or("").to_string();
        println!("{}", json!({"code": code, "id": format!("synthetic{n}")}));
    }
}
