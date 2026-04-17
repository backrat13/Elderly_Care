// ══════════════════════════════════════════════════════════════════
//  store.rs — Namespaced, thread-safe in-memory KV store
// ══════════════════════════════════════════════════════════════════

use crate::models::{now_secs, DollValue, MemoryEntry};
use parking_lot::RwLock;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::Arc;

// ── NamespaceStore ───────────────────────────────────────────────

#[derive(Debug)]
pub struct NamespaceStore {
    pub namespace: String,
    inner: RwLock<StoreInner>,
}

#[derive(Debug, Serialize, Deserialize)]
struct StoreInner {
    data: HashMap<String, MemoryEntry>,
    tag_index: HashMap<String, HashSet<String>>, // tag → {keys}
}

impl NamespaceStore {
    pub fn new(namespace: &str) -> Self {
        Self {
            namespace: namespace.to_string(),
            inner: RwLock::new(StoreInner {
                data: HashMap::new(),
                tag_index: HashMap::new(),
            }),
        }
    }

    // ── Core SET / GET ────────────────────────────────────────────

    pub fn set(&self, key: &str, entry: MemoryEntry) {
        let mut inner = self.inner.write();
        // Collect old tags first to avoid borrow conflict
        let old_tags: Vec<String> = inner
            .data
            .get(key)
            .map(|e| e.tags.iter().cloned().collect())
            .unwrap_or_default();
        for tag in &old_tags {
            if let Some(set) = inner.tag_index.get_mut(tag) {
                set.remove(key);
            }
        }
        // Add new tag associations
        for tag in &entry.tags {
            inner
                .tag_index
                .entry(tag.clone())
                .or_default()
                .insert(key.to_string());
        }
        inner.data.insert(key.to_string(), entry);
    }

    pub fn get(&self, key: &str) -> Option<MemoryEntry> {
        let expired = {
            let inner = self.inner.read();
            inner
                .data
                .get(key)
                .map(|e| (e.is_expired(), e.clone()))
        };
        match expired {
            None => None,
            Some((true, _)) => {
                self.delete(key);
                None
            }
            Some((false, mut entry)) => {
                entry.touch();
                // Write back updated access count
                let mut inner = self.inner.write();
                if let Some(e) = inner.data.get_mut(key) {
                    e.touch();
                }
                drop(inner);
                Some(entry)
            }
        }
    }

    pub fn get_raw(&self, key: &str) -> Option<MemoryEntry> {
        self.inner.read().data.get(key).cloned()
    }

    pub fn delete(&self, key: &str) -> bool {
        let mut inner = self.inner.write();
        if let Some(entry) = inner.data.remove(key) {
            for tag in &entry.tags {
                if let Some(set) = inner.tag_index.get_mut(tag) {
                    set.remove(key);
                }
            }
            true
        } else {
            false
        }
    }

    pub fn exists(&self, key: &str) -> bool {
        self.get(key).is_some()
    }

    pub fn keys(&self, pattern: &str) -> Vec<String> {
        let inner = self.inner.read();
        inner
            .data
            .iter()
            .filter(|(_, e)| !e.is_expired())
            .map(|(k, _)| k.clone())
            .filter(|k| glob_match(k, pattern))
            .collect()
    }

    // ── TTL ───────────────────────────────────────────────────────

    pub fn expire(&self, key: &str, ttl_secs: f64) -> bool {
        let mut inner = self.inner.write();
        if let Some(entry) = inner.data.get_mut(key) {
            entry.expires = Some(now_secs() + ttl_secs);
            true
        } else {
            false
        }
    }

    pub fn ttl(&self, key: &str) -> f64 {
        let inner = self.inner.read();
        match inner.data.get(key) {
            None => -2.0,
            Some(e) => e.ttl(),
        }
    }

    pub fn sweep_expired(&self) -> usize {
        let expired: Vec<String> = {
            let inner = self.inner.read();
            inner
                .data
                .iter()
                .filter(|(_, e)| e.is_expired())
                .map(|(k, _)| k.clone())
                .collect()
        };
        let count = expired.len();
        for k in expired {
            self.delete(&k);
        }
        count
    }

    // ── Hash operations (convenience) ─────────────────────────────

    pub fn hset(&self, key: &str, field: &str, value: DollValue) {
        let mut inner = self.inner.write();
        let entry = inner.data.entry(key.to_string()).or_insert_with(|| {
            MemoryEntry::new(DollValue::Hash(HashMap::new()))
        });
        if let DollValue::Hash(ref mut map) = entry.value {
            map.insert(field.to_string(), value);
        } else {
            entry.value = DollValue::Hash(HashMap::from([(field.to_string(), value)]));
        }
        entry.touch();
    }

