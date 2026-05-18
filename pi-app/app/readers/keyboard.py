"""Tastatur-Wedge-Reader (USB-QR-/RFID-Reader, der wie eine Tastatur tippt).

Liest direkt aus `/dev/input/eventX` per evdev. Damit braucht der Daemon
weder ein TTY noch X.

Robustheit:
- Wenn das Gerät nicht da ist (USB ausgesteckt, Kernel-Reboot des
  Eingabe-Subsystems), warten wir und versuchen es erneut.
- Read-Loop wird bei OSError neu aufgebaut.
- Pfad ``"auto"`` (oder leer) lässt den Reader das richtige Eingabegerät
  selbst suchen – findet *alle* USB-Tastatur-Reader unter den
  ``event*``-Devices, egal ob sie gerade auf ``event4`` oder ``event7``
  liegen. Mehrere parallel angeschlossene Reader (RFID + QR + Tastatur)
  werden alle gleichzeitig gelesen; jeder Reader läuft in seinem
  eigenen Thread und schiebt fertige Scans in eine gemeinsame Queue.
- Mehrere feste Pfade lassen sich auch komma-getrennt angeben:
  ``device_path = "/dev/input/event4,/dev/input/event5"``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Iterator

from evdev import InputDevice, categorize, ecodes, list_devices  # type: ignore[import-not-found]

from ..config import LiveConfig

log = logging.getLogger(__name__)

# Ziffer-Keycodes (KEY_0 .. KEY_9). Reine RFID-Reader senden oft *nur*
# Ziffern + Enter (keine Buchstaben), QR-/Barcode-Scanner senden alles.
_DIGIT_KEYS = (
    ecodes.KEY_0, ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3, ecodes.KEY_4,
    ecodes.KEY_5, ecodes.KEY_6, ecodes.KEY_7, ecodes.KEY_8, ecodes.KEY_9,
)


def _scanner_check(dev: InputDevice) -> tuple[bool, str]:
    """Prüft, ob das Device wie ein USB-Tastatur-Reader aussieht.

    Akzeptiert:
    - QR-/Barcode-Scanner (Enter + Ziffern + Buchstaben)
    - RFID-Reader, die nur Ziffern + Enter senden (z.B. viele
      125-kHz-EM-Reader oder MIFARE-USB-Sticks)

    Filtert raus:
    - HDMI-CEC-Pseudoinputs (kein Enter oder keine Ziffern, Name
      enthält ``hdmi`` / ``vc4``)
    - Geräte ohne Enter
    - Geräte ohne irgendeine Ziffer

    Liefert (akzeptiert?, grund_string) – der Grund wird ins Log
    gespiegelt, damit man im Diagnose-Modus sieht, warum ein Gerät
    nicht erkannt wurde.
    """
    name = (dev.name or "?")
    phys = (dev.phys or "")
    name_lc = name.lower()
    phys_lc = phys.lower()
    if "hdmi" in name_lc or "vc4" in name_lc:
        return False, f"name='{name}' sieht nach HDMI-CEC aus"
    try:
        caps = dev.capabilities().get(ecodes.EV_KEY, [])
    except OSError as e:
        return False, f"capabilities() fehlgeschlagen: {e}"
    cap_set = set(caps)
    if ecodes.KEY_ENTER not in cap_set:
        return False, "kein KEY_ENTER (kein Tastatur-Reader)"
    digits_present = sum(1 for k in _DIGIT_KEYS if k in cap_set)
    if digits_present < 5:
        return False, f"nur {digits_present}/10 Ziffern-Keys (kein Reader)"
    # USB-Devices bevorzugt; akzeptieren auch leere phys (manche
    # virtuelle Reader/HID-Adapter melden gar nichts).
    if phys_lc and "usb" not in phys_lc:
        return False, f"phys='{phys}' (nicht USB)"
    return True, f"Enter + {digits_present} Ziffern"


def _looks_like_scanner(dev: InputDevice) -> bool:
    ok, _ = _scanner_check(dev)
    return ok


def autodetect_paths() -> list[tuple[str, str]]:
    """Listet *alle* passenden Eingabegeräte unter ``/dev/input/event*``.

    Gibt eine Liste ``[(pfad, name), …]`` zurück. Reihenfolge: Devices,
    deren Name nach Scanner aussieht (``barcode``/``scanner``), zuerst,
    dann ``hid``/``rfid``, dann der Rest – damit z.B. der RFID-Reader
    und der QR-Scanner gleichzeitig erkannt und parallel gelesen werden
    können. Akzeptierte und verworfene Geräte werden ins Log geschrieben.
    """
    candidates: list[tuple[int, str, str]] = []  # (priority, path, name)
    accepted: list[str] = []
    rejected: list[str] = []
    for path in sorted(list_devices()):
        try:
            dev = InputDevice(path)
        except (FileNotFoundError, PermissionError, OSError) as e:
            rejected.append(f"{path}: open fehlgeschlagen ({e})")
            continue
        try:
            ok, reason = _scanner_check(dev)
            name = dev.name or "?"
            if not ok:
                rejected.append(f"{path} '{name}': {reason}")
                continue
            name_lc = name.lower()
            if any(t in name_lc for t in ("barcode", "scanner")):
                priority = 0
            elif "hid" in name_lc or "rfid" in name_lc:
                priority = 1
            else:
                priority = 2
            candidates.append((priority, path, name))
            accepted.append(f"{path} '{name}' (prio={priority}, {reason})")
        finally:
            try:
                dev.close()
            except OSError:
                pass
    candidates.sort(key=lambda x: (x[0], x[1]))
    if accepted:
        log.info("Auto-Detect akzeptiert: %s", "; ".join(accepted))
    if rejected:
        log.info("Auto-Detect verworfen: %s", "; ".join(rejected))
    return [(path, name) for _, path, name in candidates]


def autodetect_path() -> str | None:
    """Erste autodetectete Reader-Pfad – Bequemlichkeitswrapper für Code,
    der nur einen einzelnen Reader nutzen will. Loggt das Ergebnis."""
    found = autodetect_paths()
    if not found:
        return None
    path, name = found[0]
    log.info(
        "Auto-Detect: %s (%s, %d Kandidat(en))",
        path, name, len(found),
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


def _read_device_into_queue(dev: InputDevice, q: "queue.SimpleQueue[str]") -> None:
    """Liest Tastatur-Events von einem Device, schiebt fertige Scans in q.

    Eigener Buffer + Shift-State pro Device, damit zwei parallel
    angeschlossene Reader (z.B. QR + RFID) sich nicht gegenseitig
    Zwischenstände durcheinanderbringen.
    """
    shift_down = False
    buffer: list[str] = []
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
                q.put(scan)
            continue
        ch = (_SHIFT if shift_down else _NORMAL).get(code)
        if ch:
            buffer.append(ch)


def _reader_worker(
    label: str,
    path: str,
    q: "queue.SimpleQueue[str]",
) -> None:
    """Thread-Loop pro Reader-Device.

    Hält die Verbindung aufrecht: bei Fehlern (USB ausgesteckt, OSError,
    Kernel-Restart des Input-Subsystems) wird das Device neu geöffnet.
    """
    log.info("Reader-Worker '%s' startet (path=%s)", label, path)
    while True:
        dev = _open(path)
        if dev is None:
            time.sleep(2.0)
            continue
        try:
            _read_device_into_queue(dev, q)
        except OSError as e:
            log.warning(
                "Reader '%s' OSError (%s) – baue Verbindung neu auf.",
                label, e,
            )
        finally:
            try:
                dev.ungrab()
            except OSError:
                pass
            try:
                dev.close()
            except OSError:
                pass
        time.sleep(1.0)


def _resolve_reader_paths(configured: str) -> list[tuple[str, str]]:
    """Gibt eine Liste (label, path) der zu lesenden Devices zurück.

    - ``""`` oder ``"auto"`` → Auto-Detect aller passenden Reader.
    - ``"/dev/input/event4"`` → genau ein Pfad.
    - ``"/dev/input/event4,/dev/input/event5"`` → mehrere feste Pfade
      (komma-getrennt). Praktisch, wenn der USB-Reihenfolge-Zufall keine
      Rolle spielen soll.
    """
    configured = configured.strip()
    if configured == "" or configured.lower() == "auto":
        return [(name, path) for path, name in autodetect_paths()]
    pieces = [p.strip() for p in configured.split(",") if p.strip()]
    return [(p, p) for p in pieces]


def iter_scans(live: LiveConfig) -> Iterator[str]:
    configured = (live.reader.device_path or "").strip()
    auto = configured == "" or configured.lower() == "auto"
    if auto:
        log.info("Tastatur-Reader: Auto-Detect aktiviert (alle gefundenen lesen parallel)")
    else:
        log.info("Tastatur-Reader: Pfad(e) aus Config: %s", configured)

    # Beim Start auf mindestens einen Reader warten. Sobald wir einen
    # haben, starten wir Worker für *alle* aktuell gefundenen Reader.
    paths: list[tuple[str, str]] = []
    while not paths:
        paths = _resolve_reader_paths(configured)
        if paths:
            break
        log.warning(
            "Kein passendes Eingabegerät gefunden – warte auf Scanner …"
        )
        time.sleep(2.0)

    log.info(
        "Starte %d Reader-Worker: %s",
        len(paths),
        ", ".join(f"{label} ({path})" for label, path in paths),
    )

    q: "queue.SimpleQueue[str]" = queue.SimpleQueue()
    for label, path in paths:
        threading.Thread(
            target=_reader_worker, args=(label, path, q),
            name=f"reader:{path.rsplit('/', 1)[-1]}",
            daemon=True,
        ).start()

    # Mainloop liest aus der gemeinsamen Queue – jeder Worker pusht seine
    # Scans hierher, der Daemon weiß nicht (und muss nicht wissen),
    # welcher Reader es war.
    while True:
        yield q.get()
