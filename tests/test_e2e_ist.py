"""
AŞAMA 9 - E2E: Istanbul Airport (IST / LTFM) gerçekçi senaryosu.

Gerçek airport kimliği (IST/LTFM/Istanbul Airport/TR/Europe-Istanbul)
kullanılır - flight_airports.sql'den gelen GERÇEK değerlerdir, uydurma
değildir. Uçuş verisi ise TEST fixture'ıdır (uçuş numaraları, saatler,
uçak tipleri) - gerçek canlı uçuş iddiası taşımaz.

Hiçbir test production database.sqlite dosyasına dokunmaz; hepsi
bellek içi (`sqlite://`) izole oturumlar kullanır.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.baseline import get_baseline
from app.queue.config import AirportConfigView
from app.queue.constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PASSPORT_RELEASE_BUFFER_MINUTES,
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
)
from app.queue.core.erlang import erlang_c_wait_time
from app.queue.core.scoring import (
    passport_effective_service_rate,
    passport_queue_model,
    security_density_score,
)
from app.queue.domain.demand import DemandCalculator, effective_time
from app.queue.domain.flows import passport_flights, security_flights
from app.queue.engine import (
    floor_to_window,
    predict_airport,
    predict_window,
    run_predictions,
    window_starts,
)
from app.queue.ingestion.airports_import import (
    country_lookup,
    import_airports,
    parse_airports_sql,
)
from app.queue.ingestion.refresh import (
    aircraft_changes_for_airport,
    refresh_flights,
)
from app.queue.ingestion.sources import build_aircraft_index, parse_source_a
from app.queue.models import (
    Airport,
    AirportOperationalConfig,
    Flight,
    FlightEvent,
    HistoricalFlightCount,
    QueuePrediction,
)
from app.queue.reasons.detector import DetectedReason, detect_reasons
from app.queue.reporting import hourly_report

from .factories import MockCapacityResolver, MockFlight

IST = "IST"
REAL_SQL_PATH = "data/flight_airports.sql"


# ----------------------------------------------------------------------
# Ortak fixture'lar
# ----------------------------------------------------------------------

@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def ist_session(session):
    """
    IST'in GERÇEK kimliğini (IATA/ICAO/isim/ülke) taşıyan tek satırlık
    Airport kaydı - flight_airports.sql'deki gerçek değerlerle birebir.
    Tüm dosyayı her testte parse etmemek için doğrudan ekleniyor;
    Bölüm 14 (Full Pipeline) gerçek dosyayı uçtan uca parse eder.
    """
    session.add(Airport(
        iata_code="IST", icao_code="LTFM", airport_name="Istanbul Airport",
        city_code="IST", country_code="TR", timezone="Europe/Istanbul",
    ))
    session.commit()
    return session


def roomy_config(airport=IST) -> AirportConfigView:
    return AirportConfigView(
        airport_iata=airport,
        passport_counter_count=24,
        passport_staff_count=24,
        passport_service_time_minutes=1.5,
        security_lane_count=8,
        security_service_time_minutes=1.0,
        passport_staff_per_counter=2.0,
        passport_service_rate_per_staff=0.5,
        passport_efficiency_multiplier=1.5,
        arrival_bank_threshold=5,
        is_default=False,
    )


def spec_passport_config(airport=IST) -> AirportConfigView:
    """AŞAMA 9 madde 5'te verilen TAM değerler."""
    return AirportConfigView(
        airport_iata=airport,
        passport_counter_count=4,
        passport_staff_count=99,          # kasıtlı tutarsız - mu'yu etkilememeli
        passport_service_time_minutes=1.5,
        security_lane_count=8,
        security_service_time_minutes=1.0,
        passport_staff_per_counter=2,
        passport_service_rate_per_staff=1.5,
        passport_efficiency_multiplier=1.0,
        arrival_bank_threshold=5,
        is_default=False,
    )


def window_of(f) -> datetime:
    """Bir uçuşun düştüğü pencerenin başlangıcı (effective_time üzerinden)."""
    return floor_to_window(effective_time(f))


def flight(
    direction, hour, minute=0, *, aircraft="A320", location=LOCATION_INTERNATIONAL,
    duration_minutes=90, key=None, number="1", airline="TK",
    status="scheduled", dep_estimated=None, dep_actual=None,
    arr_estimated=None, arr_actual=None, airport=IST,
) -> MockFlight:
    """IST'e bağlı, tüm zaman alanlarını kontrol edebilen esnek fixture."""
    base = datetime(2026, 9, 15, hour, minute)
    if direction == DIRECTION_DEPARTURE:
        dep_scheduled = base
        arr_scheduled = base + timedelta(minutes=duration_minutes)
        dep_iata, arr_iata = airport, "ZZZ"
    else:
        arr_scheduled = base
        dep_scheduled = base - timedelta(minutes=duration_minutes)
        dep_iata, arr_iata = "ZZZ", airport

    return MockFlight(
        flight_key=key or f"{airline}_{number}_{hour:02d}{minute:02d}{direction[0]}",
        airport_iata=airport,
        direction=direction,
        location=location,
        airline_iata=airline,
        flight_number=number,
        flight_iata=f"{airline}{number}",
        aircraft_icao=aircraft,
        aircraft_match_found=aircraft is not None,
        dep_iata=dep_iata,
        arr_iata=arr_iata,
        dep_scheduled_utc=dep_scheduled,
        dep_estimated_utc=dep_estimated,
        dep_actual_utc=dep_actual,
        arr_scheduled_utc=arr_scheduled,
        arr_estimated_utc=arr_estimated,
        arr_actual_utc=arr_actual,
        status=status,
    )


