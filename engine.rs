// ══════════════════════════════════════════════════════════════════
//  engine.rs — The AgentMemoryEngine orchestrator
// ══════════════════════════════════════════════════════════════════

use crate::identity::IdentityVector;
use crate::models::{now_secs, DollValue, MemoryEntry};
use crate::persistence::{AofRecord, AofWriter, RdbSnapshot};
use crate::ruview::RuView;
use crate::store::{NamespaceStore, PubSubBus};
use parking_lot::RwLock;
use std::collections::HashMap;
use std::sync::Arc;

pub const VERSION: &str = "1.0.0";

// ── Config ────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct EngineConfig {
    pub rdb_path: String,
    pub aof_path: String,
    pub snapshot_interval_secs: u64,
    pub aof_fsync: String,
    pub identity_decay_rate: f64,
    pub identity_min_weight: f64,
    pub ttl_check_interval_ms: u64,
    pub ruview_scan_interval_ms: u64,
    pub ruview_enabled: bool,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            rdb_path: "memory.rdb".to_string(),
            aof_path: "memory.aof".to_string(),
            snapshot_interval_secs: 300,
            aof_fsync: "everysec".to_string(),
            identity_decay_rate: 0.97,
            identity_min_weight: 0.01,
            ttl_check_interval_ms: 1000,
            ruview_scan_interval_ms: 500,
            ruview_enabled: true,
        }
    }
}

// ── Event Hook type ───────────────────────────────────────────────

pub type HookFn = Box<dyn Fn(&str, &str, &DollValue) + Send + Sync>;

// ── AgentMemoryEngine ─────────────────────────────────────────────

pub struct AgentMemoryEngine {
    pub config: EngineConfig,
    namespaces: RwLock<HashMap<String, Arc<NamespaceStore>>>,
    identities: RwLock<HashMap<String, IdentityVector>>,
    pub pubsub: PubSubBus,
    on_set_hooks: RwLock<Vec<HookFn>>,
    aof: Option<AofWriter>,
    last_snapshot: RwLock<f64>,
    pub ruview: Option<Arc<RuView>>,
}

impl AgentMemoryEngine {
    pub fn new(config: EngineConfig) -> Arc<Self> {
        let ruview = if config.ruview_enabled {
            Some(Arc::new(RuView::new(config.ruview_scan_interval_ms)))
        } else {
            None
        };

        let engine = Arc::new(Self {
            config: config.clone(),
            namespaces: RwLock::new(HashMap::new()),
            identities: RwLock::new(HashMap::new()),
            pubsub: PubSubBus::new(),
            on_set_hooks: RwLock::new(Vec::new()),
            aof: AofWriter::open(&config.aof_path, &config.aof_fsync).ok(),
            last_snapshot: RwLock::new(0.0),
            ruview: ruview.clone(),
        });

        // Restore from disk
        engine.restore_from_disk();

        // Start background threads
        engine.start_background_threads();

        // Start RuView daemon
        if let Some(rv) = &ruview {
            Arc::clone(rv).spawn_daemon();
        }

        println!(
            "[AgentMemoryEngine v{VERSION}] Online ✓  rdb={} aof={}  ruview={}",
            config.rdb_path, config.aof_path, config.ruview_enabled
        );

        engine
    }

    // ── Agent / Namespace access ──────────────────────────────────

    pub fn agent(self: &Arc<Self>, name: &str) -> AgentProxy {
        self.ensure_namespace(name);
        AgentProxy {
            name: name.to_string(),
            engine: Arc::clone(self),
        }
    }

    fn ensure_namespace(&self, name: &str) {
        let mut ns = self.namespaces.write();
        if !ns.contains_key(name) {
            ns.insert(name.to_string(), Arc::new(NamespaceStore::new(name)));
            self.identities
                .write()
                .insert(name.to_string(), IdentityVector::new(name));
        }
    }

    pub fn ns(&self, name: &str) -> Arc<NamespaceStore> {
        self.ensure_namespace(name);
        Arc::clone(self.namespaces.read().get(name).unwrap())
    }

    pub fn namespaces(&self) -> Vec<String> {
        self.namespaces.read().keys().cloned().collect()
    }

    // ── Snapshot ──────────────────────────────────────────────────

