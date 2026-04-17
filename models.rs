// ══════════════════════════════════════════════════════════════════
//  models.rs — Core data types including Nesting Doll recursive memory
// ══════════════════════════════════════════════════════════════════

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};

// ── Nesting Doll Value ──────────────────────────────────────────
/// A recursive value type - the "Nesting Doll".
/// Any value can contain another full map of nested dolls,
/// allowing arbitrarily deep layered memory structures.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum DollValue {
    /// Innermost doll — a plain string
    String(String),
    /// Numeric counter
    Float(f64),
    /// A list of dolls
    List(VecDeque<DollValue>),
    /// A set of string members
    Set(HashSet<String>),
    /// A scored sorted map (member → score)
    SortedSet(BTreeMap<String, OrderedFloat>),
    /// A hash sub-map of dolls
    Hash(HashMap<String, DollValue>),
    /// The nesting doll itself — a full named map of dolls (sub-namespace)
    /// This is the recursive "doll inside a doll" variant.
    Doll(HashMap<String, Box<DollValue>>),
}

/// Wrapper so f64 can live inside BTreeMap keys.
/// We store it as ordered bits for stability.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, PartialOrd)]
pub struct OrderedFloat(pub f64);

impl Eq for OrderedFloat {}

impl Ord for OrderedFloat {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.partial_cmp(other).unwrap_or(std::cmp::Ordering::Equal)
    }
}

impl std::fmt::Display for OrderedFloat {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:.4}", self.0)
    }
}

impl DollValue {
    pub fn as_string(&self) -> Option<&str> {
        if let DollValue::String(s) = self {
            Some(s.as_str())
        } else {
            None
        }
    }

    pub fn as_float(&self) -> Option<f64> {
        if let DollValue::Float(f) = self {
            Some(*f)
        } else {
            None
        }
    }

    pub fn type_name(&self) -> &'static str {
        match self {
            DollValue::String(_) => "string",
            DollValue::Float(_) => "float",
            DollValue::List(_) => "list",
            DollValue::Set(_) => "set",
            DollValue::SortedSet(_) => "sortedset",
            DollValue::Hash(_) => "hash",
            DollValue::Doll(_) => "doll",
        }
    }

    /// Pretty-print the nesting doll with depth indentation.
    pub fn pretty_print(&self, indent: usize) -> String {
        let pad = "  ".repeat(indent);
        match self {
            DollValue::String(s) => format!("{pad}\"{}\"", s),
            DollValue::Float(f) => format!("{pad}{f}"),
            DollValue::List(items) => {
                let inner: Vec<String> =
                    items.iter().map(|v| v.pretty_print(indent + 1)).collect();
                format!("{pad}[\n{}\n{pad}]", inner.join(",\n"))
            }
            DollValue::Set(members) => {
                let mut sorted: Vec<&String> = members.iter().collect();
                sorted.sort();
                let inner: Vec<String> = sorted.iter().map(|s| format!("{}  \"{s}\"", pad)).collect();
                format!("{pad}{{\n{}\n{pad}}}", inner.join(",\n"))
            }
            DollValue::SortedSet(map) => {
                let inner: Vec<String> = map
                    .iter()
                    .map(|(k, v)| format!("{}  {}: {}", pad, v, k))
                    .collect();
                format!("{pad}sorted{{\n{}\n{pad}}}", inner.join(",\n"))
            }
            DollValue::Hash(fields) => {
                let inner: Vec<String> = fields
                    .iter()
                    .map(|(k, v)| format!("{pad}  \"{k}\": {}", v.pretty_print(0)))
                    .collect();
                format!("{pad}hash{{\n{}\n{pad}}}", inner.join(",\n"))
            }
            DollValue::Doll(children) => {
                let inner: Vec<String> = children
                    .iter()
                    .map(|(k, v)| {
                        format!("{pad}  🪆 \"{k}\":\n{}", v.pretty_print(indent + 2))
                    })
                    .collect();
                format!("{pad}🪆 doll{{\n{}\n{pad}}}", inner.join(",\n"))
            }
        }
    }
}

// ── Memory Entry ────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryEntry {
    pub value: DollValue,
    pub tags: HashSet<String>,
    pub created: f64,
    pub updated: f64,
    pub expires: Option<f64>, // unix timestamp
    pub access_count: u64,
    pub importance: f64, // 0.0 – 1.0
}

impl MemoryEntry {
    pub fn new(value: DollValue) -> Self {
        let now = now_secs();
        Self {
            value,
            tags: HashSet::new(),
            created: now,
            updated: now,
            expires: None,
            access_count: 0,
            importance: 1.0,
        }
    }

    pub fn with_tags(mut self, tags: HashSet<String>) -> Self {
        self.tags = tags;
        self
    }

    pub fn with_ttl(mut self, ttl_secs: f64) -> Self {
        self.expires = Some(now_secs() + ttl_secs);
        self
    }

    pub fn with_importance(mut self, importance: f64) -> Self {
        self.importance = importance;
        self
    }

    pub fn is_expired(&self) -> bool {
        if let Some(exp) = self.expires {
            now_secs() > exp
        } else {
            false
        }
    }

    pub fn touch(&mut self) {
        self.updated = now_secs();
        self.access_count += 1;
    }

    pub fn ttl(&self) -> f64 {
        match self.expires {
            None => -1.0,
            Some(exp) => {
                let rem = exp - now_secs();
                if rem < 0.0 {
                    0.0
                } else {
                    rem
                }
            }
        }
    }
}

// ── Helpers ─────────────────────────────────────────────────────

pub fn now_secs() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
