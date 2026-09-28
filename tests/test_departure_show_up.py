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
        day.replace(hour=9): 65,
        day.replace(hour=10): 130,
        day.replace(hour=11): 85,
        day.replace(hour=12): 10,
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


def test_multi_flight_hour_contribution_from_different_percentage_segments():
    """
    12:00-13:00 aralığı SADECE 12:00'da kalkan uçuşlardan oluşmaz -
    farklı saatlerdeki (ve 15:20 gibi ara saatli) uçuşların FARKLI
    profil segmentlerinden gelen katkıları birleşir. cap=180 sabit.
    """
    day = datetime(2026, 3, 10)
    flights = {
        "13:00 (T-60..T, %5)": make_departure(when=day.replace(hour=13)),
        "14:00 (T-120..T-60, %40)": make_departure(when=day.replace(hour=14)),
        "15:00 (T-180..T-120, %45)": make_departure(when=day.replace(hour=15)),
        "16:00 (T-240..T-180, %10)": make_departure(when=day.replace(hour=16)),
        "15:20 (partial T-180..T-120)": make_departure(when=day.replace(hour=15, minute=20)),
    }
    cap = 180

    def contribution_to_12_13(events):
        window_start = day.replace(hour=12)
        window_end = day.replace(hour=13)
        return sum(c for t, c in events if window_start <= t < window_end)

    contributions = {
        label: contribution_to_12_13(departure_show_up_events(flight, cap))
        for label, flight in flights.items()
    }

    assert contributions["13:00 (T-60..T, %5)"] == 9        # 180*0.05
    assert contributions["14:00 (T-120..T-60, %40)"] == 72  # 180*0.40
    assert contributions["15:00 (T-180..T-120, %45)"] == 81  # 180*0.45
    assert contributions["16:00 (T-240..T-180, %10)"] == 18  # 180*0.10
    assert contributions["15:20 (partial T-180..T-120)"] == 60  # 6 + 54 (bkz. audit)

    # HİÇBİRİ 12:00'da kalkmıyor ama hepsi 12:00-13:00'a katkı veriyor -
    # bu contract'ın kendisi, tek bir yapılandırma değişikliğiyle
    # BOZULMAMALI.
    assert sum(contributions.values()) == 240
