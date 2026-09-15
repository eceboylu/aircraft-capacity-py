"""
ADIM 4 - Synthetic AirLabs `schedules` fixture'larını (tests/fixtures/
airlabs_mock/, tests/fixtures/airlabs_mock_t1/) gerçek `airlabs_client`
/ `pipeline` akışına besleyen TEK ortak test kaynağı.

    synthetic AirLabs schedules (JSON dosyaları)
            -> bu modülün sahte urlopen'i (SADECE ağ çağrısı taklit edilir)
            -> app.queue.ingestion.airlabs_client (GERÇEK kod, değişmedi)
            -> app.queue.pipeline.run(source_a=..., source_b=...) (GERÇEK)

Production kodu bu dosyayı hiç bilmez/import etmez - mock/gerçek AirLabs
ayrımı SADECE test tarafında, bu modülde yapılır. `app/queue/ingestion/
airlabs_client.py` üretim kodunda hiçbir test-özel dal YOKTUR; gerçek
API ile mock testin tek farkı, `urllib.request.urlopen`'ın bu modülde
sahte bir fonksiyonla değiştirilmiş olmasıdır - istek kurma, sayfalama
(`has_more`/`offset`), hata/retry mantığının TAMAMI gerçek client
kodundan geçer.

`AIRLABS_API_KEY` burada sadece `airlabs_client._api_key()`'in URL
kurma mantığı değişmeden çalışsın diye gereken bir DUMMY string'tir;
ağa hiç çıkılmadığı için gerçek bir key ASLA gerekmez/istenmez.

Kullanım (bkz. tests/test_e2e_refresh.py):

    source = install(monkeypatch)
    ... pipeline.run(source_a=airlabs_client.build_source_a([...]),
                      source_b=airlabs_client.build_source_b()) ...
    source.round = "t1"
    ... pipeline.run(...) tekrar - artık T1 verisini döner ...
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

FIXTURES_T0 = os.path.join(os.path.dirname(__file__), "fixtures", "airlabs_mock")
FIXTURES_T1 = os.path.join(os.path.dirname(__file__), "fixtures", "airlabs_mock_t1")

# (airport, direction) -> sayfa dosyaları, sırayla. ADIM 2'de sadece bu
# 3 havalimanı (IST/SAW/ADB) için mock schedules üretildi (bkz.
# tests/fixtures/airlabs_mock/README.md).
SCHEDULE_FILES = {
    ("IST", "departure"): [
        "schedules_IST_departures_page_1.json",
        "schedules_IST_departures_page_2.json",
    ],
    ("IST", "arrival"): [
        "schedules_IST_arrivals_page_1.json",
        "schedules_IST_arrivals_page_2.json",
    ],
    ("SAW", "departure"): ["schedules_SAW_departures.json"],
    ("SAW", "arrival"): ["schedules_SAW_arrivals.json"],
    ("ADB", "departure"): ["schedules_ADB_departures.json"],
    ("ADB", "arrival"): ["schedules_ADB_arrivals.json"],
}

AIRPORTS = ["IST", "SAW", "ADB"]


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


class AirLabsMockSource:
    """
    `.round` ("t0"/"t1") tek bir yerden değiştirilir; `airlabs_client`
    kodu bundan habersizdir - tıpkı gerçek AirLabs API'sinin 30 dakika
    sonra farklı veri döndürmesi gibi, sadece HTTP yanıtı değişir.
    """

    def __init__(self, fixtures_t0: str = FIXTURES_T0, fixtures_t1: str = FIXTURES_T1):
        self.round = "t0"
        self._fixtures_t0 = fixtures_t0
        self._fixtures_t1 = fixtures_t1

    def _base_dir(self) -> str:
        return self._fixtures_t1 if self.round == "t1" else self._fixtures_t0

    def urlopen(self, request, timeout=None) -> _FakeHTTPResponse:
        parsed = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        endpoint = parsed.path.rsplit("/", 1)[-1]

        if endpoint == "flights":
            # ADIM 2 kapsamı dışı bırakıldı - Kaynak B bilinçli olarak boş.
            return _FakeHTTPResponse({
                "_mock_disclaimer": (
                    "SYNTHETIC - Kaynak B (flights/canlı ADS-B) bu mock "
                    "source'ta üretilmedi, bilinçli olarak boş."
                ),
                "request": {"host": "airlabs.co", "method": "flights", "has_more": False},
                "response": [],
            })

        if "dep_iata" in query:
            airport, direction = query["dep_iata"][0], "departure"
        else:
            airport, direction = query["arr_iata"][0], "arrival"

        files = SCHEDULE_FILES[(airport, direction)]
        offset = int(query.get("offset", ["0"])[0])
        page_index = 0 if offset == 0 else 1

        path = os.path.join(self._base_dir(), files[page_index])
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        return _FakeHTTPResponse(payload)


def install(monkeypatch, fixtures_t0: str = FIXTURES_T0, fixtures_t1: str = FIXTURES_T1) -> AirLabsMockSource:
    """
    `urllib.request.urlopen`'ı bu mock kaynağa bağlar ve dummy
    `AIRLABS_API_KEY`'i ayarlar. `monkeypatch`, çağıran testin kendi
    `pytest.MonkeyPatch`/`monkeypatch` fixture'ı olabilir - bu modül
    kendi patch ömrünü YÖNETMEZ, çağıranın `undo()`'suna bağlıdır.
    """
    source = AirLabsMockSource(fixtures_t0, fixtures_t1)
    monkeypatch.setenv("AIRLABS_API_KEY", "test-dummy-key-not-real-never-sent")
    monkeypatch.setattr(urllib.request, "urlopen", source.urlopen)
    return source
