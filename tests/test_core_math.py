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
    security_density_score,
)


class MockConfig:
    """AirportOperationalConfig'in varsayılanlarıyla aynı değerler."""

    def __init__(
        self,
        passport_counter_count=4,
        passport_service_rate_per_staff=0.5,
        passport_efficiency_multiplier=1.5,
    ):
        self.passport_counter_count = passport_counter_count
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
    # 0.5 * 1.5 = 0.75 yolcu/dakika/gişe
    assert passport_effective_service_rate(MockConfig()) == pytest.approx(0.75)


def test_passport_low_utilization():
    """
    2 uçuş x 100 yolcu = 200 yolcu / 15 dk -> lambda = 13.33
    c*mu = 4 * 0.75 = 3.0  -> rho = 4.44 (kapasiteyi aşar)
    Bu yüzden düşük talep senaryosu için küçük rakam kullanılır.
    """
    flights = [MockFlight(15)]          # 15 yolcu / 15 dk -> lambda = 1.0
    result = passport_queue_model(flights, MockConfig(), demand_fn)

    assert result["arrival_rate"] == pytest.approx(1.0)
    assert result["utilization"] == pytest.approx(1.0 / 3.0, abs=1e-3)
    assert result["risk"] == "LOW"
    assert result["estimated_wait_minutes"] is not None
    assert result["estimated_wait_minutes"] >= 0


def test_passport_wait_time_matches_erlang_reference():
    flights = [MockFlight(36)]          # lambda = 2.4
    config = MockConfig()
    result = passport_queue_model(flights, config, demand_fn)

    lam, c, mu = 2.4, 4, 0.75
    expected = erlang_c_wait_time(c, lam, mu)
    assert result["estimated_wait_minutes"] == pytest.approx(round(expected, 1))


@pytest.mark.parametrize(
    "demand,expected_risk",
    [
        (15, "LOW"),        # rho 0.33
        (31, "LOW"),        # rho 0.69
        (32, "MEDIUM"),     # rho 0.71
        (40, "MEDIUM"),     # rho 0.89
        (41, "HIGH"),       # rho 0.91
        (44, "HIGH"),       # rho 0.98
    ],
)
def test_passport_risk_bands(demand, expected_risk):
    result = passport_queue_model([MockFlight(demand)], MockConfig(), demand_fn)
    assert result["risk"] == expected_risk


def test_passport_overload_returns_none_wait_and_critical():
    """Senaryo 13: rho >= 1 -> dakika None, risk CRITICAL."""
    # 45 yolcu / 15 dk = 3.0 = c*mu -> rho tam 1.0
    result = passport_queue_model([MockFlight(45)], MockConfig(), demand_fn)

    assert result["utilization"] == pytest.approx(1.0)
    assert result["estimated_wait_minutes"] is None
    assert result["risk"] == "CRITICAL"
    assert result["reasons"] == [
        "Talep kapasiteyi aşıyor — kuyruk teorik olarak sürdürülemez"
    ]


def test_passport_far_overload_still_none_wait():
    result = passport_queue_model([MockFlight(5000)], MockConfig(), demand_fn)
    assert result["estimated_wait_minutes"] is None
    assert result["risk"] == "CRITICAL"


def test_passport_config_override_changes_result():
    """
    YASAK 5 yönü: config havalimanı bazlı override edilebilir olmalı.
    Aynı talep, daha çok gişe -> daha düşük utilization.
    """
    flights = [MockFlight(40)]
    small = passport_queue_model(flights, MockConfig(passport_counter_count=4), demand_fn)
    large = passport_queue_model(flights, MockConfig(passport_counter_count=8), demand_fn)

    assert large["utilization"] < small["utilization"]
    assert large["estimated_wait_minutes"] < small["estimated_wait_minutes"]


def test_passport_empty_window_is_low_risk():
    result = passport_queue_model([], MockConfig(), demand_fn)
    assert result["expected_passengers"] == 0
    assert result["utilization"] == 0.0
    assert result["risk"] == "LOW"


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
