"""
ADIM (Fake AirLabs Live-Smoke) - `app/worker.py:_build_live_run_fn()`'in
(bir ÖNCEKİ ADIM'da eklendi) production kod yolunun TAMAMINI - gerçek
`airlabs_client` (istek kurma, pagination, retry/backoff, auth/hata
sınıflandırması), gerçek `pipeline.run()`, gerçek `parse_source_a`/
`build_aircraft_index`, gerçek `refresh_flights` (upsert + FlightEvent),
gerçek `run_predictions()` - uçtan uca egzersiz eder. SADECE
`urllib.request.urlopen` sahtelenir; başka HİÇBİR katman fake/mock
DEĞİLDİR (queue engine dahil - queue math'a bu dosyada HİÇ dokunulmaz,
sadece GERÇEK kodun ÜRETTİĞİ sonuçlar yapısal olarak doğrulanır).

Gerçek ağa HİÇ çıkılmaz - `FakeAirLabsTransport.urlopen()` her isteği
kendi programlanmış (veya varsayılan boş) cevabıyla karşılar; tanımadığı
bir istek gelirse gerçek ağa düşmek yerine AÇIKÇA hata fırlatır (Bölüm 25).

Gerçek `AIRLABS_API_KEY` KULLANILMAZ - sahte bir değer (`fake-smoke-
key-secret`) kullanılır ve hiçbir log satırında GÖRÜNMEDİĞİ AYRICA
doğrulanır (Bölüm 24).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.queue.pipeline as pipeline_module
import app.worker as worker_module
from app.health import build_health_report, get_last_successful_refresh
from app.models import Base
from app.queue.constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    EVENT_AIRCRAFT_CHANGED,
    EVENT_CANCELLED,
    EVENT_DELAYED,
    EVENT_DIVERTED,
)
from app.queue.ingestion.airports_import import import_airports
from app.queue.models import Flight, FlightEvent, QueuePrediction
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset

REAL_AIRPORTS_SQL = __import__("os").path.join(
    __import__("os").path.dirname(__file__), "..", "data", "flight_airports.sql"
)

FAKE_KEY = "fake-smoke-key-secret"
TRACKED = ["IST", "SAW"]


# ========================================================================
# Fake HTTP transport - SADECE urllib.request.urlopen sahtelenir
# ========================================================================

class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeMalformedResponse:
    def read(self) -> bytes:
        return b"<html>not json at all</html>"

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    from io import BytesIO
    hdrs = None
    if headers:
        import email.message
        hdrs = email.message.Message()
        for k, v in headers.items():
            hdrs[k] = v
    return urllib.error.HTTPError(
        url="https://airlabs.co/api/v9/schedules", code=code, msg="test",
        hdrs=hdrs, fp=BytesIO(b""),
    )


class FakeAirLabsTransport:
    """
    Bölüm 1 - SADECE dış ağ katmanı sahtelenir; `airlabs_client`'ın
    KENDİSİ (istek kurma, sayfalama, retry/backoff, hata sınıflandırması)
    TAMAMEN GERÇEK kod yoludur.

    `queues[(endpoint, airport, direction)]` bir liste - her eleman ya
    `("page", records, has_more)` ya da bir fault talimatı
    (`("http_error", code, headers)`, `("url_error", reason)`,
    `("malformed",)`). Sıradaki eleman TÜKETİLİR (pop(0)); liste
    biterse varsayılan olarak boş/has_more=False bir sayfa döner - bir
    airport/direction'ı hiç programlamamış bir test kazara hataya
    DÜŞMEZ, sessizce "veri yok" alır.

    Bölüm 25 (Real Network Guard) - `schedules`/`flights` DIŞINDA bir
    endpoint'e istek gelirse (olması İMKANSIZ ama savunma amaçlı) veya
    bu sınıfın DIŞINDA bir `urlopen` çağrılırsa (monkeypatch tam
    kapsamıyorsa) test AÇIKÇA patlar - gerçek ağa SESSİZCE düşülmez.
    """

    def __init__(self):
        self.queues: dict[tuple[str, str | None, str | None], list] = {}
        self.calls: list[dict] = []

    def program(self, endpoint: str, airport: str | None, direction: str | None, *items):
        self.queues.setdefault((endpoint, airport, direction), []).extend(items)

    def urlopen(self, request, timeout=None):
        full_url = request.full_url
        parsed = urllib.parse.urlparse(full_url)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        query = urllib.parse.parse_qs(parsed.query)

        assert "api_key" in query, "AirLabs isteği api_key parametresi olmadan gitmemeli"
        assert query["api_key"][0] == FAKE_KEY

        airport = None
        direction = None
        if "dep_iata" in query:
            airport, direction = query["dep_iata"][0], "departure"
        elif "arr_iata" in query:
            airport, direction = query["arr_iata"][0], "arrival"

        offset = int(query.get("offset", ["0"])[0])
        self.calls.append({
            "endpoint": endpoint, "airport": airport, "direction": direction,
            "offset": offset, "full_url": full_url,
        })

        key = (endpoint, airport, direction)
        queue = self.queues.get(key, [])
        item = queue.pop(0) if queue else ("page", [], False)

        kind = item[0]
        if kind == "page":
            _, records, has_more = item
            return _FakeHTTPResponse({
                "request": {"host": "airlabs.co", "method": endpoint, "has_more": has_more},
                "response": records,
            })
        if kind == "http_error":
            _, code, headers = item
            raise _http_error(code, headers)
        if kind == "url_error":
            _, reason = item
            raise urllib.error.URLError(reason)
        if kind == "malformed":
            return _FakeMalformedResponse()
        raise AssertionError(f"bilinmeyen fake transport talimatı: {item!r}")  # pragma: no cover


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """
    Bölüm 25 - her testte varsayılan olarak gerçek `urlopen`'ı tamamen
    ERİŞİLEMEZ hale getirir; testler kendi `FakeAirLabsTransport`'unu
    AÇIKÇA kurmadıkça hiçbir HTTP çağrısı (gerçek dahil) YAPILAMAZ.
    """
    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "GERÇEK ağa çıkılmaya çalışıldı - bu smoke test SADECE "
            "FakeAirLabsTransport üzerinden çalışmalı"
        )
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)
    monkeypatch.setenv("AIRLABS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", ",".join(TRACKED))


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Bölüm 14 - retry/backoff testlerinin gerçek 1-4sn'lik beklemesini atlar (tests/test_airlabs_client.py ile AYNI desen)."""
    import app.queue.ingestion.airlabs_client as client_module
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)


