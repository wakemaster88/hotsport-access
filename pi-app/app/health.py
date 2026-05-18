"""Lokaler Health-Endpunkt auf 127.0.0.1.

Wird von systemd-Healthchecks und vom Updater (nach Restart) konsumiert. Kein
Auth, weil ausschließlich an Loopback gebunden.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as cfg_mod
from .state import State
from .version import current_version

log = logging.getLogger(__name__)


def start_health_server(
    boot: cfg_mod.Bootstrap, state: State, healthy_fn
) -> "ThreadingHTTPServer":
    started_at = time.time()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            payload = {
                "ok": bool(healthy_fn()),
                "pi_id": boot.pi_id,
                "version": current_version(),
                "uptime_seconds": int(time.time() - started_at),
                "last_scan": state.last_scan_snapshot(),
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200 if payload["ok"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((boot.health_bind_host, boot.health_bind_port), Handler)
    t = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    t.start()
    return server
