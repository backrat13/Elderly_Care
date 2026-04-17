// ══════════════════════════════════════════════════════════════════
// Macro so we can early-return DispatchResult::Continue when arg missing
macro_rules! req {
    ($args:expr, $idx:expr, $usage:expr) => {
        match $args.get($idx) {
            Some(s) => s.as_str(),
            None => {
                println!("  usage: {}", $usage);
                return DispatchResult::Continue;
            }
        }
    };
}
//  cli.rs — Interactive REPL powered by rustyline
// ══════════════════════════════════════════════════════════════════

use crate::engine::{AgentMemoryEngine, AgentProxy};
use crate::models::DollValue;
use rustyline::error::ReadlineError;
use rustyline::{DefaultEditor, Result as RlResult};
use std::sync::Arc;

const HELP_TEXT: &str = r#"
╔══════════════════════════════════════════════════════════════════╗
║              AgentMemoryEngine RS — CLI Commands                ║
╠══════════════════════════════════════════════════════════════════╣
║  AGENT   <name>                  switch active agent namespace  ║
║  AGENTS                          list all agent namespaces      ║
║                                                                  ║
║  SET     <key> <value> [ttl=N]   store a string value           ║
║  GET     <key>                   retrieve a value               ║
║  DEL     <key>                   delete a key                   ║
║  EXISTS  <key>                   check if key exists            ║
║  KEYS    [pattern]               list keys (glob ok)            ║
║  TTL     <key>                   remaining TTL in seconds       ║
║  EXPIRE  <key> <secs>            set TTL on existing key        ║
║                                                                  ║
║  HSET    <key> <field> <value>   set hash field                 ║
║  HGET    <key> <field>           get hash field                 ║
║  HGETALL <key>                   get full hash                  ║
║                                                                  ║
║  LPUSH   <key> <value>           push to list head              ║
║  RPUSH   <key> <value>           push to list tail              ║
║  LPOP    <key>                   pop from list head             ║
║  RPOP    <key>                   pop from list tail             ║
║  LRANGE  <key> [start] [stop]    list slice                     ║
║  LLEN    <key>                   list length                    ║
║                                                                  ║
║  SADD    <key> <member>          add to set                     ║
║  SMEMBERS <key>                  all set members                ║
║  SISMEMBER <key> <member>        test membership                ║
║                                                                  ║
║  ZADD    <key> <member> <score>  add to sorted set              ║
║  ZRANGE  <key> [start] [stop]    sorted range                   ║
║  ZSCORE  <key> <member>          get score                      ║
║                                                                  ║
║  INCR    <key> [by]              increment counter              ║
║  DECR    <key> [by]              decrement counter              ║
║                                                                  ║
║  🪆 DOLL <key> <path> <value>    set nested doll path           ║
║  🪆 DOLLGET <key> <path>         get value at doll path         ║
║  🪆 DOLLSHOW <key>               pretty-print entire doll       ║
║                                                                  ║
║  TAG     <tag1> [tag2...]        find keys by tag               ║
║  IDENTITY [agent]                show identity report           ║
║  STATS                           store statistics               ║
║  SNAPSHOT                        force RDB snapshot             ║
║                                                                  ║
║  PUBLISH <channel> <message>     pub/sub publish                ║
║                                                                  ║
║  🛜 RUVIEW                       show live body map             ║
║  🛜 RUZONE <zone_name>           zone detail (e.g. chest)       ║
║  🛜 RUSTATS                      ruview scan stats              ║
║                                                                  ║
║  HELP                            this message                   ║
║  EXIT / QUIT                     save & exit                    ║
╚══════════════════════════════════════════════════════════════════╝
"#;

pub struct Cli {
    engine: Arc<AgentMemoryEngine>,
    current_agent: String,
}

impl Cli {
    pub fn new(engine: Arc<AgentMemoryEngine>) -> Self {
        Self {
            engine,
            current_agent: "global".to_string(),
        }
    }

    pub fn run(&mut self) -> RlResult<()> {
        let mut rl = DefaultEditor::new()?;

        println!("\n{}", "═".repeat(62));
        println!("  🧠 AgentMemoryEngine RS  |  type HELP, EXIT to quit");
        println!("  🪆 Nesting Doll Memory   |  🛜 RuView body mapper");
        println!("{}\n", "═".repeat(62));

        loop {
            let prompt = format!("[{}]> ", self.current_agent);
            match rl.readline(&prompt) {
                Ok(line) => {
                    let line = line.trim().to_string();
                    if line.is_empty() {
                        continue;
                    }
                    let _ = rl.add_history_entry(&line);
                    match self.dispatch(&line) {
                        DispatchResult::Exit => {
                            self.engine.shutdown();
                            break;
                        }
                        DispatchResult::Continue => {}
                    }
                }
                Err(ReadlineError::Interrupted) | Err(ReadlineError::Eof) => {
                    self.engine.shutdown();
                    break;
                }
                Err(e) => {
                    eprintln!("  (readline error) {e}");
                    break;
                }
            }
        }
        Ok(())
    }

