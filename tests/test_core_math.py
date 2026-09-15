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
)


class MockConfig:
    """AirportOperationalConfig'in varsayılanlarıyla aynı değerler."""

    def __init__(
        self,
        passport_counter_count=4,
        passport_staff_count=8,
        passport_staff_per_counter=2.0,
        passport_service_rate_per_staff=0.5,
        passport_efficiency_multiplier=1.5,
    ):
        self.passport_counter_count = passport_counter_count
        self.passport_staff_count = passport_staff_count
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
    # mu_per_counter = service_rate_per_staff * staff_per_counter * efficiency
    #                = 0.5 * 2.0 * 1.5 = 1.5 yolcu/dakika/gişe
    assert passport_effective_service_rate(MockConfig()) == pytest.approx(1.5)


def test_passport_effective_service_rate_one_staff_per_counter():
    """MADDE 1: staff_per_counter=1 -> mu yarıya iner (staff_count değil)."""
    config = MockConfig(passport_staff_per_counter=1.0)
    assert passport_effective_service_rate(config) == pytest.approx(0.75)


def test_passport_effective_service_rate_two_staff_per_counter():
    """MADDE 1: staff_per_counter=2 (varsayılan) -> mu = 1.5."""
    config = MockConfig(passport_staff_per_counter=2.0)
    assert passport_effective_service_rate(config) == pytest.approx(1.5)


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
    assert passport_effective_service_rate(mismatched) == pytest.approx(1.5)


def test_passport_low_utilization():
    """
    2 uçuş x 100 yolcu = 200 yolcu / 15 dk -> lambda = 13.33
    c*mu = 4 * 1.5 = 6.0  -> rho = 2.22 (kapasiteyi aşar)
    Bu yüzden düşük talep senaryosu için küçük rakam kullanılır.
    """
    flights = [MockFlight(15)]          # 15 yolcu / 15 dk -> lambda = 1.0
    result = passport_queue_model(flights, MockConfig(), demand_fn)

    assert result["arrival_rate"] == pytest.approx(1.0)
    assert result["utilization"] == pytest.approx(1.0 / 6.0, abs=1e-3)
    assert result["risk"] == "LOW"
    assert result["estimated_wait_minutes"] is not None
    assert result["estimated_wait_minutes"] >= 0


def test_passport_wait_time_matches_erlang_reference():
    flights = [MockFlight(36)]          # lambda = 2.4
    config = MockConfig()
    result = passport_queue_model(flights, config, demand_fn)

    lam, c, mu = 2.4, 4, 1.5
    expected = erlang_c_wait_time(c, lam, mu)
    assert result["estimated_wait_minutes"] == pytest.approx(round(expected, 1))


@pytest.mark.parametrize(
    "demand,expected_risk",
    [
        (30, "LOW"),        # rho 0.33
        (62, "LOW"),        # rho 0.69
        (64, "MEDIUM"),     # rho 0.71
        (80, "MEDIUM"),     # rho 0.89
        (82, "HIGH"),       # rho 0.91
        (88, "HIGH"),       # rho 0.98
    ],
)
def test_passport_risk_bands(demand, expected_risk):
    """c=4, mu=1.5 -> capacity_rate=6.0/dk -> pencere kapasitesi 90 yolcu."""
    result = passport_queue_model([MockFlight(demand)], MockConfig(), demand_fn)
    assert result["risk"] == expected_risk


def test_passport_risk_bands_change_with_counter_count():
    """MADDE 2: c doğru şekilde counter_count olmalı - sayısı değişince rho değişmeli."""
    flights = [MockFlight(80)]
    few_counters = passport_queue_model(
        flights, MockConfig(passport_counter_count=4), demand_fn
    )
    many_counters = passport_queue_model(
        flights, MockConfig(passport_counter_count=8), demand_fn
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

    90 yolcu / 15 dk = 6.0 = c*mu (4*1.5) -> rho tam 1.0.
    backlog_start=0 (varsayılan) -> queue_ahead = 0 + 90 = 90;
    wait = 90 / capacity_rate(6) = 15.0 dk.
    """
    result = passport_queue_model([MockFlight(90)], MockConfig(), demand_fn)

    assert result["utilization"] == pytest.approx(1.0)
    assert result["estimated_wait_minutes"] == 15.0
    assert result["risk"] == "CRITICAL"
    assert result["reasons"] == [
        "Talep kapasiteyi aşıyor — kuyruk teorik olarak sürdürülemez"
    ]


def test_passport_far_overload_scales_wait_up_not_none():
    """
    ADIM 6D ile GÜNCELLENDİ: 5000 yolculuk çok daha ağır bir aşım,
    ESKİ None davranışı yerine ORANTILI olarak DAHA UZUN (sahte bir
    üst sınıra çarpmadan) bir bekleme üretmeli.
    queue_ahead=5000 -> wait = 5000/6 = 833.3 dk.
    """
    result = passport_queue_model([MockFlight(5000)], MockConfig(), demand_fn)
    assert result["estimated_wait_minutes"] == round(5000 / 6, 1)
    assert result["risk"] == "CRITICAL"


def test_passport_config_override_changes_result():
    """
    YASAK 5 yönü: config havalimanı bazlı override edilebilir olmalı.
    Aynı talep, daha çok gişe -> daha düşük utilization.
    """
    flights = [MockFlight(80)]
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
