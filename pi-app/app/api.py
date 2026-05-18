"""Client für die Binarytec-API.

Ersetzt `api_binarytec.php` und `api_binarytec2.php`.

Schlüsselprinzipien:
- Knappe Timeouts (Connect 1s, Read 2s) – ein Drehkreuz darf nicht hängen.
- Retries mit exponentiellem Backoff für transiente Fehler.
- Kein eigenes Logging hier; das macht der Aufrufer mit Kontext.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import ApiConfig

log = logging.getLogger(__name__)


class ApiError(Exception):
    """Wird geworfen, wenn die API endgültig nicht erreichbar/antwortet."""


@dataclass(frozen=True)
class AccessResult:
    granted: bool
    raw_status: str  # "ok", "denied", "error"
    detail: str = ""


_RETRYABLE = (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError)


class BinarytecClient:
    def __init__(self, cfg: ApiConfig) -> None:
        self._cfg = cfg
        timeout = httpx.Timeout(
            connect=cfg.connect_timeout_seconds,
            read=cfg.request_timeout_seconds,
            write=cfg.request_timeout_seconds,
            pool=cfg.connect_timeout_seconds,
        )
        self._client = httpx.Client(
            base_url=cfg.base_url.rstrip("/"),
            timeout=timeout,
            verify=cfg.verify_tls,
            headers={
                "Authorization": f"Bearer {cfg.bearer_token}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        wait=wait_exponential_jitter(initial=0.1, max=1.0),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def check_access(self, ac_number: str) -> AccessResult:
        """POST /api/v1/raspi/access-controls/check-access."""
        ac_number = ac_number.replace("ß", "-")
        try:
            resp = self._client.post(
                "/api/v1/raspi/access-controls/check-access",
                json={"resourceId": self._cfg.interface_id, "acNumber": ac_number},
            )
        except _RETRYABLE:
            raise
        except httpx.HTTPError as e:
            raise ApiError(f"HTTP-Fehler: {e}") from e

        if resp.status_code >= 500:
            # Server-Fehler ist retry-würdig – als ConnectError neu werfen
            raise httpx.ConnectError(f"5xx vom Backend: {resp.status_code}")

        if resp.status_code == 401:
            return AccessResult(False, "denied", "401 unauthorized (Token prüfen)")

        try:
            obj = resp.json()
        except ValueError as e:
            raise ApiError(f"Ungültige JSON-Antwort: {e}") from e

        try:
            access = int(obj["data"]["resource"]["access"])
        except (KeyError, TypeError, ValueError):
            return AccessResult(False, "error", f"unerwartete Antwort: {obj!r}")

        return AccessResult(granted=access == 1, raw_status="ok", detail=str(access))

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        wait=wait_exponential_jitter(initial=0.1, max=1.0),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def gone(self, ac_number: str, *, inout: str) -> None:
        """POST /api/v1/raspi/access-controls/gone-(in|out).

        Das Backend will wissen, dass die Person tatsächlich durchgegangen ist.
        Fire-and-forget aus Sicht des Drehkreuzes; wir loggen Fehler nur.
        """
        ac_number = ac_number.replace("ß", "-")
        if inout not in ("in", "out"):
            raise ValueError("inout muss 'in' oder 'out' sein")
        path = f"/api/v1/raspi/access-controls/gone-{inout}"
        try:
            resp = self._client.post(
                path,
                json={"resourceId": self._cfg.interface_id, "acNumber": ac_number},
            )
            if resp.status_code >= 500:
                raise httpx.ConnectError(f"5xx beim gone-{inout}: {resp.status_code}")
        except _RETRYABLE:
            raise
        except httpx.HTTPError as e:
            log.warning("gone-%s fehlgeschlagen: %s", inout, e)
