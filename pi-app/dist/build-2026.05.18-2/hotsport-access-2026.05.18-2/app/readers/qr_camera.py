"""QR-Reader per Kamera (OpenCV).

Ersetzt das alte `qr.py`. Doppel-Scans werden über einen Cooldown entprellt.
"""

from __future__ import annotations

import logging
import time
from typing import Iterator

from ..config import LiveConfig

log = logging.getLogger(__name__)

_COOLDOWN_SECONDS = 4.0


def iter_scans(live: LiveConfig) -> Iterator[str]:
    import cv2  # type: ignore[import-not-found]

    log.info("Kamera-QR-Reader an Index %d", live.reader.camera_index)
    cap = cv2.VideoCapture(live.reader.camera_index)
    if not cap.isOpened():
        raise RuntimeError(f"Kamera {live.reader.camera_index} nicht erreichbar")

    detector = cv2.QRCodeDetector()
    last_data = ""
    last_at = 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            data, _, _ = detector.detectAndDecode(frame)
            if not data:
                continue
            now = time.time()
            if data == last_data and (now - last_at) < _COOLDOWN_SECONDS:
                continue
            last_data, last_at = data, now
            yield data
    finally:
        cap.release()
