"""
MADDE 8 doğrulaması - aynı 15 dakikalık prediction window içinde
birden fazla aircraft change yaşanan bir flight'ın TÜM değişikliklerinin
korunması (sadece son değişiklik değil), duplicate event oluşmaması,
kronolojik sıranın korunması ve bu event'lerin risk/passenger demand'a
DOĞRUDAN etki ETMEMESİ - sadece açıklama/context olması.

Test verisi (A320/A321/A330/B77W kapasiteleri) açıkça TEST fixture'ıdır.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.constants import DEMAND_WINDOW_MINUTES, PROCESS_PASSPORT
from app.queue.domain.demand import DemandCalculator
from app.queue.engine import predict_airport, predict_window, run_predictions
from app.queue.ingestion.refresh import (
    aircraft_changes_for_airport,
    refresh_flights,
)
from app.queue.models import AirportOperationalConfig, Flight, FlightEvent
from app.queue.reasons.detector import detect_aircraft_changes

from .factories import MockCapacityResolver, at, departure


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


def make_row(
    aircraft_icao,
    *,
    flight_key="TK_1_2026-09-15",
    dep_scheduled=datetime(2026, 9, 15, 10, 0),
    dep_estimated=None,
    status="scheduled",
    airport="AAA",
    location="domestic",
):
    """
    TEST fixture satırı - refresh_flights()'in beklediği ham sözlük
    biçiminde. dep_scheduled/estimated sabit tutulursa effective_time
    (dolayısıyla prediction window) da sabit kalır - MADDE 8'in
    "aynı window'da birden fazla değişiklik" senaryosunu üretir.
    """
    return {
        "flight_key": flight_key,
        "airport_iata": airport,
        "direction": "departure",
        "location": location,
        "airline_iata": "TK",
        "flight_number": "1",
        "flight_iata": "TK1",
        "aircraft_icao": aircraft_icao,
        "aircraft_match_found": True,
        "dep_iata": airport,
        "arr_iata": "ZZZ",
        "dep_scheduled_utc": dep_scheduled,
        "dep_estimated_utc": dep_estimated,
        "dep_actual_utc": None,
        "arr_scheduled_utc": dep_scheduled + timedelta(minutes=60),
        "arr_estimated_utc": None,
        "arr_actual_utc": None,
        "dep_terminal": None, "dep_gate": None,
        "arr_terminal": None, "arr_gate": None,
        "status": status,
    }


def events(session) -> list[FlightEvent]:
    return list(session.execute(
        select(FlightEvent).order_by(FlightEvent.id)
    ).scalars().all())


# --------------------------------------------------------------------
# Tek aircraft change
# --------------------------------------------------------------------

def test_single_aircraft_change_is_recorded(session):
    refresh_flights(session, [make_row("A320")])
    summary = refresh_flights(session, [make_row("A321")])

    assert summary["events_written"] == 1
    rows = events(session)
    assert len(rows) == 1
    assert (rows[0].old_value, rows[0].new_value) == ("A320", "A321")
    assert rows[0].flight_effective_time is not None


# --------------------------------------------------------------------
# Aynı window'da 2 ve 3+ aircraft change - HİÇBİRİ üzerine yazılmıyor
# --------------------------------------------------------------------

def test_two_aircraft_changes_same_window_both_preserved(session):
    """10:02 A320, 10:07 A321, 10:13 A330 - dep_scheduled sabit,
    dolayısıyla effective_time (window) da sabit kalır."""
    refresh_flights(session, [make_row("A320")])
    refresh_flights(session, [make_row("A321")])
    refresh_flights(session, [make_row("A330")])

    rows = events(session)
    assert len(rows) == 2
    assert (rows[0].old_value, rows[0].new_value) == ("A320", "A321")
    assert (rows[1].old_value, rows[1].new_value) == ("A321", "A330")
    # İkisi de AYNI window'a (aynı flight_effective_time) ait.
    assert rows[0].flight_effective_time == rows[1].flight_effective_time


def test_three_plus_aircraft_changes_all_preserved(session):
    chain = ["A320", "A321", "A330", "B77W", "B738"]
    refresh_flights(session, [make_row(chain[0])])
    for aircraft in chain[1:]:
        refresh_flights(session, [make_row(aircraft)])

    rows = events(session)
    assert len(rows) == 4
    transitions = [(r.old_value, r.new_value) for r in rows]
    assert transitions == [
        ("A320", "A321"), ("A321", "A330"), ("A330", "B77W"), ("B77W", "B738"),
    ]


def test_no_event_overwrite_first_event_untouched_after_second(session):
    """İlk event'in DEĞERLERİ ikinci change geldikten sonra da aynı kalmalı."""
    refresh_flights(session, [make_row("A320")])
    refresh_flights(session, [make_row("A321")])
    first_snapshot = (events(session)[0].old_value, events(session)[0].new_value)

    refresh_flights(session, [make_row("A330")])
    first_after_second_change = events(session)[0]

    assert (first_after_second_change.old_value, first_after_second_change.new_value) == first_snapshot
    assert first_after_second_change.old_value == "A320"
    assert first_after_second_change.new_value == "A321"


