"""Arrival Passport deterministic passenger-release profile regressions."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.config import default_config
from app.queue.constants import (
    ARRIVAL_RELEASE_PROFILE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
)
from app.queue.core.event_queue import simulate_fifo_queue, total_count
from app.queue.domain.demand import (
    DemandCalculator,
    arrival_passenger_release_events,
)
from app.queue.engine import predict_airport, run_predictions
from app.queue.models import Airport, Flight

from .factories import MockCapacityResolver, arrival, at, departure


def _demand():
    return DemandCalculator(MockCapacityResolver())


def _intl_arrival(hour, minute=0, **kwargs):
    kwargs.setdefault("location", LOCATION_INTERNATIONAL)
    return arrival(hour, minute, **kwargs)


def test_profile_shape_and_passenger_conservation():
    assert ARRIVAL_RELEASE_PROFILE == (
        (10, 0.15), (15, 0.35), (20, 0.30), (25, 0.15), (30, 0.05),
    )
    assert sum(fraction for _, fraction in ARRIVAL_RELEASE_PROFILE) == 1.0

    flight = _intl_arrival(14, 0, aircraft="B77W", key="CONSERVE")
    total = _demand().passenger_demand(flight)
    events = arrival_passenger_release_events(flight, total)
    assert sum(count for _, count in events) == total

    for small_total in (1, 2, 3, 5, 7, 13, 27):
        small_events = arrival_passenger_release_events(flight, small_total)
        assert sum(count for _, count in small_events) == small_total
        assert all(count > 0 for _, count in small_events)


def test_raw_arrival_priority_and_no_double_offset():
    scheduled = at(14, 0)
    estimated = at(14, 3)
    actual = at(14, 7)
    flight = SimpleNamespace(
        arr_scheduled_utc=scheduled,
        arr_estimated_utc=estimated,
        arr_actual_utc=actual,
    )

    events = arrival_passenger_release_events(flight, 100)
    assert events[0][0] == actual + timedelta(minutes=10)
    assert events[0][0] != actual + timedelta(minutes=25)
    assert [moment for moment, _ in events] == [
        actual + timedelta(minutes=offset)
        for offset, _ in ARRIVAL_RELEASE_PROFILE
    ]

    flight.arr_actual_utc = None
    assert arrival_passenger_release_events(flight, 100)[0][0] == estimated + timedelta(minutes=10)
    flight.arr_estimated_utc = None
    assert arrival_passenger_release_events(flight, 100)[0][0] == scheduled + timedelta(minutes=10)


def test_release_events_are_deterministic_and_idempotent():
    flight = _intl_arrival(14, 0, key="IDEMPOTENT")
    first = arrival_passenger_release_events(flight, 333)
    assert first == arrival_passenger_release_events(flight, 333)
    assert first == arrival_passenger_release_events(flight, 333)
    assert sum(count for _, count in first) == 333


def test_each_flight_is_profiled_independently_and_batches_overlap():
    flight_a = _intl_arrival(14, 0, key="ARR_A")
    flight_b = _intl_arrival(14, 5, key="ARR_B")
    events_a = arrival_passenger_release_events(flight_a, 200)
    events_b = arrival_passenger_release_events(flight_b, 180)

    assert events_a[0][0] == at(14, 10)
    assert events_b[0][0] == at(14, 15)
    assert {t for t, _ in events_a} & {t for t, _ in events_b}
    assert sum(count for _, count in events_a) == 200
    assert sum(count for _, count in events_b) == 180


def test_arrival_queue_is_continuous_across_batches_and_hour_boundary():
    flight = _intl_arrival(14, 40, key="CONTINUOUS")
    arrivals = arrival_passenger_release_events(flight, 100)
    service_events = simulate_fifo_queue(
        [(moment, "arrival", count) for moment, count in arrivals],
        server_count=1,
        service_time_minutes=1,
    )

    assert total_count(service_events) == 100
    assert any(
        event.arrival_time < at(15, 0) < event.completion_time
        for event in service_events
    )
    after_hour = [event for event in service_events if event.arrival_time >= at(15, 0)]
    assert after_hour
    assert any(event.service_start_time > event.arrival_time for event in after_hour)


def test_domestic_arrival_is_excluded_from_every_queue():
    flight = arrival(14, 0, location=LOCATION_DOMESTIC, key="DOM_ARR")
    predictions = predict_airport("AAA", [flight], default_config("AAA"), _demand())
    assert predictions == []


def test_arrival_load_does_not_change_departure_pools_or_security_chain():
    intl_dep = departure(
        17, 0, location=LOCATION_INTERNATIONAL, aircraft="B77W", key="INTL_DEP"
    )
    domestic_dep = departure(
        17, 0, location=LOCATION_DOMESTIC, aircraft="B77W", key="DOM_DEP"
    )
    arrivals = [
        _intl_arrival(14, minute, aircraft="A388", key=f"HEAVY_{minute}")
        for minute in (0, 5, 10)
    ]

    baseline = predict_airport(
        "AAA", [intl_dep, domestic_dep], default_config("AAA"), _demand()
    )
    with_arrivals = predict_airport(
        "AAA", [intl_dep, domestic_dep, *arrivals], default_config("AAA"), _demand()
    )

    departure_processes = {
        PROCESS_PASSPORT_DEPARTURE, PROCESS_SECURITY_INTL, PROCESS_SECURITY_DOMESTIC
    }

    def signature(predictions):
        return [
            (
                p.process, p.window_start, p.expected_passengers,
                p.utilization, p.estimated_wait_minutes, p.risk,
            )
            for p in predictions if p.process in departure_processes
        ]

    assert signature(with_arrivals) == signature(baseline)


def test_cross_midnight_release_events_keep_real_timestamps():
    flight = _intl_arrival(23, 50, key="CROSS_MIDNIGHT")
    events = arrival_passenger_release_events(flight, 100)
    assert [moment for moment, _ in events] == [
        datetime(2026, 9, 15, 0, minute) for minute in (0, 5, 10, 15, 20)
    ]
    assert sum(count for _, count in events) == 100


def test_arrival_release_visible_graph_remains_exactly_24_hours():
    db_engine = create_engine("sqlite://")
    Base.metadata.create_all(db_engine)
    session = sessionmaker(bind=db_engine)()
    day = datetime(2026, 9, 14)
    try:
        session.add(Airport(
            iata_code="AAA", airport_name="Arrival Test", timezone="UTC"
        ))
        session.add(Flight(
            flight_key="ARR_GRAPH", airport_iata="AAA",
            direction="arrival", location="international",
            airline_iata="XX", flight_number="1", flight_iata="XX1",
            aircraft_icao="A320", aircraft_match_found=True,
            dep_iata="ZZZ", arr_iata="AAA",
            dep_scheduled_utc=day.replace(hour=12),
            arr_scheduled_utc=day.replace(hour=14), status="scheduled",
        ))
        session.commit()

        run_predictions(
            session, MockCapacityResolver(), airports=["AAA"],
            now=day.replace(hour=18),
        )
        windows = airport_predictions(
            session, "AAA", now=day.replace(hour=18)
        )["international_arrival"]["windows"]
        assert len(windows) == 24
        assert sum(row["expected_passengers"] for row in windows) == pytest.approx(180)
    finally:
        session.close()


def test_missing_time_or_nonpositive_demand_produces_no_release_events():
    missing = SimpleNamespace(
        arr_actual_utc=None, arr_estimated_utc=None, arr_scheduled_utc=None
    )
    assert arrival_passenger_release_events(missing, 100) == []
    flight = _intl_arrival(14, 0)
    assert arrival_passenger_release_events(flight, 0) == []
    assert arrival_passenger_release_events(flight, -1) == []
