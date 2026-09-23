"""
AirLabs API client birim testleri - GERÇEK AĞ ÇAĞRISI YAPMAZ.

`urllib.request.urlopen` her testte sahtelenir (monkeypatch); bu
dosya normal `pytest` çalıştırmasının PARÇASIDIR ve `AIRLABS_API_KEY`
gerektirmez (o senaryo test_airlabs_integration.py'dedir).

Özellikle doğrulanan: URL/parametre kurulumu, 401'de retry YOK,
429/5xx/bağlantı hatasında sınırlı retry, JSON hatasında retry YOK,
ve EN ÖNEMLİSİ - hiçbir log/exception mesajında gerçek api_key
değerinin geçmemesi.
"""

import io
import json
import logging
import urllib.error

import pytest

from app.queue.ingestion import airlabs_client as client


FAKE_KEY = "test-fixture-key-not-real"


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("AIRLABS_API_KEY", FAKE_KEY)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Retry testlerinin gerçek backoff süresince beklememesi için."""
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def _http_error(code: int, headers=None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://airlabs.co/api/v9/schedules",
        code=code,
        msg="test",
        hdrs=headers,
        fp=io.BytesIO(b""),
    )


# --------------------------------------------------------------------
# API key
# --------------------------------------------------------------------

def test_missing_api_key_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("AIRLABS_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        client.fetch_schedules("departure", "IST")


def test_api_key_never_appears_in_log_messages(monkeypatch, caplog):
    """401 hatası (loglanan bir senaryo) mesajında key GEÇMEMELİ."""
    monkeypatch.setattr(
        client.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(_http_error(401)),
    )
    with caplog.at_level(logging.ERROR):
        with pytest.raises(client.AirLabsAuthError) as excinfo:
            client.fetch_schedules("departure", "IST")

    assert FAKE_KEY not in str(excinfo.value)
    for record in caplog.records:
        assert FAKE_KEY not in record.getMessage()


def test_api_key_never_appears_on_retry_exhaustion(monkeypatch, caplog):
    monkeypatch.setattr(
        client.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(_http_error(500)),
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(client.AirLabsError) as excinfo:
            client.fetch_schedules("departure", "IST")

    assert FAKE_KEY not in str(excinfo.value)
    for record in caplog.records:
        assert FAKE_KEY not in record.getMessage()


# --------------------------------------------------------------------
# URL / parametre kurulumu
# --------------------------------------------------------------------

def test_fetch_schedules_departure_uses_dep_iata(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return _FakeResponse({"response": [{"flight_iata": "TK1"}]})

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    rows = client.fetch_schedules("departure", "IST")

    assert "dep_iata=IST" in captured["url"]
    assert "arr_iata" not in captured["url"]
    assert rows == [{"flight_iata": "TK1"}]


def test_fetch_schedules_arrival_uses_arr_iata(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return _FakeResponse({"response": []})

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    client.fetch_schedules("arrival", "IST")

    assert "arr_iata=IST" in captured["url"]
    assert "dep_iata" not in captured["url"]


def test_fetch_schedules_url_includes_api_key_param(monkeypatch):
    """api_key URL'e EKLENİR (bu normal - loglanmaması ayrı bir kural)."""
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return _FakeResponse({"response": []})

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    client.fetch_schedules("departure", "IST")

    assert f"api_key={FAKE_KEY}" in captured["url"]


