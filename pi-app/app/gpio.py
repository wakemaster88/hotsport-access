"""GPIO-Steuerung für Relais (Drehkreuz) und Buzzer.

Buzzer-Töne sind bewusst musikalisch gewählt, damit sie selbst aus dem Augenwinkel
unterscheidbar sind:

- `beep_valid`    – aufsteigende C-Quint-Triade (C5 → G5 → C6), kurz,
                    hell, freundlich. ~260 ms.
- `beep_invalid`  – absteigende Quarte plus tiefer Doppel-Brummer
                    (F4 → C4 → F3 / F3), klar negativ. ~550 ms.
- `beep_error`    – schnelles dreifaches Stuttern bei tiefer Frequenz, NICHT
                    mit Invalid verwechselbar (Netzwerk-/API-Fehler).
- `beep_startup`  – kurzer Dreiklang (C5 → E5 → G5 → C6) als „Bereit"-Signal.
- `beep_offline`  – tiefer Doppelton, signalisiert Verlust der Hub-Verbindung.

Alle Töne nutzen Software-PWM und sind so getaktet, dass selbst preiswerte
Piezos sie sauber wiedergeben.

Wenn `gpiozero` nicht verfügbar ist (z.B. lokal beim Entwickeln), nutzen wir
ein Stub-Backend, das nur loggt.
"""

from __future__ import annotations

import logging
import threading
import time

from .config import GpioConfig

log = logging.getLogger(__name__)


# Note frequencies (gleichschwebend gestimmt, A4 = 440 Hz)
_C2 = 65
_A2 = 110
_C3 = 131
_E3 = 165
_F3 = 175
_A3 = 220
_C4 = 262
_E4 = 330
_F4 = 349
_G4 = 392
_A4 = 440
_C5 = 523
_E5 = 659
_G5 = 784
_C6 = 1047


class _GpioBackend:
    def open_relay(self, seconds: float) -> None: ...
    def beep(self, freq: int, seconds: float) -> None: ...
    def silence(self, seconds: float) -> None: ...
    def close(self) -> None: ...


class _StubBackend(_GpioBackend):
    def open_relay(self, seconds: float) -> None:
        log.info("[STUB] Relais offen für %.2fs", seconds)

    def beep(self, freq: int, seconds: float) -> None:
        log.info("[STUB] Buzzer %dHz für %.2fs", freq, seconds)

    def silence(self, seconds: float) -> None:
        log.debug("[STUB] silence %.2fs", seconds)
        time.sleep(seconds)

    def close(self) -> None:
        pass


class _RealBackend(_GpioBackend):
    """Echte GPIO-Steuerung über `gpiozero` mit Software-PWM für den Buzzer.

    Die Frequenz wird als PWM-Frequenz gesetzt, der Duty-Cycle auf 50%. Das
    entspricht 1:1 dem Verhalten des alten `buzzer.py` (RPi.GPIO PWM).
    """

    def __init__(self, cfg: GpioConfig) -> None:
        from gpiozero import OutputDevice, PWMOutputDevice  # type: ignore[import-not-found]

        self._relay = OutputDevice(cfg.relay_pin, active_high=True, initial_value=False)
        self._buzzer = PWMOutputDevice(cfg.buzzer_pin, frequency=1000, initial_value=0.0)

    def open_relay(self, seconds: float) -> None:
        try:
            self._relay.on()
            time.sleep(seconds)
        finally:
            self._relay.off()

    def beep(self, freq: int, seconds: float) -> None:
        try:
            self._buzzer.frequency = max(20, freq)
            self._buzzer.value = 0.5
            time.sleep(seconds)
        finally:
            self._buzzer.value = 0.0

    def silence(self, seconds: float) -> None:
        time.sleep(seconds)

    def close(self) -> None:
        try:
            self._relay.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._buzzer.close()
        except Exception:  # noqa: BLE001
            pass


