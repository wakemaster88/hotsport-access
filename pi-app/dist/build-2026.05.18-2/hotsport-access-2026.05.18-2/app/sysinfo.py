"""Sammelt Systeminformationen vom Pi via /proc/, /sys/, /sbin/.

Bewusst nur stdlib – keine externen Abhängigkeiten. Alle Lesefehler werden
abgefangen und führen zu einem `None`-Wert für das jeweilige Feld; das
Heartbeat-Payload bleibt damit auch auf untypischen Systemen sauber.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEVTREE_MODEL = Path("/proc/device-tree/model")
_THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
_LOADAVG = Path("/proc/loadavg")
_MEMINFO = Path("/proc/meminfo")
_UPTIME = Path("/proc/uptime")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _model() -> str | None:
    text = _read_text(_DEVTREE_MODEL)
    if not text:
        return None
    return text.replace("\x00", "").strip()


def _kernel() -> str | None:
    try:
        u = __import__("os").uname()
        return f"{u.sysname} {u.release}"
    except Exception:  # noqa: BLE001
        return None


def _cpu_temp_c() -> float | None:
    text = _read_text(_THERMAL)
    if not text:
        return None
    try:
        return round(int(text) / 1000.0, 1)
    except ValueError:
        return None


def _load_1() -> float | None:
    text = _read_text(_LOADAVG)
    if not text:
        return None
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return None


def _meminfo_mb() -> tuple[int | None, int | None]:
    text = _read_text(_MEMINFO)
    if not text:
        return None, None
    fields: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)\s*kB", line)
        if m:
            fields[m.group(1)] = int(m.group(2))
    total = fields.get("MemTotal")
    available = fields.get("MemAvailable")
    if total is None or available is None:
        return None, None
    used_kb = max(0, total - available)
    return used_kb // 1024, total // 1024


def _uptime_seconds() -> int | None:
    text = _read_text(_UPTIME)
    if not text:
        return None
    try:
        return int(float(text.split()[0]))
    except (IndexError, ValueError):
        return None


def _disk_mb(path: str = "/") -> tuple[int | None, int | None]:
    try:
        usage = shutil.disk_usage(path)
        return usage.free // (1024 * 1024), usage.total // (1024 * 1024)
    except OSError:
        return None, None


def _mac() -> str | None:
    """Erste nicht-loopback MAC-Adresse, die wir finden."""
    base = Path("/sys/class/net")
    if not base.is_dir():
        return None
    for iface_dir in sorted(base.iterdir()):
        if iface_dir.name in ("lo",):
            continue
        addr = _read_text(iface_dir / "address")
        if addr and addr != "00:00:00:00:00:00":
            return addr
    return None


def collect() -> dict[str, Any]:
    used, total = _meminfo_mb()
    free_d, total_d = _disk_mb("/")
    return {
        "cpu_temp_c": _cpu_temp_c(),
        "load_1": _load_1(),
        "mem_used_mb": used,
        "mem_total_mb": total,
        "disk_free_mb": free_d,
        "disk_total_mb": total_d,
        "uptime_seconds": _uptime_seconds(),
        "model": _model(),
        "kernel": _kernel(),
        "mac": _mac(),
        "collected_at": int(time.time()),
    }
