// ══════════════════════════════════════════════════════════════════
//  identity.rs — Emergent identity vectors that drift over time
// ══════════════════════════════════════════════════════════════════

use crate::models::{now_secs, DollValue};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IdentityVector {
    pub agent_id: String,
    pub traits: HashMap<String, f64>,
    pub created: f64,
    pub snapshots: u64,
    pub total_writes: u64,
    pub total_reads: u64,
}

impl IdentityVector {
    pub fn new(agent_id: &str) -> Self {
        Self {
            agent_id: agent_id.to_string(),
            traits: HashMap::new(),
            created: now_secs(),
            snapshots: 0,
            total_writes: 0,
            total_reads: 0,
        }
    }

    /// Absorb a write event into trait weights.
    pub fn absorb(&mut self, key: &str, value: &DollValue, importance: f64) {
        // Key-based trait
        let short_key: String = key.split(':').last().unwrap_or(key).chars().take(32).collect();
        let trait_key = format!("key:{short_key}");
        *self.traits.entry(trait_key).or_insert(0.0) += importance;

        // Value-length trait (verbosity signature)
        let val_str = format!("{:?}", value);
        let verbosity = if val_str.len() < 50 {
            "verbosity:short"
        } else if val_str.len() < 200 {
            "verbosity:medium"
        } else {
            "verbosity:long"
        };
        *self.traits.entry(verbosity.to_string()).or_insert(0.0) += 0.3;

        // Nesting doll depth trait
        let depth = doll_depth(value);
        let depth_trait = format!("nesting:depth_{}", depth.min(5));
        *self.traits.entry(depth_trait).or_insert(0.0) += 0.2 * depth as f64;

        // Temporal rhythm trait
        let period = time_period();
        *self.traits.entry(period).or_insert(0.0) += 0.1;

        // Data type trait
        let type_trait = format!("type:{}", value.type_name());
        *self.traits.entry(type_trait).or_insert(0.0) += 0.15;

        self.total_writes += 1;
    }

    /// Decay trait weights, pruning negligible ones.
    pub fn decay(&mut self, rate: f64, min_weight: f64) {
        self.traits.retain(|_, v| {
            *v *= rate;
            *v >= min_weight
        });
        self.snapshots += 1;
    }

    /// Top N dominant traits by weight.
    pub fn dominant_traits(&self, top_n: usize) -> Vec<(&str, f64)> {
        let mut sorted: Vec<(&str, f64)> =
            self.traits.iter().map(|(k, v)| (k.as_str(), *v)).collect();
        sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        sorted.truncate(top_n);
        sorted
    }

    /// Stable short fingerprint of this identity.
    pub fn signature(&self) -> String {
        let top = self.dominant_traits(3);
        let raw: String = top
            .iter()
            .map(|(k, v)| format!("{k}={v:.2}"))
            .collect::<Vec<_>>()
            .join("|");
        let hash = Sha256::digest(raw.as_bytes());
        hex::encode(&hash[..6])
    }

    pub fn report(&self) -> String {
        let sig = self.signature();
        let top = self.dominant_traits(8);
        let max_w: f64 = top.first().map(|(_, w)| *w).unwrap_or(1.0).max(0.001);
        let created = format_ts(self.created);

        let mut lines = vec![
            format!("╔═ Identity: {} [sig:{}] ═╗", self.agent_id, sig),
            format!(
                "  Snapshots: {}  Writes: {}  Reads: {}",
                self.snapshots, self.total_writes, self.total_reads
            ),
            format!("  Created:   {created}"),
            "  Dominant traits:".to_string(),
        ];

        for (trait_name, weight) in &top {
            let bar_len = ((weight / max_w) * 20.0) as usize;
            let bar = "█".repeat(bar_len.max(1));
            lines.push(format!("    {trait_name:<40}  {bar:<20}  {weight:.3}"));
        }

        let hdr_len = lines[0].chars().count();
        lines.push(format!("╚{}╝", "═".repeat(hdr_len.saturating_sub(2))));
        lines.join("\n")
    }
}

// ── Helpers ─────────────────────────────────────────────────────

fn doll_depth(v: &DollValue) -> usize {
    use crate::models::DollValue;
    match v {
        DollValue::Doll(children) => {
            1 + children.values().map(|c| doll_depth(c)).max().unwrap_or(0)
        }
        DollValue::Hash(fields) => {
            1 + fields.values().map(|c| doll_depth(c)).max().unwrap_or(0)
        }
        _ => 0,
    }
}

fn time_period() -> String {
    let hour = chrono::Local::now().hour();
    let period = if (5..12).contains(&hour) {
        "morning"
    } else if (12..18).contains(&hour) {
        "afternoon"
    } else if (18..22).contains(&hour) {
        "evening"
    } else {
        "night"
    };
    format!("rhythm:{period}")
}

pub fn format_ts(ts: f64) -> String {
    use chrono::{DateTime, Local, TimeZone};
    let dt: DateTime<Local> = Local.timestamp_opt(ts as i64, 0).single().unwrap_or_default();
    dt.format("%Y-%m-%d %H:%M:%S").to_string()
}

// We need the chrono Hour trait
use chrono::Timelike;
