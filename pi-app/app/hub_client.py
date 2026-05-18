"""Kommunikation mit dem Hub.

Aufgaben:
- Periodischer Heartbeat (Status + Systeminfo).
- Push gepufferter Scans an den Hub (`/api/scan`).
- Pull der Live-Config bei Fingerprint-Wechsel (`/api/config/{pi_id}`).

Wenn der Hub kurz nicht erreichbar ist, läuft der Daemon mit der zuletzt
gesehenen Config weiter. Mit ``discover = true`` (oder ``base_url = "auto"``)
wird das LAN periodisch nach dem Hub durchsucht, bis er erreichbar ist.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from . import config as cfg_mod
from . import hub_discovery
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
        self._active_url: str | None = None
        self._last_fingerprint: str | None = None
        self._tick_count = 0
        self._connect_failures = 0
        self._discover = hub_discovery.should_discover(
            boot.hub.base_url, boot.hub.discover
        )

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        hub = self._boot.hub
        if not self._hub_enabled():
            log.warning("Kein Hub konfiguriert – Heartbeat deaktiviert.")
            return

        if self._discover:
            log.info(
                "Hub-Erkennung aktiv (Port %d, Intervall %.0fs).",
                hub.hub_port,
                hub.discover_interval_seconds,
            )
        elif not hub.base_url:
            log.warning("hub.base_url fehlt – Heartbeat deaktiviert.")
            return

        while not self._stop.is_set():
            if not self._ensure_client():
                wait = (
                    hub.discover_interval_seconds
                    if self._discover
                    else hub.heartbeat_interval_seconds
                )
                self._stop.wait(wait)
                continue
            try:
                self._tick()
                self._connect_failures = 0
            except httpx.HTTPError as e:
                self._connect_failures += 1
                log.warning("Hub-Tick fehlgeschlagen: %s", e)
                if self._discover and self._connect_failures >= 2:
                    log.info("Hub nicht erreichbar – starte erneute LAN-Suche …")
                    self._reset_client()
            except Exception as e:  # noqa: BLE001
                log.warning("Hub-Tick fehlgeschlagen: %s", e)
            self._stop.wait(hub.heartbeat_interval_seconds)

        self._reset_client()

    # ---------- intern ----------

    def _hub_enabled(self) -> bool:
        hub = self._boot.hub
        if hub.pi_token:
            return True
        if hub.base_url and not hub_discovery.is_auto_url(hub.base_url):
            return True
        if self._discover:
            return True
        return False

    def _ensure_client(self) -> bool:
        if self._client is not None and self._active_url:
            return True

        url = self._resolve_hub_url()
        if not url:
            if self._discover:
                log.info(
                    "Hub noch nicht gefunden – erneuter Scan in %.0fs …",
                    self._boot.hub.discover_interval_seconds,
                )
            return False

        self._active_url = url
        headers = (
            {"Authorization": f"Bearer {self._boot.hub.pi_token}"}
            if self._boot.hub.pi_token
            else {}
        )
        self._client = httpx.Client(
            base_url=url,
            timeout=httpx.Timeout(connect=1.5, read=4.0, write=4.0, pool=1.5),
            headers=headers,
        )
        log.info("Hub-Client verbunden: %s", url)
        return True

    def _resolve_hub_url(self) -> str | None:
        hub = self._boot.hub
        hint = hub.base_url or None

        if self._discover:
            found = hub_discovery.discover_hub(
                hint_url=hint,
                port=hub.hub_port,
                state_dir=self._boot.state_dir,
                priority_hosts=hub.priority_hosts,
            )
            if found:
                return found
            if hint and not hub_discovery.is_auto_url(hint):
                host = urlparse(hint).hostname
                if host:
                    for port in hub_discovery.ports_to_probe(hub.hub_port, hint):
                        url = f"http://{host}:{port}"
                        if hub_discovery.is_hotsport_hub(url):
                            hub_discovery.save_cached_hub(self._boot.state_dir, url)
                            return url
            return None

        if hint and not hub_discovery.is_auto_url(hint):
            return hint.rstrip("/")
        return None

    def _reset_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
        self._client = None
        self._active_url = None
        self._connect_failures = 0

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
            if resp.status_code in (401, 403):
                raise httpx.HTTPStatusError(
                    "auth failed", request=resp.request, response=resp
                )
            return
        body = resp.json() if resp.content else {}
        fp = body.get("config_fingerprint")
        if fp and fp != self._last_fingerprint:
            self._refresh_config(fp)
        self._flush_pending_scans()

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
                kind = row["kind"]
            except (IndexError, KeyError):
                kind = "scan"
            granted_raw = row["granted"]
            granted = bool(granted_raw) if granted_raw is not None else None
            try:
                resp = self._client.post(
                    "/api/scan",
                    json={
                        "pi_id": self._boot.pi_id,
                        "kind": kind or "scan",
                        "code": row["code"],
                        "granted": granted,
                        "reason": row["reason"],
                        "at": row["scanned_at"],
                    },
                )
                if resp.status_code < 400:
                    ok_ids.append(int(row["id"]))
                else:
                    log.warning(
                        "Event-Push %s abgelehnt: %s", row["id"], resp.status_code
                    )
                    break
            except httpx.HTTPError as e:
                log.warning("Event-Push fehlgeschlagen: %s", e)
                break
        self._state.mark_pushed(ok_ids)
