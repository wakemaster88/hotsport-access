"""Lädt die zentrale Pi-Konfiguration aus `pi-app/devices.json`.

`devices.json` ist die Quelle der Wahrheit für die Pi-Liste:
- Welche Pis existieren? (Liste in `pis[]`)
- Welche Soll-Konfiguration hat jeder? (name, location, reader_mode, inout,
  interface_id, GPIO-Pins, …)

Das Dashboard listet alle hier definierten Pis – auch Pis, die noch nie einen
Heartbeat geschickt haben. Live-Daten (Heartbeat, Sysinfo, letzter Scan)
ergänzen das später aus der SQLite-DB des Hubs.

Suchpfad-Resolution (nimmt die erste existierende Datei):
1. `$HOTSPORT_HUB_DEVICES_JSON` (explizit)
2. `<repo_root>/pi-app/devices.json` – wenn Hub aus dem geklonten Repo läuft
3. `/etc/hotsport-hub/devices.json` – Production-Fallback
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def resolve_devices_path() -> Path | None:
    """Bestimmt den Pfad zur `devices.json`. Gibt None zurück, wenn nichts
    gefunden wurde (Dashboard zeigt dann einen klaren Hinweis statt zu crashen)."""
    explicit = os.environ.get("HOTSPORT_HUB_DEVICES_JSON")
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None

    candidates: list[Path] = []
    here = Path(__file__).resolve()
    # hub/app/devices.py -> repo_root = here.parents[2]
    if len(here.parents) >= 3:
        candidates.append(here.parents[2] / "pi-app" / "devices.json")
    candidates.append(Path("/etc/hotsport-hub/devices.json"))

    for c in candidates:
        if c.is_file():
            return c
    return None


def load_raw(path: Path | None = None) -> dict[str, Any] | None:
    p = path or resolve_devices_path()
    if not p:
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError) as e:
        log.warning("devices.json konnte nicht geladen werden (%s): %s", p, e)
        return None


def list_devices(path: Path | None = None) -> list[dict[str, Any]]:
    """Gibt die Pi-Einträge aus `devices.json` zurück, mit aufgelösten Defaults.

    Jedes Dict enthält die Felder, die der DB-`pis`-Tabelle entsprechen, plus
    `_source = "devices.json"` zur Markierung. Damit kann das Template
    einheitlich rendern, egal ob der Pi schon online war oder nicht.
    """
    raw = load_raw(path)
    if not raw:
        return []

    defaults = raw.get("defaults") or {}
    pis = raw.get("pis") or []
    out: list[dict[str, Any]] = []
    for entry in pis:
        if not isinstance(entry, dict) or not entry.get("pi_id"):
            continue
        merged = _merge_with_defaults(entry, defaults)
        out.append(merged)
    return out


def hub_url(path: Path | None = None) -> str | None:
    """Liest die zentrale Hub-URL aus `devices.json` (für Anzeige im Dashboard)."""
    raw = load_raw(path)
    if not raw:
        return None
    hub = raw.get("hub") or {}
    return hub.get("base_url") or None


def api_settings(path: Path | None = None) -> dict[str, Any]:
    """Liest die globalen API-Settings aus `devices.json`."""
    raw = load_raw(path)
    if not raw:
        return {}
    return raw.get("api") or {}


def _merge_with_defaults(
    entry: dict[str, Any], defaults: dict[str, Any]
) -> dict[str, Any]:
    """Fasst Pi-Eintrag mit `defaults` zusammen. Liefert ein Dict, das die
    DB-Spalten der `pis`-Tabelle widerspiegelt, plus eine Marker-Quelle."""

    def pick(key: str, fallback: Any = None) -> Any:
        v = entry.get(key)
        if v not in (None, ""):
            return v
        v = defaults.get(key)
        if v not in (None, ""):
            return v
        return fallback

    return {
        "pi_id": entry["pi_id"],
        "name": entry.get("name") or entry["pi_id"],
        "location": entry.get("location") or "",
        "reader_mode": pick("reader_mode", "keyboard"),
        "inout": entry.get("inout") or "in",
        "interface_id": entry.get("interface_id") or None,
        "relay_pin": _to_int(pick("relay_pin", 24)),
        "relay_pulse_seconds": _to_float(pick("relay_pulse_seconds", 1.0)),
        "buzzer_pin": _to_int(pick("buzzer_pin", 23)),
        "reader_device_path": pick("reader_device_path", "/dev/input/event0"),
        "reader_camera_index": _to_int(pick("reader_camera_index", 0)),
        "_source": "devices.json",
    }


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- Dashboard-Merge ----------


def merge_for_dashboard(
    devices: list[dict[str, Any]],
    db_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Verbindet Pis aus `devices.json` mit Live-Daten aus der DB.

    - Soll-Felder (name, location, reader_mode, inout, interface_id,
      GPIO-Pins, reader_device_path, reader_camera_index): aus `devices.json`,
      DB-Wert nur, wenn er gesetzt ist und vom Soll abweicht (Dashboard-Override).
    - Live-Felder (last_seen, healthy, current_version, last_scan, sysinfo,
      ip, mac, model, kernel): immer aus DB, sonst None.
    - Pis nur in DB (z.B. aus alten Tests): werden trotzdem mit angezeigt,
      damit der Operator sie sieht und ggf. aufräumen kann
      (`_source = "db-only"`).
    """

    by_id = {d["pi_id"]: d for d in devices}
    db_by_id: dict[str, dict[str, Any]] = {}
    for row in db_rows:
        if isinstance(row, dict):
            d = row
        else:
            d = dict(row)  # sqlite3.Row -> dict
        db_by_id[d["pi_id"]] = d

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. devices.json-Pis (Solldaten + ggf. Live-Daten aus DB).
    for pid, dev in by_id.items():
        seen.add(pid)
        live = db_by_id.get(pid) or {}
        out.append(_merge_one(dev, live, source="devices.json"))

    # 2. Pis nur in DB (nicht in devices.json) – am Ende anhängen.
    for pid, live in db_by_id.items():
        if pid in seen:
            continue
        # Solldaten leer; Anzeige rein aus DB. Das Template fängt das ab.
        empty_dev = {
            "pi_id": pid,
            "name": live.get("name") or pid,
            "location": live.get("location") or "",
            "reader_mode": live.get("reader_mode") or "keyboard",
            "inout": live.get("inout") or "in",
            "interface_id": live.get("interface_id"),
            "relay_pin": live.get("relay_pin"),
            "relay_pulse_seconds": live.get("relay_pulse_seconds"),
            "buzzer_pin": live.get("buzzer_pin"),
            "reader_device_path": live.get("reader_device_path"),
            "reader_camera_index": live.get("reader_camera_index"),
        }
        out.append(_merge_one(empty_dev, live, source="db-only"))

    out.sort(key=lambda r: (r["_source"] == "db-only", r["name"], r["pi_id"]))
    return out


