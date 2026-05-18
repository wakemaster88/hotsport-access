"""Konfiguration für den Hub.

Wir lesen ausschließlich aus Umgebungsvariablen mit klaren Defaults. Das passt
gut zu einer systemd-Unit mit `EnvironmentFile=`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HubConfig:
    data_dir: Path
    releases_dir: Path
    db_path: Path
    bind_host: str
    bind_port: int
    public_url: str | None
    dashboard_user: str | None
    dashboard_password: str | None
    pi_token: str | None
    offline_threshold_seconds: int


def load() -> HubConfig:
    data_dir = Path(os.environ.get("HOTSPORT_HUB_DATA_DIR", "/var/lib/hotsport-hub"))
    releases_dir = Path(
        os.environ.get("HOTSPORT_HUB_RELEASES_DIR", str(data_dir / "releases"))
    )
    db_path = Path(os.environ.get("HOTSPORT_HUB_DB", str(data_dir / "hub.sqlite3")))
    public_url = os.environ.get("HOTSPORT_HUB_PUBLIC_URL")
    if public_url:
        public_url = public_url.rstrip("/")
    return HubConfig(
        data_dir=data_dir,
        releases_dir=releases_dir,
        db_path=db_path,
        bind_host=os.environ.get("HOTSPORT_HUB_HOST", "0.0.0.0"),
        bind_port=int(os.environ.get("HOTSPORT_HUB_PORT", "8000")),
        public_url=public_url,
        dashboard_user=os.environ.get("HOTSPORT_HUB_DASHBOARD_USER"),
        dashboard_password=os.environ.get("HOTSPORT_HUB_DASHBOARD_PASSWORD"),
        pi_token=os.environ.get("HOTSPORT_HUB_PI_TOKEN"),
        offline_threshold_seconds=int(
            os.environ.get("HOTSPORT_HUB_OFFLINE_THRESHOLD_SECONDS", "30")
        ),
    )
