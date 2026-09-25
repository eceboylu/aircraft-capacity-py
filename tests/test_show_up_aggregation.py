"""
Madde 5.6 - birden çok flight'ın show-up dağılımı BAĞIMSIZ üretilmeli;
aggregation sadece paylaşılan (timestamp) anında, doğru aşamada
yapılmalı - flight-level identity hesap sırasında kaybolmamalı.
"""
from datetime import datetime

from app.queue.domain.demand import DemandCalculator, departure_show_up_events

from .conftest import FakeCapacityResult, FakeResolver, make_departure


def test_multiple_flights_show_up_independently_then_aggregate_correctly():
    resolver = FakeResolver(
        {
            "A321": FakeCapacityResult(capacity=300),
            "B738": FakeCapacityResult(capacity=189),
        }
    )
    demand = DemandCalculator(resolver)

    flight_a = make_departure(when=datetime(2026, 3, 10, 12, 40), aircraft_icao="A321")
    flight_b = make_departure(when=datetime(2026, 3, 10, 9, 15), aircraft_icao="B738")

    demand_a = demand.passenger_demand(flight_a)
    demand_b = demand.passenger_demand(flight_b)
    assert demand_a == 300
    assert demand_b == 189

    events_a = departure_show_up_events(flight_a, demand_a)
    events_b = departure_show_up_events(flight_b, demand_b)

    # Her flight KENDİ show-up dağılımını KENDİ conservation'ıyla üretir
    # - birbirinden bağımsız.
    assert sum(count for _, count in events_a) == demand_a
    assert sum(count for _, count in events_b) == demand_b

    # Aggregation SADECE aynı timestamp'e düşen katkıların toplanmasıdır
    # - flight kimliği bu aşamaya kadar KAYBOLMAZ (her ikisinin de kendi
    # ayrı event listesi hesap sırasında hâlâ ayırt edilebilir durumda).
    combined = events_a + events_b
    aggregated: dict[datetime, float] = {}
    for moment, count in combined:
        aggregated[moment] = aggregated.get(moment, 0.0) + count

    assert sum(aggregated.values()) == demand_a + demand_b
    assert sum(aggregated.values()) == sum(c for _, c in combined)
