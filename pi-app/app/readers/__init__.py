"""Reader-Backends.

Jeder Reader implementiert eine `iter_scans(boot, live)`-Funktion, die
Strings yieldet. Wir importieren sie lazy in `factory()`, damit z.B. OpenCV
nur dann geladen wird, wenn Kamera-QR aktiv ist.
"""

from __future__ import annotations

from typing import Callable, Iterator

from ..config import Bootstrap, LiveConfig


def factory(boot: Bootstrap, live: LiveConfig) -> Callable[[], Iterator[str]]:
    mode = live.reader.mode
    if mode == "keyboard":
        from .keyboard import iter_scans as fn  # noqa: PLC0415
        return lambda: fn(live)
    if mode == "qr_camera":
        from .qr_camera import iter_scans as fn  # noqa: PLC0415
        return lambda: fn(live)
    if mode == "rfid_mfrc522":
        from .rfid_mfrc522 import iter_scans as fn  # noqa: PLC0415
        return lambda: fn(live)
    raise ValueError(f"Unbekannter Reader-Modus: {mode!r}")