    pub fn hget(&self, key: &str, field: &str) -> Option<DollValue> {
        let inner = self.inner.read();
        if let Some(entry) = inner.data.get(key) {
            if let DollValue::Hash(ref map) = entry.value {
                return map.get(field).cloned();
            }
        }
        None
    }

    pub fn hgetall(&self, key: &str) -> HashMap<String, DollValue> {
        let inner = self.inner.read();
        if let Some(entry) = inner.data.get(key) {
            if let DollValue::Hash(ref map) = entry.value {
                return map.clone();
            }
        }
        HashMap::new()
    }

    pub fn hdel(&self, key: &str, field: &str) -> bool {
        let mut inner = self.inner.write();
        if let Some(entry) = inner.data.get_mut(key) {
            if let DollValue::Hash(ref mut map) = entry.value {
                return map.remove(field).is_some();
            }
        }
        false
    }

    // ── List operations ───────────────────────────────────────────

    pub fn lpush(&self, key: &str, value: DollValue) -> usize {
        let mut inner = self.inner.write();
        let entry = inner.data.entry(key.to_string()).or_insert_with(|| {
            MemoryEntry::new(DollValue::List(VecDeque::new()))
        });
        if let DollValue::List(ref mut list) = entry.value {
            list.push_front(value);
            let len = list.len();
            entry.touch();
            len
        } else {
            0
        }
    }

    pub fn rpush(&self, key: &str, value: DollValue) -> usize {
        let mut inner = self.inner.write();
        let entry = inner.data.entry(key.to_string()).or_insert_with(|| {
            MemoryEntry::new(DollValue::List(VecDeque::new()))
        });
        if let DollValue::List(ref mut list) = entry.value {
            list.push_back(value);
            let len = list.len();
            entry.touch();
            len
        } else {
            0
        }
    }

    pub fn lpop(&self, key: &str) -> Option<DollValue> {
        let mut inner = self.inner.write();
        if let Some(entry) = inner.data.get_mut(key) {
            if let DollValue::List(ref mut list) = entry.value {
                return list.pop_front();
            }
        }
        None
    }

    pub fn rpop(&self, key: &str) -> Option<DollValue> {
        let mut inner = self.inner.write();
        if let Some(entry) = inner.data.get_mut(key) {
            if let DollValue::List(ref mut list) = entry.value {
                return list.pop_back();
            }
        }
        None
    }

    pub fn lrange(&self, key: &str, start: i64, stop: i64) -> Vec<DollValue> {
        let inner = self.inner.read();
        if let Some(entry) = inner.data.get(key) {
            if let DollValue::List(ref list) = entry.value {
                let len = list.len() as i64;
                let s = if start < 0 { (len + start).max(0) } else { start.min(len) } as usize;
                let e = if stop < 0 { (len + stop + 1).max(0) } else { (stop + 1).min(len) } as usize;
                return list.range(s..e).cloned().collect();
            }
        }
        vec![]
    }

    pub fn llen(&self, key: &str) -> usize {
        let inner = self.inner.read();
        if let Some(entry) = inner.data.get(key) {
            if let DollValue::List(ref list) = entry.value {
                return list.len();
            }
        }
        0
    }

    // ── Set operations ────────────────────────────────────────────

    pub fn sadd(&self, key: &str, member: String) -> bool {
        let mut inner = self.inner.write();
        let entry = inner.data.entry(key.to_string()).or_insert_with(|| {
            MemoryEntry::new(DollValue::Set(HashSet::new()))
        });
        if let DollValue::Set(ref mut set) = entry.value {
            return set.insert(member);
        }
        false
    }

    pub fn smembers(&self, key: &str) -> HashSet<String> {
        let inner = self.inner.read();
        if let Some(entry) = inner.data.get(key) {
            if let DollValue::Set(ref set) = entry.value {
                return set.clone();
            }
        }
        HashSet::new()
    }

    pub fn sismember(&self, key: &str, member: &str) -> bool {
        let inner = self.inner.read();
        if let Some(entry) = inner.data.get(key) {
            if let DollValue::Set(ref set) = entry.value {
                return set.contains(member);
            }
        }
        false
    }

    pub fn srem(&self, key: &str, member: &str) -> bool {
        let mut inner = self.inner.write();
        if let Some(entry) = inner.data.get_mut(key) {
            if let DollValue::Set(ref mut set) = entry.value {
                return set.remove(member);
            }
        }
        false
    }

