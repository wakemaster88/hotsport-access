"""Tastatur-Wedge-Reader (USB-QR-/RFID-Reader, der wie eine Tastatur tippt).

Liest direkt aus `/dev/input/eventX` per evdev. Damit braucht der Daemon
weder ein TTY noch X.

Robustheit:
- Wenn das Gerät nicht da ist (USB ausgesteckt, Kernel-Reboot des
  Eingabe-Subsystems), warten wir und versuchen es erneut.
- Read-Loop wird bei OSError neu aufgebaut.
- Pfad ``"auto"`` (oder leer) lässt den Reader das richtige Eingabegerät
  selbst suchen – findet den Barcode-Scanner unter den `event*`-Devices,
  egal ob er gerade ``event4`` oder ``event7`` heißt.
"""

from __future__ import annotations

import logging
import time
from typing import Iterator

from evdev import InputDevice, categorize, ecodes, list_devices  # type: ignore[import-not-found]

from ..config import LiveConfig

log = logging.getLogger(__name__)

# Keys, die ein echtes Tastatur-Eingabegerät (Barcode-Scanner, Tastatur,
# Wedge-Reader) haben muss. HDMI-CEC-Inputs scheitern hier durch.
_REQUIRED_KEYS = (
    ecodes.KEY_ENTER,
    ecodes.KEY_A,
    ecodes.KEY_0,
)


def _looks_like_scanner(dev: InputDevice) -> bool:
    """Heuristik: USB-Keyboard mit Buchstaben + Ziffern + Enter.

    Filtert HDMI-CEC-Inputs (vc4-hdmi-*) und andere Pseudo-Keyboards aus,
    die nur Multimedia-Tasten besitzen.
    """
    try:
        caps = dev.capabilities().get(ecodes.EV_KEY, [])
    except OSError:
        return False
    cap_set = set(caps)
    if not all(k in cap_set for k in _REQUIRED_KEYS):
        return False
    name = (dev.name or "").lower()
    # HDMI-CEC explizit ausschließen, falls eines mal alle KEY-Caps hat.
    if "hdmi" in name or "vc4" in name:
        return False
    phys = (dev.phys or "").lower()
    # USB-Geräte bevorzugt; intern (z.B. „virtual“) ignorieren.
    return "usb" in phys or not phys


def autodetect_path() -> str | None:
    """Sucht das erste passende Eingabegerät unter ``/dev/input/event*``.

    Bevorzugt Devices, deren Name nach Scanner aussieht
    (``barcode``/``scanner``/``hid``); sonst das erste, das zumindest die
    Pflicht-Keys hat. Gibt ``None`` zurück, wenn nichts gefunden wurde.
    """
    candidates: list[tuple[int, str, str]] = []  # (priority, path, name)
    for path in sorted(list_devices()):
        try:
            dev = InputDevice(path)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        try:
            if not _looks_like_scanner(dev):
                continue
            name_lc = (dev.name or "").lower()
            if any(t in name_lc for t in ("barcode", "scanner")):
                priority = 0
            elif "hid" in name_lc:
                priority = 1
            else:
                priority = 2
            candidates.append((priority, path, dev.name or "?"))
        finally:
            try:
                dev.close()
            except OSError:
                pass
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    prio, path, name = candidates[0]
    log.info(
        "Auto-Detect: %s (%s, prio=%d, %d Kandidat(en))",
        path, name, prio, len(candidates),
    )
    return path

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
    configured = (live.reader.device_path or "").strip()
    auto = configured == "" or configured.lower() == "auto"
    if auto:
        log.info("Tastatur-Reader: Auto-Detect aktiviert")
    else:
        log.info("Tastatur-Reader auf %s", configured)

    while True:
        path = configured if not auto else (autodetect_path() or "")
        if not path:
            log.warning(
                "Kein passendes Eingabegerät gefunden – warte auf Scanner …"
            )
            time.sleep(2.0)
            continue
        dev = _open(path)
        if dev is None:
            # Beim nächsten Versuch erneut scannen – vielleicht hat der
            # USB-Stecker zwischenzeitlich umgehängt und das Gerät heißt
            # anders.
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
