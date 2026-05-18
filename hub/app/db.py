"""SQLite-Datenhaltung für den Hub.

Bewusst klein gehalten: Tabellen `pis`, `scans`, `audit`, `settings`. Keine
ORM-Schicht; die Abfragen sind so simpel, dass das mehr Last als Nutzen wäre.

Die Schema-Migration ist additiv: fehlende Spalten werden per `ALTER TABLE`
nachgezogen. Damit verträgt der Hub auch ältere Datenbanken.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_LOCK = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS pis (
    pi_id            TEXT PRIMARY KEY,
    name             TEXT,
    location         TEXT,
    ip               TEXT,
    current_version  TEXT,
    desired_version  TEXT,
    last_seen        INTEGER,
    healthy          INTEGER DEFAULT 0,
    last_scan_at     INTEGER,
    last_scan_code   TEXT,
    last_scan_grant  INTEGER,
    notes            TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,

    -- Pi-spezifische API-Konfiguration
    interface_id            TEXT,
    inout                   TEXT,
    relay_pin               INTEGER,
    relay_pulse_seconds     REAL,
    buzzer_pin              INTEGER,
    reader_mode             TEXT,
    reader_device_path      TEXT,
    reader_camera_index     INTEGER,

    -- Systeminfo (vom Heartbeat befüllt)
    cpu_temp_c              REAL,
    load_1                  REAL,
    mem_used_mb             INTEGER,
    mem_total_mb            INTEGER,
    disk_free_mb            INTEGER,
    disk_total_mb           INTEGER,
    uptime_seconds          INTEGER,
    model                   TEXT,
    kernel                  TEXT,
    mac                     TEXT
);

CREATE TABLE IF NOT EXISTS scans (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    pi_id            TEXT NOT NULL,
    code             TEXT NOT NULL,
    granted          INTEGER NOT NULL,
    reason           TEXT,
    scanned_at       INTEGER NOT NULL,
    received_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scans_pi_time ON scans(pi_id, scanned_at DESC);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          INTEGER NOT NULL,
    actor       TEXT,
    action      TEXT NOT NULL,
    target      TEXT,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# Spalten, die in älteren Datenbanken eventuell fehlen. Werden per
# `_ensure_columns` nachgetragen.
_PI_COLUMNS_TO_BACKFILL: list[tuple[str, str]] = [
    ("enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("interface_id", "TEXT"),
    ("inout", "TEXT"),
    ("relay_pin", "INTEGER"),
    ("relay_pulse_seconds", "REAL"),
    ("buzzer_pin", "INTEGER"),
    ("reader_mode", "TEXT"),
    ("reader_device_path", "TEXT"),
    ("reader_camera_index", "INTEGER"),
    ("cpu_temp_c", "REAL"),
    ("load_1", "REAL"),
    ("mem_used_mb", "INTEGER"),
    ("mem_total_mb", "INTEGER"),
    ("disk_free_mb", "INTEGER"),
    ("disk_total_mb", "INTEGER"),
    ("uptime_seconds", "INTEGER"),
    ("model", "TEXT"),
    ("kernel", "TEXT"),
    ("mac", "TEXT"),
]


# Default-Werte für globale API-Einstellungen
DEFAULT_API_SETTINGS: dict[str, str] = {
    "api.base_url": "https://192.168.251.50:444",
    "api.bearer_token": "",
    "api.verify_tls": "false",
    "api.connect_timeout_seconds": "1.0",
    "api.request_timeout_seconds": "2.0",
}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _ensure_columns(conn, "pis", _PI_COLUMNS_TO_BACKFILL)
    _seed_settings(conn)
    conn.commit()
    return conn


def _ensure_columns(
    conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]
) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, decl in columns:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _seed_settings(conn: sqlite3.Connection) -> None:
    for key, value in DEFAULT_API_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    with _LOCK:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ---------- Settings ----------


def get_settings(conn: sqlite3.Connection, prefix: str | None = None) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = {row["key"]: row["value"] for row in rows}
    if prefix:
        return {k: v for k, v in out.items() if k.startswith(prefix)}
    return out


def set_setting(conn: sqlite3.Connection, key: str, value: str, *, actor: str) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.execute(
            "INSERT INTO audit (at, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), actor, "set_setting", key, value if "token" not in key else "***"),
        )


# ---------- Pis ----------


def upsert_heartbeat(
    conn: sqlite3.Connection,
    *,
    pi_id: str,
    name: str | None,
    location: str | None,
    ip: str | None,
    current_version: str | None,
    healthy: bool,
    last_scan_at: int | None,
    last_scan_code: str | None,
    last_scan_grant: int | None,
    sysinfo: dict[str, Any] | None = None,
) -> None:
    now = int(time.time())
    si = sysinfo or {}
    with tx(conn):
        conn.execute(
            """
            INSERT INTO pis (
                pi_id, name, location, ip, current_version, last_seen,
                healthy, last_scan_at, last_scan_code, last_scan_grant,
                cpu_temp_c, load_1, mem_used_mb, mem_total_mb, disk_free_mb,
                disk_total_mb, uptime_seconds, model, kernel, mac
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pi_id) DO UPDATE SET
                name             = COALESCE(excluded.name, pis.name),
                location         = COALESCE(excluded.location, pis.location),
                ip               = COALESCE(excluded.ip, pis.ip),
                current_version  = excluded.current_version,
                last_seen        = excluded.last_seen,
                healthy          = excluded.healthy,
                last_scan_at     = COALESCE(excluded.last_scan_at, pis.last_scan_at),
                last_scan_code   = COALESCE(excluded.last_scan_code, pis.last_scan_code),
                last_scan_grant  = COALESCE(excluded.last_scan_grant, pis.last_scan_grant),
                cpu_temp_c       = COALESCE(excluded.cpu_temp_c, pis.cpu_temp_c),
                load_1           = COALESCE(excluded.load_1, pis.load_1),
                mem_used_mb      = COALESCE(excluded.mem_used_mb, pis.mem_used_mb),
                mem_total_mb    = COALESCE(excluded.mem_total_mb, pis.mem_total_mb),
                disk_free_mb     = COALESCE(excluded.disk_free_mb, pis.disk_free_mb),
                disk_total_mb    = COALESCE(excluded.disk_total_mb, pis.disk_total_mb),
                uptime_seconds   = COALESCE(excluded.uptime_seconds, pis.uptime_seconds),
                model            = COALESCE(excluded.model, pis.model),
                kernel           = COALESCE(excluded.kernel, pis.kernel),
                mac              = COALESCE(excluded.mac, pis.mac)
            """,
            (
                pi_id,
                name,
                location,
                ip,
                current_version,
                now,
                1 if healthy else 0,
                last_scan_at,
                last_scan_code,
                last_scan_grant,
                si.get("cpu_temp_c"),
                si.get("load_1"),
                si.get("mem_used_mb"),
                si.get("mem_total_mb"),
                si.get("disk_free_mb"),
                si.get("disk_total_mb"),
                si.get("uptime_seconds"),
                si.get("model"),
                si.get("kernel"),
                si.get("mac"),
            ),
        )


def list_pis(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM pis ORDER BY name, pi_id"))


def get_pi(conn: sqlite3.Connection, pi_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pis WHERE pi_id = ?", (pi_id,)).fetchone()


def get_desired_version(conn: sqlite3.Connection, pi_id: str) -> str | None:
    row = conn.execute(
        "SELECT desired_version FROM pis WHERE pi_id = ?", (pi_id,)
    ).fetchone()
    return row["desired_version"] if row else None


def set_desired_version(
    conn: sqlite3.Connection, pi_id: str, version: str | None, actor: str
) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE pis SET desired_version = ? WHERE pi_id = ?", (version, pi_id)
        )
        conn.execute(
            "INSERT INTO audit (at, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), actor, "set_desired_version", pi_id, version or ""),
        )


def set_desired_version_for_all(
    conn: sqlite3.Connection, version: str, actor: str
) -> int:
    with tx(conn):
        cur = conn.execute("UPDATE pis SET desired_version = ?", (version,))
        conn.execute(
            "INSERT INTO audit (at, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), actor, "set_desired_version_all", "*", version),
        )
        return cur.rowcount or 0


def insert_scan(
    conn: sqlite3.Connection,
    *,
    pi_id: str,
    code: str,
    granted: bool,
    reason: str | None,
    scanned_at: int,
) -> None:
    with tx(conn):
        conn.execute(
            """
            INSERT INTO scans (pi_id, code, granted, reason, scanned_at, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (pi_id, code, 1 if granted else 0, reason, scanned_at, int(time.time())),
        )