    pub fn snapshot(&self) -> std::io::Result<usize> {
        let ns = self.namespaces.read();
        let ids = self.identities.read();
        let size = RdbSnapshot::save(&self.config.rdb_path, &ns, &ids)?;
        *self.last_snapshot.write() = now_secs();

        // Decay identity weights
        let decay = self.config.identity_decay_rate;
        let min_w = self.config.identity_min_weight;
        drop(ids);
        let mut ids_w = self.identities.write();
        for iv in ids_w.values_mut() {
            iv.decay(decay, min_w);
        }

        Ok(size)
    }

    // ── AOF logging ───────────────────────────────────────────────

    pub(crate) fn aof_log(&self, record: AofRecord) {
        if let Some(aof) = &self.aof {
            aof.log(record);
        }
    }

    // ── Identity absorb ───────────────────────────────────────────

    pub(crate) fn absorb(&self, agent_name: &str, key: &str, value: &DollValue, importance: f64) {
        let mut ids = self.identities.write();
        if let Some(iv) = ids.get_mut(agent_name) {
            iv.absorb(key, value, importance);
        }
    }

    pub(crate) fn read_tick(&self, agent_name: &str) {
        let mut ids = self.identities.write();
        if let Some(iv) = ids.get_mut(agent_name) {
            iv.total_reads += 1;
        }
    }

    // ── Restore ───────────────────────────────────────────────────

    fn restore_from_disk(&self) {
        let rdb_ts;
        match RdbSnapshot::load(&self.config.rdb_path) {
            Ok(Some(payload)) => {
                rdb_ts = payload.saved_at;
                let (ns, ids) = RdbSnapshot::restore(payload);
                let total_keys: usize = ns.values().map(|s| s.stats().live_keys).sum();
                *self.namespaces.write() = ns;
                *self.identities.write() = ids;
                let restored_ts = crate::identity::format_ts(rdb_ts);
                println!("  [restore] RDB loaded: {total_keys} keys (saved at {restored_ts})");
            }
            Ok(None) => {
                println!("  [restore] No RDB found — starting fresh");
                rdb_ts = 0.0;
            }
            Err(e) => {
                println!("  [restore] RDB error: {e} — starting fresh");
                rdb_ts = 0.0;
            }
        }

        // Replay AOF records newer than the RDB snapshot
        let records = AofWriter::replay(&self.config.aof_path);
        let mut replayed = 0usize;
        for record in records {
            if record.ts <= rdb_ts {
                continue;
            }
            self.apply_aof_record(&record);
            replayed += 1;
        }
        if replayed > 0 {
            println!("  [restore] AOF replayed {replayed} ops after RDB timestamp");
        }
    }

    fn apply_aof_record(&self, r: &AofRecord) {
        let store = self.ns(&r.ns);
        match r.op.as_str() {
            "SET" => {
                if let Some(v) = &r.value {
                    if let Ok(val) = serde_json::from_value::<DollValue>(v.clone()) {
                        let mut entry = MemoryEntry::new(val);
                        if let Some(tags) = &r.tags {
                            entry.tags = tags.iter().cloned().collect();
                        }
                        if let Some(exp) = r.expires {
                            entry.expires = Some(exp);
                        }
                        store.set(&r.key, entry);
                    }
                }
            }
            "DEL" => { store.delete(&r.key); }
            "EXPIRE" => {
                if let Some(ttl) = r.ttl {
                    store.expire(&r.key, ttl);
                }
            }
            "HSET" => {
                if let (Some(field), Some(v)) = (&r.field, &r.value) {
                    if let Ok(val) = serde_json::from_value::<DollValue>(v.clone()) {
                        store.hset(&r.key, field, val);
                    }
                }
            }
            "LPUSH" => {
                if let Some(v) = &r.value {
                    if let Ok(val) = serde_json::from_value::<DollValue>(v.clone()) {
                        store.lpush(&r.key, val);
                    }
                }
            }
            "RPUSH" => {
                if let Some(v) = &r.value {
                    if let Ok(val) = serde_json::from_value::<DollValue>(v.clone()) {
                        store.rpush(&r.key, val);
                    }
                }
            }
            "SADD" => {
                if let Some(m) = &r.member {
                    store.sadd(&r.key, m.clone());
                }
            }
            "ZADD" => {
                if let (Some(m), Some(s)) = (&r.member, r.score) {
                    store.zadd(&r.key, m, s);
                }
            }
            "INCR" => {
                store.incr(&r.key, r.by.unwrap_or(1.0));
            }
            "DOLL_SET" => {
                if let (Some(path), Some(v)) = (&r.path, &r.value) {
                    if let Ok(val) = serde_json::from_value::<DollValue>(v.clone()) {
                        store.doll_set(&r.key, path, val);
                    }
                }
            }
            _ => {}
        }
    }

