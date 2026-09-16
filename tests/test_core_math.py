"""
Adım 2 doğrulaması - saf matematik katmanı.

Bu testler hiçbir veritabanına dokunmaz; mock nesneler ve elle
hesaplanmış referans değerler kullanır.
"""

import math

import pytest

from app.queue.core.erlang import erlang_c_probability, erlang_c_wait_time
from app.queue.core.scoring import (
    confidence_score,
    passport_effective_service_rate,
    passport_queue_model,
    passport_staff_count_mismatch,
    security_density_score,
    security_queue_model,
)


class MockConfig:
    """AirportOperationalConfig'in varsayılanlarıyla aynı değerler."""

    def __init__(
        self,
        passport_counter_count=4,
        passport_staff_count=8,
        passport_service_time_minutes=1.5,
        security_lane_count=8,
        security_service_time_minutes=1.0,
        passport_staff_per_counter=2.0,
        passport_service_rate_per_staff=0.5,
        passport_efficiency_multiplier=1.5,
    ):
        self.passport_counter_count = passport_counter_count
        self.passport_staff_count = passport_staff_count
        self.passport_service_time_minutes = passport_service_time_minutes
        self.security_lane_count = security_lane_count
        self.security_service_time_minutes = security_service_time_minutes
        self.passport_staff_per_counter = passport_staff_per_counter
        self.passport_service_rate_per_staff = passport_service_rate_per_staff
        self.passport_efficiency_multiplier = passport_efficiency_multiplier


class MockFlight:
    def __init__(self, demand=100, aircraft_icao="A320"):
        self.demand = demand
        self.aircraft_icao = aircraft_icao


def demand_fn(flight):
    return flight.demand


# --------------------------------------------------------------------
# Erlang-C çekirdeği
# --------------------------------------------------------------------

def test_erlang_c_probability_matches_hand_calculation():
    """c=2, a=1.0 için C(2, 1) = 1/3 (literatürdeki standart değer)."""
    assert erlang_c_probability(2, 1.0) == pytest.approx(1 / 3)


def test_erlang_c_probability_formula_expanded():
    """c=3, a=2.0 için formülü bağımsız olarak yeniden kurup karşılaştır."""
    c, a = 3, 2.0
    top = (a ** c) / (math.factorial(c) * (1 - a / c))
    bottom = sum((a ** k) / math.factorial(k) for k in range(c)) + top
    assert erlang_c_probability(c, a) == pytest.approx(top / bottom)


def test_erlang_c_probability_saturated_returns_one():
    """traffic >= c: sistem kararsız, kesin bekleme."""
    assert erlang_c_probability(2, 2.0) == 1.0
    assert erlang_c_probability(2, 5.0) == 1.0


def test_erlang_c_probability_zero_traffic():
    assert erlang_c_probability(4, 0.0) == 0.0


def test_erlang_c_wait_time_matches_definition():
    c, lam, mu = 4, 2.0, 0.75
    expected = erlang_c_probability(c, lam / mu) / (c * mu - lam)
    assert erlang_c_wait_time(c, lam, mu) == pytest.approx(expected)


def test_erlang_c_wait_time_raises_when_saturated():
    with pytest.raises(ValueError):
        erlang_c_wait_time(4, 3.0, 0.75)


# --------------------------------------------------------------------
# Security - YASAK 1: dakika ASLA üretilmez
# --------------------------------------------------------------------

def test_security_without_baseline_returns_unknown_and_no_fake_ratio():
    flights = [MockFlight(120), MockFlight(150)]
    result = security_density_score(flights, None, demand_fn)

    assert result["risk"] == "UNKNOWN"
    assert result["baseline_ratio"] is None
    assert result["estimated_wait_minutes"] is None
    assert result["expected_passengers"] == 270
    assert result["flight_count"] == 2


def test_security_zero_baseline_treated_as_missing():
    result = security_density_score([MockFlight(100)], 0, demand_fn)
    assert result["risk"] == "UNKNOWN"
    assert result["baseline_ratio"] is None


