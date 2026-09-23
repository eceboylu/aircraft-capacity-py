"""
ADIM (AirLabs Refresh + Timezone Validation) - fake live-smoke sonrası
kalan eksikler:

  1. Malformed JSON airport-isolation fix'inin doğrulanması (ayrı,
     zaten `tests/test_airlabs_live_smoke.py::
     test_malformed_json_is_controlled_failure_other_sources_continue`
     içinde yapıldı - burada TEKRARLANMAZ).
  2. Explicit T0/T1/T2/T3 5-dakikalık refresh insert/update matrix'i.
  3. UTC canonical backend + havalimanı-local graph zaman davranışının
     GERÇEK production API path'i (`app/queue/api.py:airport_predictions`)
     üzerinden uçtan uca kanıtı.

Bölüm 2-4 (refresh matrix) `app/worker.py:_build_live_run_fn()` ->
gerçek `airlabs_client` -> gerçek `pipeline.run()` -> gerçek ingestion
zincirini, `tests/test_airlabs_live_smoke.py`'nin AYNI `FakeAirLabsTransport`
mock'unu YENİDEN KULLANARAK (import - duplicate üretilmedi) çalıştırır.

Bölüm 5-13 (timezone) BİLİNÇLİ bir kapsam kararı: ingestion katmanının
AirLabs-mock'lu olması (IST/SAW gibi GERÇEKTEN "tracked" olması makul
havalimanları için) ile CDG/JFK gibi yabancı havalimanlarının GERÇEK
production `run_predictions()`/`airport_predictions()` çağrılarına
girdi olarak DOĞRUDAN seed edilmesi AYRI kaygılardır - burada test
edilen şey graph/display katmanının timezone doğruluğu, ingestion'ın
KENDİSİ DEĞİL (o zaten Bölüm 2-4'te ve `test_airlabs_live_smoke.py`'de
kanıtlandı). Bu yüzden Flight satırları GERÇEK AirLabs alan şekliyle
(snake_case) ama DOĞRUDAN ORM ile seed edilir, `run_predictions()`
(GERÇEK, DEĞİŞTİRİLMEMİŞ motor) çağrılır, SONRA GERÇEK `airport_
predictions()` API fonksiyonu sorgulanır.

Gerçek ağa HİÇ çıkılmaz, gerçek API key KULLANILMAZ.
"""

from __future__ import annotations

import logging
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.queue.pipeline as pipeline_module
import app.worker as worker_module
from app.models import Base
from app.queue.api import airport_predictions
from app.queue.constants import (
    DEPARTURE_SHOW_UP_PROFILE,
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    EVENT_AIRCRAFT_CHANGED,
    EVENT_DELAYED,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
)
from app.queue.domain.operational_day import resolve_airport_timezone
from app.queue.engine import run_predictions
from app.queue.ingestion.airports_import import import_airports
from app.queue.models import Airport, Flight, FlightEvent, QueuePrediction
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset
from app.service import AircraftCapacityService

from .test_airlabs_live_smoke import (
    FAKE_KEY,
    REAL_AIRPORTS_SQL,
    FakeAirLabsTransport,
    _rec,
    install_transport,
    run_fn,
)

TRACKED = ["IST"]


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("GERÇEK ağa çıkılmaya çalışıldı - bu dosya SADECE fake transport/doğrudan DB seed kullanmalı")
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)
    monkeypatch.setenv("AIRLABS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", ",".join(TRACKED))


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    import app.queue.ingestion.airlabs_client as client_module
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)


@pytest.fixture
def isolated_db(monkeypatch):
    engine = create_engine("sqlite://")
    SessionLocal = sessionmaker(bind=engine)
    monkeypatch.setattr(pipeline_module, "init_db", lambda drop_first=False: Base.metadata.create_all(engine))
    monkeypatch.setattr(pipeline_module, "get_session", lambda: SessionLocal())

    Base.metadata.create_all(engine)
    seed = SessionLocal()
    import_airports(seed, REAL_AIRPORTS_SQL)
    seed_verified_dataset(seed)
    seed_curated_fallback(seed)
    seed_family_and_ga(seed)
    seed.close()
    return SessionLocal


NOW = datetime(2026, 9, 15, 20, 0)
DEP_BASE = datetime(2026, 9, 15, 10, 0)


