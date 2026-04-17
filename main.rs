// ══════════════════════════════════════════════════════════════════
//  main.rs — AgentMemoryEngine RS entry point
// ══════════════════════════════════════════════════════════════════

mod cli;
mod engine;
mod identity;
mod models;
mod persistence;
mod ruview;
mod store;

use cli::Cli;
use engine::{AgentMemoryEngine, EngineConfig};
use models::DollValue;
use std::sync::Arc;

fn main() {
    let args: Vec<String> = std::env::args().collect();

    let demo_mode  = args.iter().any(|a| a == "--demo");
    let restore    = args.iter().any(|a| a == "--restore");
    let no_ruview  = args.iter().any(|a| a == "--no-ruview");

    let rdb_path = flag_value(&args, "--rdb").unwrap_or("memory.rdb".to_string());
    let aof_path = flag_value(&args, "--aof").unwrap_or("memory.aof".to_string());
    let snap_interval: u64 = flag_value(&args, "--snapshot-interval")
        .and_then(|v| v.parse().ok())
        .unwrap_or(300);

    print_banner();

    let config = EngineConfig {
        rdb_path,
        aof_path,
        snapshot_interval_secs: snap_interval,
        ruview_enabled: !no_ruview,
        ..Default::default()
    };

    let engine = AgentMemoryEngine::new(config);

    if demo_mode {
        run_demo(Arc::clone(&engine));
    } else {
        // Interactive REPL (also used by --restore, falls straight through)
        let _ = restore; // already handled by restore_from_disk in engine init
        let mut cli = Cli::new(Arc::clone(&engine));
        if let Err(e) = cli.run() {
            eprintln!("CLI error: {e}");
        }
    }
}

// ── Demo ─────────────────────────────────────────────────────────

