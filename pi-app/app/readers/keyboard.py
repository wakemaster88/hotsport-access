"""Tastatur-Wedge-Reader (USB-QR-/RFID-Reader, der wie eine Tastatur tippt).

Liest direkt aus `/dev/input/eventX` per evdev. Damit braucht der Daemon
weder ein TTY noch X.

Robustheit:
- Wenn das Gerät nicht da ist (USB ausgesteckt, Kernel-Reboot des
  Eingabe-Subsystems), warten wir und versuchen es erneut.
- Read-Loop wird bei OSError neu aufgebaut.
"""

from __future__ import annotations

import logging
import time
from typing import Iterator

from evdev import InputDevice, categorize, ecodes  # type: ignore[import-not-found]

from ..config import LiveConfig

log = logging.getLogger(__name__)

_NORMAL = {
    "KEY_1": "1", "KEY_2": "2", "KEY_3": "3", "KEY_4": "4", "KEY_5": "5",
    "KEY_6": "6", "KEY_7": "7", "KEY_8": "8", "KEY_9": "9", "KEY_0": "0",
    "KEY_A": "a", "KEY_B": "b", "KEY_C": "c", "KEY_D": "d", "KEY_E": "e",
    "KEY_F": "f", "KEY_G": "g", "KEY_H": "h", "KEY_I": "i", "KEY_J": "j",
    "KEY_K": "k", "KEY_L": "l", "KEY_M": "m", "KEY_N": "n", "KEY_O": "o",
    "KEY_P": "p", "KEY_Q": "q", "KEY_R": "r", "KEY_S": "s", "KEY_T": "t",
    "KEY_U": "u", "KEY_V": "v", "KEY_W": "w", "KEY_X": "x", "KEY_Y": "y",
    "KEY_Z": "z",
    "KEY_MINUS": "-", "KEY_DOT": ".", "KEY_SLASH": "/", "KEY_SPACE": " ",
}
_SHIFT = {
    "KEY_1": "!", "KEY_2": "\"", "KEY_3": "§", "KEY_4": "$", "KEY_5": "%",
    "KEY_6": "&", "KEY_7": "/", "KEY_8": "(", "KEY_9": ")", "KEY_0": "=",
    "KEY_A": "A", "KEY_B": "B", "KEY_C": "C", "KEY_D": "D", "KEY_E": "E",
    "KEY_F": "F", "KEY_G": "G", "KEY_H": "H", "KEY_I": "I", "KEY_J": "J",
    "KEY_K": "K", "KEY_L": "L", "KEY_M": "M", "KEY_N": "N", "KEY_O": "O",
    "KEY_P": "P", "KEY_Q": "Q", "KEY_R": "R", "KEY_S": "S", "KEY_T": "T",
    "KEY_U": "U", "KEY_V": "V", "KEY_W": "W", "KEY_X": "X", "KEY_Y": "Y",
    "KEY_Z": "Z",
}


def _open(path: str) -> InputDevice | None:
    try:
        dev = InputDevice(path)
    except (FileNotFoundError, PermissionError, OSError) as e:
        log.warning("Konnte %s nicht öffnen (%s) – warte …", path, e)
        return None
    try:
        dev.grab()
    except OSError as e:
        log.warning("dev.grab() fehlgeschlagen (%s) – wir lesen trotzdem.", e)
    return dev


def iter_scans(live: LiveConfig) -> Iterator[str]:
    path = live.reader.device_path
    log.info("Tastatur-Reader auf %s", path)

    while True:
        dev = _open(path)
        if dev is None:
            time.sleep(2.0)
            continue

        shift_down = False
        buffer: list[str] = []
        try:
            for event in dev.read_loop():
                if event.type != ecodes.EV_KEY:
                    continue
                key = categorize(event)
                if key.keystate not in (1, 2):
                    continue
                code = key.keycode if isinstance(key.keycode, str) else key.keycode[0]
                if code in ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"):
                    shift_down = key.keystate == 1
                    continue
                if code == "KEY_ENTER":
                    scan = "".join(buffer).strip()
                    buffer.clear()
                    if scan:
                        yield scan
                    continue
                ch = (_SHIFT if shift_down else _NORMAL).get(code)
                if ch:
                    buffer.append(ch)
        except OSError as e:
            log.warning("Reader-OSError (%s) – baue Verbindung neu auf.", e)
            time.sleep(1.0)
        finally:
            try:
                dev.ungrab()
            except OSError:
                pass
            try:
                dev.close()
            except OSError:
                pass