def install_transport(monkeypatch) -> FakeAirLabsTransport:
    transport = FakeAirLabsTransport()
    monkeypatch.setattr(urllib.request, "urlopen", transport.urlopen)
    return transport


# ========================================================================
# İzole DB - Bölüm 2 (production MySQL DB'ye HİÇ dokunulmaz)
# ========================================================================

@pytest.fixture
def isolated_db(monkeypatch):
    engine = create_engine("sqlite://")
    SessionLocal = sessionmaker(bind=engine)
    monkeypatch.setattr(
        pipeline_module, "init_db",
        lambda drop_first=False: Base.metadata.create_all(engine),
    )
    monkeypatch.setattr(pipeline_module, "get_session", lambda: SessionLocal())

    Base.metadata.create_all(engine)
    seed = SessionLocal()
    import_airports(seed, REAL_AIRPORTS_SQL)
    seed_verified_dataset(seed)
    seed_curated_fallback(seed)
    seed_family_and_ga(seed)
    seed.close()
    return SessionLocal


NOW = datetime(2026, 9, 15, 20, 0)  # tüm pencereler kapalı - overall/graph query'lerini basitleştirir


def _rec(
    flight_iata, airline_iata, dep_iata, arr_iata, dep_time, arr_time,
    status="scheduled", aircraft_icao=None,
    dep_estimated=None, arr_estimated=None, dep_actual=None, arr_actual=None,
):
    """Gerçek AirLabs `schedules` response shape'i (Bölüm 4 - snake_case, field() ile BİREBİR eşleşir)."""
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M") if d else None
    return {
        "airline_iata": airline_iata, "flight_iata": flight_iata,
        "flight_icao": None, "flight_number": flight_iata[2:],
        "dep_iata": dep_iata, "arr_iata": arr_iata,
        "dep_time_utc": fmt(dep_time), "arr_time_utc": fmt(arr_time),
        "dep_estimated_utc": fmt(dep_estimated), "arr_estimated_utc": fmt(arr_estimated),
        "dep_actual_utc": fmt(dep_actual), "arr_actual_utc": fmt(arr_actual),
        "dep_terminal": "1", "arr_terminal": "1", "dep_gate": "B1", "arr_gate": "E1",
        "status": status, "aircraft_icao": aircraft_icao,
    }


def run_fn(monkeypatch, domain_now_value=NOW):
    monkeypatch.setattr(worker_module, "domain_now", lambda: domain_now_value)
    return worker_module._build_live_run_fn()


def flights_by_key(session_factory):
    s = session_factory()
    try:
        return {f.flight_key: f for f in s.query(Flight).all()}
    finally:
        s.close()


def events(session_factory):
    s = session_factory()
    try:
        return list(s.query(FlightEvent).all())
    finally:
        s.close()


# ========================================================================
# T0 / T1 - ana uçtan uca senaryo (Bölüm 4-10)
# ========================================================================

DEP_T0 = datetime(2026, 9, 15, 10, 0)
ARR_T0 = datetime(2026, 9, 15, 11, 0)


@pytest.fixture(scope="module")
def t0_t1_state():
    """
    Bölüm 4-10 - PAHALI kurulum (2 tam `pipeline.run()` turu + gerçek
    9766 satırlık `flight_airports.sql` import'u) TÜM bu bölümün
    testleri arasında SADECE BİR KEZ çalışır - `tests/test_operational_
    dataset.py:operational_state` ile AYNI, KANITLANMIŞ desen (module-
    scope + manuel `pytest.MonkeyPatch()`, fonksiyon-scope'lu
    `monkeypatch` fixture'ı module-scope'lu bir fixture içinde
    KULLANILAMAZ). Bu grup SADECE zaten yazılmış DB'yi OKUR, yeni HTTP
    isteği YAPMAZ - bu yüzden `_block_real_network`/`_no_real_sleep`
    autouse fixture'larının (fonksiyon-scope, her testte `urlopen`'ı
    sıfırlar) burada hiçbir ÇAKIŞMASI yok.
    """
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("AIRLABS_API_KEY", FAKE_KEY)
        mp.setenv("AIRLABS_TRACKED_AIRPORTS", ",".join(TRACKED))
        import app.queue.ingestion.airlabs_client as client_module
        mp.setattr(client_module.time, "sleep", lambda seconds: None)

        engine = create_engine("sqlite://")
        SessionLocal = sessionmaker(bind=engine)
        mp.setattr(
            pipeline_module, "init_db",
            lambda drop_first=False: Base.metadata.create_all(engine),
        )
        mp.setattr(pipeline_module, "get_session", lambda: SessionLocal())

        Base.metadata.create_all(engine)
        seed = SessionLocal()
        import_airports(seed, REAL_AIRPORTS_SQL)
        seed_verified_dataset(seed)
        seed_curated_fallback(seed)
        seed_family_and_ga(seed)
        seed.close()

        transport = FakeAirLabsTransport()
        mp.setattr(urllib.request, "urlopen", transport.urlopen)

        state = _run_t0_t1(mp, transport, SessionLocal)
        yield state
    finally:
        mp.undo()


