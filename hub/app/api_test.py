"""Hub-seitiger API-Test gegen die Binarytec-API.

Simuliert vom Hub aus exakt den `check-access`-Call, den der Pi bei
einem Scan abschickt – mit denselben Settings (URL, Token, interface_id,
inout, verify_tls, Timeouts), die der Pi auch bekäme.

Bewusst kein Aufruf von ``gone-in/out`` – ein Test soll keine
Statusänderung im Backend auslösen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class ApiTestResult:
    ok: bool
    granted: bool | None
    http_status: int | None
    latency_ms: int
    detail: str
    request_url: str
    request_body: dict[str, Any]
    response_excerpt: str  # gekürzte JSON/Text-Antwort fürs Debug


def run_api_test(
    *,
    base_url: str,
    bearer_token: str,
    interface_id: str,
    code: str,
    verify_tls: bool = False,
    connect_timeout_s: float = 1.0,
    request_timeout_s: float = 2.0,
) -> ApiTestResult:
    """Führt einen `check-access`-Call gegen die Binarytec-API aus.

    Wirft keine Exceptions nach außen – jede Fehlerursache landet in
    `detail`, damit das Dashboard sie ohne 500er rendern kann.
    """
    if not base_url:
        return _fail("Keine API-Base-URL konfiguriert (devices.json oder Override)")
    if not bearer_token:
        return _fail("Kein API-Bearer-Token gesetzt – siehe Hinweis im Pi-Detail")
    if not interface_id:
        return _fail("Keine interface_id für diesen Pi gesetzt")
    code = (code or "").replace("ß", "-").strip()
    if not code:
        return _fail("Bitte einen Test-Code eingeben")

    url_path = "/api/v1/raspi/access-controls/check-access"
    full_url = base_url.rstrip("/") + url_path
    body: dict[str, Any] = {"resourceId": interface_id, "acNumber": code}

    timeout = httpx.Timeout(
        connect=connect_timeout_s,
        read=request_timeout_s,
        write=request_timeout_s,
        pool=connect_timeout_s,
    )
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
    }

    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout, verify=verify_tls) as client:
            resp = client.post(full_url, headers=headers, json=body)
    except httpx.ConnectError as e:
        return _fail(
            f"Netzwerk-/Verbindungsfehler: {e} – API erreichbar? "
            "TLS verifiziert?",
            elapsed=time.monotonic() - started,
            request_url=full_url,
            request_body=body,
        )
    except httpx.TimeoutException as e:
        return _fail(
            f"Timeout nach {time.monotonic() - started:.1f}s: {e}",
            elapsed=time.monotonic() - started,
            request_url=full_url,
            request_body=body,
        )
    except httpx.HTTPError as e:
        return _fail(
            f"HTTP-Fehler: {e}",
            elapsed=time.monotonic() - started,
            request_url=full_url,
            request_body=body,
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    excerpt = (resp.text or "")[:400]

    if resp.status_code == 401:
        return ApiTestResult(
            ok=False,
            granted=False,
            http_status=resp.status_code,
            latency_ms=latency_ms,
            detail="401 Unauthorized – API-Token prüfen",
            request_url=full_url,
            request_body=body,
            response_excerpt=excerpt,
        )
    if resp.status_code >= 500:
        return ApiTestResult(
            ok=False,
            granted=None,
            http_status=resp.status_code,
            latency_ms=latency_ms,
            detail=f"Server-Fehler vom Backend ({resp.status_code})",
            request_url=full_url,
            request_body=body,
            response_excerpt=excerpt,
        )
    if resp.status_code >= 400:
        return ApiTestResult(
            ok=False,
            granted=None,
            http_status=resp.status_code,
            latency_ms=latency_ms,
            detail=f"Backend antwortet {resp.status_code}",
            request_url=full_url,
            request_body=body,
            response_excerpt=excerpt,
        )

    try:
        obj = resp.json()
    except ValueError:
        return ApiTestResult(
            ok=False,
            granted=None,
            http_status=resp.status_code,
            latency_ms=latency_ms,
            detail="Antwort ist kein gültiges JSON",
            request_url=full_url,
            request_body=body,
            response_excerpt=excerpt,
        )

    try:
        access = int(obj["data"]["resource"]["access"])
    except (KeyError, TypeError, ValueError):
        return ApiTestResult(
            ok=False,
            granted=None,
            http_status=resp.status_code,
            latency_ms=latency_ms,
            detail="Antwort hat kein data.resource.access – API geantwortet, aber Code unbekannt",
            request_url=full_url,
            request_body=body,
            response_excerpt=excerpt,
        )

    granted = access == 1
    return ApiTestResult(
        ok=True,
        granted=granted,
        http_status=resp.status_code,
        latency_ms=latency_ms,
        detail=("Zugang erlaubt" if granted else "Zugang abgelehnt") + f" (access={access})",
        request_url=full_url,
        request_body=body,
        response_excerpt=excerpt,
    )


def _fail(
    detail: str,
    *,
    elapsed: float = 0.0,
    request_url: str = "",
    request_body: dict[str, Any] | None = None,
) -> ApiTestResult:
    return ApiTestResult(
        ok=False,
        granted=None,
        http_status=None,
        latency_ms=int(elapsed * 1000),
        detail=detail,
        request_url=request_url,
        request_body=request_body or {},
        response_excerpt="",
    )
