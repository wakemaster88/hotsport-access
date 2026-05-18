"""Sehr einfache Basic-Auth, nur für das Dashboard und Admin-Endpunkte.

Heartbeat- und Update-Endpunkte sind absichtlich NICHT geschützt – die Pis
authentifizieren sich über einen statischen Bearer-Token aus der Hub-Config.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials, HTTPBearer

from .config import HubConfig

_basic = HTTPBasic(auto_error=False)
_bearer = HTTPBearer(auto_error=False)


def require_dashboard(request: Request, creds: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    cfg: HubConfig = request.app.state.cfg
    if cfg.dashboard_user is None or cfg.dashboard_password is None:
        return "anonymous"
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Anmeldung erforderlich",
            headers={"WWW-Authenticate": 'Basic realm="hotsport-hub"'},
        )
    user_ok = secrets.compare_digest(creds.username, cfg.dashboard_user)
    pass_ok = secrets.compare_digest(creds.password, cfg.dashboard_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falsche Zugangsdaten",
            headers={"WWW-Authenticate": 'Basic realm="hotsport-hub"'},
        )
    return creds.username


def require_pi_token(request: Request, token=Depends(_bearer)) -> None:
    cfg: HubConfig = request.app.state.cfg
    if cfg.pi_token is None:
        return
    if token is None or not secrets.compare_digest(token.credentials, cfg.pi_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Ungültiges Token")