    // ── SortedSet operations ──────────────────────────────────────

    pub fn zadd(&self, key: &str, member: &str, score: f64) {
        use crate::models::OrderedFloat;
        let mut inner = self.inner.write();
        let entry = inner.data.entry(key.to_string()).or_insert_with(|| {
            MemoryEntry::new(DollValue::SortedSet(std::collections::BTreeMap::new()))
        });
        if let DollValue::SortedSet(ref mut map) = entry.value {
            map.insert(member.to_string(), OrderedFloat(score));
        }
        entry.touch();
    }

    pub fn zrange(&self, key: &str, start: i64, stop: i64) -> Vec<(String, f64)> {
        let inner = self.inner.read();
        if let Some(entry) = inner.data.get(key) {
            if let DollValue::SortedSet(ref map) = entry.value {
                let items: Vec<(String, f64)> =
                    map.iter().map(|(k, v)| (k.clone(), v.0)).collect();
                let len = items.len() as i64;
                let s = start.max(0) as usize;
                let e = if stop < 0 { (len + stop + 1).max(0) as usize } else { (stop + 1).min(len) as usize };
                return items[s..e.min(items.len())].to_vec();
            }
        }
        vec![]
    }

    pub fn zscore(&self, key: &str, member: &str) -> Option<f64> {
        let inner = self.inner.read();
        if let Some(entry) = inner.data.get(key) {
            if let DollValue::SortedSet(ref map) = entry.value {
                return map.get(member).map(|v| v.0);
            }
        }
        None
    }

    // ── Counter operations ────────────────────────────────────────

    pub fn incr(&self, key: &str, by: f64) -> f64 {
        let mut inner = self.inner.write();
        let entry = inner.data.entry(key.to_string()).or_insert_with(|| {
            MemoryEntry::new(DollValue::Float(0.0))
        });
        let new_val = match &entry.value {
            DollValue::Float(f) => f + by,
            _ => by,
        };
        entry.value = DollValue::Float(new_val);
        entry.touch();
        new_val
    }

    // ── Nesting Doll operations ───────────────────────────────────

    /// Set a value inside a doll at a dot-separated path.
    /// e.g. doll_set("brain", "sensory.vision.left", value)
    pub fn doll_set(&self, key: &str, path: &str, value: DollValue) -> bool {
        let mut inner = self.inner.write();
        let entry = inner.data.entry(key.to_string()).or_insert_with(|| {
            MemoryEntry::new(DollValue::Doll(HashMap::new()))
        });
        if let DollValue::Doll(ref mut root) = entry.value {
            let parts: Vec<&str> = path.split('.').collect();
            set_doll_path(root, &parts, value);
            entry.touch();
            true
        } else {
            false
        }
    }

    /// Get a value from inside a doll at a dot-separated path.
    pub fn doll_get(&self, key: &str, path: &str) -> Option<DollValue> {
        let inner = self.inner.read();
        if let Some(entry) = inner.data.get(key) {
            if let DollValue::Doll(ref root) = entry.value {
                let parts: Vec<&str> = path.split('.').collect();
                return get_doll_path(root, &parts);
            }
        }
        None
    }

    // ── Tag index ─────────────────────────────────────────────────

    pub fn by_tag(&self, tags: &[&str], match_all: bool) -> Vec<String> {
        let inner = self.inner.read();
        if tags.is_empty() {
            return vec![];
        }
        let sets: Vec<&HashSet<String>> = tags
            .iter()
            .filter_map(|t| inner.tag_index.get(*t))
            .collect();
        if sets.is_empty() {
            return vec![];
        }
        let result: HashSet<&String> = if match_all {
            let mut iter = sets.iter();
            let first: HashSet<&String> = iter.next().unwrap().iter().collect();
            iter.fold(first, |acc, s| {
                acc.intersection(&s.iter().collect()).cloned().collect()
            })
        } else {
            sets.iter()
                .flat_map(|s| s.iter())
                .collect()
        };
        result
            .into_iter()
            .filter(|k| inner.data.get(k.as_str()).map(|e| !e.is_expired()).unwrap_or(false))
            .map(|k| k.clone())
            .collect()
    }

    // ── Stats ─────────────────────────────────────────────────────