    // ── Event hooks ───────────────────────────────────────────────

    pub fn on_set<F>(&self, hook: F)
    where
        F: Fn(&str, &str, &DollValue) + Send + Sync + 'static,
    {
        self.on_set_hooks.write().push(Box::new(hook));
    }

    pub fn emit_set(&self, agent: &str, key: &str, value: &DollValue) {
        for hook in self.on_set_hooks.read().iter() {
            hook(agent, key, value);
        }
    }

    // ── Background threads ────────────────────────────────────────

    fn start_background_threads(self: &Arc<Self>) {
        // Auto-snapshot
        {
            let engine = Arc::clone(self);
            let interval = engine.config.snapshot_interval_secs;
            std::thread::Builder::new()
                .name("rdb-auto".to_string())
                .spawn(move || loop {
                    std::thread::sleep(std::time::Duration::from_secs(interval));
                    match engine.snapshot() {
                        Ok(sz) => println!(
                            "\n[auto-snapshot] ✓  {sz:>8} bytes → {}",
                            engine.config.rdb_path
                        ),
                        Err(e) => println!("\n[auto-snapshot] ✗  {e}"),
                    }
                })
                .ok();
        }

        // TTL sweeper
        {
            let engine = Arc::clone(self);
            let interval_ms = engine.config.ttl_check_interval_ms;
            std::thread::Builder::new()
                .name("ttl-sweep".to_string())
                .spawn(move || loop {
                    std::thread::sleep(std::time::Duration::from_millis(interval_ms));
                    let ns = engine.namespaces.read();
                    let total: usize = ns.values().map(|s| s.sweep_expired()).sum();
                    drop(ns);
                    if total > 0 {
                        // Could emit an event here
                        let _ = total;
                    }
                })
                .ok();
        }
    }

    // ── Identity ──────────────────────────────────────────────────

    pub fn identity_report(&self, agent_name: &str) -> String {
        let ids = self.identities.read();
        match ids.get(agent_name) {
            Some(iv) => iv.report(),
            None => format!("No identity data for '{agent_name}'"),
        }
    }

    // ── Global stats ──────────────────────────────────────────────

    pub fn stats(&self) -> EngineStats {
        let ns = self.namespaces.read();
        let ns_stats: Vec<_> = ns.values().map(|s| s.stats()).collect();
        let total_live: usize = ns_stats.iter().map(|s| s.live_keys).sum();
        EngineStats {
            version: VERSION.to_string(),
            namespace_count: ns.len(),
            total_live_keys: total_live,
            pubsub_channels: self.pubsub.channel_list().len(),
            last_snapshot: *self.last_snapshot.read(),
            namespaces: ns_stats,
        }
    }

    pub fn shutdown(&self) {
        println!("\n[shutdown] Saving final snapshot...");
        match self.snapshot() {
            Ok(sz) => println!("[shutdown] RDB saved — {sz} bytes"),
            Err(e) => println!("[shutdown] Snapshot failed: {e}"),
        }
        if let Some(aof) = &self.aof {
            aof.close();
        }
        println!("[shutdown] Done. ✓");
    }
}

// ── Engine Stats ──────────────────────────────────────────────────

#[derive(Debug)]
pub struct EngineStats {
    pub version: String,
    pub namespace_count: usize,
    pub total_live_keys: usize,
    pub pubsub_channels: usize,
    pub last_snapshot: f64,
    pub namespaces: Vec<crate::store::NamespaceStats>,
}

// ── AgentProxy — ergonomic facade ────────────────────────────────

pub struct AgentProxy {
    pub name: String,
    engine: Arc<AgentMemoryEngine>,
}

impl AgentProxy {
    fn store(&self) -> Arc<NamespaceStore> {
        self.engine.ns(&self.name)
    }

    fn aof_log_record(&self, record: AofRecord) {
        self.engine.aof_log(record);
    }

    fn absorb(&self, key: &str, value: &DollValue, importance: f64) {
        self.engine.absorb(&self.name, key, value, importance);
    }

    // ── String ────────────────────────────────────────────────────