# --------------------------------------------------------------------
# Chronological order
# --------------------------------------------------------------------

def test_events_preserve_chronological_order_in_db(session):
    for aircraft in ["A320", "A321", "A330"]:
        refresh_flights(session, [make_row(aircraft)])

    rows = events(session)
    assert [r.old_value for r in rows] == ["A320", "A321"]
    assert [r.new_value for r in rows] == ["A321", "A330"]


def test_aircraft_changes_for_airport_returns_chronological_list(session):
    for aircraft in ["A320", "A321", "A330"]:
        refresh_flights(session, [make_row(aircraft)])

    changes = aircraft_changes_for_airport(session, "AAA")
    key = "TK_1_2026-09-15"
    assert key in changes
    pairs = [(old, new) for old, new, _t in changes[key]]
    assert pairs == [("A320", "A321"), ("A321", "A330")]


# --------------------------------------------------------------------
# Duplicate event - aynı flight+eski+yeni+effective_time tekrar girerse
# --------------------------------------------------------------------

def test_exact_duplicate_ingestion_does_not_create_second_event(session):
    """
    10:07 A320->A321, 10:07 A320->A321 (aynı kaynak verisi tekrar
    ingestion'a girer) - tek bir event kalmalı.
    """
    refresh_flights(session, [make_row("A320")])
    refresh_flights(session, [make_row("A321")])
    assert len(events(session)) == 1

    # AYNI transition'ı simüle etmek için Flight'ı elle A320'ye
    # geri alıp AYNI dep_scheduled ile tekrar A321'e "değiştiriyoruz" -
    # aynı flight_key + aynı eski/yeni + AYNI effective_time.
    flight = session.execute(
        select(Flight).where(Flight.flight_key == "TK_1_2026-09-15")
    ).scalar_one()
    flight.aircraft_icao = "A320"
    session.commit()

    summary = refresh_flights(session, [make_row("A321")])

    assert summary["events_written"] == 0
    assert len(events(session)) == 1


def test_different_effective_time_creates_separate_event_even_if_same_transition(session):
    """
    Kural: aynı (eski,yeni) çifti farklı effective_time'da tekrar
    olursa bu GERÇEK farklı bir olaydır, duplicate DEĞİLDİR.
    """
    base_time = datetime(2026, 9, 15, 10, 0)
    refresh_flights(session, [make_row("A320", dep_scheduled=base_time)])
    refresh_flights(session, [make_row("A321", dep_scheduled=base_time)])

    # Uçuşun programı değişti (yeni bir gün/saat) - flight_key aynı
    # kalacak şekilde SADECE estimated değiştiriyoruz ki
    # operational tarih (flight_key) sabit kalsın ama effective_time
    # değişsin.
    later = base_time + timedelta(hours=3)
    session.execute(
        select(Flight).where(Flight.flight_key == "TK_1_2026-09-15")
    ).scalar_one()

    flight = session.execute(
        select(Flight).where(Flight.flight_key == "TK_1_2026-09-15")
    ).scalar_one()
    flight.aircraft_icao = "A320"
    session.commit()

    refresh_flights(session, [
        make_row("A321", dep_scheduled=base_time, dep_estimated=later)
    ])

    # Sadece AIRCRAFT_CHANGED tipini incele - estimated 3 saat kaydığı
    # için ayrıca bir DELAYED event'i de oluşur, bu testin kapsamı dışı.
    rows = [e for e in events(session) if e.event_type == "AIRCRAFT_CHANGED"]
    assert len(rows) == 2
    assert rows[0].flight_effective_time != rows[1].flight_effective_time
    assert (rows[0].old_value, rows[0].new_value) == ("A320", "A321")
    assert (rows[1].old_value, rows[1].new_value) == ("A320", "A321")


