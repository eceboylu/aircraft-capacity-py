
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..db import get_session
from ..health import build_health_report
from ..queue.api import airport_directory, airport_predictions, tracked_airports

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

_AIRPORT_PREDICTIONS_RE = re.compile(
    r"^/api/airports/([A-Za-z0-9]{2,10})/predictions$"
)

_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
}


class QueueMonitorHandler(BaseHTTPRequestHandler):

    server_version = "AirportQueueMonitor/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, filename: str, content_type: str) -> None:
        path = os.path.join(STATIC_DIR, filename)
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _test_now_override(self) -> "datetime | None":
        query = parse_qs(urlparse(self.path).query)
        raw = query.get("now", [None])[0]
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler sözleşmesi)
        path = urlparse(self.path).path

        if path == "/health":
            try:
                session = get_session()
                try:
                    report = build_health_report(session)
                finally:
                    session.close()
            except Exception:
                report = {
                    "status": "unhealthy",
                    "db": "error",
                    "last_successful_refresh": None,
                    "data_age_seconds": None,
                    "stale": True,
                }
            http_status = 200 if report["status"] == "healthy" else 503
            self._send_json(report, status=http_status)
            return

        if path == "/api/airports":
            session = get_session()
            try:
                self._send_json(tracked_airports(session))
            finally:
                session.close()
            return

        if path == "/api/airports/directory":
            session = get_session()
            try:
                self._send_json(airport_directory(session))
            finally:
                session.close()
            return

        match = _AIRPORT_PREDICTIONS_RE.match(path)
        if match:
            iata = match.group(1).upper()
            session = get_session()
            try:
                self._send_json(airport_predictions(session, iata, now=self._test_now_override()))
            finally:
                session.close()
            return

        static_entry = _STATIC_FILES.get(path)
        if static_entry:
            self._send_static(*static_entry)
            return

        self.send_response(404)
        self.end_headers()


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    port = 8000
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])

    server = ThreadingHTTPServer(("0.0.0.0", port), QueueMonitorHandler)
    print(f"Airport Queue Monitor: http://localhost:{port}/  (Ctrl+C ile durdurun)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
