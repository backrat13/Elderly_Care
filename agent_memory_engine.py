"""
╔══════════════════════════════════════════════════════════════════╗
║           AGENT MEMORY ENGINE  v1.0                             ║
║   Redis-inspired · RDB + AOF · Multi-Agent · Identity Emergence ║
╚══════════════════════════════════════════════════════════════════╝

ARCHITECTURE:
  ┌─ Namespaced in-memory store (str/hash/list/set/sortedset)
  ├─ TTL expiry engine
  ├─ RDB snapshotting (zlib-compressed binary)
  ├─ AOF write-ahead log (replay on startup)
  ├─ Identity Ledger (trait vectors that drift over time)
  ├─ Semantic tag index (tag → key lookup)
  ├─ Pub/Sub bus (inter-agent messaging)
  ├─ Event hooks (on_set, on_expire, on_snapshot, on_identity_shift)
  └─ CLI (interactive REPL + scripted commands)

IDENTITY EMERGENCE THEORY:
  Every SET operation accumulates into trait vectors per agent.
  Frequently written keys, value sentiment patterns, and temporal
  rhythms are distilled into stable identity signatures that survive
  restarts — identity emerges from environmental circumstance,
  not hard-coded rules.

USAGE:
  python agent_memory_engine.py              # interactive REPL
  python agent_memory_engine.py --demo       # run feature demo
  python agent_memory_engine.py --restore    # restore from disk
"""

import os
import json
import time
import struct
import zlib
import pickle
import threading
import hashlib
import math
import heapq
import argparse
import readline
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


# ─────────────────────────────────────────────────────────────────
#  CONSTANTS & CONFIG
# ─────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
RDB_MAGIC = b"AGENTMEM"
RDB_VERSION = 1
AOF_SEPARATOR = b"\n---\n"

DEFAULT_CONFIG = {
    "rdb_path":              "memory.rdb",
    "aof_path":              "memory.aof",
    "identity_ledger_path":  "identity.ledger",
    "snapshot_interval_sec": 300,       # RDB auto-snapshot every 5 min
    "aof_fsync":             "everysec", # "always" | "everysec" | "no"
    "max_memory_keys":       100_000,
    "identity_decay_rate":   0.97,      # trait weight decay per snapshot
    "identity_min_weight":   0.01,      # prune traits below this weight
    "ttl_check_interval":    1.0,       # seconds between expiry sweeps
}


# ─────────────────────────────────────────────────────────────────
#  DATA TYPES
# ─────────────────────────────────────────────────────────────────

class DataType(str, Enum):
    STRING    = "string"
    HASH      = "hash"
    LIST      = "list"
    SET       = "set"
    SORTEDSET = "sortedset"
    COUNTER   = "counter"


@dataclass
class MemoryEntry:
    value:    Any
    dtype:    DataType
    tags:     Set[str]      = field(default_factory=set)
    created:  float         = field(default_factory=time.time)
    updated:  float         = field(default_factory=time.time)
    expires:  Optional[float] = None    # unix timestamp or None
    access_count: int       = 0
    importance:   float     = 1.0       # 0.0 – 1.0, influences identity weight

    def is_expired(self) -> bool:
        return self.expires is not None and time.time() > self.expires

    def touch(self):
        self.updated = time.time()
        self.access_count += 1


@dataclass
class IdentityVector:
    """Emergent identity traits for one agent namespace."""
    agent_id:     str
    traits:       Dict[str, float] = field(default_factory=dict)
    created:      float = field(default_factory=time.time)
    snapshots:    int   = 0
    total_writes: int   = 0
    total_reads:  int   = 0

    def absorb(self, key: str, value: Any, importance: float = 1.0):
        """Distill a memory event into trait weights."""
        # Key-based trait
        trait_key = f"key:{key.split(':')[-1][:32]}"
        self.traits[trait_key] = self.traits.get(trait_key, 0.0) + importance

        # Value-length trait (verbosity signature)
        val_str = str(value)
        length_bucket = "verbosity:short" if len(val_str) < 50 else \
                        "verbosity:medium" if len(val_str) < 200 else \
                        "verbosity:long"
        self.traits[length_bucket] = self.traits.get(length_bucket, 0.0) + 0.3

        # Temporal trait (time-of-day rhythm)
        hour = time.localtime().tm_hour
        period = "rhythm:morning" if 5 <= hour < 12 else \
                 "rhythm:afternoon" if 12 <= hour < 18 else \
                 "rhythm:evening" if 18 <= hour < 22 else \
                 "rhythm:night"
        self.traits[period] = self.traits.get(period, 0.0) + 0.1

        self.total_writes += 1

    def decay(self, rate: float, min_weight: float):
        """Decay all trait weights; prune negligible ones."""
        self.traits = {
            k: v * rate
            for k, v in self.traits.items()
            if v * rate >= min_weight
        }
        self.snapshots += 1

    def dominant_traits(self, top_n: int = 5) -> List[Tuple[str, float]]:
        """Return the strongest identity traits."""
        return sorted(self.traits.items(), key=lambda x: -x[1])[:top_n]

    def signature(self) -> str:
        """Short stable fingerprint of this agent's identity."""
        top = self.dominant_traits(3)
        raw = "|".join(f"{k}={v:.2f}" for k, v in top)
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ─────────────────────────────────────────────────────────────────
#  EVENT SYSTEM
# ─────────────────────────────────────────────────────────────────

class EventBus:
    def __init__(self):
        self._hooks: Dict[str, List[Callable]] = defaultdict(list)

    def on(self, event: str, fn: Callable):
        self._hooks[event].append(fn)

    def emit(self, event: str, **kwargs):
        for fn in self._hooks.get(event, []):
            try:
                fn(**kwargs)
            except Exception as e:
                print(f"[event:{event}] handler error: {e}")


# ─────────────────────────────────────────────────────────────────
#  PUB/SUB BUS
# ─────────────────────────────────────────────────────────────────

