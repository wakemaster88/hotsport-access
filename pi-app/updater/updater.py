"""Pi-Updater: pollt den Hub, lädt neue Releases, swapt atomar, rollback bei Fehler.

Aufruf: `python -m updater.updater` (oder via systemd-Unit).

Ablauf pro Tick:
1. GET /api/desired/<pi_id>
2. Wenn Soll-Version != aktuelle Version (aus /opt/hotsport-access/current/VERSION):
   a) Release-ZIP laden, SHA-256 prüfen
   b) in /opt/hotsport-access/releases/<version>/ entpacken
   c) Symlink /opt/hotsport-access/current → neuen Pfad swapen
   d) hotsport-access.service neu starten
   e) Health prüfen (lokaler /health-Endpunkt)
   f) bei Misserfolg: Symlink zurück, Service erneut starten
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


log = logging.getLogger("hotsport.updater")

INSTALL_ROOT = Path("/opt/hotsport-access")
RELEASES_DIR = INSTALL_ROOT / "releases"
CURRENT_LINK = INSTALL_ROOT / "current"
LAST_GOOD_FILE = INSTALL_ROOT / "last_good"
SERVICE_NAME = "hotsport-access.service"


def _read_config(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _http_request(url: str, *, token: str | None, method: str = "GET") -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (LAN only)
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""


def _download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as resp, dest.open("wb") as fh:  # noqa: S310
        shutil.copyfileobj(resp, fh)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _current_version() -> str | None:
    vfile = CURRENT_LINK / "VERSION"
    try:
        return vfile.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _atomic_symlink(target: Path, link: Path) -> Path | None:
    """Setze Symlink atomar. Gibt den vorherigen Ziel-Pfad zurück, falls vorhanden."""
    previous: Path | None = None
    if link.is_symlink() or link.exists():
        try:
            previous = Path(os.readlink(link))
        except OSError:
            previous = None
    tmp = link.with_name(link.name + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target, target_is_directory=True)
    os.replace(tmp, link)
    return previous


def _systemctl(action: str) -> int:
    return subprocess.run(  # noqa: S603,S607
        ["systemctl", action, SERVICE_NAME], check=False
    ).returncode


def _is_healthy(timeout_seconds: int, host: str, port: int) -> bool:
    deadline = time.time() + timeout_seconds
    url = f"http://{host}:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                if resp.status == 200:
                    body = json.loads(resp.read().decode("utf-8") or "{}")
                    if body.get("ok"):
                        return True
        except (urllib.error.URLError, ValueError):
            pass
        time.sleep(1)
    return False


def _apply_update(desired: dict, cfg: dict) -> bool:
    version: str = desired["version"]
    url: str = desired["url"]
    expected_sha: str = desired["sha256"].lower()

    target_dir = RELEASES_DIR / version
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    zip_path = RELEASES_DIR / f"hotsport-access-{version}.zip"
    log.info("Download %s -> %s", url, zip_path)
    _download(url, zip_path)
    actual_sha = _sha256(zip_path)
    if actual_sha != expected_sha:
        log.error(
            "SHA-256 mismatch (expected=%s actual=%s) – Release verworfen",
            expected_sha, actual_sha,
        )
        zip_path.unlink(missing_ok=True)
        shutil.rmtree(target_dir, ignore_errors=True)
        return False

    log.info("Entpacken nach %s", target_dir)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target_dir)

    extracted_root = _find_app_root(target_dir, version)
    previous = _atomic_symlink(extracted_root, CURRENT_LINK)
    log.info("Symlink current -> %s (vorher: %s)", extracted_root, previous)

    log.info("Service neu starten")
    _systemctl("restart")

    health_host = cfg.get("health_bind_host", "127.0.0.1")
    health_port = int(cfg.get("health_bind_port", 8765))
    if _is_healthy(timeout_seconds=20, host=health_host, port=health_port):
        log.info("Update %s OK", version)
        LAST_GOOD_FILE.write_text(str(extracted_root))
        return True

    log.error("Health nach Update %s fehlgeschlagen – Rollback", version)
    if previous and previous.exists():
        _atomic_symlink(previous, CURRENT_LINK)
        _systemctl("restart")
    return False


def _find_app_root(extract_dir: Path, version: str) -> Path:
    """ZIPs können das `pi-app/`-Verzeichnis als Wurzel oder einen einzigen
    Sub-Ordner enthalten. Wir suchen die Datei VERSION und nehmen deren Ordner.
    """
    direct = extract_dir / "VERSION"
    if direct.is_file():
        return extract_dir
    for child in extract_dir.iterdir():
        if child.is_dir() and (child / "VERSION").is_file():
            return child
    # Fallback: Verzeichnis selbst, mit nachträglich erzeugter VERSION-Datei
    (extract_dir / "VERSION").write_text(version + "\n")
    return extract_dir


def _tick(cfg: dict) -> None:
    hub = cfg.get("hub") or {}
    base_url = (hub.get("base_url") or "").rstrip("/")
    pi_id = cfg.get("pi_id")
    token = hub.get("pi_token")
    if not base_url or not pi_id:
        log.debug("Kein Hub konfiguriert – nichts zu tun")
        return

    status, body = _http_request(f"{base_url}/api/desired/{pi_id}", token=token)
    if status != 200:
        log.warning("Hub /api/desired/%s antwortet mit %s", pi_id, status)
        return

    try:
        desired = json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        log.warning("Ungültiges JSON vom Hub")
        return

    if not desired.get("version"):
        log.debug("Keine Soll-Version gesetzt")
        return

    if desired["version"] == _current_version():
        log.debug("Bereits auf Soll-Version %s", desired["version"])
        return

    log.info("Wechsel %s -> %s", _current_version(), desired["version"])
    _apply_update(desired, cfg)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    parser = argparse.ArgumentParser(prog="hotsport-updater")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("HOTSPORT_ACCESS_CONFIG", "/etc/hotsport-access/config.toml")),
    )
    parser.add_argument("--once", action="store_true", help="Einmal prüfen und beenden")
    args = parser.parse_args()

    cfg = _read_config(args.config) if args.config.is_file() else {}
    INSTALL_ROOT.mkdir(parents=True, exist_ok=True)
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)

    interval = float((cfg.get("hub") or {}).get("update_check_interval_seconds", 30))
    while True:
        try:
            _tick(cfg)
        except Exception as e:  # noqa: BLE001
            log.exception("Updater-Fehler: %s", e)
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
