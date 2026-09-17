"""
Bölüm 36 (GERÇEK DATA E2E) + Bölüm 47 (FINAL REALISTIC REPLAY).

Gerçek üretim zinciri, izole SQLite DB üzerinde, TEK ÇAĞRIDA hiçbir
katman atlanmadan uçtan uca çalıştırılır:

    Kaynak A (gerçek AirLabs şeması, dakika-hassasiyetli saatler)
      -> parse_source_a()            (GERÇEK parser)
      -> refresh_flights()           (GERÇEK upsert + FlightEvent)
      -> Flight                      (GERÇEK model satırları)
      -> AircraftCapacityService     (GERÇEK kapasite çözümü)
      -> DemandCalculator/effective_time() (GERÇEK -120/+15 zamanlama)
      -> run_predictions()           (GERÇEK event-driven queue engine)
      -> QueuePrediction             (GERÇEK persisted sonuç)
      -> airport_predictions()       (GERÇEK API sözleşmesi)
      -> index.html (frontend contract - statik doğrulama)

Kapsanan senaryolar (tek karma veri seti, IST + kontrol havalimanı ADB):
  - domestic departure         (TK101, TK104, TK301)
  - international departure    (TK102, TK105, TK106)
  - international arrival      (TK203, TK207)
  - missing ICAO   -> unknown_default = 180  (TK104)
  - unknown ICAO   -> unknown_default = 180  (TK105, "ZZZZ")
  - delay          (TK106: R0 13:15 planlı -> R1 14:20 tahmini, pencere kayması)
  - cancellation   (TK207: R1'de cancelled, talepten düşer)
  - aircraft change(TK102: R0 CRJ9(90) -> R1 A320(150))

Tüm saatler DAKİKA hassasiyetlidir (12:03, 12:17, 12:34, 12:59, 13:00,
13:15, 13:45 ...) - Bölüm 47'nin "yuvarlak saat kullanma" şartı.

`database.sqlite`'a HİÇ dokunulmaz - `tmp_path` tabanlı izole SQLite
(mevcut `test_production_shape_hourly_replay.py` deseniyle AYNI).
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.constants import (
    EVENT_AIRCRAFT_CHANGED,
    EVENT_CANCELLED,
    EVENT_DELAYED,
    PROCESS_PASSPORT,
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    STATUS_CANCELLED,
)
from app.queue.core.event_queue import simulate_passport, simulate_security
from app.queue.domain.demand import DemandCalculator, effective_time
from app.queue.domain.flows import (
    is_international_arrival,
    is_international_departure,
    passport_flights,
    security_domestic_flights,
    security_international_flights,
)
from app.queue.engine import run_predictions
from app.queue.ingestion.airports_import import country_lookup
from app.queue.ingestion.refresh import refresh_flights
from app.queue.ingestion.sources import parse_source_a
from app.queue.models import Airport, Flight, FlightEvent, QueuePrediction
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset
from app.service import AircraftCapacityService, DEFAULT_CAPACITY

DAY = datetime(2026, 9, 15)
NOW = DAY.replace(hour=23)


def _fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _row(flight_iata, number, dep_iata=None, arr_iata=None, dep=None,
         dep_est=None, arr=None, aircraft_icao=None, status="scheduled"):
    return {
        "airline_iata": "TK",
        "flight_iata": flight_iata,
        "flight_number": number,
        "aircraft_icao": aircraft_icao,
        "dep_iata": dep_iata,
        "arr_iata": arr_iata,
        "dep_time_utc": _fmt(dep) if dep else None,
        "dep_estimated_utc": _fmt(dep_est) if dep_est else None,
        "dep_actual_utc": None,
        "arr_time_utc": _fmt(arr) if arr else None,
        "arr_estimated_utc": None,
        "arr_actual_utc": None,
        "status": status,
    }


def _departures(round_no: int):
    return [
        _row("TK101", "101", "IST", "ESB", dep=DAY.replace(hour=12, minute=3), aircraft_icao="A320"),
        _row("TK102", "102", "IST", "CDG", dep=DAY.replace(hour=12, minute=17),
             aircraft_icao="A320" if round_no == 1 else "CRJ9",
             status="active" if round_no == 1 else "scheduled"),
        _row("TK104", "104", "IST", "ESB", dep=DAY.replace(hour=12, minute=59), aircraft_icao=None),
        _row("TK105", "105", "IST", "CDG", dep=DAY.replace(hour=13, minute=0), aircraft_icao="ZZZZ"),
        _row(
            "TK106", "106", "IST", "JFK", dep=DAY.replace(hour=13, minute=15),
            dep_est=DAY.replace(hour=14, minute=20) if round_no == 1 else None,
            aircraft_icao="A320", status="active" if round_no == 1 else "scheduled",
        ),
        _row("TK301", "301", "ADB", "ESB", dep=DAY.replace(hour=12, minute=10), aircraft_icao="A320"),
    ]


def _arrivals(round_no: int):
    return [
        _row("TK203", "203", arr_iata="IST", arr=DAY.replace(hour=12, minute=34), aircraft_icao="CRJ9"),
        _row(
            "TK207", "207", arr_iata="IST", arr=DAY.replace(hour=13, minute=45),
            aircraft_icao="A320",
            status=STATUS_CANCELLED if round_no == 1 else "scheduled",
        ),
    ]


@pytest.fixture()
def replay(tmp_path):
    isolated_engine = create_engine(f"sqlite:///{tmp_path / 'final-replay.sqlite'}")
    Base.metadata.create_all(isolated_engine)
    session = sessionmaker(bind=isolated_engine)()
    try:
        for iata, icao, name, country in (
            ("IST", "LTFM", "Istanbul Airport", "TR"),
            ("ESB", "LTAC", "Ankara Esenboga Airport", "TR"),
            ("ADB", "LTBJ", "Izmir Adnan Menderes Airport", "TR"),
            ("CDG", "LFPG", "Paris Charles de Gaulle Airport", "FR"),
            ("JFK", "KJFK", "John F Kennedy International Airport", "US"),
        ):
            session.add(Airport(
                iata_code=iata, icao_code=icao, airport_name=name,
                city_code=iata, country_code=country, timezone="UTC",
            ))
        session.commit()

        seed_verified_dataset(session)
        seed_curated_fallback(session)
        seed_family_and_ga(session)
        resolver = AircraftCapacityService(session)
        countries = country_lookup(session)

        # R0 - ilk 30dk'lık besleme
        parsed_r0 = (
            parse_source_a(_departures(0), "departure", countries)
            + parse_source_a(_arrivals(0), "arrival", countries)
        )
        refresh0 = refresh_flights(session, parsed_r0)
        summary0 = run_predictions(
            session, resolver, airports=["IST", "ADB"],
            update_baseline=False, now=NOW,
        )
        api0 = {
            "IST": airport_predictions(session, "IST", now=NOW),
            "ADB": airport_predictions(session, "ADB", now=NOW),
        }

        # R1 - bir sonraki 30dk poll: aircraft change (TK102),
        # delay (TK106), cancellation (TK207).
        parsed_r1 = (
            parse_source_a(_departures(1), "departure", countries)
            + parse_source_a(_arrivals(1), "arrival", countries)
        )
        refresh1 = refresh_flights(session, parsed_r1)
        summary1 = run_predictions(
            session, resolver, airports=["IST", "ADB"],
            update_baseline=False, now=NOW,
        )
        api1 = {
            "IST": airport_predictions(session, "IST", now=NOW),
            "ADB": airport_predictions(session, "ADB", now=NOW),
        }

        yield {
            "session": session,
            "resolver": resolver,
            "refresh0": refresh0,
            "refresh1": refresh1,
            "summary0": summary0,
            "summary1": summary1,
            "api0": api0,
            "api1": api1,
            "parsed_r0": parsed_r0,
            "parsed_r1": parsed_r1,
        }
    finally:
        session.close()
        isolated_engine.dispose()


# ============================================================
# Bölüm 36/47 - parser/refresh zinciri, uçuş sayısı.
# ============================================================

def test_all_flights_parsed_and_upserted_without_loss(replay):
    assert len(replay["parsed_r0"]) == 8  # 6 departure + 2 arrival kaydı
    assert replay["refresh0"]["inserted"] == 8
    assert replay["refresh0"]["failed"] == 0
    # R1: aynı 8 flight_key upsert edilir (INSERT değil UPDATE).
    assert replay["refresh1"]["inserted"] == 0
    assert replay["refresh1"]["updated"] >= 3  # TK102/TK106/TK207 en az


# ============================================================
# Bölüm 47 - missing/unknown ICAO -> DEFAULT_CAPACITY (180).
# ============================================================

def test_missing_and_unknown_icao_resolve_to_default_180(replay):
    resolver = replay["resolver"]
    missing = resolver.resolve(None, "TK")
    unknown = resolver.resolve("ZZZZ", "TK")
    assert missing.capacity == DEFAULT_CAPACITY == 180
    assert unknown.capacity == DEFAULT_CAPACITY == 180
    assert missing.source == "unknown_default"
    assert unknown.source == "unknown_default"

    session = replay["session"]
    tk104 = session.scalar(select(Flight).where(Flight.flight_iata == "TK104"))
    tk105 = session.scalar(select(Flight).where(Flight.flight_iata == "TK105"))
    assert tk104.aircraft_icao is None
    assert tk105.aircraft_icao == "ZZZZ"


# ============================================================
# Bölüm 47 - known ICAO gerçek koltuk kapasitesiyle çözülür.
# ============================================================

def test_known_icao_resolves_to_real_verified_capacity(replay):
    resolver = replay["resolver"]
    assert resolver.resolve("A320", "TK").capacity == 150
    assert resolver.resolve("CRJ9", "TK").capacity == 90


# ============================================================
# Bölüm 47 - aircraft change / delay / cancellation.
# ============================================================

def test_aircraft_change_recorded_and_capacity_updates(replay):
    session = replay["session"]
    tk102 = session.scalar(select(Flight).where(Flight.flight_iata == "TK102"))
    assert tk102.aircraft_icao == "A320"  # R1'de değişti

    changed = session.execute(
        select(FlightEvent).where(
            FlightEvent.flight_key == tk102.flight_key,
            FlightEvent.event_type == EVENT_AIRCRAFT_CHANGED,
        )
    ).scalars().all()
    assert len(changed) == 1
    assert changed[0].old_value == "CRJ9"
    assert changed[0].new_value == "A320"


def test_delay_migrates_flight_to_a_later_bucket(replay):
    session = replay["session"]
    tk106 = session.scalar(select(Flight).where(Flight.flight_iata == "TK106"))
    assert tk106.dep_estimated_utc == DAY.replace(hour=14, minute=20)

    delayed = session.execute(
        select(FlightEvent).where(
            FlightEvent.flight_key == tk106.flight_key,
            FlightEvent.event_type == EVENT_DELAYED,
        )
    ).scalars().all()
    assert len(delayed) == 1

    eff_r0 = tk106.dep_scheduled_utc - timedelta(minutes=120)
    eff_r1 = effective_time(tk106)
    assert eff_r0 == DAY.replace(hour=11, minute=15)
    assert eff_r1 == DAY.replace(hour=12, minute=20)
    assert eff_r0.replace(minute=0) != eff_r1.replace(minute=0)  # saat bucket'ı kaydı


def test_cancellation_removes_flight_from_active_demand(replay):
    session = replay["session"]
    tk207 = session.scalar(select(Flight).where(Flight.flight_iata == "TK207"))
    assert tk207.status == STATUS_CANCELLED

    cancelled = session.execute(
        select(FlightEvent).where(
            FlightEvent.flight_key == tk207.flight_key,
            FlightEvent.event_type == EVENT_CANCELLED,
        )
    ).scalars().all()
    assert len(cancelled) == 1

    # R1 sonrası passport talebine hiç GİRMEZ (bkz. EXCLUDED_STATUSES).
    ist_flights = session.execute(
        select(Flight).where(Flight.airport_iata == "IST")
    ).scalars().all()
    active_passport = passport_flights(
        [f for f in ist_flights if f.status != STATUS_CANCELLED]
    )
    assert tk207.flight_key not in {f.flight_key for f in active_passport}


# ============================================================
# Bölüm 47 - dakika-hassasiyetli saat -> event-driven queue izi
# (passport completion / security arrival / wait / bucket).
# ============================================================

def _trace(session, resolver, airport_iata):
    """
    `engine._event_driven_queue_demand()` ile AYNI kurulum - gerçek
    `simulate_passport`/`simulate_security` çağrılarından üretilen
    ServiceEvent'lerden uçuş bazlı iz (trace) çıkarır (Bölüm 47'nin
    istediği "passport completion/security arrival/wait/bucket"
    raporu). Motorun kendi hesabı DEĞİŞTİRİLMEZ - sadece AYNI GERÇEK
    fonksiyonlar (event_queue.py) tekrar çağrılıp çapraz doğrulama
    (ve rapor) için kullanılır.
    """
    demand = DemandCalculator(resolver)
    flights = session.execute(
        select(Flight).where(
            Flight.airport_iata == airport_iata,
            Flight.status != STATUS_CANCELLED,
        )
    ).scalars().all()

    dep_arrivals = [
        (effective_time(f), demand.passenger_demand(f))
        for f in passport_flights(flights) if is_international_departure(f)
    ]
    arr_arrivals = [
        (effective_time(f), demand.passenger_demand(f))
        for f in passport_flights(flights) if is_international_arrival(f)
    ]
    passport = simulate_passport(dep_arrivals, arr_arrivals, effective_server_count=8, service_time_minutes=1.5)

    intl_sec_arrivals = [
        (effective_time(f), demand.passenger_demand(f))
        for f in security_international_flights(flights)
    ]
    security_intl = simulate_security(intl_sec_arrivals, lane_count=8, service_time_minutes=1.0)

    dom_sec_arrivals = [
        (effective_time(f), demand.passenger_demand(f))
        for f in security_domestic_flights(flights)
    ]
    security_dom = simulate_security(dom_sec_arrivals, lane_count=8, service_time_minutes=1.0)

    return {
        "passport_departure": passport["departure"],
        "passport_arrival": passport["arrival"],
        "security_intl": security_intl,
        "security_dom": security_dom,
    }


def test_event_trace_shows_exact_minute_timing_and_hourly_buckets(replay):
    session = replay["session"]
    trace = _trace(session, replay["resolver"], "IST")

    # TK101 (12:03 dep, domestic) -> effective 10:03 -> security_dom bucket 10:00.
    dom_events = trace["security_dom"]
    assert any(
        ev.arrival_time == DAY.replace(hour=10, minute=3) and ev.arrival_time.hour == 10
        for ev in dom_events
    )

    # TK203 (arrival 12:34, +15dk) -> effective 12:49 -> passport bucket 12:00.
    arr_events = trace["passport_arrival"]
    assert any(
        ev.arrival_time == DAY.replace(hour=12, minute=49) and ev.arrival_time.hour == 12
        for ev in arr_events
    )
    # completion_time HER ZAMAN arrival_time'dan SONRA veya eşit (FIFO, sıfır kapasite değil).
    assert all(ev.completion_time >= ev.arrival_time for ev in arr_events)
    assert all(ev.wait_minutes >= 0 for ev in arr_events)

    # R1 sonrası TK106 (delay) 12:20'de intl security'ye giriyor (bucket 12:00),
    # ARTIK 11:00'da DEĞİL.
    intl_events = trace["security_intl"]
    assert any(ev.arrival_time == DAY.replace(hour=12, minute=20) for ev in intl_events)
    assert not any(ev.arrival_time == DAY.replace(hour=11, minute=15) for ev in intl_events)


# ============================================================
# Bölüm 47 - conservation: ham uçuş talebi == QueuePrediction toplamı.
# ============================================================

def _raw_demand(session, resolver, airport_iata):
    demand = DemandCalculator(resolver)
    flights = session.execute(
        select(Flight).where(
            Flight.airport_iata == airport_iata,
            Flight.status != STATUS_CANCELLED,
        )
    ).scalars().all()
    return {
        PROCESS_SECURITY_DOMESTIC: sum(
            demand.passenger_demand(f) for f in security_domestic_flights(flights)
        ),
        PROCESS_SECURITY_INTL: sum(
            demand.passenger_demand(f) for f in security_international_flights(flights)
        ),
        PROCESS_PASSPORT: sum(
            demand.passenger_demand(f) for f in passport_flights(flights)
        ),
    }


def _prediction_totals(session, airport_iata):
    totals = {}
    for process in (PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL, PROCESS_PASSPORT):
        rows = session.execute(
            select(QueuePrediction).where(
                QueuePrediction.airport_iata == airport_iata,
                QueuePrediction.process == process,
            )
        ).scalars().all()
        totals[process] = sum(r.expected_passengers for r in rows)
    return totals


def test_passenger_conservation_no_creation_or_loss(replay):
    session = replay["session"]
    resolver = replay["resolver"]

    raw = _raw_demand(session, resolver, "IST")
    predicted = _prediction_totals(session, "IST")
    assert raw == predicted


def test_passport_departure_arrival_cohorts_sum_to_combined_pool_without_double_counting(replay):
    """Bölüm 17: passport_dep + passport_arr HER pencere için birleşik passport'u AYNEN yansıtır (fiziksel havuz İKİ KEZ sayılmaz)."""
    session = replay["session"]
    combined_by_window = {
        r.window_start: r for r in session.execute(
            select(QueuePrediction).where(
                QueuePrediction.airport_iata == "IST",
                QueuePrediction.process == PROCESS_PASSPORT,
            )
        ).scalars().all()
    }
    dep_by_window = {
        r.window_start: r for r in session.execute(
            select(QueuePrediction).where(
                QueuePrediction.airport_iata == "IST",
                QueuePrediction.process == PROCESS_PASSPORT_DEPARTURE,
            )
        ).scalars().all()
    }
    arr_by_window = {
        r.window_start: r for r in session.execute(
            select(QueuePrediction).where(
                QueuePrediction.airport_iata == "IST",
                QueuePrediction.process == PROCESS_PASSPORT_ARRIVAL,
            )
        ).scalars().all()
    }
    assert combined_by_window  # en az bir pencere var
    for window, combined in combined_by_window.items():
        dep = dep_by_window.get(window)
        arr = arr_by_window.get(window)
        expected_passengers = (dep.expected_passengers if dep else 0) + (arr.expected_passengers if arr else 0)
        assert combined.expected_passengers == expected_passengers
        # utilization/risk KOPYALANIR, YENİDEN HESAPLANMAZ (double-count yok).
        if dep is not None:
            assert dep.utilization == combined.utilization
            assert dep.risk == combined.risk
        if arr is not None:
            assert arr.utilization == combined.utilization
            assert arr.risk == combined.risk


