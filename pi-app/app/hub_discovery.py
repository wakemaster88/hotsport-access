"""LAN-Hub-Erkennung für den Pi-Daemon.

Sucht im lokalen Netz nach dem Hotsport-Hub (GET /health ohne pi_id), bis
eine erreichbare Instanz gefunden wurde.

Strategie (in dieser Reihenfolge):

1. **Cache** der zuletzt gefundenen URL.
2. **Konfigurations-Hint** aus ``config.toml`` (falls gesetzt).
3. **Default-Gateway** des Pis und benachbarte IPs im selben /24 (typisch:
   Mac/Workstation steht 1–5 Hosts neben dem Pi).
4. **„Klassische" Server-Endungen** im selben /24 (.1, .10, .100, .200, …) –
   die finden den Hub praktisch sofort, weil PCs/Laptops mit DHCP fast
   immer auf einer dieser Endungen landen.
5. **mDNS-Namen** (``hub.local``, …) – aber nur, wenn der Resolver sie
   innerhalb 0,5 s zurückliefert. So kein 15-s-DNS-Hänger auf Pis ohne
   Avahi.
6. **Full /24-Sweep** als letzter Fallback.

Phase 1 (Schritt 1–5) und Phase 2 (Schritt 6) laufen separat im
ThreadPool: erst Phase 1 → wenn nichts gefunden, Phase 2. Dadurch
findet der Pi den Hub typischerweise in unter einer Sekunde.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

# Hub-Standard (Produktion). 8765 wird oft lokal für den Hub genutzt;
# auf dem Pi selbst ist 8765 der Health-Port – Erkennung unterscheidet per
# GET /health (Hub hat ``service=hotsport-hub``, Pi hat ``pi_id``).
HUB_STANDARD_PORT = 8000
HUB_ALT_PORTS = (8765,)  # lokale Dev-Instanzen / Dashboard-Empfehlung

AUTO_URLS = frozenset({"", "auto", "discover"})
MDNS_HOSTS = ("hub.local", "hotsport-hub.local", "hotsport-hub")

# Letzte Oktette, auf denen ein Hub sehr wahrscheinlich läuft.
# Reihenfolge = Probier-Reihenfolge: Server-Range zuerst, dann typische
# DHCP-Endungen, dann „kosmetische" Endungen wie 254.
_TYPICAL_LAST_OCTETS = (1, 2, 5, 10, 20, 50, 80, 85, 86, 100, 150, 200, 254)

# Wenn der Pi z.B. 192.168.0.101 hat, ist sein Hub-Gegenstück oft
# 192.168.0.100 oder 192.168.0.102 (Workstation gleich neben dem Pi).
_NEIGHBOR_DELTAS = (-1, 1, -2, 2, -5, 5)


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
        if not url:
            return None
        if not is_hotsport_hub(url, timeout=0.5):
            log.info("Hub-Cache ungültig (%s) – wird ignoriert.", url)
            try:
                p.unlink()
            except OSError:
                pass
            return None
        return url
    except OSError:
        return None


def ports_to_probe(config_port: int, hint_url: str | None) -> tuple[int, ...]:
    """Ports für GET /health am Hub (Hint + Config + 8000 + 8765)."""
    ports: list[int] = []

    def add(p: int, *, front: bool = False) -> None:
        if p > 0 and p not in ports:
            if front:
                ports.insert(0, p)
            else:
                ports.append(p)

    if hint_url and not is_auto_url(hint_url):
        try:
            parsed = urlparse(hint_url)
            if parsed.port:
                add(parsed.port, front=True)
        except Exception:  # noqa: BLE001
            pass
    if config_port > 0:
        add(config_port, front=True)
    add(HUB_STANDARD_PORT)
    for alt in HUB_ALT_PORTS:
        add(alt)
    return tuple(ports)


def save_cached_hub(state_dir: Path, url: str) -> None:
    p = hub_cache_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(url.rstrip("/") + "\n", encoding="utf-8")


def is_hotsport_hub(base_url: str, timeout: float = 0.5) -> bool:
    """True wenn GET /health vom Hub-Dashboard antwortet (nicht Pi-Health)."""
    base = base_url.rstrip("/")
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=True) as client:
            resp = client.get(f"{base}/health")
            if resp.status_code != 200:
                return False
            data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return False
    if not isinstance(data, dict) or data.get("ok") is not True:
        return False
    if data.get("service") == "hotsport-hub":
        return True
    # Fallback ältere Hub-Versionen / ohne service-Feld
    return "uptime_seconds" in data and "pi_id" not in data


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


def _networks_from_ips(
    ips: list[str],
    hint_url: str | None = None,
) -> list[ipaddress.IPv4Network]:
    nets: list[ipaddress.IPv4Network] = []
    seen: set[str] = set()

    def _add_ip(ip: str) -> None:
        try:
            addr = ipaddress.IPv4Address(ip)
            net = ipaddress.ip_network(f"{addr}/24", strict=False)
            key = str(net)
            if key not in seen:
                seen.add(key)
                nets.append(net)
        except ValueError:
            pass

    for ip in ips:
        _add_ip(ip)
    if hint_url and not is_auto_url(hint_url):
        try:
            host = urlparse(hint_url).hostname
            if host:
                _add_ip(host)
        except Exception:  # noqa: BLE001
            pass
    return nets


def _url_for_host(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _gateway_ips() -> list[str]:
    """Default-Gateway-IPs aus ``/proc/net/route`` (ohne Subprocess)."""
    out: list[str] = []
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            next(f, None)  # Header überspringen
            for line in f:
                parts = line.split()
                if len(parts) < 4 or parts[1] != "00000000":
                    continue
                hex_gw = parts[2]
                if len(hex_gw) != 8:
                    continue
                try:
                    ip = ".".join(
                        str(int(hex_gw[i:i + 2], 16)) for i in (6, 4, 2, 0)
                    )
                except ValueError:
                    continue
                if ip != "0.0.0.0" and ip not in out:
                    out.append(ip)
    except OSError:
        pass
    return out


def _priority_hosts(local_ips: list[str]) -> list[str]:
    """Wahrscheinliche Hub-Hosts in den lokalen /24-Subnetzen.

    Reihenfolge: Pi-Nachbarn (±1, ±2, ±5) zuerst, dann typische
    Server-Endungen. Eigene IP wird ausgelassen.
    """
    out: list[str] = []
    seen: set[str] = set()
    for ip in local_ips:
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        prefix = ".".join(str(b) for b in addr.packed[:3])
        my_last = addr.packed[3]
        candidates: list[int] = []
        for delta in _NEIGHBOR_DELTAS:
            last = my_last + delta
            if 1 <= last <= 254 and last != my_last:
                candidates.append(last)
        for last in _TYPICAL_LAST_OCTETS:
            if last != my_last and last not in candidates:
                candidates.append(last)
        for last in candidates:
            host = f"{prefix}.{last}"
            if host not in seen:
                seen.add(host)
                out.append(host)
    return out


def _resolve_many_with_timeout(
    hosts: Iterable[str],
    timeout: float = 0.5,
) -> list[str]:
    """Löst mehrere Hostnamen *parallel* mit hartem Gesamt-Timeout auf.

    Hintergrund: ``socket.gethostbyname`` ist nicht abbrechbar – ein
    ``ThreadPoolExecutor.shutdown`` würde auf den hängenden Thread
    warten. Wir nutzen daher Daemon-Threads + ``Thread.join(timeout)``;
    nach dem Deadline-Ablauf laufen offene Threads im Hintergrund
    weiter und sterben mit dem Prozess. Liefert die Hosts, die
    innerhalb ``timeout`` Sekunden auflösbar waren.
    """
    hosts_list = list(hosts)
    if not hosts_list:
        return []
    resolved: dict[str, str] = {}

    def _worker(h: str) -> None:
        try:
            resolved[h] = socket.gethostbyname(h)
        except OSError:
            pass

    threads: list[tuple[str, threading.Thread]] = []
    for h in hosts_list:
        t = threading.Thread(target=_worker, args=(h,), daemon=True)
        t.start()
        threads.append((h, t))
    deadline = time.monotonic() + timeout
    for _, t in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(remaining)
    return [h for h in hosts_list if h in resolved]


def _yield_host_ports(host: str, ports: tuple[int, ...]) -> Iterator[str]:
    for p in ports:
        yield _url_for_host(host, p)


def _iter_priority_candidates(
    *,
    hint_url: str | None,
    ports: tuple[int, ...],
    state_dir: Path | None,
    local_ips: list[str],
) -> Iterator[str]:
    """Phase 1: Cache, Hint, Gateways, Nachbarn, typische Server-IPs, mDNS."""
    if state_dir:
        cached = load_cached_hub(state_dir)
        if cached:
            yield cached

    if hint_url and not is_auto_url(hint_url):
        try:
            parsed = urlparse(hint_url)
            host = parsed.hostname
            if host:
                yield from _yield_host_ports(host, ports)
            else:
                yield hint_url.rstrip("/")
        except Exception:  # noqa: BLE001
            yield hint_url.rstrip("/")

    for gw in _gateway_ips():
        yield from _yield_host_ports(gw, ports)

    for host in _priority_hosts(local_ips):
        yield from _yield_host_ports(host, ports)

    for h in _resolve_many_with_timeout(MDNS_HOSTS, timeout=0.5):
        yield from _yield_host_ports(h, ports)


def _iter_sweep_candidates(
    *,
    ports: tuple[int, ...],
    local_ips: list[str],
    hint_url: str | None = None,
) -> Iterator[str]:
    """Phase 2: Full /24-Sweep (eigenes Subnetz + Hint-Subnetz bei Cross-LAN)."""
    for net in _networks_from_ips(local_ips, hint_url=hint_url):
        for addr in net.hosts():
            yield from _yield_host_ports(str(addr), ports)


def iter_hub_candidates(
    *,
    hint_url: str | None,
    port: int,
    state_dir: Path | None,
) -> Iterator[str]:
    """Backwards-kompatibler Iterator: Prio-Liste + Sweep, in Reihenfolge."""
    local_ips = _local_ipv4_addresses()
    ports = ports_to_probe(port, hint_url)
    yield from _iter_priority_candidates(
        hint_url=hint_url,
        ports=ports,
        state_dir=state_dir,
        local_ips=local_ips,
    )
    yield from _iter_sweep_candidates(
        ports=ports, local_ips=local_ips, hint_url=hint_url,
    )


def _race_probe(
    urls: list[str],
    probe_timeout: float,
    max_workers: int,
) -> str | None:
    """Parallel probieren, beim ersten Treffer abbrechen."""
    if not urls:
        return None
    with ThreadPoolExecutor(max_workers=min(max_workers, len(urls))) as pool:
        futures = {
            pool.submit(is_hotsport_hub, url, probe_timeout): url for url in urls
        }
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                if fut.result():
                    log.info("Hub gefunden: %s", url)
                    for f in futures:
                        f.cancel()
                    return url
            except Exception as e:  # noqa: BLE001
                log.debug("Kandidat %s: %s", url, e)
    return None


def _dedupe(urls: Iterator[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        u = u.rstrip("/")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def discover_hub(
    *,
    hint_url: str | None = None,
    port: int = HUB_STANDARD_PORT,
    state_dir: Path | None = None,
    probe_timeout: float = 0.5,
    max_workers: int = 48,
) -> str | None:
    """Erst Prio-Kandidaten (~30, sehr schnell), dann Full-Sweep (~250)."""
    local_ips = _local_ipv4_addresses()
    ports = ports_to_probe(port, hint_url)

    priority = _dedupe(_iter_priority_candidates(
        hint_url=hint_url,
        ports=ports,
        state_dir=state_dir,
        local_ips=local_ips,
    ))
    if priority:
        log.info(
            "Hub-Suche Phase 1 (Prio): %d Kandidaten, Ports %s …",
            len(priority),
            ",".join(str(p) for p in ports),
        )
        found = _race_probe(priority, probe_timeout, max_workers)
        if found:
            if state_dir:
                save_cached_hub(state_dir, found)
            return found

    sweep_seen = set(priority)
    sweep = [
        u.rstrip("/")
        for u in _iter_sweep_candidates(
            ports=ports, local_ips=local_ips, hint_url=hint_url,
        )
        if u.rstrip("/") not in sweep_seen
    ]
    sweep = _dedupe(iter(sweep))
    if not sweep:
        return None

    log.info(
        "Hub-Suche Phase 2 (Full-Sweep): %d Kandidaten, Ports %s …",
        len(sweep),
        ",".join(str(p) for p in ports),
    )
    found = _race_probe(sweep, probe_timeout, max_workers)
    if found and state_dir:
        save_cached_hub(state_dir, found)
    if not found:
        log.warning(
            "Hub nicht gefunden (Ports %s, Pi-IPs %s). "
            "Hub muss im LAN auf 0.0.0.0 lauschen (nicht nur 127.0.0.1) "
            "und die Windows-Firewall Port %s erlauben.",
            ",".join(str(p) for p in ports),
            local_ips or ["?"],
            ",".join(str(p) for p in ports),
        )
    return found