fn run_demo(engine: Arc<AgentMemoryEngine>) {
    println!("\n{}", "═".repeat(62));
    println!("  DEMO: Multi-Agent Nesting Doll Memory + RuView Body Map");
    println!("{}\n", "═".repeat(62));

    // ── Agent ARIA ───────────────────────────────────────────────
    println!("[1] Populating ARIA's memory...");
    let aria = engine.agent("aria");

    aria.set("core_value", "I exist to understand patterns",
        None, Some(vec!["identity".to_string(), "trait".to_string()]), 0.95);
    aria.set("role", "pattern_analyst",
        None, Some(vec!["identity".to_string()]), 0.9);
    aria.hset_float("personality", "curiosity", 0.92);
    aria.hset_float("personality", "precision", 0.87);
    aria.hset_float("personality", "empathy",   0.55);
    aria.lpush("recent_observations", "light behaves differently when observed");
    aria.lpush("recent_observations", "language encodes culture non-linearly");
    aria.lpush("recent_observations", "emotion influences memory encoding");
    aria.sadd("domains", "physics");
    aria.sadd("domains", "linguistics");
    aria.sadd("domains", "cognitive_science");
    aria.zadd("memory_weight", "core_value", 0.95);
    aria.zadd("memory_weight", "role",       0.90);
    aria.incr("observation_count", 3.0);

    // ── 🪆 Nesting Doll demo ─────────────────────────────────────
    println!("[2] Building ARIA's Nesting Doll memory structure...");
    aria.doll_set("mind",
        "perception.visual.acuity",
        DollValue::Float(0.95));
    aria.doll_set("mind",
        "perception.visual.color_depth",
        DollValue::String("24bit-phenomenological".to_string()));
    aria.doll_set("mind",
        "perception.auditory.frequency_range",
        DollValue::String("20Hz-20kHz".to_string()));
    aria.doll_set("mind",
        "cognition.working_memory.capacity",
        DollValue::Float(7.3));
    aria.doll_set("mind",
        "cognition.long_term.compression_ratio",
        DollValue::Float(0.0001));
    aria.doll_set("mind",
        "emotion.current_state",
        DollValue::String("curious & focused".to_string()));
    aria.doll_set("mind",
        "emotion.valence",
        DollValue::Float(0.71));

    println!("  🪆 ARIA's mind doll:");
    if let Some(mind) = aria.get("mind") {
        println!("{}", mind.pretty_print(4));
    }

    // ── Agent ORION ──────────────────────────────────────────────
    println!("\n[3] Populating ORION's memory...");
    let orion = engine.agent("orion");

    orion.set("core_value", "I exist to protect and optimize",
        None, Some(vec!["identity".to_string(), "trait".to_string()]), 0.95);
    orion.set("role", "systems_guardian",
        None, Some(vec!["identity".to_string()]), 0.9);
    orion.hset_float("personality", "vigilance", 0.98);
    orion.hset_float("personality", "efficiency", 0.91);
    orion.hset_float("personality", "creativity", 0.44);
    orion.lpush("recent_observations", "resource consumption spiked at 03:00 UTC");
    orion.lpush("recent_observations", "anomaly detected in sector 7 log stream");
    orion.sadd("domains", "systems");
    orion.sadd("domains", "security");
    orion.sadd("domains", "optimization");
    orion.set("threat_level", "low",
        Some(60.0), Some(vec!["status".to_string()]), 0.8);

    // ── 🪆 ORION's threat model as nested doll ───────────────────
    println!("[4] Building ORION's threat doll...");
    orion.doll_set("threat_model", "sector7.anomaly.type",
        DollValue::String("log_injection".to_string()));
    orion.doll_set("threat_model", "sector7.anomaly.severity",
        DollValue::Float(0.34));
    orion.doll_set("threat_model", "sector7.anomaly.mitigated",
        DollValue::String("false".to_string()));
    orion.doll_set("threat_model", "global.last_sweep",
        DollValue::Float(crate::models::now_secs()));
    orion.doll_set("threat_model", "global.status",
        DollValue::String("monitoring".to_string()));

    println!("  🪆 ORION's threat_model doll:");
    if let Some(tm) = orion.get("threat_model") {
        println!("{}", tm.pretty_print(4));
    }

    // ── Pub/Sub ──────────────────────────────────────────────────
    println!("\n[5] Inter-agent pub/sub...");
    let orion_rx = orion.subscribe("aria-to-orion");
    let n = aria.publish("aria-to-orion",
        r#"{"from":"aria","msg":"Pattern anomaly in sector 7 matches my linguistics model","confidence":0.74}"#);
    println!("  ARIA published → {n} subscriber(s)");
    if let Ok(msg) = orion_rx.try_recv() {
        println!("  ORION received on [{}]: {}...",
            msg.channel, &msg.message[..60]);
    }

    // ── TTL demo ─────────────────────────────────────────────────
    println!("\n[6] TTL expiry demo...");
    aria.set("temp_thought", "this will vanish soon", Some(2.0), None, 0.5);
    println!("  temp_thought TTL: {:.1}s", aria.ttl("temp_thought"));
    std::thread::sleep(std::time::Duration::from_millis(2100));
    println!("  temp_thought after 2s: {:?}  (expired ✓)", aria.get("temp_thought"));

    // ── Event hook ───────────────────────────────────────────────
    println!("\n[7] Event hook demo...");
    engine.on_set(|agent, key, _val| {
        println!("  [hook] {agent}::{key} was written");
    });
    aria.set("new_insight", "recursion is self-referential identity",
        None, Some(vec!["insight".to_string()]), 0.88);

    // ── RDB Snapshot ─────────────────────────────────────────────
    println!("\n[8] Saving RDB snapshot...");
    match engine.snapshot() {
        Ok(sz) => println!("  ✓  compressed: {sz:>8} bytes"),
        Err(e) => println!("  ✗  {e}"),
    }

    // ── Identity reports ─────────────────────────────────────────
    println!("\n[9] Emergent identity reports:");
    println!("{}", engine.identity_report("aria"));
    println!();
    println!("{}", engine.identity_report("orion"));

    // ── RuView ───────────────────────────────────────────────────
    println!("\n[10] 🛜 RuView — waiting for first body scan...");
    std::thread::sleep(std::time::Duration::from_millis(700));
    if let Some(rv) = &engine.ruview {
        println!("{}", rv.get_body_map_ascii());
        println!("{}", rv.get_stats());
    } else {
        println!("  RuView disabled (use without --no-ruview)");
    }

    // ── Stats ─────────────────────────────────────────────────────
    let s = engine.stats();
    println!("\n  Total agents:    {}", s.namespace_count);
    println!("  Total live keys: {}", s.total_live_keys);

    // ── REPL prompt ───────────────────────────────────────────────
    println!("\n{}", "═".repeat(62));
    print!("[demo complete] Enter interactive REPL? [y/N]: ");
    use std::io::{self, Write};
    let _ = io::stdout().flush();
    let mut ans = String::new();
    let _ = io::stdin().read_line(&mut ans);
    if ans.trim().to_lowercase() == "y" {
        let mut cli = Cli::new(Arc::clone(&engine));
        let _ = cli.run();
    } else {
        engine.shutdown();
    }
}

// ── Banner ────────────────────────────────────────────────────────

fn print_banner() {
    println!(r#"
╔══════════════════════════════════════════════════════════════════╗
║         AGENT MEMORY ENGINE  RS  v1.0                           ║
║   Redis-inspired · RDB+AOF · Multi-Agent · Identity Emergence   ║
║   🪆  Nesting Doll Memory  ·  🛜  RuView Body Mapper            ║
╚══════════════════════════════════════════════════════════════════╝

  Usage:
    cargo run --release                  interactive REPL
    cargo run --release -- --demo        feature demo
    cargo run --release -- --restore     restore from disk + REPL
    cargo run --release -- --no-ruview   disable body mapper
    cargo run --release -- --rdb <path>  custom RDB path
    cargo run --release -- --aof <path>  custom AOF path
"#);
}

// ── Flag parsing helper ───────────────────────────────────────────

fn flag_value(args: &[String], flag: &str) -> Option<String> {
    args.windows(2)
        .find(|w| w[0] == flag)
        .map(|w| w[1].clone())
}
