
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..constants import (
    DELAY_SIGNIFICANT_MINUTES,
    EVENT_AIRCRAFT_CHANGED,
    EVENT_CANCELLED,
    EVENT_DELAYED,
    EVENT_DIVERTED,
    EXCLUDED_STATUSES,
    STATUS_CANCELLED,
    STATUS_DIVERTED,
)
from ..domain.demand import effective_time
from ..domain.flight_rules import delay_minutes
from ..models import Flight, FlightEvent

logger = logging.getLogger(__name__)

REFRESH_CHUNK_SIZE = 500

_MUTABLE_FLIGHT_FIELDS = (
    "airport_iata", "direction", "location", "requires_passport",
    "airline_iata", "flight_number",
    "flight_iata", "aircraft_icao", "aircraft_match_found", "dep_iata", "arr_iata",
    "dep_scheduled_utc", "dep_estimated_utc", "dep_actual_utc",
    "arr_scheduled_utc", "arr_estimated_utc", "arr_actual_utc",
    "dep_terminal", "dep_gate", "arr_terminal", "arr_gate",
)


class _RowView:

    def __init__(self, row: dict):
        self.__dict__.update(row)


def _detect_changes(
    existing: Flight, row: dict
) -> list[tuple[str, str | None, str | None, datetime | None]]:
    events: list[tuple[str, str | None, str | None, datetime | None]] = []

    old_icao = existing.aircraft_icao
    new_icao = row.get("aircraft_icao")
    if old_icao and new_icao and old_icao != new_icao:
        events.append((
            EVENT_AIRCRAFT_CHANGED, old_icao, new_icao,
            effective_time(_RowView(row)),
        ))

    old_status = (existing.status or "").lower()
    new_status = (row.get("status") or "").lower()
    if old_status != new_status:
        if new_status == STATUS_CANCELLED:
            events.append((EVENT_CANCELLED, old_status, new_status, None))
        elif new_status == STATUS_DIVERTED:
            events.append((EVENT_DIVERTED, old_status, new_status, None))

    old_delay = delay_minutes(existing)
    new_delay = delay_minutes(_RowView(row))
    crossed_threshold = (
        new_delay > DELAY_SIGNIFICANT_MINUTES
        and old_delay <= DELAY_SIGNIFICANT_MINUTES
    )
    grew_further = (
        new_delay > DELAY_SIGNIFICANT_MINUTES
        and old_delay > DELAY_SIGNIFICANT_MINUTES
        and new_delay != old_delay
    )
    if crossed_threshold or grew_further:
        events.append((EVENT_DELAYED, str(old_delay), str(new_delay), None))

    return events


def _aircraft_change_already_recorded(
    session,
    flight_key: str,
    old_icao: str | None,
    new_icao: str | None,
    flight_effective_time: datetime | None,
) -> bool:
    existing = session.execute(
        select(FlightEvent.id).where(
            FlightEvent.flight_key == flight_key,
            FlightEvent.event_type == EVENT_AIRCRAFT_CHANGED,
            FlightEvent.old_value == old_icao,
            FlightEvent.new_value == new_icao,
            FlightEvent.flight_effective_time == flight_effective_time,
        )
    ).first()
    return existing is not None


def _resolved_status(existing: Flight | None, row: dict) -> str | None:
    new_status = row.get("status")
    if existing is not None:
        old_status = (existing.status or "").lower()
        if old_status in EXCLUDED_STATUSES and (new_status or "").lower() not in EXCLUDED_STATUSES:
            return existing.status
    return new_status


def _row_differs_from_existing(existing: Flight, row: dict, resolved_status) -> bool:
    if (existing.status or None) != resolved_status:
        return True
    for name in _MUTABLE_FLIGHT_FIELDS:
        if getattr(existing, name) != row.get(name):
            return True
    return False


def _apply_row_to_flight(flight: Flight, row: dict, resolved_status, now: datetime) -> None:
    for name in _MUTABLE_FLIGHT_FIELDS:
        setattr(flight, name, row.get(name))
    flight.status = resolved_status
    flight.last_refreshed_at = now


def _detect_and_stage_events(session, existing: Flight, row: dict, now: datetime) -> int:
    flight_key = row["flight_key"]
    written = 0
    for event_type, old_value, new_value, flight_eff_time in _detect_changes(existing, row):
        if event_type == EVENT_AIRCRAFT_CHANGED and _aircraft_change_already_recorded(
            session, flight_key, old_value, new_value, flight_eff_time
        ):
            continue
        session.add(FlightEvent(
            flight_key=flight_key,
            airport_iata=row["airport_iata"],
            event_type=event_type,
            old_value=old_value,
            new_value=new_value,
            flight_effective_time=flight_eff_time,
            detected_at=now,
        ))
        written += 1
    return written