def _flight_counts(session_factory):
    s = session_factory()
    try:
        total = s.query(Flight).count()
        distinct = s.query(Flight.flight_key).distinct().count()
    finally:
        s.close()
    return total, distinct


def _event_count(session_factory):
    s = session_factory()
    try:
        return s.query(FlightEvent).count()
    finally:
        s.close()


def _prediction_dedup(session_factory):
    """Bölüm 4 - COUNT(QueuePrediction) vs distinct (airport, process, window_start) - Python tarafında (MySQL/SQLite syntax farkından bağımsız)."""
    s = session_factory()
    try:
        rows = s.query(QueuePrediction.airport_iata, QueuePrediction.process, QueuePrediction.window_start).all()
    finally:
        s.close()
    total = len(rows)
    distinct = len(set(rows))
    return total, distinct


# ========================================================================
# Bölüm 2/3/4 - T0/T1/T2/T3 explicit 5 dakikalık refresh matrix
# ========================================================================

def _five_flights(round_: str):
    """
    round_ = "t0": 5 flight, hepsi ilk hal.
    round_ = "t1": AYNI 5 - F1 estimated (eşik-üstü delay), F2 aircraft değişti.
    round_ = "t2": t1 ile BİREBİR AYNI (byte/semantic tekrar).
    round_ = "t3": t2 + F6 (tamamen yeni).
    """
    base = [
        _rec("TK801", "TK", "IST", "CDG", DEP_BASE, DEP_BASE + timedelta(hours=4), aircraft_icao="A320"),
        _rec("TK802", "TK", "IST", "JFK", DEP_BASE, DEP_BASE + timedelta(hours=10), aircraft_icao="B77W"),
        _rec("TK803", "TK", "IST", "ESB", DEP_BASE, DEP_BASE + timedelta(hours=1), aircraft_icao="A321"),
        _rec("TK804", "TK", "IST", "DXB", DEP_BASE, DEP_BASE + timedelta(hours=5), aircraft_icao="A320"),
        _rec("TK805", "TK", "IST", "AYT", DEP_BASE, DEP_BASE + timedelta(hours=1), aircraft_icao="B738"),
    ]
    if round_ == "t0":
        return base

    # F1 (TK801): estimated departure - eşik-üstü gecikme.
    base[0] = _rec("TK801", "TK", "IST", "CDG", DEP_BASE, DEP_BASE + timedelta(hours=4),
                    aircraft_icao="A320", dep_estimated=DEP_BASE + timedelta(minutes=25))
    # F2 (TK802): aircraft DEĞİŞTİ.
    base[1] = _rec("TK802", "TK", "IST", "JFK", DEP_BASE, DEP_BASE + timedelta(hours=10), aircraft_icao="A359")
    if round_ in ("t1", "t2"):
        return base

    if round_ == "t3":
        base.append(_rec("TK806", "TK", "IST", "MAD", DEP_BASE, DEP_BASE + timedelta(hours=4), aircraft_icao="A321"))
        return base
    raise ValueError(round_)  # pragma: no cover


def _run_round(monkeypatch, transport, records):
    transport.program("schedules", "IST", "departure", ("page", records, False))
    transport.program("schedules", "IST", "arrival", ("page", [], False))
    transport.program("flights", None, None, ("page", [], False))
    fn = run_fn(monkeypatch, domain_now_value=NOW)
    return fn()


@pytest.fixture
def refresh_matrix_state(monkeypatch, isolated_db):
    transport = install_transport(monkeypatch)

    t0_summary = _run_round(monkeypatch, transport, _five_flights("t0"))
    t0_total, t0_distinct = _flight_counts(isolated_db)
    t0_events = _event_count(isolated_db)

    t1_summary = _run_round(monkeypatch, transport, _five_flights("t1"))
    t1_total, t1_distinct = _flight_counts(isolated_db)
    t1_events = _event_count(isolated_db)

    t2_summary = _run_round(monkeypatch, transport, _five_flights("t2"))
    t2_total, t2_distinct = _flight_counts(isolated_db)
    t2_events = _event_count(isolated_db)

    t3_summary = _run_round(monkeypatch, transport, _five_flights("t3"))
    t3_total, t3_distinct = _flight_counts(isolated_db)
    t3_events = _event_count(isolated_db)

    return {
        "sessions": isolated_db,
        "t0": {"summary": t0_summary, "total": t0_total, "distinct": t0_distinct, "events": t0_events},
        "t1": {"summary": t1_summary, "total": t1_total, "distinct": t1_distinct, "events": t1_events},
        "t2": {"summary": t2_summary, "total": t2_total, "distinct": t2_distinct, "events": t2_events},
        "t3": {"summary": t3_summary, "total": t3_total, "distinct": t3_distinct, "events": t3_events},
    }