# ========================================================================
# 1. TEST UÇUŞLARI - hem departure hem arrival, farklı aircraft tipleri
# ========================================================================

def test_ist_test_flights_cover_departure_arrival_and_multiple_aircraft():
    fleet = ["A320", "A321", "B77W", "B738"]
    flights = (
        [flight(DIRECTION_DEPARTURE, 10, i * 3, aircraft=a, key=f"D{a}")
         for i, a in enumerate(fleet)]
        + [flight(DIRECTION_ARRIVAL, 10, i * 3, aircraft=a, key=f"A{a}")
           for i, a in enumerate(fleet)]
    )
    demand = DemandCalculator(MockCapacityResolver())

    assert {f.direction for f in flights} == {DIRECTION_DEPARTURE, DIRECTION_ARRIVAL}
    assert {f.aircraft_icao for f in flights} == set(fleet)
    assert all(f.airport_iata == IST for f in flights)

    # Kapasite MockCapacityResolver (AircraftCapacityService arayüzü) üzerinden.
    capacities = {f.aircraft_icao: demand.seat_capacity(f) for f in flights}
    assert capacities == {"A320": 180, "A321": 220, "B77W": 350, "B738": 189}


def test_ist_airport_identity_matches_real_source(ist_session):
    row = ist_session.get(Airport, "IST")
    assert row.icao_code == "LTFM"
    assert row.airport_name == "Istanbul Airport"
    assert row.country_code == "TR"
    assert row.timezone == "Europe/Istanbul"


# ========================================================================
# 2. SAATLİK (60 DK) WINDOW - boundary (ADIM 6D-2 HOURLY MIGRATION)
# ========================================================================

@pytest.mark.parametrize(
    "moment,expected_window",
    [
        # 07:59 -> önceki saat [07:00-08:00)
        (datetime(2026, 9, 15, 7, 59, 0), datetime(2026, 9, 15, 7, 0)),
        # 08:00 -> [08:00-09:00) tam sınırda
        (datetime(2026, 9, 15, 8, 0, 0), datetime(2026, 9, 15, 8, 0)),
        (datetime(2026, 9, 15, 8, 30, 0), datetime(2026, 9, 15, 8, 0)),
        # 08:59 -> hâlâ [08:00-09:00)
        (datetime(2026, 9, 15, 8, 59, 59), datetime(2026, 9, 15, 8, 0)),
        # 09:00 -> [09:00-10:00) yeni saate geçti
        (datetime(2026, 9, 15, 9, 0, 0), datetime(2026, 9, 15, 9, 0)),
    ],
)
def test_window_boundary_exact_timestamps(moment, expected_window):
    assert floor_to_window(moment) == expected_window


def test_window_boundary_flight_assignment_via_effective_time():
    """
    Varış uçuşları: effective_time = arr_scheduled + 15 dk (sabit
    buffer). arr_scheduled'ı window_end - 15dk seçerek effective_time'ı
    TAM sınıra oturtuyoruz.
    """
    window_end = datetime(2026, 9, 15, 10, 15)
    just_inside = flight(
        DIRECTION_ARRIVAL, 0, 0, key="JI",
        arr_estimated=None,
    )
    just_inside.arr_scheduled_utc = window_end - timedelta(
        minutes=PASSPORT_RELEASE_BUFFER_MINUTES, seconds=1
    )
    just_outside = flight(DIRECTION_ARRIVAL, 0, 0, key="JO")
    just_outside.arr_scheduled_utc = window_end - timedelta(
        minutes=PASSPORT_RELEASE_BUFFER_MINUTES
    )

    assert effective_time(just_inside) == datetime(2026, 9, 15, 10, 14, 59)
    assert effective_time(just_outside) == window_end

    from app.queue.domain.demand import flights_in_window
    window_start = datetime(2026, 9, 15, 10, 0)
    in_window = flights_in_window([just_inside, just_outside], window_start, 15)

    assert just_inside in in_window
    assert just_outside not in in_window   # 10:15:00 -> SONRAKİ pencere


# ========================================================================
# 3. DELAY / EFFECTIVE TIME
# ========================================================================

