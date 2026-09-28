
from __future__ import annotations

from datetime import datetime

from ..constants import DIRECTION_DEPARTURE

USAGE_HORIZON_HOURS = 48


def canonical_operational_time(
    direction: str,
    dep_scheduled_utc: datetime | None,
    arr_scheduled_utc: datetime | None,
) -> datetime | None:
    if direction == DIRECTION_DEPARTURE:
        return dep_scheduled_utc
    return arr_scheduled_utc


def canonical_flight_time(flight) -> datetime | None:
    return canonical_operational_time(
        flight.direction, flight.dep_scheduled_utc, flight.arr_scheduled_utc
    )