# --------------------------------------------------------------------
# Window boundary
# --------------------------------------------------------------------

def test_window_boundary_event_just_before_close_is_in_current_window():
    flight = departure(9, 0, aircraft="B77W", location="domestic")
    # Pencere floor_to_window(effective_time,15) ile hesaplanır.
    from app.queue.domain.demand import effective_time
    eff = effective_time(flight)
    from app.queue.engine import floor_to_window
    window_start = floor_to_window(eff)
    window_end = window_start + timedelta(minutes=15)

    just_before_close = window_end - timedelta(seconds=1)

    prediction = predict_window(
        airport_iata="AAA", process=PROCESS_PASSPORT, window_start=window_start,
        process_flights=[], config=_roomy_config(), demand=DemandCalculator(MockCapacityResolver()),
        historical_baseline=None, aircraft_match_rate=1.0,
        aircraft_changes={"K": [("A320", "B77W", just_before_close)]},
    )
    assert any(r.code == "aircraft_change" for r in prediction.reasons)


def test_window_boundary_event_exactly_at_close_belongs_to_next_window():
    from app.queue.engine import floor_to_window
    window_start = at(9, 0)
    window_end = window_start + timedelta(minutes=DEMAND_WINDOW_MINUTES)

    prediction_current = predict_window(
        airport_iata="AAA", process=PROCESS_PASSPORT, window_start=window_start,
        process_flights=[], config=_roomy_config(), demand=DemandCalculator(MockCapacityResolver()),
        historical_baseline=None, aircraft_match_rate=1.0,
        aircraft_changes={"K": [("A320", "B77W", window_end)]},   # tam sınırda
    )
    prediction_next = predict_window(
        airport_iata="AAA", process=PROCESS_PASSPORT, window_start=window_end,
        process_flights=[], config=_roomy_config(), demand=DemandCalculator(MockCapacityResolver()),
        historical_baseline=None, aircraft_match_rate=1.0,
        aircraft_changes={"K": [("A320", "B77W", window_end)]},
    )

    assert not any(r.code == "aircraft_change" for r in prediction_current.reasons)
    assert any(r.code == "aircraft_change" for r in prediction_next.reasons)


def test_event_previous_window_and_next_window_are_distinct():
    """
    Aynı flight'ın iki farklı window'a düşen iki event'i birbirine
    karışmıyor. ADIM 6D-2 HOURLY MIGRATION: window_b artık bir SONRAKİ
    SAAT (window_a + DEMAND_WINDOW_MINUTES) - eski +15dk kullanılsaydı
    60dk'lık pencereler [09:00-10:00)/[09:15-10:15) ÇAKIŞIRDI.
    """
    window_a = at(9, 0)
    window_b = window_a + timedelta(minutes=DEMAND_WINDOW_MINUTES)

    changes = {
        "K": [
            ("A320", "A321", window_a + timedelta(minutes=5)),   # window_a içinde
            ("A321", "A330", window_b + timedelta(minutes=5)),   # window_b içinde
        ]
    }

    prediction_a = predict_window(
        airport_iata="AAA", process=PROCESS_PASSPORT, window_start=window_a,
        process_flights=[], config=_roomy_config(), demand=DemandCalculator(MockCapacityResolver()),
        historical_baseline=None, aircraft_match_rate=1.0, aircraft_changes=changes,
    )
    prediction_b = predict_window(
        airport_iata="AAA", process=PROCESS_PASSPORT, window_start=window_b,
        process_flights=[], config=_roomy_config(), demand=DemandCalculator(MockCapacityResolver()),
        historical_baseline=None, aircraft_match_rate=1.0, aircraft_changes=changes,
    )

    a_reasons = [r for r in prediction_a.reasons if r.code == "aircraft_change"]
    b_reasons = [r for r in prediction_b.reasons if r.code == "aircraft_change"]

    assert len(a_reasons) == 1
    assert "A320→A321" in a_reasons[0].message
    assert len(b_reasons) == 1
    assert "A321→A330" in b_reasons[0].message