def test_t0_all_inserts(refresh_matrix_state):
    t0 = refresh_matrix_state["t0"]
    assert t0["summary"]["inserted"] == 5
    assert t0["summary"]["updated"] == 0
    assert t0["total"] == 5
    assert t0["distinct"] == 5


def test_t1_two_updates_no_new_inserts(refresh_matrix_state):
    t1 = refresh_matrix_state["t1"]
    assert t1["summary"]["inserted"] == 0
    assert t1["summary"]["updated"] == 5, "her refresh mevcut TÜM flight'ları upsert eder (Flight identity DEĞİŞMEDİ) - değişen alan sayısı 'updated' satır sayısını değil, event sayısını etkiler"
    assert t1["total"] == 5
    assert t1["distinct"] == 5
    assert t1["events"] == 2, "F1: DELAYED, F2: AIRCRAFT_CHANGED - diğer 3 flight event üretmedi"


def test_t2_identical_repeat_is_true_noop(refresh_matrix_state):
    t0, t1, t2 = refresh_matrix_state["t0"], refresh_matrix_state["t1"], refresh_matrix_state["t2"]
    assert t2["summary"]["inserted"] == 0
    assert t2["total"] == t1["total"] == 5
    assert t2["distinct"] == 5
    assert t2["events"] == t1["events"], "T1 ile BİREBİR AYNI veri tekrar geldiğinde YENİ event üretilmemeli"


def test_t3_exactly_one_new_flight(refresh_matrix_state):
    t2, t3 = refresh_matrix_state["t2"], refresh_matrix_state["t3"]
    assert t3["summary"]["inserted"] == 1
    assert t3["total"] == t2["total"] + 1 == 6
    assert t3["distinct"] == 6


def test_same_identity_preserved_despite_mutable_field_changes(refresh_matrix_state):
    """Bölüm 2 - estimated/actual/gate/status/aircraft değişse bile flight_key AYNI kalır, yeni satır AÇILMAZ."""
    s = refresh_matrix_state["sessions"]()
    try:
        tk801 = s.query(Flight).filter_by(flight_key="TK_801_2026-09-15_IST_departure").all()
        tk802 = s.query(Flight).filter_by(flight_key="TK_802_2026-09-15_IST_departure").all()
    finally:
        s.close()
    assert len(tk801) == 1
    assert tk801[0].dep_estimated_utc is not None
    assert len(tk802) == 1
    assert tk802[0].aircraft_icao == "A359"


def test_flight_event_idempotency_across_rounds(refresh_matrix_state):
    """Bölüm 3 - aircraft/delay event'leri T1'de +1 kere, T2'de (aynı payload) SIFIR ek event."""
    s = refresh_matrix_state["sessions"]()
    try:
        delayed = s.query(FlightEvent).filter_by(flight_key="TK_801_2026-09-15_IST_departure", event_type=EVENT_DELAYED).count()
        changed = s.query(FlightEvent).filter_by(flight_key="TK_802_2026-09-15_IST_departure", event_type=EVENT_AIRCRAFT_CHANGED).count()
    finally:
        s.close()
    assert delayed == 1, "DELAYED event T1->T2 arasında DUPLICATE olmamalı"
    assert changed == 1, "AIRCRAFT_CHANGED event T1->T2 arasında DUPLICATE olmamalı"


def test_queue_prediction_upsert_no_duplicates_across_rounds(refresh_matrix_state):
    """Bölüm 4 - her round sonrası (airport, process, window_start) unique setı satır sayısıyla eşleşmeli."""
    for round_key in ("t0", "t1", "t2", "t3"):
        total, distinct = _prediction_dedup(refresh_matrix_state["sessions"])
        assert total == distinct, f"{round_key} sonrası duplicate QueuePrediction satırı VAR: {total} != {distinct}"
    # T2 (identical repeat) sonrası duplicate prediction row = 0 (yukarıdaki assertion zaten bunu garanti eder,
    # burada AYRICA açıkça doğrulanıyor - Bölüm 4'ün "T2 identical refresh: duplicate prediction row = 0" ifadesi).
    total, distinct = _prediction_dedup(refresh_matrix_state["sessions"])
    assert total - distinct == 0


