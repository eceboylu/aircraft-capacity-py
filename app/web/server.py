"""
ADIM 5H - Minimal, READ-ONLY görselleştirme sunucusu.

Sadece Python standart kütüphanesi kullanılır (`http.server`) - yeni
bir bağımlılık (Flask/FastAPI vb.) EKLENMEDİ; `requirements.txt`
değişmedi.

Sunulan uç noktalar:

    GET /api/airports                       -> ["ADB", "IST", "SAW"]  (GERİYE DÖNÜK UYUMLU, değişmedi)
    GET /api/airports/directory             -> [{"iata": "IST", "name": "Istanbul Airport"}, ...]  (ADIM 6A-UI-2)
    GET /api/airports/{iata}/predictions     -> {"airport", "overall", "security", "passport", "breakdown"}
    GET /                                    -> static/index.html (frontend)

Hiçbir POST/PUT/DELETE YOK. Hiçbir uç nokta AirLabs'a çağrı yapmaz,
prediction ÜRETMEZ - sadece `app/queue/api.py`'nin (o da sadece
DB'den okuyan) fonksiyonlarını JSON'a çevirir. Prediction hesaplama
her zaman `python -m app.queue.pipeline` (scheduler) tarafındadır;
bu sunucu ona hiç dokunmaz.

Çalıştırma:
    venv/Scripts/python -m app.web.server            (varsayılan port 8000)
    venv/Scripts/python -m app.web.server --port 8080
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..db import get_session
from ..queue.api import airport_directory, airport_predictions, tracked_airports

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

_AIRPORT_PREDICTIONS_RE = re.compile(
    r"^/api/airports/([A-Za-z0-9]{2,10})/predictions$"
)

# path -> (dosya adı, content-type). Sadece frontend'in ihtiyacı olan
# statik dosyalar - genel amaçlı bir dosya sunucusu DEĞİL.
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
}


class QueueMonitorHandler(BaseHTTPRequestHandler):
    """Sadece GET destekler - CRUD YOK, prediction hesaplama YOK."""

    server_version = "AirportQueueMonitor/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Varsayılan stderr gürültüsünü sadeleştiriyoruz; bu sunucu
        # read-only/lokal bir görselleştirme aracıdır, erişim logu
        # kritik değildir. Hiçbir secret zaten bu sunucudan geçmez.
        pass

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # ADIM 6A-UI §1 - "eski server process/tarayıcı cache'i mi
        # gösteriyor" ihtimalini KÖKTEN kapatır: her prediction yanıtı
        # DB'den taze okunur ve tarayıcıya "hiç saklama" denir.
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
        """
        ADIM (Local Test-DB Viewing) - SADECE görsel/lokal test rahatlığı
        için: `?now=YYYY-MM-DDTHH:MM(:SS)` query param'ı VERİLİRSE naive
        UTC datetime'a çevrilip döner - production "current day" mantığını
        DEĞİŞTİRMEZ, sadece `app/queue/api.py:airport_predictions(now=...)`'ın
        ZATEN var olan enjeksiyon noktasını buradan da erişilebilir kılar
        (pipeline/testler bunu dosyadan zaten kullanıyor - Bölüm 59).
        Param yoksa/parse edilemezse None - çağıran taraf gerçek duvar
        saatine (varsayılan, DEĞİŞMEDİ) düşer.
        """
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