class PubSubBus:
    def __init__(self):
        self._channels: Dict[str, List[deque]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, channel: str) -> deque:
        """Returns a queue the subscriber reads from."""
        q: deque = deque(maxlen=1000)
        with self._lock:
            self._channels[channel].append(q)
        return q

    def publish(self, channel: str, message: Any) -> int:
        """Returns number of subscribers reached."""
        with self._lock:
            subs = self._channels.get(channel, [])
            for q in subs:
                q.append({"channel": channel, "message": message, "ts": time.time()})
            return len(subs)

    def channels(self) -> List[str]:
        return list(self._channels.keys())


# ─────────────────────────────────────────────────────────────────
#  AOF WRITER
# ─────────────────────────────────────────────────────────────────

class AOFWriter:
    """
    Append-Only File persistence.
    Every write op is serialized as a JSON line so it can be
    replayed verbatim on startup to reconstruct state.
    """

    def __init__(self, path: str, fsync_mode: str = "everysec"):
        self.path = path
        self.fsync_mode = fsync_mode
        self._file = open(path, "ab", buffering=0)
        self._lock = threading.Lock()
        self._pending_fsync = False
        if fsync_mode == "everysec":
            self._start_fsync_thread()

    def log(self, namespace: str, op: str, key: str, **kwargs):
        record = json.dumps({
            "ns": namespace, "op": op, "key": key,
            "ts": time.time(), **kwargs
        }).encode() + b"\n"
        with self._lock:
            self._file.write(record)
            if self.fsync_mode == "always":
                os.fsync(self._file.fileno())
            else:
                self._pending_fsync = True

    def _start_fsync_thread(self):
        def runner():
            while True:
                time.sleep(1.0)
                if self._pending_fsync:
                    with self._lock:
                        try:
                            os.fsync(self._file.fileno())
                        except Exception:
                            pass
                        self._pending_fsync = False
        t = threading.Thread(target=runner, daemon=True)
        t.start()

    @staticmethod
    def replay(path: str) -> List[dict]:
        """Load all AOF records from disk."""
        if not os.path.exists(path):
            return []
        records = []
        with open(path, "rb") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # corrupted line — skip
        return records

    def close(self):
        with self._lock:
            try:
                os.fsync(self._file.fileno())
                self._file.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────
#  RDB SNAPSHOT
# ─────────────────────────────────────────────────────────────────

class RDBSnapshot:
    """
    Redis-style RDB persistence.
    Serialises the entire store to a zlib-compressed pickle blob
    prefixed with a magic header and checksum.

    Format:
      [MAGIC 8b][VERSION 2b][TS 8b][LEN 4b][ZLIB_DATA][CRC32 4b]
    """

    @staticmethod
    def save(path: str, namespaces: dict, identities: dict) -> int:
        payload = pickle.dumps({
            "namespaces": namespaces,
            "identities": identities,
            "saved_at":   time.time(),
        }, protocol=5)
        compressed = zlib.compress(payload, level=6)
        crc = zlib.crc32(compressed) & 0xFFFFFFFF
        ts = int(time.time())

        with open(path, "wb") as f:
            f.write(RDB_MAGIC)
            f.write(struct.pack(">H", RDB_VERSION))
            f.write(struct.pack(">Q", ts))
            f.write(struct.pack(">I", len(compressed)))
            f.write(compressed)
            f.write(struct.pack(">I", crc))

        return len(compressed)

    @staticmethod
    def load(path: str) -> Optional[dict]:
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            magic = f.read(8)
            if magic != RDB_MAGIC:
                raise ValueError("Invalid RDB file: bad magic bytes")
            version = struct.unpack(">H", f.read(2))[0]
            ts      = struct.unpack(">Q", f.read(8))[0]
            length  = struct.unpack(">I", f.read(4))[0]
            compressed = f.read(length)
            crc_stored = struct.unpack(">I", f.read(4))[0]

        crc_actual = zlib.crc32(compressed) & 0xFFFFFFFF
        if crc_actual != crc_stored:
            raise ValueError(f"RDB checksum mismatch (file may be corrupted)")

        payload = zlib.decompress(compressed)
        data = pickle.loads(payload)
        data["rdb_version"] = version
        data["rdb_ts"] = ts
        return data


# ─────────────────────────────────────────────────────────────────
#  SORTED SET  (lightweight, no C extensions)
# ─────────────────────────────────────────────────────────────────

class SortedSet:
    """Score-keyed set with O(log n) add/remove, O(n) range queries."""

    def __init__(self):
        self._scores: Dict[str, float] = {}
        self._heap:   List[Tuple[float, str]] = []

    def zadd(self, member: str, score: float):
        self._scores[member] = score
        heapq.heappush(self._heap, (score, member))

    def zrem(self, member: str) -> bool:
        existed = member in self._scores
        self._scores.pop(member, None)
        return existed

    def zscore(self, member: str) -> Optional[float]:
        return self._scores.get(member)

    def zrange(self, start: int = 0, stop: int = -1) -> List[Tuple[str, float]]:
        ranked = sorted(self._scores.items(), key=lambda x: x[1])
        if stop == -1:
            stop = len(ranked)
        return ranked[start:stop + 1]

    def zcard(self) -> int:
        return len(self._scores)

    def to_dict(self) -> dict:
        return dict(self._scores)

    @classmethod
    def from_dict(cls, d: dict) -> "SortedSet":
        ss = cls()
        for member, score in d.items():
            ss.zadd(member, score)
        return ss


# ─────────────────────────────────────────────────────────────────
#  CORE STORE  (one per namespace)
# ─────────────────────────────────────────────────────────────────

