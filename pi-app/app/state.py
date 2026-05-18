"""Lokaler Zustand des Pi-Daemons.

Speichert die letzten 100 Ereignisse (Scans + Service-Events) als
Append-only-Log. Damit:
- Der Pi ist auch ohne Hub-Verbindung lange stabil benutzbar.
- Beim nächsten Heartbeat werden alle ungepushten Ereignisse an den Hub
  nachgeliefert.
- Die DB bleibt klein – `trim_to_max_rows()` kappt nach jedem Insert auf
  die letzten 100 Einträge (FIFO).

Die Tabelle heißt aus historischen Gründen weiter `scans`, enthält aber
über das `kind`-Feld auch Nicht-Scan-Events (z.B. ``service_start``,
``config_applied``, ``api_error``). Für reine Scans bleibt `code`,
`granted`, `reason` befüllt; bei Events ist `code`/`granted` typischerweise
NULL und der Inhalt steckt in `reason`.
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
    kind        TEXT NOT NULL DEFAULT 'scan',
    code        TEXT,
    granted     INTEGER,
    reason      TEXT,
    scanned_at  INTEGER NOT NULL,
    pushed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scans_pushed ON scans(pushed, scanned_at);
CREATE INDEX IF NOT EXISTS idx_scans_at     ON scans(scanned_at DESC);
"""

MAX_LOCAL_EVENTS = 100


class State:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        # Schema-Migration für ältere DBs: code/granted werden zu nullable,
        # neue Spalte `kind` ergänzen wo sie noch fehlt.
        self._migrate_existing_schema()
        self._conn.commit()
        self._lock = threading.Lock()
        self._last_scan_at: int | None = None
        self._last_scan_code: str | None = None
        self._last_scan_granted: bool | None = None

    def _migrate_existing_schema(self) -> None:
        info = list(self._conn.execute("PRAGMA table_info(scans)"))
        cols = {row["name"]: row for row in info}
        if "kind" not in cols:
            self._conn.execute(
                "ALTER TABLE scans ADD COLUMN kind TEXT NOT NULL DEFAULT 'scan'"
            )
            info = list(self._conn.execute("PRAGMA table_info(scans)"))
            cols = {row["name"]: row for row in info}

        # Ältere DBs hatten code/granted als NOT NULL – passt nicht zu
        # Service-Events. SQLite kann ALTER COLUMN nicht, also Tabelle
        # umbenennen, neu anlegen, Daten zurückkopieren.
        needs_relax = any(
            col in cols and cols[col]["notnull"] for col in ("code", "granted")
        )
        if needs_relax:
            self._conn.executescript(
                """
                ALTER TABLE scans RENAME TO scans__old;
                CREATE TABLE scans (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind        TEXT NOT NULL DEFAULT 'scan',
                    code        TEXT,
                    granted     INTEGER,
                    reason      TEXT,
                    scanned_at  INTEGER NOT NULL,
                    pushed      INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO scans (id, kind, code, granted, reason, scanned_at, pushed)
                    SELECT id,
                           COALESCE(kind, 'scan'),
                           code, granted, reason, scanned_at, pushed
                    FROM scans__old;
                DROP TABLE scans__old;
                CREATE INDEX IF NOT EXISTS idx_scans_pushed ON scans(pushed, scanned_at);
                CREATE INDEX IF NOT EXISTS idx_scans_at     ON scans(scanned_at DESC);
                """
            )

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
        return self._insert(
            kind="scan", code=code, granted=granted, reason=reason
        )

    def record_event(self, *, kind: str, reason: str | None = None) -> int:
        """Loggt ein Service-/System-Event (kein Scan).

        Beispiele für `kind`: ``service_start``, ``service_stop``,
        ``config_applied``, ``api_error``, ``hub_lost``, ``hub_reconnect``,
        ``reader_error``.
        """
        return self._insert(kind=kind, code=None, granted=None, reason=reason)

    def _insert(
        self,
        *,
        kind: str,
        code: str | None,
        granted: bool | None,
        reason: str | None,
    ) -> int:
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO scans (kind, code, granted, reason, scanned_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    kind,
                    code,
                    None if granted is None else (1 if granted else 0),
                    reason,
                    now,
                ),
            )
            # Auf max 100 Einträge kappen – ältester gepushter Eintrag fliegt
            # zuerst raus, damit ungepushte Events bei Hub-Ausfall nicht
            # verloren gehen.
            self._trim_to_max_rows_locked(c)
            if kind == "scan":
                self._last_scan_at = now
                self._last_scan_code = code
                self._last_scan_granted = bool(granted) if granted is not None else None
            return cur.lastrowid or 0

    def _trim_to_max_rows_locked(self, c: sqlite3.Connection) -> None:
        cnt = c.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        if cnt <= MAX_LOCAL_EVENTS:
            return
        excess = cnt - MAX_LOCAL_EVENTS
        # Bevorzugt gepushte Einträge löschen (sind beim Hub gesichert);
        # wenn das nicht reicht, auch ältere ungepushte – dort hat der Pi
        # offensichtlich länger keine Hub-Verbindung gehabt.
        c.execute(
            "DELETE FROM scans WHERE id IN ("
            "  SELECT id FROM scans "
            "  ORDER BY pushed DESC, scanned_at ASC, id ASC LIMIT ?"
            ")",
            (excess,),
        )

    def unpushed(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM scans WHERE pushed = 0 ORDER BY scanned_at LIMIT ?",
                    (limit,),
                )
            )

    def recent(self, limit: int = MAX_LOCAL_EVENTS) -> list[sqlite3.Row]:
        """Letzte `limit` Ereignisse, neueste zuerst."""
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT * FROM scans ORDER BY scanned_at DESC, id DESC LIMIT ?",
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
        """Legacy-Hook (wird vom HubClient noch aufgerufen). Mit dem 100er-
        Limit ist das eigentlich redundant; wir lassen den Aufruf no-op und
        geben 0 zurück, damit alte Pi-Versionen kompatibel bleiben."""
        return 0
