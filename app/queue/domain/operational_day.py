
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..constants import DIRECTION_DEPARTURE


def resolve_airport_timezone(timezone_name: str | None) -> ZoneInfo | None:
    if not timezone_name:
        return None
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def flight_reference_time(flight) -> datetime | None:
    if flight.direction == DIRECTION_DEPARTURE:
        return flight.dep_actual_utc or flight.dep_estimated_utc or flight.dep_scheduled_utc
    return flight.arr_actual_utc or flight.arr_estimated_utc or flight.arr_scheduled_utc


def operational_day_window(tz: ZoneInfo, now_utc: datetime, day_offset: int = 0) -> tuple[datetime, datetime]:
    aware_now = now_utc.replace(tzinfo=timezone.utc)
    local_now = aware_now.astimezone(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_midnight += timedelta(days=day_offset)
    local_next_midnight = local_midnight + timedelta(days=1)

    start_utc = local_midnight.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = local_next_midnight.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def operational_date(tz: ZoneInfo, now_utc: datetime) -> date:
    aware_now = now_utc.replace(tzinfo=timezone.utc)
    return aware_now.astimezone(tz).date()


def filter_flights_for_operational_day(flights, tz: ZoneInfo, now_utc: datetime) -> list:
    start_utc, end_utc = operational_day_window(tz, now_utc)
    selected = []
    for flight in flights:
        reference = flight_reference_time(flight)
        if reference is None:
            continue
        if start_utc <= reference < end_utc:
            selected.append(flight)
    return selected
