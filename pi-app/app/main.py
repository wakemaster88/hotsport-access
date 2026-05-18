"""Hotsport-Access Pi-Daemon.

Eine Schleife, ein Prozess. Zyklus:

1. Bootstrap-Config lesen (`/etc/hotsport-access/config.toml`).
2. Live-Config (API + Pi-Settings) vom Hub laden – bei Bedarf aus Cache.
3. GPIO + Reader + Binarytec-Client mit der Live-Config initialisieren.
4. Scans verarbeiten (lesen → API → Relais/Buzzer → loggen).
5. Hub-Client aktualisiert Heartbeat, Systeminfo und holt neue Configs.
   Bei jeder „funktional relevanten" Änderung wird der Daemon mit Code 0
   beendet → systemd startet ihn unmittelbar mit der neuen Konfiguration.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import config as cfg_mod
from . import sdnotify
from .api import ApiError, BinarytecClient
from .gpio import GpioController
from .health import start_health_server
from .hub_client import HubClient
from .readers import factory as reader_factory
from .state import State
from .version import current_version

log = logging.getLogger("hotsport.access")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main() -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="hotsport-access")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--print-config", action="store_true")
    args = parser.parse_args()

    boot = cfg_mod.load_bootstrap(args.config)
    log.info(
        "Boot: pi=%s name=%s location=%s version=%s hub=%s",
        boot.pi_id, boot.name, boot.location, current_version(), boot.hub.base_url,
    )
    if args.print_config:
        print(boot)
        return 0

    state = State(boot.state_dir / "state.sqlite3")

    # Reihenfolge bewusst:
    #   1. Cache (`state_dir/live_config.json`) – wenn der Hub schonmal eine
    #      Live-Config geliefert hat, gewinnt sie. Sonst entstünde eine
    #      Restart-Schleife: Hub liefert fp=X → Inline-Restart → fp=Y → wieder Hub.
    #   2. Inline-Config aus `config.toml` – Standardweg ohne Hub. install.sh
    #      löscht den Cache bei Re-Install, damit eine geänderte devices.json
    #      tatsächlich greift.
    #   3. Sonst: auf Hub warten.
    live: cfg_mod.LiveConfig | None = None
    cached = cfg_mod.load_cache(boot)
    if cached:
        c_live = cfg_mod.parse_live(cached)
        if c_live.complete:
            live = c_live
            log.info(
                "Live-Config aus Cache übernommen (fp=%s).", live.fingerprint[:12]
            )
    if live is None:
        inline = cfg_mod.load_inline_live(args.config)
        if inline and inline.complete:
            live = inline
            log.info(
                "Live-Config inline aus config.toml übernommen (fp=%s).",
                live.fingerprint[:12],
            )
    if live is None:
        log.warning(
            "Keine vollständige Live-Config (weder Cache noch inline) – "
            "warte auf Hub. Bitte api.base_url + pi.interface_id in "
            "/etc/hotsport-access/config.toml setzen."
        )

    # Wir starten GPIO + Health bereits ohne Live-Config, damit das System
    # zumindest „lebt" und der Hub einen Heartbeat bekommt.
    gpio = GpioController(live.gpio if live else cfg_mod.GpioConfig())

    healthy = {"flag": True, "last_loop": time.time()}

    def is_healthy() -> bool:
        return healthy["flag"] and (time.time() - healthy["last_loop"]) < 60

    health_server = start_health_server_with(boot, state, is_healthy)

    config_state: dict[str, cfg_mod.LiveConfig | None] = {"current": live}
    config_changed = threading.Event()
    new_config_holder: dict[str, cfg_mod.LiveConfig] = {}

    def on_config_change(new: cfg_mod.LiveConfig) -> None:
        old = config_state["current"]
        if old and old.fingerprint == new.fingerprint:
            return
        log.info(
            "Neue Live-Config vom Hub (fp=%s, complete=%s).",
            new.fingerprint[:12], new.complete,
        )
        new_config_holder["live"] = new
        config_changed.set()
        # Hauptthread aus blockierendem Reader-Syscall aufwecken.
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except OSError:
            pass

        # Falls der Reader trotzdem nicht reagiert: nach 2 s harter Exit.
        # systemd startet uns sofort wieder mit der neuen Konfig.
        def _force_exit() -> None:
            time.sleep(2.0)
            log.warning("Force-Exit nach Config-Change (Reader hing).")
            os._exit(0)

        threading.Thread(target=_force_exit, name="force-exit", daemon=True).start()

    hub = HubClient(boot, state, is_healthy, on_config_change)
    hub.start()

    stop_flag = {"stop": False}

    def _on_signal(signum, _frame):  # noqa: ANN001
        log.info("Signal %s – fahre runter", signum)
        sdnotify.stopping()
        stop_flag["stop"] = True

        # Reader hängt typischerweise in einem blockierenden read() auf
        # /dev/input/event0 – ohne Tastendruck reagiert die for-Schleife
        # nicht. Spätestens nach 3 s erzwingen wir den Exit, damit systemd
        # nicht erst nach SIGTERM-Timeout (10 s) mit SIGKILL kommt.
        def _force_exit_on_stop() -> None:
            time.sleep(3.0)
            if stop_flag["stop"]:
                log.warning("Force-Exit nach Stop-Signal (Reader hing).")
                try:
                    health_server.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                os._exit(0)

        threading.Thread(
            target=_force_exit_on_stop, name="force-stop-exit", daemon=True
        ).start()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        gpio.beep_startup()
    except Exception as e:  # noqa: BLE001
        log.warning("Startup-Beep fehlgeschlagen: %s", e)

    sdnotify.ready()
    sdnotify.status("running")

    # Watchdog-Heartbeat unabhängig vom Reader-Mainloop – sonst killt
    # systemd den Daemon nach 60 s, wenn niemand scannt (Reader hängt
    # blockierend in read_loop()). Solange der Python-Prozess GIL und
    # Thread-Scheduler bedient, lebt er aus systemd-Sicht.
    def _watchdog_loop() -> None:
        while not stop_flag["stop"]:
            try:
                sdnotify.watchdog()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(10.0)

    threading.Thread(
        target=_watchdog_loop, name="systemd-watchdog", daemon=True
    ).start()

    state.record_event(
        kind="service_start",
        reason=f"version={current_version()} hub={'an' if boot.hub.base_url else 'aus'}",
    )

    # Wenn wir noch keine vollständige Config haben, idle bis sie eintrifft.
    if not (live and live.complete):
        sdnotify.status("waiting for config")
        log.info("Warte auf vollständige Konfiguration vom Hub …")
        state.record_event(kind="waiting_config", reason="no live config available")
        try:
            while not stop_flag["stop"] and not (
                config_changed.wait(timeout=2.0) and new_config_holder.get("live") and new_config_holder["live"].complete
            ):
                healthy["last_loop"] = time.time()
                sdnotify.watchdog()
                if stop_flag["stop"]:
                    break
            if stop_flag["stop"]:
                return _shutdown(hub, gpio, health_server)
            live = new_config_holder["live"]
            config_state["current"] = live
            config_changed.clear()
            new_config_holder.clear()
        except KeyboardInterrupt:
            return _shutdown(hub, gpio, health_server)

    assert live is not None
    gpio.reconfigure(live.gpio)
    api = BinarytecClient(live.api)
    reader_iter = reader_factory(boot, live)()

    if live.enabled:
        sdnotify.status(f"ready iface={live.api.interface_id} mode={live.reader.mode}")
        log.info(
            "Bereit. iface=%s reader=%s relay=GPIO%d buzzer=GPIO%d",
            live.api.interface_id, live.reader.mode,
            live.gpio.relay_pin, live.gpio.buzzer_pin,
        )
        state.record_event(
            kind="config_applied",
            reason=f"iface={live.api.interface_id} mode={live.reader.mode} fp={live.fingerprint[:12]}",
        )
    else:
        sdnotify.status("disabled (admin) – Scans loggen, aber kein API-Call/Relais")
        log.warning(
            "Pi ist administrativ DEAKTIVIERT (vom Hub). Scans werden nur geloggt."
        )
        state.record_event(kind="config_applied", reason="pi disabled by admin")

    try:
        for scan in reader_iter:
            if stop_flag["stop"]:
                break
            if config_changed.is_set():
                # Funktional relevante Konfig hat sich geändert → sauber neu starten.
                log.info("Config-Änderung erkannt – beende Prozess für Neustart.")
                state.record_event(kind="config_change", reason="restart for new config")
                gpio.beep_config_applied()
                break
            healthy["last_loop"] = time.time()
            sdnotify.watchdog()
            try:
                _handle_scan(scan, live, api, gpio, state)
            except Exception as e:  # noqa: BLE001
                log.exception("Scan-Verarbeitung fehlgeschlagen: %s", e)
                state.record_scan(code=scan, granted=False, reason=f"error: {e}")
                gpio.beep_error()
    finally:
        state.record_event(kind="service_stop", reason="shutting down")
        api.close()
        _shutdown(hub, gpio, health_server)
    return 0


def start_health_server_with(boot, state, healthy_fn):  # noqa: ANN001 – kleine Helferfunktion
    return start_health_server(boot, state, healthy_fn)


def _shutdown(hub: HubClient, gpio: GpioController, health_server) -> int:  # noqa: ANN001
    log.info("Cleanup …")
    sdnotify.status("shutting down")
    try:
        health_server.shutdown()
    except Exception:  # noqa: BLE001
        pass
    hub.stop()
    try:
        gpio.close()
    except Exception:  # noqa: BLE001
        pass
    return 0


def _handle_scan(
    scan: str,
    live: cfg_mod.LiveConfig,
    api: BinarytecClient,
    gpio: GpioController,
    state: State,
) -> None:
    code = scan.strip()
    if not code:
        return
    log.info("Scan: %r (len=%d)", code, len(code))

    if not live.enabled:
        log.info("Pi ist deaktiviert – kein API-Call, kein Relais.")
        state.record_scan(code=code, granted=False, reason="pi_disabled")
        gpio.beep_disabled()
        return

    try:
        result = api.check_access(code)
    except ApiError as e:
        log.warning("API-Fehler beim Scan %s: %s", code, e)
        state.record_scan(code=code, granted=False, reason=f"api_error: {e}")
        gpio.beep_error()
        return
    except Exception as e:  # noqa: BLE001
        log.warning("Netzwerk-/Timeout-Fehler: %s", e)
        state.record_scan(code=code, granted=False, reason=f"network: {e}")
        gpio.beep_error()
        return

    if result.granted:
        state.record_scan(
            code=code, granted=True,
            reason=f"ok (access=1, {result.detail})" if result.detail else "ok",
        )
        gpio.open_relay()
        gpio.beep_valid()
        try:
            api.gone(code, inout=live.api.inout)
        except Exception as e:  # noqa: BLE001
            log.warning("gone-%s nicht bestätigt: %s", live.api.inout, e)
    else:
        state.record_scan(
            code=code, granted=False,
            reason=f"denied ({result.detail})" if result.detail else "denied",
        )
        gpio.beep_invalid()


if __name__ == "__main__":
    raise SystemExit(main())
