
from typing import Sequence

from ..constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
)

FLOW_DOMESTIC_DEPARTURE = "domestic_departure"
FLOW_INTERNATIONAL_DEPARTURE = "international_departure"
FLOW_INTERNATIONAL_ARRIVAL = "international_arrival"

REPORTED_FLOWS = (
    FLOW_DOMESTIC_DEPARTURE,
    FLOW_INTERNATIONAL_DEPARTURE,
    FLOW_INTERNATIONAL_ARRIVAL,
)


def is_domestic_departure(flight) -> bool:
    return (
        flight.direction == DIRECTION_DEPARTURE
        and flight.location == LOCATION_DOMESTIC
    )


def is_international_departure(flight) -> bool:
    return (
        flight.direction == DIRECTION_DEPARTURE
        and flight.location == LOCATION_INTERNATIONAL
    )


def is_international_arrival(flight) -> bool:
    return (
        flight.direction == DIRECTION_ARRIVAL
        and flight.location == LOCATION_INTERNATIONAL
    )


def _requires_passport(flight) -> bool:
    return getattr(flight, "requires_passport", True)


def is_international_departure_requiring_passport(flight) -> bool:
    return is_international_departure(flight) and _requires_passport(flight)


def is_international_arrival_requiring_passport(flight) -> bool:
    return is_international_arrival(flight) and _requires_passport(flight)


def is_schengen_departure_skipping_passport(flight) -> bool:
    return is_international_departure(flight) and not _requires_passport(flight)


def flow_of(flight) -> str | None:
    if is_domestic_departure(flight):
        return FLOW_DOMESTIC_DEPARTURE
    if is_international_departure(flight):
        return FLOW_INTERNATIONAL_DEPARTURE
    if is_international_arrival(flight):
        return FLOW_INTERNATIONAL_ARRIVAL
    return None


def security_flights(flights: Sequence) -> list:
    return [f for f in flights if f.direction == DIRECTION_DEPARTURE]


def security_domestic_flights(flights: Sequence) -> list:
    return [f for f in flights if is_domestic_departure(f)]


def security_international_flights(flights: Sequence) -> list:
    return [f for f in flights if is_international_departure(f)]


def passport_flights(flights: Sequence) -> list:
    return [
        f for f in flights
        if is_international_departure_requiring_passport(f)
        or is_international_arrival_requiring_passport(f)
    ]


def passport_departure_flights(flights: Sequence) -> list:
    return [f for f in flights if is_international_departure_requiring_passport(f)]


def passport_arrival_flights(flights: Sequence) -> list:
    return [f for f in flights if is_international_arrival_requiring_passport(f)]
