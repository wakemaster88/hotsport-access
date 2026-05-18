"""Client für die Binarytec-API.

Ersetzt `api_binarytec.php` und `api_binarytec2.php`.

Schlüsselprinzipien:
- Knappe Timeouts (Connect 1s, Read 2s) – ein Drehkreuz darf nicht hängen.
- Retries mit exponentiellem Backoff für transiente Fehler.
- Kein eigenes Logging hier; das macht der Aufrufer mit Kontext.
"""

from __future__ import annotations

import json
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
        """POST /api/v1/raspi/access-controls/check-access.

        Loggt Request- und Response-Details auf INFO-Level, damit man im
        ``journalctl`` direkt sieht, was die Binarytec-API zurückgibt –
        insbesondere wenn ein Code unerwartet abgelehnt wird (access=0)
        und man wissen muss, *warum*.
        """
        ac_number_orig = ac_number
        ac_number = ac_number.replace("ß", "-")
        # resourceId und acNumber bewusst als JSON-Strings senden (1:1
        # zum alten PHP-Code: '{"resourceId":"3","acNumber":"…"}').
        body = {
            "resourceId": str(self._cfg.interface_id),
            "acNumber": str(ac_number),
        }
        body_json = json.dumps(body, ensure_ascii=False)
        token = self._cfg.bearer_token or ""
        token_redacted = (
            f"{token[:4]}…{token[-4:]} (len={len(token)})" if len(token) >= 8
            else f"len={len(token)}"
        )
        log.info(
            "API check-access -> POST %s/api/v1/raspi/access-controls/check-access "
            "Authorization=Bearer %s body=%s%s",
            (self._cfg.base_url or "").rstrip("/"),
            token_redacted,
            body_json,
            " (orig acNumber=" + ac_number_orig + ")" if ac_number_orig != ac_number else "",
        )
        try:
            resp = self._client.post(
                "/api/v1/raspi/access-controls/check-access", json=body,
            )
        except _RETRYABLE:
            raise
        except httpx.HTTPError as e:
            log.warning("API check-access Transport-Fehler: %s", e)
            raise ApiError(f"HTTP-Fehler: {e}") from e

        # Response-Body als Text immer loggen (gekürzt) – das Backend
        # liefert oft eine sprechende statusMessage / message, die wir
        # sonst niemals sehen würden.
        body_text = (resp.text or "").strip()
        log.info(
            "API check-access <- HTTP %s (%d Bytes): %s",
            resp.status_code,
            len(resp.content or b""),
            (body_text[:500] + ("…" if len(body_text) > 500 else "")) or "<leer>",
        )

        if resp.status_code >= 500:
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
            log.warning(
                "API-Antwort ohne data.resource.access (acNumber=%s): %r",
                ac_number, obj,
            )
            return AccessResult(False, "error", f"unerwartete Antwort: {obj!r}")

        # Versuche, eine sprechende Status-Message aus dem Backend zu
        # bekommen – Felder, die Binarytec/JSON-APIs typischerweise nutzen.
        detail_msg = ""
        if isinstance(obj, dict):
            for key in ("statusMessage", "message", "status"):
                v = obj.get(key)
                if isinstance(v, (str, int, float)) and str(v):
                    detail_msg = str(v)
                    break
            if not detail_msg:
                data = obj.get("data")
                if isinstance(data, dict):
                    res = data.get("resource")
                    if isinstance(res, dict):
                        for key in (
                            "statusMessage", "message", "reason",
                            "status", "denyReason",
                        ):
                            v = res.get(key)
                            if isinstance(v, (str, int, float)) and str(v):
                                detail_msg = str(v)
                                break

        log.info(
            "API check-access result: access=%d -> %s%s",
            access,
            "ZUGANG ERLAUBT" if access == 1 else "ZUGANG ABGELEHNT",
            f" ({detail_msg})" if detail_msg else "",
        )
        return AccessResult(
            granted=access == 1,
            raw_status="ok",
            detail=detail_msg or str(access),
        )

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
        log.info(
            "API gone-%s -> resourceId=%s acNumber=%s",
            inout, self._cfg.interface_id, ac_number,
        )
        try:
            resp = self._client.post(
                path,
                json={"resourceId": self._cfg.interface_id, "acNumber": ac_number},
            )
            body_text = (resp.text or "").strip()
            log.info(
                "API gone-%s <- HTTP %s: %s",
                inout, resp.status_code,
                (body_text[:300] + ("…" if len(body_text) > 300 else "")) or "<leer>",
            )
            if resp.status_code >= 500:
                raise httpx.ConnectError(f"5xx beim gone-{inout}: {resp.status_code}")
        except _RETRYABLE:
            raise
        except httpx.HTTPError as e:
            log.warning("gone-%s fehlgeschlagen: %s", inout, e)
