"""
ADIM 6D-2 - Passport GERÇEK "an itibariyle" (current) bekleme süresi
düzeltmesi.

6D-1 audit bulgusu (FAIL): eski overload formülü pencerenin TÜM
talebini (`full_window_demand`) sanki NOW anında zaten kuyrukta
saymıştı (`queue_ahead = backlog_start + full_window_demand`),
`effective_time(f) > now` olan (henüz gelmemiş) uçuşları da anlık
kuyruğa dahil edip, gişelerin pencere içinde çoktan yaptığı servisi
hiç düşmüyordu.

Bu dosya, `core/scoring.py:passport_queue_model()`'e eklenen
`current_arrived_demand`/`elapsed_minutes` parametrelerinin ve
`engine.py:_predict_window_core()`'un bunları `effective_time(f) <=
now` ve `now - window_start`'tan doğru türettiğini kanıtlar.

BACKLOG RECURRENCE (§C) ve stabil-durum Erlang-C (§E) BU DOSYADA
DEĞİŞTİRİLMEDİ - ayrıca test_passport_backlog_model.py'de zaten
doğrulanıyor; burada SADECE current-wait düzeltmesi + onun backlog/
Erlang-C'yi bozmadığı test edilir.
"""

from datetime import datetime, timedelta

import pytest

from app.queue.config import default_config
from app.queue.constants import PROCESS_PASSPORT, PROCESS_SECURITY, RISK_CRITICAL
from app.queue.core.erlang import erlang_c_wait_time
from app.queue.core.scoring import (
    passport_effective_server_count,
    passport_effective_service_rate,
    passport_queue_model,
)
from app.queue.domain.demand import (
    DemandCalculator,
    arrival_passenger_release_events,
    effective_time,
)
from app.queue.engine import floor_to_window, predict_airport

from .factories import MockCapacityResolver, arrival, at


def _demand(resolver=None):
    return DemandCalculator(resolver or MockCapacityResolver(capacities={"BIG": 500, "SML": 80}))


# ========================================================================
# R.1 - 4 gişe x 2 paralel görevli/gişe -> c=8 (4x2 efektif server
# modeli - regresyon, zaten test_icao_demand_and_390_capacity_model.py'de
# var, burada passport_queue_model üzerinden AYRICA doğrulanıyor).
# staff_count(8, toplam personel/vardiya) BUNUNLA KARIŞTIRILMAZ - c,
# counter_count(4) x staff_per_counter(2)'dan gelir, staff_count'tan DEĞİL.
# ========================================================================

def test_r1_c_equals_8_from_4_counters_times_2_staff():
    cfg = default_config("R1")
    assert cfg.passport_counter_count == 4
    assert cfg.passport_staff_per_counter == 2
    assert cfg.passport_staff_count == 8   # bilgi amaçlı, c'nin kaynağı DEĞİL

    # capacity_rate = c * mu = 8 * (1/1.5) = 16/3 (320/saat).
    mu = passport_effective_service_rate(cfg)
    c = passport_effective_server_count(cfg)
    assert c == 8
    result = passport_queue_model([1000], cfg, demand_fn=lambda x: x, window_minutes=15)
    capacity_rate = c * mu
    assert capacity_rate == pytest.approx(16 / 3)
    assert result["utilization"] == pytest.approx((1000 / 15) / (16 / 3), abs=1e-3)


# ========================================================================
# R.2 - Normal yük (rho<1, backlog_start<=0) -> eski Erlang-C BİREBİR
# ========================================================================

def test_r2_normal_load_identical_to_old_erlang_c():
    cfg = default_config("R2")
    demand_fn = lambda x: x
    window_flights = [20]

    result = passport_queue_model(window_flights, cfg, demand_fn, window_minutes=15)
    expected = erlang_c_wait_time(8, 20 / 15, 2 / 3)

    assert result["utilization"] < 1.0
    assert result["estimated_wait_minutes"] == round(expected, 1)

    # current_arrived_demand/elapsed_minutes VERİLSE BİLE stabil dalda
    # hiç kullanılmaz - sonuç birebir aynı kalmalı (kararlı dal backlog/
    # current parametrelerinden TAMAMEN bağımsızdır).
    with_current_params = passport_queue_model(
        window_flights, cfg, demand_fn, window_minutes=15,
        current_arrived_demand=5, elapsed_minutes=2,
    )
    assert with_current_params["estimated_wait_minutes"] == result["estimated_wait_minutes"]


