# AgentMemoryEngine RS

> **A Redis-inspired local memory runtime for multi-agent systems**  
> Persistent state. Recursive memory. Emergent identity. Live embodied telemetry.

![Rust](https://img.shields.io/badge/Rust-systems%20runtime-black?logo=rust)
![Status](https://img.shields.io/badge/status-experimental-orange)
![Persistence](https://img.shields.io/badge/persistence-RDB%20%2B%20AOF-blue)
![Mode](https://img.shields.io/badge/mode-local--first-brightgreen)

AgentMemoryEngine RS is a **Rust-based memory kernel for agents**.
It combines a namespaced key-value store, append-only persistence, recursive “nesting doll” memory, identity drift over time, and a live RuView subsystem that writes simulated body-state into the same memory fabric.

This is not just a storage toy. It is the beginning of a **continuity engine** for multi-agent systems.

---

## Why this project is interesting

Most agent stacks treat memory as an afterthought: a vector DB, a few summaries, and some scratch state.

AgentMemoryEngine RS goes in a different direction:

- **Per-agent namespaces** let multiple minds coexist without collapsing into one blob
- **Recursive memory structures** allow layered internal state instead of flat text chunks
- **Identity vectors** absorb usage patterns and decay over time
- **RDB + AOF persistence** preserves continuity across restarts
- **RuView** feeds live signal-derived state into memory as if perception were just another substrate

The result feels closer to an **agent operating layer** than a simple database.

---

## Core capabilities

### Multi-agent memory namespaces
Each agent gets its own isolated memory store, with an ergonomic `AgentProxy` interface for reads, writes, counters, tags, sorted sets, lists, hashes, TTLs, and nested doll paths.

### Recursive “Nesting Doll” memory
The `DollValue` type supports nested maps of nested values, allowing deep structures like:

- perception
- cognition
- emotion
- body state
- world state
- relationship state

This makes the memory model much more natural for agent continuity than a flat key/value layer.

### Emergent identity vectors
Each namespace develops an identity profile based on write behavior:

- key usage
- verbosity
- nesting depth
- value types
- temporal rhythm

Traits decay at snapshot time, so identity is dynamic rather than hardcoded.

### Local-first durability
State is preserved with:

- **RDB snapshots** for compact full-state saves
- **AOF logging** for operation replay and crash recovery

### RuView body-state subsystem
RuView runs as a background daemon that continuously samples 19 body zones, computes signal features, stores them in the memory substrate, renders an ASCII body map, and exposes live zone stats through the CLI.

---

## Architecture at a glance

```text
┌─────────────────────────────────────────────────────────────┐
│                    AgentMemoryEngine RS                     │
├─────────────────────────────────────────────────────────────┤
│  Engine / Orchestrator                                     │
│  - namespaces                                               │
│  - identity vectors                                         │
│  - event hooks                                              │
│  - pub/sub                                                  │
│  - background snapshotting                                  │
│  - TTL sweeping                                             │
│  - RuView daemon                                            │
├─────────────────────────────────────────────────────────────┤
│  Storage Layer                                              │
│  - strings / floats                                         │
│  - hashes / lists / sets / sorted sets                      │
│  - recursive Doll memory                                    │
│  - tags / TTL / stats                                       │
├─────────────────────────────────────────────────────────────┤
│  Persistence Layer                                          │
│  - RDB snapshot                                             │
│  - AOF append-only log                                      │
│  - restore + replay                                         │
├─────────────────────────────────────────────────────────────┤
│  Identity Layer                                             │
│  - trait absorption                                         │
│  - decay                                                    │
│  - dominant traits                                          │
│  - fingerprint / report                                     │
├─────────────────────────────────────────────────────────────┤
│  Perception Layer                                           │
│  - RuView signal sampler                                    │
│  - body-zone doll writes                                    │
│  - ASCII map + summary stats                                │
└─────────────────────────────────────────────────────────────┘
```

---

## Project layout

```text
.
├── main.rs         # entry point, demo mode, CLI bootstrapping
├── cli.rs          # interactive REPL and command dispatch
├── engine.rs       # orchestrator, namespaces, identity, hooks, background threads
├── store.rs        # thread-safe namespaced KV store + tags + TTL + collections
├── models.rs       # DollValue, MemoryEntry, core types
├── persistence.rs  # RDB snapshot + AOF logging/replay
├── identity.rs     # emergent identity vectors and reporting
└── ruview.rs       # live signal model + body-map subsystem
```

---

## Quick start

### Run the interactive REPL

```bash
cargo run --release
```

### Run the full feature demo

```bash
cargo run --release -- --demo
```

### Restore from disk and open the REPL

```bash
cargo run --release -- --restore
```

### Disable RuView

```bash
cargo run --release -- --no-ruview
```

### Use custom persistence files

```bash
cargo run --release -- --rdb memory.rdb --aof memory.aof
```

---

## CLI highlights

The REPL includes support for:

### Core KV + TTL
```text
SET <key> <value> [ttl=N]
GET <key>
DEL <key>
EXISTS <key>
KEYS [pattern]
TTL <key>
EXPIRE <key> <secs>
```

### Structured data
```text
HSET / HGET / HGETALL
LPUSH / RPUSH / LPOP / RPOP / LRANGE / LLEN
SADD / SMEMBERS / SISMEMBER
ZADD / ZRANGE / ZSCORE
INCR / DECR
```

### Recursive doll memory
```text
DOLL <key> <path> <value>
DOLLGET <key> <path>
DOLLSHOW <key>
```

### Agent and engine introspection
```text
AGENT <name>
AGENTS
TAG <tag1> [tag2...]
IDENTITY [agent]
STATS
SNAPSHOT
PUBLISH <channel> <message>
```

### RuView commands
```text
RUVIEW
RUZONE <zone_name>
RUSTATS
```

---

## Demo walkthrough

The built-in demo does more than print a few values. It demonstrates:

- creation of multiple agents
- identity-tagged writes
- nested doll construction
- counters, sets, lists, and sorted sets
- TTL expiry
- event hooks
- RDB snapshotting
- identity reports
- RuView startup and live stats

That makes the demo a great first stop if you want to understand the system quickly.

---

## RuView

RuView is the wild part.

It is a background subsystem that models Wi‑Fi/CSI-style body sensing as memory-native state. On each scan tick it:

- samples 19 body zones
- computes RSSI, phase shift, CSI magnitude, and motion score
- writes zone data into a nested `body` doll
- renders an ASCII body map
- stores summary values like `avg_rssi_dbm`, `avg_motion_score`, `breath_score`, and `scan_tick`

### Current RuView status
RuView is currently a **simulated signal model**, not a hardware-backed sensing pipeline.
That is a feature for experimentation and visualization, but also a clear future upgrade path.

### The path forward
A serious next step is to evolve RuView from:

- **mock zone sampler**

to:

- **real sensor adapter + feature extractor + state estimator + memory publisher**

That would turn RuView into a genuine embodied-state feed rather than a sandboxed concept.

---

## Example mental model

Think of this project as:

- **Redis** for fast local primitives
- **a recursive internal state tree** for richer memory
- **a proto self-model** via identity vectors
- **a body-state bus** via RuView

That combination is what gives it its own personality.

---

## Current strengths

- clean module boundaries
- strong conceptual coherence
- local-first persistence model
- expressive recursive memory type
- fun, usable REPL
- compelling demo story
- identity as an emergent property, not a config file
- RuView already integrated into the same memory substrate instead of bolted on

---

## Current caveats / honest notes

This project is already impressive, but it is still clearly **prototype-stage** in a few important ways:

- the current sorted-set representation is member-ordered rather than truly score-ordered
- the AOF has no rewrite/compaction pass yet
- RuView is simulated rather than hardware-backed
- some CLI and metadata behaviors could be tightened further

That is not a weakness of the idea. It just means the architecture is ahead of some implementation details.

---

## Roadmap ideas

### Memory engine
- real score-ordered sorted sets
- AOF rewrite / compaction
- stronger conflict resolution and contradiction handling
- salience and retrieval ranking
- memory compression / summarization

### Identity layer
- more stable identity fingerprinting
- belief / preference / relationship modeling
- episodic identity summaries

### RuView
- sensor adapter abstraction
- replay mode for captured traces
- hardware-backed CSI ingestion
- confidence scores and calibration state
- browser-based live dashboard

### Platform
- JSON export/import for long-term archives
- richer pub/sub event streams
- plugin system for external perceptual inputs

---

## Who this is for

This repo will be especially interesting to people exploring:

- local-first AI systems
- multi-agent architectures
- long-term memory for LLM-based agents
- embodied or semi-embodied assistants
- experimental cognitive runtimes in Rust

---

## Suggested positioning

If you show this off publicly, the strongest framing is not:

> “Here’s my database.”

It is:

> “Here’s a local continuity engine for multi-agent memory, identity, and embodied state.”

That framing matches what the code is already trying to become.

---

## Example README tagline options

Pick whichever vibe fits your repo best:

1. **A local-first memory kernel for multi-agent systems**
2. **Recursive memory, emergent identity, and embodied state for agents**
3. **A continuity engine for multi-agent AI in Rust**
4. **Redis-inspired memory infrastructure for stateful agents**
5. **A Rust runtime for persistent agent memory and RuView telemetry**

---

## Status

**Experimental, promising, and unusually coherent.**

This repo already has the shape of something bigger than a toy.

If the next iterations strengthen retrieval, durability tooling, and real-world RuView ingestion, this could become a genuinely distinctive foundation for local multi-agent systems.

---

## License

Add your license here.

```text
MIT
Apache-2.0
Proprietary
TBD
```

---

## Final note

If you are into long-term AI memory, local-first systems, and weird-but-serious experiments in agent continuity:

**this repo is worth watching.**
