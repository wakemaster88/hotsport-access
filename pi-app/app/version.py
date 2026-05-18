"""Liefert die installierte Release-Version.

Die Version steht in der Datei `VERSION` direkt neben der App. Beim Build wird
das Tag dort eingetragen, beim Update vom Hub bringt das ZIP eine neue VERSION
mit.
"""

from __future__ import annotations

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def current_version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"