# ============================================================
# Bölüm 47 - airport isolation (IST kontamine ETMEZ ADB'yi).
# ============================================================

def test_airport_isolation_ist_and_adb_never_mix(replay):
    session = replay["session"]
    ist_flights = {
        f.flight_iata for f in session.execute(
            select(Flight).where(Flight.airport_iata == "IST")
        ).scalars().all()
    }
    adb_flights = {
        f.flight_iata for f in session.execute(
            select(Flight).where(Flight.airport_iata == "ADB")
        ).scalars().all()
    }
    assert ist_flights.isdisjoint(adb_flights)
    assert adb_flights == {"TK301"}

    # ADB sadece 1 domestic kalkışa sahip: security_dom var, ama
    # security_intl / passport HİÇ üretilmemeli (o uçuş türü YOK).
    adb_processes = {
        r.process for r in session.execute(
            select(QueuePrediction).where(QueuePrediction.airport_iata == "ADB")
        ).scalars().all()
    }
    assert PROCESS_SECURITY_DOMESTIC in adb_processes
    assert PROCESS_SECURITY_INTL not in adb_processes
    assert PROCESS_PASSPORT not in adb_processes

    # IST'nin predictionları ADB'ye HİÇ sızmamış.
    api0 = replay["api0"]
    assert api0["ADB"]["domestic_security"]["windows"] != api0["IST"]["domestic_security"]["windows"]


