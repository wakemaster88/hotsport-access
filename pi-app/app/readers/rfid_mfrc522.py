"""RFID-Reader auf Basis MFRC522 über SPI."""

from __future__ import annotations

import logging
import time
from typing import Iterator

from ..config import LiveConfig

log = logging.getLogger(__name__)

_COOLDOWN_SECONDS = 1.5


def iter_scans(live: LiveConfig) -> Iterator[str]:  # noqa: ARG001
    from mfrc522 import SimpleMFRC522  # type: ignore[import-not-found]

    log.info("RFID-Reader (MFRC522) gestartet")
    reader = SimpleMFRC522()
    last_uid = 0
    last_at = 0.0
    try:
        while True:
            uid, _ = reader.read_no_block()
            if not uid:
                time.sleep(0.05)
                continue
            now = time.time()
            if uid == last_uid and (now - last_at) < _COOLDOWN_SECONDS:
                continue
            last_uid, last_at = uid, now
            yield str(uid)
    finally:
        try:
            import RPi.GPIO as GPIO  # type: ignore[import-not-found]
            GPIO.cleanup()
        except Exception:  # noqa: BLE001
            pass