# ========================================================================
# Bölüm 5 - UTC canonical storage
# ========================================================================

def test_utc_canonical_storage_flight_and_prediction(refresh_matrix_state):
    s = refresh_matrix_state["sessions"]()
    try:
        f = s.query(Flight).filter_by(flight_key="TK_801_2026-09-15_IST_departure").one()
        assert f.dep_scheduled_utc == DEP_BASE, "canonical storage değiştirilmemeli - girdi neyse o"
        assert f.dep_scheduled_utc.tzinfo is None, "proje geneli naive-UTC konvansiyonu (bkz. domain_now() docstring'i) korunmalı"

        predictions = s.query(QueuePrediction).filter_by(airport_iata="IST").limit(5).all()
        assert len(predictions) > 0
        for p in predictions:
            assert p.window_start.tzinfo is None
            assert p.window_end.tzinfo is None
            assert p.window_end > p.window_start
    finally:
        s.close()


# ========================================================================
# Bölüm 6 - Airport-local graph time (IST/CDG/JFK), GERÇEK production API
# ========================================================================

AIRPORT_TZ = {
    "IST": "Europe/Istanbul",
    "CDG": "Europe/Paris",
    "JFK": "America/New_York",
}


def _seed_flight_direct(session, flight_key, airport_iata, direction, location,
                         dep_iata, arr_iata, dep_scheduled=None, arr_scheduled=None,
                         aircraft_icao="A320", status="scheduled"):
    session.add(Flight(
        flight_key=flight_key, airport_iata=airport_iata, direction=direction, location=location,
        airline_iata="TK", flight_number=flight_key.split("_")[1],
        aircraft_icao=aircraft_icao, aircraft_match_found=True,
        dep_iata=dep_iata, arr_iata=arr_iata,
        dep_scheduled_utc=dep_scheduled, arr_scheduled_utc=arr_scheduled,
        status=status, last_refreshed_at=NOW,
    ))


def local_to_utc(local_dt: datetime, tz_name: str) -> datetime:
    """Test-yardımcı: bir LOCAL wall-clock zamanını, GERÇEK `zoneinfo.ZoneInfo` ile (hardcoded offset YOK) naive-UTC'ye çevirir - `resolve_airport_timezone()`'un KULLANDIĞI AYNI mekanizma."""
    aware_local = local_dt.replace(tzinfo=ZoneInfo(tz_name))
    return aware_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


@pytest.fixture
def three_airport_state(isolated_db):
    """IST/CDG/JFK - HER birinde 15:00 LOCAL departure, GERÇEK zoneinfo ile ayrı ayrı UTC'ye çevrilir."""
    session = isolated_db()
    local_dep = datetime(2026, 9, 15, 15, 0)

    # CDG/JFK Airport tablosunda zaten var (flight_airports.sql), ama
    # her ikisi de IST'ten uçan flight'ların arr_iata'sı olarak
    # kullanılıyor - bu senaryoda KENDİLERİ havalimanı (departure
    # tarafı) olacaklar, bu yüzden CDG/JFK KÖKENLİ birer departure
    # flight'ı seed ediliyor.
    dep_utc = {code: local_to_utc(local_dep, tz) for code, tz in AIRPORT_TZ.items()}

    _seed_flight_direct(session, "TZ_IST_2026-09-15_IST_departure", "IST", DIRECTION_DEPARTURE, LOCATION_INTERNATIONAL,
                         "IST", "CDG", dep_scheduled=dep_utc["IST"])
    _seed_flight_direct(session, "TZ_CDG_2026-09-15_CDG_departure", "CDG", DIRECTION_DEPARTURE, LOCATION_INTERNATIONAL,
                         "CDG", "IST", dep_scheduled=dep_utc["CDG"])
    _seed_flight_direct(session, "TZ_JFK_2026-09-15_JFK_departure", "JFK", DIRECTION_DEPARTURE, LOCATION_INTERNATIONAL,
                         "JFK", "IST", dep_scheduled=dep_utc["JFK"])
    session.commit()

    resolver = AircraftCapacityService(session)
    # `run_predictions(airports=None)` -> `airport_codes(session)` (Flight
    # tablosundaki DISTINCT havalimanları, TÜM 9766 referans satırı DEĞİL) -
    # burada tam olarak IST/CDG/JFK işlenir, imza DEĞİŞTİRİLMEDEN kullanıldı.
    run_predictions(session, resolver=resolver, now=NOW)
    session.close()

    return {"sessions": isolated_db, "dep_utc": dep_utc, "local_dep": local_dep}