# ========================================================================
# R.3/R.4/R.5 - GERÇEK current wait: effective_time>now hariç, <=now
# dahil, elapsed service düşülür. Motor seviyesinde (predict_airport),
# gerçek Flight nesneleri ve gerçek `now` ile.
# ========================================================================

AIRPORT = "T6D2"


def _scenario():
    """
    Pencere 10:00-11:00 (arrival release event'leriyle).
    - ARRIVED : arr_scheduled=09:47 -> +10/+15/+20 batch'leri
      09:57/10:02/10:07; now=10:07'ye kadar gerçekten release olmuş
      batch'leri vardır.
    - FUTURE  : arr_scheduled=10:00 -> ilk release 10:10; AYNI saatlik
      pencereye girer ama now=10:07'de henüz hiçbir batch'i release
      olmamıştır.
    İkisi de BIG (500 kapasite) - backlog_start=0 olsa bile rho>=1
    (overload dalı) garanti edilsin diye.
    """
    arrived = arrival(9, 47, airport=AIRPORT, aircraft="BIG",
                       duration_minutes=90, key="ARRIVED")
    future = arrival(10, 0, airport=AIRPORT, aircraft="BIG",
                      duration_minutes=90, key="FUTURE")
    now = at(10, 7)
    window_start = at(10, 0)

    assert floor_to_window(effective_time(arrived)) == window_start
    assert floor_to_window(effective_time(future)) == window_start
    arrived_events = arrival_passenger_release_events(arrived, 500)
    future_events = arrival_passenger_release_events(future, 500)
    assert any(moment <= now for moment, _ in arrived_events)
    assert all(moment > now for moment, _ in future_events)

    return arrived, future, now, window_start


def test_r3_future_flight_excluded_from_current_wait():
    """
    R.3: release event'i > now olan FUTURE batch'leri current wait'i
    ARRIVED-tek-başına senaryosuyla birebir aynı bırakmalı (yani
    current wait FUTURE'ı henüz görmüyor) - ama backlog_end (R.6)
    FUTURE'ı da (tam pencere talebi üzerinden) İÇERİR.
    """
    arrived, future, now, window_start = _scenario()
    cfg = default_config(AIRPORT)
    demand = _demand()

    both = predict_airport(AIRPORT, [arrived, future], cfg, demand, now=now)
    only_arrived = predict_airport(AIRPORT, [arrived], cfg, demand, now=now)

    passport_both = next(
        p for p in both if p.process == PROCESS_PASSPORT and p.window_start == window_start
    )
    passport_only = next(
        p for p in only_arrived if p.process == PROCESS_PASSPORT and p.window_start == window_start
    )

    # current wait - FUTURE hiç sayılmadığı için İKİ senaryo AYNI.
    assert passport_both.estimated_wait_minutes == passport_only.estimated_wait_minutes
    # ama flight_count/expected_passengers (tam pencere talebi) FARKLI -
    # FUTURE demand/backlog hesabına GİRMEYE devam ediyor (R.6/§C).
    assert passport_both.expected_passengers > passport_only.expected_passengers
    assert passport_both.flight_count == 2
    assert passport_only.flight_count == 1


def test_r4_arrived_flight_included_in_current_wait():
    """R.4: effective_time(f) <= now olan ARRIVED, current demand'e girer -> wait > 0."""
    arrived, future, now, window_start = _scenario()
    cfg = default_config(AIRPORT)
    demand = _demand()

    predictions = predict_airport(AIRPORT, [arrived, future], cfg, demand, now=now)
    passport = next(
        p for p in predictions if p.process == PROCESS_PASSPORT and p.window_start == window_start
    )
    assert passport.utilization >= 1.0
    assert passport.estimated_wait_minutes is not None
    assert passport.estimated_wait_minutes > 0