@pytest.mark.parametrize(
    "flight_count,baseline,expected_risk",
    [
        (10, 10, "LOW"),        # ratio 1.0
        (11, 10, "LOW"),        # ratio 1.1  -> sınır altı
        (12, 10, "MEDIUM"),     # ratio 1.2  -> banda giriş
        (13, 10, "MEDIUM"),     # ratio 1.3
        (14, 10, "HIGH"),       # ratio 1.4  -> banda giriş
        (17, 10, "HIGH"),       # ratio 1.7
        (18, 10, "CRITICAL"),   # ratio 1.8  -> banda giriş
        (30, 10, "CRITICAL"),   # ratio 3.0
    ],
)
def test_security_risk_bands(flight_count, baseline, expected_risk):
    flights = [MockFlight(100) for _ in range(flight_count)]
    result = security_density_score(flights, baseline, demand_fn)
    assert result["risk"] == expected_risk
    assert result["baseline_ratio"] == pytest.approx(flight_count / baseline)


def test_security_never_produces_wait_minutes_in_any_band():
    """YASAK 1: hangi risk seviyesi olursa olsun dakika None kalır."""
    for count in range(0, 40):
        flights = [MockFlight(200) for _ in range(count)]
        for baseline in (None, 0, 1, 5, 10):
            result = security_density_score(flights, baseline, demand_fn)
            assert result["estimated_wait_minutes"] is None


def test_security_empty_window():
    result = security_density_score([], 10, demand_fn)
    assert result["flight_count"] == 0
    assert result["expected_passengers"] == 0
    assert result["risk"] == "LOW"


# --------------------------------------------------------------------
# Passport - tam Erlang-C
# --------------------------------------------------------------------

def test_passport_effective_service_rate():
    # mu = 1 / 1.5 = 0.6666667 yolcu/dakika/gişe.
    assert passport_effective_service_rate(MockConfig()) == pytest.approx(2 / 3)


def test_passport_effective_service_rate_comes_only_from_service_time():
    config = MockConfig(passport_service_time_minutes=2.0)
    assert passport_effective_service_rate(config) == pytest.approx(0.5)


def test_passport_legacy_staff_per_counter_does_not_change_mu():
    one = MockConfig(passport_staff_per_counter=1.0)
    two = MockConfig(passport_staff_per_counter=2.0)
    assert passport_effective_service_rate(one) == passport_effective_service_rate(two)


def test_passport_effective_service_rate_ignores_staff_count():
    """
    MADDE 1 kuralı: passport_staff_count kapasite hesabında İKİNCİ KEZ
    kullanılmamalı. staff_count 8'den 100'e çıksa bile mu değişmemeli -
    aksi halde personel sayısı iki kez büyütülmüş (double-count) olur.
    """
    low = MockConfig(passport_staff_count=8)
    high = MockConfig(passport_staff_count=100)
    assert passport_effective_service_rate(low) == passport_effective_service_rate(high)


def test_passport_staff_count_mismatch_detection():
    """MADDE 1 kural 4: staff_count ile counter*staff_per_counter uyuşmazsa işaretlenir."""
    consistent = MockConfig(
        passport_counter_count=4, passport_staff_per_counter=2.0, passport_staff_count=8
    )
    mismatched = MockConfig(
        passport_counter_count=4, passport_staff_per_counter=2.0, passport_staff_count=50
    )
    assert passport_staff_count_mismatch(consistent) is False
    assert passport_staff_count_mismatch(mismatched) is True


def test_passport_staff_count_mismatch_does_not_affect_mu():
    """Mismatch işaretlense bile mu/rho hesabı staff_count'tan etkilenmez."""
    mismatched = MockConfig(
        passport_counter_count=4, passport_staff_per_counter=2.0, passport_staff_count=999
    )
    assert passport_staff_count_mismatch(mismatched) is True
    assert passport_effective_service_rate(mismatched) == pytest.approx(2 / 3)


# Production persistence ve queue recurrence saatliktir.
_W = 60


def test_passport_low_utilization():
    flights = [MockFlight(80)]
    result = passport_queue_model(flights, MockConfig(), demand_fn, window_minutes=_W)

    assert result["arrival_rate"] == pytest.approx(80 / 60, abs=1e-3)
    # ADIM (4x2 efektif server modeli): c=4 gişe x 2 görevli/gişe=8,
    # mu=2/3 -> capacity_rate=16/3/dk (320 pax/saat).
    assert result["capacity_rate"] == pytest.approx(16 / 3, abs=1e-6)
    assert result["utilization"] == pytest.approx(0.25)
    assert result["risk"] == "LOW"
    assert result["estimated_wait_minutes"] is not None
    assert result["estimated_wait_minutes"] >= 0


