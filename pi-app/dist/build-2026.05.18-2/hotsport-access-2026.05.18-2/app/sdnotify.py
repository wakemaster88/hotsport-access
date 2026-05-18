"""Mini-Implementierung von sd_notify (systemd) – stdlib-only.

Wir senden:
- `READY=1` einmal nach Init,
- `WATCHDOG=1` regelmäßig im Hauptloop,
- `STOPPING=1` beim Shutdown.

Wenn `NOTIFY_SOCKET` nicht gesetzt ist (Daemon nicht von systemd gestartet),
sind alle Aufrufe No-Ops.
"""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)


def _notify(state: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.connect(addr)
            s.sendall(state.encode("utf-8"))
        finally:
            s.close()
    except OSError as e:
        log.debug("sd_notify(%s) fehlgeschlagen: %s", state, e)


def ready() -> None:
    _notify("READY=1")


def watchdog() -> None:
    _notify("WATCHDOG=1")


def stopping() -> None:
    _notify("STOPPING=1")


def status(text: str) -> None:
    _notify(f"STATUS={text}")