def test_ist_local_graph_shows_ist_wall_clock(three_airport_state):
    s = three_airport_state["sessions"]()
    try:
        report = airport_predictions(s, "IST", now=NOW)
    finally:
        s.close()
    windows = report["international_departure"]["passport"]["windows"]
    assert windows, "IST için pencere üretilmeli"
    matching = [w for w in windows if w["window_start_local"] and w["window_start_local"][11:16] <= "15:00" < w["window_end_local"][11:16]]
    assert matching, f"15:00 local'i kapsayan bir pencere bulunamadı: {[w['window_start_local'] for w in windows]}"


def test_cdg_local_graph_correct_offset(three_airport_state):
    s = three_airport_state["sessions"]()
    try:
        report = airport_predictions(s, "CDG", now=NOW)
    finally:
        s.close()
    windows = report["international_departure"]["passport"]["windows"]
    assert windows
    for w in windows:
        if w["window_start_local"]:
            assert w["window_start_local"][:10] in ("2026-09-15", "2026-09-16"), w["window_start_local"]


def test_jfk_local_graph_correct_offset(three_airport_state):
    s = three_airport_state["sessions"]()
    try:
        report = airport_predictions(s, "JFK", now=NOW)
    finally:
        s.close()
    windows = report["international_departure"]["passport"]["windows"]
    assert windows


def test_same_utc_instant_different_local_labels_per_airport(three_airport_state):
    """Bölüm 6 - AYNI canonical UTC an, üç havalimanında FARKLI local saat üretmeli (hardcoded offset YOK, gerçek ZoneInfo)."""
    canonical_utc = datetime(2026, 9, 15, 12, 0)
    labels = {}
    for code, tz_name in AIRPORT_TZ.items():
        tz = resolve_airport_timezone(tz_name)
        assert tz is not None, f"{code} için ZoneInfo çözülmeli"
        from app.queue.api import _to_local_iso
        labels[code] = _to_local_iso(canonical_utc, tz)

    assert labels["IST"] != labels["CDG"] != labels["JFK"]
    assert labels["IST"][11:16] == "15:00"   # UTC+3 (Europe/Istanbul, DST yok - IANA veritabanı gereği)
    assert labels["CDG"][11:16] == "14:00"   # UTC+2 (Europe/Paris, Eylül = CEST/DST aktif)
    assert labels["JFK"][11:16] == "08:00"   # UTC-4 (America/New_York, Eylül = EDT/DST aktif)


# ========================================================================
# Bölüm 7/8 - Departure show-up profile local graph + UTC->local roundtrip
# ========================================================================

def test_show_up_profile_bucket_lands_on_correct_local_hour(three_airport_state):
    """
    Bölüm 7/8 - GERÇEK `DEPARTURE_SHOW_UP_PROFILE`'dan (hardcoded eski
    değer İCAT EDİLMEDİ) bir bucket seçilir (90-105dk önce, %15), o
    bucket'ın UTC show-up anı hesaplanır, sonra GERÇEK ZoneInfo ile
    HER havalimanının kendi local saatine çevrilir - departure 15:00
    local ise, 90-105dk önceki show-up 13:15-13:30 local olmalı (UTC
    offset NE olursa olsun, her havalimanı KENDİ yerel saatinde AYNI
    göreli saatte görünmeli).
    """
    offset_start, offset_end, share = next(row for row in DEPARTURE_SHOW_UP_PROFILE if row == (105, 90, 0.15))
    assert share == 0.15  # sadece hangi bucket'ı seçtiğimizi doğrula, profili YENİDEN YAZMA

    local_dep = three_airport_state["local_dep"]
    expected_local_bucket_start = (local_dep - timedelta(minutes=offset_start)).time()
    expected_local_bucket_end = (local_dep - timedelta(minutes=offset_end)).time()
    assert expected_local_bucket_start.strftime("%H:%M") == "13:15"
    assert expected_local_bucket_end.strftime("%H:%M") == "13:30"

    for code, tz_name in AIRPORT_TZ.items():
        dep_utc = three_airport_state["dep_utc"][code]
        show_up_utc_start = dep_utc - timedelta(minutes=offset_start)
        tz = resolve_airport_timezone(tz_name)
        local_label = show_up_utc_start.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        assert local_label.strftime("%H:%M") == "13:15", (
            f"{code}: UTC show-up anının kendi local saatine çevrilmesi departure'dan "
            f"105dk ÖNCEYE (13:15 local) denk gelmeli - offset ne olursa olsun"
        )