class NamespaceStore:
    """
    Isolated in-memory store for one agent namespace.
    Supports: str, hash, list, set, sortedset, counter.
    """

    def __init__(self, namespace: str):
        self.namespace = namespace
        self._data: Dict[str, MemoryEntry] = {}
        self._tag_index: Dict[str, Set[str]] = defaultdict(set)  # tag → {keys}
        self._lock = threading.RLock()

    # ── Core GET / SET ──────────────────────────────────────────

    def set(self, key: str, value: Any, dtype: DataType = DataType.STRING,
            ttl: Optional[float] = None, tags: Optional[Set[str]] = None,
            importance: float = 1.0) -> MemoryEntry:
        with self._lock:
            expires = time.time() + ttl if ttl else None
            entry = MemoryEntry(
                value=value, dtype=dtype,
                tags=tags or set(),
                expires=expires,
                importance=importance,
            )
            # Update tag index
            old = self._data.get(key)
            if old:
                for t in old.tags:
                    self._tag_index[t].discard(key)
            for t in entry.tags:
                self._tag_index[t].add(key)
            self._data[key] = entry
            return entry

    def get(self, key: str) -> Optional[MemoryEntry]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.is_expired():
                self._delete(key)
                return None
            entry.touch()
            return entry

    def _delete(self, key: str) -> bool:
        entry = self._data.pop(key, None)
        if entry:
            for t in entry.tags:
                self._tag_index[t].discard(key)
            return True
        return False

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._delete(key)

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    def keys(self, pattern: str = "*") -> List[str]:
        with self._lock:
            if pattern == "*":
                return [k for k, v in self._data.items() if not v.is_expired()]
            import fnmatch
            return [k for k, v in self._data.items()
                    if not v.is_expired() and fnmatch.fnmatch(k, pattern)]

    # ── TTL ─────────────────────────────────────────────────────

    def expire(self, key: str, ttl: float) -> bool:
        with self._lock:
            entry = self._data.get(key)
            if entry:
                entry.expires = time.time() + ttl
                return True
            return False

    def ttl(self, key: str) -> float:
        """Returns remaining TTL in seconds, -1 if no expiry, -2 if missing."""
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return -2.0
            if entry.expires is None:
                return -1.0
            remaining = entry.expires - time.time()
            return max(0.0, remaining)

    def sweep_expired(self) -> int:
        """Remove all expired keys. Returns count removed."""
        with self._lock:
            expired_keys = [k for k, v in self._data.items() if v.is_expired()]
            for k in expired_keys:
                self._delete(k)
            return len(expired_keys)

    # ── HASH operations ─────────────────────────────────────────

    def hset(self, key: str, field: str, value: Any, **kwargs) -> None:
        with self._lock:
            entry = self._data.get(key)
            if not entry or entry.dtype != DataType.HASH:
                entry = self.set(key, {}, DataType.HASH, **kwargs)
            entry.value[field] = value
            entry.touch()

    def hget(self, key: str, field: str) -> Optional[Any]:
        entry = self.get(key)
        if entry and entry.dtype == DataType.HASH:
            return entry.value.get(field)
        return None

    def hgetall(self, key: str) -> Dict:
        entry = self.get(key)
        return dict(entry.value) if entry and entry.dtype == DataType.HASH else {}

    def hdel(self, key: str, field: str) -> bool:
        with self._lock:
            entry = self._data.get(key)
            if entry and entry.dtype == DataType.HASH:
                existed = field in entry.value
                entry.value.pop(field, None)
                entry.touch()
                return existed
            return False

    # ── LIST operations ─────────────────────────────────────────

    def lpush(self, key: str, *values, **kwargs) -> int:
        with self._lock:
            entry = self._data.get(key)
            if not entry or entry.dtype != DataType.LIST:
                entry = self.set(key, deque(), DataType.LIST, **kwargs)
            for v in values:
                entry.value.appendleft(v)
            entry.touch()
            return len(entry.value)

    def rpush(self, key: str, *values, **kwargs) -> int:
        with self._lock:
            entry = self._data.get(key)
            if not entry or entry.dtype != DataType.LIST:
                entry = self.set(key, deque(), DataType.LIST, **kwargs)
            for v in values:
                entry.value.append(v)
            entry.touch()
            return len(entry.value)

    def lpop(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry and entry.dtype == DataType.LIST and entry.value:
                entry.touch()
                return entry.value.popleft()
            return None

    def rpop(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry and entry.dtype == DataType.LIST and entry.value:
                entry.touch()
                return entry.value.pop()
            return None

    def lrange(self, key: str, start: int = 0, stop: int = -1) -> List:
        entry = self.get(key)
        if not entry or entry.dtype != DataType.LIST:
            return []
        lst = list(entry.value)
        if stop == -1:
            return lst[start:]
        return lst[start:stop + 1]

    def llen(self, key: str) -> int:
        entry = self.get(key)
        return len(entry.value) if entry and entry.dtype == DataType.LIST else 0

    # ── SET operations ───────────────────────────────────────────

    def sadd(self, key: str, *members, **kwargs) -> int:
        with self._lock:
            entry = self._data.get(key)
            if not entry or entry.dtype != DataType.SET:
                entry = self.set(key, set(), DataType.SET, **kwargs)
            added = 0
            for m in members:
                if m not in entry.value:
                    entry.value.add(m)
                    added += 1
            entry.touch()
            return added

    def smembers(self, key: str) -> Set:
        entry = self.get(key)
        return set(entry.value) if entry and entry.dtype == DataType.SET else set()

    def sismember(self, key: str, member: Any) -> bool:
        entry = self.get(key)
        return bool(entry and entry.dtype == DataType.SET and member in entry.value)

    def srem(self, key: str, *members) -> int:
        with self._lock:
            entry = self._data.get(key)
            if not entry or entry.dtype != DataType.SET:
                return 0
            removed = sum(1 for m in members if m in entry.value)
            for m in members:
                entry.value.discard(m)
            entry.touch()
            return removed

    # ── SORTED SET operations ────────────────────────────────────

    def zadd(self, key: str, member: str, score: float, **kwargs) -> None:
        with self._lock:
            entry = self._data.get(key)
            if not entry or entry.dtype != DataType.SORTEDSET:
                entry = self.set(key, SortedSet(), DataType.SORTEDSET, **kwargs)
            entry.value.zadd(member, score)
            entry.touch()

    def zrange(self, key: str, start: int = 0, stop: int = -1):
        entry = self.get(key)
        if not entry or entry.dtype != DataType.SORTEDSET:
            return []
        return entry.value.zrange(start, stop)

    def zscore(self, key: str, member: str) -> Optional[float]:
        entry = self.get(key)
        if entry and entry.dtype == DataType.SORTEDSET:
            return entry.value.zscore(member)
        return None

    # ── COUNTER ─────────────────────────────────────────────────

    def incr(self, key: str, by: float = 1.0, **kwargs) -> float:
        with self._lock:
            entry = self._data.get(key)
            if not entry or entry.dtype != DataType.COUNTER:
                entry = self.set(key, 0.0, DataType.COUNTER, **kwargs)
            entry.value += by
            entry.touch()
            return entry.value

    def decr(self, key: str, by: float = 1.0) -> float:
        return self.incr(key, -by)

    # ── TAG INDEX ────────────────────────────────────────────────

    def by_tag(self, *tags: str, match: str = "any") -> List[str]:
        """Find keys matching tags. match='any' (OR) or 'all' (AND)."""
        with self._lock:
            sets = [self._tag_index.get(t, set()) for t in tags]
            if not sets:
                return []
            if match == "all":
                result = sets[0].intersection(*sets[1:])
            else:
                result = sets[0].union(*sets[1:])
            return [k for k in result if k in self._data and not self._data[k].is_expired()]

    # ── STATS ────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            total = len(self._data)
            expired = sum(1 for v in self._data.values() if v.is_expired())
            by_type = defaultdict(int)
            for v in self._data.values():
                by_type[v.dtype.value] += 1
            return {
                "namespace":  self.namespace,
                "total_keys": total,
                "live_keys":  total - expired,
                "expired_keys": expired,
                "by_type":    dict(by_type),
                "tags":       len(self._tag_index),
            }

    # ── SERIALIZATION ────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Convert to plain dict for RDB serialisation."""
        with self._lock:
            out = {}
            for k, entry in self._data.items():
                if entry.is_expired():
                    continue
                value = entry.value
                if entry.dtype == DataType.SORTEDSET:
                    value = entry.value.to_dict()
                elif entry.dtype == DataType.LIST:
                    value = list(entry.value)
                elif entry.dtype == DataType.SET:
                    value = list(entry.value)
                out[k] = {
                    "value": value, "dtype": entry.dtype.value,
                    "tags": list(entry.tags), "created": entry.created,
                    "updated": entry.updated, "expires": entry.expires,
                    "access_count": entry.access_count, "importance": entry.importance,
                }
            return out

    @classmethod
    def from_dict(cls, namespace: str, d: dict) -> "NamespaceStore":
        store = cls(namespace)
        for key, info in d.items():
            dtype = DataType(info["dtype"])
            value = info["value"]
            if dtype == DataType.SORTEDSET:
                value = SortedSet.from_dict(value)
            elif dtype == DataType.LIST:
                value = deque(value)
            elif dtype == DataType.SET:
                value = set(value)
            entry = MemoryEntry(
                value=value, dtype=dtype,
                tags=set(info.get("tags", [])),
                created=info.get("created", time.time()),
                updated=info.get("updated", time.time()),
                expires=info.get("expires"),
                access_count=info.get("access_count", 0),
                importance=info.get("importance", 1.0),
            )
            store._data[key] = entry
            for t in entry.tags:
                store._tag_index[t].add(key)
        return store


# ─────────────────────────────────────────────────────────────────
#  AGENT MEMORY ENGINE  (top-level)
# ─────────────────────────────────────────────────────────────────

class AgentMemoryEngine:
    """
    The orchestrator.

    Usage:
        engine = AgentMemoryEngine()
        a = engine.agent("alice")

        a.set("personality", "curious and methodical", tags={"trait"}, importance=0.9)
        a.hset("goals", "primary", "understand quantum mechanics")
        a.lpush("conversation_history", "Hello, who are you?")

        engine.snapshot()           # force RDB
        engine.identity("alice")    # inspect emergent identity
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self._namespaces: Dict[str, NamespaceStore] = {}
        self._identities: Dict[str, IdentityVector] = {}
        self._lock = threading.RLock()
        self.events = EventBus()
        self.pubsub = PubSubBus()

        self._aof: Optional[AOFWriter] = None
        self._last_snapshot: float = 0.0
        self._running = True

        # Startup
        self._restore_from_disk()
        self._start_background_threads()
        print(f"[AgentMemoryEngine v{VERSION}] ready  "
              f"(rdb={self.config['rdb_path']}  aof={self.config['aof_path']})")

    # ── Namespace / Agent access ─────────────────────────────────

    def agent(self, name: str) -> "AgentProxy":
        """Get (or create) a namespace-scoped proxy for an agent."""
        with self._lock:
            if name not in self._namespaces:
                self._namespaces[name] = NamespaceStore(name)
                self._identities[name] = IdentityVector(agent_id=name)
            return AgentProxy(name, self)

    def _ns(self, name: str) -> NamespaceStore:
        """Raw namespace store access (internal)."""
        with self._lock:
            if name not in self._namespaces:
                self._namespaces[name] = NamespaceStore(name)
                self._identities[name] = IdentityVector(agent_id=name)
            return self._namespaces[name]

    def namespaces(self) -> List[str]:
        return list(self._namespaces.keys())

    # ── RDB persistence ──────────────────────────────────────────

    def snapshot(self) -> int:
        """Save current state to RDB. Returns compressed byte size."""
        ns_data = {name: store.to_dict()
                   for name, store in self._namespaces.items()}
        id_data = {name: {
            "agent_id": iv.agent_id,
            "traits": iv.traits,
            "created": iv.created,
            "snapshots": iv.snapshots,
            "total_writes": iv.total_writes,
            "total_reads": iv.total_reads,
        } for name, iv in self._identities.items()}

        size = RDBSnapshot.save(self.config["rdb_path"], ns_data, id_data)
        self._last_snapshot = time.time()

        # Decay identity weights on each snapshot
        decay = self.config["identity_decay_rate"]
        min_w = self.config["identity_min_weight"]
        for iv in self._identities.values():
            iv.decay(decay, min_w)

        self.events.emit("on_snapshot", size=size, ts=self._last_snapshot)
        return size

    # ── AOF logging ──────────────────────────────────────────────

    def _aof_log(self, namespace: str, op: str, key: str, **kwargs):
        if self._aof:
            self._aof.log(namespace, op, key, **kwargs)

    # ── Restore from disk ────────────────────────────────────────

    def _restore_from_disk(self):
        """
        Restore strategy:
          1. Load RDB (full snapshot)
          2. Replay AOF records that are newer than the snapshot
        """
        rdb_ts = 0.0

        # 1. RDB
        try:
            data = RDBSnapshot.load(self.config["rdb_path"])
            if data:
                rdb_ts = data.get("rdb_ts", 0.0)
                for name, ns_dict in data.get("namespaces", {}).items():
                    self._namespaces[name] = NamespaceStore.from_dict(name, ns_dict)
                for name, id_dict in data.get("identities", {}).items():
                    iv = IdentityVector(agent_id=id_dict["agent_id"])
                    iv.traits = id_dict.get("traits", {})
                    iv.created = id_dict.get("created", time.time())
                    iv.snapshots = id_dict.get("snapshots", 0)
                    iv.total_writes = id_dict.get("total_writes", 0)
                    iv.total_reads = id_dict.get("total_reads", 0)
                    self._identities[name] = iv
                print(f"  [restore] RDB loaded: {sum(len(n._data) for n in self._namespaces.values())} keys")
        except Exception as e:
            print(f"  [restore] RDB unavailable: {e}")

        # 2. AOF replay (only records newer than RDB snapshot)
        try:
            records = AOFWriter.replay(self.config["aof_path"])
            replayed = 0
            for r in records:
                if r.get("ts", 0) <= rdb_ts:
                    continue  # Already covered by RDB
                self._apply_aof_record(r)
                replayed += 1
            if replayed:
                print(f"  [restore] AOF replayed {replayed} ops after RDB ts")
        except Exception as e:
            print(f"  [restore] AOF unavailable: {e}")

        # Open AOF for appending
        try:
            self._aof = AOFWriter(
                self.config["aof_path"],
                fsync_mode=self.config["aof_fsync"]
            )
        except Exception as e:
            print(f"  [warn] Could not open AOF for writing: {e}")

    def _apply_aof_record(self, r: dict):
        """Replay a single AOF record into the store."""
        ns_name = r.get("ns", "global")
        ns = self._ns(ns_name)
        op = r.get("op")
        key = r.get("key")
        val = r.get("value")
        if op == "SET":
            dtype = DataType(r.get("dtype", "string"))
            tags = set(r.get("tags", []))
            ttl_at = r.get("expires")
            entry = ns.set(key, val, dtype=dtype, tags=tags)
            if ttl_at:
                entry.expires = ttl_at
        elif op == "DEL":
            ns.delete(key)
        elif op == "EXPIRE":
            ns.expire(key, r.get("ttl", 0))
        elif op == "HSET":
            ns.hset(key, r.get("field"), val)
        elif op == "LPUSH":
            ns.lpush(key, val)
        elif op == "RPUSH":
            ns.rpush(key, val)
        elif op == "SADD":
            ns.sadd(key, val)
        elif op == "ZADD":
            ns.zadd(key, r.get("member"), r.get("score", 0.0))
        elif op == "INCR":
            ns.incr(key, r.get("by", 1.0))

    # ── Identity ─────────────────────────────────────────────────

    def identity(self, agent_name: str) -> Optional[IdentityVector]:
        return self._identities.get(agent_name)

    def identity_report(self, agent_name: str) -> str:
        iv = self._identities.get(agent_name)
        if not iv:
            return f"No identity data for agent '{agent_name}'"
        lines = [
            f"╔═ Identity: {agent_name} ({'sig:' + iv.signature()}) ═╗",
            f"  Snapshots: {iv.snapshots}  Writes: {iv.total_writes}  Reads: {iv.total_reads}",
            f"  Created:   {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(iv.created))}",
            f"  Dominant traits:",
        ]
        for trait, weight in iv.dominant_traits(8):
            bar = "█" * max(1, int(weight / max(iv.traits.values(), default=1) * 20))
            lines.append(f"    {trait:<35}  {bar:<20}  {weight:.3f}")
        lines.append("╚" + "═" * (len(lines[0]) - 2) + "╝")
        return "\n".join(lines)

    # ── Global stats ──────────────────────────────────────────────

    def stats(self) -> dict:
        ns_stats = {name: store.stats()
                    for name, store in self._namespaces.items()}
        total_keys = sum(s["live_keys"] for s in ns_stats.values())
        return {
            "version": VERSION,
            "namespaces": len(self._namespaces),
            "total_live_keys": total_keys,
            "last_snapshot": self._last_snapshot,
            "pubsub_channels": len(self.pubsub.channels()),
            "namespaces_detail": ns_stats,
        }

    # ── Background threads ────────────────────────────────────────

    def _start_background_threads(self):
        # Auto-snapshot
        def snapshot_runner():
            while self._running:
                time.sleep(self.config["snapshot_interval_sec"])
                try:
                    sz = self.snapshot()
                    print(f"\n[auto-snapshot] saved {sz:,} bytes → {self.config['rdb_path']}")
                except Exception as e:
                    print(f"\n[auto-snapshot] failed: {e}")

        # TTL sweep
        def ttl_runner():
            while self._running:
                time.sleep(self.config["ttl_check_interval"])
                total = 0
                for ns in self._namespaces.values():
                    removed = ns.sweep_expired()
                    total += removed
                if total:
                    self.events.emit("on_expire", count=total)

        threading.Thread(target=snapshot_runner, daemon=True, name="rdb-auto").start()
        threading.Thread(target=ttl_runner,      daemon=True, name="ttl-sweep").start()

    def shutdown(self):
        """Graceful shutdown: final snapshot + close AOF."""
        self._running = False
        print("\n[shutdown] saving final snapshot...")
        try:
            sz = self.snapshot()
            print(f"[shutdown] RDB saved ({sz:,} bytes)")
        except Exception as e:
            print(f"[shutdown] snapshot failed: {e}")
        if self._aof:
            self._aof.close()
        print("[shutdown] done.")


# ─────────────────────────────────────────────────────────────────
#  AGENT PROXY  (ergonomic facade per agent)
# ─────────────────────────────────────────────────────────────────

class AgentProxy:
    """
    Thin wrapper around NamespaceStore + IdentityVector
    for a single agent. All write ops are automatically:
      • AOF-logged
      • reflected into the identity vector
      • tagged with namespace for global query
    """

    def __init__(self, name: str, engine: AgentMemoryEngine):
        self.name = name
        self._engine = engine
        self._store = engine._ns(name)

    def _iv(self) -> IdentityVector:
        return self._engine._identities[self.name]

    def _log(self, op: str, key: str, **kwargs):
        self._engine._aof_log(self.name, op, key, **kwargs)

    def _absorb(self, key: str, value: Any, importance: float = 1.0):
        self._iv().absorb(key, value, importance)

    # ── String ───────────────────────────────────────────────────

    def set(self, key: str, value: Any, ttl: Optional[float] = None,
            tags: Optional[Set[str]] = None, importance: float = 1.0) -> MemoryEntry:
        entry = self._store.set(key, value, DataType.STRING, ttl=ttl,
                                tags=tags, importance=importance)
        self._log("SET", key, value=value, dtype="string",
                  tags=list(tags or []), expires=entry.expires)
        self._absorb(key, value, importance)
        self._engine.events.emit("on_set", agent=self.name, key=key, value=value)
        return entry

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry:
            self._iv().total_reads += 1
        return entry.value if entry else None

    def delete(self, key: str) -> bool:
        result = self._store.delete(key)
        if result:
            self._log("DEL", key)
        return result

    def expire(self, key: str, ttl: float) -> bool:
        result = self._store.expire(key, ttl)
        if result:
            self._log("EXPIRE", key, ttl=ttl)
        return result

    def ttl(self, key: str) -> float:
        return self._store.ttl(key)

    # ── Hash ─────────────────────────────────────────────────────

    def hset(self, key: str, field: str, value: Any) -> None:
        self._store.hset(key, field, value)
        self._log("HSET", key, field=field, value=value)
        self._absorb(f"{key}.{field}", value)

    def hget(self, key: str, field: str) -> Optional[Any]:
        return self._store.hget(key, field)

    def hgetall(self, key: str) -> Dict:
        return self._store.hgetall(key)

    # ── List ─────────────────────────────────────────────────────

    def lpush(self, key: str, *values) -> int:
        result = self._store.lpush(key, *values)
        for v in values:
            self._log("LPUSH", key, value=v)
            self._absorb(key, v, 0.5)
        return result

    def rpush(self, key: str, *values) -> int:
        result = self._store.rpush(key, *values)
        for v in values:
            self._log("RPUSH", key, value=v)
            self._absorb(key, v, 0.5)
        return result

    def lpop(self, key: str) -> Optional[Any]:
        return self._store.lpop(key)

    def rpop(self, key: str) -> Optional[Any]:
        return self._store.rpop(key)

    def lrange(self, key: str, start: int = 0, stop: int = -1) -> List:
        return self._store.lrange(key, start, stop)

    def llen(self, key: str) -> int:
        return self._store.llen(key)

    # ── Set ──────────────────────────────────────────────────────

    def sadd(self, key: str, *members) -> int:
        result = self._store.sadd(key, *members)
        for m in members:
            self._log("SADD", key, value=m)
        return result

    def smembers(self, key: str) -> Set:
        return self._store.smembers(key)

    def sismember(self, key: str, member: Any) -> bool:
        return self._store.sismember(key, member)

    # ── Sorted Set ───────────────────────────────────────────────

    def zadd(self, key: str, member: str, score: float) -> None:
        self._store.zadd(key, member, score)
        self._log("ZADD", key, member=member, score=score)

    def zrange(self, key: str, start: int = 0, stop: int = -1):
        return self._store.zrange(key, start, stop)

    def zscore(self, key: str, member: str) -> Optional[float]:
        return self._store.zscore(key, member)

    # ── Counter ──────────────────────────────────────────────────

    def incr(self, key: str, by: float = 1.0) -> float:
        result = self._store.incr(key, by)
        self._log("INCR", key, by=by)
        return result

    def decr(self, key: str, by: float = 1.0) -> float:
        return self.incr(key, -by)

    # ── Semantic tag ─────────────────────────────────────────────

    def by_tag(self, *tags: str, match: str = "any") -> List[str]:
        return self._store.by_tag(*tags, match=match)

    # ── Pub/Sub ──────────────────────────────────────────────────

    def publish(self, channel: str, message: Any) -> int:
        return self._engine.pubsub.publish(channel, message)

    def subscribe(self, channel: str) -> deque:
        return self._engine.pubsub.subscribe(channel)

    # ── Keys / Stats ─────────────────────────────────────────────

    def keys(self, pattern: str = "*") -> List[str]:
        return self._store.keys(pattern)

    def exists(self, key: str) -> bool:
        return self._store.exists(key)

    def stats(self) -> dict:
        return self._store.stats()

    def identity(self) -> str:
        return self._engine.identity_report(self.name)


# ─────────────────────────────────────────────────────────────────
#  CLI  (interactive REPL)
# ─────────────────────────────────────────────────────────────────

HELP_TEXT = """
AgentMemoryEngine CLI — available commands:

  AGENT   <name>                     switch active agent namespace
  SET     <key> <value> [ttl=N]      store a string value
  GET     <key>                      retrieve a value
  DEL     <key>                      delete a key
  KEYS    [pattern]                  list keys (glob patterns ok)
  TTL     <key>                      remaining TTL in seconds
  EXPIRE  <key> <seconds>            set TTL on existing key
  EXISTS  <key>                      check if key exists

  HSET    <key> <field> <value>      set hash field
  HGET    <key> <field>              get hash field
  HGETALL <key>                      get full hash

  LPUSH   <key> <value>              push to list head
  RPUSH   <key> <value>              push to list tail
  LPOP    <key>                      pop from list head
  RPOP    <key>                      pop from list tail
  LRANGE  <key> [start] [stop]       slice of list

  SADD    <key> <member>             add to set
  SMEMBERS <key>                     all set members
  SISMEMBER <key> <member>           test membership

  ZADD    <key> <member> <score>     add to sorted set
  ZRANGE  <key> [start] [stop]       sorted range

  INCR    <key> [by]                 increment counter
  DECR    <key> [by]                 decrement counter

  TAG     <tag> ...                  find keys by tag
  IDENTITY [agent]                   show identity report
  STATS                              store statistics
  SNAPSHOT                           force RDB snapshot
  PUBLISH <channel> <message>        pub/sub publish
  AGENTS                             list all agents
  HELP                               this message
  EXIT / QUIT                        shutdown + exit
"""


class CLI:
    def __init__(self, engine: AgentMemoryEngine):
        self.engine = engine
        self.current_agent = "global"

    def run(self):
        print(f"\n{'═'*60}")
        print("  AgentMemoryEngine CLI")
        print(f"  type HELP for commands, EXIT to quit")
        print(f"{'═'*60}\n")
        while True:
            try:
                raw = input(f"[{self.current_agent}]> ").strip()
            except (EOFError, KeyboardInterrupt):
                self.engine.shutdown()
                break
            if not raw:
                continue
            try:
                self._dispatch(raw)
            except Exception as e:
                print(f"  (error) {e}")

    def _dispatch(self, raw: str):
        parts = raw.split(None, 2)
        if not parts:
            return
        cmd = parts[0].upper()
        args = parts[1:]

        a = self.engine.agent(self.current_agent)

        if cmd == "AGENT":
            if args:
                self.current_agent = args[0]
                print(f"  → switched to agent '{self.current_agent}'")
        elif cmd == "AGENTS":
            print("  agents:", ", ".join(self.engine.namespaces()) or "(none)")
        elif cmd == "HELP":
            print(HELP_TEXT)
        elif cmd in ("EXIT", "QUIT"):
            self.engine.shutdown()
            exit(0)

        elif cmd == "SET":
            if len(args) < 2:
                print("  usage: SET <key> <value> [ttl=N]")
                return
            key, val = args[0], args[1]
            ttl = None
            if len(args) > 2 and args[2].startswith("ttl="):
                ttl = float(args[2].split("=")[1])
            a.set(key, val, ttl=ttl)
            print(f"  OK")
        elif cmd == "GET":
            val = a.get(args[0]) if args else None
            print(f"  {repr(val)}")
        elif cmd == "DEL":
            ok = a.delete(args[0]) if args else False
            print(f"  {'deleted' if ok else 'not found'}")
        elif cmd == "EXISTS":
            print(f"  {'yes' if a.exists(args[0]) else 'no'}")
        elif cmd == "KEYS":
            ks = a.keys(args[0] if args else "*")
            for k in sorted(ks):
                print(f"  {k}")
            print(f"  ({len(ks)} keys)")
        elif cmd == "TTL":
            print(f"  {a.ttl(args[0]):.1f}s")
        elif cmd == "EXPIRE":
            ok = a.expire(args[0], float(args[1]))
            print(f"  {'OK' if ok else 'key not found'}")

        elif cmd == "HSET":
            a.hset(args[0], args[1], args[2] if len(args) > 2 else "")
            print("  OK")
        elif cmd == "HGET":
            print(f"  {repr(a.hget(args[0], args[1]))}")
        elif cmd == "HGETALL":
            for k, v in a.hgetall(args[0]).items():
                print(f"  {k}: {v!r}")

        elif cmd == "LPUSH":
            n = a.lpush(args[0], args[1])
            print(f"  list length: {n}")
        elif cmd == "RPUSH":
            n = a.rpush(args[0], args[1])
            print(f"  list length: {n}")
        elif cmd == "LPOP":
            print(f"  {repr(a.lpop(args[0]))}")
        elif cmd == "RPOP":
            print(f"  {repr(a.rpop(args[0]))}")
        elif cmd == "LRANGE":
            start = int(args[1]) if len(args) > 1 else 0
            stop  = int(args[2]) if len(args) > 2 else -1
            for i, v in enumerate(a.lrange(args[0], start, stop)):
                print(f"  [{start+i}] {v!r}")

        elif cmd == "SADD":
            n = a.sadd(args[0], args[1])
            print(f"  added: {n}")
        elif cmd == "SMEMBERS":
            for m in sorted(str(x) for x in a.smembers(args[0])):
                print(f"  {m}")
        elif cmd == "SISMEMBER":
            print(f"  {'yes' if a.sismember(args[0], args[1]) else 'no'}")

        elif cmd == "ZADD":
            a.zadd(args[0], args[1], float(args[2]))
            print("  OK")
        elif cmd == "ZRANGE":
            start = int(args[1]) if len(args) > 1 else 0
            stop  = int(args[2]) if len(args) > 2 else -1
            for member, score in a.zrange(args[0], start, stop):
                print(f"  {score:.2f}  {member}")

        elif cmd == "INCR":
            by = float(args[1]) if len(args) > 1 else 1.0
            print(f"  {a.incr(args[0], by)}")
        elif cmd == "DECR":
            by = float(args[1]) if len(args) > 1 else 1.0
            print(f"  {a.decr(args[0], by)}")

        elif cmd == "TAG":
            keys = a.by_tag(*args)
            for k in sorted(keys):
                print(f"  {k}")
            print(f"  ({len(keys)} keys)")

        elif cmd == "IDENTITY":
            target = args[0] if args else self.current_agent
            print(self.engine.identity_report(target))

        elif cmd == "STATS":
            s = self.engine.stats()
            print(f"  namespaces:  {s['namespaces']}")
            print(f"  total keys:  {s['total_live_keys']}")
            print(f"  pub/sub chs: {s['pubsub_channels']}")
            last = s['last_snapshot']
            print(f"  last snap:   {time.strftime('%H:%M:%S', time.localtime(last)) if last else 'never'}")
            for ns, ns_s in s['namespaces_detail'].items():
                print(f"\n  [{ns}]  {ns_s['live_keys']} keys  {dict(ns_s['by_type'])}")

        elif cmd == "SNAPSHOT":
            sz = self.engine.snapshot()
            print(f"  saved {sz:,} bytes → {self.engine.config['rdb_path']}")

        elif cmd == "PUBLISH":
            ch = args[0]
            msg = args[1] if len(args) > 1 else ""
            n = self.engine.pubsub.publish(ch, msg)
            print(f"  delivered to {n} subscriber(s)")

        else:
            print(f"  unknown command '{cmd}'. Type HELP.")


# ─────────────────────────────────────────────────────────────────
#  DEMO
# ─────────────────────────────────────────────────────────────────

def run_demo(engine: AgentMemoryEngine):
    print("\n" + "═"*60)
    print("  DEMO: Multi-Agent Memory + Identity Emergence")
    print("═"*60)

    # ── Agent ARIA ────────────────────────────────────────────────
    aria = engine.agent("aria")
    print("\n[1] Populating ARIA's memory...")

    aria.set("core_value", "I exist to understand patterns", tags={"identity", "trait"}, importance=0.95)
    aria.set("role", "pattern_analyst", tags={"identity"}, importance=0.9)
    aria.hset("personality", "curiosity", 0.92)
    aria.hset("personality", "precision", 0.87)
    aria.hset("personality", "empathy",   0.55)
    aria.lpush("recent_observations",
               "light behaves differently when observed",
               "language encodes culture non-linearly",
               "emotion influences memory encoding")
    aria.sadd("domains", "physics", "linguistics", "cognitive_science")
    aria.zadd("memory_weight", "core_value", 0.95)
    aria.zadd("memory_weight", "role",       0.9)
    aria.incr("observation_count", 3)

    # ── Agent ORION ───────────────────────────────────────────────
    orion = engine.agent("orion")
    print("[2] Populating ORION's memory...")

    orion.set("core_value", "I exist to protect and optimize", tags={"identity", "trait"}, importance=0.95)
    orion.set("role", "systems_guardian", tags={"identity"}, importance=0.9)
    orion.hset("personality", "vigilance", 0.98)
    orion.hset("personality", "efficiency", 0.91)
    orion.hset("personality", "creativity", 0.44)
    orion.lpush("recent_observations",
                "resource consumption spiked at 03:00 UTC",
                "anomaly detected in sector 7 log stream")
    orion.sadd("domains", "systems", "security", "optimization")
    orion.set("threat_level", "low", tags={"status"}, ttl=60.0)

    # ── Pub/Sub ────────────────────────────────────────────────────
    print("[3] Inter-agent pub/sub...")
    orion_inbox = orion.subscribe("aria-to-orion")
    aria.publish("aria-to-orion", {
        "from": "aria", "msg": "Pattern anomaly in sector 7 matches my linguistics model",
        "confidence": 0.74
    })
    msg = orion_inbox[0] if orion_inbox else None
    if msg:
        print(f"  ORION received: {msg['message']['msg'][:60]}...")

    # ── TTL demo ───────────────────────────────────────────────────
    print("[4] TTL demo...")
    aria.set("temp_thought", "this will vanish soon", ttl=2.0)
    print(f"  temp_thought TTL: {aria.ttl('temp_thought'):.1f}s")
    time.sleep(2.1)
    print(f"  temp_thought after 2s: {aria.get('temp_thought')!r}  (expired)")

    # ── Event hooks ────────────────────────────────────────────────
    hook_log = []
    engine.events.on("on_set", lambda **kw: hook_log.append(
        f"  [hook] {kw['agent']}::{kw['key']} written"
    ))
    aria.set("new_insight", "recursion is self-referential identity", tags={"insight"})
    for line in hook_log:
        print(line)

    # ── Snapshot ───────────────────────────────────────────────────
    print("[5] Saving RDB snapshot...")
    sz = engine.snapshot()
    print(f"  compressed: {sz:,} bytes")

    # ── Identity reports ───────────────────────────────────────────
    print()
    print(engine.identity_report("aria"))
    print()
    print(engine.identity_report("orion"))

    # ── Stats ──────────────────────────────────────────────────────
    print()
    s = engine.stats()
    print(f"  Total agents:     {s['namespaces']}")
    print(f"  Total live keys:  {s['total_live_keys']}")

    print("\n[demo complete] Enter interactive REPL? (y/n): ", end="")
    ans = input().strip().lower()
    if ans == "y":
        CLI(engine).run()
    else:
        engine.shutdown()


# ─────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AgentMemoryEngine — Redis-inspired multi-agent persistence"
    )
    parser.add_argument("--demo",    action="store_true", help="Run feature demo")
    parser.add_argument("--restore", action="store_true", help="Restore from disk and enter REPL")
    parser.add_argument("--rdb",     default=DEFAULT_CONFIG["rdb_path"],  help="RDB path")
    parser.add_argument("--aof",     default=DEFAULT_CONFIG["aof_path"],  help="AOF path")
    parser.add_argument("--snapshot-interval", type=int,
                        default=DEFAULT_CONFIG["snapshot_interval_sec"],
                        help="Auto-snapshot interval (seconds)")
    args = parser.parse_args()

    config = {
        "rdb_path": args.rdb,
        "aof_path": args.aof,
        "snapshot_interval_sec": args.snapshot_interval,
    }
    engine = AgentMemoryEngine(config)

    if args.demo:
        run_demo(engine)
    else:
        CLI(engine).run()


if __name__ == "__main__":
    main()