    fn agent(&self) -> AgentProxy {
        self.engine.agent(&self.current_agent)
    }

    fn dispatch(&mut self, raw: &str) -> DispatchResult {
        // Split preserving quoted strings
        let parts = split_args(raw);
        if parts.is_empty() {
            return DispatchResult::Continue;
        }

        let cmd = parts[0].to_uppercase();
        let args = &parts[1..];
        let a = self.agent();

        match cmd.as_str() {
            // ── Meta ─────────────────────────────────────────────
            "HELP" => println!("{HELP_TEXT}"),

            "EXIT" | "QUIT" => return DispatchResult::Exit,

            "AGENT" => {
                if let Some(name) = args.first() {
                    self.current_agent = name.to_string();
                    println!("  → switched to agent '{}'", self.current_agent);
                } else {
                    println!("  usage: AGENT <name>");
                }
            }

            "AGENTS" => {
                let agents = self.engine.namespaces();
                if agents.is_empty() {
                    println!("  (none)");
                } else {
                    for ag in &agents {
                        println!("  • {ag}");
                    }
                }
            }

            // ── String ───────────────────────────────────────────
            "SET" => {
                if args.len() < 2 {
                    println!("  usage: SET <key> <value> [ttl=N]");
                    return DispatchResult::Continue;
                }
                let key = &args[0];
                let val = &args[1];
                let ttl = args.iter().find(|a| a.starts_with("ttl=")).and_then(|s| {
                    s.strip_prefix("ttl=").and_then(|v| v.parse::<f64>().ok())
                });
                a.set(key, val.clone(), ttl, None, 1.0);
                println!("  OK");
            }

            "GET" => {
                let key = req!(args, 0, "GET <key>");
                match a.get(key) {
                    Some(v) => println!("  {}", display_doll(&v)),
                    None => println!("  (nil)"),
                }
            }

            "DEL" => {
                let key = req!(args, 0, "DEL <key>");
                println!("  {}", if a.delete(key) { "deleted" } else { "not found" });
            }

            "EXISTS" => {
                let key = req!(args, 0, "EXISTS <key>");
                println!("  {}", if a.exists(key) { "yes" } else { "no" });
            }

            "KEYS" => {
                let pattern = args.first().map(|s| s.as_str()).unwrap_or("*");
                let mut ks = a.keys(pattern);
                ks.sort();
                for k in &ks {
                    println!("  {k}");
                }
                println!("  ({} key{})", ks.len(), if ks.len() == 1 { "" } else { "s" });
            }

            "TTL" => {
                let key = req!(args, 0, "TTL <key>");
                let t = a.ttl(key);
                if t == -2.0 {
                    println!("  (key does not exist)");
                } else if t == -1.0 {
                    println!("  (no expiry)");
                } else {
                    println!("  {t:.2}s remaining");
                }
            }

            "EXPIRE" => {
                if args.len() < 2 {
                    println!("  usage: EXPIRE <key> <secs>");
                    return DispatchResult::Continue;
                }
                let key = &args[0];
                match args[1].parse::<f64>() {
                    Ok(ttl) => println!(
                        "  {}",
                        if a.expire(key, ttl) { "OK" } else { "key not found" }
                    ),
                    Err(_) => println!("  (error) TTL must be a number"),
                }
            }

            // ── Hash ─────────────────────────────────────────────
            "HSET" => {
                if args.len() < 3 {
                    println!("  usage: HSET <key> <field> <value>");
                    return DispatchResult::Continue;
                }
                a.hset(&args[0], &args[1], args[2].clone());
                println!("  OK");
            }

            "HGET" => {
                if args.len() < 2 {
                    println!("  usage: HGET <key> <field>");
                    return DispatchResult::Continue;
                }
                match a.hget(&args[0], &args[1]) {
                    Some(v) => println!("  {}", display_doll(&v)),
                    None => println!("  (nil)"),
                }
            }

            "HGETALL" => {
                let key = req!(args, 0, "HGETALL <key>");
                let map = a.hgetall(key);
                if map.is_empty() {
                    println!("  (empty hash)");
                } else {
                    let mut entries: Vec<_> = map.iter().collect();
                    entries.sort_by_key(|(k, _)| k.as_str());
                    for (field, val) in entries {
                        println!("  {field}: {}", display_doll(val));
                    }
                }
            }

            // ── List ─────────────────────────────────────────────
            "LPUSH" => {
                if args.len() < 2 { println!("  usage: LPUSH <key> <value>"); return DispatchResult::Continue; }
                let n = a.lpush(&args[0], args[1].clone());
                println!("  list length: {n}");
            }
            "RPUSH" => {
                if args.len() < 2 { println!("  usage: RPUSH <key> <value>"); return DispatchResult::Continue; }
                let n = a.rpush(&args[0], args[1].clone());
                println!("  list length: {n}");
            }
            "LPOP" => {
                let key = req!(args, 0, "LPOP <key>");
                match a.lpop(key) {
                    Some(v) => println!("  {}", display_doll(&v)),
                    None => println!("  (nil)"),
                }
            }
            "RPOP" => {
                let key = req!(args, 0, "RPOP <key>");
                match a.rpop(key) {
                    Some(v) => println!("  {}", display_doll(&v)),
                    None => println!("  (nil)"),
                }
            }
            "LRANGE" => {
                let key = req!(args, 0, "LRANGE <key> [start] [stop]");
                let start: i64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
                let stop: i64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(-1);
                let items = a.lrange(key, start, stop);
                for (i, v) in items.iter().enumerate() {
                    println!("  [{i}] {}", display_doll(v));
                }
                if items.is_empty() { println!("  (empty)"); }
            }
            "LLEN" => {
                let key = req!(args, 0, "LLEN <key>");
                println!("  {}", a.llen(key));
            }

            // ── Set ───────────────────────────────────────────────
            "SADD" => {
                if args.len() < 2 { println!("  usage: SADD <key> <member>"); return DispatchResult::Continue; }
                let added = if a.sadd(&args[0], args[1].clone()) { 1 } else { 0 };
                println!("  added: {added}");
            }
            "SMEMBERS" => {
                let key = req!(args, 0, "SMEMBERS <key>");
                let mut members: Vec<String> = a.smembers(key).into_iter().collect();
                members.sort();
                for m in &members { println!("  {m}"); }
                println!("  ({} members)", members.len());
            }
            "SISMEMBER" => {
                if args.len() < 2 { println!("  usage: SISMEMBER <key> <member>"); return DispatchResult::Continue; }
                println!("  {}", if a.sismember(&args[0], &args[1]) { "yes" } else { "no" });
            }

            // ── Sorted Set ────────────────────────────────────────
            "ZADD" => {
                if args.len() < 3 { println!("  usage: ZADD <key> <member> <score>"); return DispatchResult::Continue; }
                match args[2].parse::<f64>() {
                    Ok(score) => { a.zadd(&args[0], args[1].clone(), score); println!("  OK"); }
                    Err(_) => println!("  (error) score must be a number"),
                }
            }
            "ZRANGE" => {
                let key = req!(args, 0, "ZRANGE <key> [start] [stop]");
                let start: i64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
                let stop: i64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(-1);
                for (member, score) in a.zrange(key, start, stop) {
                    println!("  {score:>8.2}  {member}");
                }
            }
            "ZSCORE" => {
                if args.len() < 2 { println!("  usage: ZSCORE <key> <member>"); return DispatchResult::Continue; }
                match a.zscore(&args[0], &args[1]) {
                    Some(s) => println!("  {s}"),
                    None => println!("  (nil)"),
                }
            }

            // ── Counter ───────────────────────────────────────────
            "INCR" => {
                let key = req!(args, 0, "INCR <key> [by]");
                let by: f64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(1.0);
                println!("  {}", a.incr(key, by));
            }
            "DECR" => {
                let key = req!(args, 0, "DECR <key> [by]");
                let by: f64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(1.0);
                println!("  {}", a.decr(key, by));
            }

            // ── 🪆 Nesting Doll ──────────────────────────────────
            "DOLL" => {
                if args.len() < 3 {
                    println!("  usage: DOLL <key> <dot.path> <value>");
                    return DispatchResult::Continue;
                }
                let val = DollValue::String(args[2..].join(" "));
                if a.doll_set(&args[0], &args[1], val) {
                    println!("  🪆 OK");
                } else {
                    println!("  (error) failed to set doll path");
                }
            }

            "DOLLGET" => {
                if args.len() < 2 {
                    println!("  usage: DOLLGET <key> <dot.path>");
                    return DispatchResult::Continue;
                }
                match a.doll_get(&args[0], &args[1]) {
                    Some(v) => println!("  🪆 {}", display_doll(&v)),
                    None => println!("  🪆 (nil)"),
                }
            }

            "DOLLSHOW" => {
                let key = req!(args, 0, "DOLLSHOW <key>");
                match a.get(key) {
                    Some(v) => println!("{}", v.pretty_print(2)),
                    None => println!("  (nil)"),
                }
            }

            // ── Tags ──────────────────────────────────────────────
            "TAG" => {
                if args.is_empty() {
                    println!("  usage: TAG <tag1> [tag2...]");
                    return DispatchResult::Continue;
                }
                let tag_refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
                let mut keys = a.by_tag(&tag_refs, false);
                keys.sort();
                for k in &keys { println!("  {k}"); }
                println!("  ({} keys)", keys.len());
            }

            // ── Identity ──────────────────────────────────────────
            "IDENTITY" => {
                let target = args.first().map(|s| s.as_str()).unwrap_or(&self.current_agent);
                println!("{}", self.engine.identity_report(target));
            }

            // ── Stats ─────────────────────────────────────────────
            "STATS" => {
                let s = self.engine.stats();
                println!("  version:    {}", s.version);
                println!("  namespaces: {}", s.namespace_count);
                println!("  live keys:  {}", s.total_live_keys);
                println!("  pub/sub ch: {}", s.pubsub_channels);
                let snap_str = if s.last_snapshot == 0.0 {
                    "never".to_string()
                } else {
                    crate::identity::format_ts(s.last_snapshot)
                };
                println!("  last snap:  {snap_str}");
                println!();
                for ns in &s.namespaces {
                    println!(
                        "  [{:<20}]  {} live keys  {:?}",
                        ns.namespace, ns.live_keys, ns.by_type
                    );
                }
            }

            // ── Snapshot ──────────────────────────────────────────
            "SNAPSHOT" => {
                match self.engine.snapshot() {
                    Ok(sz) => println!(
                        "  ✓  {sz:>8} bytes → {}",
                        self.engine.config.rdb_path
                    ),
                    Err(e) => println!("  (error) {e}"),
                }
            }

            // ── Pub/Sub ───────────────────────────────────────────
            "PUBLISH" => {
                if args.len() < 2 { println!("  usage: PUBLISH <channel> <message>"); return DispatchResult::Continue; }
                let channel = &args[0];
                let msg = args[1..].join(" ");
                let n = a.publish(channel, &msg);
                println!("  delivered to {n} subscriber(s)");
            }

            // ── 🛜 RuView ─────────────────────────────────────────
            "RUVIEW" => {
                match &self.engine.ruview {
                    Some(rv) => println!("{}", rv.get_body_map_ascii()),
                    None => println!("  RuView is disabled (start with --ruview)"),
                }
            }

            "RUZONE" => {
                let zone = req!(args, 0, "RUZONE <zone_name>");
                match &self.engine.ruview {
                    Some(rv) => {
                        let data = rv.get_zone_data(zone);
                        if data.is_empty() {
                            println!("  Zone '{zone}' not found or not yet scanned.");
                            println!("  Available zones: skull, neck, chest, abdomen, pelvis,");
                            println!("    left_shoulder, right_shoulder, left_arm, right_arm,");
                            println!("    left_forearm, right_forearm, left_thigh, right_thigh,");
                            println!("    left_knee, right_knee, left_shin, right_shin,");
                            println!("    left_foot, right_foot");
                        } else {
                            println!("  🛜 Zone: {zone}");
                            let mut entries: Vec<_> = data.iter().collect();
                            entries.sort_by_key(|(k, _)| k.as_str());
                            for (field, val) in entries {
                                println!("    {field:<20}  {val:.4}");
                            }
                        }
                    }
                    None => println!("  RuView is disabled"),
                }
            }

            "RUSTATS" => {
                match &self.engine.ruview {
                    Some(rv) => println!("{}", rv.get_stats()),
                    None => println!("  RuView is disabled"),
                }
            }

            // ── Unknown ───────────────────────────────────────────
            _ => println!("  unknown command '{cmd}' — type HELP"),
        }

        DispatchResult::Continue
    }
}