def _make_backend(cfg: GpioConfig) -> _GpioBackend:
    try:
        return _RealBackend(cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("gpiozero nicht verfügbar (%s) – nutze Stub-Backend.", e)
        return _StubBackend()


class GpioController:
    """Thread-safe Wrapper – Aktionen werden serialisiert."""

    def __init__(self, cfg: GpioConfig) -> None:
        self._cfg = cfg
        self._backend = _make_backend(cfg)
        self._lock = threading.Lock()

    def reconfigure(self, cfg: GpioConfig) -> None:
        """Pin-Zuordnung wechseln (z.B. bei Live-Konfig vom Hub).

        Wir bauen das Backend komplett neu, damit gpiozero die neuen Pins
        sauber initialisiert. Während des Wechsels bleibt der Lock genommen.
        """
        with self._lock:
            try:
                self._backend.close()
            except Exception:  # noqa: BLE001
                pass
            self._cfg = cfg
            self._backend = _make_backend(cfg)

    def open_relay(self) -> None:
        with self._lock:
            self._backend.open_relay(self._cfg.relay_pulse_seconds)

    def beep_valid(self) -> None:
        """Aufsteigende C-Quint-Triade mit Oktav-Auflösung – „ding-ding-DONG".

        Drei kurze Stufen C5 → G5 → C6, der letzte Ton trägt deutlich
        länger. Klar aufsteigend, hell, freundlich – signalisiert
        eindeutig „bitte durch". Total ~260 ms, knapp genug damit der
        Durchlauf nicht stockt.
        """
        with self._lock:
            self._backend.beep(_C5, 0.04)
            self._backend.silence(0.015)
            self._backend.beep(_G5, 0.04)
            self._backend.silence(0.015)
            self._backend.beep(_C6, 0.18)

    def beep_invalid(self) -> None:
        """Absteigende Quarte mit tiefer Bestätigung – „bä-bä-BUMMM".

        Mittellage F4 → C4 → F3, kurze Pause, dann bestätigender tiefer
        Brummer auf F3. Bewusst klar absteigend und im tiefen Register,
        damit es sich von:
        - `beep_error`     (3× gleich-tonige Stutter auf E3) und
        - `beep_disabled`  (sanfter F4-Doppelklopf)
        unmissverständlich abhebt. Dauer ~550 ms.
        """
        with self._lock:
            self._backend.beep(_F4, 0.10)
            self._backend.beep(_C4, 0.10)
            self._backend.beep(_F3, 0.10)
            self._backend.silence(0.05)
            self._backend.beep(_F3, 0.22)

    def beep_error(self) -> None:
        """Drei kurze tiefe Stutter (Netzwerk-/API-Fehler).

        Klingt deutlich anders als `beep_invalid`, damit Personal sofort
        erkennt: das System hat den Server nicht erreicht.
        """
        with self._lock:
            for _ in range(3):
                self._backend.beep(_E3, 0.07)
                self._backend.silence(0.07)

    def beep_offline(self) -> None:
        """Doppelter tiefer Ton – Hub-Verbindung verloren (im Hintergrund)."""
        with self._lock:
            self._backend.beep(_C3, 0.12)
            self._backend.silence(0.08)
            self._backend.beep(_C3, 0.12)

    def beep_startup(self) -> None:
        """C-Dur-Akkord aufsteigend: C5 → E5 → G5 → C6 (~0,4 s)."""
        with self._lock:
            self._backend.beep(_C5, 0.07)
            self._backend.beep(_E5, 0.07)
            self._backend.beep(_G5, 0.07)
            self._backend.beep(_C6, 0.18)

    def beep_config_applied(self) -> None:
        """Sehr kurzer Klick (50 ms) – Config-Update wurde live übernommen."""
        with self._lock:
            self._backend.beep(_G5, 0.05)

    def beep_disabled(self) -> None:
        """Sanfter, tiefer „Klopf-Klopf"-Ton – Pi ist administrativ deaktiviert.

        Bewusst freundlicher als `beep_invalid` (kein abweisendes Brummen),
        weil das Ticket möglicherweise gültig ist – der Pi soll aber nicht öffnen.
        """
        with self._lock:
            self._backend.beep(_F4, 0.06)
            self._backend.silence(0.06)
            self._backend.beep(_F4, 0.06)

    def close(self) -> None:
        self._backend.close()