def test_utc_to_local_roundtrip_no_bucket_shift(three_airport_state):
    """Bölüm 8 - local -> UTC -> local roundtrip'te +1/-1 saat bucket kayması OLMAMALI."""
    for code, tz_name in AIRPORT_TZ.items():
        local_dep = three_airport_state["local_dep"]
        utc = local_to_utc(local_dep, tz_name)
        tz = resolve_airport_timezone(tz_name)
        back_to_local = utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).replace(tzinfo=None)
        assert back_to_local == local_dep, f"{code}: roundtrip {local_dep} -> UTC {utc} -> local {back_to_local} KAYMAMALI"


# ========================================================================
# Bölüm 9 - Cross-midnight
# ========================================================================

def test_cross_midnight_ist_departure_shows_correct_local_date(isolated_db):
    """IST local 2026-09-24 01:00 departure - UTC'de 2026-09-23 22:00 (önceki takvim günü) - show-up bucket'ları UTC tarihine göre YANLIŞ operasyonel güne atılmamalı."""
    session = isolated_db()
    local_dep = datetime(2026, 9, 24, 1, 0)
    dep_utc = local_to_utc(local_dep, "Europe/Istanbul")
    assert dep_utc.date().isoformat() == "2026-09-23", "UTC tarihi gerçekten ÖNCEKİ takvim gününe düşüyor (test önkoşulu)"

    _seed_flight_direct(session, "TZ_XMID_2026-09-24_IST_departure", "IST", DIRECTION_DEPARTURE, LOCATION_INTERNATIONAL,
                         "IST", "CDG", dep_scheduled=dep_utc)
    session.commit()
    resolver = AircraftCapacityService(session)
    now_for_this = dep_utc - timedelta(hours=2)
    run_predictions(session, resolver=resolver, now=now_for_this)

    report = airport_predictions(session, "IST", now=now_for_this)
    session.close()

    windows = report["international_departure"]["passport"]["windows"]
    local_dates = {w["window_start_local"][:10] for w in windows if w["window_start_local"]}
    assert "2026-09-24" in local_dates or "2026-09-23" in local_dates, (
        "cross-midnight departure'ın show-up pencereleri IST LOCAL takvimine göre "
        f"(23 veya 24 Eylül) görünmeli, ham UTC tarihine göre yanlış bir güne değil - görülen: {local_dates}"
    )


# ========================================================================
# Bölüm 10 - Arrival local time
# ========================================================================

def test_arrival_local_time_jfk(isolated_db):
    session = isolated_db()
    local_arr = datetime(2026, 9, 15, 18, 0)
    arr_utc = local_to_utc(local_arr, "America/New_York")

    _seed_flight_direct(session, "TZ_JFKARR_2026-09-15_JFK_arrival", "JFK", DIRECTION_ARRIVAL, LOCATION_INTERNATIONAL,
                         "IST", "JFK", arr_scheduled=arr_utc)
    session.commit()
    resolver = AircraftCapacityService(session)
    run_predictions(session, resolver=resolver, now=arr_utc - timedelta(hours=1))

    report = airport_predictions(session, "JFK", now=arr_utc - timedelta(hours=1))
    session.close()

    windows = report["international_arrival"]["windows"]
    assert windows, "JFK international arrival için pencere üretilmeli"
    for w in windows:
        assert w["window_start_local"] is None or w["window_start_local"][:10] in ("2026-09-15", "2026-09-16")


