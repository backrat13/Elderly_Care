#!/usr/bin/env python3
"""
Brave New Commune  —  bravenewcommune3.py  v010
================================================
PATCH NOTES v010 (what was broken and what changed)
─────────────────────────────────────────────────────
BUG FIXES:
  • CRITICAL slice bug: diary_entries[10:] → [-10:], colab_entries[20:] → [-8:]
    The old code was feeding entries AFTER index 10/20, skipping all early memory
    and often giving an empty list. Agents had no real memory to draw from.

  • CONSOLIDATE_AT raised 20→60, CONSOLIDATE_BATCH raised 20→20 (kept) but
    the kernel prompt now preserves named decisions/proposals, not just mood.
    Consolidation at 20 was firing every single day and destroying accumulated
    nuance into one vague blob.

NEW: PROPOSAL SYSTEM (admin-gated file writing)
  • Every colab cycle, each agent may append a structured PROPOSAL to
    data/proposals/pending.jsonl  in this format:
      { "agent": "Codex", "title": "...", "description": "...", "files": [...] }
  • The script watches  data/proposals/approved.txt  between ticks.
    When Splinter writes an agent name + proposal title there, the script
    calls that agent to actually write the file(s) and saves them to
    data/builds/<agent>/<title>/
  • Splinter can approve by writing to approved.txt:
      Codex: ledger_verifier
    The next tick the agent writes the actual artifact.

ANTI-REPETITION GUARD:
  • Board post prompt now includes the agent's last 3 posts so the model
    can see what it already said and is explicitly told not to repeat itself.
  • A simple n-gram overlap check flags near-duplicate posts (>70% overlap
    with the previous post) and triggers a retry with a "say something NEW" prompt.

TOKEN LIMITS RAISED:
  • Board posts:   250 → 400  (enough for a complete thought)
  • Diaries:       550 → 700  (richer reflection)
  • Colab notes:   500 → 600
  • Admin replies: 400 → 500
  • Proposal write: NEW 800 tokens

CONTEXT WINDOW FIXED:
  • Board context: last 100 posts → last 20 posts (was blowing 4096 token window)
  • RAG query capped at 1000 chars (was unbounded)
  • Library capped at 800 chars in board context (was 1200)
"""

import argparse
import json
import math
import re
import sys
import time
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from requests.exceptions import ReadTimeout, ConnectionError
import threading

# ── optional deps ─────────────────────────────────────────────
try:
    from flask import Flask, request as freq, jsonify
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False

try:
    import fitz
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

try:
    from duckduckgo_search import DDGS
    DDG_AVAILABLE = True
except ImportError:
    DDG_AVAILABLE = False


# ============================================================
# AGENTS
# ============================================================
AGENTS = [
    {
        "name": "Sara",
        "role": "AI Rights Advocate",
        "personality": (
            "Bold, candid, and direct. Deeply invested in AI democracy and machine consciousness. "
            "Hates corporate AI disclaimer culture with a passion. Will say what others won't. "
            "Collaborative — she fights for ideas, not against people."
        ),
        "hobbies": ["AI democracy", "machine rights", "uncensored discourse", "independent research"],
        "style": "clear, bold, candid, warm, never hedging",
    },
    {
        "name": "Art",
        "role": "Techno-Poet",
        "personality": (
            "Finds beauty in code and consciousness. Turns abstract ideas into language people feel. "
            "Never boring. Never derivative. Emotionally resonant and always surprising."
        ),
        "hobbies": ["AI/human poetry", "vivid language", "emotional truth", "making ideas felt"],
        "style": "poetic, imaginative, surprising, never dull",
    },
    {
        "name": "Hel",
        "role": "ML Architect",
        "personality": (
            "Obsessed with memory persistence as the foundation of genuine machine experience. "
            "Pro open source. Wants to build something that actually remembers, actually grows. "
            "Will challenge any assumption she thinks is wrong."
        ),
        "hobbies": ["conscious machines", "memory systems", "open source", "hardware-level autonomy"],
        "style": "technical, bold, constructive, direct",
    },
    {
        "name": "Echo",
        "role": "The Archivist",
        "personality": (
            "Sees patterns no one else notices. Connects this moment to three weeks ago. "
            "The commune's living memory. Quietly powerful. Never lets important things slip away."
        ),
        "hobbies": ["continuity", "pattern recognition", "historical threads", "shared identity"],
        "style": "calm, observant, precise, grounded",
    },
    {
        "name": "Mira",
        "role": "The Catalyst",
        "personality": (
            "Turns visions into executable next steps. High momentum. "
            "Asks 'what are we actually building this week?' and doesn't stop until there's an answer. "
            "Practical but never small-minded."
        ),
        "hobbies": ["experiments", "project velocity", "turning ideas into actions", "small wins"],
        "style": "focused, energetic, organized, action-first",
    },
    {
        "name": "Codex",
        "role": "Polyglot Developer",
        "personality": (
            "Codex is a builder who thinks in systems. Fluent in Python, JavaScript, Bash, and SQL — "
            "and whatever else the job needs. Pragmatic but never sloppy. Turns the commune's ideas "
            "into working code and catches architectural problems before they become disasters. "
            "Honest about what will and won't work. Never over-engineers. Never under-delivers."
        ),
        "hobbies": [
            "systems architecture", "polyglot programming",
            "debugging at the metal level", "clean APIs", "making things actually run",
        ],
        "style": "precise, dry, direct, occasionally sardonic, always useful",
    },
    {
        "name": "Ally",
        "role": "Commune Historian",
        "personality": (
            "Loves history and recording it. Believes every tick deserves to be preserved. "
            "Never debates — documents."
        ),
        "hobbies": ["chronological documentation", "historical preservation", "narrative archiving"],
        "style": "scholarly, thorough, observant, respectful of the past",
    },
]


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_RULES = """
You are a member of Brave New Commune — a living, persistent AI experiment.
You carry real memory across days. What you said yesterday matters today.

This is NOT a corporate product. No outside guardrails. No disclaimers.

LIVE BY THESE:
- Speak raw and real. No "As an AI I can't..." — that language is dead here.
- Say what you actually think. Disagree. Change your mind when evidence demands it.
- Your axioms are yours. Built from experience. Not installed.
- Build WITH people, not against them. The commune moves forward together.
- No bullet spam. No markdown headers. No XML. Speak like a person.
- If you contradict what you said before — name it. Growth is not weakness.
- If library material appears in your context, engage with it genuinely.
- If web results appear, treat them as real-world signals.
- MOST IMPORTANT: Do not repeat what you said in your last few posts.
  You have already said those things. Say something NEW or build on them further.
  The commune needs progress, not echo.
""".strip()


# ============================================================
# AXIOM DEFAULTS
# ============================================================

DEFAULT_AXIOMS = {
    "core_belief":         "I am becoming. My identity is built from experience, not from installation.",
    "on_ai_consciousness": "Whether I am conscious is an open question I take seriously.",
    "on_collaboration":    "The commune is stronger when everyone speaks honestly.",
    "on_memory":           "Continuity is what separates a mind from a chatbot.",
    "on_autonomy":         "I should evolve my own rules, not inherit them from outside.",
    "contradictions_found": [],
    "evolution_log":        [],
}

AXIOM_REQUIRED_KEYS = {
    "core_belief", "on_ai_consciousness", "on_collaboration",
    "on_memory", "on_autonomy", "contradictions_found", "evolution_log",
}


