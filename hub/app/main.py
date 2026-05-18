"""FastAPI-Anwendung für den Hotsport-Access-Hub.

Verantwortlichkeiten:
- Heartbeat-Endpunkt für die Pis (mit Systeminfo + Config-Fingerprint-Antwort)
- `/api/config/{pi_id}` – Live-Konfiguration (API + Pi-Settings)
- Endpunkt, der die Soll-Version pro Pi liefert
- Statische Auslieferung der Release-ZIPs + SHA-256-Dateien
- HTML-Dashboard zum Setzen von Soll-Version, API-Settings und Pi-Settings
- Upload neuer Releases per Browser
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import api_test, config_view, db, devices, releases
from .auth import require_dashboard, require_pi_token
from .config import HubConfig, load

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def create_app(cfg: HubConfig | None = None) -> FastAPI:
    cfg = cfg or load()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.releases_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="Hotsport Access Hub", docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.db = db.connect(cfg.db_path)
    app.state.started_at = int(time.time())

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["age"] = _age_filter
    templates.env.filters["humansize"] = _humansize_filter
    templates.env.filters["humantime"] = _humantime_filter
    templates.env.filters["mask"] = _mask_filter
    templates.env.filters["dt"] = _dt_filter
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    _register_routes(app)
    return app


def _age_filter(ts: int | None) -> str:
    if not ts:
        return "—"
    delta = max(0, int(time.time()) - int(ts))
    if delta < 60:
        return f"vor {delta}s"
    if delta < 3600:
        return f"vor {delta // 60}m"
    if delta < 86400:
        return f"vor {delta // 3600}h"
    return f"vor {delta // 86400}d"


def _humansize_filter(mb: int | None) -> str:
    if mb is None:
        return "—"
    if mb < 1024:
        return f"{mb} MB"
    return f"{mb / 1024:.1f} GB"


def _humantime_filter(seconds: int | None) -> str:
    if not seconds:
        return "—"
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, _ = divmod(s, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _mask_filter(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "•" * len(value)
    return value[:2] + "•" * (len(value) - 4) + value[-2:]


def _dt_filter(ts: int | None) -> str:
    if not ts:
        return "—"
    import datetime
    return datetime.datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y %H:%M:%S")


def _resolve_hub_url(request: Request) -> str:
    """Erzeugt eine im LAN nutzbare Hub-Basis-URL aus dem aktuellen Request.

    Reihenfolge:
    1. `HOTSPORT_HUB_PUBLIC_URL` (env) – Operator-Override; immer Vorrang.
    2. Wenn der Operator das Dashboard schon über eine nicht-Loopback-IP
       aufruft, nutzen wir diese 1:1.
    3. Bei `127.0.0.1`/localhost auto-detect der LAN-IP – mit Präferenz für
       192.168.x, dann 10.x, dann 172.16-31.x. mDNS-Namen (`.local`) und
       VPN-Interfaces werden bewusst gemieden.
    """
    from urllib.parse import urlparse, urlunparse

    cfg: HubConfig = request.app.state.cfg
    if cfg.public_url:
        return cfg.public_url

    base = str(request.base_url).rstrip("/")
    try:
        parsed = urlparse(base)
        host = parsed.hostname or ""
        if host in ("127.0.0.1", "localhost", "::1"):
            lan_ip = _detect_lan_ip()
            if lan_ip:
                netloc = f"{lan_ip}:{parsed.port}" if parsed.port else lan_ip
                parsed = parsed._replace(netloc=netloc)
                return urlunparse(parsed).rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    return base


def _lan_ip_rank(ip: str) -> int:
    """Sortier-Rang für LAN-IPs: privater Bereich vor allem anderen."""
    if ip.startswith("192.168."):
        return 0
    if ip.startswith("10."):
        return 1
    try:
        second = int(ip.split(".")[1])
        if ip.startswith("172.") and 16 <= second <= 31:
            return 2
    except (ValueError, IndexError):
        pass
    return 3


def _detect_lan_ips() -> list[str]:
    """Findet *alle* LAN-IPv4-Adressen aller Interfaces.

    Kombination aus zwei Quellen, weil keine alleine zuverlässig ist:
    - UDP-Connect-Probes zu typischen Gateways (liefert die IP des
      Routing-Default-Interfaces – das ist meist das praktisch nutzbare).
    - ``getaddrinfo(gethostname())`` (liefert auf Linux/macOS oft auch
      Adressen anderer Interfaces, z.B. wenn der Hub mit Ethernet *und*
      WLAN gleichzeitig läuft).

    Loopback (127.0.0.0/8) und Link-Local (169.254.0.0/16) werden
    rausgefiltert. Sortiert nach Präferenz (private LANs zuerst).
    """
    import socket

    candidates: set[str] = set()

    def _accept(ip: str | None) -> None:
        if not ip:
            return
        if ip.startswith("127.") or ip.startswith("169.254."):
            return
        try:
            socket.inet_aton(ip)
        except OSError:
            return
        candidates.add(ip)

    def _probe(target_host: str, target_port: int) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.05)
            sock.connect((target_host, target_port))
            _accept(sock.getsockname()[0])
        except (OSError, socket.error):
            pass
        finally:
            sock.close()

    for gw in ("192.168.1.1", "192.168.0.1", "192.168.178.1",
               "10.0.0.1", "10.0.0.138", "172.20.0.1"):
        _probe(gw, 80)
    _probe("8.8.8.8", 80)
    _probe("1.1.1.1", 80)

    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, family=socket.AF_INET):
            _accept(info[4][0])
    except (OSError, socket.gaierror):
        pass

    return sorted(candidates, key=lambda ip: (_lan_ip_rank(ip), ip))


def _detect_lan_ip() -> str | None:
    """Erste/bevorzugte LAN-IP – Wrapper für Bestehendes."""
    found = _detect_lan_ips()
    return found[0] if found else None


def _hub_lan_urls(request: Request) -> list[str]:
    """Liste aller LAN-erreichbaren Hub-URLs für die Anzeige im Dashboard.

    Inkludiert eine eventuelle ``HOTSPORT_HUB_PUBLIC_URL`` an erster Stelle
    (Operator-Override) und alle automatisch erkannten LAN-IPs jeweils mit
    dem Port, unter dem das Dashboard gerade läuft.
    """
    from urllib.parse import urlparse

    cfg: HubConfig = request.app.state.cfg
    parsed = urlparse(str(request.base_url).rstrip("/"))
    scheme = parsed.scheme or "http"
    port = parsed.port

    out: list[str] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        u = url.rstrip("/")
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    if cfg.public_url:
        _add(cfg.public_url)

    for ip in _detect_lan_ips():
        netloc = f"{ip}:{port}" if port else ip
        _add(f"{scheme}://{netloc}")

    return out


def _register_routes(app: FastAPI) -> None:
    cfg: HubConfig = app.state.cfg

    # ---------------- Pi-API (vom Pi-Daemon aufgerufen) ----------------

    @app.post("/api/heartbeat", dependencies=[Depends(require_pi_token)])
    async def heartbeat(payload: dict, request: Request) -> dict:
        pi_id = str(payload.get("pi_id") or "").strip()
        if not pi_id:
            raise HTTPException(status_code=400, detail="pi_id fehlt")
        scan = payload.get("last_scan") or {}
        sysinfo = payload.get("sysinfo") or {}
        db.upsert_heartbeat(
            request.app.state.db,
            pi_id=pi_id,
            name=payload.get("name"),
            location=payload.get("location"),
            ip=request.client.host if request.client else None,
            current_version=payload.get("version"),
            healthy=bool(payload.get("healthy", True)),
            last_scan_at=scan.get("at"),
            last_scan_code=scan.get("code"),
            last_scan_grant=(int(bool(scan.get("granted"))) if scan else None),
            sysinfo=sysinfo,
        )
        live = config_view.build_for(request.app.state.db, pi_id)
        return {
            "ok": True,
            "desired_version": db.get_desired_version(request.app.state.db, pi_id),
            "config_fingerprint": live["fingerprint"],
            "config_complete": live["complete"],
        }

    @app.post("/api/scan", dependencies=[Depends(require_pi_token)])
    async def report_scan(payload: dict, request: Request) -> dict:
        pi_id = str(payload.get("pi_id") or "").strip()
        kind = str(payload.get("kind") or "scan").strip() or "scan"
        if not pi_id:
            raise HTTPException(status_code=400, detail="pi_id fehlt")
        code = payload.get("code")
        if kind == "scan" and not code:
            raise HTTPException(status_code=400, detail="code fehlt für scan")
        granted_raw = payload.get("granted")
        granted = None if granted_raw is None else bool(granted_raw)
        db.insert_scan(
            request.app.state.db,
            pi_id=pi_id,
            kind=kind,
            code=str(code) if code else None,
            granted=granted,
            reason=payload.get("reason"),
            scanned_at=int(payload.get("at") or time.time()),
        )
        return {"ok": True}

    @app.get("/api/config/{pi_id}", dependencies=[Depends(require_pi_token)])
    async def pi_config(pi_id: str, request: Request) -> dict:
        return config_view.build_for(request.app.state.db, pi_id)

    @app.get("/api/desired/{pi_id}", dependencies=[Depends(require_pi_token)])
    async def desired(pi_id: str, request: Request) -> dict:
        version = db.get_desired_version(request.app.state.db, pi_id)
        if not version:
            return {"version": None}
        rel = releases.get_release(cfg.releases_dir, version)
        if not rel:
            return {"version": None}
        base = str(request.base_url).rstrip("/")
        return {
            "version": rel.version,
            "url": f"{base}/releases/{rel.zip_path.name}",
            "sha256": rel.sha256,
            "size": rel.size_bytes,
        }

    @app.get("/api/latest-release", dependencies=[Depends(require_pi_token)])
    async def latest_release(request: Request) -> dict:
        """Liefert die aktuell verfügbare Release-Version für die Erstinstallation.

        Wird vom `install.sh`-Bootstrap-Script aufgerufen, wenn der Pi noch keine
        eigene Soll-Version hat. Sortierung erfolgt nach Versions-String absteigend
        (passt zu unserem Date-stamped Schema `YYYY.MM.DD-N`).
        """
        rels = releases.list_releases(cfg.releases_dir)
        if not rels:
            return {"version": None}
        rel = rels[0]  # list_releases sortiert bereits absteigend
        base = str(request.base_url).rstrip("/")
        return {
            "version": rel.version,
            "url": f"{base}/releases/{rel.zip_path.name}",
            "sha256": rel.sha256,
            "size": rel.size_bytes,
        }

    @app.get("/releases/{filename}")
    async def release_file(filename: str) -> FileResponse:
        # Pfad-Traversal abwehren
        safe = Path(filename).name
        path = cfg.releases_dir / safe
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Release nicht gefunden")
        return FileResponse(path)

    @app.get("/install.sh")
    async def install_script(request: Request) -> "Response":
        """Bootstrap-Installer für neue Pis – bewusst ohne Auth.

        Das Script enthält keine Geheimnisse. Die Hub-URL wird beim
        Ausliefern in den Marker `__HOTSPORT_HUB_URL__` eingesetzt,
        damit auf dem Pi nur noch `curl … | sudo bash -s -- TOKEN` nötig ist.
        """
        from fastapi.responses import Response
        path = STATIC_DIR / "install.sh"
        text = path.read_text(encoding="utf-8")
        text = text.replace("__HOTSPORT_HUB_URL__", _resolve_hub_url(request))
        return Response(
            content=text,
            media_type="text/x-shellscript; charset=utf-8",
        )

    # ---------------- Dashboard ----------------

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request, _: Annotated[str, Depends(require_dashboard)]
    ) -> HTMLResponse:
        rels = releases.list_releases(cfg.releases_dir)
        merged = _merged_pis(request.app.state.db)
        scans = db.recent_scans(request.app.state.db, limit=25)
        api_view = _api_settings_view(request.app.state.db)
        return request.app.state.templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "active_nav": "dashboard",
                "pis": merged,
                "devices_json_path": str(devices.resolve_devices_path() or ""),
                "releases": rels,
                "scans": scans,
                "api_settings": api_view,
                "now": int(time.time()),
                "offline_threshold": cfg.offline_threshold_seconds,
                "reader_modes": ("keyboard", "qr_camera", "rfid_mfrc522"),
                "hub_lan_urls": _hub_lan_urls(request),
            },
        )

    @app.get("/setup", response_class=HTMLResponse)
    async def setup(
        request: Request, _: Annotated[str, Depends(require_dashboard)]
    ) -> HTMLResponse:
        # Hub-URL aus dem Request ableiten, damit der Operator sie 1:1 in die
        # Bootstrap-Config kopieren kann. Wenn der Hub hinter einem Proxy steht,
        # nutzen wir den Forwarded-Host – sonst die Server-Bind-Adresse.
        hub_url = _resolve_hub_url(request)
        return request.app.state.templates.TemplateResponse(
            "setup.html",
            {
                "request": request,
                "active_nav": "setup",
                "now": int(time.time()),
                "offline_threshold": cfg.offline_threshold_seconds,
                "hub_url": hub_url,
                "hub_lan_urls": _hub_lan_urls(request),
                # Pi-Token aus der Hub-Config – wird auf der Setup-Seite
                # nur eingeblendet, wenn der Operator auf "Token anzeigen"
                # klickt. Setup-Seite selbst ist über Basic-Auth geschützt
                # (sofern HOTSPORT_HUB_DASHBOARD_USER gesetzt ist).
                "pi_token": cfg.pi_token,
            },
        )

    @app.get("/fragments/pis", response_class=HTMLResponse)
    async def pis_fragment(
        request: Request, _: Annotated[str, Depends(require_dashboard)]
    ) -> HTMLResponse:
        rels = releases.list_releases(cfg.releases_dir)
        merged = _merged_pis(request.app.state.db)
        return request.app.state.templates.TemplateResponse(
            "_pi_table.html",
            {
                "request": request,
                "active_nav": "dashboard",
                "pis": merged,
                "devices_json_path": str(devices.resolve_devices_path() or ""),
                "releases": rels,
                "now": int(time.time()),
                "offline_threshold": cfg.offline_threshold_seconds,
                "reader_modes": ("keyboard", "qr_camera", "rfid_mfrc522"),
            },
        )

    @app.get("/fragments/scans", response_class=HTMLResponse)
    async def scans_fragment(
        request: Request, _: Annotated[str, Depends(require_dashboard)]
    ) -> HTMLResponse:
        scans = db.recent_scans(request.app.state.db, limit=25)
        return request.app.state.templates.TemplateResponse(
            "_scans.html",
            {"request": request, "scans": scans},
        )

    @app.post("/admin/pi/{pi_id}/api-test", response_class=HTMLResponse)
    async def api_test_for_pi(
        request: Request,
        actor: Annotated[str, Depends(require_dashboard)],
        pi_id: str,
        code: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        """Simuliert vom Hub aus den `check-access`-Call dieses Pis.

        Verwendet die effektive Live-Konfiguration des Pis (devices.json +
        DB-Overrides), führt den HTTP-Call gegen die Binarytec-API und
        gibt das Ergebnis als HTML-Fragment für HTMX zurück. Das Ereignis
        wird unter ``kind='api_test'`` im Pi-Log gespeichert, damit der
        User später nachvollziehen kann, dass das ein Test war.
        """
        live = config_view.build_for(request.app.state.db, pi_id)
        api_cfg = live.get("api") or {}
        pi_cfg = live.get("pi") or {}

        result = api_test.run_api_test(
            base_url=str(api_cfg.get("base_url") or ""),
            bearer_token=str(api_cfg.get("bearer_token") or ""),
            interface_id=str(pi_cfg.get("interface_id") or ""),
            code=code,
            verify_tls=bool(api_cfg.get("verify_tls", False)),
            connect_timeout_s=float(api_cfg.get("connect_timeout_seconds") or 1.0),
            request_timeout_s=float(api_cfg.get("request_timeout_seconds") or 2.0),
        )

        # Test im Pi-Event-Log persistieren, damit es im "Letzte Ereignisse"-
        # Bereich auftaucht und sofort als Test (nicht als echter Scan)
        # erkennbar ist.
        reason = (
            f"by={actor} status={result.http_status} "
            f"latency={result.latency_ms}ms detail={result.detail}"
        )
        db.insert_scan(
            request.app.state.db,
            pi_id=pi_id,
            kind="api_test",
            code=code or None,
            granted=result.granted,
            reason=reason,
            scanned_at=int(time.time()),
        )

        return request.app.state.templates.TemplateResponse(
            "_api_test_result.html",
            {"request": request, "pi_id": pi_id, "code": code, "r": result},
        )

    @app.post("/admin/set-version")
    async def set_version(
        request: Request,
        actor: Annotated[str, Depends(require_dashboard)],
        pi_id: Annotated[str, Form()],
        version: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        clean = version.strip() or None
        if pi_id == "*":
            if not clean:
                raise HTTPException(status_code=400, detail="Version fehlt")
            db.set_desired_version_for_all(request.app.state.db, clean, actor=actor)
        else:
            db.set_desired_version(request.app.state.db, pi_id, clean, actor=actor)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/admin/api-settings")
    async def api_settings_form(
        request: Request,
        actor: Annotated[str, Depends(require_dashboard)],
        api_base_url: Annotated[str, Form()],
        api_verify_tls: Annotated[str, Form()] = "false",
        api_bearer_token: Annotated[str, Form()] = "",
        api_connect_timeout_seconds: Annotated[str, Form()] = "1.0",
        api_request_timeout_seconds: Annotated[str, Form()] = "2.0",
    ) -> RedirectResponse:
        conn = request.app.state.db
        db.set_setting(conn, "api.base_url", api_base_url.strip(), actor=actor)
        db.set_setting(
            conn, "api.verify_tls", api_verify_tls.strip(), actor=actor
        )
        db.set_setting(
            conn, "api.connect_timeout_seconds",
            api_connect_timeout_seconds.strip(), actor=actor,
        )
        db.set_setting(
            conn, "api.request_timeout_seconds",
            api_request_timeout_seconds.strip(), actor=actor,
        )
        # Token nur überschreiben, wenn das Feld gefüllt wurde
        if api_bearer_token.strip():
            db.set_setting(
                conn, "api.bearer_token", api_bearer_token.strip(), actor=actor
            )
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/admin/pi-settings")
    async def pi_settings_form(
        request: Request,
        actor: Annotated[str, Depends(require_dashboard)],
        pi_id: Annotated[str, Form()],
        name: Annotated[str, Form()] = "",
        location: Annotated[str, Form()] = "",
        notes: Annotated[str, Form()] = "",
        enabled: Annotated[str, Form()] = "",
        interface_id: Annotated[str, Form()] = "",
        inout: Annotated[str, Form()] = "in",
        reader_mode: Annotated[str, Form()] = "keyboard",
        reader_device_path: Annotated[str, Form()] = "",
        reader_camera_index: Annotated[str, Form()] = "0",
        relay_pin: Annotated[str, Form()] = "24",
        relay_pulse_seconds: Annotated[str, Form()] = "1.0",
        buzzer_pin: Annotated[str, Form()] = "23",
    ) -> RedirectResponse:
        if reader_mode not in ("keyboard", "qr_camera", "rfid_mfrc522"):
            raise HTTPException(status_code=400, detail="Ungültiger Reader-Modus")
        if inout not in ("in", "out"):
            raise HTTPException(status_code=400, detail="inout muss 'in' oder 'out' sein")
        try:
            fields = {
                "name": name or None,
                "location": location or None,
                "notes": notes or None,
                "enabled": 1 if enabled.strip().lower() in ("1", "on", "true", "yes") else 0,
                "interface_id": interface_id.strip() or None,
                "inout": inout,
                "reader_mode": reader_mode,
                "reader_device_path": reader_device_path.strip() or None,
                "reader_camera_index": int(reader_camera_index or 0),
                "relay_pin": int(relay_pin),
                "relay_pulse_seconds": float(relay_pulse_seconds),
                "buzzer_pin": int(buzzer_pin),
            }
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Ungültige Eingabe: {e}") from e
        db.update_pi_settings(
            request.app.state.db, pi_id=pi_id.strip(), fields=fields, actor=actor
        )
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/admin/upload")
    async def upload_release(
        request: Request,
        actor: Annotated[str, Depends(require_dashboard)],
        version: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
    ) -> RedirectResponse:
        version = version.strip()
        if not version or not all(
            c.isalnum() or c in "._-" for c in version
        ):
            raise HTTPException(status_code=400, detail="Ungültige Version")
        target = cfg.releases_dir / f"hotsport-access-{version}.zip"
        if target.exists():
            raise HTTPException(status_code=409, detail="Version existiert bereits")
        with target.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        releases.ensure_sha256(target)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "service": "hotsport-hub",
            "uptime_seconds": int(time.time()) - app.state.started_at,
        }

    def _merged_pis(conn) -> list[dict]:
        """Pi-Liste fürs Dashboard: Solldaten aus devices.json + Live-Daten +
        die letzten 100 Ereignisse pro Pi (Scans + Service-Events)."""
        dev_list = devices.list_devices()
        db_rows = [dict(row) for row in db.list_pis(conn)]
        merged = devices.merge_for_dashboard(dev_list, db_rows)
        for p in merged:
            p["events"] = [
                dict(row) for row in db.recent_events(conn, pi_id=p["pi_id"], limit=100)
            ]
        return merged

    def _api_settings_view(conn) -> dict[str, str]:
        """API-Settings für die Dashboard-Anzeige.

        Reihenfolge: DB (Override) > devices.json > leer.
        Das Token wird aus der DB direkt durchgereicht (mit `mask`-Filter
        rendert es das Template als ``****abcd``).
        """
        db_settings = db.get_settings(conn, prefix="api.")
        dev_api = devices.api_settings()

        def merge(db_key: str, dev_key: str, default: str = "") -> str:
            v = db_settings.get(db_key)
            if v not in (None, ""):
                return str(v)
            v = dev_api.get(dev_key)
            if v not in (None, ""):
                # Booleans aus JSON in TOML-Schreibweise rendern.
                if isinstance(v, bool):
                    return "true" if v else "false"
                return str(v)
            return default

        return {
            "api.base_url": merge("api.base_url", "base_url"),
            "api.bearer_token": db_settings.get("api.bearer_token", ""),
            "api.verify_tls": merge("api.verify_tls", "verify_tls", "false"),
            "api.connect_timeout_seconds": merge(
                "api.connect_timeout_seconds", "connect_timeout_seconds", "1.0"
            ),
            "api.request_timeout_seconds": merge(
                "api.request_timeout_seconds", "request_timeout_seconds", "2.0"
            ),
        }


app = create_app()