// ── Helper types ──────────────────────────────────────────────────

enum DispatchResult {
    Continue,
    Exit,
}



fn display_doll(v: &DollValue) -> String {
    match v {
        DollValue::String(s) => format!("\"{}\"", s),
        DollValue::Float(f) => format!("{f}"),
        DollValue::List(l) => format!("[list, {} items]", l.len()),
        DollValue::Set(s) => format!("{{set, {} members}}", s.len()),
        DollValue::SortedSet(m) => format!("(sortedset, {} entries)", m.len()),
        DollValue::Hash(h) => format!("{{hash, {} fields}}", h.len()),
        DollValue::Doll(d) => format!("🪆 doll({} keys)", d.len()),
    }
}

/// Split a command line respecting double-quoted strings.
fn split_args(raw: &str) -> Vec<String> {
    let mut args = Vec::new();
    let mut current = String::new();
    let mut in_quotes = false;
    let mut chars = raw.chars().peekable();

    while let Some(c) = chars.next() {
        match c {
            '"' => in_quotes = !in_quotes,
            ' ' | '\t' if !in_quotes => {
                if !current.is_empty() {
                    args.push(current.clone());
                    current.clear();
                }
            }
            _ => current.push(c),
        }
    }
    if !current.is_empty() {
        args.push(current);
    }
    args
}