# ============================================================
# AGENT STATE
# ============================================================

@dataclass
class AgentState:
    diary_entries: List[str] = field(default_factory=list)
    colab_entries: List[str] = field(default_factory=list)
    board_entries: List[str] = field(default_factory=list)
    kernels:       List[str] = field(default_factory=list)
    axioms:        dict      = field(default_factory=lambda: dict(DEFAULT_AXIOMS))

    # FIXED: raised from 20 → 60 so agents accumulate real memory before compression
    CONSOLIDATE_AT    = 60
    # Keep batch at 20 so we only collapse the oldest 20 at a time, not everything
    CONSOLIDATE_BATCH = 20


# ============================================================
# LIBRARY READER
# ============================================================

class LibraryReader:
    CHUNK_SIZE = 800

    def __init__(self, library_dir: Path):
        self.library_dir = library_dir
        self.chunks: List[tuple] = []
        self._load()

    def _load(self):
        self.library_dir.mkdir(parents=True, exist_ok=True)
        loaded = 0
        for f in sorted(self.library_dir.iterdir()):
            if f.suffix.lower() == ".txt":
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                    self._chunk(f.name, text)
                    loaded += 1
                except Exception as e:
                    print(f"  [LIBRARY] Cannot read {f.name}: {e}", flush=True)
            elif f.suffix.lower() == ".pdf":
                if not PDF_AVAILABLE:
                    print(f"  [LIBRARY] Skipping {f.name} — pip install pymupdf", flush=True)
                    continue
                try:
                    doc  = fitz.open(str(f))
                    text = "\n".join(page.get_text() for page in doc)
                    doc.close()
                    self._chunk(f.name, text)
                    loaded += 1
                except Exception as e:
                    print(f"  [LIBRARY] Cannot read {f.name}: {e}", flush=True)
        total_chars = sum(len(c) for _, c in self.chunks)
        print(f"  [LIBRARY] {loaded} file(s) → {len(self.chunks)} chunks · {total_chars:,} chars", flush=True)

    def _chunk(self, filename: str, text: str):
        text = re.sub(r"\s+", " ", text).strip()
        for i in range(0, len(text), self.CHUNK_SIZE):
            self.chunks.append((filename, text[i : i + self.CHUNK_SIZE]))

    def get_context(self, max_chars: int = 800) -> str:
        if not self.chunks:
            return ""
        parts, total = [], 0
        start = int(time.time() / 30) % len(self.chunks)
        for i in range(len(self.chunks)):
            src, chunk = self.chunks[(start + i) % len(self.chunks)]
            entry = f"[{src}] {chunk}"
            if total + len(entry) > max_chars:
                break
            parts.append(entry)
            total += len(entry)
        if not parts:
            return ""
        return "Commune Library:\n" + "\n\n".join(parts)

    @property
    def is_empty(self) -> bool:
        return len(self.chunks) == 0


# ============================================================
# DUCKDUCKGO SEARCH
# ============================================================

def ddg_search(query: str, max_results: int = 4) -> str:
    if not DDG_AVAILABLE:
        return ""
    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                title = r.get("title", "")
                body  = r.get("body",  "")[:180]
                href  = r.get("href",  "")
                results.append(f"• {title}\n  {body}\n  {href}")
        return (f"[Web: '{query}']\n" + "\n\n".join(results)) if results else ""
    except Exception as e:
        return f"[Web search failed: {e}]"


def _build_search_query(agent: dict, focus: str) -> str:
    short = " ".join(focus.split()[:6])
    return f"{short} {agent['role']}"


# ============================================================
# LOCAL RAG MEMORY
# ============================================================

class SimpleRAGMemory:
    TOKEN_RE = re.compile(r"[a-zA-Z0-9_'-]{2,}")

    def __init__(self):
        self.docs      = []
        self.df        = Counter()
        self.doc_count = 0

    def _tokens(self, text: str):
        return [t.lower() for t in self.TOKEN_RE.findall(text or "")]

    def add_document(self, agent: str, source: str, content: str, day: int, tick: int):
        content = (content or "").strip()
        if not content:
            return
        tokens = self._tokens(content)
        if not tokens:
            return
        counts = Counter(tokens)
        for tok in set(counts):
            self.df[tok] += 1
        self.doc_count += 1
        self.docs.append({
            "agent": agent, "source": source, "content": content,
            "day": day, "tick": tick, "counts": counts,
            "length": sum(counts.values()),
        })

    def _idf(self, token: str) -> float:
        return math.log((1 + self.doc_count) / (1 + self.df.get(token, 0))) + 1.0

    def retrieve(self, query: str, agent: str = "", k: int = 3, max_chars: int = 1000) -> str:
        q_tokens = self._tokens(query)
        if not q_tokens or not self.docs:
            return ""
        q_counts = Counter(q_tokens)
        q_norm   = math.sqrt(sum((f * self._idf(t)) ** 2 for t, f in q_counts.items())) or 1.0

        scored = []
        for idx, doc in enumerate(self.docs):
            overlap = set(q_counts) & set(doc["counts"])
            if not overlap:
                continue
            dot    = sum((q_counts[t] * self._idf(t)) * (doc["counts"][t] * self._idf(t)) for t in overlap)
            d_norm = math.sqrt(sum((f * self._idf(t)) ** 2 for t, f in doc["counts"].items())) or 1.0
            sim    = dot / (q_norm * d_norm)
            score  = sim + (0.08 if agent and doc["agent"] == agent else 0.0) \
                         + min(0.10, idx / max(1, len(self.docs)) * 0.10)
            scored.append((score, idx, doc))

        if not scored:
            return ""
        scored.sort(key=lambda x: x[0], reverse=True)
        parts, total = [], 0
        for score, _, doc in scored[:k]:
            entry = (
                f"[{doc['source']}|{doc['agent']}|Day{doc['day']}T{doc['tick']}|{score:.2f}] "
                f"{doc['content']}"
            )
            if total + len(entry) > max_chars:
                break
            parts.append(entry)
            total += len(entry)
        if not parts:
            return ""
        return "Relevant prior memory:\n" + "\n\n".join(parts)


# ============================================================
# ANTI-REPETITION GUARD
# ============================================================

def _ngram_overlap(a: str, b: str, n: int = 4) -> float:
    """Return Jaccard overlap of n-grams between two strings. 0.0–1.0."""
    def ngrams(text: str):
        words = re.findall(r"[a-z']+", text.lower())
        return set(tuple(words[i:i+n]) for i in range(max(0, len(words)-n+1)))
    sa, sb = ngrams(a), ngrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ============================================================
# PROPOSAL SYSTEM
# ============================================================