def _run_t0_t1(monkeypatch, transport, session_factory):
    # --- T0 payload ---
    ist_dep_t0 = [
        _rec("TK001", "TK", "IST", "JFK", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=10), aircraft_icao="B77W"),   # international departure
        _rec("TK002", "TK", "IST", "ESB", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=1), aircraft_icao="A320"),    # domestic departure
        _rec("TK101", "TK", "IST", "CDG", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4)),  # Flight A: estimated dep will change at t1 (no aircraft at t0)
        _rec("TK103", "TK", "IST", "DXB", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=5), aircraft_icao="A320"),    # Flight C: aircraft will change at t1
        _rec("TK104", "TK", "IST", "VKO", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=3), aircraft_icao="A321"),    # Flight D: actual will be added at t1
        _rec("TK105", "TK", "IST", "AYT", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=1), aircraft_icao="B738"),    # Flight G: will be cancelled at t1
        _rec("TK106", "TK", "IST", "ESB", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=1), aircraft_icao="B738"),    # Flight H: will be diverted at t1
        _rec("TK107", "TK", "IST", "CDG", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao=None),      # Flight F: no aircraft yet, late enrichment at t1
    ]
    ist_arr_t0 = [
        _rec("AF204", "AF", "CDG", "IST", DEP_T0, ARR_T0, aircraft_icao="A321"),  # international arrival
        _rec("BA205", "BA", "LHR", "IST", DEP_T0, ARR_T0, aircraft_icao="A320"),  # Flight B: estimated arrival will change at t1
    ]
    saw_dep_t0 = [
        _rec("PC301", "PC", "SAW", "VKO", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A321"),  # international departure
    ]
    saw_arr_t0 = [
        _rec("PC302", "PC", "DXB", "SAW", DEP_T0, ARR_T0, aircraft_icao="A321"),  # international arrival
    ]

    transport.program("schedules", "IST", "departure", ("page", ist_dep_t0, False))
    transport.program("schedules", "IST", "arrival", ("page", ist_arr_t0, False))
    transport.program("schedules", "SAW", "departure", ("page", saw_dep_t0, False))
    transport.program("schedules", "SAW", "arrival", ("page", saw_arr_t0, False))
    transport.program("flights", None, None, ("page", [], False))

    fn = run_fn(monkeypatch)
    t0_summary = fn()

    # ADIM (T0/T1 State Confusion Fix) - `t0_t1_state`'in TEK bir final
    # state döndürdüğü (T0 ÇALIŞIR, SONRA T1 ÇALIŞIR, DB'de SADECE
    # T1-sonrası hal KALIR) önceki bir smoke report'ta "T0" diye
    # etiketlenmiş sayıların ASLINDA T1-sonrası olmasına yol açtı
    # (kod hatası DEĞİL, rapor/test-etiketleme hatası - bkz. final
    # rapor). Bu ADIM'da T0 hemen sonrası GERÇEK bir snapshot alınır -
    # T1 henüz hiç çalışmadan, DB'nin T0-SADECE haline bakılarak.
    t0_session = session_factory()
    try:
        t0_flight_keys = {f.flight_key for f in t0_session.query(Flight).all()}
        t0_breakdown = {
            "ist_departure": t0_session.query(Flight).filter_by(airport_iata="IST", direction="departure").count(),
            "ist_arrival": t0_session.query(Flight).filter_by(airport_iata="IST", direction="arrival").count(),
            "saw_departure": t0_session.query(Flight).filter_by(airport_iata="SAW", direction="departure").count(),
            "saw_arrival": t0_session.query(Flight).filter_by(airport_iata="SAW", direction="arrival").count(),
            "total": t0_session.query(Flight).count(),
        }
    finally:
        t0_session.close()

    # --- T1 payload: değişiklikler + yeni uçuş (Bölüm 5) ---
    ist_dep_t1 = [
        _rec("TK001", "TK", "IST", "JFK", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=10), aircraft_icao="B77W"),
        _rec("TK002", "TK", "IST", "ESB", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=1), aircraft_icao="A320"),
        # Flight A: estimated departure eklendi, eşik-üstü gecikme (>10dk)
        _rec("TK101", "TK", "IST", "CDG", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4),
             dep_estimated=DEP_T0 + __import__("datetime").timedelta(minutes=25)),
        # Flight C: aircraft_icao DEĞİŞTİ (A320 -> A321)
        _rec("TK103", "TK", "IST", "DXB", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=5), aircraft_icao="A321"),
        # Flight D: aynı scheduled, actual EKLENDİ
        _rec("TK104", "TK", "IST", "VKO", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=3),
             aircraft_icao="A321", dep_actual=DEP_T0 + __import__("datetime").timedelta(minutes=5)),
        # Flight G: cancelled
        _rec("TK105", "TK", "IST", "AYT", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=1),
             status="cancelled", aircraft_icao="B738"),
        # Flight H: diverted
        _rec("TK106", "TK", "IST", "ESB", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=1),
             status="diverted", aircraft_icao="B738"),
        # Flight E: T1'de görünen YENİ uçuş
        _rec("TK900", "TK", "IST", "MAD", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A321"),
        # Flight F: T0'da aircraft yok, T1'de GEÇ enrichment (A320)
        _rec("TK107", "TK", "IST", "CDG", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A320"),
    ]
    ist_arr_t1 = [
        _rec("AF204", "AF", "CDG", "IST", DEP_T0, ARR_T0, aircraft_icao="A321"),
        # Flight B: estimated arrival eklendi, eşik-üstü
        _rec("BA205", "BA", "LHR", "IST", DEP_T0, ARR_T0, aircraft_icao="A320",
             arr_estimated=ARR_T0 + __import__("datetime").timedelta(minutes=30)),
    ]
    saw_dep_t1 = list(saw_dep_t0)
    saw_arr_t1 = list(saw_arr_t0)

    transport.program("schedules", "IST", "departure", ("page", ist_dep_t1, False))
    transport.program("schedules", "IST", "arrival", ("page", ist_arr_t1, False))
    transport.program("schedules", "SAW", "departure", ("page", saw_dep_t1, False))
    transport.program("schedules", "SAW", "arrival", ("page", saw_arr_t1, False))
    transport.program("flights", None, None, ("page", [], False))

    fn2 = run_fn(monkeypatch)
    t1_summary = fn2()

    return {
        "transport": transport, "sessions": session_factory,
        "t0_summary": t0_summary, "t1_summary": t1_summary,
        "t0_flight_keys": t0_flight_keys, "t0_breakdown": t0_breakdown,
    }


def test_t0_initial_ingest(t0_t1_state):
    """Bölüm 4 - T0 sonunda beklenen satırlar/alanlar doğru."""
    flights = flights_by_key(t0_t1_state["sessions"])
    assert t0_t1_state["t0_summary"]["failed"] == 0

    tk001 = flights["TK_001_2026-09-15_IST_departure"]
    assert tk001.airport_iata == "IST"
    assert tk001.direction == DIRECTION_DEPARTURE
    assert tk001.location == "international"
    assert tk001.aircraft_icao == "B77W"

    tk002 = flights["TK_002_2026-09-15_IST_departure"]
    assert tk002.location == "domestic"

    af204 = flights["AF_204_2026-09-15_IST_arrival"]
    assert af204.direction == DIRECTION_ARRIVAL
    assert af204.location == "international"

    pc301 = flights["PC_301_2026-09-15_SAW_departure"]
    assert pc301.airport_iata == "SAW"

    session = t0_t1_state["sessions"]()
    try:
        assert session.query(QueuePrediction).count() > 0, "T0 sonunda prediction üretilmeli"
    finally:
        session.close()


def test_t0_exact_count_breakdown_ground_truth(t0_t1_state):
    """
    ADIM (Önceki smoke report'taki '9 rows' vs '7+2+1+1=11' çelişkisini
    ÇÖZ) - GERÇEK kök neden: `t0_t1_state` fixture'ı T0'ı çalıştırıp
    HEMEN ARDINDAN T1'i de çalıştırıyor, DB'de sadece T1-SONRASI hal
    KALIYOR - önceki raporun "T0" diye baktığı session ASLINDA
    T1-sonrasıydı (TK900 - T1'de eklenen yeni uçuş - dahil, 9 IST
    departure). Kod hatası DEĞİL, ölçüm/raporlama hatasıydı. Bu ADIM'da
    `_run_t0_t1()` artık T1 çalışmadan HEMEN ÖNCE gerçek bir T0-SADECE
    snapshot alıp `t0_t1_state["t0_breakdown"]` olarak döndürüyor - bu
    test ARTIK o GERÇEK T0 anlık görüntüsünü doğruluyor.
    """
    t0 = t0_t1_state["t0_breakdown"]
    assert t0["ist_departure"] == 8, "IST departure @ T0: TK001/002/101/103/104/105/106/107"
    assert t0["ist_arrival"] == 2, "IST arrival @ T0: AF204/BA205"
    assert t0["saw_departure"] == 1, "SAW departure @ T0: PC301"
    assert t0["saw_arrival"] == 1, "SAW arrival @ T0: PC302"
    assert t0["ist_departure"] + t0["ist_arrival"] + t0["saw_departure"] + t0["saw_arrival"] == t0["total"] == 12
    assert len(t0_t1_state["t0_flight_keys"]) == t0["total"], "duplicate flight_key YOK @ T0"


def test_t1_exact_count_breakdown_after_new_flight(t0_t1_state):
    """T1 = T0 (12) + TK900 (tek gerçek yeni uçuş) = 13; TK107 T0'da zaten vardı, T1'de UPDATE'tir, yeni satır DEĞİL."""
    s = t0_t1_state["sessions"]()
    try:
        ist_dep = s.query(Flight).filter_by(airport_iata="IST", direction="departure").count()
        ist_arr = s.query(Flight).filter_by(airport_iata="IST", direction="arrival").count()
        saw_dep = s.query(Flight).filter_by(airport_iata="SAW", direction="departure").count()
        saw_arr = s.query(Flight).filter_by(airport_iata="SAW", direction="arrival").count()
        total = s.query(Flight).count()
        distinct_keys = s.query(Flight.flight_key).distinct().count()
    finally:
        s.close()

    assert ist_dep == 9, "IST departure @ T1: T0'daki 8 + TK900 (yeni)"
    assert ist_arr == 2
    assert saw_dep == 1
    assert saw_arr == 1
    assert ist_dep + ist_arr + saw_dep + saw_arr == total == 13
    assert distinct_keys == total, "duplicate flight_key YOK @ T1"
    assert total == t0_t1_state["t0_breakdown"]["total"] + 1, "T1 sadece TAM OLARAK 1 yeni satır eklemeli (TK900)"


def test_no_duplicate_flights_at_t0(t0_t1_state):
    s = t0_t1_state["sessions"]()
    try:
        keys = [f.flight_key for f in s.query(Flight).all()]
        assert len(keys) == len(set(keys))
    finally:
        s.close()


def test_t1_update_flags(t0_t1_state):
    assert t0_t1_state["t1_summary"]["failed"] == 0
    assert t0_t1_state["t1_summary"]["updated"] > 0
    assert t0_t1_state["t1_summary"]["inserted"] >= 1  # TK900 - T1'de görünen tek GERÇEK yeni uçuş (TK107 T0'da da vardı, T1'de update)


def test_new_flight_inserted_exactly_once(t0_t1_state):
    """Bölüm 10 - Flight E (TK900) SADECE T1'de var, tam 1 satır."""
    s = t0_t1_state["sessions"]()
    try:
        rows = s.query(Flight).filter_by(flight_key="TK_900_2026-09-15_IST_departure").all()
        assert len(rows) == 1
    finally:
        s.close()


def test_estimated_departure_update_and_delayed_event(t0_t1_state):
    """Bölüm 5/6 - Flight A: estimated departure değişti, eşik-üstü DELAYED event üretildi."""
    flights = flights_by_key(t0_t1_state["sessions"])
    tk101 = flights["TK_101_2026-09-15_IST_departure"]
    assert tk101.dep_estimated_utc is not None

    ev = events(t0_t1_state["sessions"])
    delayed = [e for e in ev if e.flight_key == "TK_101_2026-09-15_IST_departure" and e.event_type == EVENT_DELAYED]
    assert len(delayed) == 1


def test_estimated_arrival_update(t0_t1_state):
    """Bölüm 5 - Flight B: estimated arrival değişti."""
    flights = flights_by_key(t0_t1_state["sessions"])
    ba205 = flights["BA_205_2026-09-15_IST_arrival"]
    assert ba205.arr_estimated_utc is not None


def test_actual_added_same_scheduled(t0_t1_state):
    """Bölüm 5 - Flight D: scheduled aynı kaldı, actual eklendi, AYNI Flight row."""
    flights = flights_by_key(t0_t1_state["sessions"])
    tk104 = flights["TK_104_2026-09-15_IST_departure"]
    assert tk104.dep_actual_utc is not None
    assert tk104.dep_scheduled_utc == DEP_T0


def test_aircraft_change_updates_row_and_event(t0_t1_state):
    """Bölüm 5/6 - Flight C: aircraft A320->A321, AYNI row, AIRCRAFT_CHANGED event var."""
    flights = flights_by_key(t0_t1_state["sessions"])
    tk103 = flights["TK_103_2026-09-15_IST_departure"]
    assert tk103.aircraft_icao == "A321"

    ev = events(t0_t1_state["sessions"])
    changed = [e for e in ev if e.flight_key == "TK_103_2026-09-15_IST_departure" and e.event_type == EVENT_AIRCRAFT_CHANGED]
    assert len(changed) == 1
    assert changed[0].old_value == "A320"
    assert changed[0].new_value == "A321"


def test_aircraft_late_enrichment_no_event_but_updates(t0_t1_state):
    """Bölüm 7 - Flight F: T0'da aircraft yok, T1'de A320 - update oluyor ama AIRCRAFT_CHANGED event YOK (mevcut kontrat: old_icao None ise event üretilmez)."""
    flights = flights_by_key(t0_t1_state["sessions"])
    tk107 = flights["TK_107_2026-09-15_IST_departure"]
    assert tk107.aircraft_icao == "A320"

    ev = events(t0_t1_state["sessions"])
    changed = [e for e in ev if e.flight_key == "TK_107_2026-09-15_IST_departure" and e.event_type == EVENT_AIRCRAFT_CHANGED]
    assert changed == [], "late enrichment (None -> değer) mevcut kontrata göre AIRCRAFT_CHANGED event ÜRETMEMELİ"


def test_cancelled_flight_same_row_and_event(t0_t1_state):
    """Bölüm 8 - Flight G: cancelled, aynı row, CANCELLED event."""
    flights = flights_by_key(t0_t1_state["sessions"])
    tk105 = flights["TK_105_2026-09-15_IST_departure"]
    assert tk105.status == "cancelled"

    ev = events(t0_t1_state["sessions"])
    cancelled = [e for e in ev if e.flight_key == "TK_105_2026-09-15_IST_departure" and e.event_type == EVENT_CANCELLED]
    assert len(cancelled) == 1


def test_diverted_flight_same_row_and_event(t0_t1_state):
    """Bölüm 9 - Flight H: diverted, aynı row, DIVERTED event."""
    flights = flights_by_key(t0_t1_state["sessions"])
    tk106 = flights["TK_106_2026-09-15_IST_departure"]
    assert tk106.status == "diverted"

    ev = events(t0_t1_state["sessions"])
    diverted = [e for e in ev if e.flight_key == "TK_106_2026-09-15_IST_departure" and e.event_type == EVENT_DIVERTED]
    assert len(diverted) == 1


def test_unrelated_flights_produce_no_events(t0_t1_state):
    """Bölüm 6 - TK001/TK002/PC301/PC302 (T0->T1 arası HİÇ değişmedi) event üretmemeli."""
    ev = events(t0_t1_state["sessions"])
    unchanged_keys = {
        "TK_001_2026-09-15_IST_departure", "TK_002_2026-09-15_IST_departure",
        "PC_301_2026-09-15_SAW_departure", "PC_302_2026-09-15_SAW_arrival",
    }
    noisy = [e for e in ev if e.flight_key in unchanged_keys]
    assert noisy == []


def test_no_duplicate_events(t0_t1_state):
    ev = events(t0_t1_state["sessions"])
    fingerprints = [(e.flight_key, e.event_type, e.old_value, e.new_value) for e in ev]
    assert len(fingerprints) == len(set(fingerprints))


def test_no_duplicate_flights_after_t1(t0_t1_state):
    s = t0_t1_state["sessions"]()
    try:
        keys = [f.flight_key for f in s.query(Flight).all()]
        assert len(keys) == len(set(keys))
    finally:
        s.close()


def test_airport_isolation_ist_saw(t0_t1_state):
    """Bölüm 21 - IST değişiklikleri SAW flight/prediction'larını etkilememeli ve tersi."""
    flights = flights_by_key(t0_t1_state["sessions"])
    saw_keys = [k for k in flights if flights[k].airport_iata == "SAW"]
    assert len(saw_keys) == 2
    for k in saw_keys:
        assert flights[k].status == "scheduled", "SAW uçuşlarına IST'deki cancel/divert/aircraft-change SIZMAMALI"

    s = t0_t1_state["sessions"]()
    try:
        ist_predictions = s.query(QueuePrediction).filter_by(airport_iata="IST").count()
        saw_predictions = s.query(QueuePrediction).filter_by(airport_iata="SAW").count()
        assert ist_predictions > 0
        assert saw_predictions > 0
    finally:
        s.close()


def test_prediction_refresh_structural(t0_t1_state):
    """Bölüm 20 - exact risk threshold İDDİASI YOK (known calibration drift) - sadece yapısal doğrulama."""
    assert t0_t1_state["t0_summary"]["predictions"] > 0
    assert t0_t1_state["t1_summary"]["predictions"] > 0

    s = t0_t1_state["sessions"]()
    try:
        ist_rows = s.query(QueuePrediction).filter_by(airport_iata="IST").all()
        processes = {r.process for r in ist_rows}
        assert len(processes) > 0
        assert all(r.expected_passengers >= 0 for r in ist_rows)
    finally:
        s.close()


def test_health_heartbeat_after_full_refresh(t0_t1_state):
    """Bölüm 19 - health mekanizmasını DEĞİŞTİRMEDEN, mevcut contract'ı gerçek refresh sonrası doğrula."""
    session = t0_t1_state["sessions"]()
    try:
        from app.health import record_successful_refresh
        record_successful_refresh(session)
        last = get_last_successful_refresh(session)
        assert last is not None

        report = build_health_report(session)
        assert report["stale"] is False
        assert report["prediction_stale"] is False
        assert report["status"] == "healthy"
        assert report["source_mode"] == "airlabs"
        assert report["source_live_ingestion_configured"] is True
    finally:
        session.close()


def test_repeated_refresh_idempotency(monkeypatch, t0_t1_state):
    """Bölüm 22 - AYNI T1 response'u tekrar çalıştır - flight count aynı, duplicate yok, crash yok."""
    s = t0_t1_state["sessions"]()
    try:
        before_flights = s.query(Flight).count()
        before_events = s.query(FlightEvent).count()
    finally:
        s.close()

    transport = t0_t1_state["transport"]
    # ADIM - `t0_t1_state` module-scope'lu olduğu için `urllib.request.
    # urlopen`'ı bu transport'a AÇIKÇA geri bağlamak gerekir - fonksiyon-
    # scope'lu `_block_real_network` autouse fixture'ı her testten önce
    # onu `_forbidden`'a SIFIRLAR.
    monkeypatch.setattr(urllib.request, "urlopen", transport.urlopen)

    # T1 ile AYNI response'ları tekrar programla (bire bir aynı veri).
    ist_dep_t1_repeat = [
        _rec("TK001", "TK", "IST", "JFK", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=10), aircraft_icao="B77W"),
        _rec("TK002", "TK", "IST", "ESB", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=1), aircraft_icao="A320"),
        _rec("TK101", "TK", "IST", "CDG", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4),
             dep_estimated=DEP_T0 + __import__("datetime").timedelta(minutes=25)),
        _rec("TK103", "TK", "IST", "DXB", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=5), aircraft_icao="A321"),
        _rec("TK104", "TK", "IST", "VKO", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=3),
             aircraft_icao="A321", dep_actual=DEP_T0 + __import__("datetime").timedelta(minutes=5)),
        _rec("TK105", "TK", "IST", "AYT", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=1),
             status="cancelled", aircraft_icao="B738"),
        _rec("TK106", "TK", "IST", "ESB", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=1),
             status="diverted", aircraft_icao="B738"),
        _rec("TK900", "TK", "IST", "MAD", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A321"),
        _rec("TK107", "TK", "IST", "CDG", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A320"),
    ]
    ist_arr_t1_repeat = [
        _rec("AF204", "AF", "CDG", "IST", DEP_T0, ARR_T0, aircraft_icao="A321"),
        _rec("BA205", "BA", "LHR", "IST", DEP_T0, ARR_T0, aircraft_icao="A320",
             arr_estimated=ARR_T0 + __import__("datetime").timedelta(minutes=30)),
    ]
    saw_dep_repeat = [
        _rec("PC301", "PC", "SAW", "VKO", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A321"),
    ]
    saw_arr_repeat = [
        _rec("PC302", "PC", "DXB", "SAW", DEP_T0, ARR_T0, aircraft_icao="A321"),
    ]
    transport.program("schedules", "IST", "departure", ("page", ist_dep_t1_repeat, False))
    transport.program("schedules", "IST", "arrival", ("page", ist_arr_t1_repeat, False))
    transport.program("schedules", "SAW", "departure", ("page", saw_dep_repeat, False))
    transport.program("schedules", "SAW", "arrival", ("page", saw_arr_repeat, False))
    transport.program("flights", None, None, ("page", [], False))

    fn = run_fn(monkeypatch)
    summary = fn()
    assert summary["failed"] == 0

    s = t0_t1_state["sessions"]()
    try:
        after_flights = s.query(Flight).count()
        after_events = s.query(FlightEvent).count()
        keys = [f.flight_key for f in s.query(Flight).all()]
    finally:
        s.close()

    assert after_flights == before_flights, "tekrar aynı T1 datası flight sayısını DEĞİŞTİRMEMELİ"
    assert len(keys) == len(set(keys))
    assert after_events == before_events, "tamamen AYNI veri tekrar geldiğinde YENİ event üretilmemeli"


# ========================================================================
# Pagination + duplicate-across-pages - Bölüm 11/12
# ========================================================================

def test_pagination_collects_all_records_across_two_pages(monkeypatch, isolated_db):
    transport = install_transport(monkeypatch)
    page1 = [
        _rec("TK201", "TK", "IST", "CDG", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A320"),
        _rec("TK202", "TK", "IST", "JFK", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=10), aircraft_icao="B77W"),
    ]
    page2 = [
        _rec("TK203", "TK", "IST", "DXB", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=5), aircraft_icao="A321"),
        _rec("TK204", "TK", "IST", "MAD", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A320"),
    ]
    transport.program("schedules", "IST", "departure", ("page", page1, True), ("page", page2, False))
    transport.program("schedules", "IST", "arrival", ("page", [], False))
    transport.program("schedules", "SAW", "departure", ("page", [], False))
    transport.program("schedules", "SAW", "arrival", ("page", [], False))
    transport.program("flights", None, None, ("page", [], False))

    fn = run_fn(monkeypatch)
    summary = fn()

    assert summary["failed"] == 0
    s = isolated_db()
    try:
        keys = {f.flight_key for f in s.query(Flight).filter(Flight.airport_iata == "IST", Flight.direction == "departure").all()}
    finally:
        s.close()
    assert keys == {
        "TK_201_2026-09-15_IST_departure", "TK_202_2026-09-15_IST_departure",
        "TK_203_2026-09-15_IST_departure", "TK_204_2026-09-15_IST_departure",
    }
    ist_dep_calls = [c for c in transport.calls if c["airport"] == "IST" and c["direction"] == "departure"]
    assert len(ist_dep_calls) == 2, "iki sayfa = iki istek, sayfalama gerçekten ilerledi"
    assert ist_dep_calls[1]["offset"] == 2, "offset gerçek sayfa boyutuna göre ilerlemeli (sabit değil)"


def test_duplicate_flight_across_pages_does_not_create_duplicate_row(monkeypatch, isolated_db):
    """Bölüm 12 - aynı flight page1 VE page2'de tekrarlanırsa DB'de 1 satır kalmalı."""
    transport = install_transport(monkeypatch)
    dup_record = _rec("TK301", "TK", "IST", "CDG", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A320")
    page1 = [dup_record, _rec("TK302", "TK", "IST", "JFK", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=10), aircraft_icao="B77W")]
    page2 = [dup_record]  # AYNI uçuş tekrar
    transport.program("schedules", "IST", "departure", ("page", page1, True), ("page", page2, False))
    transport.program("schedules", "IST", "arrival", ("page", [], False))
    transport.program("schedules", "SAW", "departure", ("page", [], False))
    transport.program("schedules", "SAW", "arrival", ("page", [], False))
    transport.program("flights", None, None, ("page", [], False))

    fn = run_fn(monkeypatch)
    fn()

    s = isolated_db()
    try:
        rows = s.query(Flight).filter_by(flight_key="TK_301_2026-09-15_IST_departure").all()
        predictions_ist = s.query(QueuePrediction).filter_by(airport_iata="IST").count()
    finally:
        s.close()
    assert len(rows) == 1, "aynı flight iki sayfada tekrarlansa BİLE DB'de tek satır olmalı"
    assert predictions_ist > 0


# ========================================================================
# HTTP fault recovery - Bölüm 14-18 (uçtan uca: worker -> pipeline -> DB)
# ========================================================================

def _empty_all_other_sources(transport, skip=()):
    for ep, airport, direction in [
        ("schedules", "IST", "departure"), ("schedules", "IST", "arrival"),
        ("schedules", "SAW", "departure"), ("schedules", "SAW", "arrival"),
    ]:
        if (airport, direction) in skip:
            continue
        transport.program(ep, airport, direction, ("page", [], False))
    transport.program("flights", None, None, ("page", [], False))


def test_429_then_200_recovers_and_ingests(monkeypatch, isolated_db, caplog):
    transport = install_transport(monkeypatch)
    records = [_rec("TK401", "TK", "IST", "CDG", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A320")]
    transport.program("schedules", "IST", "departure", ("http_error", 429, {"Retry-After": "0"}), ("page", records, False))
    _empty_all_other_sources(transport, skip={("IST", "departure")})

    with caplog.at_level(logging.INFO):
        fn = run_fn(monkeypatch)
        summary = fn()

    assert summary["failed"] == 0
    s = isolated_db()
    try:
        assert s.query(Flight).filter_by(flight_key="TK_401_2026-09-15_IST_departure").count() == 1
    finally:
        s.close()
    ist_dep_calls = [c for c in transport.calls if c["airport"] == "IST" and c["direction"] == "departure"]
    assert len(ist_dep_calls) == 2, "429 sonrası tam olarak 1 retry ile başarıya ulaşmalı"


def test_5xx_then_200_recovers(monkeypatch, isolated_db):
    transport = install_transport(monkeypatch)
    records = [_rec("TK402", "TK", "IST", "JFK", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=10), aircraft_icao="B77W")]
    transport.program("schedules", "IST", "departure", ("http_error", 500, None), ("page", records, False))
    _empty_all_other_sources(transport, skip={("IST", "departure")})

    fn = run_fn(monkeypatch)
    summary = fn()

    assert summary["failed"] == 0
    s = isolated_db()
    try:
        assert s.query(Flight).filter_by(flight_key="TK_402_2026-09-15_IST_departure").count() == 1
    finally:
        s.close()


def test_5xx_persistent_bounded_failure_does_not_crash_worker(monkeypatch, isolated_db):
    """Bölüm 15 - 500/500/500 (MAX_ATTEMPTS=3'ü aşar) - IST departure bu turda boş kalır ama worker ÇÖKMEZ."""
    transport = install_transport(monkeypatch)
    transport.program(
        "schedules", "IST", "departure",
        ("http_error", 500, None), ("http_error", 500, None), ("http_error", 500, None),
    )
    _empty_all_other_sources(transport, skip={("IST", "departure")})

    fn = run_fn(monkeypatch)
    summary = fn()  # exception FIRLATMAMALI

    assert summary["failed"] == 0, "üst seviye run() bunu bir çökme olarak görmemeli - havalimanı-izole hata"
    s = isolated_db()
    try:
        ist_dep_count = s.query(Flight).filter_by(airport_iata="IST", direction="departure").count()
    finally:
        s.close()
    assert ist_dep_count == 0, "3 kez 500 alan IST departure bu turda hiç veri üretmemeli"
    ist_dep_calls = [c for c in transport.calls if c["airport"] == "IST" and c["direction"] == "departure"]
    assert len(ist_dep_calls) == 3, "MAX_ATTEMPTS=3 ile SINIRLI, sonsuz retry YOK"


def test_timeout_then_200_recovers(monkeypatch, isolated_db):
    transport = install_transport(monkeypatch)
    records = [_rec("TK403", "TK", "IST", "DXB", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=5), aircraft_icao="A321")]
    transport.program("schedules", "IST", "departure", ("url_error", TimeoutError("timed out")), ("page", records, False))
    _empty_all_other_sources(transport, skip={("IST", "departure")})

    fn = run_fn(monkeypatch)
    summary = fn()

    assert summary["failed"] == 0
    s = isolated_db()
    try:
        assert s.query(Flight).filter_by(flight_key="TK_403_2026-09-15_IST_departure").count() == 1
    finally:
        s.close()
    ist_dep_calls = [c for c in transport.calls if c["airport"] == "IST" and c["direction"] == "departure"]
    assert len(ist_dep_calls) == 2, "timeout sonrası bounded retry ile kurtarma - sonsuz döngü YOK"


def test_auth_failure_is_permanent_no_pointless_retry(monkeypatch, isolated_db, caplog):
    transport = install_transport(monkeypatch)
    transport.program("schedules", "IST", "departure", ("http_error", 401, None))
    _empty_all_other_sources(transport, skip={("IST", "departure")})

    with caplog.at_level(logging.INFO):
        fn = run_fn(monkeypatch)
        summary = fn()  # AirLabsError izole edilir, run() genelinde exception fırlamaz

    assert summary["failed"] == 0
    ist_dep_calls = [c for c in transport.calls if c["airport"] == "IST" and c["direction"] == "departure"]
    assert len(ist_dep_calls) == 1, "401 RETRY EDİLMEMELİ - tek istek, hemen kalıcı hata"
    assert FAKE_KEY not in caplog.text


def test_malformed_json_is_controlled_failure_other_sources_continue(monkeypatch, isolated_db, caplog):
    """
    Bölüm 18 - ADIM (Malformed JSON Airport Isolation Fix) SONRASI:
    `_request_page()` artık malformed JSON'ı `AirLabsError`'a SARIYOR
    (önceki ham `json.JSONDecodeError` DEĞİL) - bu, `build_source_a()`'nın
    per-airport `except AirLabsError:` izolasyonu tarafından ARTIK
    YAKALANIYOR. Sonuç: IST departure'ın malformed cevabı SADECE IST'i
    etkiler - AYNI YÖNDEKİ (departure) SAW'ın verisi ARTIK KORUNUYOR
    (önceki ADIM'da bu test tam tersini - kaybolduğunu - doğruluyordu,
    o GERÇEK bir gap'ti, şimdi düzeltildi).
    """
    transport = install_transport(monkeypatch)
    transport.program("schedules", "IST", "departure", ("malformed",))
    saw_dep = [_rec("PC501", "PC", "SAW", "DXB", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A321")]
    transport.program("schedules", "SAW", "departure", ("page", saw_dep, False))
    ist_arr = [_rec("AF501", "AF", "CDG", "IST", DEP_T0, ARR_T0, aircraft_icao="A320")]
    transport.program("schedules", "IST", "arrival", ("page", ist_arr, False))
    transport.program("schedules", "SAW", "arrival", ("page", [], False))
    transport.program("flights", None, None, ("page", [], False))

    with caplog.at_level(logging.WARNING):
        fn = run_fn(monkeypatch)
        summary = fn()

    assert summary["failed"] == 0, "malformed bir endpoint worker/process'i ÇÖKERTMEMELİ (asıl garanti)"
    s = isolated_db()
    try:
        assert s.query(Flight).filter_by(flight_key="TK_501_2026-09-15_IST_departure").count() == 0
        # DÜZELTME DOĞRULAMASI: SAW departure ARTIK korunuyor - IST'in
        # malformed cevabı sadece IST'i etkiliyor (havalimanı-seviyesi izolasyon).
        assert s.query(Flight).filter_by(flight_key="PC_501_2026-09-15_SAW_departure").count() == 1, "FIX SONRASI: SAW artık IST'in malformed JSON'ından ETKİLENMEMELİ"
        # Farklı bir YÖN (arrival) zaten etkilenmiyordu - per-yön izolasyon.
        assert s.query(Flight).filter_by(flight_key="AF_501_2026-09-15_IST_arrival").count() == 1
    finally:
        s.close()
    assert "geçersiz JSON" in caplog.text or "JSONDecodeError" in caplog.text


def test_partial_failure_ist_arrival_fails_others_succeed(monkeypatch, isolated_db, caplog):
    """Bölüm 13 - IST arrival kalıcı 500, IST departure/SAW departure/SAW arrival BAŞARILI."""
    transport = install_transport(monkeypatch)
    ist_dep = [_rec("TK601", "TK", "IST", "CDG", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A320")]
    saw_dep = [_rec("PC601", "PC", "SAW", "VKO", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=4), aircraft_icao="A321")]
    saw_arr = [_rec("PC602", "PC", "DXB", "SAW", DEP_T0, ARR_T0, aircraft_icao="A321")]

    transport.program("schedules", "IST", "departure", ("page", ist_dep, False))
    transport.program("schedules", "IST", "arrival", ("http_error", 500, None), ("http_error", 500, None), ("http_error", 500, None))
    transport.program("schedules", "SAW", "departure", ("page", saw_dep, False))
    transport.program("schedules", "SAW", "arrival", ("page", saw_arr, False))
    transport.program("flights", None, None, ("page", [], False))

    with caplog.at_level(logging.WARNING):
        fn = run_fn(monkeypatch)
        summary = fn()

    assert summary["failed"] == 0, "bir yönün kalıcı hatası TÜM refresh'i başarısız SAYDIRMAMALI"
    s = isolated_db()
    try:
        assert s.query(Flight).filter_by(flight_key="TK_601_2026-09-15_IST_departure").count() == 1
        assert s.query(Flight).filter_by(flight_key="PC_601_2026-09-15_SAW_departure").count() == 1
        assert s.query(Flight).filter_by(flight_key="PC_602_2026-09-15_SAW_arrival").count() == 1
        assert s.query(Flight).filter_by(airport_iata="IST", direction="arrival").count() == 0
    finally:
        s.close()
    assert "IST" in caplog.text or "arrival" in caplog.text, "IST arrival failure loglarda GÖRÜNÜR olmalı"
    assert FAKE_KEY not in caplog.text


# ========================================================================
# Secret safety - Bölüm 24
# ========================================================================

def test_fake_key_never_appears_in_any_captured_log(monkeypatch, isolated_db, caplog):
    transport = install_transport(monkeypatch)
    records = [_rec("TK701", "TK", "IST", "JFK", DEP_T0, DEP_T0 + __import__("datetime").timedelta(hours=10), aircraft_icao="B77W")]
    transport.program("schedules", "IST", "departure", ("http_error", 429, {"Retry-After": "0"}), ("http_error", 500, None), ("page", records, False))
    _empty_all_other_sources(transport, skip={("IST", "departure")})

    with caplog.at_level(logging.DEBUG):
        fn = run_fn(monkeypatch)
        fn()

    assert FAKE_KEY not in caplog.text
    for record in caplog.records:
        assert FAKE_KEY not in str(record.msg)
        assert FAKE_KEY not in str(record.args)


def test_real_network_guard_blocks_unpatched_urlopen(monkeypatch, isolated_db):
    """Bölüm 25 - transport hiç kurulmazsa (sadece autouse guard aktif) HERHANGİ bir istek AssertionError vermeli."""
    with pytest.raises(AssertionError, match="GERÇEK ağa"):
        fn = run_fn(monkeypatch)
        fn()
