"""
cache.py — Local severity cache: SQLite (fast) + JSON checkpoint (persistent).

Architecture:
  SeverityCache       — SQLite-backed, thread-safe, 7-day TTL, fast lookups.
  CheckpointStore     — JSON file in data/processed_items.json, survives crashes.
                        Checked BEFORE every Groq call; written AFTER every success.
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "data", "severity_cache.sqlite"
)
_DEFAULT_TTL = 7 * 24 * 3600  # 7 days


class SeverityCache:
    """SQLite-backed cache for severity classification results.

    Usage:
        cache = SeverityCache()
        result = cache.get("source_name", "Title text", "Description text")
        if result is None:
            result = await groq_call(...)
            cache.set("source_name", "Title text", "Description text", result)
    """

    def __init__(self, db_path: str | None = None, ttl: int = _DEFAULT_TTL):
        self._db_path = db_path or _DEFAULT_DB
        self._ttl = ttl
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = self._get_conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS severity_cache ("
            "  key TEXT PRIMARY KEY,"
            "  result TEXT NOT NULL,"
            "  created_at REAL NOT NULL"
            ")"
        )
        conn.commit()

    @staticmethod
    def _make_key(source: str, title: str, description: str) -> str:
        raw = f"{source}||{title}||{description}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, source: str, title: str, description: str) -> dict | None:
        key = self._make_key(source, title, description)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT result, created_at FROM severity_cache WHERE key = ?",
            (key,)
        ).fetchone()
        if row is None:
            return None
        result_json, created_at = row
        if time.time() - created_at > self._ttl:
            conn.execute("DELETE FROM severity_cache WHERE key = ?", (key,))
            conn.commit()
            return None
        try:
            return json.loads(result_json)
        except (json.JSONDecodeError, TypeError):
            conn.execute("DELETE FROM severity_cache WHERE key = ?", (key,))
            conn.commit()
            return None

    def set(self, source: str, title: str, description: str, result: dict) -> None:
        key = self._make_key(source, title, description)
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO severity_cache (key, result, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(result), time.time())
        )
        conn.commit()

    def clear(self) -> None:
        conn = self._get_conn()
        conn.execute("DELETE FROM severity_cache")
        conn.commit()

    @property
    def size(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM severity_cache").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None


class CheckpointStore:
    """JSON file-based checkpoint store for Groq severity results.

    Survives pipeline crashes or rate-limit exhaustion so that items
    already classified are never re-processed by the LLM.

    File: data/processed_items.json  (one level above this utils/ directory)

    Thread-safe via threading.Lock. Atomic writes via temp-file + rename.
    """

    def __init__(self, file_path: str | None = None):
        self._path = file_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "data", "processed_items.json",
        )
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._load()

    # ── public interface (compatible with SeverityCache) ────────────────

    def get(self, source: str, title: str, description: str) -> dict | None:
        key = self._make_key(source, title, description)
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                log.debug("[checkpoint] HIT for key=%s…", key[:12])
            return entry

    def set(self, source: str, title: str, description: str, result: dict) -> None:
        key = self._make_key(source, title, description)
        with self._lock:
            self._data[key] = {
                "severity": result["severity"],
                "reason": result.get("reason", ""),
                "timestamp": time.time(),
            }
            self._flush()

    # ── internals ──────────────────────────────────────────────────────

    @staticmethod
    def _make_key(source: str, title: str, description: str) -> str:
        raw = f"{source}||{title}||{description}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            self._data = {}
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            log.info("[checkpoint] Loaded %d entries from %s", len(self._data), self._path)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("[checkpoint] Corrupt/empty %s — starting fresh: %s", self._path, exc)
            self._data = {}

    def _flush(self) -> None:
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            log.error("[checkpoint] Write error: %s", exc)

    @property
    def size(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._flush()

    def close(self) -> None:
        pass