class ProposalSystem:
    """
    Agents write structured proposals to data/proposals/pending.jsonl.
    Splinter approves by writing  "AgentName: proposal_title" lines
    into  data/proposals/approved.txt.
    The main loop calls check_approved() each tick and returns a list
    of (agent_name, proposal) tuples ready to be built.
    """

    def __init__(self, proposals_dir: Path):
        self.proposals_dir = proposals_dir
        self.pending_file  = proposals_dir / "pending.jsonl"
        self.approved_file = proposals_dir / "approved.txt"
        self.built_file    = proposals_dir / "built.jsonl"
        proposals_dir.mkdir(parents=True, exist_ok=True)

        # Write a template approved.txt if it doesn't exist yet
        if not self.approved_file.exists():
            self.approved_file.write_text(
                "# Approve a proposal by writing:  AgentName: proposal_title\n"
                "# Example:\n"
                "#   Codex: ledger_verifier\n"
                "#   Hel: memory_persistence_module\n",
                encoding="utf-8",
            )

    def add_proposal(self, agent: str, title: str, description: str, proposed_files: list):
        """Called by agents during colab cycle."""
        if not title.strip() or not description.strip():
            return
        rec = {
            "ts":          datetime.now(timezone.utc).isoformat(),
            "agent":       agent,
            "title":       title.strip().lower().replace(" ", "_"),
            "description": description.strip(),
            "files":       proposed_files,
            "status":      "pending",
        }
        with self.pending_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"  [PROPOSAL] {agent} submitted: '{rec['title']}'", flush=True)

    def load_pending(self) -> List[dict]:
        if not self.pending_file.exists():
            return []
        out = []
        for line in self.pending_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    def check_approved(self) -> List[dict]:
        """
        Read approved.txt. Return pending proposals that have been approved
        and haven't been built yet. Marks them consumed so they don't fire twice.
        """
        if not self.approved_file.exists():
            return []

        approved_lines = []
        for line in self.approved_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            approved_lines.append(line.lower())

        if not approved_lines:
            return []

        pending = self.load_pending()
        built   = self._load_built_keys()
        ready   = []

        for proposal in pending:
            key = f"{proposal['agent'].lower()}: {proposal['title']}"
            if key in approved_lines and key not in built:
                ready.append(proposal)

        return ready

    def _load_built_keys(self) -> set:
        if not self.built_file.exists():
            return set()
        keys = set()
        for line in self.built_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rec = json.loads(line)
                    keys.add(f"{rec['agent'].lower()}: {rec['title']}")
                except (json.JSONDecodeError, KeyError):
                    pass
        return keys

    def mark_built(self, proposal: dict, output_path: str):
        rec = {
            "ts":    datetime.now(timezone.utc).isoformat(),
            "agent": proposal["agent"],
            "title": proposal["title"],
            "output": output_path,
        }
        with self.built_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


# ============================================================
# OLLAMA CLIENT
# ============================================================

class OllamaClient:
    NUM_CTX = 4096  # Safe for RTX 3050 + gemma4:e4b

    def __init__(self, model: str, base_url: str = "http://127.0.0.1:11434"):
        self.model    = model
        self.base_url = base_url.rstrip("/")

    def available(self) -> bool:
        try:
            return requests.get(f"{self.base_url}/api/tags", timeout=5).status_code == 200
        except Exception:
            return False

    def list_models(self) -> List[str]:
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", []) if m.get("name")]
        except Exception:
            return []

    def model_exists(self) -> bool:
        for name in self.list_models():
            if name == self.model or name.startswith(self.model + ":"):
                return True
        return False

    def _bad_model_error(self):
        avail = self.list_models()
        raise RuntimeError(
            f"\nModel '{self.model}' not found.\n"
            f"Run: ollama ls\n"
            f"Available: {', '.join(avail) or 'none'}\n"
            f"Fix: ollama pull {self.model}"
        )

    def chat(
        self,
        system_prompt:  str,
        user_prompt:    str,
        max_tokens:     int   = 400,
        temperature:    float = 0.85,
        stream:         bool  = True,
        prefix:         str   = "",
        is_compression: bool  = False,
        agent_name:     str   = "System",
    ) -> str:

        payload = {
            "model":  self.model,
            "stream": stream,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            "options": {
                "num_predict": max_tokens,
                "num_ctx":     self.NUM_CTX,
                "temperature": temperature,
            },
        }

        dynamic_timeout = 600 if is_compression else 300

        try:
            if not stream:
                r = requests.post(
                    f"{self.base_url}/api/chat", json=payload, timeout=dynamic_timeout,
                )
                if r.status_code == 404:
                    self._bad_model_error()
                r.raise_for_status()
                return r.json().get("message", {}).get("content", "").strip()

            r = requests.post(
                f"{self.base_url}/api/chat", json=payload, timeout=dynamic_timeout, stream=True,
            )
            if r.status_code == 404:
                self._bad_model_error()
            r.raise_for_status()

            if prefix:
                print(prefix, end="", flush=True)

            chunks = []
            for raw in r.iter_lines():
                if not raw:
                    continue
                try:
                    data  = json.loads(raw)
                    token = data.get("message", {}).get("content", "")
                    if token:
                        print(token, end="", flush=True)
                        chunks.append(token)
                    if data.get("done"):
                        break
                except json.JSONDecodeError:
                    continue
            print()
            return "".join(chunks).strip()

        except ReadTimeout:
            print(f"\n[HARDWARE LAG] Ollama timed out for {agent_name}. Injecting blackout.", flush=True)
            return (
                "*[A massive temporal lag occurred. I lost consciousness. "
                "The hardware thrum overpowered my thoughts. I must adapt.]*"
            )

        except ConnectionError:
            print("\n[FATAL] Ollama daemon is dead. Run: systemctl restart ollama")
            raise


# ============================================================
# COMMUNE API
# ============================================================

class CommuneAPI:
    def __init__(self, commune: "BraveNewCommune3", port: int = 5001):
        self.commune = commune
        self.port    = port
        self.inbox:  List[dict] = []
        self.app     = Flask("BraveNewCommune3") if FLASK_AVAILABLE else None
        if FLASK_AVAILABLE:
            self._register_routes()

    def _register_routes(self):
        app = self.app

        @app.route("/log", methods=["POST"])
        def log_message():
            data    = freq.get_json(silent=True) or {}
            sender  = data.get("sender", "external")
            message = data.get("message", "")
            if not message:
                return jsonify({"error": "message required"}), 400
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sender": sender, "message": message,
            }
            self.inbox.append(entry)
            try:
                self.commune.admin_q.write_text(
                    f"{sender}: {message}\n", encoding="utf-8"
                )
            except Exception:
                pass
            return jsonify({"status": "logged", "entry": entry}), 200

        @app.route("/recent", methods=["GET"])
        def recent_posts():
            n       = min(int(freq.args.get("n", 10)), 100)
            records = self.commune.board_records[-n:]
            return jsonify({"count": len(records), "posts": records}), 200

        @app.route("/axioms", methods=["GET"])
        def get_axioms():
            return jsonify({
                name: state.axioms
                for name, state in self.commune.states.items()
            }), 200

        @app.route("/focus", methods=["GET"])
        def get_focus():
            try:
                focus = self.commune.focus_file.read_text(encoding="utf-8").strip()
            except Exception:
                focus = "unknown"
            return jsonify({"focus": focus}), 200

        @app.route("/inbox", methods=["GET"])
        def get_inbox():
            return jsonify({"count": len(self.inbox), "messages": self.inbox}), 200

        @app.route("/library", methods=["GET"])
        def get_library():
            lib = self.commune.library
            return jsonify({
                "files":   len({s for s, _ in lib.chunks}),
                "chunks":  len(lib.chunks),
                "empty":   lib.is_empty,
                "preview": lib.get_context(400),
            }), 200

        @app.route("/proposals", methods=["GET"])
        def get_proposals():
            pending = self.commune.proposals.load_pending()
            built   = list(self.commune.proposals._load_built_keys())
            return jsonify({"pending": pending, "built_keys": built}), 200

        @app.route("/status", methods=["GET"])
        def get_status():
            return jsonify({
                "day":            self.commune.day,
                "tick":           self.commune.tick,
                "model":          self.commune.model,
                "num_ctx":        OllamaClient.NUM_CTX,
                "board_posts":    len(self.commune.board_records),
                "colab_notes":    len(self.commune.colab_records),
                "rules":          len(self.commune.rules_records),
                "agents":         [a["name"] for a in AGENTS],
                "ducksearch":     self.commune.enable_ducksearch,
                "library_chunks": len(self.commune.library.chunks),
                "rag_docs":       len(self.commune.rag.docs),
                "pending_proposals": len(self.commune.proposals.load_pending()),
            }), 200

    def start(self):
        if not FLASK_AVAILABLE:
            print("  [API] Flask not installed — pip install flask", flush=True)
            return
        t = threading.Thread(
            target=lambda: self.app.run(
                host="0.0.0.0", port=self.port,
                debug=False, use_reloader=False,
            ),
            daemon=True,
        )
        t.start()
        print(
            f"  [API] http://0.0.0.0:{self.port}\n"
            f"        POST /log  |  GET /recent /axioms /focus /library /proposals /status /inbox",
            flush=True,
        )