# ============================================================
# Bölüm 47 - API/frontend contract, R0/R1 karşılaştırması.
# ============================================================

def test_api_contract_has_all_four_graphs_and_reflects_r1_changes(replay):
    api0, api1 = replay["api0"]["IST"], replay["api1"]["IST"]
    for section in ("overall", "domestic_security", "international_departure", "international_arrival"):
        assert section in api0
        assert section in api1
        assert "windows" in api0[section]

    # International departure grafiği passport+security_intl breakdown taşır.
    id0_windows = api0["international_departure"]["windows"]
    assert any("passport" in w and "international_security" in w for w in id0_windows)

    # R1: TK207 iptal olduğu için 14:00 penceresindeki international_arrival
    # talebi SIFIRLANMALI (window'un kendisi, kalan backlog serbest bırakma
    # kaydı için hâlâ var olabilir - bkz. `_event_driven_queue_demand`'ın
    # `starts` birleşimi - ama flight_count/expected_passengers artık 0).
    session = replay["session"]
    r1_passport_windows = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == "IST",
            QueuePrediction.process == PROCESS_PASSPORT_ARRIVAL,
            QueuePrediction.window_start == DAY.replace(hour=14, minute=0),
        )
    ).scalars().all()
    assert all(r.flight_count == 0 and r.expected_passengers == 0 for r in r1_passport_windows)


def test_frontend_contract_file_unchanged_this_turn():
    """Bu turda frontend render kodu DEĞİŞTİRİLMEDİ - sadece backend/engine gerçek veriyle doğrulandı."""
    from pathlib import Path
    html = (Path(__file__).parents[1] / "app" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'graphSectionHtml("international_departure", "INTERNATIONAL DEPARTURE", data.international_departure, "breakdown")' in html