def test_fetch_live_flights_calls_flights_endpoint(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return _FakeResponse({"response": [{"aircraft_icao": "A320"}]})

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    rows = client.fetch_live_flights()

    assert "/flights?" in captured["url"]
    assert rows == [{"aircraft_icao": "A320"}]


def test_empty_response_returns_empty_list(monkeypatch):
    monkeypatch.setattr(
        client.urllib.request, "urlopen",
        lambda *a, **k: _FakeResponse({"response": []}),
    )
    assert client.fetch_schedules("departure", "IST") == []


def test_non_dict_records_are_filtered_out(monkeypatch):
    monkeypatch.setattr(
        client.urllib.request, "urlopen",
        lambda *a, **k: _FakeResponse({"response": [{"a": 1}, "garbage", None]}),
    )
    assert client.fetch_schedules("departure", "IST") == [{"a": 1}]


# --------------------------------------------------------------------
# Hata sınıflandırması
# --------------------------------------------------------------------

def test_401_raises_auth_error_without_retry(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        raise _http_error(401)

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(client.AirLabsAuthError):
        client.fetch_schedules("departure", "IST")

    assert calls["n"] == 1   # retry YAPILMADI


def test_403_raises_auth_error_without_retry(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        raise _http_error(403)

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(client.AirLabsAuthError):
        client.fetch_schedules("departure", "IST")

    assert calls["n"] == 1


def test_other_4xx_raises_without_retry(monkeypatch):
    """404 gibi diğer 4xx'ler retry edilmeyen genel AirLabsError'dır."""
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        raise _http_error(404)

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(client.AirLabsError):
        client.fetch_schedules("departure", "IST")

    assert calls["n"] == 1


def test_500_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _http_error(500)
        return _FakeResponse({"response": [{"ok": True}]})

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    rows = client.fetch_schedules("departure", "IST")

    assert rows == [{"ok": True}]
    assert calls["n"] == 2


def test_500_exhausts_retries_and_raises(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        raise _http_error(500)

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(client.AirLabsError):
        client.fetch_schedules("departure", "IST")

    assert calls["n"] == client.MAX_ATTEMPTS


def test_429_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _http_error(429)
        return _FakeResponse({"response": []})

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    client.fetch_schedules("departure", "IST")
    assert calls["n"] == 2


def test_connection_error_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        raise urllib.error.URLError("connection refused (TEST)")

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(client.AirLabsError):
        client.fetch_schedules("departure", "IST")

    assert calls["n"] == client.MAX_ATTEMPTS


def test_malformed_json_raises_without_retry(monkeypatch):
    """
    ADIM (Malformed JSON Airport Isolation Fix) - ham `json.JSONDecodeError`
    artık `AirLabsError`'a SARILIYOR (böylece `build_source_a()`'nın
    per-airport `except AirLabsError:` izolasyonu bunu yakalayabiliyor -
    bkz. tests/test_airlabs_live_smoke.py). Retry semantiği DEĞİŞMEDİ -
    hâlâ TEK istek, retry YOK (calls["n"] == 1 ile doğrulanır).
    """
    calls = {"n": 0}

    class _BadResponse(_FakeResponse):
        def read(self):
            return b"{not valid json"

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        return _BadResponse({"response": []})

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(client.AirLabsError) as excinfo:
        client.fetch_schedules("departure", "IST")

    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError), "orijinal JSONDecodeError __cause__ olarak korunmalı"
    assert calls["n"] == 1, "malformed JSON RETRY edilmemeli"

    assert calls["n"] == 1


# --------------------------------------------------------------------
# build_source_a / build_source_b - mevcut source_a/source_b arayüzü
# --------------------------------------------------------------------

def test_build_source_a_merges_multiple_airports(monkeypatch):
    calls = []

    def fake_fetch_schedules(direction, airport_iata):
        calls.append((direction, airport_iata))
        return [{"airport": airport_iata}]

    monkeypatch.setattr(client, "fetch_schedules", fake_fetch_schedules)

    provide = client.build_source_a(["IST", "JFK"])
    rows = provide("departure")

    assert calls == [("departure", "IST"), ("departure", "JFK")]
    assert rows == [{"airport": "IST"}, {"airport": "JFK"}]


def test_build_source_a_matches_source_a_signature():
    """(direction) -> list[dict] - mevcut file_source_a() ile aynı imza."""
    provide = client.build_source_a([])
    result = provide("departure")
    assert result == []


def test_build_source_b_calls_fetch_live_flights(monkeypatch):
    monkeypatch.setattr(
        client, "fetch_live_flights", lambda: [{"aircraft_icao": "B77W"}]
    )
    provide = client.build_source_b()
    assert provide() == [{"aircraft_icao": "B77W"}]


# --------------------------------------------------------------------
# Pagination (Bug 4)
#
# `request.has_more`, gerçek fixture'larda `response` içinde DEĞİL,
# `request` zarfının içinde bulunur (bkz. data/Delays - Type
# Departures.json: request.has_more, request.total_items) - mock
# payload'lar bu gerçek zarf yapısını yansıtır.
# --------------------------------------------------------------------

def _paged_response(records, has_more):
    return {"request": {"has_more": has_more}, "response": records}


def test_pagination_follows_has_more_across_two_pages(monkeypatch):
    pages = [
        _paged_response([{"id": "A"}, {"id": "B"}], True),
        _paged_response([{"id": "C"}, {"id": "D"}], False),
    ]
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        return _FakeResponse(pages.pop(0))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    rows = client.fetch_schedules("departure", "IST")

    assert rows == [{"id": "A"}, {"id": "B"}, {"id": "C"}, {"id": "D"}]
    assert len(calls) == 2
    assert "offset" not in calls[0]          # ilk sayfa - offset yok
    assert "offset=2" in calls[1]            # ikinci sayfa - ilk sayfanın kayıt sayısı kadar ilerledi


def test_pagination_stops_when_has_more_false_on_first_page(monkeypatch):
    """Tek sayfalık response - has_more=false - ikinci istek ATILMAMALI."""
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        return _FakeResponse(_paged_response([{"id": "A"}], False))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    rows = client.fetch_schedules("departure", "IST")

    assert rows == [{"id": "A"}]
    assert calls["n"] == 1


def test_pagination_stops_on_empty_response_even_if_has_more_true(monkeypatch):
    """
    Bozuk/pathological bir API davranışına karşı savunma: sayfa boş
    kayıt döndürürse has_more=true olsa bile DÖNGÜ durur (sonsuz
    döngüye girilmez).
    """
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        return _FakeResponse(_paged_response([], True))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    rows = client.fetch_schedules("departure", "IST")

    assert rows == []
    assert calls["n"] == 1


def test_pagination_offset_advances_by_actual_page_size_not_fixed_limit(monkeypatch):
    """Sayfa boyutu sabit VARSAYILMAZ - offset her seferinde GERÇEK dönen kayıt sayısı kadar ilerler."""
    pages = [
        _paged_response([{"id": i} for i in range(5)], True),    # 5 kayıt
        _paged_response([{"id": i} for i in range(2)], False),   # 2 kayıt
    ]
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        return _FakeResponse(pages.pop(0))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    rows = client.fetch_schedules("departure", "IST")

    assert len(rows) == 7
    assert "offset=5" in calls[1]


def test_pagination_does_not_stop_early_just_because_there_are_many_pages(monkeypatch):
    """
    ADIM 5D - eski davranış (MAX_PAGES=50) "çok sayfa var" diye kayıt
    KESERDİ; bu artık YOK. has_more=True olduğu ve her sayfa GERÇEKTEN
    farklı (yeni) içerik getirdiği sürece, eski tavanın (50) ÇOK
    ÜZERİNDE bir sayfa sayısında bile HİÇ VERİ KAYBI olmadan devam eder.
    """
    total_pages = 60   # eski MAX_PAGES=50'nin üzerinde, kasıtlı
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        has_more = calls["n"] < total_pages
        return _FakeResponse(_paged_response([{"id": calls["n"]}], has_more))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    rows = client.fetch_schedules("departure", "IST")

    assert calls["n"] == total_pages
    assert len(rows) == total_pages   # HİÇBİR kayıp yok


def test_pagination_absolute_safety_cap_stops_and_logs_error_when_forced_low(monkeypatch, caplog):
    """
    Mutlak güvenlik sınırı GERÇEKTEN var (sonsuz has_more=True'ya karşı
    son çare) - ama bunu binlerce sayfa çalıştırmadan doğrulamak için
    düşük bir `max_pages` DOĞRUDAN `_request()`'e verilir (iç
    fonksiyon, ama tam olarak `fetch_schedules`'ın kullandığı
    mekanizma). Sessizce değil, açık bir ERROR logu ile durmalı.
    """
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        return _FakeResponse(_paged_response([{"id": calls["n"]}], True))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    with caplog.at_level(logging.ERROR):
        rows = client._request("schedules", {"dep_iata": "IST"}, max_pages=5)

    assert calls["n"] == 5
    assert len(rows) == 5
    assert any(
        "MUTLAK güvenlik sınırına" in r.getMessage() and r.levelno == logging.ERROR
        for r in caplog.records
    )


def test_pagination_stuck_same_page_repeat_stops_without_data_loss_silence(monkeypatch, caplog):
    """
    API `offset`'i yok sayıp AYNI sayfayı tekrar tekrar dönerse (bozuk
    davranış), sonsuz döngüye girmeden, SESSİZCE değil açık bir ERROR
    logu ile durur.
    """
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        # Her zaman AYNI içerik - offset artsa da API bunu yok sayıyor.
        return _FakeResponse(_paged_response([{"id": "STUCK"}], True))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    with caplog.at_level(logging.ERROR):
        rows = client.fetch_schedules("departure", "IST")

    assert calls["n"] == 2          # ilk sayfa + tekrar tespit edilen ikinci sayfa
    assert rows == [{"id": "STUCK"}]  # ilk sayfa kaydedildi, tekrar EKLENMEDİ
    assert any(
        "aynı sayfa içeriği" in r.getMessage() and r.levelno == logging.ERROR
        for r in caplog.records
    )


def test_pagination_40_pages_has_more_false_receives_all_records(monkeypatch):
    """40 sayfa, sonunda has_more=false - hiçbir kayıp olmadan tüm kayıtlar gelir."""
    total_pages = 40
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        calls["n"] += 1
        has_more = calls["n"] < total_pages
        return _FakeResponse(_paged_response([{"id": calls["n"]}], has_more))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    rows = client.fetch_schedules("departure", "IST")

    assert calls["n"] == total_pages
    assert len(rows) == total_pages


def test_pagination_3000_records_page_size_50_receives_all_3000(monkeypatch):
    """
    ADIM 5C'de tespit edilen tam senaryo: page_size=50, 3.000 kayıt ->
    60 sayfa. Artık HİÇBİR kayıp OLMAMALI (eski davranışta 500 kayıt
    kaybediliyordu).
    """
    page_size = 50
    total_records = 3000
    all_synthetic = [{"id": i} for i in range(total_records)]
    pages = [
        all_synthetic[i:i + page_size]
        for i in range(0, total_records, page_size)
    ]
    calls = {"n": 0}

    def fake_urlopen(request, timeout):
        page = pages[calls["n"]]
        calls["n"] += 1
        has_more = calls["n"] < len(pages)
        return _FakeResponse(_paged_response(page, has_more))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    rows = client.fetch_schedules("departure", "IST")

    assert calls["n"] == 60
    assert len(rows) == total_records
    assert rows == all_synthetic


def test_pagination_single_page_response_without_request_envelope(monkeypatch):
    """`request` zarfı hiç yoksa (basit mock'lar) has_more=False sayılır, tek sayfa döner."""
    monkeypatch.setattr(
        client.urllib.request, "urlopen",
        lambda *a, **k: _FakeResponse({"response": [{"id": "solo"}]}),
    )
    assert client.fetch_schedules("departure", "IST") == [{"id": "solo"}]


def test_pagination_works_for_fetch_live_flights_too(monkeypatch):
    pages = [
        _paged_response([{"aircraft_icao": "A320"}], True),
        _paged_response([{"aircraft_icao": "B738"}], False),
    ]
    monkeypatch.setattr(
        client.urllib.request, "urlopen",
        lambda *a, **k: _FakeResponse(pages.pop(0)),
    )
    rows = client.fetch_live_flights()
    assert rows == [{"aircraft_icao": "A320"}, {"aircraft_icao": "B738"}]


# --------------------------------------------------------------------
# ADIM 5G-1 - Multi-airport ingestion failure isolation (Bug: ADIM 5F
# audit'te bulunan P1). `build_source_a()` birden fazla havalimanını
# işlerken bir havalimanının kalıcı hatası, DİĞER havalimanlarının
# zaten toplanmış kayıtlarını silmemeli, sonraki havalimanına devam
# etmeli.
# --------------------------------------------------------------------

def _always_fails(direction, airport_iata):
    raise client.AirLabsError(f"synthetic failure for {airport_iata}")


def _fetch_by_airport(results: dict, failing: set):
    """
    `results`: {airport -> [records]} başarılı havalimanları.
    `failing`: hangi havalimanları AirLabsError fırlatsın.
    """
    def fake_fetch_schedules(direction, airport_iata):
        if airport_iata in failing:
            raise client.AirLabsError(f"synthetic failure for {airport_iata}")
        return list(results.get(airport_iata, []))
    return fake_fetch_schedules


def test_isolation_middle_airport_failure_preserves_others(monkeypatch):
    """IST success, SAW failure, ADB success -> IST + ADB kayıtları döner."""
    calls = []
    results = {
        "IST": [{"flight_iata": "TK1"}],
        "ADB": [{"flight_iata": "TK2"}],
    }

    def fake_fetch_schedules(direction, airport_iata):
        calls.append(airport_iata)
        if airport_iata == "SAW":
            raise client.AirLabsError("synthetic SAW failure")
        return list(results[airport_iata])

    monkeypatch.setattr(client, "fetch_schedules", fake_fetch_schedules)

    provide = client.build_source_a(["IST", "SAW", "ADB"])
    rows = provide("departure")

    assert rows == [{"flight_iata": "TK1"}, {"flight_iata": "TK2"}]
    # SAW'un başarısız olması ADB fetch'inin GERÇEKTEN çağrılmasını
    # engellemedi - iteration devam etti (sadece output değil).
    assert calls == ["IST", "SAW", "ADB"]


def test_isolation_first_airport_failure_does_not_stop_loop(monkeypatch):
    """IST failure, SAW success, ADB success -> SAW + ADB korunur."""
    calls = []
    results = {"SAW": [{"flight_iata": "PC1"}], "ADB": [{"flight_iata": "PC2"}]}

    def fake_fetch_schedules(direction, airport_iata):
        calls.append(airport_iata)
        if airport_iata == "IST":
            raise client.AirLabsError("synthetic IST failure")
        return list(results[airport_iata])

    monkeypatch.setattr(client, "fetch_schedules", fake_fetch_schedules)

    provide = client.build_source_a(["IST", "SAW", "ADB"])
    rows = provide("departure")

    assert rows == [{"flight_iata": "PC1"}, {"flight_iata": "PC2"}]
    assert calls == ["IST", "SAW", "ADB"]


def test_isolation_last_airport_failure_preserves_earlier_records(monkeypatch):
    """IST success, SAW success, ADB failure -> IST + SAW korunur."""
    calls = []
    results = {"IST": [{"flight_iata": "AF1"}], "SAW": [{"flight_iata": "AF2"}]}

    def fake_fetch_schedules(direction, airport_iata):
        calls.append(airport_iata)
        if airport_iata == "ADB":
            raise client.AirLabsError("synthetic ADB failure")
        return list(results[airport_iata])

    monkeypatch.setattr(client, "fetch_schedules", fake_fetch_schedules)

    provide = client.build_source_a(["IST", "SAW", "ADB"])
    rows = provide("departure")

    assert rows == [{"flight_iata": "AF1"}, {"flight_iata": "AF2"}]
    assert calls == ["IST", "SAW", "ADB"]


def test_isolation_all_airports_failing_returns_empty_list_without_raising(monkeypatch, caplog):
    """
    IST, SAW, ADB hepsi başarısız -> [] döner (exception FIRLATILMAZ),
    her biri için ayrı bir ERROR logu üretilir - sessiz kayıp yok.
    """
    monkeypatch.setattr(client, "fetch_schedules", _always_fails)

    with caplog.at_level(logging.ERROR):
        provide = client.build_source_a(["IST", "SAW", "ADB"])
        rows = provide("departure")

    assert rows == []
    failed_airports_logged = {
        r.args[0] for r in caplog.records
        if "AirLabs schedules fetch failed" in r.getMessage()
    }
    assert failed_airports_logged == {"IST", "SAW", "ADB"}


def test_isolation_arrival_direction_partial_failure(monkeypatch):
    """Aynı izolasyon arrival yönünde de çalışıyor."""
    calls = []
    results = {"IST": [{"flight_iata": "BA1"}], "ADB": [{"flight_iata": "BA2"}]}

    def fake_fetch_schedules(direction, airport_iata):
        calls.append((direction, airport_iata))
        if airport_iata == "SAW":
            raise client.AirLabsError("synthetic SAW arrival failure")
        return list(results[airport_iata])

    monkeypatch.setattr(client, "fetch_schedules", fake_fetch_schedules)

    provide = client.build_source_a(["IST", "SAW", "ADB"])
    rows = provide("arrival")

    assert rows == [{"flight_iata": "BA1"}, {"flight_iata": "BA2"}]
    assert calls == [("arrival", "IST"), ("arrival", "SAW"), ("arrival", "ADB")]


def test_isolation_departure_direction_partial_failure(monkeypatch):
    """Aynı izolasyon departure yönünde de çalışıyor (arrival testinden ayrık)."""
    results = {"IST": [{"flight_iata": "LH1"}], "ADB": [{"flight_iata": "LH2"}]}

    def fake_fetch_schedules(direction, airport_iata):
        if airport_iata == "SAW":
            raise client.AirLabsError("synthetic SAW departure failure")
        return list(results[airport_iata])

    monkeypatch.setattr(client, "fetch_schedules", fake_fetch_schedules)

    provide = client.build_source_a(["IST", "SAW", "ADB"])
    rows = provide("departure")

    assert rows == [{"flight_iata": "LH1"}, {"flight_iata": "LH2"}]


def test_isolation_auth_error_subclass_is_also_isolated(monkeypatch):
    """
    `AirLabsAuthError` (401/403), `AirLabsError`'ın alt sınıfı - o da
    per-airport izole edilmeli, sadece genel `RuntimeError` (eksik
    key) izole EDİLMEMELİ.
    """
    results = {"ADB": [{"flight_iata": "SU1"}]}

    def fake_fetch_schedules(direction, airport_iata):
        if airport_iata == "SAW":
            raise client.AirLabsAuthError("synthetic 401 for SAW")
        return list(results.get(airport_iata, []))

    monkeypatch.setattr(client, "fetch_schedules", fake_fetch_schedules)

    provide = client.build_source_a(["SAW", "ADB"])
    rows = provide("departure")

    assert rows == [{"flight_iata": "SU1"}]


def test_isolation_does_not_catch_unrelated_runtime_error(monkeypatch):
    """
    Eksik AIRLABS_API_KEY gibi genel bir konfigürasyon hatası
    (RuntimeError) havalimanına özel DEĞİLDİR - izole edilmez, yukarı
    taşınır (her havalimanında aynı şekilde başarısız olacağı için
    hepsini tek tek "denemek" anlamsız ve yanıltıcı olurdu).
    """
    def fake_fetch_schedules(direction, airport_iata):
        raise RuntimeError("AIRLABS_API_KEY tanımlı değil")

    monkeypatch.setattr(client, "fetch_schedules", fake_fetch_schedules)

    provide = client.build_source_a(["IST", "SAW"])
    with pytest.raises(RuntimeError):
        provide("departure")


def test_isolation_failure_log_message_never_contains_api_key(monkeypatch, caplog):
    """Havalimanı-izolasyon logu da api_key/URL sızdırmaz."""
    monkeypatch.setattr(client, "fetch_schedules", _always_fails)

    with caplog.at_level(logging.ERROR):
        provide = client.build_source_a(["IST"])
        provide("departure")

    for record in caplog.records:
        assert FAKE_KEY not in record.getMessage()
        assert "api_key" not in record.getMessage().lower()