# --------------------------------------------------------------------
# Reason / explanation tüm değişiklikleri içeriyor
# --------------------------------------------------------------------

def test_reason_lists_every_change_in_the_window():
    changes = {"K": [("A320", "A321"), ("A321", "A330")]}
    resolver = MockCapacityResolver(capacities={"A330": 277})
    found = detect_aircraft_changes(changes, lambda i: resolver.resolve(i).capacity)

    assert len(found) == 2
    assert found[0].message == "K: A320→A321, kapasite +40 yolcu"
    assert found[1].message == "K: A321→A330, kapasite +57 yolcu"


# --------------------------------------------------------------------
# Risk'e DOĞRUDAN etki YOK
# --------------------------------------------------------------------

def test_aircraft_change_does_not_directly_boost_risk():
    flights = [
        departure(9, 0, aircraft="A320", location="domestic", key=f"F{i}", number=str(i))
        for i in range(3)
    ]
    demand = DemandCalculator(MockCapacityResolver())

    without_changes = predict_window(
        airport_iata="AAA", process=PROCESS_PASSPORT, window_start=at(8, 30),
        process_flights=flights, config=_roomy_config(), demand=demand,
        historical_baseline=None, aircraft_match_rate=1.0, aircraft_changes=None,
    )
    with_changes = predict_window(
        airport_iata="AAA", process=PROCESS_PASSPORT, window_start=at(8, 30),
        process_flights=flights, config=_roomy_config(), demand=demand,
        historical_baseline=None, aircraft_match_rate=1.0,
        aircraft_changes={"F0": [("A320", "B77W", at(8, 35))]},
    )

    assert without_changes.risk == with_changes.risk
    assert without_changes.utilization == with_changes.utilization
    assert without_changes.estimated_wait_minutes == with_changes.estimated_wait_minutes
    assert without_changes.baseline_ratio == with_changes.baseline_ratio


def test_aircraft_change_reason_severity_does_not_leak_into_risk_field():
    """Reason'ın severity'si (warning/info) risk alanını hiç etkilemiyor."""
    flights = [departure(9, 0, aircraft="A320", location="domestic", key="F0", number="0")]
    demand = DemandCalculator(MockCapacityResolver())

    prediction = predict_window(
        airport_iata="AAA", process=PROCESS_PASSPORT, window_start=at(8, 30),
        process_flights=flights, config=_roomy_config(), demand=demand,
        historical_baseline=None, aircraft_match_rate=1.0,
        aircraft_changes={"F0": [("A320", "B77W", at(8, 35))]},   # +170 kapasite, warning
    )
    assert prediction.risk == "LOW"   # roomy config -> hâlâ düşük risk


# --------------------------------------------------------------------
# Passenger demand double-count edilmiyor
# --------------------------------------------------------------------

def test_passenger_demand_unaffected_by_number_of_change_events():
    """
    expected_passengers, window_flights'ın MEVCUT (final) aircraft
    tipinden hesaplanır - aircraft_changes'in kendisi talebe hiç girmez.
    0, 1 ya da 3 event ile expected_passengers AYNI kalmalı.
    """
    flight = departure(9, 0, aircraft="B77W", location="domestic", key="F0", number="0")
    demand = DemandCalculator(MockCapacityResolver())

    no_events = predict_window(
        airport_iata="AAA", process=PROCESS_PASSPORT, window_start=at(8, 30),
        process_flights=[flight], config=_roomy_config(), demand=demand,
        historical_baseline=None, aircraft_match_rate=1.0, aircraft_changes=None,
    )
    one_event = predict_window(
        airport_iata="AAA", process=PROCESS_PASSPORT, window_start=at(8, 30),
        process_flights=[flight], config=_roomy_config(), demand=demand,
        historical_baseline=None, aircraft_match_rate=1.0,
        aircraft_changes={"F0": [("A320", "B77W", at(8, 35))]},
    )
    three_events = predict_window(
        airport_iata="AAA", process=PROCESS_PASSPORT, window_start=at(8, 30),
        process_flights=[flight], config=_roomy_config(), demand=demand,
        historical_baseline=None, aircraft_match_rate=1.0,
        aircraft_changes={"F0": [
            ("A320", "A321", at(8, 31)),
            ("A321", "A330", at(8, 33)),
            ("A330", "B77W", at(8, 35)),
        ]},
    )

    assert no_events.expected_passengers == one_event.expected_passengers == three_events.expected_passengers


