"""
Madde 5.13 - fine-grained/event-driven show-up event'lerinin saatlik
grafiğe aggregate edilmesi: passenger total korunmalı, passenger-
ağırlıklı ortalama wait korunmalı, hour boundary'ler doğru olmalı.

Bu test `engine.py:predict_airport()`'u (gerçek orkestrasyon
fonksiyonu) çağırır ve sonucu, AYNI production fonksiyonlarını
(`departure_show_up_events`, `simulate_fifo_queue`) bağımsızca tekrar
çağırarak elde edilen GERÇEK ServiceEvent'lerden türetilen beklenen
değerle karşılaştırır - algoritmanın bir kopyası YAZILMIYOR.
"""
from datetime import datetime

from app.queue.config import default_config
from app.queue.constants import PROCESS_PASSPORT_DEPARTURE
from app.queue.core.event_queue import simulate_fifo_queue
from app.queue.domain.demand import DemandCalculator, departure_show_up_events
from app.queue.engine import floor_to_window, predict_airport

from .conftest import FakeCapacityResult, FakeResolver, make_departure


def test_hourly_passport_departure_conserves_passengers_and_weighted_wait():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=60)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")

    flight = make_departure(
        when=datetime(2026, 3, 10, 9, 30), location="international", aircraft_icao="A321"
    )

    total_demand = demand.passenger_demand(flight)
    assert total_demand == 60

    predictions = predict_airport("IST", [flight], config, demand)
    dep_rows = [p for p in predictions if p.process == PROCESS_PASSPORT_DEPARTURE]
    assert dep_rows

    # Passenger conservation across ALL hourly rows for this process.
    assert sum(p.expected_passengers for p in dep_rows) == total_demand

    # Ground truth: AYNI production fonksiyonlarıyla (kopyalamadan)
    # bağımsızca üretilen GERÇEK ServiceEvent'ler.
    show_up_events = departure_show_up_events(flight, total_demand)
    events = simulate_fifo_queue(
        [(t, "departure", c) for t, c in show_up_events],
        server_count=config.passport_departure_server_count,
        service_time_minutes=config.passport_service_time_minutes,
    )
    assert sum(e.count for e in events) == total_demand

    weighted_sum_by_hour: dict = {}
    count_by_hour: dict = {}
    for event in events:
        hour = floor_to_window(event.arrival_time, 60)
        weighted_sum_by_hour[hour] = weighted_sum_by_hour.get(hour, 0.0) + (
            event.wait_minutes * event.count
        )
        count_by_hour[hour] = count_by_hour.get(hour, 0.0) + event.count

    assert sum(count_by_hour.values()) == total_demand

    for row in dep_rows:
        expected_count = count_by_hour.get(row.window_start, 0.0)
        assert row.expected_passengers == expected_count
        if expected_count > 0:
            expected_wait = round(
                weighted_sum_by_hour[row.window_start] / expected_count, 1
            )
            assert row.estimated_wait_minutes == expected_wait

    # Hour boundaries doğru: her satırın window_end'i window_start + 1
    # saat.
    for row in dep_rows:
        assert (row.window_end - row.window_start).total_seconds() == 3600