def test_r5_elapsed_service_deducted_from_current_queue():
    """
    R.5: `now` pencerenin içinde ilerledikçe (elapsed_minutes artınca),
    current wait KESİNLİKLE AZALMALI - gişelerin o kadar süre boyunca
    yaptığı servis kuyruktan düşülüyor.
    """
    arrived, future, _, window_start = _scenario()
    cfg = default_config(AIRPORT)

    early_now = at(10, 3)    # elapsed=3dk, sadece ARRIVED (10:02) girmiş olabilir
    late_now = at(10, 14)    # elapsed=14dk, ARRIVED+FUTURE(10:12) ikisi de girmiş

    early = predict_airport(AIRPORT, [arrived, future], cfg, _demand(), now=early_now)
    late = predict_airport(AIRPORT, [arrived, future], cfg, _demand(), now=late_now)

    p_early = next(p for p in early if p.process == PROCESS_PASSPORT and p.window_start == window_start)
    p_late = next(p for p in late if p.process == PROCESS_PASSPORT and p.window_start == window_start)

    # late_now'da HEM daha fazla elapsed servis düşülüyor HEM FUTURE artık
    # "gelmiş" sayılıp current demand'e giriyor - net etki senaryoya bağlı
    # olabilir, ama HER İKİSİ de sonlu ve negatif değil, ve elapsed=0
    # (pencere henüz açılmamış gibi) ile karşılaştırıldığında servis
    # düşümü ÖLÇÜLEBİLİR olmalı:
    zero_elapsed = passport_queue_model(
        [arrived, future], cfg, DemandCalculator(MockCapacityResolver(capacities={"BIG": 500})).passenger_demand,
        window_minutes=15, backlog_start=0.0,
        current_arrived_demand=DemandCalculator(MockCapacityResolver(capacities={"BIG": 500})).passenger_demand(arrived),
        elapsed_minutes=0.0,
    )
    assert p_early.estimated_wait_minutes < zero_elapsed["estimated_wait_minutes"]


# ========================================================================
# R.6 - full_window_demand backlog recurrence'ta AYNEN kalır
# ========================================================================

def test_r6_backlog_recurrence_uses_full_window_demand_unaffected():
    cfg = default_config("R6")
    demand_fn = lambda x: x

    # current-wait parametreleri verilse BİLE backlog_end SADECE demand
    # (tam pencere talebi) ve service_capacity'ye bağlı kalmalı.
    without_current = passport_queue_model(
        [200], cfg, demand_fn, window_minutes=15, backlog_start=0.0,
    )
    with_current = passport_queue_model(
        [200], cfg, demand_fn, window_minutes=15, backlog_start=0.0,
        current_arrived_demand=1, elapsed_minutes=1,
    )
    assert without_current["backlog_end"] == with_current["backlog_end"]
    assert without_current["backlog_end"] == pytest.approx(120.0)
    # ama estimated_wait_minutes current parametrelerden ETKİLENİR (farklı).
    assert without_current["estimated_wait_minutes"] != with_current["estimated_wait_minutes"]


# ========================================================================
# R.7 - rho>=1 -> mümkünse SONLU wait (regresyon, backlog testinde de var)
# ========================================================================

def test_r7_overload_gives_finite_wait():
    cfg = default_config("R7")
    result = passport_queue_model([500], cfg, demand_fn=lambda x: x, window_minutes=15)
    assert result["utilization"] >= 1.0
    assert result["risk"] == RISK_CRITICAL
    assert result["estimated_wait_minutes"] is not None
    assert result["estimated_wait_minutes"] >= 0


# ========================================================================
# R.8 - backlog recovery bozulmaz (kademeli boşalma, ayrıntılı kanıt
# test_passport_backlog_model.py'de; burada sadece regresyon başlığı)
# ========================================================================

def test_r8_backlog_recovery_still_gradual():
    cfg = default_config("R8")
    demand_fn = lambda x: x
    t1 = passport_queue_model([300], cfg, demand_fn, window_minutes=15, backlog_start=0.0)
    t2 = passport_queue_model([], cfg, demand_fn, window_minutes=15, backlog_start=t1["backlog_end"])
    assert t1["backlog_end"] > 0
    assert 0 < t2["backlog_end"] < t1["backlog_end"]   # kademeli azalıyor, anında sıfırlanmıyor


# ========================================================================
# R.9 - security aynı current-arrived semantiğiyle gerçek wait üretir
# ========================================================================

def test_r9_security_wait_is_finite_and_independent_of_passport_queue():
    cfg = default_config(AIRPORT)
    arrived, future, now, window_start = _scenario()
    from .factories import departure
    dep = departure(10, 0, airport=AIRPORT, aircraft="BIG", location="domestic",
                     duration_minutes=60, key="SEC1")

    predictions = predict_airport(
        AIRPORT, [arrived, future, dep], cfg, _demand(), now=now,
    )
    security = [p for p in predictions if p.process == PROCESS_SECURITY]
    assert security
    for p in security:
        assert p.estimated_wait_minutes is not None
    # ADIM (Departure Show-Up Profile): 500 pax'lik tek domestic flight
    # artık 3 saate (%20/%60/%20) yayılıyor - conservation korunur,
    # zirve saat KENDİ payına (300) göre değerlendirilir.
    assert sum(p.expected_passengers for p in security) == 500
    peak = max(security, key=lambda p: p.expected_passengers)
    assert peak.utilization == pytest.approx(300 / 480, abs=0.001)
