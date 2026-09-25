"""
Madde 1 - departure show-up: interval-overlap ile 5 dakikalık global
bucket'lara dağıtım, conservation, ve non-aligned departure davranışı.
"""
from datetime import datetime, timedelta

import pytest

from app.queue.constants import DEPARTURE_SHOW_UP_PROFILE
from app.queue.domain.demand import departure_show_up_events

from .conftest import make_departure


@pytest.mark.parametrize("total_demand", [300, 220, 1, 2, 3, 7, 30, 47, 5])
def test_show_up_conservation_for_various_passenger_counts(total_demand):
    flight = make_departure(when=datetime(2026, 3, 10, 12, 40))
    events = departure_show_up_events(flight, total_demand)

    assert sum(count for _, count in events) == total_demand
    assert all(count > 0 for _, count in events)


def test_12_40_300_exact_acceptance_example():
    """Görev.md'nin MUTLAKA geçmesi gereken kabul kriteri."""
    flight = make_departure(when=datetime(2026, 3, 10, 12, 40))
    events = departure_show_up_events(flight, 300)

    hourly: dict[datetime, float] = {}
    for moment, count in events:
        hour_key = moment.replace(minute=0, second=0, microsecond=0)
        hourly[hour_key] = hourly.get(hour_key, 0.0) + count

    day = datetime(2026, 3, 10)
    expected = {
        day.replace(hour=8): 10,
        day.replace(hour=9): 55,
        day.replace(hour=10): 115,
        day.replace(hour=11): 100,
        day.replace(hour=12): 20,
    }

    assert hourly == expected
    assert sum(hourly.values()) == 300


def test_non_aligned_departure_minute_still_conserves_and_respects_profile_start():
    """
    12:43 gibi 5'in katı OLMAYAN bir departure - global bucket'lar
    flight'ın dakikasına göre değil mutlak saate göre hizalanmalı, ve
    hiçbir yolcu kendi show-up bandı başlamadan sisteme girmemeli.
    """
    base = datetime(2026, 3, 10, 12, 43)
    flight = make_departure(when=base)
    total_demand = 300
    events = departure_show_up_events(flight, total_demand)

    # Conservation korunuyor.
    assert sum(count for _, count in events) == total_demand

    # Hiçbir passenger, KENDİ bandının başlangıcından önce show-up
    # etmiyor (en erken izin verilen an: T-4h, yani ilk bandın başı).
    earliest_allowed = base - timedelta(minutes=DEPARTURE_SHOW_UP_PROFILE[0][0])
    assert all(moment >= earliest_allowed for moment, _ in events)

    # Hour-boundary overlap matematiksel olarak doğru: saatlik toplamlar
    # da tam toplamı korumalı (hiçbir passenger kaybolmuyor/double
    # count olmuyor).
    hourly: dict[datetime, float] = {}
    for moment, count in events:
        hour_key = moment.replace(minute=0, second=0, microsecond=0)
        hourly[hour_key] = hourly.get(hour_key, 0.0) + count
    assert sum(hourly.values()) == total_demand

    # Departure zamanının kendisinden SONRA hiçbir show-up event'i yok
    # (son bant T-1h..T0'da biter).
    assert all(moment < base for moment, _ in events)


def test_no_reference_time_returns_empty():
    flight = make_departure(when=None)
    assert departure_show_up_events(flight, 100) == []


def test_zero_or_negative_demand_returns_empty():
    flight = make_departure(when=datetime(2026, 3, 10, 12, 0))
    assert departure_show_up_events(flight, 0) == []
    assert departure_show_up_events(flight, -5) == []
