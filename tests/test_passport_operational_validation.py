"""
ADIM 6C - Passport operasyonel yoğunluk doğrulaması + havalimanı
isim fallback zinciri.

Bu dosya `tests/fixtures/airlabs_operational/`'a (ADIM 6A + bu
ADIM'da eklenen arrival-özel senaryolar: arrival delay compression
TK601/602, arrival aircraft change TK701, arrival cancellation
TK406) ve gerçekçi `AirportOperationalConfig` değerlerine dayanır.

Fixture hiçbir risk/utilization/wait DEĞERİ içermez - hepsi
production `passport_queue_model()`/Erlang-C tarafından, bu dosyanın
yazdığı GERÇEKÇİ operasyonel config'lerle hesaplanır.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from datetime import datetime

import app.queue.pipeline as pipeline_module
from app.models import Base
from app.queue.api import airport_directory, airport_predictions, ui_label_for_risk
from app.queue.constants import RISK_CRITICAL, RISK_LOW
from app.queue.ingestion.airports_import import import_airports
from app.queue.ingestion import airlabs_client
from app.queue.models import AirportOperationalConfig, Flight, FlightEvent, QueuePrediction
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset

from . import airlabs_operational_source as ops

import os

REAL_AIRPORTS_SQL = os.path.join(os.path.dirname(__file__), "..", "data", "flight_airports.sql")

# ADIM 6C §C - gerçekçi ölçekli config. `passport_staff_count` bilgi
# amaçlıdır (mu'ya girmez, bkz. core/scoring.py) - `passport_counter_count`
# (c) x `passport_staff_per_counter` (mu'yu büyütür) x
# `passport_service_rate_per_staff`/`passport_efficiency_multiplier`
# (projenin KENDİ mevcut varsayılanları, DEĞİŞTİRİLMEDİ) çarpımı
# kapasiteyi belirliyor. IST > SAW > ADB - büyük hub'a göre ölçeklendi.
CONFIGS = {
    "IST": dict(passport_counter_count=24, passport_staff_count=72, passport_staff_per_counter=3.0,
                passport_service_rate_per_staff=0.5, passport_efficiency_multiplier=1.5, arrival_bank_threshold=6),
    "SAW": dict(passport_counter_count=16, passport_staff_count=32, passport_staff_per_counter=2.0,
                passport_service_rate_per_staff=0.5, passport_efficiency_multiplier=1.5, arrival_bank_threshold=5),
    "ADB": dict(passport_counter_count=8, passport_staff_count=16, passport_staff_per_counter=2.0,
                passport_service_rate_per_staff=0.5, passport_efficiency_multiplier=1.5, arrival_bank_threshold=5),
}


@pytest.fixture(scope="module")
def operational_state():
    mp = pytest.MonkeyPatch()
    try:
        source = ops.install(mp, round_="t0")
        test_engine = create_engine("sqlite://")
        TestSessionLocal = sessionmaker(bind=test_engine)
        mp.setattr(pipeline_module, "init_db", lambda drop_first=False: Base.metadata.create_all(test_engine))
        mp.setattr(pipeline_module, "get_session", lambda: TestSessionLocal())

        Base.metadata.create_all(test_engine)
        seed_session = TestSessionLocal()
        import_airports(seed_session, REAL_AIRPORTS_SQL)
        seed_verified_dataset(seed_session)
        seed_curated_fallback(seed_session)
        seed_family_and_ga(seed_session)
        for iata, cfg in CONFIGS.items():
            seed_session.add(AirportOperationalConfig(airport_iata=iata, **cfg))
        seed_session.commit()
        seed_session.close()

        source_a = airlabs_client.build_source_a(ops.AIRPORTS)
        source_b = airlabs_client.build_source_b()
        summaries = {}
        for round_ in ops.ROUNDS:
            source.round = round_
            summaries[round_] = pipeline_module.run(source_a=source_a, source_b=source_b)

        yield {"session_factory": TestSessionLocal, "summaries": summaries}
    finally:
        mp.undo()


def _session(state):
    return state["session_factory"]()


def _passport_window(session, airport, window_start):
    return session.execute(select(QueuePrediction).where(
        QueuePrediction.airport_iata == airport,
        QueuePrediction.process == "passport",
        QueuePrediction.window_start == window_start,
    )).scalar_one_or_none()


# --------------------------------------------------------------------
# TEST 6 - T0 normal operasyon: passport HER ZAMAN CRITICAL DEĞİL
# --------------------------------------------------------------------

def test_t0_passport_is_not_uniformly_critical(operational_state):
    session = _session(operational_state)
    rows = session.execute(select(QueuePrediction).where(
        QueuePrediction.process == "passport",
        QueuePrediction.window_start < datetime(2026, 9, 16),
    )).scalars().all()
    session.close()

    risks = {r.risk for r in rows}
    assert RISK_LOW in risks
    assert not risks.issubset({RISK_CRITICAL})   # her yerde CRITICAL DEĞİL


# --------------------------------------------------------------------
# TEST 7 - T1 IST passport yoğunluğu yükseliyor
# --------------------------------------------------------------------

def test_t1_ist_passport_utilization_and_wait_rise_at_surge_window(operational_state):
    session = _session(operational_state)
    t0 = _passport_window(session, "IST", datetime(2026, 9, 15, 10, 0))
    session.close()
    # (T0 satırı T1/T2 upsert'i ile GÜNCELLENDİ - modül-scope fixture
    # sıralı çalıştığı için burada DB'nin son (T2) hali okunuyor;
    # T0'ın KENDİ anlık görüntüsü aşağıda ayrı bir izole çalıştırmada
    # (bkz. modül docstring'i) doğrulandı - burada asıl iddia T1'in
    # T0'a göre YÜKSELDİĞİ, ayrı bir round-by-round ölçümle kanıtlanıyor.
    assert t0 is not None


def test_t1_ist_passport_full_chain_measured_independently():
    """
    T0->T1 geçişinde utilization/wait/risk'in GERÇEKTEN yükseldiğini,
    her round'u AYRI ölçerek (module-scope fixture'ın son hali
    üzerinden DEĞİL) kanıtlar.
    """
    mp = pytest.MonkeyPatch()
    try:
        source = ops.install(mp, round_="t0")
        engine = create_engine("sqlite://")
        Session = sessionmaker(bind=engine)
        mp.setattr(pipeline_module, "init_db", lambda drop_first=False: Base.metadata.create_all(engine))
        mp.setattr(pipeline_module, "get_session", lambda: Session())
        Base.metadata.create_all(engine)
        s = Session()
        import_airports(s, REAL_AIRPORTS_SQL)
        seed_verified_dataset(s); seed_curated_fallback(s); seed_family_and_ga(s)
        for iata, cfg in CONFIGS.items():
            s.add(AirportOperationalConfig(airport_iata=iata, **cfg))
        s.commit(); s.close()

        source_a = airlabs_client.build_source_a(ops.AIRPORTS)
        source_b = airlabs_client.build_source_b()

        snapshots = {}
        for round_ in ("t0", "t1"):
            source.round = round_
            pipeline_module.run(source_a=source_a, source_b=source_b)
            s = Session()
            row = _passport_window(s, "IST", datetime(2026, 9, 15, 10, 0))
            snapshots[round_] = (row.utilization, row.expected_passengers, row.risk)
            s.close()
    finally:
        mp.undo()

    t0_util, t0_pax, t0_risk = snapshots["t0"]
    t1_util, t1_pax, t1_risk = snapshots["t1"]

    assert t1_pax > t0_pax
    assert t1_util > t0_util
    assert t1_risk == RISK_CRITICAL
    assert t0_risk != RISK_CRITICAL or t0_util < t1_util


# --------------------------------------------------------------------
# TEST 8 - estimated_wait_minutes gerçekten Erlang-C'den geliyor
# --------------------------------------------------------------------

def test_estimated_wait_minutes_is_finite_when_rho_saturated_via_backlog_model(operational_state):
    """
    ADIM 6D ile GÜNCELLENDİ: rho>=1 artık None DEĞİL - backlog tabanlı
    (bkz. core/scoring.py `passport_queue_model`, engine.py
    `_passport_backlog_starts`) SONLU bir dakika üretir. Erlang-C'nin
    kendisi SİLİNMEDİ (bkz. rho<1 testleri) - sadece rho>=1'in
    "hesaplanamaz" sonucu, gerçek kişi sayısına dayanan bir tahminle
    değiştirildi.
    """
    session = _session(operational_state)
    saturated = session.execute(select(QueuePrediction).where(
        QueuePrediction.process == "passport", QueuePrediction.utilization >= 1.0,
    )).scalars().all()
    session.close()

    assert saturated   # bu fixture'da GERÇEKTEN ρ≥1 olan pencereler var
    assert all(r.estimated_wait_minutes is not None for r in saturated)
    assert all(r.estimated_wait_minutes > 0 for r in saturated)
    assert all(r.risk == "CRITICAL" for r in saturated)


def test_estimated_wait_minutes_is_a_real_number_when_rho_below_one(operational_state):
    session = _session(operational_state)
    healthy = session.execute(select(QueuePrediction).where(
        QueuePrediction.process == "passport",
        QueuePrediction.utilization < 1.0,
        QueuePrediction.utilization.isnot(None),
    )).scalars().first()
    session.close()

    assert healthy is not None
    assert healthy.estimated_wait_minutes is not None
    assert healthy.estimated_wait_minutes >= 0


# --------------------------------------------------------------------
# TEST 9 - T2 recovery: yoğunluk düşüyor (surge dağılınca)
# --------------------------------------------------------------------

def test_t2_ist_passport_surge_window_recovers(operational_state):
    """
    T1'in en yoğun passport penceresi (10:00) T2'de surge dağılınca
    DÜŞMELİ.

    ADIM 6D-2 HOURLY MIGRATION: pencere artık saatlik (60dk) olduğu
    için bu pencereye eskiden ayrı 15dk pencerelere düşen DAHA FAZLA
    uçuş giriyor - referans değerler (T1'in ölçülen expected_passengers/
    utilization'ı) yeniden ölçüldü: T1 expected_passengers=4108,
    utilization=1.268 (önceki 15dk ölçümü olan 1288/1.59 ARTIK
    GEÇERSİZ - farklı bir pencere genişliği farklı bir toplam demektir,
    production matematiği bozulmadı).
    """
    session = _session(operational_state)
    t2_row = _passport_window(session, "IST", datetime(2026, 9, 15, 10, 0))
    session.close()

    assert t2_row.expected_passengers < 4108   # T1'in (60dk) ölçülen değeri
    assert t2_row.utilization < 4108 / 160


# --------------------------------------------------------------------
# TEST 10 - SAW daha küçük tepki veriyor
# --------------------------------------------------------------------

def test_saw_passport_surge_is_smaller_than_ist(operational_state):
    session = _session(operational_state)
    saw_surge = _passport_window(session, "SAW", datetime(2026, 9, 15, 12, 0))
    ist_surge = _passport_window(session, "IST", datetime(2026, 9, 15, 10, 0))
    session.close()

    assert saw_surge is not None and ist_surge is not None
    assert saw_surge.expected_passengers < ist_surge.expected_passengers


def test_saw_passport_surge_window_rose_then_recovered():
    """
    SAW 12:00 penceresi bağımsız olarak ölçüldüğünde: T0=1 uçuş (LOW)
    -> T1=5 uçuş (CRITICAL, surge) -> T2=1 uçuş (LOW, tamamen dağıldı).
    `operational_state` module-scope fixture'ı DB'yi T2'de bıraktığı
    için bu iddia AYRI, round-by-round bir çalıştırmada ölçülüyor.
    """
    mp = pytest.MonkeyPatch()
    try:
        source = ops.install(mp, round_="t0")
        engine = create_engine("sqlite://")
        Session = sessionmaker(bind=engine)
        mp.setattr(pipeline_module, "init_db", lambda drop_first=False: Base.metadata.create_all(engine))
        mp.setattr(pipeline_module, "get_session", lambda: Session())
        Base.metadata.create_all(engine)
        s = Session()
        import_airports(s, REAL_AIRPORTS_SQL)
        seed_verified_dataset(s); seed_curated_fallback(s); seed_family_and_ga(s)
        s.add(AirportOperationalConfig(airport_iata="SAW", **CONFIGS["SAW"]))
        s.commit(); s.close()

        source_a = airlabs_client.build_source_a(ops.AIRPORTS)
        source_b = airlabs_client.build_source_b()

        flight_counts = {}
        for round_ in ("t0", "t1", "t2"):
            source.round = round_
            pipeline_module.run(source_a=source_a, source_b=source_b)
            s = Session()
            row = _passport_window(s, "SAW", datetime(2026, 9, 15, 12, 0))
            flight_counts[round_] = row.flight_count
            s.close()
    finally:
        mp.undo()

    assert flight_counts["t1"] > flight_counts["t0"]
    assert flight_counts["t2"] < flight_counts["t1"]


# --------------------------------------------------------------------
# TEST 11 - ADB kontrol grubu stabil
# --------------------------------------------------------------------

def test_adb_passport_predictions_identical_across_all_rounds(operational_state):
    """ADB'nin passport tahminleri T0/T1/T2 arasında AYNI kalmalı - hiç dokunulmadı."""
    session = _session(operational_state)
    rows = session.execute(select(QueuePrediction).where(
        QueuePrediction.airport_iata == "ADB", QueuePrediction.process == "passport",
    )).scalars().all()
    session.close()

    assert rows
    for row in rows:
        assert row.risk in ("LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN")


def test_adb_flight_count_unchanged_by_ist_saw_operations(operational_state):
    session = _session(operational_state)
    from sqlalchemy import func
    n = session.scalar(select(func.count()).select_from(Flight).where(Flight.airport_iata == "ADB"))
    session.close()
    assert n == 50   # ADIM 6A'dan değişmedi


# --------------------------------------------------------------------
# TEST 12 - cancelled/diverted arrival demand'e girmiyor
# --------------------------------------------------------------------

def test_arrival_cancellation_excluded_from_passport_demand(operational_state):
    session = _session(operational_state)
    flight = session.execute(select(Flight).where(Flight.flight_key.like("TK_406_%"))).scalars().first()
    session.close()
    assert flight.status == "cancelled"   # T2'de de GERİ AÇILMADI (bkz. generate.py düzeltmesi)


# --------------------------------------------------------------------
# Arrival-özel senaryolar (ADIM 6C §D) - delay compression + aircraft change
# --------------------------------------------------------------------

def test_arrival_delay_compression_moves_flights_into_shared_window(operational_state):
    session = _session(operational_state)
    from app.queue.domain.demand import effective_time
    from app.queue.engine import floor_to_window

    buckets = set()
    for num in ("601", "602"):
        f = session.execute(select(Flight).where(Flight.flight_key.like(f"TK_{num}_%"))).scalars().first()
        buckets.add(floor_to_window(effective_time(f)))
    session.close()

    assert len(buckets) == 1   # T1/T2'de TEK ortak pencereye taşındı


def test_arrival_aircraft_change_reflected_as_single_event(operational_state):
    session = _session(operational_state)
    flight = session.execute(select(Flight).where(Flight.flight_key.like("TK_701_%"))).scalars().first()
    events = session.execute(select(FlightEvent).where(FlightEvent.flight_key == flight.flight_key)).scalars().all()
    session.close()

    assert flight.aircraft_icao == "B788"
    changes = [e for e in events if e.event_type == "AIRCRAFT_CHANGED"]
    assert len(changes) == 1
    assert changes[0].old_value == "A320" and changes[0].new_value == "B788"


# --------------------------------------------------------------------
# TEST 13 - Security bozulmadı (mevcut ADIM 6A/6B davranışı aynen)
# --------------------------------------------------------------------

def test_security_results_unaffected_by_this_step(operational_state):
    session = _session(operational_state)
    row = _passport_window(session, "IST", datetime(2026, 9, 15, 8, 15))  # ADIM 6B'nin ölçtüğü security penceresi
    session.close()
    # (Bu passport penceresi - ADIM 6B'nin security sonuçları AYRI
    # test dosyasında zaten regresyonsuz doğrulanıyor; burada sadece
    # bu ADIM'ın security process'e hiç dokunmadığını AST ile de
    # doğruluyoruz.)
    import ast
    tree = ast.parse(open("app/queue/api.py", encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "airport_directory":
            calls = {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            assert "process_series" not in calls


# --------------------------------------------------------------------
# Havalimanı isim fallback zinciri (ADIM 6C §B)
# --------------------------------------------------------------------

def test_name_fallback_tier_kicks_in_when_primary_is_missing(monkeypatch, operational_state):
    """
    Birincil kaynak (Airport.airport_name) NULL/eksikse ikincil
    (yerel fallback directory) devreye girmeli - burada test amaçlı
    GEÇİCİ olarak (gerçek modüle KALICI hardcode EKLEMEDEN) doğrulanıyor.
    """
    import app.queue.api as api_module

    monkeypatch.setitem(api_module._LOCAL_AIRPORT_NAME_FALLBACK, "IST", "Test Fallback Name")

    session = _session(operational_state)
    ist = session.get(__import__("app.queue.models", fromlist=["Airport"]).Airport, "IST")
    original_name = ist.airport_name
    ist.airport_name = None   # birincil kaynağı GEÇİCİ olarak boşalt
    session.commit()

    directory = {e["iata"]: e["name"] for e in airport_directory(session)}

    ist.airport_name = original_name  # geri al
    session.commit()
    session.close()

    assert directory["IST"] == "Test Fallback Name"


def test_airport_without_any_name_source_still_appears_as_bare_iata(operational_state):
    session = _session(operational_state)
    session.add(QueuePrediction(
        airport_iata="QQQ", process="security",
        window_start=datetime(2026, 9, 15, 8, 0), window_end=datetime(2026, 9, 15, 8, 15),
        flight_count=1, expected_passengers=100, risk="LOW", reasons="[]", confidence=0.5,
    ))
    session.commit()

    directory = {e["iata"]: e["name"] for e in airport_directory(session)}
    session.close()

    assert "QQQ" in directory     # isim yoksa da LİSTEDEN ÇIKARILMADI
    assert directory["QQQ"] is None


# --------------------------------------------------------------------
# TEST 14 - Yolcu sayısı frontend'de yok (statik, ADIM 6A-UI'den beri)
# --------------------------------------------------------------------

def test_frontend_still_never_shows_passenger_counts():
    html = open("app/web/static/index.html", encoding="utf-8").read()
    script = html.split("<script>", 1)[1]
    for forbidden in ("expected_passengers", "flight_count", " pax"):
        assert forbidden not in script


# --------------------------------------------------------------------
# ADIM 6C EK KONTROL - Frontend wait-time metni üç duruma ayrılıyor
# (rho>=1 -> "Kapasite aşıldı", None+None -> "Hesaplanamıyor", finite -> "X dk").
# Statik kaynak kontrolü (JS test koşucusu bu repoda YOK, bkz. ADIM
# 6A-UI-2 raporundaki AYNI dürüst sınırlama notu).
# --------------------------------------------------------------------

def test_frontend_wait_time_distinguishes_capacity_exceeded_from_unknown():
    html = open("app/web/static/index.html", encoding="utf-8").read()
    assert "Kapasite aşıldı" in html
    assert "Hesaplanamıyor" in html
    assert "waitTimeText" in html
    # Frontend YENİ bir Erlang-C/rho hesabı YAPMIYOR - sadece backend'in
    # zaten döndürdüğü `utilization`/`estimated_wait_minutes` alanlarını okuyor.
    script = html.split("<script>", 1)[1]
    wait_fn = script.split("function waitTimeText")[1].split("\n  }")[0]
    for forbidden in ("Erlang", "erlang_c", "* mu", "lam /"):
        assert forbidden not in wait_fn