    pub fn stats(&self) -> NamespaceStats {
        let inner = self.inner.read();
        let total = inner.data.len();
        let expired = inner.data.values().filter(|e| e.is_expired()).count();
        let mut by_type: HashMap<String, usize> = HashMap::new();
        for entry in inner.data.values() {
            *by_type.entry(entry.value.type_name().to_string()).or_insert(0) += 1;
        }
        NamespaceStats {
            namespace: self.namespace.clone(),
            total_keys: total,
            live_keys: total - expired,
            expired_keys: expired,
            by_type,
            tag_count: inner.tag_index.len(),
        }
    }

    // ── Serialization ─────────────────────────────────────────────

    pub fn snapshot_data(&self) -> HashMap<String, MemoryEntry> {
        let inner = self.inner.read();
        inner
            .data
            .iter()
            .filter(|(_, e)| !e.is_expired())
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect()
    }

    pub fn restore_data(&self, data: HashMap<String, MemoryEntry>) {
        let mut inner = self.inner.write();
        for (k, entry) in &data {
            for tag in &entry.tags {
                inner
                    .tag_index
                    .entry(tag.clone())
                    .or_default()
                    .insert(k.clone());
            }
        }
        inner.data = data;
    }
}

// ── Pub/Sub Bus ──────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct PubSubBus {
    inner: Arc<RwLock<PubSubInner>>,
}

#[derive(Debug)]
struct PubSubInner {
    channels: HashMap<String, Vec<crossbeam_channel::Sender<PubSubMessage>>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PubSubMessage {
    pub channel: String,
    pub message: String,
    pub ts: f64,
}

impl PubSubBus {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(RwLock::new(PubSubInner {
                channels: HashMap::new(),
            })),
        }
    }

    pub fn subscribe(&self, channel: &str) -> crossbeam_channel::Receiver<PubSubMessage> {
        let (tx, rx) = crossbeam_channel::unbounded();
        self.inner
            .write()
            .channels
            .entry(channel.to_string())
            .or_default()
            .push(tx);
        rx
    }

    pub fn publish(&self, channel: &str, message: &str) -> usize {
        let mut inner = self.inner.write();
        let subs = inner.channels.entry(channel.to_string()).or_default();
        let msg = PubSubMessage {
            channel: channel.to_string(),
            message: message.to_string(),
            ts: now_secs(),
        };
        subs.retain(|tx| tx.send(msg.clone()).is_ok());
        subs.len()
    }

    pub fn channel_list(&self) -> Vec<String> {
        self.inner.read().channels.keys().cloned().collect()
    }
}

// ── Namespace Stats ───────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct NamespaceStats {
    pub namespace: String,
    pub total_keys: usize,
    pub live_keys: usize,
    pub expired_keys: usize,
    pub by_type: HashMap<String, usize>,
    pub tag_count: usize,
}

// ── Helper: glob matching ──────────────────────────────────────────

fn glob_match(key: &str, pattern: &str) -> bool {
    if pattern == "*" {
        return true;
    }
    // Simple glob: * matches anything
    let parts: Vec<&str> = pattern.split('*').collect();
    if parts.len() == 1 {
        return key == pattern;
    }
    let mut pos = 0;
    for (i, part) in parts.iter().enumerate() {
        if part.is_empty() {
            continue;
        }
        if i == 0 {
            if !key.starts_with(part) {
                return false;
            }
            pos = part.len();
        } else if let Some(idx) = key[pos..].find(part) {
            pos += idx + part.len();
        } else {
            return false;
        }
    }
    true
}

// ── Nesting Doll path helpers ─────────────────────────────────────

fn set_doll_path(root: &mut HashMap<String, Box<DollValue>>, path: &[&str], value: DollValue) {
    if path.is_empty() {
        return;
    }
    let key = path[0].to_string();
    if path.len() == 1 {
        root.insert(key, Box::new(value));
    } else {
        let child = root
            .entry(key)
            .or_insert_with(|| Box::new(DollValue::Doll(HashMap::new())));
        if let DollValue::Doll(ref mut sub) = **child {
            set_doll_path(sub, &path[1..], value);
        } else {
            // Overwrite with a new doll
            let mut sub = HashMap::new();
            set_doll_path(&mut sub, &path[1..], value);
            **child = DollValue::Doll(sub);
        }
    }
}

fn get_doll_path(root: &HashMap<String, Box<DollValue>>, path: &[&str]) -> Option<DollValue> {
    if path.is_empty() {
        return None;
    }
    let child = root.get(path[0])?;
    if path.len() == 1 {
        Some(*child.clone())
    } else if let DollValue::Doll(ref sub) = **child {
        get_doll_path(sub, &path[1..])
    } else {
        None
    }
}
