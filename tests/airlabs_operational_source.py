"""
ADIM 6A - `tests/fixtures/airlabs_operational/` fixture'larını GERÇEK
`airlabs_client`/`pipeline` zincirine besleyen mock kaynak.

`tests/airlabs_mock_source.py` (ADIM 4) ile AYNI desen: sadece
`urllib.request.urlopen` sahtelenir, `app.queue.ingestion.airlabs_client`
kodu HİÇ değişmeden çalışır - istek kurma, sayfalama (`has_more`/
`offset`), hata/retry mantığının TAMAMI gerçek client kodundan geçer.

Bu modül ayrı tutuldu (mevcut `airlabs_mock_source.py` DEĞİŞTİRİLMEDİ)
çünkü operasyonel senaryo üç round (t0/t1/t2) ve farklı bir havalimanı/
sayfa haritası kullanıyor - paylaşılan ADIM 4 altyapısına dokunmadan
kendi haritasını taşıyor.

Kullanım:

    source = install(monkeypatch, round_="t0")
    ... pipeline.run(source_a=airlabs_client.build_source_a([...]),
                      source_b=airlabs_client.build_source_b()) ...
    source.round = "t1"
    ... pipeline.run(...) tekrar - artık T1 verisini döner ...
"""

from __future__ import annotations

import glob
import json
import os
import urllib.parse
import urllib.request

FIXTURES_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "airlabs_operational")

AIRPORTS = ["IST", "SAW", "ADB"]
ROUNDS = ("t0", "t1", "t2")


def _page_files(round_: str, airport: str, direction: str) -> list[str]:
    pattern = os.path.join(FIXTURES_ROOT, round_, f"schedules_{airport}_{direction}_page_*.json")
    files = sorted(
        glob.glob(pattern),
        key=lambda p: int(p.rsplit("_page_", 1)[1].split(".")[0]),
    )
    if not files:
        raise FileNotFoundError(f"operasyonel fixture bulunamadı: {pattern}")
    return files


class _FakeHTTPResponse:
    """`urllib.request.urlopen`'ın context-manager sözleşmesini taklit eder."""

    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class AirLabsOperationalSource:
    """
    `.round` ("t0"/"t1"/"t2") tek bir yerden değiştirilir -
    `airlabs_client` kodu bundan habersizdir, tıpkı gerçek AirLabs
    API'sinin 30 dakika sonra farklı veri döndürmesi gibi.
    """

    def __init__(self, root: str = FIXTURES_ROOT):
        self.round = "t0"
        self._root = root

    def urlopen(self, request, timeout=None) -> _FakeHTTPResponse:
        parsed = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        endpoint = parsed.path.rsplit("/", 1)[-1]

        if endpoint == "flights":
            # ADIM 6A kapsamı Kaynak A (schedules) - Kaynak B (flights/
            # canlı ADS-B) bilinçli olarak boş; aircraft_icao TAMAMEN
            # Kaynak A'nın kendi alanından geliyor (bkz. generate.py).
            return _FakeHTTPResponse({
                "_mock_disclaimer": "SYNTHETIC - ADIM 6A kapsamında Kaynak B üretilmedi.",
                "request": {"host": "airlabs.co", "method": "flights", "has_more": False},
                "response": [],
            })

        if "dep_iata" in query:
            airport, direction = query["dep_iata"][0], "departure"
        else:
            airport, direction = query["arr_iata"][0], "arrival"

        files = _page_files(self.round, airport, direction)
        offset = int(query.get("offset", ["0"])[0])

        # Sayfalar EŞİT olmayan boyutlarda olabilir (bkz. generate.py
        # `_paginate` - 40'lık parçalar); doğru sayfayı offset'in
        # KÜMÜLATİF kayıt sayısına göre bulmak gerekiyor - sabit bir
        # page_index=offset//page_size varsayımı YANLIŞ olurdu.
        cumulative = 0
        chosen_path = files[-1]
        for path in files:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            page_size = len(payload["response"])
            if offset < cumulative + page_size or not payload["request"]["has_more"]:
                chosen_path = path
                break
            cumulative += page_size
        else:
            chosen_path = files[-1]

        with open(chosen_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        return _FakeHTTPResponse(payload)


def install(monkeypatch, round_: str = "t0", root: str = FIXTURES_ROOT) -> AirLabsOperationalSource:
    """
    `urllib.request.urlopen`'ı bu mock kaynağa bağlar ve dummy
    `AIRLABS_API_KEY`'i ayarlar. Gerçek ağa HİÇ çıkılmaz.
    """
    source = AirLabsOperationalSource(root)
    source.round = round_
    monkeypatch.setenv("AIRLABS_API_KEY", "test-dummy-key-not-real-never-sent")
    monkeypatch.setattr(urllib.request, "urlopen", source.urlopen)
    return source
