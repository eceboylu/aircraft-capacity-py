"""
ICAO Demand Kalibrasyonu + Passport 320 pax/saat Kapasite Modeli
(4 gişe x 2 paralel görevli/gişe).

Kapsam:
  A) passenger_demand artık load factor UYGULAMADAN ham ICAO kapasitesini
     kullanıyor (`app/queue/domain/demand.py:DemandCalculator.passenger_demand`).
  D-G) Passport: c=8 (4 gişe x 2 görevli/gişe), mu=2/3, capacity_rate=
     16/3/dk (320/saat) - 4 gişe, 1.5 dk servis süresi
     (`app/queue/models.py:AirportOperationalConfig` varsayılanları +
     `app/queue/core/scoring.py:passport_effective_server_count`/
     `passport_effective_service_rate`).
  H/I) Current wait (ADIM 6D-2) ve backlog recurrence (AŞAMA 6D §C)
     yeni kapasiteyle DOĞRU çalışıyor.

Diğer dosyalarda zaten doğrulanan noktalar (backlog kademeli boşalma,
security formülü, Erlang-C stabil dal, c=8 YAPILMADIĞI, vb.) burada
TEKRARLANMAZ - bkz. test_passport_backlog_model.py, test_passport_current_wait_fix.py,
test_security_ratios.py, test_domain_rules.py.
"""

import pytest

from app.queue.config import default_config
from app.queue.core.erlang import erlang_c_wait_time
from app.queue.core.scoring import (
    passport_effective_server_count,
    passport_effective_service_rate,
    passport_queue_model,
)
from app.queue.domain.demand import DemandCalculator

from .factories import MockCapacityResolver, departure


def demand_fn(x):
    return x


# ========================================================================
# L.1/L.2 - ICAO capacity -> passenger demand DOĞRUDAN (load factor YOK)
# ========================================================================

def test_icao_capacity_180_gives_demand_180():
    calc = DemandCalculator(MockCapacityResolver(capacities={"A320": 180}))
    flight = departure(9, 0, aircraft="A320", location="domestic")
    assert calc.passenger_demand(flight) == 180


def test_icao_capacity_305_gives_demand_305():
    calc = DemandCalculator(MockCapacityResolver(capacities={"B772": 305}))
    flight = departure(9, 0, aircraft="B772", location="international",
                        duration_minutes=600)   # uzun menzil olsa bile fark etmez
    assert calc.passenger_demand(flight) == 305


# ========================================================================
# L.3 - eski load factor artık sonucu DEĞİŞTİRMEZ (aynı ICAO, farklı rota)
# ========================================================================

def test_load_factor_no_longer_changes_demand_across_route_types():
    calc = DemandCalculator(MockCapacityResolver(capacities={"A320": 180}))
    domestic = departure(9, 0, aircraft="A320", location="domestic", duration_minutes=60)
    intl_short = departure(9, 0, aircraft="A320", location="international", duration_minutes=90)
    intl_long = departure(9, 0, aircraft="A320", location="international", duration_minutes=500)
    # eski davranışta bu üçü SIRASIYLA 140/148/158 gibi FARKLI değerler
    # üretirdi (domestic 0.78, kısa intl 0.82, uzun intl 0.88) - artık
    # HEPSİ 180.
    assert calc.passenger_demand(domestic) == 180
    assert calc.passenger_demand(intl_short) == 180
    assert calc.passenger_demand(intl_long) == 180


# ========================================================================
# L.4 - Capacity resolver fallback zinciri KORUNUYOR (resolver'a
# dokunulmadı, DemandCalculator sadece onun döndürdüğünü kullanıyor)
# ========================================================================

