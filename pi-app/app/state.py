"""Lokaler Zustand des Pi-Daemons.

Wir halten ihn bewusst klein: nur die letzten Scans als Append-only-Log und
einen kleinen Health-State. Damit ist der Pi auch ohne Hub-Verbindung lange
stabil benutzbar – beim nächsten Heartbeat wird alles aufgeholt.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    granted     INTEGER NOT NULL,
    reason      TEXT,
    scanned_at  INTEGER NOT NULL,
    pushed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scans_pushed ON scans(pushed, scanned_at);
"""


class State:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()
        self._last_scan_at: int | None = None
        self._last_scan_code: str | None = None
        self._last_scan_granted: bool | None = None

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def record_scan(self, *, code: str, granted: bool, reason: str | None) -> int:
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO scans (code, granted, reason, scanned_at) VALUES (?, ?, ?, ?)",
                (code, 1 if granted else 0, reason, now),
            )
            self._last_scan_at = now
            self._last_scan_code = code
            self._last_scan_granted = granted
            return cur.lastrowid or 0

    def unpushed(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM scans WHERE pushed = 0 ORDER BY scanned_at LIMIT ?",
                    (limit,),
                )
            )

    def mark_pushed(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._tx() as c:
            c.executemany("UPDATE scans SET pushed = 1 WHERE id = ?", [(i,) for i in ids])

    def last_scan_snapshot(self) -> dict | None:
        with self._lock:
            if self._last_scan_at is None:
                return None
            return {
                "at": self._last_scan_at,
                "code": self._last_scan_code,
                "granted": self._last_scan_granted,
            }

    def cleanup_old(self, *, keep_days: int = 30) -> int:
        """Löscht gepushte Scans, die älter als `keep_days` sind.

        Verhindert, dass die lokale SQLite über Monate ungebremst wächst.
        """
        cutoff = int(time.time()) - keep_days * 86400
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM scans WHERE pushed = 1 AND scanned_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0
