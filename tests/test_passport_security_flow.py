"""
Madde 5.8 - International Departure: show-up -> Departure Passport ->
passport completion -> International Security. Security'nin arrival
zamanı passport'un ARRIVAL'ı değil, passport'un GERÇEK completion'ı
olmalı; aynı passenger show-up'tan security'ye duplicate edilmemeli.
"""
import pytest

from datetime import datetime

from app.queue.core.event_queue import simulate_international_departure_journey
from app.queue.domain.demand import DemandCalculator, departure_show_up_events

from .conftest import FakeCapacityResult, FakeResolver, make_departure


def test_international_departure_show_up_to_passport_to_security_chain():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=30)})
    demand = DemandCalculator(resolver)
    flight = make_departure(
        when=datetime(2026, 3, 10, 12, 0), location="international", aircraft_icao="A321"
    )

    total_demand = demand.passenger_demand(flight)
    assert total_demand == 30

    show_up_events = departure_show_up_events(flight, total_demand)
    assert show_up_events

    result = simulate_international_departure_journey(
        departure_arrivals=show_up_events,
        arrival_arrivals=[],
        passport_departure_server_count=2,
        passport_arrival_server_count=2,
        passport_service_time_minutes=1.5,
        international_security_lane_count=2,
        security_service_time_minutes=0.4,
    )

    passport_departure_events = result["passport_departure"]
    security_events = result["security"]

    assert passport_departure_events
    assert security_events

    # Security'nin arrival_time'ı GERÇEKTEN passport'un completion_time'ı
    # olmalı - passport'un KENDİ arrival_time'ı (show-up anı) DEĞİL.
    passport_completions = sorted(e.completion_time for e in passport_departure_events)
    security_arrivals = sorted(e.arrival_time for e in security_events)
    assert passport_completions == security_arrivals

    # Conservation: show-up -> passport -> security zincirinde hiçbir
    # yolcu kaybolmuyor/duplicate edilmiyor.
    assert sum(c for _, c in show_up_events) == pytest.approx(
        sum(e.count for e in passport_departure_events)
    )
    assert sum(e.count for e in passport_departure_events) == pytest.approx(
        sum(e.count for e in security_events)
    )

    # Arrival-bağlantılı yolculuk (bu testte hiç yok) security'ye HİÇ
    # girmemeli.
    assert result["passport_arrival"] == []
