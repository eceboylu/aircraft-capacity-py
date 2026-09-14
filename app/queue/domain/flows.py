"""
AŞAMA 2 - Hangi akış hangi kuyruğu besler.

  domestic_departure      -> security
  international_departure -> security + passport
  international_arrival   -> passport
  domestic_arrival        -> HİÇBİR kuyruğu beslemez

domestic_arrival için ayrı bir akış anahtarı üretilmez; raporlama
katmanında da yer almaz (AŞAMA 9).

direction ve location alanları ingestion sırasında bir kez yazılır;
burada YENİDEN HESAPLANMAZ, doğrudan okunur.
"""

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

# Raporlamada görünen akışlar - domestic_arrival bilinçli olarak YOK.
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


def flow_of(flight) -> str | None:
    """Uçuşun ait olduğu akış; domestic arrival için None."""
    if is_domestic_departure(flight):
        return FLOW_DOMESTIC_DEPARTURE
    if is_international_departure(flight):
        return FLOW_INTERNATIONAL_DEPARTURE
    if is_international_arrival(flight):
        return FLOW_INTERNATIONAL_ARRIVAL
    return None


def security_flights(flights: Sequence) -> list:
    """Security kuyruğunu besleyen uçuşlar: TÜM kalkışlar."""
    return [f for f in flights if f.direction == DIRECTION_DEPARTURE]


def passport_flights(flights: Sequence) -> list:
    """
    Passport kuyruğunu besleyen uçuşlar:
    uluslararası kalkış + uluslararası varış.
    """
    return [
        f for f in flights
        if is_international_departure(f) or is_international_arrival(f)
    ]
