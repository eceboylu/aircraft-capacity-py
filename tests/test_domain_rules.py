"""
Adım 3 doğrulaması - uçuş domain kuralları ve pencere toplama.
Veritabanı kullanılmaz; mock uçuş + mock kapasite çözümleyici.
"""

from datetime import timedelta

import pytest

from app.queue.domain.demand import (
    DemandCalculator,
    effective_time,
    flights_in_window,
)
from app.queue.domain.flight_rules import (
    delay_minutes,
    estimated_duration_minutes,
    route_based_load_factor,
    security_arrival_buffer_minutes,
)
from tests.factories import (
    MockCapacityResolver,
    arrival,
    at,
    departure,
)


# --------------------------------------------------------------------
# Süre türetimi
# --------------------------------------------------------------------

def test_duration_derived_from_scheduled_times():
    flight = departure(8, duration_minutes=245)
    assert estimated_duration_minutes(flight) == 245


def test_duration_none_when_times_missing():
    flight = departure(8)
    flight.arr_scheduled_utc = None
    assert estimated_duration_minutes(flight) is None


# --------------------------------------------------------------------
# Load factor - AÇIK VARSAYIM tablosu
# --------------------------------------------------------------------

def test_load_factor_domestic_ignores_duration():
    """Domestic her zaman 0.78, süreye bakılmaz."""
    assert route_based_load_factor(departure(8, location="domestic", duration_minutes=60)) == 0.78
    assert route_based_load_factor(departure(8, location="domestic", duration_minutes=600)) == 0.78


@pytest.mark.parametrize(
    "duration,expected",
    [
        (60, 0.82),    # kısa menzil uluslararası
        (120, 0.82),   # sınır: 120 dahil değil -> kısa
        (121, 0.84),   # orta menzil
        (360, 0.84),   # sınır: 360 dahil değil -> orta
        (361, 0.88),   # uzun menzil
        (600, 0.88),
    ],
)
def test_load_factor_international_bands(duration, expected):
    flight = departure(8, location="international", duration_minutes=duration)
    assert route_based_load_factor(flight) == expected


def test_load_factor_international_unknown_duration():
    flight = departure(8, location="international")
    flight.arr_scheduled_utc = None
    assert route_based_load_factor(flight) == 0.83


# --------------------------------------------------------------------
# Security buffer - AÇIK VARSAYIM tablosu
# --------------------------------------------------------------------

@pytest.mark.parametrize(
    "duration,expected",
    [
        (60, 45),
        (120, 45),
        (121, 60),
        (360, 60),
        (361, 90),
        (720, 90),
    ],
)
def test_security_buffer_bands(duration, expected):
    flight = departure(8, duration_minutes=duration)
    assert security_arrival_buffer_minutes(flight) == expected


def test_security_buffer_unknown_duration_defaults_to_45():
    flight = departure(8)
    flight.arr_scheduled_utc = None
    assert security_arrival_buffer_minutes(flight) == 45


# --------------------------------------------------------------------
# Gecikme
# --------------------------------------------------------------------

def test_delay_minutes_departure_uses_actual():
    assert delay_minutes(departure(8, delay=35)) == 35


def test_delay_minutes_arrival_uses_actual():
    assert delay_minutes(arrival(8, delay=50)) == 50


def test_delay_minutes_falls_back_to_estimated():
    flight = departure(8)
    flight.dep_estimated_utc = flight.dep_scheduled_utc + timedelta(minutes=20)
    assert delay_minutes(flight) == 20


def test_delay_minutes_zero_when_no_actual_or_estimated():
    """Bilinmiyorsa uydurma gecikme üretilmez."""
    assert delay_minutes(departure(8)) == 0


def test_delay_minutes_negative_for_early_departure():
    flight = departure(8, delay=-10)
    assert delay_minutes(flight) == -10


# --------------------------------------------------------------------
# effective_time
# --------------------------------------------------------------------

def test_effective_time_departure_subtracts_dynamic_buffer():
    """Kısa mesafe kalkış: 08:00 - 45 dk = 07:15"""
    flight = departure(8, duration_minutes=90)
    assert effective_time(flight) == at(7, 15)


def test_effective_time_departure_long_haul_subtracts_90():
    """Senaryo 10: uzun mesafe kalkış 08:00 - 90 dk = 06:30"""
    flight = departure(8, location="international", duration_minutes=500)
    assert effective_time(flight) == at(6, 30)


def test_effective_time_arrival_adds_fixed_buffer():
    """Varış: 08:00 + 15 dk = 08:15"""
    assert effective_time(arrival(8)) == at(8, 15)


def test_effective_time_uses_actual_when_delayed():
    """Gecikmiş kalkış: actual 09:00 - 45 dk = 08:15"""
    flight = departure(8, duration_minutes=90, delay=60)
    assert effective_time(flight) == at(8, 15)


def test_effective_time_none_when_no_time_at_all():
    flight = departure(8)
    flight.dep_scheduled_utc = None
    assert effective_time(flight) is None


# --------------------------------------------------------------------
# flights_in_window
# --------------------------------------------------------------------

