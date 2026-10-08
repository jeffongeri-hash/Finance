"""
SQLite persistence for the QQQ pipeline.

* `docs`  — JSON documents keyed by (collection, id). Inserts are idempotent.
* `audit` — append-only, hash-chained audit log. Each row stores the SHA-256 of
            the previous row, so any edit or deletion breaks `verify_audit()`.
* `kv`    — small key/value state (kill switch, counters).

Each environment (research / paper) uses its own database file.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    collection TEXT NOT NULL,
    id         TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (collection, id)
);
CREATE INDEX IF NOT EXISTS docs_by_time ON docs(collection, created_at);
CREATE TABLE IF NOT EXISTS audit (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

_GENESIS = "0" * 64


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL" if self.path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── documents ─────────────────────────────────────────────────────────────

    def insert(self, collection: str, doc_id: str, data: Dict[str, Any]) -> bool:
        """Insert once. Returns False (and changes nothing) if the id already exists."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO docs(collection,id,data,created_at,updated_at) VALUES (?,?,?,?,?)",
                (collection, doc_id, _dumps(data), now, now),
            )
            return cur.rowcount == 1

    def upsert(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO docs(collection,id,data,created_at,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(collection,id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (collection, doc_id, _dumps(data), now, now),
            )

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM docs WHERE collection=? AND id=?", (collection, doc_id)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, collection: str, limit: int = 200, newest_first: bool = True) -> List[Dict[str, Any]]:
        order = "DESC" if newest_first else "ASC"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT data FROM docs WHERE collection=? ORDER BY created_at {order}, rowid {order} LIMIT ?",
                (collection, limit),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def count(self, collection: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM docs WHERE collection=?", (collection,)
            ).fetchone()[0]

    # ── kv ────────────────────────────────────────────────────────────────────

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key,value,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, _dumps(value), time.time()),
            )

    # ── audit ─────────────────────────────────────────────────────────────────

    def audit(self, actor: str, action: str, payload: Dict[str, Any]) -> str:
        with self._lock:
            row = self._conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else _GENESIS
            ts = time.time()
            body = _dumps(payload)
            h = hashlib.sha256(f"{prev}|{ts}|{actor}|{action}|{body}".encode()).hexdigest()
            self._conn.execute(
                "INSERT INTO audit(ts,actor,action,payload,prev_hash,hash) VALUES (?,?,?,?,?,?)",
                (ts, actor, action, body, prev, h),
            )
            return h

    def audit_tail(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq,ts,actor,action,payload,hash FROM audit ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"seq": s, "ts": ts, "actor": a, "action": ac, "payload": json.loads(p), "hash": h}
                for s, ts, a, ac, p, h in rows]

    def verify_audit(self) -> bool:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,actor,action,payload,prev_hash,hash FROM audit ORDER BY seq ASC"
            ).fetchall()
        prev = _GENESIS
        for ts, actor, action, body, prev_hash, h in rows:
            if prev_hash != prev:
                return False
            if hashlib.sha256(f"{prev}|{ts}|{actor}|{action}|{body}".encode()).hexdigest() != h:
                return False
            prev = h
        return True