def recent_scans(
    conn: sqlite3.Connection, *, pi_id: str | None = None, limit: int = 50
) -> list[sqlite3.Row]:
    if pi_id:
        return list(
            conn.execute(
                "SELECT * FROM scans WHERE pi_id = ? ORDER BY scanned_at DESC LIMIT ?",
                (pi_id, limit),
            )
        )
    return list(
        conn.execute(
            "SELECT * FROM scans ORDER BY scanned_at DESC LIMIT ?", (limit,)
        )
    )


def upsert_pi_meta(
    conn: sqlite3.Connection,
    *,
    pi_id: str,
    name: str | None,
    location: str | None,
    notes: str | None,
) -> None:
    with tx(conn):
        conn.execute(
            """
            INSERT INTO pis (pi_id, name, location, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(pi_id) DO UPDATE SET
                name     = COALESCE(?, pis.name),
                location = COALESCE(?, pis.location),
                notes    = COALESCE(?, pis.notes)
            """,
            (pi_id, name, location, notes, name, location, notes),
        )


# Whitelist der per Dashboard editierbaren Pi-Felder
PI_SETTINGS_FIELDS: tuple[str, ...] = (
    "name",
    "location",
    "notes",
    "enabled",
    "interface_id",
    "inout",
    "relay_pin",
    "relay_pulse_seconds",
    "buzzer_pin",
    "reader_mode",
    "reader_device_path",
    "reader_camera_index",
)


def update_pi_settings(
    conn: sqlite3.Connection, *, pi_id: str, fields: dict[str, Any], actor: str
) -> None:
    """Aktualisiert die per Dashboard editierbaren Felder eines Pis.

    Unbekannte Schlüssel werden ignoriert; `None`-Werte überschreiben gezielt
    auf NULL (für „leer machen").
    """
    safe = {k: v for k, v in fields.items() if k in PI_SETTINGS_FIELDS}
    if not safe:
        return
    with tx(conn):
        # upsert: lege Pi an, falls er per Dashboard zuerst angelegt wird
        conn.execute(
            "INSERT OR IGNORE INTO pis (pi_id) VALUES (?)", (pi_id,)
        )
        assignments = ", ".join(f"{k} = ?" for k in safe)
        params = list(safe.values()) + [pi_id]
        conn.execute(f"UPDATE pis SET {assignments} WHERE pi_id = ?", params)
        conn.execute(
            "INSERT INTO audit (at, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (
                int(time.time()),
                actor,
                "update_pi_settings",
                pi_id,
                json.dumps({k: v for k, v in safe.items() if "token" not in k.lower()}),
            ),
        )