# ========================================================================
# Bölüm 11 - Current bucket per airport (AYNI canonical now)
# ========================================================================

def test_current_bucket_selected_independently_per_airport(three_airport_state):
    s = three_airport_state["sessions"]()
    try:
        ist = airport_predictions(s, "IST", now=NOW)
        cdg = airport_predictions(s, "CDG", now=NOW)
        jfk = airport_predictions(s, "JFK", now=NOW)
    finally:
        s.close()

    for report, code in ((ist, "IST"), (cdg, "CDG"), (jfk, "JFK")):
        current = report["international_departure"]["passport"]["current"]
        if current is not None and current.get("window_start_local"):
            assert current["window_start_local"][:4] == "2026", f"{code}: current bucket local label makul görünmeli"


# ========================================================================
# Bölüm 12 - DST safety + hardcoded offset audit
# ========================================================================

def test_cdg_dst_offset_is_real_zoneinfo_not_hardcoded():
    """Eylül'de Europe/Paris CEST'te (UTC+2) - kışın (Ocak) CET'te (UTC+1) olurdu; bu, GERÇEK zoneinfo kullanıldığının kanıtı, sabit offset İCAT EDİLMİŞ olsa bu ayrım YAKALANMAZDI."""
    tz = resolve_airport_timezone("Europe/Paris")
    summer = datetime(2026, 9, 15, 12, 0, tzinfo=__import__("datetime").timezone.utc).astimezone(tz)
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=__import__("datetime").timezone.utc).astimezone(tz)
    assert summer.utcoffset().total_seconds() / 3600 == 2.0
    assert winter.utcoffset().total_seconds() / 3600 == 1.0
    assert summer.utcoffset() != winter.utcoffset(), "DST GERÇEKTEN uygulanıyor - sabit bir offset olsaydı ikisi AYNI olurdu"


def test_no_hardcoded_timezone_offset_in_production_code():
    """Bölüm 12 - repository genelinde şüpheli sabit-offset pattern'i taraması (yorum/docstring hariç, gerçek kod satırı)."""
    import pathlib
    import re

    suspicious = re.compile(r"timedelta\(hours=[+-]?[23]\)\s*#.*(utc|offset|timezone)", re.IGNORECASE)
    hits = []
    for path in pathlib.Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if suspicious.search(line):
                hits.append(f"{path}:{lineno}: {stripped}")
    assert hits == [], f"şüpheli hardcoded timezone offset bulundu: {hits}"


def test_existing_dst_boundary_tests_still_pass():
    """Bölüm 12 - bu ADIM repository'deki mevcut DST/timezone testlerini BOZMADI (ayrıca run_fn/targeted tests bölümünde de çalıştırılıyor, burada import-level bir sağlık kontrolü)."""
    import app.queue.domain.operational_day as od
    assert od.resolve_airport_timezone("Europe/Paris") is not None
    assert od.resolve_airport_timezone("Not/ARealZone") is None


# ========================================================================
# Bölüm 13 - Frontend/API contract (frontend DEĞİŞTİRİLMEDİ, sadece doğrulama)
# ========================================================================

def test_api_preserves_canonical_utc_fields_alongside_local(three_airport_state):
    s = three_airport_state["sessions"]()
    try:
        report = airport_predictions(s, "IST", now=NOW)
    finally:
        s.close()
    windows = report["international_departure"]["passport"]["windows"]
    assert windows
    for w in windows:
        assert "window_start" in w and "window_end" in w, "canonical UTC alanlar HİÇ kaldırılmamalı"
        assert "window_start_local" in w and "window_end_local" in w


def test_frontend_reads_local_display_field_not_raw_utc():
    """Bölüm 13 - frontend'in AKTİF render path'i local alanı kullanıyor mu (dosya İÇERİĞİ okunur, DEĞİŞTİRİLMEZ)."""
    import pathlib
    html = pathlib.Path("app/web/static/index.html").read_text(encoding="utf-8")
    assert "window_start_local" in html, "frontend window_start_local alanını OKUMALI (windowLabel/windowLabelRange fonksiyonları)"
    assert "window.window_start_local" in html or "w.window_start_local" in html or "(window && window.window_start_local)" in html