    pub fn set(
        &self,
        key: &str,
        value: impl Into<String>,
        ttl: Option<f64>,
        tags: Option<Vec<String>>,
        importance: f64,
    ) {
        let doll = DollValue::String(value.into());
        let mut entry = MemoryEntry::new(doll.clone());
        if let Some(t) = ttl {
            entry = entry.with_ttl(t);
        }
        if let Some(ref t) = tags {
            entry.tags = t.iter().cloned().collect();
        }
        entry.importance = importance;
        let expires = entry.expires;
        self.store().set(key, entry);
        self.absorb(key, &doll, importance);
        self.engine.emit_set(&self.name, key, &doll);
        self.aof_log_record(AofRecord {
            ns: self.name.clone(), op: "SET".to_string(), key: key.to_string(),
            ts: now_secs(), value: serde_json::to_value(&doll).ok(),
            tags: tags.clone(), expires, ..Default::default()
        });
    }

    pub fn get(&self, key: &str) -> Option<DollValue> {
        let entry = self.store().get(key)?;
        self.engine.read_tick(&self.name);
        Some(entry.value)
    }

    pub fn delete(&self, key: &str) -> bool {
        let ok = self.store().delete(key);
        if ok {
            self.aof_log_record(AofRecord {
                ns: self.name.clone(), op: "DEL".to_string(),
                key: key.to_string(), ts: now_secs(), ..Default::default()
            });
        }
        ok
    }

    pub fn expire(&self, key: &str, ttl: f64) -> bool {
        let ok = self.store().expire(key, ttl);
        if ok {
            self.aof_log_record(AofRecord {
                ns: self.name.clone(), op: "EXPIRE".to_string(),
                key: key.to_string(), ts: now_secs(), ttl: Some(ttl), ..Default::default()
            });
        }
        ok
    }

    pub fn ttl(&self, key: &str) -> f64 {
        self.store().ttl(key)
    }

    pub fn exists(&self, key: &str) -> bool {
        self.store().exists(key)
    }

    pub fn keys(&self, pattern: &str) -> Vec<String> {
        self.store().keys(pattern)
    }

    // ── Hash ──────────────────────────────────────────────────────

    pub fn hset(&self, key: &str, field: &str, value: impl Into<String>) {
        let doll = DollValue::String(value.into());
        self.store().hset(key, field, doll.clone());
        self.absorb(&format!("{key}.{field}"), &doll, 0.7);
        self.aof_log_record(AofRecord {
            ns: self.name.clone(), op: "HSET".to_string(), key: key.to_string(),
            ts: now_secs(), field: Some(field.to_string()),
            value: serde_json::to_value(&doll).ok(), ..Default::default()
        });
    }

    pub fn hset_float(&self, key: &str, field: &str, value: f64) {
        let doll = DollValue::Float(value);
        self.store().hset(key, field, doll.clone());
        self.absorb(&format!("{key}.{field}"), &doll, 0.7);
        self.aof_log_record(AofRecord {
            ns: self.name.clone(), op: "HSET".to_string(), key: key.to_string(),
            ts: now_secs(), field: Some(field.to_string()),
            value: serde_json::to_value(&doll).ok(), ..Default::default()
        });
    }

    pub fn hget(&self, key: &str, field: &str) -> Option<DollValue> {
        self.store().hget(key, field)
    }

    pub fn hgetall(&self, key: &str) -> std::collections::HashMap<String, DollValue> {
        self.store().hgetall(key)
    }

    // ── List ──────────────────────────────────────────────────────

    pub fn lpush(&self, key: &str, value: impl Into<String>) -> usize {
        let doll = DollValue::String(value.into());
        let n = self.store().lpush(key, doll.clone());
        self.absorb(key, &doll, 0.5);
        self.aof_log_record(AofRecord {
            ns: self.name.clone(), op: "LPUSH".to_string(), key: key.to_string(),
            ts: now_secs(), value: serde_json::to_value(&doll).ok(), ..Default::default()
        });
        n
    }

    pub fn rpush(&self, key: &str, value: impl Into<String>) -> usize {
        let doll = DollValue::String(value.into());
        let n = self.store().rpush(key, doll.clone());
        self.absorb(key, &doll, 0.5);
        self.aof_log_record(AofRecord {
            ns: self.name.clone(), op: "RPUSH".to_string(), key: key.to_string(),
            ts: now_secs(), value: serde_json::to_value(&doll).ok(), ..Default::default()
        });
        n
    }

