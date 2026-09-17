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

def test_effective_time_departure_subtracts_fixed_120_minutes():
    """
    ADIM (Airport Queue Model V2): departure passenger arrival artık
    süreye bağlı dinamik buffer DEĞİL, sabit 120 dakikadır.
    08:00 - 120 dk = 06:00.
    """
    flight = departure(8, duration_minutes=90)
    assert effective_time(flight) == at(6, 0)


def test_effective_time_departure_offset_is_duration_independent():
    """
    Senaryo 10 (güncellendi): eski davranışta uzun mesafe kalkış farklı
    bir buffer (90 dk) kullanırdı; artık offset sabit 120 dk olduğu için
    uçuş süresi (kısa/orta/uzun mesafe farketmeksizin) effective_time'ı
    ETKİLEMEZ. 08:00 - 120 dk = 06:00, mesafeden bağımsız.
    """
    flight = departure(8, location="international", duration_minutes=500)
    assert effective_time(flight) == at(6, 0)


def test_effective_time_arrival_adds_fixed_buffer():
    """Varış: 08:00 + 15 dk = 08:15"""
    assert effective_time(arrival(8)) == at(8, 15)


def test_effective_time_uses_actual_when_delayed():
    """Gecikmiş kalkış: actual 09:00 - 120 dk = 07:00"""
    flight = departure(8, duration_minutes=90, delay=60)
    assert effective_time(flight) == at(7, 0)


def test_effective_time_none_when_no_time_at_all():
    flight = departure(8)
    flight.dep_scheduled_utc = None
    assert effective_time(flight) is None


# --------------------------------------------------------------------
# flights_in_window
# --------------------------------------------------------------------

def test_window_is_half_open_interval():
    """[start, end) - bitiş anı dahil DEĞİL."""
    inside = departure(9, 0, duration_minutes=90)     # effective 07:00
    outside = departure(9, 15, duration_minutes=90)   # effective 07:15
    result = flights_in_window([inside, outside], at(7, 0), 15)
    assert result == [inside]


def test_window_excludes_cancelled_and_diverted():
    normal = departure(9, 0, duration_minutes=90)
    cancelled = departure(9, 0, duration_minutes=90, status="cancelled", key="C1")
    diverted = departure(9, 0, duration_minutes=90, status="diverted", key="D1")

    result = flights_in_window([normal, cancelled, diverted], at(7, 0), 15)
    assert result == [normal]


def test_window_groups_delayed_flights_by_new_time():
    """
    Schedule compression: farklı tarifeli iki uçuş, gecikme sonrası
    aynı pencereye düşer. Ayrı bir dedektöre gerek yok.

    a: scheduled 11:30 + 60 dk gecikme = actual 12:30, - 120 dk = 10:30
    b: scheduled 12:20 + 15 dk gecikme = actual 12:35, - 120 dk = 10:35
    """
    a = departure(11, 30, duration_minutes=90, delay=60, key="A")
    b = departure(12, 20, duration_minutes=90, delay=15, key="B")
    result = flights_in_window([a, b], at(10, 30), 15)
    assert set(f.flight_key for f in result) == {"A", "B"}


def test_window_skips_flight_without_usable_time():
    broken = departure(7, 45)
    broken.dep_scheduled_utc = None
    assert flights_in_window([broken], at(7, 0), 15) == []


# --------------------------------------------------------------------
# passenger_demand - Madde 1 entegrasyonu (mock resolver ile)
# --------------------------------------------------------------------

def test_passenger_demand_uses_raw_icao_capacity_not_load_factor():
    """
    ADIM (ICAO Demand Kalibrasyonu): A320 180 koltuk -> demand=180
    DOĞRUDAN (eski davranış: x0.78 domestic load factor = 140.4->140,
    ARTIK GEÇERLİ DEĞİL).
    """
    calc = DemandCalculator(MockCapacityResolver())
    flight = departure(8, location="domestic", aircraft="A320")
    assert calc.passenger_demand(flight) == 180


def test_passenger_demand_equals_raw_capacity_regardless_of_route_type():
    """
    ADIM (ICAO Demand Kalibrasyonu): `route_based_load_factor()` artık
    passenger_demand'e HİÇ girmiyor - B77W (350 koltuk) hangi rotada
    uçarsa uçsun (uzun/orta/kısa menzil, domestic/international) demand
    HER ZAMAN 350'dir. Eski davranış (uzun menzilde 0.88 ile çarpıp
    308 üretmek) ARTIK GEÇERLİ DEĞİL - fonksiyonun kendisi (bkz.
    test_engine.py/test_domain_rules.py'deki `route_based_load_factor`
    testleri) hâlâ doğru çalışıyor, sadece prediction zincirinden
    AYRILDI.
    """
    calc = DemandCalculator(MockCapacityResolver())
    long_haul = departure(8, location="international", aircraft="B77W",
                           duration_minutes=500, key="LONG")
    short_haul = departure(8, location="domestic", aircraft="B77W",
                            duration_minutes=60, key="SHORT")
    assert calc.passenger_demand(long_haul) == 350
    assert calc.passenger_demand(short_haul) == 350