def test_effective_time_uses_estimated_when_no_actual():
    f = flight(DIRECTION_DEPARTURE, 10, 0, key="E1")
    f.dep_estimated_utc = datetime(2026, 9, 15, 10, 20)
    f.dep_actual_utc = None

    # ADIM (Airport Queue Model V2 - sabit -120dk offset): departure
    # passenger arrival artık süreye bağlı dinamik buffer DEĞİL, sabit
    # 120 dakikadır (DEPARTURE_PASSENGER_ARRIVAL_OFFSET_MINUTES).
    assert effective_time(f) == datetime(2026, 9, 15, 10, 20) - timedelta(minutes=120)


def test_effective_time_uses_actual_over_estimated():
    f = flight(DIRECTION_DEPARTURE, 10, 0, key="E2")
    f.dep_estimated_utc = datetime(2026, 9, 15, 10, 20)
    f.dep_actual_utc = datetime(2026, 9, 15, 10, 25)

    assert effective_time(f) == datetime(2026, 9, 15, 10, 25) - timedelta(minutes=120)


def test_flight_key_stable_across_scheduled_estimated_actual_variants(session):
    """Aynı flight_key, scheduled sabit kaldıkça estimated/actual eklense de değişmez."""
    def row(**overrides):
        base = {
            "flight_key": "TK_55_2026-09-15", "airport_iata": IST,
            "direction": "departure", "location": "international",
            "airline_iata": "TK", "flight_number": "55", "flight_iata": "TK55",
            "aircraft_icao": "A320", "aircraft_match_found": True,
            "dep_iata": IST, "arr_iata": "CDG",
            "dep_scheduled_utc": datetime(2026, 9, 15, 10, 0),
            "dep_estimated_utc": None, "dep_actual_utc": None,
            "arr_scheduled_utc": datetime(2026, 9, 15, 12, 0),
            "arr_estimated_utc": None, "arr_actual_utc": None,
            "dep_terminal": None, "dep_gate": None,
            "arr_terminal": None, "arr_gate": None, "status": "scheduled",
        }
        base.update(overrides)
        return base

    refresh_flights(session, [row()])
    refresh_flights(session, [row(dep_estimated_utc=datetime(2026, 9, 15, 10, 20))])
    refresh_flights(session, [row(
        dep_estimated_utc=datetime(2026, 9, 15, 10, 20),
        dep_actual_utc=datetime(2026, 9, 15, 10, 25),
    )])

    assert session.scalar(select(func.count()).select_from(Flight)) == 1
    stored = session.execute(
        select(Flight).where(Flight.flight_key == "TK_55_2026-09-15")
    ).scalar_one()
    assert stored.dep_actual_utc == datetime(2026, 9, 15, 10, 25)


# ========================================================================
# 4. SECURITY E2E - aynı flight count, farklı aircraft -> farklı risk
# ========================================================================

def test_security_e2e_4x_a320_vs_4x_b77w_same_count_different_demand():
    calc = DemandCalculator(MockCapacityResolver())

    a320_flights = [
        flight(DIRECTION_DEPARTURE, 10, i * 3, aircraft="A320",
               location=LOCATION_DOMESTIC, key=f"S320-{i}", number=str(i))
        for i in range(4)
    ]
    b77w_flights = [
        flight(DIRECTION_DEPARTURE, 10, i * 3, aircraft="B77W",
               location=LOCATION_DOMESTIC, key=f"S77W-{i}", number=str(i))
        for i in range(4)
    ]
    a320_demand = sum(calc.passenger_demand(f) for f in a320_flights)

    a320_score = security_density_score(
        a320_flights, 4, calc.passenger_demand,
        historical_passenger_baseline=a320_demand,
    )
    b77w_score = security_density_score(
        b77w_flights, 4, calc.passenger_demand,
        historical_passenger_baseline=a320_demand,
    )

    assert a320_score["flight_count"] == b77w_score["flight_count"] == 4
    assert a320_score["flight_ratio"] == b77w_score["flight_ratio"] == 1.0
    assert b77w_score["expected_passengers"] > a320_score["expected_passengers"]
    assert b77w_score["passenger_ratio"] > a320_score["passenger_ratio"]
    assert b77w_score["baseline_ratio"] > a320_score["baseline_ratio"]
    assert b77w_score["risk"] != a320_score["risk"]

    # Security'de kesinlikle: Erlang-C yok, wait yok, utilization yok.
    assert a320_score["estimated_wait_minutes"] is None
    assert b77w_score["estimated_wait_minutes"] is None
    assert "utilization" not in a320_score


