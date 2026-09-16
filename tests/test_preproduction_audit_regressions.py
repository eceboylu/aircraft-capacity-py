"""Pre-production audit regressions derived from the final production contract.

These tests intentionally describe required behaviour that was not covered by the
existing suite.  They do not mutate production data and use an in-memory database.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.constants import (
    PROCESS_PASSPORT,
    PROCESS_SECURITY_DOMESTIC,
    STATUS_CANCELLED,
)
from app.queue.engine import run_predictions
from app.queue.ingestion.refresh import refresh_flights
from app.queue.models import AirportOperationalConfig, Flight, QueuePrediction


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def _flight_row(status: str) -> dict:
    scheduled = datetime(2026, 9, 16, 9, 0)
    return {
        "flight_key": "TK_900_2026-09-16_AAA_departure",
        "airport_iata": "AAA",
        "direction": "departure",
        "location": "international",
        "airline_iata": "TK",
        "flight_number": "900",
        "flight_iata": "TK900",
        "aircraft_icao": "A320",
        "aircraft_match_found": True,
        "dep_iata": "AAA",
        "arr_iata": "BBB",
        "dep_scheduled_utc": scheduled,
        "dep_estimated_utc": None,
        "dep_actual_utc": None,
        "arr_scheduled_utc": scheduled,
        "arr_estimated_utc": None,
        "arr_actual_utc": None,
        "dep_terminal": None,
        "dep_gate": None,
        "arr_terminal": None,
        "arr_gate": None,
        "status": status,
    }


def _prediction(process: str, start: datetime, risk: str, wait=None):
    return QueuePrediction(
        airport_iata="AAA",
        process=process,
        window_start=start,
        window_end=start.replace(hour=start.hour + 1),
        flight_count=1,
        expected_passengers=100,
        baseline_ratio=1.0,
        flight_ratio=1.0,
        passenger_ratio=1.0,
        utilization=0.5 if process == PROCESS_PASSPORT else None,
        estimated_wait_minutes=wait,
        risk=risk,
        reasons="[]",
        confidence=0.8,
        calculated_at=datetime.now(timezone.utc),
    )


def test_cancelled_flight_cannot_be_resurrected_by_a_later_active_snapshot(session):
    """A terminal cancellation must not silently become active/landed later."""
    refresh_flights(session, [_flight_row("active")])
    refresh_flights(session, [_flight_row(STATUS_CANCELLED)])
    refresh_flights(session, [_flight_row("active")])

    flight = session.scalar(select(Flight))
    assert flight.status == STATUS_CANCELLED


def test_overall_current_compares_only_processes_from_the_same_hour(session):
    """Fallback currents from different hours must not be severity-compared."""
    eight = datetime(2026, 9, 16, 8, 0)
    nine = datetime(2026, 9, 16, 9, 0)
    session.add(_prediction(PROCESS_SECURITY_DOMESTIC, eight, "CRITICAL"))
    session.add(_prediction(PROCESS_PASSPORT, nine, "LOW", wait=0.1))
    session.commit()

    result = airport_predictions(session, "AAA", now=datetime(2026, 9, 16, 9, 30))

    assert result["overall"]["current"]["window_start"] == nine.isoformat()
    assert result["overall"]["current"]["risk"] == "LOW"


class _Capacity:
    capacity = 180
    counts_toward_passenger_total = True


class _Resolver:
    def resolve(self, *_args):
        return _Capacity()


def test_invalid_zero_counter_config_fails_the_airport_instead_of_persisting_a_prediction(session):
    """counter_count<=0 must be surfaced as invalid config, not a prediction."""
    refresh_flights(session, [_flight_row("active")])
    session.add(AirportOperationalConfig(
        airport_iata="AAA",
        passport_counter_count=0,
        passport_staff_count=8,
        passport_staff_per_counter=2.0,
        passport_service_rate_per_staff=1.0,
        passport_efficiency_multiplier=0.8125,
        arrival_bank_threshold=5,
    ))
    session.commit()

    result = run_predictions(
        session,
        _Resolver(),
        airports=["AAA"],
        update_baseline=False,
        now=datetime(2026, 9, 16, 9, 30),
    )

    assert result["failed_airports"] == ["AAA"]
    assert session.scalar(select(QueuePrediction)) is None


def test_security_empty_wait_uses_required_hesaplanamiyor_copy():
    html = (
        Path(__file__).parents[1] / "app" / "web" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    start = html.index("function waitTimeText")
    end = html.index("function windowWaitText", start)
    security_wait_function = html[start:end]

    assert 'return "Hesaplanamıyor";' in security_wait_function
    assert "estimated_wait_minutes != null" in security_wait_function