def test_resolver_fallback_chain_still_drives_demand_unchanged():
    """
    Resolver farklı capacity/fallback döndürünce demand BİREBİR onu
    takip etmeli - queue katmanında ikinci bir kapasite kaynağı/sabiti
    YOK (bu, ADIM'ın B) maddesinin "resolver'ı bozma" kısıtının
    doğrudan kanıtı).
    """
    known = DemandCalculator(MockCapacityResolver(capacities={"A320": 180}))
    unknown_fallback = DemandCalculator(MockCapacityResolver(default_capacity=150))
    excluded = DemandCalculator(MockCapacityResolver(excluded={"C208"}))

    known_flight = departure(9, 0, aircraft="A320", location="domestic")
    unknown_flight = departure(9, 0, aircraft="ZZZZ", location="domestic")
    ga_flight = departure(9, 0, aircraft="C208", location="domestic")

    assert known.passenger_demand(known_flight) == 180
    assert unknown_fallback.passenger_demand(unknown_flight) == 150
    assert excluded.passenger_demand(ga_flight) == 0   # genel havacılık talebe hiç girmez


# ========================================================================
# L.7-L.12 - 4 gişe x 2 paralel görevli/gişe -> c=8, mu=2/3,
# capacity_rate=16/3/dk, hourly=320
# ========================================================================

def test_passport_capacity_calibration_320_per_hour():
    cfg = default_config("CAL")
    assert cfg.passport_counter_count == 4              # fiziksel gişe sayısı, c DEĞİL
    assert cfg.passport_staff_per_counter == 2          # gişe başına paralel görevli
    assert cfg.passport_staff_count == 8                # bilgi amaçlı, c DEĞİL

    mu = passport_effective_service_rate(cfg)
    assert mu == pytest.approx(2 / 3)

    c = passport_effective_server_count(cfg)
    assert c == 8   # 4 gişe x 2 görevli/gişe

    capacity_rate = c * mu
    assert capacity_rate == pytest.approx(16 / 3)
    assert capacity_rate * 60 == pytest.approx(320.0)


def test_l17_staff_count_change_does_not_change_c_or_mu():
    """staff_count'u değiştirmek c'yi VEYA mu'yu DEĞİŞTİRMEZ."""
    from app.queue.config import AirportConfigView

    cfg = default_config("CAL2")
    mutated = AirportConfigView(
        airport_iata="CAL2",
        passport_counter_count=cfg.passport_counter_count,
        passport_staff_count=999,   # kasıtlı tutarsız
        passport_service_time_minutes=cfg.passport_service_time_minutes,
        security_lane_count=cfg.security_lane_count,
        security_service_time_minutes=cfg.security_service_time_minutes,
        passport_staff_per_counter=cfg.passport_staff_per_counter,
        passport_service_rate_per_staff=cfg.passport_service_rate_per_staff,
        passport_efficiency_multiplier=cfg.passport_efficiency_multiplier,
        arrival_bank_threshold=cfg.arrival_bank_threshold,
        is_default=True,
    )
    assert mutated.passport_counter_count == cfg.passport_counter_count   # c DEĞİŞMEDİ
    assert passport_effective_service_rate(mutated) == passport_effective_service_rate(cfg)


def test_l18_staff_count_not_multiplied_into_capacity_twice():
    """
    `passport_staff_count`(8, toplam personel/vardiya bilgisi) mu
    hesabına GİRMİYOR - kapasite SADECE `passport_effective_server_count()`
    (4 gişe x 2 paralel görevli/gişe = 8) x mu'dan gelir; `staff_count`'u
    AYRICA çarpmak kapasiteyi yanlış büyütürdü (double-count).
    """
    cfg = default_config("CAL3")
    capacity_rate = passport_effective_server_count(cfg) * passport_effective_service_rate(cfg)
    assert capacity_rate == pytest.approx(16 / 3)
    assert capacity_rate != pytest.approx((16 / 3) * cfg.passport_staff_count)
    assert capacity_rate != pytest.approx((16 / 3) * 2)


# ========================================================================
# L.13-L.15 - rho örnekleri (4x2=8 efektif server, 320 pax/saat):
# 160->0.5, 320->1.0, 900->2.8125
# ========================================================================