def test_window_is_half_open_interval():
    """[start, end) - bitiş anı dahil DEĞİL."""
    inside = departure(7, 45, duration_minutes=90)    # effective 07:00
    outside = departure(8, 0, duration_minutes=90)    # effective 07:15
    result = flights_in_window([inside, outside], at(7, 0), 15)
    assert result == [inside]


def test_window_excludes_cancelled_and_diverted():
    normal = departure(7, 45, duration_minutes=90)
    cancelled = departure(7, 45, duration_minutes=90, status="cancelled", key="C1")
    diverted = departure(7, 45, duration_minutes=90, status="diverted", key="D1")

    result = flights_in_window([normal, cancelled, diverted], at(7, 0), 15)
    assert result == [normal]


def test_window_groups_delayed_flights_by_new_time():
    """
    Schedule compression: farklı tarifeli iki uçuş, gecikme sonrası
    aynı pencereye düşer. Ayrı bir dedektöre gerek yok.
    """
    a = departure(8, 0, duration_minutes=90, delay=60, key="A")   # eff 08:15
    b = departure(8, 50, duration_minutes=90, delay=15, key="B")  # eff 08:20
    result = flights_in_window([a, b], at(8, 15), 15)
    assert set(f.flight_key for f in result) == {"A", "B"}


def test_window_skips_flight_without_usable_time():
    broken = departure(7, 45)
    broken.dep_scheduled_utc = None
    assert flights_in_window([broken], at(7, 0), 15) == []


# --------------------------------------------------------------------
# passenger_demand - Madde 1 entegrasyonu (mock resolver ile)
# --------------------------------------------------------------------

def test_passenger_demand_applies_route_load_factor():
    """A320 180 koltuk x 0.78 (domestic) = 140.4 -> 140"""
    calc = DemandCalculator(MockCapacityResolver())
    flight = departure(8, location="domestic", aircraft="A320")
    assert calc.passenger_demand(flight) == 140


def test_passenger_demand_long_haul_uses_higher_load_factor():
    """B77W 350 koltuk x 0.88 (uzun menzil) = 308"""
    calc = DemandCalculator(MockCapacityResolver())
    flight = departure(8, location="international", aircraft="B77W", duration_minutes=500)
    assert calc.passenger_demand(flight) == 308


def test_passenger_demand_zero_for_general_aviation():
    """counts_toward_passenger_total False -> talebe HİÇ girmez."""
    calc = DemandCalculator(MockCapacityResolver(excluded={"C208"}))
    flight = departure(8, aircraft="C208")
    assert calc.passenger_demand(flight) == 0
    assert calc.seat_capacity(flight) == 0


def test_seat_capacity_is_raw_not_load_adjusted():
    calc = DemandCalculator(MockCapacityResolver())
    flight = departure(8, aircraft="B77W", location="international", duration_minutes=500)
    assert calc.seat_capacity(flight) == 350
    assert calc.passenger_demand(flight) == 308


def test_unknown_aircraft_falls_back_to_resolver_default():
    calc = DemandCalculator(MockCapacityResolver(default_capacity=150))
    flight = departure(8, location="domestic", aircraft="ZZZZ")
    assert calc.passenger_demand(flight) == round(150 * 0.78)


def test_demand_calculator_caches_resolver_calls():
    """Aynı tip tekrar tekrar çözümlenmez - gereksiz DB trafiği olmasın."""
    resolver = MockCapacityResolver()
    calc = DemandCalculator(resolver)
    flights = [departure(8, aircraft="A320", key=f"K{i}") for i in range(5)]
    for f in flights:
        calc.passenger_demand(f)
    assert len(resolver.calls) == 1


# --------------------------------------------------------------------
# effective_time ile delay_minutes aynı zaman önceliğini kullanmalı
# --------------------------------------------------------------------

def test_effective_time_uses_estimated_when_actual_missing():
    """
    Gelecek uçuşta actual yoktur. Gecikme estimated'tan okunuyorsa
    pencere de estimated'tan seçilmeli - yoksa yolcular hiç
    var olmayacakları pencereye yazılır.
    """
    flight = departure(8, 0, duration_minutes=90)
    flight.dep_actual_utc = None
    flight.dep_estimated_utc = at(9, 0)

    # 09:00 kalkış - 45 dk kısa mesafe buffer'ı
    assert effective_time(flight) == at(8, 15)


def test_effective_time_arrival_uses_estimated_when_actual_missing():
    flight = arrival(8, 0)
    flight.arr_actual_utc = None
    flight.arr_estimated_utc = at(10, 0)

    assert effective_time(flight) == at(10, 15)


def test_effective_time_prefers_actual_over_estimated():
    flight = departure(8, 0, duration_minutes=90)
    flight.dep_actual_utc = at(9, 0)
    flight.dep_estimated_utc = at(11, 0)

    assert effective_time(flight) == at(8, 15)


def test_effective_time_and_delay_minutes_agree_on_source():
    """
    İkisi de aynı saati baz almalı: estimated'a göre 60 dk gecikme
    varsa pencere de 60 dk kaymış olmalı.
    """
    on_time = departure(8, 0, duration_minutes=90)
    delayed = departure(8, 0, duration_minutes=90)
    delayed.dep_estimated_utc = at(9, 0)

    assert delay_minutes(delayed) == 60
    assert effective_time(delayed) - effective_time(on_time) == timedelta(
        minutes=60
    )