# --------------------------------------------------------------------
# Mevcut aircraft capacity resolution regresyonu
# --------------------------------------------------------------------

def test_capacity_resolution_uses_current_aircraft_regardless_of_change_history(session):
    """
    3 kez aircraft değişse bile talep SADECE flight'ın ŞU ANKİ
    (final) aircraft_icao'sundan hesaplanır - sanki hiç değişmemiş
    gibi aynı sonucu vermeli.
    """
    for aircraft in ["A320", "A321", "A330"]:
        refresh_flights(session, [make_row(aircraft)])

    changed_flight = session.execute(
        select(Flight).where(Flight.flight_key == "TK_1_2026-09-15")
    ).scalar_one()

    never_changed_flight = session.execute(
        select(Flight).where(Flight.flight_key == "TK_1_2026-09-15")
    ).scalar_one()

    resolver = MockCapacityResolver(capacities={"A330": 277})
    demand = DemandCalculator(resolver)

    assert changed_flight.aircraft_icao == "A330"
    assert demand.seat_capacity(changed_flight) == demand.seat_capacity(never_changed_flight) == 277


# --------------------------------------------------------------------
# DB persistence regresyonu
# --------------------------------------------------------------------

def test_flight_row_stays_singular_despite_multiple_aircraft_changes(session):
    for aircraft in ["A320", "A321", "A330"]:
        refresh_flights(session, [make_row(aircraft)])

    assert session.scalar(select(func.count()).select_from(Flight)) == 1
    assert session.scalar(select(func.count()).select_from(FlightEvent)) == 2


def test_other_event_types_unaffected_by_flight_effective_time_column(session):
    """Cancellation/diversion event'leri yeni kolon eklendikten sonra da doğru çalışıyor."""
    refresh_flights(session, [make_row("A320", status="scheduled")])
    refresh_flights(session, [make_row("A320", status="cancelled")])

    rows = events(session)
    assert len(rows) == 1
    assert rows[0].event_type == "CANCELLED"
    assert rows[0].flight_effective_time is None


# --------------------------------------------------------------------
# E2E hazırlığı - A320 -> A321 -> A330, tam pipeline
# --------------------------------------------------------------------

def test_e2e_full_pipeline_multiple_aircraft_changes(session):
    session.add(AirportOperationalConfig(airport_iata="AAA", passport_counter_count=24))
    session.commit()

    # international -> passport'u besler (domestic beslemez).
    base_time = datetime(2026, 9, 15, 10, 0)
    refresh_flights(session, [make_row(
        "A320", dep_scheduled=base_time, airport="AAA", location="international",
    )])
    refresh_flights(session, [make_row(
        "A321", dep_scheduled=base_time, airport="AAA", location="international",
    )])
    refresh_flights(session, [make_row(
        "A330", dep_scheduled=base_time, airport="AAA", location="international",
    )])

    assert session.scalar(select(func.count()).select_from(FlightEvent)) == 2

    resolver = MockCapacityResolver(capacities={"A330": 277})
    predictions = predict_airport(
        airport_iata="AAA",
        flights=list(session.execute(select(Flight)).scalars().all()),
        config=_roomy_config(),
        demand=DemandCalculator(resolver),
        aircraft_changes=aircraft_changes_for_airport(session, "AAA"),
    )

    passport = next(p for p in predictions if p.process == PROCESS_PASSPORT)
    change_reasons = [r for r in passport.reasons if r.code == "aircraft_change"]

    assert len(change_reasons) == 2
    assert change_reasons[0].message == "TK_1_2026-09-15: A320→A321, kapasite +40 yolcu"
    assert change_reasons[1].message == "TK_1_2026-09-15: A321→A330, kapasite +57 yolcu"

    # Talep SADECE final aircraft'tan (A330=277 ham kapasite - ADIM ICAO
    # Demand Kalibrasyonu, load factor YOK) - 3 değişiklik nedeniyle ne
    # tripled ne toplanmış.
    assert passport.flight_count == 1
    assert passport.expected_passengers == 277
    assert passport.risk == "LOW"   # değişiklik risk'i doğrudan artırmadı


def _roomy_config():
    from app.queue.config import AirportConfigView
    return AirportConfigView(
        airport_iata="AAA",
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
