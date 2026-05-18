"""Pi-Updater: pollt GitHub, fährt bei neuem Commit `install.sh` aus,
prüft Health und rollt bei Misserfolg zurück.

Ablauf pro Tick:

1. ``git -C <repo> fetch origin <branch>``
2. Vergleiche lokalen HEAD mit ``origin/<branch>``.
3. Wenn unterschiedlich (und Commit nicht in ``bad_commits.txt``):
   a. Aktuellen HEAD-SHA als ``last_good_commit`` ablegen.
   b. Schreibe ``in_progress.json`` mit dem Ziel-Commit.
   c. ``git reset --hard origin/<branch>``.
   d. ``bash pi-app/scripts/install.sh -y <pi_id>`` ausführen, mit
      ``HOTSPORT_NO_UPDATER_RESTART=1`` damit der Updater sich nicht
      selbst killt.
4. Beim *Start* des Updaters: wenn ``in_progress.json`` da ist,
   warte ``health_check_delay_seconds`` und prüfe lokalen
   ``/health``-Endpoint. Falls nicht healthy: rollback per
   ``git reset --hard <last_good_commit>`` + ``install.sh``,
   schreibe Ziel-Commit in ``bad_commits.txt`` damit er nicht
   gleich wieder probiert wird, und melde ``update_rolled_back``
   an den Hub. Sonst ``update_applied`` melden.

Erfolgs-Signal kommt über den lokalen ``/health``-Endpoint des
Daemons – der Updater macht keine Vermutungen über Pakete oder
Zips, sondern verlässt sich auf die idempotente ``install.sh``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


log = logging.getLogger("hotsport.updater")

INSTALL_ROOT = Path("/opt/hotsport-access")
STATE_DIR = Path("/var/lib/hotsport-access/updater")
IN_PROGRESS_FILE = STATE_DIR / "in_progress.json"
LAST_GOOD_FILE = STATE_DIR / "last_good_commit"
BAD_COMMITS_FILE = STATE_DIR / "bad_commits.txt"

ACCESS_SERVICE = "hotsport-access.service"


# ---------- Hilfen ----------

def _read_config(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _git(repo: Path, *args: str, check: bool = True, timeout: float = 60.0) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(repo), *args]
    log.debug("git %s", " ".join(args))
    return subprocess.run(  # noqa: S603,S607
        cmd, check=check, capture_output=True, text=True, timeout=timeout,
    )


def _git_head(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def _git_describe(repo: Path, sha: str) -> str:
    """Kurze Beschreibung für Log-Output: 'a1b2c3d Kommittitel'."""
    try:
        out = _git(repo, "log", "-1", "--format=%h %s", sha).stdout.strip()
        return out or sha[:8]
    except subprocess.CalledProcessError:
        return sha[:8]


def _is_healthy(*, timeout_seconds: int, host: str, port: int) -> bool:
    deadline = time.time() + timeout_seconds
    url = f"http://{host}:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                if resp.status == 200:
                    body = json.loads(resp.read().decode("utf-8") or "{}")
                    if body.get("ok"):
                        return True
        except (urllib.error.URLError, ValueError, OSError):
            pass
        time.sleep(1.0)
    return False


def _bad_commits() -> set[str]:
    if not BAD_COMMITS_FILE.exists():
        return set()
    return {
        line.strip().split()[0]
        for line in BAD_COMMITS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _mark_bad(sha: str, reason: str) -> None:
    BAD_COMMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"{sha} {int(time.time())} {reason}\n"
    with BAD_COMMITS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _post_event_to_hub(cfg: dict, *, kind: str, reason: str) -> None:
    """Pusht ein Update-Event direkt an den Hub.

    Geht über /api/scan – derselbe Endpoint, den der Daemon nutzt – damit
    es im Pi-Detail-Log neben den Scans landet. Best-effort, kein Retry.
    """
    hub = cfg.get("hub") or {}
    base_url = (hub.get("base_url") or "").rstrip("/")
    pi_token = hub.get("pi_token") or ""
    pi_id = cfg.get("pi_id") or ""
    if not (base_url and pi_token and pi_id):
        return
    payload = {
        "pi_id": pi_id, "kind": kind, "code": None,
        "granted": None, "reason": reason, "at": int(time.time()),
    }
    req = urllib.request.Request(
        f"{base_url}/api/scan",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {pi_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3.0).read()  # noqa: S310 (LAN)
    except (urllib.error.URLError, OSError) as e:
        log.warning("Hub-Push %s fehlgeschlagen (%s)", kind, e)


# ---------- Updater-Settings aus config.toml ----------

@dataclass(frozen=True)
class UpdaterCfg:
    enabled: bool
    git_repo: Path
    branch: str
    install_script: Path
    pi_id: str
    check_interval_s: float
    health_check_delay_s: float
    health_check_timeout_s: float
    health_host: str
    health_port: int


def _load_updater_cfg(cfg: dict) -> UpdaterCfg | None:
    upd = cfg.get("updater") or {}
    repo_str = (upd.get("git_repo") or "").strip()
    if not repo_str:
        return None
    repo = Path(repo_str)
    if not (repo / ".git").exists():
        log.warning("git_repo %s ist kein git-Repo – Auto-Update aus", repo)
        return None
    install_script = Path(
        upd.get("install_script") or repo / "pi-app" / "scripts" / "install.sh"
    )
    return UpdaterCfg(
        enabled=bool(upd.get("enabled", True)),
        git_repo=repo,
        branch=str(upd.get("branch") or "main"),
        install_script=install_script,
        pi_id=str(cfg.get("pi_id") or ""),
        check_interval_s=float(upd.get("check_interval_seconds") or 300.0),
        health_check_delay_s=float(upd.get("health_check_delay_seconds") or 30.0),
        health_check_timeout_s=float(upd.get("health_check_timeout_seconds") or 60.0),
        health_host=str(cfg.get("health_bind_host") or "127.0.0.1"),
        health_port=int(cfg.get("health_bind_port") or 8765),
    )


# ---------- Hauptlogik ----------

def _run_install(uc: UpdaterCfg) -> int:
    cmd = ["bash", str(uc.install_script), "-y", uc.pi_id]
    env = os.environ.copy()
    env["HOTSPORT_NO_UPDATER_RESTART"] = "1"
    log.info("Starte install.sh für Pi %s …", uc.pi_id)
    proc = subprocess.run(  # noqa: S603,S607
        cmd, env=env, check=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        log.error("install.sh exit=%s\nSTDOUT:\n%s\nSTDERR:\n%s",
                  proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:])
    else:
        log.info("install.sh OK")
    return proc.returncode


def _finalize_in_progress(cfg: dict, uc: UpdaterCfg) -> None:
    """Wird beim Updater-Start aufgerufen. Wenn ein Update mitten drin
    war, prüft jetzt die Health und entscheidet commit/rollback."""
    if not IN_PROGRESS_FILE.exists():
        return
    try:
        info = json.loads(IN_PROGRESS_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        log.warning("in_progress.json unlesbar – wird ignoriert.")
        IN_PROGRESS_FILE.unlink(missing_ok=True)
        return

    target = info.get("target_commit") or ""
    last_good = info.get("last_good_commit") or ""

    log.info(
        "Update-Finalize: warte %.0fs auf Health …", uc.health_check_delay_s
    )
    time.sleep(uc.health_check_delay_s)
    healthy = _is_healthy(
        timeout_seconds=int(uc.health_check_timeout_s),
        host=uc.health_host, port=uc.health_port,
    )
    if healthy:
        log.info("Update auf %s ist healthy – committed.", _git_describe(uc.git_repo, target))
        LAST_GOOD_FILE.write_text(target + "\n", encoding="utf-8")
        IN_PROGRESS_FILE.unlink(missing_ok=True)
        _post_event_to_hub(
            cfg, kind="update_applied",
            reason=f"commit={target[:8]} {_git_describe(uc.git_repo, target)}",
        )
        return

    log.error(
        "Update auf %s NICHT healthy – Rollback auf %s",
        _git_describe(uc.git_repo, target),
        _git_describe(uc.git_repo, last_good) if last_good else "unbekannt",
    )
    _mark_bad(target, "health-check failed after install")
    IN_PROGRESS_FILE.unlink(missing_ok=True)
    _post_event_to_hub(
        cfg, kind="update_failed",
        reason=f"target={target[:8]} health-check failed, rollback to {last_good[:8] if last_good else '?'}",
    )

    if not last_good:
        log.error("Kein last_good_commit bekannt – manueller Eingriff nötig.")
        return

    try:
        _git(uc.git_repo, "reset", "--hard", last_good)
    except subprocess.CalledProcessError as e:
        log.error("git reset auf %s fehlgeschlagen: %s", last_good[:8], e.stderr)
        return

    rc = _run_install(uc)
    if rc != 0:
        log.error("Rollback-install.sh exit=%d – manueller Eingriff nötig.", rc)
        _post_event_to_hub(
            cfg, kind="update_rolled_back",
            reason=f"rollback to {last_good[:8]} install.sh exit={rc}",
        )
        return
    if _is_healthy(
        timeout_seconds=int(uc.health_check_timeout_s),
        host=uc.health_host, port=uc.health_port,
    ):
        log.info("Rollback auf %s healthy – Drehkreuz wieder online.", last_good[:8])
        LAST_GOOD_FILE.write_text(last_good + "\n", encoding="utf-8")
        _post_event_to_hub(
            cfg, kind="update_rolled_back",
            reason=f"healthy on {last_good[:8]} after rollback",
        )
    else:
        log.error("Rollback war nicht healthy – Drehkreuz offline!")
        _post_event_to_hub(
            cfg, kind="update_rolled_back",
            reason=f"rollback {last_good[:8]} NOT healthy – manuell prüfen!",
        )


def _check_and_apply(cfg: dict, uc: UpdaterCfg) -> None:
    try:
        _git(uc.git_repo, "fetch", "--quiet", "origin", uc.branch, timeout=30.0)
    except subprocess.CalledProcessError as e:
        log.warning("git fetch fehlgeschlagen: %s", (e.stderr or "").strip()[:200])
        return
    except subprocess.TimeoutExpired:
        log.warning("git fetch Timeout – nächster Tick versucht es neu.")
        return

    local = _git_head(uc.git_repo, "HEAD")
    remote = _git_head(uc.git_repo, f"origin/{uc.branch}")
    if local == remote:
        return

    bad = _bad_commits()
    if remote in bad:
        log.warning(
            "Origin/%s steht auf bekannt-kaputtem Commit %s – warte auf Fix.",
            uc.branch, remote[:8],
        )
        return

    log.info(
        "Neuer Commit %s (lokal: %s) – starte Update.",
        _git_describe(uc.git_repo, remote), local[:8],
    )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    IN_PROGRESS_FILE.write_text(
        json.dumps({
            "target_commit": remote,
            "last_good_commit": local,
            "started_at": int(time.time()),
        }), encoding="utf-8",
    )

    try:
        _git(uc.git_repo, "reset", "--hard", remote)
    except subprocess.CalledProcessError as e:
        log.error("git reset --hard %s fehlgeschlagen: %s", remote[:8], e.stderr)
        IN_PROGRESS_FILE.unlink(missing_ok=True)
        return

    rc = _run_install(uc)
    if rc != 0:
        # install.sh ist gescheitert – keine Health-Check-Phase, sofort
        # zurück und Rollback einleiten (in_progress.json bleibt liegen
        # falls der Updater jetzt durch install.sh restartet wird).
        _mark_bad(remote, f"install.sh exit={rc}")
        _post_event_to_hub(
            cfg, kind="update_failed",
            reason=f"install.sh exit={rc} for {remote[:8]}",
        )
        try:
            _git(uc.git_repo, "reset", "--hard", local)
        except subprocess.CalledProcessError:
            pass
        _run_install(uc)
        IN_PROGRESS_FILE.unlink(missing_ok=True)
        return

    # install.sh hat den hotsport-access neu gestartet, den Updater aber
    # NICHT (HOTSPORT_NO_UPDATER_RESTART=1). Health-Check zur Verifikation.
    log.info("Update angewandt – warte %.0fs auf Health …", uc.health_check_delay_s)
    time.sleep(uc.health_check_delay_s)
    healthy = _is_healthy(
        timeout_seconds=int(uc.health_check_timeout_s),
        host=uc.health_host, port=uc.health_port,
    )
    if healthy:
        LAST_GOOD_FILE.write_text(remote + "\n", encoding="utf-8")
        IN_PROGRESS_FILE.unlink(missing_ok=True)
        log.info("Update %s OK", remote[:8])
        _post_event_to_hub(
            cfg, kind="update_applied",
            reason=f"commit={remote[:8]} {_git_describe(uc.git_repo, remote)}",
        )
        return

    log.error("Health nach Update %s fehlgeschlagen – Rollback", remote[:8])
    _mark_bad(remote, "health-check failed after install")
    try:
        _git(uc.git_repo, "reset", "--hard", local)
    except subprocess.CalledProcessError as e:
        log.error("git reset für Rollback fehlgeschlagen: %s", e.stderr)
        IN_PROGRESS_FILE.unlink(missing_ok=True)
        _post_event_to_hub(
            cfg, kind="update_rolled_back",
            reason=f"git reset failed: {(e.stderr or '').strip()[:200]}",
        )
        return
    _run_install(uc)
    IN_PROGRESS_FILE.unlink(missing_ok=True)
    _post_event_to_hub(
        cfg, kind="update_rolled_back",
        reason=f"target {remote[:8]} unhealthy, back to {local[:8]}",
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg_path = Path(os.environ.get("HOTSPORT_ACCESS_CONFIG", "/etc/hotsport-access/config.toml"))
    if not cfg_path.exists():
        log.error("Konfig %s fehlt – Updater pausiert.", cfg_path)
        while True:
            time.sleep(30)

    cfg = _read_config(cfg_path)
    uc = _load_updater_cfg(cfg)
    if uc is None or not uc.enabled:
        log.info("Auto-Update deaktiviert (config: [updater].enabled=false oder kein git_repo)")
        while True:
            time.sleep(60)
    if not uc.pi_id:
        log.error("pi_id in config.toml fehlt – Updater pausiert.")
        while True:
            time.sleep(60)
    if not uc.install_script.exists():
        log.error("install.sh fehlt unter %s – Updater pausiert.", uc.install_script)
        while True:
            time.sleep(60)

    log.info(
        "Auto-Update aktiv: repo=%s branch=%s intervall=%.0fs pi=%s",
        uc.git_repo, uc.branch, uc.check_interval_s, uc.pi_id,
    )

    # Wenn der Updater nach einem install.sh-Restart hochkommt und es
    # eine schwebende Update-Session gibt, *zuerst* die finalisieren –
    # andernfalls könnte ein zweiter Tick einen weiteren Update-Versuch
    # starten, bevor wir die Health vom letzten geprüft haben.
    try:
        _finalize_in_progress(cfg, uc)
    except Exception:  # noqa: BLE001
        log.exception("Finalize-Fehler – wird ignoriert, Loop läuft weiter.")

    while True:
        try:
            _check_and_apply(cfg, uc)
        except Exception:  # noqa: BLE001
            log.exception("Tick-Fehler – wird ignoriert.")
        time.sleep(uc.check_interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