def test_security_never_touches_passport_machinery(monkeypatch):
    monkeypatch.setattr(
        "app.queue.core.scoring.erlang_c_wait_time",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Erlang-C çağrıldı!")),
    )
    monkeypatch.setattr(
        "app.queue.core.scoring.passport_effective_service_rate",
        lambda cfg: (_ for _ in ()).throw(AssertionError("passport service rate çağrıldı!")),
    )
    calc = DemandCalculator(MockCapacityResolver())
    flights = [flight(DIRECTION_DEPARTURE, 10, 0, location=LOCATION_DOMESTIC)]
    score = security_density_score(flights, 1, calc.passenger_demand, 100)
    assert score["estimated_wait_minutes"] is None


# ========================================================================
# 5. PASSPORT E2E - verilen tam sayısal değerler
# ========================================================================

def test_passport_numeric_mu_and_total_capacity_match_spec():
    config = spec_passport_config()
    mu = passport_effective_service_rate(config)
    assert mu == pytest.approx(2 / 3)                     # 1 / 1.5 dk
    assert config.passport_counter_count * mu == pytest.approx(8 / 3)
    assert config.passport_counter_count * mu * 60 == pytest.approx(160.0)


def test_passport_staff_count_does_not_double_boost_capacity():
    baseline = spec_passport_config()
    inflated = spec_passport_config()
    inflated.passport_staff_count = 999999   # sadece staff_count değişti

    assert passport_effective_service_rate(baseline) == passport_effective_service_rate(inflated)


def test_passport_e2e_prediction_reflects_spec_capacity():
    """
    12 yolcu/dk kapasiteyle, 15 dk pencerede kapasite = 180 yolcu -
    bu AŞAMA 9 dokümanındaki YAZILI örnek senaryodur (window_minutes=15
    AÇIKÇA sabitlenir, ADIM 6D-2'nin production varsayılanı olan 60'tan
    BAĞIMSIZ - bkz. test_core_math.py'deki aynı desen).
    lambda kapasitenin altında kalacak şekilde uçuş kur, rho ve wait'in
    Erlang-C ile TUTARLI olduğunu doğrula.

    ADIM (ICAO Demand Kalibrasyonu): passenger_demand artık load factor
    UYGULAMADAN ham ICAO kapasitesini kullanıyor - A320 (180 koltuk)
    bu spec config'inin kapasitesine (12/dk * 15dk = 180) TAM sınırda
    (rho=1.0) denk gelirdi; "lambda kapasitenin ALTINDA" senaryosunu
    korumak için daha küçük kapasiteli E190 (100 koltuk) kullanıldı.
    """
    calc = DemandCalculator(MockCapacityResolver())
    flights = [
        flight(DIRECTION_DEPARTURE, 10, 0, aircraft="E190",
               location=LOCATION_INTERNATIONAL, key="P1", number="1"),
    ]
    result = passport_queue_model(
        flights, spec_passport_config(), calc.passenger_demand, window_minutes=60,
    )

    demand = calc.passenger_demand(flights[0])
    lam = demand / 60
    # ADIM (4x2 efektif server modeli): c=4 gişe x 2 görevli/gişe=8,
    # capacity_rate=16/3/dk (320/saat).
    expected_rho = lam / (16 / 3)
    expected_wait = erlang_c_wait_time(8, lam, 2 / 3)

    assert result["arrival_rate"] == pytest.approx(lam, abs=1e-3)
    assert result["utilization"] == pytest.approx(expected_rho, abs=1e-3)
    assert result["estimated_wait_minutes"] == pytest.approx(round(expected_wait, 1))
    assert result["risk"] in ("LOW", "MEDIUM", "HIGH")


def test_passport_e2e_overload_returns_finite_wait_and_critical():
    """
    ADIM 6D ile GÜNCELLENDİ: rho>=1 artık None DEĞİL, backlog tabanlı
    sonlu bir dakika üretir (risk hâlâ CRITICAL, bağımsız çıktı).
    """
    calc = DemandCalculator(MockCapacityResolver())
    flights = [
        flight(DIRECTION_DEPARTURE, 10, i, aircraft="B77W",
               location=LOCATION_INTERNATIONAL, key=f"OV{i}", number=str(i))
        for i in range(6)
    ]
    result = passport_queue_model(flights, spec_passport_config(), calc.passenger_demand)

    assert result["utilization"] >= 1.0
    assert result["estimated_wait_minutes"] is not None
    assert result["estimated_wait_minutes"] > 0
    assert result["risk"] == "CRITICAL"


# ========================================================================
# 6. FLIGHT CLUSTERING
# ========================================================================

def test_clustering_and_arrival_bank_detected_in_dense_window():
    departures = [
        flight(DIRECTION_DEPARTURE, 10, i, location=LOCATION_DOMESTIC,
               key=f"CD{i}", number=str(i))
        for i in range(6)
    ]
    arrivals = [
        flight(DIRECTION_ARRIVAL, 10, i, location=LOCATION_INTERNATIONAL,
               key=f"CA{i}", number=str(i))
        for i in range(6)
    ]
    demand = DemandCalculator(MockCapacityResolver())

    security = predict_window(
        airport_iata=IST, process=PROCESS_SECURITY, window_start=window_of(departures[0]),
        process_flights=departures, config=roomy_config(), demand=demand,
        historical_baseline=2, aircraft_match_rate=1.0,
    )
    passport = predict_window(
        airport_iata=IST, process=PROCESS_PASSPORT, window_start=window_of(arrivals[0]),
        process_flights=arrivals, config=roomy_config(), demand=demand,
        historical_baseline=None, aircraft_match_rate=1.0,
    )

    assert "clustering" in {r.code for r in security.reasons}
    assert "arrival_bank" in {r.code for r in passport.reasons}


