"""LAN-Hub-Erkennung für den Pi-Daemon.

Sucht im lokalen Netz nach dem Hotsport-Hub (GET /health ohne pi_id), bis eine
erreichbare Instanz gefunden wurde. Kandidaten: Cache, konfigurierte URL,
mDNS-Namen, dann /24-Subnetze der lokalen Interfaces.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

AUTO_URLS = frozenset({"", "auto", "discover"})
MDNS_HOSTS = ("hub.local", "hotsport-hub.local", "hotsport-hub")


def is_auto_url(url: str | None) -> bool:
    if not url:
        return True
    return url.strip().lower() in AUTO_URLS


def should_discover(base_url: str, discover_flag: bool) -> bool:
    return bool(discover_flag or is_auto_url(base_url))


def hub_cache_path(state_dir: Path) -> Path:
    return state_dir / "discovered_hub_url"


def load_cached_hub(state_dir: Path) -> str | None:
    p = hub_cache_path(state_dir)
    if not p.is_file():
        return None
    try:
        url = p.read_text(encoding="utf-8").strip()
        return url if url else None
    except OSError:
        return None


def save_cached_hub(state_dir: Path, url: str) -> None:
    p = hub_cache_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(url.rstrip("/") + "\n", encoding="utf-8")


def is_hotsport_hub(base_url: str, timeout: float = 0.4) -> bool:
    """True wenn GET /health wie ein Hotsport-Hub antwortet (kein pi_id-Feld)."""
    base = base_url.rstrip("/")
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=True) as client:
            resp = client.get(f"{base}/health")
            if resp.status_code != 200:
                return False
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return False
    return (
        isinstance(data, dict)
        and data.get("ok") is True
        and "uptime_seconds" in data
        and "pi_id" not in data
    )


def _local_ipv4_addresses() -> list[str]:
    candidates: list[str] = []
    for gw in (
        "192.168.1.1",
        "192.168.0.1",
        "192.168.178.1",
        "10.0.0.1",
        "10.0.0.138",
        "172.20.0.1",
        "8.8.8.8",
        "1.1.1.1",
    ):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.05)
            sock.connect((gw, 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127.") and ip not in candidates:
                candidates.append(ip)
        except OSError:
            pass
        finally:
            sock.close()
    return candidates


def _networks_from_ips(ips: list[str]) -> list[ipaddress.IPv4Network]:
    nets: list[ipaddress.IPv4Network] = []
    seen: set[str] = set()
    for ip in ips:
        try:
            addr = ipaddress.IPv4Address(ip)
            net = ipaddress.ip_network(f"{addr}/24", strict=False)
            key = str(net)
            if key not in seen:
                seen.add(key)
                nets.append(net)
        except ValueError:
            continue
    return nets


def _url_for_host(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def iter_hub_candidates(
    *,
    hint_url: str | None,
    port: int,
    state_dir: Path | None,
) -> Iterator[str]:
    if state_dir:
        cached = load_cached_hub(state_dir)
        if cached:
            yield cached

    if hint_url and not is_auto_url(hint_url):
        yield hint_url.rstrip("/")
        try:
            parsed = urlparse(hint_url)
            if parsed.hostname:
                yield _url_for_host(parsed.hostname, port)
        except Exception:  # noqa: BLE001
            pass

    for host in MDNS_HOSTS:
        yield _url_for_host(host, port)

    for net in _networks_from_ips(_local_ipv4_addresses()):
        for addr in net.hosts():
            yield _url_for_host(str(addr), port)


def discover_hub(
    *,
    hint_url: str | None = None,
    port: int = 8000,
    state_dir: Path | None = None,
    probe_timeout: float = 0.35,
    max_workers: int = 48,
) -> str | None:
    """Paralleler Scan; erste passende Hub-URL oder None."""
    seen: set[str] = set()
    candidates: list[str] = []
    for url in iter_hub_candidates(hint_url=hint_url, port=port, state_dir=state_dir):
        u = url.rstrip("/")
        if u not in seen:
            seen.add(u)
            candidates.append(u)

    if not candidates:
        return None

    log.info("Hub-Suche: %d Kandidaten auf Port %d …", len(candidates), port)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(is_hotsport_hub, url, probe_timeout): url for url in candidates
        }
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                if fut.result():
                    log.info("Hub gefunden: %s", url)
                    if state_dir:
                        save_cached_hub(state_dir, url)
                    return url
            except Exception as e:  # noqa: BLE001
                log.debug("Kandidat %s: %s", url, e)
    return None