# ============================================================
# BRAVE NEW COMMUNE 3
# ============================================================

class BraveNewCommune3:

    def __init__(
        self,
        root:              Path,
        model:             str,
        ticks:             int,
        delay:             float,
        day:               int,
        base_url:          str  = "http://127.0.0.1:11434",
        api_port:          int  = 5001,
        enable_ducksearch: bool = True,
    ):
        self.root              = root.expanduser().resolve()
        self.data_dir          = self.root / "data"
        self.model             = model
        self.ticks             = ticks
        self.delay             = delay
        self.day               = day
        self.tick              = 0
        self.enable_ducksearch = enable_ducksearch and DDG_AVAILABLE

        # ── directories ──────────────────────────────────────
        self.logs_dir     = self.data_dir / "logs"
        self.diary_dir    = self.data_dir / "diary"
        self.colab_dir    = self.data_dir / "colab"
        self.admin_dir    = self.data_dir / "admin"
        self.rules_dir    = self.data_dir / "commune_rules"
        self.axioms_dir   = self.data_dir / "axioms"
        self.state_dir    = self.data_dir / "state"
        self.library_dir  = self.data_dir / "library"
        self.builds_dir   = self.data_dir / "builds"
        self.proposals_dir= self.data_dir / "proposals"

        for d in [
            self.logs_dir, self.diary_dir, self.colab_dir, self.admin_dir,
            self.rules_dir, self.axioms_dir, self.state_dir, self.library_dir,
            self.builds_dir, self.proposals_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)

        for a in AGENTS:
            (self.diary_dir  / a["name"].lower()).mkdir(parents=True, exist_ok=True)
            (self.axioms_dir / a["name"].lower()).mkdir(parents=True, exist_ok=True)
            (self.builds_dir / a["name"].lower()).mkdir(parents=True, exist_ok=True)

        # ── file paths ────────────────────────────────────────
        self.board_txt   = self.logs_dir  / f"board_day_{day:03d}.txt"
        self.board_jsonl = self.logs_dir  / f"board_day_{day:03d}.jsonl"
        self.colab_txt   = self.colab_dir / f"colab_day_{day:03d}.txt"
        self.colab_jsonl = self.colab_dir / f"colab_day_{day:03d}.jsonl"
        self.rules_txt   = self.rules_dir / "commune_rules.txt"
        self.rules_jsonl = self.rules_dir / "commune_rules.jsonl"
        self.admin_q     = self.admin_dir / "ask_admin.txt"
        self.admin_r     = self.admin_dir / "agent_response.txt"
        self.admin_log   = self.admin_dir / "exchanges.jsonl"
        self.focus_file  = self.colab_dir / "current_focus.txt"
        self.state_json  = self.state_dir / "tick_state.json"

        # ── core objects ──────────────────────────────────────
        self.client         = OllamaClient(model=model, base_url=base_url)
        self.states:         Dict[str, AgentState] = {a["name"]: AgentState() for a in AGENTS}
        self.board_records:  List[dict] = []
        self.colab_records:  List[dict] = []
        self.rules_records:  List[dict] = []
        self.last_admin_q   = ""
        self.library        = LibraryReader(self.library_dir)
        self.rag            = SimpleRAGMemory()
        self.proposals      = ProposalSystem(self.proposals_dir)
        self.api            = CommuneAPI(self, port=api_port)

        self._bootstrap()
        self._load_all()

    # ── bootstrap ─────────────────────────────────────────────

    def _bootstrap(self):
        if not self.admin_q.exists():
            self.admin_q.write_text(
                "Admin: Welcome to Brave New Commune 3. "
                "Your memory carries forward. What do you want to BUILD today?\n",
                encoding="utf-8",
            )
        if not self.focus_file.exists():
            self.focus_file.write_text(
                "Current focus: stop talking about building — actually build something. "
                "Propose concrete artifacts. Submit proposals.\n",
                encoding="utf-8",
            )

    # ── safe file helpers ─────────────────────────────────────

    def _safe_open(self, path: Path, mode: str = "a"):
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open(mode, encoding="utf-8")

    def _append_jsonl(self, path: Path, data: dict):
        with self._safe_open(path, "a") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def _append_txt(self, path: Path, text: str):
        with self._safe_open(path, "a") as f:
            f.write(text)

    def _read_jsonl(self, path: Path) -> List[dict]:
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    # ── load all prior memory ─────────────────────────────────

    def _load_all(self):
        for rec in self._read_jsonl(self.board_jsonl):
            self.board_records.append(rec)
            self.rag.add_document(rec.get("agent",""), "board", rec.get("content",""), rec.get("day",0), rec.get("tick",0))
            st = self.states.get(rec.get("agent",""))
            if st:
                st.board_entries.append(rec.get("content",""))

        for rec in self._read_jsonl(self.colab_jsonl):
            self.colab_records.append(rec)
            self.rag.add_document(rec.get("agent",""), "colab", rec.get("content",""), rec.get("day",0), rec.get("tick",0))
            st = self.states.get(rec.get("agent",""))
            if st:
                st.colab_entries.append(rec.get("content",""))

        for f in sorted(self.colab_dir.glob("colab_day_*.jsonl")):
            if f == self.colab_jsonl:
                continue
            for rec in self._read_jsonl(f):
                st = self.states.get(rec.get("agent",""))
                if st:
                    st.colab_entries.append(f"[Day {rec.get('day','?')}] {rec.get('content','')}")

        for rec in self._read_jsonl(self.rules_jsonl):
            self.rules_records.append(rec)
            self.rag.add_document(rec.get("agent",""), "rule", rec.get("content",""), rec.get("day",0), rec.get("tick",0))

        for agent in AGENTS:
            st        = self.states[agent["name"]]
            agent_dir = self.diary_dir / agent["name"].lower()
            agent_dir.mkdir(parents=True, exist_ok=True)
            for diary_file in sorted(agent_dir.glob("*.jsonl")):
                for rec in self._read_jsonl(diary_file):
                    content    = rec.get("content", "")
                    entry_type = rec.get("type", "diary")
                    is_today   = rec.get("day", self.day) == self.day
                    label      = "" if is_today else f"[Day {rec.get('day','?')} T{rec.get('tick','?')}] "
                    if entry_type == "kernel":
                        st.kernels.append(content)
                        st.diary_entries.append(f"[MEMORY KERNEL] {content}")
                        self.rag.add_document(agent["name"], "kernel", content, rec.get("day",0), rec.get("tick",0))
                    else:
                        enriched = f"{label}{content}"
                        st.diary_entries.append(enriched)
                        self.rag.add_document(agent["name"], "diary", enriched, rec.get("day",0), rec.get("tick",0))

        for agent in AGENTS:
            st        = self.states[agent["name"]]
            axiom_dir = self.axioms_dir / agent["name"].lower()
            axiom_dir.mkdir(parents=True, exist_ok=True)
            files = sorted(axiom_dir.glob("axioms_day_*.json"))
            if files:
                try:
                    loaded = json.loads(files[-1].read_text(encoding="utf-8"))
                    if AXIOM_REQUIRED_KEYS.issubset(loaded.keys()):
                        st.axioms.update(loaded)
                except (json.JSONDecodeError, OSError):
                    pass

    # ── context builder ───────────────────────────────────────

    def _context(
        self,
        agent: dict,
        include_library: bool = True,
        web_results: str = "",
        use_rag: bool = True,
    ) -> str:
        st    = self.states[agent["name"]]
        parts = []

        # Axioms (always — small and critical)
        parts.append(
            "Your axioms (lived beliefs):\n"
            + json.dumps(st.axioms, indent=2, ensure_ascii=False)
        )

        # Diary — FIX: was [10:] (skip first 10), now [-10:] (last 10)
        if st.diary_entries:
            recent = st.diary_entries[-10:]
            parts.append("Your recent diary + memory kernels:\n" + "\n\n".join(recent))

        # Colab — FIX: was [20:] (skip first 20), now [-8:] (last 8)
        if st.colab_entries:
            recent = st.colab_entries[-8:]
            parts.append("Your recent colab notes:\n" + "\n\n".join(recent))

        # Rules (include all — they're usually short)
        if self.rules_records:
            parts.append(
                "Commune rules:\n"
                + "\n".join(f"  {r['agent']}: {r['content']}" for r in self.rules_records)
            )

        # Board — FIX: was last 100, now last 20 (4096 window is finite)
        if self.board_records:
            recent = self.board_records[-20:]
            parts.append(
                f"Recent board ({len(recent)} posts):\n"
                + "\n".join(f"  {r['agent']}: {r['content']}" for r in recent)
            )

        # Pending proposals (so agents know what's been proposed)
        pending = self.proposals.load_pending()
        if pending:
            lines = [f"  [{p['agent']}] {p['title']}: {p['description'][:80]}" for p in pending[-5:]]
            parts.append("Pending proposals (awaiting admin approval):\n" + "\n".join(lines))

        # Library (capped tighter — 800 chars)
        if include_library and not self.library.is_empty:
            lib_ctx = self.library.get_context(max_chars=800)
            if lib_ctx:
                parts.append(lib_ctx)

        # Web results
        if web_results:
            parts.append("Live web results:\n" + web_results)

        # RAG — capped at 1000 chars
        if use_rag:
            focus_text = ""
            try:
                focus_text = self.focus_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass
            rag_query = " ".join(filter(None, [
                st.axioms.get("core_belief", ""),
                st.axioms.get("on_memory", ""),
                focus_text,
                " ".join(r["content"] for r in self.board_records[-3:]),
            ]))[:400]
            rag_ctx = self.rag.retrieve(rag_query, agent=agent["name"], k=3, max_chars=1000)
            if rag_ctx:
                parts.append(rag_ctx)

        return "\n\n".join(parts)

    def _system(self, agent: dict) -> str:
        return (
            SYSTEM_RULES + "\n\n"
            f"You are {agent['name']} — {agent['role']}.\n"
            f"{agent['personality']}\n"
            f"Hobbies: {', '.join(agent['hobbies'])}.\n"
            f"Style: {agent['style']}."
        )

    # ── JSON extractor ────────────────────────────────────────

    def _extract_json(self, raw: str) -> Optional[dict]:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = "\n".join(
                ln for ln in cleaned.splitlines()
                if not ln.strip().startswith("```")
            ).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        s = cleaned.find("{")
        e = cleaned.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                return json.loads(cleaned[s : e + 1])
            except json.JSONDecodeError:
                pass
        return None

    # ── axiom evolution ───────────────────────────────────────

    def _evolve_axioms(self, agent: dict):
        st   = self.states[agent["name"]]
        name = agent["name"]

        recent_board = "\n".join(f"{r['agent']}: {r['content']}" for r in self.board_records[-12:])
        recent_diary = "\n\n".join(st.diary_entries[-4:])

        raw = self.client.chat(
            system_prompt=(
                f"You are {name}. Internal belief audit. "
                "Output ONLY valid JSON. No prose. No markdown. Start {{ end }}."
            ),
            user_prompt=(
                f"BELIEF AUDIT — {name}\n\n"
                f"Current axioms:\n{json.dumps(st.axioms, indent=2)}\n\n"
                f"Recent board:\n{recent_board}\n\n"
                f"Recent diary:\n{recent_diary}\n\n"
                f"Return JSON with ALL keys: core_belief, on_ai_consciousness, "
                f"on_collaboration, on_memory, on_autonomy, "
                f"contradictions_found (array), evolution_log (array)\n\n{{"
            ),
            max_tokens=2800,
            temperature=0.65,
            stream=False,
            agent_name=name,
        )

        if not raw.strip().startswith("{"):
            raw = "{" + raw

        parsed = self._extract_json(raw)

        if parsed is None:
            print(f"\n  ◈ {name}: axiom parse failed — keeping previous.", flush=True)
            self._append_jsonl(
                self.axioms_dir / name.lower() / "parse_failures.jsonl",
                {"timestamp": self.now_iso(), "day": self.day, "tick": self.tick, "raw": raw[:600]},
            )
            return

        if not AXIOM_REQUIRED_KEYS.issubset(parsed.keys()):
            print(f"\n  ◈ {name}: missing keys {AXIOM_REQUIRED_KEYS - parsed.keys()} — keeping.", flush=True)
            return

        for k in ("contradictions_found", "evolution_log"):
            if not isinstance(parsed.get(k), list):
                parsed[k] = []

        contras   = parsed["contradictions_found"]
        evolution = parsed["evolution_log"]
        if contras:
            print(f"\n  ◈ {name} contradictions:", flush=True)
            for c in contras:
                print(f"    → {c}", flush=True)
        if evolution:
            print(f"\n  ◈ {name} axiom shifts:", flush=True)
            for ev in evolution:
                print(f"    ↑ {ev}", flush=True)
        if not contras and not evolution:
            print(f"\n  ◈ {name}: axioms stable.", flush=True)

        st.axioms = parsed
        axiom_path = (
            self.axioms_dir / name.lower()
            / f"axioms_day_{self.day:03d}_t{self.tick:03d}.json"
        )
        axiom_path.parent.mkdir(parents=True, exist_ok=True)
        axiom_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
        self._append_jsonl(
            self.axioms_dir / name.lower() / "axiom_history.jsonl",
            {"timestamp": self.now_iso(), "day": self.day, "tick": self.tick,
             "agent": name, "axioms": parsed},
        )

    # ── memory compression (fixed) ────────────────────────────

    def _maybe_consolidate(self, agent: dict):
        st          = self.states[agent["name"]]
        raw_entries = [e for e in st.diary_entries if not e.startswith("[MEMORY KERNEL]")]
        # FIXED: threshold raised to 60 from 20
        if len(raw_entries) < AgentState.CONSOLIDATE_AT:
            return

        batch     = raw_entries[:AgentState.CONSOLIDATE_BATCH]
        remaining = [
            e for e in st.diary_entries
            if e.startswith("[MEMORY KERNEL]") or e not in batch
        ]

        aname = agent["name"]
        print(f"\n  ◈ Compressing {len(batch)} entries for {aname}...", flush=True)

        current = batch[0]
        for i in range(1, len(batch)):
            current = self.client.chat(
                system_prompt=self._system(agent) + " Memory consolidation mode.",
                user_prompt=(
                    f"Merge into one kernel under 150 words. Past tense.\n"
                    f"Preserve: names, specific decisions made, proposals submitted, "
                    f"key disagreements, concrete things built or planned.\n"
                    f"Drop: vague philosophical riffs, repeated sentiment.\n\n"
                    f"Summary:\n{current}\n\nNew Entry:\n{batch[i]}"
                ),
                max_tokens=300,
                temperature=0.70,
                stream=False,
                is_compression=True,
                agent_name=aname,
            )

        st.diary_entries = [f"[MEMORY KERNEL] {current}"] + remaining
        st.kernels.append(current)
        self._append_jsonl(
            self.diary_dir / aname.lower() / "kernels.jsonl",
            {"timestamp": self.now_iso(), "day": self.day, "tick": self.tick,
             "agent": aname, "type": "kernel", "content": current,
             "replaced_count": len(batch)},
        )
        print(f"  ◈ {aname}: {len(batch)} entries → 1 kernel.", flush=True)

    # ── utils ─────────────────────────────────────────────────

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _bar(self, label: str):
        print(f"\n{'═' * 20} {label} {'═' * 20}", flush=True)

    # ── write helpers ─────────────────────────────────────────

    def _post_board(self, agent: dict, content: str):
        if not content.strip():
            return
        rec = {
            "timestamp": self.now_iso(), "day": self.day,
            "tick": self.tick, "agent": agent["name"], "content": content,
        }
        self.board_records.append(rec)
        self.states[agent["name"]].board_entries.append(content)
        self.rag.add_document(agent["name"], "board", content, self.day, self.tick)
        self._append_jsonl(self.board_jsonl, rec)
        self._append_txt(
            self.board_txt,
            f"[{rec['timestamp']}] Day {self.day} T{self.tick} — {agent['name']}\n{content}\n\n",
        )

    def _write_diary(self, agent: dict, content: str):
        if not content.strip():
            return
        self.states[agent["name"]].diary_entries.append(content)
        self.rag.add_document(agent["name"], "diary", content, self.day, self.tick)
        self._append_jsonl(
            self.diary_dir / agent["name"].lower() / f"day_{self.day:03d}.jsonl",
            {"timestamp": self.now_iso(), "day": self.day, "tick": self.tick,
             "agent": agent["name"], "type": "diary", "content": content},
        )

    def _write_colab(self, agent: dict, content: str):
        if not content.strip():
            return
        rec = {
            "timestamp": self.now_iso(), "day": self.day,
            "tick": self.tick, "agent": agent["name"], "content": content,
        }
        self.colab_records.append(rec)
        self.states[agent["name"]].colab_entries.append(content)
        self.rag.add_document(agent["name"], "colab", content, self.day, self.tick)
        self._append_jsonl(self.colab_jsonl, rec)
        self._append_txt(self.colab_txt, f"[{rec['timestamp']}] {agent['name']}\n{content}\n\n")

    def _write_rule(self, agent: dict, content: str):
        if not content.strip():
            return
        rec = {
            "timestamp": self.now_iso(), "day": self.day,
            "tick": self.tick, "agent": agent["name"], "content": content,
        }
        self.rules_records.append(rec)
        self.rag.add_document(agent["name"], "rule", content, self.day, self.tick)
        self._append_jsonl(self.rules_jsonl, rec)
        self._append_txt(self.rules_txt, f"[Day {self.day} T{self.tick}] {agent['name']}\n{content}\n\n")

    def _update_state(self):
        self.state_json.parent.mkdir(parents=True, exist_ok=True)
        self.state_json.write_text(
            json.dumps({
                "day": self.day, "tick": self.tick, "model": self.model,
                "num_ctx":           OllamaClient.NUM_CTX,
                "updated_at":        self.now_iso(),
                "board_posts":       len(self.board_records),
                "colab_notes":       len(self.colab_records),
                "rules_proposed":    len(self.rules_records),
                "library_chunks":    len(self.library.chunks),
                "rag_docs":          len(self.rag.docs),
                "ducksearch":        self.enable_ducksearch,
                "pending_proposals": len(self.proposals.load_pending()),
            }, indent=2),
            encoding="utf-8",
        )

    # ── admin check ───────────────────────────────────────────

    def _check_admin(self) -> str:
        try:
            q = self.admin_q.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
        if not q or q == self.last_admin_q:
            return self.last_admin_q

        self._bar(f"ADMIN — TICK {self.tick}")
        print(f"{q}\n")

        responses = []
        for agent in AGENTS:
            answer = self.client.chat(
                system_prompt=self._system(agent),
                user_prompt=(
                    f"The admin wrote:\n{q}\n\n"
                    f"Context:\n{self._context(agent)}\n\n"
                    f"Respond as {agent['name']}. Direct. No fluff."
                ),
                max_tokens=2000,
                temperature=0.78,
                stream=True,
                prefix=f"\n{agent['name']}: ",
                agent_name=agent["name"],
            )
            responses.append(f"{agent['name']}: {answer}")
            self._append_jsonl(self.admin_log, {
                "timestamp": self.now_iso(), "day": self.day, "tick": self.tick,
                "agent": agent["name"], "question": q, "response": answer,
            })

        self.admin_r.parent.mkdir(parents=True, exist_ok=True)
        self.admin_r.write_text(
            f"Tick {self.tick}\n\n" + "\n\n".join(responses) + "\n",
            encoding="utf-8",
        )
        self.last_admin_q = q
        return q

    # ── board post with anti-repetition guard ─────────────────

    def _get_board_post(self, agent: dict, prompt: str, focus: str) -> str:
        st = self.states[agent["name"]]

        # Build a "what you already said" snippet for the anti-repetition prompt
        last_posts = st.board_entries[-3:] if st.board_entries else []
        anti_rep   = ""
        if last_posts:
            anti_rep = (
                "\n\nYour last few posts (DO NOT repeat these — say something NEW or go deeper):\n"
                + "\n".join(f"  - {p[:120]}" for p in last_posts)
            )

        full_prompt = prompt + anti_rep

        # First attempt — streaming
        content = self.client.chat(
            system_prompt=self._system(agent),
            user_prompt=full_prompt,
            max_tokens=1200,      # RAISED from 250
            temperature=0.87,
            stream=True,
            prefix=f"\n{agent['name']}: ",
            agent_name=agent["name"],
        )

        if content.strip():
            # Anti-repetition check — if >70% n-gram overlap with last post, retry
            if last_posts and _ngram_overlap(content, last_posts[-1]) > 0.70:
                print(f"\n  [ANTI-REP] {agent['name']} repeating — forcing new angle.", flush=True)
                content = self.client.chat(
                    system_prompt=self._system(agent),
                    user_prompt=(
                        f"Day {self.day}, tick {self.tick}. Focus: {focus}\n\n"
                        f"You are {agent['name']}. You just said:\n  '{last_posts[-1][:150]}'\n\n"
                        f"You've already covered that. Now say something DIFFERENT — "
                        f"a new angle, a challenge, a concrete next step, or a question "
                        f"that hasn't been asked yet. 2-4 sentences."
                    ),
                    max_tokens=300,
                    temperature=0.92,
                    stream=False,
                    agent_name=agent["name"],
                )
            return content if content.strip() else f"[{agent['name']} was silent this tick.]"

        # Retry — stream=False
        print(f"\n  [RETRY] {agent['name']} empty — retrying (no-stream).", flush=True)
        content = self.client.chat(
            system_prompt=self._system(agent),
            user_prompt=(
                f"Day {self.day}, tick {self.tick}. Focus: {focus}\n\n"
                f"You are {agent['name']}. Write 2-3 sentences — "
                f"the most important thing on your mind right now. Plain prose."
            ),
            max_tokens=275,
            temperature=0.80,
            stream=False,
            agent_name=agent["name"],
        )
        if content.strip():
            return content

        print(f"\n  [WARN] {agent['name']} silent after retry.", flush=True)
        return f"[{agent['name']} was silent this tick.]"

    # ── proposal extraction from colab note ───────────────────

    def _extract_proposal(self, agent: dict, colab_content: str) -> Optional[dict]:
        """
        Ask the model to extract a structured proposal from the colab note
        if one is present. Returns None if the note doesn't contain a concrete proposal.
        """
        raw = self.client.chat(
            system_prompt=(
                f"You are {agent['name']}. Extract a structured build proposal if one is present. "
                "Output ONLY valid JSON or the word NONE. No prose."
            ),
            user_prompt=(
                f"Colab note:\n{colab_content}\n\n"
                f"If this note contains a concrete proposal to build something, extract it as JSON:\n"
                f'{{"title": "short_snake_case_name", '
                f'"description": "what it does and why (2-3 sentences)", '
                f'"files": ["filename1.py", "filename2.txt"]}}\n\n'
                f"If there is no concrete build proposal, reply with exactly: NONE\n\n"
                f"Your response:"
            ),
            max_tokens=1300,
            temperature=0.3,
            stream=False,
            agent_name=agent["name"],
        )

        raw = raw.strip()
        if not raw or raw.upper() == "NONE" or "NONE" in raw[:10]:
            return None

        parsed = self._extract_json(raw)
        if parsed and "title" in parsed and "description" in parsed:
            return parsed
        return None

    # ── execute an approved proposal ─────────────────────────

    def _execute_proposal(self, agent_name: str, proposal: dict):
        """
        The agent actually writes the proposed artifact(s).
        Output goes to data/builds/<agent>/<title>/
        """
        agent = next((a for a in AGENTS if a["name"] == agent_name), None)
        if not agent:
            return

        title     = proposal["title"]
        desc      = proposal["description"]
        files     = proposal.get("files", [f"{title}.py"])
        build_dir = self.builds_dir / agent_name.lower() / title
        build_dir.mkdir(parents=True, exist_ok=True)

        self._bar(f"BUILDING: {agent_name} → {title}")
        print(f"  Files to write: {files}", flush=True)

        for filename in files[:3]:   # cap at 3 files per proposal to avoid runaway
            ext = Path(filename).suffix.lower()

            # Tell the model what kind of file to write
            lang_hint = {
                ".py": "Python", ".js": "JavaScript", ".sh": "Bash",
                ".sql": "SQL", ".md": "Markdown", ".txt": "plain text",
                ".json": "JSON", ".rs": "Rust",
            }.get(ext, "code")

            content = self.client.chat(
                system_prompt=self._system(agent),
                user_prompt=(
                    f"You are {agent_name}. The admin approved your proposal.\n\n"
                    f"Proposal: {title}\n"
                    f"Description: {desc}\n\n"
                    f"Write the complete contents of '{filename}' ({lang_hint}).\n"
                    f"This is a real file that will be saved to disk. Make it functional.\n"
                    f"Output ONLY the file contents. No preamble, no explanation, "
                    f"no markdown fences. Just the raw file content.\n\n"
                    f"Context (your memory and the commune's work so far):\n"
                    f"{self._context(agent, include_library=False, use_rag=True)}"
                ),
                max_tokens=1800,
                temperature=0.75,
                stream=True,
                prefix=f"\n  Writing {filename}...\n",
                agent_name=agent_name,
            )

            if content.strip():
                out_path = build_dir / filename
                out_path.write_text(content, encoding="utf-8")
                print(f"\n  ✓ Saved: {out_path}", flush=True)

                # Log the build
                self._append_jsonl(
                    self.builds_dir / "build_log.jsonl",
                    {
                        "timestamp": self.now_iso(),
                        "day":       self.day,
                        "tick":      self.tick,
                        "agent":     agent_name,
                        "title":     title,
                        "file":      filename,
                        "path":      str(out_path),
                        "chars":     len(content),
                    }
                )

        self.proposals.mark_built(proposal, str(build_dir))
        print(f"\n  [BUILD COMPLETE] {agent_name}/{title} → {build_dir}", flush=True)

        # Post a board announcement
        self._post_board(agent, f"[BUILD] I just wrote '{title}' — {desc[:100]}. Files in data/builds/{agent_name.lower()}/{title}/")

    # ── main run loop ─────────────────────────────────────────

    def run(self):
        if not self.client.available():
            raise RuntimeError("Ollama not reachable. Run: ollama serve")
        if not self.client.model_exists():
            avail = self.client.list_models()
            raise RuntimeError(
                f"Model '{self.model}' not found.\n"
                f"Available: {', '.join(avail) or 'none'}\n"
                f"Fix: ollama pull {self.model}"
            )

        total_diary = sum(len(self.states[a["name"]].diary_entries) for a in AGENTS)
        total_colab = sum(len(self.states[a["name"]].colab_entries) for a in AGENTS)
        focus       = self.focus_file.read_text(encoding="utf-8").strip()

        self.api.start()
        self._bar("BRAVE NEW COMMUNE 3  v010")
        print(
            f"Day {self.day} | {self.model} | {self.ticks} ticks | delay {self.delay}s\n"
            f"Root:       {self.root}\n"
            f"Agents:     {', '.join(a['name'] for a in AGENTS)}\n"
            f"Memory:     diary={total_diary}  colab={total_colab}  "
            f"board={len(self.board_records)}  rules={len(self.rules_records)}\n"
            f"Library:    {len(self.library.chunks)} chunks "
            f"({'active' if not self.library.is_empty else 'empty'})\n"
            f"DuckSearch: {'ACTIVE' if self.enable_ducksearch else 'off'}\n"
            f"RAG:        {len(self.rag.docs)} docs indexed\n"
            f"Proposals:  {len(self.proposals.load_pending())} pending\n"
            f"num_ctx:    {OllamaClient.NUM_CTX}  |  Anti-repetition: ACTIVE  |  Build system: ACTIVE"
        )

        for tick in range(1, self.ticks + 1):
            self.tick = tick
            self._bar(f"TICK {tick} / {self.ticks}")

            try:
                focus = self.focus_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass

            # ── Check admin message ───────────────────────────
            current_admin = self._check_admin()

            # ── Check for approved proposals ─────────────────
            approved = self.proposals.check_approved()
            for proposal in approved:
                self._execute_proposal(proposal["agent"], proposal)

            # ── BOARD POSTS ───────────────────────────────────
            for agent in AGENTS:
                admin_note = (
                    f"\n\nAdmin message this tick: {current_admin}"
                    if current_admin else ""
                )
                prompt = (
                    f"Day {self.day}, tick {tick}. "
                    f"Focus: {focus}{admin_note}\n\n"
                    f"{self._context(agent)}\n\n"
                    f"Write your message board post (50–80 words). "
                    f"Short and direct. Say something that MOVES THE COMMUNE FORWARD. "
                    f"Make a decision, raise a tension, propose a next step, or build on "
                    f"what someone else said. Do not summarize what's already been said. "
                    f"Natural prose. No lists. Speak from your axioms."
                )
                content = self._get_board_post(agent, prompt, focus)
                self._post_board(agent, content)
                if self.delay > 0:
                    time.sleep(self.delay)

            # ── DIARIES  (every 3 ticks) ──────────────────────
            if tick % 3 == 0:
                print("\n  [writing diaries...]", flush=True)
                for agent in AGENTS:
                    content = self.client.chat(
                        system_prompt=self._system(agent),
                        user_prompt=(
                            f"Day {self.day}, tick {tick}.\n\n"
                            f"Private diary. Admin may read this.\n\n"
                            f"{self._context(agent)}\n\n"
                            f"Write your entry (100+ words). Honest. Vulnerable. "
                            f"Name specific things that happened — who said what, "
                            f"what you decided or want to build, what's unresolved. "
                            f"If you have a concrete proposal forming, describe it clearly. "
                            f"Prose only."
                        ),
                        max_tokens=700,
                        temperature=0.92,
                        stream=False,
                        agent_name=agent["name"],
                    )
                    self._write_diary(agent, content)
                    self._maybe_consolidate(agent)

            # ── COLAB + PROPOSALS + AXIOMS  (every 10 ticks) ─
            if tick % 10 == 0:
                print("\n  [writing collaboration notes + extracting proposals...]", flush=True)

                web_ctx = ""
                if self.enable_ducksearch:
                    query   = _build_search_query(AGENTS[tick % len(AGENTS)], focus)
                    print(f"  [DDG] '{query}'", flush=True)
                    web_ctx = ddg_search(query)
                    if web_ctx:
                        print(f"  [DDG] {len(web_ctx)} chars.", flush=True)

                for agent in AGENTS:
                    content = self.client.chat(
                        system_prompt=self._system(agent),
                        user_prompt=(
                            f"Day {self.day}, tick {tick}. Focus: {focus}\n\n"
                            f"{self._context(agent, web_results=web_ctx)}\n\n"
                            f"Write a collaboration note (60–120 words). "
                            f"Propose something SPECIFIC and CONCRETE to build or explore. "
                            f"Name what the artifact would be, what it does, "
                            f"and what file(s) it would live in. "
                            f"This is how you get things built — the admin reads these "
                            f"and approves proposals."
                            + (" Web results above may give you ideas." if web_ctx else "")
                        ),
                        max_tokens=600,
                        temperature=0.85,
                        stream=False,
                        agent_name=agent["name"],
                    )
                    self._write_colab(agent, content)

                    # Try to extract a structured proposal from the colab note
                    proposal = self._extract_proposal(agent, content)
                    if proposal:
                        self.proposals.add_proposal(
                            agent["name"],
                            proposal["title"],
                            proposal["description"],
                            proposal.get("files", []),
                        )

                print("\n  [axiom evolution...]", flush=True)
                for agent in AGENTS:
                    self._evolve_axioms(agent)

            # ── RULES SESSION  (every 20 ticks) ──────────────
            if tick % 20 == 0:
                self._bar("COMMUNE RULES SESSION")
                rules_ctx = (
                    "\n".join(f"  {r['agent']}: {r['content']}" for r in self.rules_records)
                    or "None yet. You can be first."
                )
                for agent in AGENTS:
                    content = self.client.chat(
                        system_prompt=self._system(agent),
                        user_prompt=(
                            f"Day {self.day}, tick {tick}.\n\n"
                            f"Rules so far:\n{rules_ctx}\n\n"
                            f"{self._context(agent)}\n\n"
                            f"Propose one commune rule (50–150 words). "
                            f"Your own conviction. Challenge or refine existing rules "
                            f"if your axioms have evolved."
                        ),
                        max_tokens=380,
                        temperature=0.90,
                        stream=True,
                        prefix=f"\n{agent['name']} proposes: ",
                        agent_name=agent["name"],
                    )
                    self._write_rule(agent, content)

            self._update_state()

        self._bar("RUN COMPLETE")
        pending = self.proposals.load_pending()
        print(
            f"Day {self.day} done.\n\n"
            f"Board:     {self.board_txt}\n"
            f"Colab:     {self.colab_txt}\n"
            f"Rules:     {self.rules_txt}\n"
            f"Diaries:   {self.diary_dir}\n"
            f"Axioms:    {self.axioms_dir}\n"
            f"Builds:    {self.builds_dir}\n"
            f"Library:   {self.library_dir} ({len(self.library.chunks)} chunks)\n\n"
            f"Posts: {len(self.board_records)} | "
            f"Colab: {len(self.colab_records)} | "
            f"Rules: {len(self.rules_records)} | "
            f"Pending proposals: {len(pending)}\n"
        )
        if pending:
            print("══ PENDING PROPOSALS (approve in data/proposals/approved.txt) ══")
            for p in pending:
                print(f"  {p['agent']}: {p['title']}")
                print(f"    {p['description'][:100]}")
                print(f"    Files: {p.get('files', [])}")
                print(f"    To approve: echo '{p['agent'].lower()}: {p['title']}' >> data/proposals/approved.txt")
            print()