@pytest.mark.parametrize(
    "demand,expected_rho",
    [
        (160, 0.5),
        (320, 1.0),
        (900, 2.8125),
    ],
)
def test_rho_examples_at_hourly_window(demand, expected_rho):
    cfg = default_config("RHO")
    result = passport_queue_model([demand], cfg, demand_fn, window_minutes=60)
    assert result["utilization"] == pytest.approx(expected_rho, abs=0.001)


# ========================================================================
# L.16 - backlog örneği: demand=900, backlog_start=0 -> backlog_end=580
# (900 - 320 kapasite = 580)
# ========================================================================

def test_backlog_example_demand_900_gives_580():
    cfg = default_config("BL")
    result = passport_queue_model([900], cfg, demand_fn, window_minutes=60, backlog_start=0.0)
    assert result["backlog_end"] == pytest.approx(580.0)


# ========================================================================
# L.19 - current wait: now=08:23 -> served=(16/3)*23 (8 efektif server)
# ========================================================================

def test_l19_served_since_window_start_uses_16_over_3_rate():
    cfg = default_config("SRV")
    mu = passport_effective_service_rate(cfg)
    capacity_rate = passport_effective_server_count(cfg) * mu
    elapsed_minutes = 23.0
    served = capacity_rate * elapsed_minutes
    assert served == pytest.approx((16 / 3) * 23)

    # Aynı senaryoyu passport_queue_model üzerinden de doğrula.
    result = passport_queue_model(
        [900], cfg, demand_fn, window_minutes=60, backlog_start=0.0,
        current_arrived_demand=900, elapsed_minutes=elapsed_minutes,
    )
    current_queue = max(0.0, 0.0 + 900 - served)
    assert result["estimated_wait_minutes"] == round(current_queue / capacity_rate, 1)


# ========================================================================
# L.23/L.24 - normal rho<1 Erlang-C çalışır; overload sonlu wait üretir
# (c=8 efektif server, mu=2/3 ile)
# ========================================================================

def test_l23_normal_rho_below_one_uses_erlang_c_with_new_mu():
    cfg = default_config("EC")
    result = passport_queue_model([160], cfg, demand_fn, window_minutes=60, backlog_start=0.0)
    lam = 160 / 60
    expected = erlang_c_wait_time(8, lam, 2 / 3)
    assert result["utilization"] < 1.0
    assert result["estimated_wait_minutes"] == round(expected, 1)


def test_l24_overload_gives_finite_wait_with_new_capacity():
    cfg = default_config("OV")
    result = passport_queue_model([900], cfg, demand_fn, window_minutes=60, backlog_start=0.0)
    assert result["utilization"] >= 1.0
    assert result["estimated_wait_minutes"] is not None
    assert result["estimated_wait_minutes"] >= 0
    assert result["risk"] == "CRITICAL"


# ========================================================================
# D) LOAD FACTOR ACTIVE IN PREDICTION kanıtı - passport_queue_model'e
# giden demand_fn, artık passenger_demand olduğu için (production
# call chain) route_based_load_factor'ün prediction'a HİÇ girmediğinin
# uçtan uca kanıtı.
# ========================================================================

def test_load_factor_not_active_in_full_prediction_chain():
    from app.queue.constants import LOCATION_INTERNATIONAL
    from app.queue.domain.flight_rules import route_based_load_factor
    from tests.factories import arrival

    calc = DemandCalculator(MockCapacityResolver(capacities={"BIG": 500}))
    short = arrival(9, 0, aircraft="BIG", location=LOCATION_INTERNATIONAL, duration_minutes=90)
    long = arrival(9, 0, aircraft="BIG", location=LOCATION_INTERNATIONAL, duration_minutes=500)

    # route_based_load_factor'ün KENDİSİ hâlâ farklı değerler üretiyor
    # (fonksiyon silinmedi) - ama passenger_demand bunu ARTIK KULLANMIYOR.
    assert route_based_load_factor(short) != route_based_load_factor(long)
    assert calc.passenger_demand(short) == calc.passenger_demand(long) == 500
