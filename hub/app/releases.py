"""Release-Verwaltung: Verzeichnis-Scan + Hashing.

Releases sind ZIP-Dateien im Format `hotsport-access-<version>.zip` plus
einer Begleitdatei `<version>.sha256` mit dem hex-encoded SHA-256.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

VERSION_RE = re.compile(r"^hotsport-access-(?P<version>[A-Za-z0-9._\-]+)\.zip$")


@dataclass(frozen=True)
class Release:
    version: str
    zip_path: Path
    sha256: str
    size_bytes: int


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_sha256(zip_path: Path) -> str:
    """Liest oder erzeugt die `<zip>.sha256`-Datei neben dem Release-ZIP.

    Wir cachen die Hashes als Datei, damit der Hub-Prozess beim Neustart nicht
    jeden Release neu hashen muss.

    Wichtig: wir verwenden `zip_path.name + ".sha256"` statt `with_suffix`,
    weil `with_suffix` bei Versionen mit Punkten (z.B. `2026.05.18-1`) das
    falsche Segment ersetzt.
    """
    sha_path = zip_path.with_name(zip_path.name + ".sha256")
    if sha_path.exists():
        text = sha_path.read_text().strip().split()[0]
        if re.fullmatch(r"[0-9a-fA-F]{64}", text):
            return text.lower()
    digest = _sha256_of(zip_path)
    sha_path.write_text(digest + "\n")
    return digest


def list_releases(releases_dir: Path) -> list[Release]:
    if not releases_dir.is_dir():
        return []
    out: list[Release] = []
    for zip_path in sorted(releases_dir.glob("hotsport-access-*.zip")):
        m = VERSION_RE.match(zip_path.name)
        if not m:
            continue
        digest = ensure_sha256(zip_path)
        out.append(
            Release(
                version=m.group("version"),
                zip_path=zip_path,
                sha256=digest,
                size_bytes=zip_path.stat().st_size,
            )
        )
    out.sort(key=lambda r: r.version, reverse=True)
    return out


def get_release(releases_dir: Path, version: str) -> Release | None:
    for r in list_releases(releases_dir):
        if r.version == version:
            return r
    return None