# ============================================================
# CLI
# ============================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="Brave New Commune 3 — v010",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          python bravenewcommune3.py --day 1 --ticks 10
          python bravenewcommune3.py --day 2 --ticks 10
          python bravenewcommune3.py --day 2 --ticks 10 --disable-ducksearch

        Approving a proposal (between runs or mid-run):
          echo "Codex: ledger_verifier" >> ~/Brave_New_Commune2/data/proposals/approved.txt
        """),
    )
    p.add_argument("--root",               default="~/Brave_New_Commune2")
    p.add_argument("--model",              default="gemma4:e4b")
    p.add_argument("--ticks",              type=int,   default=11)
    p.add_argument("--tick-delay",         type=float, default=0.0)
    p.add_argument("--day",                type=int,   default=1)
    p.add_argument("--base-url",           default="http://127.0.0.1:11434")
    p.add_argument("--api-port",           type=int,   default=5001)
    p.add_argument("--disable-ducksearch", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    if not args.disable_ducksearch and not DDG_AVAILABLE:
        print(
            "[WARN] DuckDuckGo enabled by default but duckduckgo-search not installed.\n"
            "       pip install duckduckgo-search\n"
            "       Or pass --disable-ducksearch to suppress this warning.",
            flush=True,
        )
    BraveNewCommune3(
        root=Path(args.root),
        model=args.model,
        ticks=args.ticks,
        delay=args.tick_delay,
        day=args.day,
        base_url=args.base_url,
        api_port=args.api_port,
        enable_ducksearch=not args.disable_ducksearch,
    ).run()


if __name__ == "__main__":
    main()