def test_passport_wait_time_matches_erlang_reference():
    flights = [MockFlight(80)]
    config = MockConfig()
    result = passport_queue_model(flights, config, demand_fn, window_minutes=_W)

    # c = passport_counter_count(4) x passport_staff_per_counter(2) = 8.
    lam, c, mu = 80 / 60, 8, 2 / 3
    expected = erlang_c_wait_time(c, lam, mu)
    assert result["estimated_wait_minutes"] == pytest.approx(round(expected, 1))


@pytest.mark.parametrize(
    "demand,expected_risk",
    [
        (160, "LOW"),       # rho 0.50
        (222, "LOW"),       # rho 0.694
        (224, "MEDIUM"),    # rho 0.70
        (286, "MEDIUM"),    # rho 0.894
        (288, "HIGH"),      # rho 0.90
        (318, "HIGH"),      # rho 0.994
        (320, "CRITICAL"),  # rho 1.00
    ],
)
def test_passport_risk_bands(demand, expected_risk):
    """c=8 (4 gişe x 2 görevli), mu=2/3 -> capacity_rate=16/3/dk -> saatlik kapasite 320."""
    result = passport_queue_model([MockFlight(demand)], MockConfig(), demand_fn, window_minutes=_W)
    assert result["risk"] == expected_risk


def test_passport_risk_bands_change_with_counter_count():
    """MADDE 2: c doğru şekilde counter_count x staff_per_counter olmalı - sayısı değişince rho değişmeli."""
    flights = [MockFlight(240)]
    few_counters = passport_queue_model(
        flights, MockConfig(passport_counter_count=4), demand_fn, window_minutes=_W
    )
    many_counters = passport_queue_model(
        flights, MockConfig(passport_counter_count=8), demand_fn, window_minutes=_W
    )
    assert few_counters["utilization"] == pytest.approx(
        2 * many_counters["utilization"], abs=0.002
    )
    assert few_counters["risk"] == "MEDIUM"
    assert many_counters["risk"] == "LOW"


def test_passport_overload_returns_finite_wait_and_critical():
    """
    Senaryo 13 - ADIM 6D ile GÜNCELLENDİ: rho >= 1 artık dakika=None
    DEĞİL, backlog tabanlı SONLU bir "an itibariyle bekleme" tahmini
    üretir (risk hâlâ CRITICAL - risk/wait birbirinden bağımsız).

    ADIM 6D-2 DÜZELTMESİ: `now`/`window_start` verilmediğinde (bu test
    gibi doğrudan `passport_queue_model()` çağıran eski/saf çağıranlar)
    pencerenin TAMAMEN KAPANDIĞI varsayılır - current_arrived_demand=
    demand(90), elapsed_minutes=window_minutes(15) (bkz. fonksiyon
    docstring'i). Bu durumda current_queue == backlog_end:

    320 yolcu / 60 dk = 16/3 = c*mu (8*2/3) -> rho tam 1.0.
    served_since_window_start = 16/3 * 60 = 320.
    current_queue = max(0, 0 + 320 - 320) = 0 -> wait = 0/(16/3) = 0.0 dk
    (tam bu anda gişeler pencerenin tüm talebini karşılamış durumda -
    risk hâlâ CRITICAL çünkü rho tam sınırda, ama an itibariyle kuyruk
    boş).
    """
    result = passport_queue_model([MockFlight(320)], MockConfig(), demand_fn, window_minutes=_W)

    assert result["utilization"] == pytest.approx(1.0)
    assert result["estimated_wait_minutes"] == 0.0
    assert result["backlog_end"] == 0.0
    assert result["risk"] == "CRITICAL"
    assert result["reasons"] == [
        "Talep kapasiteyi aşıyor — kuyruk teorik olarak sürdürülemez"
    ]


