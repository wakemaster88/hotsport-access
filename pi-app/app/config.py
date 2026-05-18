"""Pi-Konfiguration.

Drei Quellen – in absteigender Priorität:

1. **Inline-Live-Config in `/etc/hotsport-access/config.toml`** (Standardweg):
   - `config.toml` enthält neben pi_id/name/location auch komplette Sections
     [api], [pi], [pi.reader] – damit kann der Daemon ohne Hub-Verbindung
     sofort scannen. Wird vom install.sh aus `devices.json` + interaktiv
     eingegebenem API-Bearer-Token befüllt.

2. **Live-Config vom Hub** (Override + Updates):
   - Wenn ein Hub konfiguriert ist und antwortet, kann er die Live-Config
     pro Pi überschreiben (Dashboard-Edit). Der Daemon übernimmt das beim
     nächsten Heartbeat.

3. **Cache** (`state_dir/live_config.json`):
   - Zuletzt bekannte Hub-Config; Fallback wenn Hub kurz nicht erreichbar.

Hub-Verbindung ist OPTIONAL: ohne `[hub] base_url` läuft der Pi autonom,
schickt nur keine Heartbeats und bekommt keine Updates.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _load_toml(path: Path) -> dict[str, Any]:
    """Lazy import von tomllib/tomli, damit Module ohne TOML-Bedarf importierbar
    bleiben (z.B. nur `parse_live`)."""
    if sys.version_info >= (3, 11):
        import tomllib  # type: ignore[import-not-found]
    else:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]
    with path.open("rb") as fh:
        return tomllib.load(fh)


DEFAULT_CONFIG_PATH = Path(
    os.environ.get("HOTSPORT_ACCESS_CONFIG", "/etc/hotsport-access/config.toml")
)


# ---------- Bootstrap-Config ----------


@dataclass(frozen=True)
class HubBootstrap:
    base_url: str = ""
    pi_token: str = ""
    discover: bool = False
    hub_port: int = 8000
    discover_interval_seconds: float = 15.0
    heartbeat_interval_seconds: float = 5.0
    update_check_interval_seconds: float = 30.0


@dataclass(frozen=True)
class Bootstrap:
    pi_id: str
    name: str
    location: str
    state_dir: Path
    health_bind_host: str
    health_bind_port: int
    hub: HubBootstrap = field(default_factory=HubBootstrap)


def _parse_hub(raw: dict[str, Any]) -> HubBootstrap:
    discover = raw.get("discover", False)
    if isinstance(discover, str):
        discover = discover.strip().lower() in ("true", "1", "yes")
    return HubBootstrap(
        base_url=str(raw.get("base_url") or "").strip(),
        pi_token=str(raw.get("pi_token") or "").strip(),
        discover=bool(discover),
        hub_port=_to_int(raw.get("hub_port"), 8000),
        discover_interval_seconds=_to_float(raw.get("discover_interval_seconds"), 15.0),
        heartbeat_interval_seconds=_to_float(raw.get("heartbeat_interval_seconds"), 5.0),
        update_check_interval_seconds=_to_float(
            raw.get("update_check_interval_seconds"), 30.0
        ),
    )


def load_bootstrap(path: Path | None = None) -> Bootstrap:
    p = path or DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if p.is_file():
        raw = _load_toml(p)

    pi_id = (raw.get("pi_id") or os.uname().nodename).strip()
    return Bootstrap(
        pi_id=pi_id,
        name=raw.get("name") or pi_id,
        location=raw.get("location", ""),
        state_dir=Path(raw.get("state_dir", "/var/lib/hotsport-access")),
        health_bind_host=raw.get("health_bind_host", "127.0.0.1"),
        health_bind_port=int(raw.get("health_bind_port", 8765)),
        hub=_parse_hub(raw.get("hub") or {}),
    )


# ---------- Live-Config (vom Hub) ----------


@dataclass(frozen=True)
class GpioConfig:
    relay_pin: int = 24
    relay_pulse_seconds: float = 1.0
    buzzer_pin: int = 23


@dataclass(frozen=True)
class ApiConfig:
    base_url: str = ""
    interface_id: str = ""
    bearer_token: str = ""
    inout: str = "in"
    verify_tls: bool | str = False
    request_timeout_seconds: float = 2.0
    connect_timeout_seconds: float = 1.0


@dataclass(frozen=True)
class ReaderConfig:
    mode: str = "keyboard"
    device_path: str = "/dev/input/event0"
    camera_index: int = 0


@dataclass(frozen=True)
class LiveConfig:
    fingerprint: str
    complete: bool
    enabled: bool
    api: ApiConfig
    gpio: GpioConfig
    reader: ReaderConfig

    def to_json(self) -> str:
        def conv(o: Any) -> Any:
            return o.__dict__ if hasattr(o, "__dict__") else o
        return json.dumps(self, default=conv, sort_keys=True, indent=2)


def load_inline_live(path: Path | None = None) -> LiveConfig | None:
    """Liest eine vollständige LiveConfig direkt aus `config.toml`.

    Gibt `None` zurück, wenn die Sections fehlen oder unvollständig sind –
    dann fällt der Daemon auf Cache+Hub zurück. Akzeptierte TOML-Struktur::

        [api]
        base_url     = "https://192.168.251.50:444"
        bearer_token = "..."
        verify_tls   = false

        [pi]
        interface_id = "101"
        inout        = "in"
        enabled      = true
        relay_pin    = 24
        buzzer_pin   = 23
        relay_pulse_seconds = 1.0

        [pi.reader]
        mode         = "keyboard"
        device_path  = "/dev/input/event0"
        camera_index = 0
    """
    p = path or DEFAULT_CONFIG_PATH
    if not p.is_file():
        return None
    raw = _load_toml(p)
    api = raw.get("api") or {}
    pi = raw.get("pi") or {}

    # Mindestanforderung: api.base_url + pi.interface_id müssen gesetzt sein,
    # sonst kann die Pi-App keinen einzigen Scan validieren.
    if not (api.get("base_url") and pi.get("interface_id")):
        return None

    payload = {
        "api": api,
        "pi": pi,
        # Fingerprint deterministisch aus den Werten ableiten, damit Hub-Pulls
        # mit identischem Inhalt nicht als „geändert" gelten.
        "fingerprint": _fingerprint(api, pi),
        "complete": True,
    }
    return parse_live(payload)


def _fingerprint(api: dict[str, Any], pi: dict[str, Any]) -> str:
    import hashlib
    canonical = json.dumps(
        {"api": api, "pi": pi}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def parse_live(payload: dict[str, Any]) -> LiveConfig:
    api = payload.get("api") or {}
    pi = payload.get("pi") or {}
    reader = pi.get("reader") or {}

    enabled_raw = pi.get("enabled", True)
    enabled = bool(enabled_raw) if enabled_raw is not None else True

    return LiveConfig(
        fingerprint=str(payload.get("fingerprint") or ""),
        complete=bool(payload.get("complete")),
        enabled=enabled,
        api=ApiConfig(
            base_url=str(api.get("base_url") or "").rstrip("/"),
            interface_id=str(pi.get("interface_id") or ""),
            bearer_token=str(api.get("bearer_token") or ""),
            inout=str(pi.get("inout") or "in"),
            verify_tls=_bool_or_str(api.get("verify_tls")),
            connect_timeout_seconds=_to_float(api.get("connect_timeout_seconds"), 1.0),
            request_timeout_seconds=_to_float(api.get("request_timeout_seconds"), 2.0),
        ),
        gpio=GpioConfig(
            relay_pin=_to_int(pi.get("relay_pin"), 24),
            relay_pulse_seconds=_to_float(pi.get("relay_pulse_seconds"), 1.0),
            buzzer_pin=_to_int(pi.get("buzzer_pin"), 23),
        ),
        reader=ReaderConfig(
            mode=str(reader.get("mode") or "keyboard"),
            device_path=str(reader.get("device_path") or "/dev/input/event0"),
            camera_index=_to_int(reader.get("camera_index"), 0),
        ),
    )


def _bool_or_str(v: Any) -> bool | str:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip()
    if s.lower() in ("true", "1", "yes"):
        return True
    if s.lower() in ("false", "0", "no", ""):
        return False
    return s


def _to_int(v: Any, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _to_float(v: Any, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# ---------- Cache ----------


def cache_path(boot: Bootstrap) -> Path:
    return boot.state_dir / "live_config.json"


def save_cache(boot: Bootstrap, payload: dict[str, Any]) -> None:
    p = cache_path(boot)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(p)


def load_cache(boot: Bootstrap) -> dict[str, Any] | None:
    p = cache_path(boot)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None
