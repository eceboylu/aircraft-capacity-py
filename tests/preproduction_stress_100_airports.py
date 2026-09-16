"""Standalone in-memory pre-production stress audit: 100 airports / 10,000 flights.

Run with:
    python -m tests.preproduction_stress_100_airports
"""

from __future__ import annotations

from datetime import datetime, timedelta
import time

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.queue.pipeline as pipeline_module
import app.queue.engine as engine_module
from app.models import AircraftCapacity, Base
from app.queue.api import airport_directory
from app.queue.engine import run_predictions
from app.queue.ingestion.refresh import refresh_flights
from app.queue.ingestion.sources import parse_source_a
from app.queue.models import Airport, Flight, QueuePrediction
from app.service import AircraftCapacityService


AIRPORTS = [f"P{i:03d}" for i in range(100)]
BASE = datetime(2026, 9, 16, 6, 0)


def _fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _records() -> list[dict]:
    records = []
    for airport_index, airport in enumerate(AIRPORTS):
        for local_index in range(100):
            global_index = airport_index * 100 + local_index
            departure = BASE + timedelta(minutes=local_index * 6)
            international = local_index % 2 == 1
            destination = "FGN" if international else "DOM"
            records.append({
                "airline_iata": "TK",
                "flight_iata": f"TK{10000 + global_index}",
                "flight_number": str(10000 + global_index),
                "dep_iata": airport,
                "arr_iata": destination,
                "dep_time_utc": _fmt(departure),
                "dep_estimated_utc": _fmt(departure),
                "arr_time_utc": _fmt(departure + timedelta(minutes=90)),
                "aircraft_icao": "A320",
                "status": "scheduled",
            })
    return records


def _prediction_snapshot(session) -> list[tuple]:
    rows = session.execute(
        select(QueuePrediction).order_by(
            QueuePrediction.airport_iata,
            QueuePrediction.process,
            QueuePrediction.window_start,
        )
    ).scalars().all()
    return [
        (
            row.airport_iata,
            row.process,
            row.window_start,
            row.window_end,
            row.flight_count,
            row.expected_passengers,
            row.utilization,
            row.estimated_wait_minutes,
            row.risk,
        )
        for row in rows
    ]


def main() -> None:
    started = time.perf_counter()
    engine = create_engine("sqlite://")
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    seed = SessionLocal()
    seed.add_all([
        Airport(iata_code=code, airport_name=f"Airport {code}", country_code="TR")
        for code in AIRPORTS
    ])
    seed.add_all([
        Airport(iata_code="DOM", airport_name="Domestic Control", country_code="TR"),
        Airport(iata_code="FGN", airport_name="Foreign Control", country_code="DE"),
        AircraftCapacity(icao_code="A320", capacity=180, source="audit", confidence="high"),
        AircraftCapacity(icao_code="B77W", capacity=350, source="audit", confidence="high"),
    ])
    seed.commit()
    seed.close()

    original_init_db = pipeline_module.init_db
    original_get_session = pipeline_module.get_session
    original_domain_now = engine_module.domain_now
    pipeline_module.init_db = lambda drop_first=False: None
    pipeline_module.get_session = lambda: SessionLocal()
    engine_module.domain_now = lambda: datetime(2026, 9, 16, 12, 0)

    records = _records()

    def source_a(direction: str):
        return records if direction == "departure" else []

    try:
        t0_start = time.perf_counter()
        first = pipeline_module.run(
            source_a=source_a,
            source_b=lambda: [],
            update_baseline=False,
        )
        t0_seconds = time.perf_counter() - t0_start

        check = SessionLocal()
        flight_count_t0 = check.scalar(select(func.count()).select_from(Flight))
        airports_with_flights = check.scalar(
            select(func.count(func.distinct(Flight.airport_iata)))
        )
        directory_count = len(airport_directory(check))
        snapshot_t0 = _prediction_snapshot(check)
        control_before = [row for row in snapshot_t0 if row[0] == "P001"]
        check.close()

        t1_start = time.perf_counter()
        second = pipeline_module.run(
            source_a=source_a,
            source_b=lambda: [],
            update_baseline=False,
        )
        t1_seconds = time.perf_counter() - t1_start

        check = SessionLocal()
        flight_count_t1 = check.scalar(select(func.count()).select_from(Flight))
        snapshot_t1 = _prediction_snapshot(check)
        control_before = [row for row in snapshot_t1 if row[0] == "P001"]

        # Isolation probe: update one P000 flight through the real parser/refresh,
        # then recompute only P000. P001's persisted predictions must be unchanged.
        changed_source = dict(records[0])
        changed_source["aircraft_icao"] = "B77W"
        changed_row = parse_source_a(
            [changed_source], "departure", {"P000": "TR", "DOM": "TR"}, {}
        )
        refresh_flights(check, changed_row)
        run_predictions(
            check,
            AircraftCapacityService(check),
            airports=["P000"],
            update_baseline=False,
            now=datetime(2026, 9, 16, 12, 0),
        )
        control_after = [row for row in _prediction_snapshot(check) if row[0] == "P001"]
        check.close()
    finally:
        pipeline_module.init_db = original_init_db
        pipeline_module.get_session = original_get_session
        engine_module.domain_now = original_domain_now

    assertions = {
        "no_crash": True,
        "100_airports": airports_with_flights == 100,
        "10000_flights_t0": flight_count_t0 == 10_000,
        "10000_flights_t1": flight_count_t1 == 10_000,
        "no_duplicate_t1": second["inserted"] == 0 and second["updated"] == 10_000,
        "directory_has_all_100": directory_count == 100,
        "deterministic_predictions": snapshot_t0 == snapshot_t1,
        "airport_isolation": control_before == control_after,
        "no_failed_rows": first["failed"] == 0 and second["failed"] == 0,
        "no_failed_airports": not first["failed_airports"] and not second["failed_airports"],
    }

    total_seconds = time.perf_counter() - started
    print({
        "t0_seconds": round(t0_seconds, 3),
        "t1_seconds": round(t1_seconds, 3),
        "total_seconds": round(total_seconds, 3),
        "predictions": len(snapshot_t0),
        **assertions,
    })
    failed = [name for name, ok in assertions.items() if not ok]
    if failed:
        raise AssertionError(f"Stress audit failed: {failed}")


if __name__ == "__main__":
    main()