def test_passport_far_overload_scales_wait_up_not_none():
    """
    ADIM 6D ile GÜNCELLENDİ: 5000 yolculuk çok daha ağır bir aşım,
    ESKİ None davranışı yerine ORANTILI olarak DAHA UZUN (sahte bir
    üst sınıra çarpmadan) bir bekleme üretmeli.

    Kapasitenin (320/saat) 1,25 katı bir talep (400) - backlog_end =
    max(0, 0 + 400 - 320) = 80 -> wait = 80 / (16/3) = 15.0 dk (aynı
    oranlı aşım, 4x2 efektif server modelinde de AYNI dakika sonucunu
    verir - kapasite VE aşım orantılı büyüdüğü için wait değişmez).
    """
    result = passport_queue_model([MockFlight(400)], MockConfig(), demand_fn, window_minutes=_W)
    assert result["backlog_end"] == 80.0
    assert result["estimated_wait_minutes"] == 15.0
    assert result["risk"] == "CRITICAL"


def test_passport_config_override_changes_result():
    """
    YASAK 5 yönü: config havalimanı bazlı override edilebilir olmalı.
    Aynı talep, daha çok gişe -> daha düşük utilization.
    """
    flights = [MockFlight(280)]
    small = passport_queue_model(flights, MockConfig(passport_counter_count=4), demand_fn, window_minutes=_W)
    large = passport_queue_model(flights, MockConfig(passport_counter_count=8), demand_fn, window_minutes=_W)

    assert large["utilization"] < small["utilization"]
    assert large["estimated_wait_minutes"] < small["estimated_wait_minutes"]


def test_passport_empty_window_is_low_risk():
    result = passport_queue_model([], MockConfig(), demand_fn, window_minutes=_W)
    assert result["expected_passengers"] == 0
    assert result["utilization"] == 0.0
    assert result["risk"] == "LOW"


def test_default_window_minutes_matches_production_constant():
    """
    ADIM 6D-2 HOURLY MIGRATION: `passport_queue_model()`'e `window_minutes`
    verilmezse production'ın GÜNCEL saatlik varsayılanı (60) kullanıldığını
    doğrudan kanıtlar - bu dosyanın geri kalanının `window_minutes=15`
    pinlemesinin NEDEN gerekli olduğunun kanıtı.
    """
    from app.queue.constants import DEMAND_WINDOW_MINUTES
    assert DEMAND_WINDOW_MINUTES == 60

    with_default = passport_queue_model([MockFlight(160)], MockConfig(), demand_fn)
    with_explicit_60 = passport_queue_model(
        [MockFlight(160)], MockConfig(), demand_fn, window_minutes=60
    )
    assert with_default == with_explicit_60


@pytest.mark.parametrize(
    "demand,expected_rho,expected_backlog",
    [(240, 0.5, 0.0), (480, 1.0, 0.0), (600, 1.25, 120.0)],
)
def test_security_real_queue_capacity_examples(demand, expected_rho, expected_backlog):
    result = security_queue_model(
        [MockFlight(demand)], MockConfig(), demand_fn, window_minutes=60
    )
    assert result["server_count"] == 8
    assert result["service_rate"] == 1.0
    assert result["capacity_rate"] == 8.0
    assert result["utilization"] == expected_rho
    assert result["backlog_end"] == expected_backlog
    assert result["estimated_wait_minutes"] is not None


# --------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------

def test_confidence_best_case():
    """
    En iyi durumda bile load factor (-0.10) ve boarding buffer (-0.10)
    cezaları koşulsuz uygulanır.
    """
    flights = [MockFlight(aircraft_icao="A320")]
    score = confidence_score(flights, True, 1.0, False)
    assert score == pytest.approx(0.80)


def test_confidence_all_penalties():
    flights = [MockFlight(aircraft_icao=None)]
    score = confidence_score(flights, False, 0.1, True)
    # 1.0 -0.10 -0.10 -0.15 -0.10 -0.10 -0.05 = 0.40
    assert score == pytest.approx(0.40)


def test_confidence_never_below_floor():
    flights = [MockFlight(aircraft_icao=None) for _ in range(5)]
    score = confidence_score(flights, False, 0.0, True)
    assert score >= 0.10


def test_confidence_drops_when_aircraft_unmatched():
    """Senaryo 14'ün confidence ayağı."""
    matched = confidence_score([MockFlight(aircraft_icao="B77W")], True, 1.0, False)
    unmatched = confidence_score([MockFlight(aircraft_icao=None)], True, 1.0, False)
    assert unmatched < matched


def test_confidence_drops_when_match_rate_low():
    high = confidence_score([MockFlight()], True, 0.95, False)
    low = confidence_score([MockFlight()], True, 0.5, False)
    assert low == pytest.approx(high - 0.15)
