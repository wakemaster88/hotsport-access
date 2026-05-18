"""Baut aus globalen API-Settings + Pi-Settings die Live-Config für einen Pi.

Ein Fingerprint (SHA-256 über das normalisierte JSON) erlaubt dem Pi, schnell
zu erkennen, ob sich die Konfiguration geändert hat.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from . import db


def _to_float(v: Any, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _to_int(v: Any, default: int | None = None) -> int | None:
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _to_bool_or_str(v: Any) -> bool | str:
    """`verify_tls` darf `true`/`false` oder ein Pfad sein."""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip()
    low = s.lower()
    if low in ("true", "1", "yes"):
        return True
    if low in ("false", "0", "no", ""):
        return False
    return s  # angenommen: Pfad zur CA


def build_for(conn: sqlite3.Connection, pi_id: str) -> dict[str, Any]:
    """Baut das config-Objekt, das der Pi konsumiert."""
    settings = db.get_settings(conn, prefix="api.")
    row = db.get_pi(conn, pi_id)

    enabled_raw = row["enabled"] if row else 1
    if enabled_raw is None:
        enabled_raw = 1
    pi: dict[str, Any] = {
        "enabled": bool(int(enabled_raw)),
        "interface_id": (row["interface_id"] if row else None) or "",
        "inout": (row["inout"] if row else None) or "in",
        "relay_pin": _to_int(row["relay_pin"] if row else None, 24) or 24,
        "relay_pulse_seconds": _to_float(
            row["relay_pulse_seconds"] if row else None, 1.0
        ),
        "buzzer_pin": _to_int(row["buzzer_pin"] if row else None, 23) or 23,
        "reader": {
            "mode": (row["reader_mode"] if row else None) or "keyboard",
            "device_path": (row["reader_device_path"] if row else None)
            or "/dev/input/event0",
            "camera_index": _to_int(
                row["reader_camera_index"] if row else None, 0
            )
            or 0,
        },
        "name": (row["name"] if row else None) or pi_id,
        "location": (row["location"] if row else None) or "",
    }

    api: dict[str, Any] = {
        "base_url": settings.get("api.base_url", "").rstrip("/"),
        "bearer_token": settings.get("api.bearer_token", ""),
        "verify_tls": _to_bool_or_str(settings.get("api.verify_tls")),
        "connect_timeout_seconds": _to_float(
            settings.get("api.connect_timeout_seconds"), 1.0
        ),
        "request_timeout_seconds": _to_float(
            settings.get("api.request_timeout_seconds"), 2.0
        ),
    }

    # Fingerprint berechnet sich nur über die *funktional relevanten* Felder
    # (nicht über reine Anzeige-Felder wie name/location).
    fp_payload = {
        "api": api,
        "pi": {
            "enabled": pi["enabled"],
            "interface_id": pi["interface_id"],
            "inout": pi["inout"],
            "relay_pin": pi["relay_pin"],
            "relay_pulse_seconds": pi["relay_pulse_seconds"],
            "buzzer_pin": pi["buzzer_pin"],
            "reader": pi["reader"],
        },
    }
    fp = hashlib.sha256(
        json.dumps(fp_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()

    # Konfiguration ist „komplett", wenn die kritischen Felder gesetzt sind.
    complete = bool(api["base_url"] and api["bearer_token"] and pi["interface_id"])

    return {
        "fingerprint": fp,
        "complete": complete,
        "api": api,
        "pi": pi,
    }