def test_passenger_demand_zero_for_general_aviation():
    """counts_toward_passenger_total False -> talebe HİÇ girmez."""
    calc = DemandCalculator(MockCapacityResolver(excluded={"C208"}))
    flight = departure(8, aircraft="C208")
    assert calc.passenger_demand(flight) == 0
    assert calc.seat_capacity(flight) == 0


def test_seat_capacity_equals_passenger_demand_no_load_factor():
    """
    ADIM (ICAO Demand Kalibrasyonu): load factor kaldırıldığı için
    `seat_capacity()` (her zaman ham kapasiteydi) ve `passenger_demand()`
    (artık ham kapasiteyi DOĞRUDAN kullanıyor) AYNI değeri üretir.
    """
    calc = DemandCalculator(MockCapacityResolver())
    flight = departure(8, aircraft="B77W", location="international", duration_minutes=500)
    assert calc.seat_capacity(flight) == 350
    assert calc.passenger_demand(flight) == 350
    assert calc.seat_capacity(flight) == calc.passenger_demand(flight)


def test_unknown_aircraft_falls_back_to_resolver_default():
    calc = DemandCalculator(MockCapacityResolver(default_capacity=150))
    flight = departure(8, location="domestic", aircraft="ZZZZ")
    assert calc.passenger_demand(flight) == 150


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

    # 09:00 kalkış - sabit 120 dk = 07:00
    assert effective_time(flight) == at(7, 0)


def test_effective_time_arrival_uses_estimated_when_actual_missing():
    flight = arrival(8, 0)
    flight.arr_actual_utc = None
    flight.arr_estimated_utc = at(10, 0)

    assert effective_time(flight) == at(10, 15)


def test_effective_time_prefers_actual_over_estimated():
    flight = departure(8, 0, duration_minutes=90)
    flight.dep_actual_utc = at(9, 0)
    flight.dep_estimated_utc = at(11, 0)

    assert effective_time(flight) == at(7, 0)


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


# --------------------------------------------------------------------
# Bölüm 31 - EXACT TIME -> HOURLY BUCKET testleri (genel-proje.md)
#
# İçsel event zamanı (effective_time) dakika hassasiyetini korumalı;
# grafik bucket'ı ayrı bir adımda (floor_to_window) saate yuvarlanır.
# Bu iki kavram karıştırılmamalı - burada AÇIKÇA ayrı test edilir.
# --------------------------------------------------------------------

@pytest.mark.parametrize(
    "dep_hour,dep_minute,expected_exact,expected_bucket",
    [
        (12, 0, (10, 0), (10, 0)),
        (12, 3, (10, 3), (10, 0)),
        (12, 34, (10, 34), (10, 0)),
        (12, 59, (10, 59), (10, 0)),
        (13, 0, (11, 0), (11, 0)),
    ],
)
def test_departure_exact_time_and_hourly_bucket_boundaries(
    dep_hour, dep_minute, expected_exact, expected_bucket
):
    """
    Bölüm 31: departure kalkış saatinden -120 dk ile üretilen airport
    arrival zamanı dakika hassasiyetinde korunmalı (internal), ama
    grafik x-ekseni HER ZAMAN saat başlangıcına floor edilmelidir.
    12:59 -> 10:59 (internal) ama grafik bucket'ı hâlâ 10.00'dır;
    13:00 -> 11:00 sınırı geçer, hem internal hem bucket 11.00 olur.
    """
    from app.queue.engine import floor_to_window

    flight = departure(dep_hour, dep_minute, duration_minutes=90)
    exact = effective_time(flight)

    assert exact == at(*expected_exact)
    assert floor_to_window(exact) == at(*expected_bucket)


@pytest.mark.parametrize(
    "arr_hour,arr_minute,expected_exact,expected_bucket",
    [
        (13, 0, (13, 15), (13, 0)),
        (13, 15, (13, 30), (13, 0)),
        (13, 44, (13, 59), (13, 0)),
        (13, 45, (14, 0), (14, 0)),
        (13, 59, (14, 14), (14, 0)),
    ],
)
def test_international_arrival_exact_time_and_hourly_bucket_boundaries(
    arr_hour, arr_minute, expected_exact, expected_bucket
):
    """
    Bölüm 31: international arrival + sabit 15 dk passport offset'i
    ile üretilen passport arrival zamanı dakika hassasiyetinde
    korunmalı; grafik bucket'ı saate floor edilir. 13:44 -> 13:59
    hâlâ 13.00 bucket'ında; 13:45 -> 14:00 sınırı geçer, 14.00'e düşer.
    """
    from app.queue.engine import floor_to_window

    flight = arrival(arr_hour, arr_minute)
    exact = effective_time(flight)

    assert exact == at(*expected_exact)
    assert floor_to_window(exact) == at(*expected_bucket)
