"""Kommunikation mit dem Hub.

Aufgaben:
- Periodischer Heartbeat (Status + Systeminfo).
- Push gepufferter Scans an den Hub (`/api/scan`).
- Pull der Live-Config bei Fingerprint-Wechsel (`/api/config/{pi_id}`).

Wenn der Hub kurz nicht erreichbar ist, ist das egal: der Daemon läuft mit
seiner zuletzt gesehenen Config einfach weiter und versucht es beim nächsten
Tick erneut.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

import httpx

from . import config as cfg_mod
from . import sysinfo
from .state import State
from .version import current_version

log = logging.getLogger(__name__)


class HubClient(threading.Thread):
    def __init__(
        self,
        boot: cfg_mod.Bootstrap,
        state: State,
        healthy_fn: Callable[[], bool],
        on_config_change: Callable[[cfg_mod.LiveConfig], None],
    ) -> None:
        super().__init__(name="hub-client", daemon=True)
        self._boot = boot
        self._state = state
        self._healthy = healthy_fn
        self._on_config_change = on_config_change
        self._stop = threading.Event()
        self._client: httpx.Client | None = None
        self._last_fingerprint: str | None = None
        self._tick_count = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not self._boot.hub.base_url:
            log.warning("Kein Hub konfiguriert – Heartbeat deaktiviert.")
            return
        self._client = httpx.Client(
            base_url=self._boot.hub.base_url.rstrip("/"),
            timeout=httpx.Timeout(connect=1.5, read=4.0, write=4.0, pool=1.5),
            headers=(
                {"Authorization": f"Bearer {self._boot.hub.pi_token}"}
                if self._boot.hub.pi_token
                else {}
            ),
        )
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                log.warning("Hub-Tick fehlgeschlagen: %s", e)
            self._stop.wait(self._boot.hub.heartbeat_interval_seconds)
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------- intern ----------

    def _tick(self) -> None:
        assert self._client is not None
        payload = {
            "pi_id": self._boot.pi_id,
            "name": self._boot.name,
            "location": self._boot.location,
            "version": current_version(),
            "healthy": bool(self._healthy()),
            "last_scan": self._state.last_scan_snapshot(),
            "sysinfo": sysinfo.collect(),
        }
        resp = self._client.post("/api/heartbeat", json=payload)
        if resp.status_code >= 400:
            log.warning("Heartbeat HTTP %s: %s", resp.status_code, resp.text[:200])
            return
        body = resp.json() if resp.content else {}
        fp = body.get("config_fingerprint")
        if fp and fp != self._last_fingerprint:
            self._refresh_config(fp)
        self._flush_pending_scans()

        # Einmal pro Stunde alte gepushte Scans aufräumen
        self._tick_count += 1
        interval = self._boot.hub.heartbeat_interval_seconds
        if interval > 0 and self._tick_count % max(1, int(3600 / interval)) == 0:
            removed = self._state.cleanup_old(keep_days=30)
            if removed:
                log.info("State-Cleanup: %d alte Scans entfernt.", removed)

    def _refresh_config(self, expected_fp: str) -> None:
        assert self._client is not None
        try:
            resp = self._client.get(f"/api/config/{self._boot.pi_id}")
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
        except httpx.HTTPError as e:
            log.warning("Config-Pull fehlgeschlagen: %s", e)
            return

        live = cfg_mod.parse_live(payload)
        if live.fingerprint != expected_fp:
            log.warning(
                "Config-Fingerprint Mismatch (Heartbeat=%s vs. Pull=%s) – nutze trotzdem.",
                expected_fp,
                live.fingerprint,
            )
        # Unvollständige Hub-Configs ignorieren – sonst würden wir eine
        # funktionierende Inline/Cache-Config kaputtmachen, nur weil der Hub
        # z.B. keinen Bearer-Token-Override gesetzt hat. Wir merken uns den
        # Fingerprint trotzdem, damit wir nicht bei jedem Heartbeat erneut
        # pullen, bis der Hub eine vollständige Antwort liefert.
        if not live.complete:
            log.info(
                "Hub liefert unvollständige Config (fp=%s) – ignoriert, lokale "
                "Config bleibt aktiv.",
                live.fingerprint[:12],
            )
            self._last_fingerprint = live.fingerprint
            return
        cfg_mod.save_cache(self._boot, payload)
        self._last_fingerprint = live.fingerprint
        self._on_config_change(live)

    def _flush_pending_scans(self) -> None:
        assert self._client is not None
        rows = self._state.unpushed(limit=100)
        ok_ids: list[int] = []
        for row in rows:
            try:
                resp = self._client.post(
                    "/api/scan",
                    json={
                        "pi_id": self._boot.pi_id,
                        "code": row["code"],
                        "granted": bool(row["granted"]),
                        "reason": row["reason"],
                        "at": row["scanned_at"],
                    },
                )
                if resp.status_code < 400:
                    ok_ids.append(int(row["id"]))
                else:
                    log.warning(
                        "Scan-Push %s abgelehnt: %s", row["id"], resp.status_code
                    )
                    break
            except httpx.HTTPError as e:
                log.warning("Scan-Push fehlgeschlagen: %s", e)
                break
        self._state.mark_pushed(ok_ids)