# ========================================================================
# 7. WIDEBODY EFFECT
# ========================================================================

def test_widebody_window_has_higher_demand_than_narrowbody_same_count():
    demand = DemandCalculator(MockCapacityResolver())
    narrow = [
        flight(DIRECTION_DEPARTURE, 10, i, aircraft="A320",
               location=LOCATION_INTERNATIONAL, key=f"N{i}", number=str(i))
        for i in range(4)
    ]
    wide = [
        flight(DIRECTION_DEPARTURE, 10, i, aircraft="B77W",
               location=LOCATION_INTERNATIONAL, key=f"W{i}", number=str(i))
        for i in range(4)
    ]

    narrow_pred = predict_window(
        airport_iata=IST, process=PROCESS_SECURITY, window_start=window_of(narrow[0]),
        process_flights=narrow, config=roomy_config(), demand=demand,
        historical_baseline=4, aircraft_match_rate=1.0,
    )
    wide_pred = predict_window(
        airport_iata=IST, process=PROCESS_SECURITY, window_start=window_of(wide[0]),
        process_flights=wide, config=roomy_config(), demand=demand,
        historical_baseline=4, aircraft_match_rate=1.0,
    )

    assert wide_pred.expected_passengers > narrow_pred.expected_passengers
    assert "widebody" in {r.code for r in wide_pred.reasons}
    assert "widebody" not in {r.code for r in narrow_pred.reasons}

    # Kapasite farkı TEK BAŞINA "aircraft_change" reason'ı ÜRETMEMELİ -
    # bu statik farklı uçuşlar, aynı flight'ın değişimi değil.
    assert "aircraft_change" not in {r.code for r in wide_pred.reasons}


# ========================================================================
# 8. DELAY COMPRESSION - tutarlılık kontrolü
# ========================================================================

def test_delay_compression_end_to_end_consistency():
    """
    3 uçuş farklı saatlerde planlı ama gecikme yüzünden AYNI 15 dk
    pencereye sıkışıyor. effective_time, window membership, flight
    count, passenger demand, clustering, security risk hepsi tutarlı
    olmalı.
    """
    f1 = flight(DIRECTION_DEPARTURE, 8, 0, aircraft="A320",
                location=LOCATION_DOMESTIC, key="DC1", number="1")
    f1.dep_actual_utc = datetime(2026, 9, 15, 9, 5)
    f2 = flight(DIRECTION_DEPARTURE, 8, 20, aircraft="A321",
                location=LOCATION_DOMESTIC, key="DC2", number="2")
    f2.dep_actual_utc = datetime(2026, 9, 15, 9, 8)
    f3 = flight(DIRECTION_DEPARTURE, 8, 30, aircraft="B738",
                location=LOCATION_DOMESTIC, key="DC3", number="3")
    f3.dep_actual_utc = datetime(2026, 9, 15, 9, 12)

    times = [effective_time(f) for f in (f1, f2, f3)]
    windows = {floor_to_window(t) for t in times}
    assert len(windows) == 1                       # hepsi AYNI pencerede
    window_start = windows.pop()

    demand = DemandCalculator(MockCapacityResolver())
    expected_demand = sum(demand.passenger_demand(f) for f in (f1, f2, f3))

    security = predict_window(
        airport_iata=IST, process=PROCESS_SECURITY, window_start=window_start,
        process_flights=[f1, f2, f3], config=roomy_config(), demand=demand,
        historical_baseline=1, aircraft_match_rate=1.0,
    )
    passport = predict_window(
        airport_iata=IST, process=PROCESS_PASSPORT, window_start=window_start,
        process_flights=[], config=roomy_config(), demand=demand,
        historical_baseline=None, aircraft_match_rate=1.0,
    )

    assert security.flight_count == 3
    assert security.expected_passengers == expected_demand
    assert "delay_compression" in {r.code for r in security.reasons}
    assert security.risk in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    assert passport.flight_count == 0   # domestic -> passport'u beslemiyor


# ========================================================================
# 9. CANCELLATION
# ========================================================================