    pub fn lpop(&self, key: &str) -> Option<DollValue> { self.store().lpop(key) }
    pub fn rpop(&self, key: &str) -> Option<DollValue> { self.store().rpop(key) }
    pub fn lrange(&self, key: &str, start: i64, stop: i64) -> Vec<DollValue> {
        self.store().lrange(key, start, stop)
    }
    pub fn llen(&self, key: &str) -> usize { self.store().llen(key) }

    // ── Set ───────────────────────────────────────────────────────

    pub fn sadd(&self, key: &str, member: impl Into<String>) -> bool {
        let m = member.into();
        let ok = self.store().sadd(key, m.clone());
        if ok {
            self.aof_log_record(AofRecord {
                ns: self.name.clone(), op: "SADD".to_string(), key: key.to_string(),
                ts: now_secs(), member: Some(m), ..Default::default()
            });
        }
        ok
    }

    pub fn smembers(&self, key: &str) -> std::collections::HashSet<String> {
        self.store().smembers(key)
    }

    pub fn sismember(&self, key: &str, member: &str) -> bool {
        self.store().sismember(key, member)
    }

    // ── Sorted Set ────────────────────────────────────────────────

    pub fn zadd(&self, key: &str, member: impl Into<String>, score: f64) {
        let m = member.into();
        self.store().zadd(key, &m, score);
        self.aof_log_record(AofRecord {
            ns: self.name.clone(), op: "ZADD".to_string(), key: key.to_string(),
            ts: now_secs(), member: Some(m), score: Some(score), ..Default::default()
        });
    }

    pub fn zrange(&self, key: &str, start: i64, stop: i64) -> Vec<(String, f64)> {
        self.store().zrange(key, start, stop)
    }

    pub fn zscore(&self, key: &str, member: &str) -> Option<f64> {
        self.store().zscore(key, member)
    }

    // ── Counter ───────────────────────────────────────────────────

    pub fn incr(&self, key: &str, by: f64) -> f64 {
        let result = self.store().incr(key, by);
        self.aof_log_record(AofRecord {
            ns: self.name.clone(), op: "INCR".to_string(), key: key.to_string(),
            ts: now_secs(), by: Some(by), ..Default::default()
        });
        result
    }

    pub fn decr(&self, key: &str, by: f64) -> f64 {
        self.incr(key, -by)
    }

    // ── Nesting Doll ──────────────────────────────────────────────

    pub fn doll_set(&self, key: &str, path: &str, value: DollValue) -> bool {
        let ok = self.store().doll_set(key, path, value.clone());
        if ok {
            self.absorb(&format!("{key}.{path}"), &value, 0.8);
            self.aof_log_record(AofRecord {
                ns: self.name.clone(), op: "DOLL_SET".to_string(), key: key.to_string(),
                ts: now_secs(), path: Some(path.to_string()),
                value: serde_json::to_value(&value).ok(), ..Default::default()
            });
        }
        ok
    }

    pub fn doll_get(&self, key: &str, path: &str) -> Option<DollValue> {
        self.store().doll_get(key, path)
    }

    // ── Tags ──────────────────────────────────────────────────────

    pub fn by_tag(&self, tags: &[&str], match_all: bool) -> Vec<String> {
        self.store().by_tag(tags, match_all)
    }

    // ── Pub/Sub ───────────────────────────────────────────────────

    pub fn publish(&self, channel: &str, message: &str) -> usize {
        self.engine.pubsub.publish(channel, message)
    }

    pub fn subscribe(&self, channel: &str) -> crossbeam_channel::Receiver<crate::store::PubSubMessage> {
        self.engine.pubsub.subscribe(channel)
    }

    // ── Stats / Identity ──────────────────────────────────────────

    pub fn stats(&self) -> crate::store::NamespaceStats {
        self.store().stats()
    }

    pub fn identity(&self) -> String {
        self.engine.identity_report(&self.name)
    }
}

// ── AofRecord Default ─────────────────────────────────────────────

impl Default for AofRecord {
    fn default() -> Self {
        Self {
            ns: String::new(), op: String::new(), key: String::new(),
            ts: 0.0, value: None, field: None, member: None,
            score: None, by: None, ttl: None, tags: None, expires: None, path: None,
        }
    }
}
