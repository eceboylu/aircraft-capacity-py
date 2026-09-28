
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.exc import IntegrityError

from .models import BaselineObservation, HistoricalFlightCount

BASELINE_KEY_LOOKUP_CHUNK_SIZE = 500


def existing_baseline_observation_keys(
    session, keys: list[tuple[str, str, datetime]]
) -> set[tuple[str, str, datetime]]:
    if not keys:
        return set()

    found: set[tuple[str, str, datetime]] = set()
    key_tuple = tuple_(
        BaselineObservation.airport_iata,
        BaselineObservation.process,
        BaselineObservation.window_start,
    )
    for start in range(0, len(keys), BASELINE_KEY_LOOKUP_CHUNK_SIZE):
        chunk = keys[start:start + BASELINE_KEY_LOOKUP_CHUNK_SIZE]
        rows = session.execute(
            select(
                BaselineObservation.airport_iata,
                BaselineObservation.process,
                BaselineObservation.window_start,
            ).where(key_tuple.in_(chunk))
        ).all()
        found.update(tuple(row) for row in rows)
    return found


def _bucket(
    session,
    airport_iata: str,
    process: str,
    hour_of_day: int,
    day_of_week: int,
) -> HistoricalFlightCount | None:
    return session.execute(
        select(HistoricalFlightCount).where(
            HistoricalFlightCount.airport_iata == airport_iata,
            HistoricalFlightCount.process == process,
            HistoricalFlightCount.hour_of_day == hour_of_day,
            HistoricalFlightCount.day_of_week == day_of_week,
        )
    ).scalar_one_or_none()


def get_baseline(
    session,
    airport_iata: str,
    process: str,
    hour_of_day: int,
    day_of_week: int,
) -> float | None:
    row = _bucket(session, airport_iata, process, hour_of_day, day_of_week)

    if row is None or row.sample_size <= 0:
        return None
    return row.average_flight_count


def get_passenger_baseline(
    session,
    airport_iata: str,
    process: str,
    hour_of_day: int,
    day_of_week: int,
) -> float | None:
    row = _bucket(session, airport_iata, process, hour_of_day, day_of_week)

    if row is None or row.passenger_sample_size <= 0:
        return None
    return row.average_expected_passengers


def record_observation(
    session,
    airport_iata: str,
    process: str,
    window_start: datetime,
    hour_of_day: int,
    day_of_week: int,
    flight_count: int,
    expected_passengers: int | None = None,
) -> float:
    now = datetime.now(timezone.utc)

    session.add(BaselineObservation(
        airport_iata=airport_iata,
        process=process,
        window_start=window_start,
        recorded_at=now,
    ))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = _bucket(
            session, airport_iata, process, hour_of_day, day_of_week
        )
        return existing.average_flight_count if existing else float(flight_count)

    existing = _bucket(session, airport_iata, process, hour_of_day, day_of_week)

    if existing is None:
        session.merge(HistoricalFlightCount(
            airport_iata=airport_iata,
            process=process,
            hour_of_day=hour_of_day,
            day_of_week=day_of_week,
            average_flight_count=float(flight_count),
            sample_size=1,
            average_expected_passengers=(
                float(expected_passengers)
                if expected_passengers is not None else None
            ),
            passenger_sample_size=1 if expected_passengers is not None else 0,
            last_updated=now,
        ))
        session.commit()
        return float(flight_count)

    total = existing.average_flight_count * existing.sample_size + flight_count
    new_sample_size = existing.sample_size + 1
    new_average = total / new_sample_size

    existing.average_flight_count = new_average
    existing.sample_size = new_sample_size

    if expected_passengers is not None:
        old_passenger_total = (
            (existing.average_expected_passengers or 0.0)
            * existing.passenger_sample_size
        )
        new_passenger_sample = existing.passenger_sample_size + 1
        existing.average_expected_passengers = (
            (old_passenger_total + expected_passengers) / new_passenger_sample
        )
        existing.passenger_sample_size = new_passenger_sample

    existing.last_updated = now
    session.commit()

    return new_average


def reset_baseline_pool(session) -> dict:
    before_hist = session.scalar(
        select(func.count()).select_from(HistoricalFlightCount)
    ) or 0
    before_obs = session.scalar(
        select(func.count()).select_from(BaselineObservation)
    ) or 0

    session.execute(delete(HistoricalFlightCount))
    session.execute(delete(BaselineObservation))
    session.commit()

    return {
        "historical_flight_counts_deleted": before_hist,
        "baseline_observations_deleted": before_obs,
    }


def main(argv: list[str] | None = None) -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m app.queue.baseline",
        description=(
            "Baseline havuzu (HistoricalFlightCount + BaselineObservation) "
            "yönetimi. Argümansız çalıştırma HİÇBİR ŞEY SİLMEZ."
        ),
    )
    parser.add_argument(
        "--reset-pool",
        action="store_true",
        help=(
            "SADECE HistoricalFlightCount + BaselineObservation'ı "
            "temizler - Flight/Airport/QueuePrediction/aircraft_capacity "
            "dahil hiçbir başka tabloya DOKUNMAZ. Pencere genişliği "
            "değiştiğinde (ör. 15dk -> 60dk) eski/yeni ölçek aynı "
            "havuzda karışmasın diye kullanılır."
        ),
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.reset_pool:
        parser.print_help()
        return

    from ..db import get_session, init_db
    from ..logging_config import configure_logging

    configure_logging()
    init_db()
    session = get_session()
    try:
        result = reset_baseline_pool(session)
    finally:
        session.close()

    print(f"historical_flight_counts_deleted: {result['historical_flight_counts_deleted']}")
    print(f"baseline_observations_deleted: {result['baseline_observations_deleted']}")


if __name__ == "__main__":
    main()