def test_cancellation_excluded_from_demand_but_reason_preserved():
    active = flight(DIRECTION_DEPARTURE, 10, 0, location=LOCATION_DOMESTIC,
                     key="ACT", number="1")
    cancelled = flight(DIRECTION_DEPARTURE, 10, 5, location=LOCATION_DOMESTIC,
                        key="CXL", number="2", status="cancelled")

    demand = DemandCalculator(MockCapacityResolver())
    prediction = predict_window(
        airport_iata=IST, process=PROCESS_SECURITY, window_start=window_of(active),
        process_flights=[active, cancelled], config=roomy_config(), demand=demand,
        historical_baseline=1, aircraft_match_rate=1.0,
    )

    assert prediction.flight_count == 1                        # sadece active
    assert prediction.expected_passengers == demand.passenger_demand(active)
    assert "cancellation" in {r.code for r in prediction.reasons}


# ========================================================================
# 10. DIVERSION
# ========================================================================

def test_diversion_excluded_from_demand_but_reason_preserved():
    active = flight(DIRECTION_DEPARTURE, 10, 0, location=LOCATION_DOMESTIC,
                     key="ACT2", number="1")
    diverted = flight(DIRECTION_DEPARTURE, 10, 5, location=LOCATION_DOMESTIC,
                       key="DIV2", number="2", status="diverted")

    demand = DemandCalculator(MockCapacityResolver())
    prediction = predict_window(
        airport_iata=IST, process=PROCESS_SECURITY, window_start=window_of(active),
        process_flights=[active, diverted], config=roomy_config(), demand=demand,
        historical_baseline=1, aircraft_match_rate=1.0,
    )

    assert prediction.flight_count == 1
    assert prediction.expected_passengers == demand.passenger_demand(active)
    diversion_reasons = [r for r in prediction.reasons if r.code == "diversion"]
    assert len(diversion_reasons) == 1
    assert "DIV2" in diversion_reasons[0].message


# ========================================================================
# 11. AIRCRAFT CHANGE E2E
# ========================================================================

def _change_row(aircraft, dep_scheduled=datetime(2026, 9, 15, 10, 0)):
    return {
        "flight_key": "TK_9_2026-09-15", "airport_iata": IST,
        "direction": "departure", "location": "international",
        "airline_iata": "TK", "flight_number": "9", "flight_iata": "TK9",
        "aircraft_icao": aircraft, "aircraft_match_found": True,
        "dep_iata": IST, "arr_iata": "CDG",
        "dep_scheduled_utc": dep_scheduled, "dep_estimated_utc": None,
        "dep_actual_utc": None,
        "arr_scheduled_utc": dep_scheduled + timedelta(minutes=90),
        "arr_estimated_utc": None, "arr_actual_utc": None,
        "dep_terminal": None, "dep_gate": None,
        "arr_terminal": None, "arr_gate": None, "status": "scheduled",
    }


def test_aircraft_change_e2e_ist(session):
    """10:02 A320 -> 10:07 A321 -> 10:13 A330, aynı window."""
    refresh_flights(session, [_change_row("A320")])
    refresh_flights(session, [_change_row("A321")])
    refresh_flights(session, [_change_row("A330")])

    rows = session.execute(
        select(FlightEvent).order_by(FlightEvent.id)
    ).scalars().all()

    assert len(rows) == 2
    assert (rows[0].old_value, rows[0].new_value) == ("A320", "A321")
    assert (rows[1].old_value, rows[1].new_value) == ("A321", "A330")
    assert rows[0].flight_effective_time == rows[1].flight_effective_time   # aynı window
    # Duplicate yok: aynı transition tekrar gönderilirse yeni satır açılmaz.
    dup_summary = refresh_flights(session, [_change_row("A330")])
    assert dup_summary["events_written"] == 0
    assert session.scalar(select(func.count()).select_from(FlightEvent)) == 2

    resolver = MockCapacityResolver(capacities={"A330": 277})
    changes = aircraft_changes_for_airport(session, IST)
    flights = list(session.execute(select(Flight)).scalars().all())
    predictions = predict_airport(
        airport_iata=IST, flights=flights, config=roomy_config(),
        demand=DemandCalculator(resolver), aircraft_changes=changes,
    )
    # ADIM (Departure Show-Up Profile): PROCESS_PASSPORT artık show-up
    # ile BİRDEN FAZLA saatlik satır üretebiliyor - `aircraft_change`
    # nedeni flight_count/reasons bucket'lamasının (effective_time()
    # tek noktası, DEĞİŞMEDİ) kullandığı 08:00 satırında kalıyor.
    passport = next(p for p in predictions if p.process == PROCESS_PASSPORT and p.window_start.hour == 8)
    change_reasons = [r for r in passport.reasons if r.code == "aircraft_change"]

    assert len(change_reasons) == 2
    assert change_reasons[0].message == "TK_9_2026-09-15: A320→A321, kapasite +40 yolcu"
    assert change_reasons[1].message == "TK_9_2026-09-15: A321→A330, kapasite +57 yolcu"

    # Risk'e DOĞRUDAN boost yok, passenger demand event sayısından etkilenmiyor.
    without_events = predict_airport(
        airport_iata=IST, flights=flights, config=roomy_config(),
        demand=DemandCalculator(resolver), aircraft_changes=None,
    )
    passport_no_events = next(p for p in without_events if p.process == PROCESS_PASSPORT and p.window_start.hour == 8)
    assert passport.risk == passport_no_events.risk
    assert passport.expected_passengers == passport_no_events.expected_passengers