def _process_one_row(session, existing_by_key: dict, row: dict, now: datetime) -> dict:
    flight_key = row["flight_key"]
    existing = existing_by_key.get(flight_key)

    if existing is None:
        flight = Flight(flight_key=flight_key)
        resolved_status = _resolved_status(None, row)
        _apply_row_to_flight(flight, row, resolved_status, now)
        session.add(flight)
        existing_by_key[flight_key] = flight
        return {"inserted": 1, "updated": 0, "events_written": 0}

    resolved_status = _resolved_status(existing, row)
    events_written = _detect_and_stage_events(session, existing, row, now)
    if _row_differs_from_existing(existing, row, resolved_status):
        _apply_row_to_flight(existing, row, resolved_status, now)
    return {"inserted": 0, "updated": 1, "events_written": events_written}


def _bulk_fetch_existing(session, flight_keys: list[str]) -> dict:
    if not flight_keys:
        return {}
    rows = session.execute(
        select(Flight).where(Flight.flight_key.in_(flight_keys))
    ).scalars().all()
    return {f.flight_key: f for f in rows}


def _process_chunk_serial_with_savepoints(session, chunk: list[dict], now: datetime) -> dict:
    keys = [row["flight_key"] for row in chunk]
    existing_by_key = _bulk_fetch_existing(session, keys)

    inserted = updated = events_written = failed = 0
    for row in chunk:
        savepoint = session.begin_nested()
        try:
            result = _process_one_row(session, existing_by_key, row, now)
            session.flush()
        except Exception:
            savepoint.rollback()
            failed += 1
            logger.exception(
                "Uçuş kaydı işlenemedi, bu satır atlanıyor (flight_key=%s)",
                row.get("flight_key", "?"),
            )
            continue
        savepoint.commit()
        inserted += result["inserted"]
        updated += result["updated"]
        events_written += result["events_written"]

    session.commit()
    return {"inserted": inserted, "updated": updated, "events_written": events_written, "failed": failed}


def _process_chunk(session, chunk: list[dict], now: datetime) -> dict:
    keys = [row["flight_key"] for row in chunk]
    existing_by_key = _bulk_fetch_existing(session, keys)

    inserted = updated = events_written = 0
    try:
        for row in chunk:
            result = _process_one_row(session, existing_by_key, row, now)
            inserted += result["inserted"]
            updated += result["updated"]
            events_written += result["events_written"]
        session.flush()
    except Exception:
        session.rollback()
        logger.warning(
            "refresh chunk toplu yol başarısız oldu (%d satır) - satır-satır "
            "SAVEPOINT fallback'ine geçiliyor (izolasyon korunuyor, hız düşüyor)",
            len(chunk),
        )
        return _process_chunk_serial_with_savepoints(session, chunk, now)

    session.commit()
    return {"inserted": inserted, "updated": updated, "events_written": events_written, "failed": 0}


def refresh_flights(session, rows: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    inserted = updated = events_written = failed = 0

    for start in range(0, len(rows), REFRESH_CHUNK_SIZE):
        chunk = rows[start:start + REFRESH_CHUNK_SIZE]
        result = _process_chunk(session, chunk, now)
        inserted += result["inserted"]
        updated += result["updated"]
        events_written += result["events_written"]
        failed += result["failed"]

    return {
        "inserted": inserted,
        "updated": updated,
        "events_written": events_written,
        "failed": failed,
    }


def aircraft_changes_for_airport(
    session, airport_iata: str
) -> dict[str, list[tuple[str | None, str | None, datetime | None]]]:
    rows = session.execute(
        select(FlightEvent)
        .where(
            FlightEvent.airport_iata == airport_iata,
            FlightEvent.event_type == EVENT_AIRCRAFT_CHANGED,
        )
        .order_by(FlightEvent.flight_effective_time, FlightEvent.detected_at)
    ).scalars().all()

    changes: dict[str, list[tuple[str | None, str | None, datetime | None]]] = {}
    for event in rows:
        changes.setdefault(event.flight_key, []).append(
            (event.old_value, event.new_value, event.flight_effective_time)
        )
    return changes
