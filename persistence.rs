// ══════════════════════════════════════════════════════════════════
//  persistence.rs — AOF write-ahead log + RDB binary snapshots
// ══════════════════════════════════════════════════════════════════

use crate::identity::IdentityVector;
use crate::models::MemoryEntry;
use crate::store::NamespaceStore;
use flate2::{read::ZlibDecoder, write::ZlibEncoder, Compression};
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

// ── RDB Magic ────────────────────────────────────────────────────

const RDB_MAGIC: &[u8] = b"AGENTMEMR"; // 9 bytes so it's Rust-specific
const RDB_VERSION: u16 = 2;

// ── AOF Record ───────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AofRecord {
    pub ns: String,
    pub op: String,
    pub key: String,
    pub ts: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub value: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub field: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub member: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub score: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub by: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ttl: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tags: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expires: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
}

// ── AOF Writer ───────────────────────────────────────────────────

pub struct AofWriter {
    file: Arc<Mutex<File>>,
    pub path: String,
    pub fsync_mode: String,
}

impl AofWriter {
    pub fn open(path: &str, fsync_mode: &str) -> std::io::Result<Self> {
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)?;
        let writer = Self {
            file: Arc::new(Mutex::new(file)),
            path: path.to_string(),
            fsync_mode: fsync_mode.to_string(),
        };
        if fsync_mode == "everysec" {
            writer.start_fsync_thread();
        }
        Ok(writer)
    }

    pub fn log(&self, record: AofRecord) {
        if let Ok(mut line) = serde_json::to_vec(&record) {
            line.push(b'\n');
            let mut f = self.file.lock();
            let _ = f.write_all(&line);
            if self.fsync_mode == "always" {
                let _ = f.flush();
            }
        }
    }

    fn start_fsync_thread(&self) {
        let file = Arc::clone(&self.file);
        std::thread::Builder::new()
            .name("aof-fsync".to_string())
            .spawn(move || loop {
                std::thread::sleep(std::time::Duration::from_secs(1));
                let _ = file.lock().flush();
            })
            .ok();
    }

    pub fn close(&self) {
        let _ = self.file.lock().flush();
    }

    pub fn replay(path: &str) -> Vec<AofRecord> {
        let file = match File::open(path) {
            Ok(f) => f,
            Err(_) => return vec![],
        };
        let reader = BufReader::new(file);
        reader
            .lines()
            .filter_map(|line| {
                let line = line.ok()?;
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    return None;
                }
                serde_json::from_str::<AofRecord>(trimmed).ok()
            })
            .collect()
    }
}

// ── RDB Snapshot ─────────────────────────────────────────────────

#[derive(Serialize, Deserialize)]
pub struct RdbPayload {
    pub namespaces: HashMap<String, HashMap<String, MemoryEntry>>,
    pub identities: HashMap<String, IdentityVector>,
    pub saved_at: f64,
}

pub struct RdbSnapshot;

impl RdbSnapshot {
    /// Serialize and compress all state to an RDB file.
    /// Format: [MAGIC][VERSION:2b][TIMESTAMP:8b][LEN:4b][ZLIB_DATA][CRC32:4b]
    pub fn save(
        path: &str,
        namespaces: &HashMap<String, Arc<NamespaceStore>>,
        identities: &HashMap<String, IdentityVector>,
    ) -> std::io::Result<usize> {
        let ns_data: HashMap<String, HashMap<String, MemoryEntry>> = namespaces
            .iter()
            .map(|(name, store)| (name.clone(), store.snapshot_data()))
            .collect();

        let payload = RdbPayload {
            namespaces: ns_data,
            identities: identities.clone(),
            saved_at: now_secs(),
        };

        let raw = bincode::serialize(&payload)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;

        // Compress
        let mut compressed = Vec::new();
        {
            let mut encoder = ZlibEncoder::new(&mut compressed, Compression::default());
            encoder.write_all(&raw)?;
            encoder.finish()?;
        }

        let crc = crc32fast::hash(&compressed);
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        let mut f = File::create(path)?;
        f.write_all(RDB_MAGIC)?;
        f.write_all(&RDB_VERSION.to_be_bytes())?;
        f.write_all(&ts.to_be_bytes())?;
        f.write_all(&(compressed.len() as u32).to_be_bytes())?;
        f.write_all(&compressed)?;
        f.write_all(&crc.to_be_bytes())?;

        Ok(compressed.len())
    }

    pub fn load(path: &str) -> std::io::Result<Option<RdbPayload>> {
        let mut f = match File::open(path) {
            Ok(f) => f,
            Err(_) => return Ok(None),
        };

        let mut magic = [0u8; 9];
        f.read_exact(&mut magic)?;
        if &magic != RDB_MAGIC {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "Invalid RDB magic bytes",
            ));
        }

        let mut version_buf = [0u8; 2];
        f.read_exact(&mut version_buf)?;
        let _version = u16::from_be_bytes(version_buf);

        let mut ts_buf = [0u8; 8];
        f.read_exact(&mut ts_buf)?;
        let _ts = u64::from_be_bytes(ts_buf);

        let mut len_buf = [0u8; 4];
        f.read_exact(&mut len_buf)?;
        let compressed_len = u32::from_be_bytes(len_buf) as usize;

        let mut compressed = vec![0u8; compressed_len];
        f.read_exact(&mut compressed)?;

        let mut crc_buf = [0u8; 4];
        f.read_exact(&mut crc_buf)?;
        let stored_crc = u32::from_be_bytes(crc_buf);

        let actual_crc = crc32fast::hash(&compressed);
        if actual_crc != stored_crc {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "RDB checksum mismatch — file may be corrupted",
            ));
        }

        let mut decoder = ZlibDecoder::new(compressed.as_slice());
        let mut raw = Vec::new();
        decoder.read_to_end(&mut raw)?;

        let payload: RdbPayload = bincode::deserialize(&raw)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;

        Ok(Some(payload))
    }

    /// Restore data from a loaded payload into the engine's stores.
    pub fn restore(
        payload: RdbPayload,
    ) -> (
        HashMap<String, Arc<NamespaceStore>>,
        HashMap<String, IdentityVector>,
    ) {
        let mut namespaces: HashMap<String, Arc<NamespaceStore>> = HashMap::new();
        for (name, data) in payload.namespaces {
            let store = Arc::new(NamespaceStore::new(&name));
            store.restore_data(data);
            namespaces.insert(name, store);
        }
        (namespaces, payload.identities)
    }
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