# ========================================================================
# 12. BASELINE E2E - idempotency
# ========================================================================

def test_baseline_e2e_repeated_refresh_of_closed_window_is_idempotent(session):
    row = {
        "flight_key": "TK_44_2026-09-15", "airport_iata": IST,
        "direction": "departure", "location": "domestic",
        "airline_iata": "TK", "flight_number": "44", "flight_iata": "TK44",
        "aircraft_icao": "A320", "aircraft_match_found": True,
        "dep_iata": IST, "arr_iata": "ESB",
        "dep_scheduled_utc": datetime(2026, 9, 15, 10, 0),
        "dep_estimated_utc": None, "dep_actual_utc": None,
        "arr_scheduled_utc": datetime(2026, 9, 15, 11, 0),
        "arr_estimated_utc": None, "arr_actual_utc": None,
        "dep_terminal": None, "dep_gate": None,
        "arr_terminal": None, "arr_gate": None, "status": "scheduled",
    }
    refresh_flights(session, [row])
    resolver = MockCapacityResolver()
    now = datetime(2026, 9, 15, 12, 0)     # pencere kesinlikle kapanmış

    for _ in range(5):
        run_predictions(session, resolver, now=now)

    # ADIM (Departure Show-Up Profile): 10:00 kalkışın talebi artık
    # show-up ile 3 saate (07:00/08:00/09:00) yayılıyor - `now=12:00`
    # saatinde ÜÇÜ DE kapanmış, bu yüzden 3 ayrı HistoricalFlightCount
    # bucket'ı (1 DEĞİL) - her biri KENDİ İÇİNDE hâlâ idempotent (5 kez
    # çağrılsa da sample_size=1). `flight_count` HÂLÂ tek effective_
    # time() noktasında (08:00 - dep 10:00-120dk) toplanıyor (DEĞİŞMEDİ) -
    # SADECE o saatin baseline'ı 1.0, diğer iki saat (show-up'ın
    # DEĞİL flight-bucket'ının GÖRMEDİĞİ saatler) flight_count=0/baseline=0.0.
    hist = session.execute(select(HistoricalFlightCount)).scalars().all()
    security_bucket = [h for h in hist if h.process == PROCESS_SECURITY]
    assert len(security_bucket) == 3
    for bucket in security_bucket:
        assert bucket.sample_size == 1
    real_bucket = next(b for b in security_bucket if b.hour_of_day == 8)
    baseline = get_baseline(
        session, IST, PROCESS_SECURITY,
        hour_of_day=real_bucket.hour_of_day,
        day_of_week=real_bucket.day_of_week,
    )
    assert baseline == 1.0


def test_baseline_e2e_open_window_never_recorded(session):
    row = {
        "flight_key": "TK_45_2026-09-15", "airport_iata": IST,
        "direction": "departure", "location": "domestic",
        "airline_iata": "TK", "flight_number": "45", "flight_iata": "TK45",
        "aircraft_icao": "A320", "aircraft_match_found": True,
        "dep_iata": IST, "arr_iata": "ESB",
        "dep_scheduled_utc": datetime(2026, 9, 15, 10, 0),
        "dep_estimated_utc": None, "dep_actual_utc": None,
        "arr_scheduled_utc": datetime(2026, 9, 15, 11, 0),
        "arr_estimated_utc": None, "arr_actual_utc": None,
        "dep_terminal": None, "dep_gate": None,
        "arr_terminal": None, "arr_gate": None, "status": "scheduled",
    }
    refresh_flights(session, [row])
    resolver = MockCapacityResolver()
    # ADIM (Departure Show-Up Profile): dep_scheduled 10:00 -> show-up
    # 3 saate (07:00/08:00/09:00) yayılıyor - HİÇBİRİNİN kapanmadığından
    # emin olmak için `now` artık EN ERKEN saatin (07:00) İÇİNDE.
    still_open = datetime(2026, 9, 15, 7, 30)   # hiçbir pencere henüz kapanmadı

    run_predictions(session, resolver, now=still_open)

    assert session.scalar(select(func.count()).select_from(HistoricalFlightCount)) == 0


# ========================================================================
# 13. UNKNOWN AIRPORT
# ========================================================================

def test_unknown_airport_run_predictions_does_not_crash(session):
    result = run_predictions(session, MockCapacityResolver(), airports=["ZZZ"])
    assert result["airports"] == {"ZZZ": 0}
    assert result["predictions"] == 0