_DEVICE_FIELDS = (
    "name", "location", "reader_mode", "inout", "interface_id",
    "relay_pin", "relay_pulse_seconds", "buzzer_pin",
    "reader_device_path", "reader_camera_index",
)
_LIVE_FIELDS = (
    "ip", "mac", "model", "kernel", "current_version", "desired_version",
    "last_seen", "healthy", "last_scan_at", "last_scan_code", "last_scan_grant",
    "cpu_temp_c", "load_1", "mem_used_mb", "mem_total_mb",
    "disk_free_mb", "disk_total_mb", "uptime_seconds", "notes", "enabled",
)


def _merge_one(
    dev: dict[str, Any], live: dict[str, Any], *, source: str
) -> dict[str, Any]:
    merged: dict[str, Any] = {"pi_id": dev["pi_id"], "_source": source}

    # Soll-Felder: Dashboard-Override aus DB hat Vorrang (wenn nicht None);
    # sonst Wert aus devices.json.
    for f in _DEVICE_FIELDS:
        live_val = live.get(f)
        merged[f] = live_val if live_val not in (None, "") else dev.get(f)

    # Live-Felder kommen ausschließlich aus DB.
    for f in _LIVE_FIELDS:
        merged[f] = live.get(f)

    # `enabled`-Default: in devices.json gibt es das Feld nicht; wenn der Pi
    # noch nie heartbeated hat, sehen wir ihn als "aktiv" an (1).
    if merged.get("enabled") is None:
        merged["enabled"] = 1

    return merged
