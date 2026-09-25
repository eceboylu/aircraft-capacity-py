"""
Madde 6/5.7 - flow routing regresyon testi (bu task routing'e
dokunmadı, ama korunduğunu kilitler):

    domestic departure      -> domestic security
    international departure -> departure passport (+ international security)
    international arrival   -> arrival passport
    domestic arrival        -> modeled queue yok
"""
from datetime import datetime

from app.queue.domain.flows import (
    FLOW_DOMESTIC_DEPARTURE,
    FLOW_INTERNATIONAL_ARRIVAL,
    FLOW_INTERNATIONAL_DEPARTURE,
    flow_of,
    passport_arrival_flights,
    passport_departure_flights,
    passport_flights,
    security_domestic_flights,
    security_international_flights,
)

from .conftest import make_arrival, make_departure

WHEN = datetime(2026, 3, 10, 9, 0)


def test_domestic_departure_routes_to_domestic_security_only():
    flight = make_departure(when=WHEN, location="domestic")

    assert flow_of(flight) == FLOW_DOMESTIC_DEPARTURE
    assert flight in security_domestic_flights([flight])
    assert flight not in security_international_flights([flight])
    assert flight not in passport_flights([flight])


def test_international_departure_routes_to_departure_passport_and_intl_security():
    flight = make_departure(when=WHEN, location="international")

    assert flow_of(flight) == FLOW_INTERNATIONAL_DEPARTURE
    assert flight in security_international_flights([flight])
    assert flight in passport_flights([flight])
    assert flight in passport_departure_flights([flight])
    assert flight not in security_domestic_flights([flight])
    assert flight not in passport_arrival_flights([flight])


def test_international_arrival_routes_to_arrival_passport():
    flight = make_arrival(when=WHEN, location="international")

    assert flow_of(flight) == FLOW_INTERNATIONAL_ARRIVAL
    assert flight in passport_flights([flight])
    assert flight in passport_arrival_flights([flight])
    assert flight not in passport_departure_flights([flight])
    assert flight not in security_domestic_flights([flight])
    assert flight not in security_international_flights([flight])


def test_domestic_arrival_has_no_modeled_queue():
    flight = make_arrival(when=WHEN, location="domestic")

    assert flow_of(flight) is None
    assert flight not in passport_flights([flight])
    assert flight not in security_domestic_flights([flight])
    assert flight not in security_international_flights([flight])