def test_unknown_airport_country_lookup_falls_back_controlled(session):
    """
    Airport tablosunda YOKSA sahte bir satır oluşturulmaz; location
    çözümü mevcut controlled fallback'e (international) düşer.
    """
    lookup = country_lookup(session)
    assert "ZZZ" not in lookup
    assert session.get(Airport, "ZZZZ") is None

    row = {
        "flight_key": "XX_1_2026-09-15", "airport_iata": "ZZZ",
        "direction": "departure", "location": None,
        "airline_iata": "XX", "flight_number": "1", "flight_iata": "XX1",
        "aircraft_icao": "A320", "aircraft_match_found": True,
        "dep_iata": "ZZZ", "arr_iata": "YYY",
        "dep_scheduled_utc": datetime(2026, 9, 15, 10, 0),
        "dep_estimated_utc": None, "dep_actual_utc": None,
        "arr_scheduled_utc": datetime(2026, 9, 15, 11, 0),
        "arr_estimated_utc": None, "arr_actual_utc": None,
        "dep_terminal": None, "dep_gate": None,
        "arr_terminal": None, "arr_gate": None, "status": "scheduled",
    }
    from app.queue.ingestion.sources import resolve_location
    resolved = resolve_location("ZZZ", "YYY", lookup)
    assert resolved == LOCATION_INTERNATIONAL   # controlled fallback, uydurma yok

    row["location"] = resolved
    refresh_flights(session, [row])
    result = run_predictions(session, MockCapacityResolver(), airports=["ZZZ"])

    assert session.get(Airport, "ZZZ") is None    # sahte airport OLUŞTURULMADI
    assert result["airports"]["ZZZ"] >= 0


# ========================================================================
# 14. FULL PIPELINE - gerçek dosya + gerçek AircraftCapacityService
# ========================================================================

def test_full_pipeline_real_airport_file_and_real_capacity_service(session):
    """
    source (gerçek flight_airports.sql + gerçek yolcu_ucaklari.json) ->
    parse -> normalize -> airport lookup -> flight rules -> demand ->
    baseline -> security scoring -> passport queue -> reasons ->
    reporting/DTO. Hiçbir adım atlanmıyor; gerçek database.sqlite'a
    dokunulmuyor (bellek içi session).
    """
    from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset
    from app.service import AircraftCapacityService

    imported = import_airports(session, REAL_SQL_PATH)
    assert imported > 9000
    assert session.get(Airport, "IST").icao_code == "LTFM"

    seed_verified_dataset(session)
    seed_curated_fallback(session)
    seed_family_and_ga(session)

    countries = country_lookup(session)
    assert countries["IST"] == "TR"

    source_a_records = [
        {
            "flight_iata": "TK1", "flight_number": "1", "airline_iata": "TK",
            "dep_iata": "IST", "arr_iata": "CDG",
            "dep_time_utc": "2026-09-15 10:00", "arr_time_utc": "2026-09-15 12:30",
            "status": "scheduled", "aircraft_icao": "A321",
        },
        {
            "flight_iata": "TK2", "flight_number": "2", "airline_iata": "TK",
            "dep_iata": "ESB", "arr_iata": "IST",
            "dep_time_utc": "2026-09-15 09:30", "arr_time_utc": "2026-09-15 10:45",
            "status": "landed", "aircraft_icao": "B738",
        },
    ]
    parsed_departures = parse_source_a(
        [r for r in source_a_records if r["dep_iata"] == "IST"],
        "departure", countries, {},
    )
    parsed_arrivals = parse_source_a(
        [r for r in source_a_records if r["arr_iata"] == "IST"],
        "arrival", countries, {},
    )
    rows = parsed_departures + parsed_arrivals
    assert len(rows) == 2
    assert {r["airport_iata"] for r in rows} == {"IST"}

    summary = refresh_flights(session, rows)
    assert summary["inserted"] == 2

    resolver = AircraftCapacityService(session)
    # ADIM (Operational-Day Scope): IST'in gerçek yerel saat dilimi
    # (Europe/Istanbul, UTC+3) artık "bugün" filtresine giriyor - 23:00
    # UTC yerel Sep 16 00:00'a denk gelip flight'ları YANLIŞ güne
    # düşürürdü; 20:00 UTC hem pencereleri kapatır hem yerel Sep 15'te kalır.
    result = run_predictions(session, resolver, airports=["IST"], now=datetime(2026, 9, 15, 20, 0))

    assert result["airports"]["IST"] > 0
    assert session.scalar(
        select(func.count()).select_from(QueuePrediction)
        .where(QueuePrediction.airport_iata == "IST")
    ) == result["predictions"]

    report = hourly_report(session, "IST", datetime(2026, 9, 15).date(), resolver)
    assert report["airport"] == "IST"
    assert len(report["hourly"]) > 0
    for entry in report["hourly"]:
        assert "flows" in entry
        assert PROCESS_SECURITY in entry
        assert PROCESS_PASSPORT in entry
