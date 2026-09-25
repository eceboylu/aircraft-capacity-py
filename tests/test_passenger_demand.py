"""
Madde 2 - passenger demand SADECE resolved ICAO kapasitesi olmalı;
occupancy factor / route-based load factor production zincirine
KARIŞMAMALI. GA (general aviation) hariç tutma davranışı korunmalı.
"""
from datetime import datetime

from app.queue.domain.demand import DemandCalculator
from app.queue.domain.flight_rules import route_based_load_factor
from app.queue.domain.occupancy_calibration import occupancy_factor_for

from .conftest import FakeCapacityResult, FakeResolver, make_departure

WHEN = datetime(2026, 3, 10, 9, 0)


def test_passenger_demand_equals_resolved_icao_capacity():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=220)})
    demand = DemandCalculator(resolver)
    flight = make_departure(when=WHEN, airport_iata="IST", aircraft_icao="A321")

    assert demand.passenger_demand(flight) == 220


def test_passenger_demand_not_scaled_by_airport_occupancy_factor():
    # IST'in occupancy_calibration.py'de GERÇEK, 1.0'dan FARKLI bir
    # faktörü var - eğer biri yanlışlıkla bu faktörü tekrar production
    # demand'ine bağlarsa bu test yakalar (156 != 220).
    factor, level = occupancy_factor_for("IST")
    assert level == "airport_specific"
    assert factor != 1.0

    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=220)})
    demand = DemandCalculator(resolver)
    flight = make_departure(when=WHEN, airport_iata="IST", aircraft_icao="A321")

    result = demand.passenger_demand(flight)
    assert result == 220
    assert result != round(220 * factor)


def test_passenger_demand_not_scaled_by_route_based_load_factor():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=220)})
    demand = DemandCalculator(resolver)
    # Bilinmeyen bir havalimanı (occupancy_factor_for -> 1.0 fallback)
    # kullanılıyor ki tek fark route load factor olsun.
    flight = make_departure(
        when=WHEN, location="domestic", airport_iata="ZZZ", aircraft_icao="A321"
    )

    factor = route_based_load_factor(flight)
    assert factor != 1.0

    result = demand.passenger_demand(flight)
    assert result == 220
    assert result != round(220 * factor)


def test_general_aviation_excluded_has_zero_demand():
    resolver = FakeResolver(
        {"C172": FakeCapacityResult(capacity=0, counts_toward_passenger_total=False)}
    )
    demand = DemandCalculator(resolver)
    flight = make_departure(when=WHEN, aircraft_icao="C172")

    assert demand.passenger_demand(flight) == 0
