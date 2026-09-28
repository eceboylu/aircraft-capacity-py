
from ..constants import (
    DIRECTION_DEPARTURE,
    LOAD_FACTOR_DOMESTIC,
    LOAD_FACTOR_INTL_LONG,
    LOAD_FACTOR_INTL_MEDIUM,
    LOAD_FACTOR_INTL_SHORT,
    LOAD_FACTOR_INTL_UNKNOWN,
    LOCATION_DOMESTIC,
    RANGE_LONG_HAUL_MINUTES,
    RANGE_MEDIUM_HAUL_MINUTES,
    SECURITY_BUFFER_LONG_MINUTES,
    SECURITY_BUFFER_MEDIUM_MINUTES,
    SECURITY_BUFFER_SHORT_MINUTES,
    SECURITY_BUFFER_UNKNOWN_MINUTES,
)


def estimated_duration_minutes(flight) -> int | None:
    if flight.dep_scheduled_utc and flight.arr_scheduled_utc:
        delta = flight.arr_scheduled_utc - flight.dep_scheduled_utc
        return int(delta.total_seconds() / 60)
    return None


def route_based_load_factor(flight) -> float:
    if flight.location == LOCATION_DOMESTIC:
        return LOAD_FACTOR_DOMESTIC

    duration = estimated_duration_minutes(flight)
    if duration is None:
        return LOAD_FACTOR_INTL_UNKNOWN
    if duration > RANGE_LONG_HAUL_MINUTES:
        return LOAD_FACTOR_INTL_LONG
    if duration > RANGE_MEDIUM_HAUL_MINUTES:
        return LOAD_FACTOR_INTL_MEDIUM
    return LOAD_FACTOR_INTL_SHORT


def security_arrival_buffer_minutes(flight) -> int:
    duration = estimated_duration_minutes(flight)
    if duration is None:
        return SECURITY_BUFFER_UNKNOWN_MINUTES
    if duration > RANGE_LONG_HAUL_MINUTES:
        return SECURITY_BUFFER_LONG_MINUTES
    if duration > RANGE_MEDIUM_HAUL_MINUTES:
        return SECURITY_BUFFER_MEDIUM_MINUTES
    return SECURITY_BUFFER_SHORT_MINUTES


def delay_minutes(flight) -> int:
    if flight.direction == DIRECTION_DEPARTURE:
        scheduled = flight.dep_scheduled_utc
        actual = flight.dep_actual_utc or flight.dep_estimated_utc
    else:
        scheduled = flight.arr_scheduled_utc
        actual = flight.arr_actual_utc or flight.arr_estimated_utc

    if scheduled and actual:
        return int((actual - scheduled).total_seconds() / 60)
    return 0
