"""
MADDE 7 doğrulaması - security risk artık SADECE uçuş sayısına değil,
uçuş oranı VE yolcu oranına göre hesaplanıyor.

Security tarafında Erlang-C, passport kuyruk modeli veya passport
servis hızı KULLANILMAZ - bu dosya bunu da açıkça test eder.

Kapasiteler gerçekçi TEST fixture'larıdır (A320=180, A321=220,
B738=189, B77W=350); production seed'e hiçbir veri eklenmemiştir.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.baseline import (
    get_baseline,
    get_passenger_baseline,
    record_observation,
)
from app.queue.constants import (
    LOCATION_DOMESTIC,
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    SECURITY_FLIGHT_RATIO_WEIGHT,
    SECURITY_PASSENGER_RATIO_WEIGHT,
)
from app.queue.core.scoring import (
    combined_security_ratio,
    security_density_score,
)
from app.queue.domain.demand import DemandCalculator

from .factories import MockCapacityResolver, at, departure


class DemandFlight:
    """Talebi doğrudan taşıyan sade mock - oran matematiği için."""

    def __init__(self, demand=100, aircraft_icao="A320"):
        self.demand = demand
        self.aircraft_icao = aircraft_icao
        self.location = LOCATION_DOMESTIC


def demand_fn(flight):
    return flight.demand


def flights_with_total(count: int, total_demand: int) -> list[DemandFlight]:
    """count uçuş, toplam talebi tam olarak total_demand olacak şekilde."""
    if count == 0:
        return []
    per_flight = total_demand // count
    remainder = total_demand - per_flight * count
    flights = [DemandFlight(per_flight) for _ in range(count)]
    flights[0].demand += remainder
    return flights


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------
# Birleşik formül
# --------------------------------------------------------------------

def test_weights_are_defined_as_constants_not_magic_numbers():
    assert SECURITY_FLIGHT_RATIO_WEIGHT > 0
    assert SECURITY_PASSENGER_RATIO_WEIGHT > 0


def test_combined_ratio_is_normalized_weighted_average():
    combined = combined_security_ratio(1.0, 3.0)
    expected = (
        SECURITY_FLIGHT_RATIO_WEIGHT * 1.0
        + SECURITY_PASSENGER_RATIO_WEIGHT * 3.0
    ) / (SECURITY_FLIGHT_RATIO_WEIGHT + SECURITY_PASSENGER_RATIO_WEIGHT)
    assert combined == pytest.approx(expected)


def test_combined_ratio_falls_back_to_flight_ratio_when_passenger_missing():
    """
    Yolcu baseline'ı yoksa eksik sinyal 0 sayılıp riski AŞAĞI çekmemeli;
    yalnızca flight_ratio geçerli olmalı.
    """
    assert combined_security_ratio(1.6, None) == 1.6


def test_combined_ratio_stays_on_same_scale_as_inputs():
    """İki oran da eşitse birleşik oran da aynı olmalı (ölçek korunur)."""
    assert combined_security_ratio(1.5, 1.5) == pytest.approx(1.5)


# --------------------------------------------------------------------
# 1. Flight ratio TEK BAŞINA riski değiştiriyor
# --------------------------------------------------------------------

def test_flight_ratio_alone_changes_risk():
    calm = security_density_score(
        flights_with_total(10, 1000), 10, demand_fn,
        historical_passenger_baseline=1000,
    )
    busy = security_density_score(
        flights_with_total(20, 1000), 10, demand_fn,
        historical_passenger_baseline=1000,
    )

    # Yolcu tarafı sabit (pr=1.0), sadece uçuş sayısı iki katına çıktı.
    # fr=2.0, pr=1.0 -> birleşik (2.0+1.0)/2 = 1.5 -> HIGH.
    # Tek sinyal yükselince ortalama sönümlenir; bu kasıtlıdır.
    assert calm["passenger_ratio"] == busy["passenger_ratio"] == 1.0
    assert busy["flight_ratio"] == 2.0
    assert busy["flight_ratio"] > calm["flight_ratio"]
    assert busy["baseline_ratio"] == pytest.approx(1.5)
    assert busy["baseline_ratio"] > calm["baseline_ratio"]
    assert calm["risk"] == "LOW"
    assert busy["risk"] == "HIGH"


def test_extreme_flight_ratio_alone_can_reach_critical():
    """Tek sinyal yeterince yükselirse CRITICAL'e ulaşabilir (fr=5, pr=1)."""
    score = security_density_score(
        flights_with_total(50, 1000), 10, demand_fn,
        historical_passenger_baseline=1000,
    )
    assert score["flight_ratio"] == 5.0
    assert score["passenger_ratio"] == 1.0
    assert score["baseline_ratio"] == pytest.approx(3.0)
    assert score["risk"] == "CRITICAL"


# --------------------------------------------------------------------
# 2. Passenger ratio TEK BAŞINA riski değiştiriyor
# --------------------------------------------------------------------

def test_passenger_ratio_alone_changes_risk():
    light = security_density_score(
        flights_with_total(10, 1000), 10, demand_fn,
        historical_passenger_baseline=1000,
    )
    heavy = security_density_score(
        flights_with_total(10, 3000), 10, demand_fn,
        historical_passenger_baseline=1000,
    )

    # Uçuş tarafı sabit (fr=1.0), sadece yolcu talebi üç katına çıktı.
    assert light["flight_ratio"] == heavy["flight_ratio"] == 1.0
    assert heavy["passenger_ratio"] > light["passenger_ratio"]
    assert heavy["baseline_ratio"] > light["baseline_ratio"]
    assert light["risk"] == "LOW"
    assert heavy["risk"] == "CRITICAL"


def test_passenger_ratio_is_ignored_when_no_passenger_baseline():
    """Yolcu baseline'ı yokken sonuç, eski (yalnız uçuş) davranışla aynı."""
    without = security_density_score(
        flights_with_total(10, 9999), 10, demand_fn,
        historical_passenger_baseline=None,
    )
    assert without["passenger_ratio"] is None
    assert without["baseline_ratio"] == without["flight_ratio"] == 1.0
    assert without["risk"] == "LOW"


# --------------------------------------------------------------------
# 3. Aynı flight count, farklı uçak kapasitesi -> farklı risk
# --------------------------------------------------------------------

def test_same_flight_count_different_capacity_gives_different_risk():
    """
    4 uçuş sabit. A320 (180 koltuk) yerine B77W (350 koltuk) gelirse
    yolcu talebi büyür -> passenger_ratio büyür -> risk artar.
    """
    calc = DemandCalculator(MockCapacityResolver())

    narrow = [
        departure(9, m, aircraft="A320", location=LOCATION_DOMESTIC,
                  key=f"N{m}", number=str(m))
        for m in (0, 3, 6, 9)
    ]
    wide = [
        departure(9, m, aircraft="B77W", location=LOCATION_DOMESTIC,
                  key=f"W{m}", number=str(m))
        for m in (0, 3, 6, 9)
    ]

    narrow_demand = sum(calc.passenger_demand(f) for f in narrow)

    narrow_score = security_density_score(
        narrow, 4, calc.passenger_demand,
        historical_passenger_baseline=narrow_demand,
    )
    wide_score = security_density_score(
        wide, 4, calc.passenger_demand,
        historical_passenger_baseline=narrow_demand,
    )

    assert narrow_score["flight_count"] == wide_score["flight_count"] == 4
    assert narrow_score["flight_ratio"] == wide_score["flight_ratio"] == 1.0
    assert wide_score["expected_passengers"] > narrow_score["expected_passengers"]
    assert wide_score["passenger_ratio"] > narrow_score["passenger_ratio"]
    assert wide_score["risk"] != narrow_score["risk"]


def test_realistic_capacities_drive_demand():
    """A320=180, A321=220, B738=189 - gerçekçi TEST kapasiteleri."""
    calc = DemandCalculator(MockCapacityResolver())
    for icao, seats in (("A320", 180), ("A321", 220), ("B738", 189)):
        flight = departure(9, 0, aircraft=icao, location=LOCATION_DOMESTIC)
        assert calc.seat_capacity(flight) == seats
        assert calc.passenger_demand(flight) == round(seats * 0.78)


# --------------------------------------------------------------------
# 4. Aynı passenger count, farklı flight count -> farklı risk
# --------------------------------------------------------------------

def test_same_passenger_count_different_flight_count_gives_different_risk():
    few = security_density_score(
        flights_with_total(4, 1200), 4, demand_fn,
        historical_passenger_baseline=1200,
    )
    many = security_density_score(
        flights_with_total(12, 1200), 4, demand_fn,
        historical_passenger_baseline=1200,
    )

    assert few["expected_passengers"] == many["expected_passengers"] == 1200
    assert few["passenger_ratio"] == many["passenger_ratio"] == 1.0
    assert many["flight_ratio"] > few["flight_ratio"]
    assert many["baseline_ratio"] > few["baseline_ratio"]
    assert few["risk"] == "LOW"
    assert many["risk"] == "CRITICAL"


# --------------------------------------------------------------------
# 5. Unknown capacity - sahte yolcu üretilmiyor
# --------------------------------------------------------------------

def test_general_aviation_contributes_zero_passengers():
    """
    counts_toward_passenger_total=False olan uçuş yolcu talebine HİÇ
    girmez - passenger_ratio'yu yapay olarak şişirmez.
    """
    calc = DemandCalculator(MockCapacityResolver(excluded={"C208"}))
    ga_flights = [
        departure(9, m, aircraft="C208", location=LOCATION_DOMESTIC,
                  key=f"GA{m}", number=str(m))
        for m in (0, 3, 6)
    ]

    score = security_density_score(
        ga_flights, 3, calc.passenger_demand,
        historical_passenger_baseline=500,
    )

    assert score["expected_passengers"] == 0
    assert score["passenger_ratio"] == 0.0
    assert score["flight_count"] == 3


def test_queue_layer_never_invents_its_own_capacity_number():
    """
    Yolcu talebi TAMAMEN AircraftCapacityService'ten gelir; queue
    katmanının kendi kapasite sabiti yoktur. Resolver'ın döndürdüğü
    kapasite değişince talep birebir onu takip etmeli.
    """
    flight = departure(9, 0, aircraft="A320", location=LOCATION_DOMESTIC)

    normal = DemandCalculator(MockCapacityResolver())
    swapped = DemandCalculator(
        MockCapacityResolver(capacities={"A320": 300})
    )

    assert normal.passenger_demand(flight) == round(180 * 0.78)
    assert swapped.passenger_demand(flight) == round(300 * 0.78)


def test_unknown_aircraft_uses_resolver_controlled_default_only():
    """
    Bilinmeyen tip için sayı, resolver'ın kontrollü varsayılanından
    gelir (queue tarafında ikinci bir uydurma default YOKTUR).
    """
    calc = DemandCalculator(MockCapacityResolver(default_capacity=150))
    unknown = departure(9, 0, aircraft="ZZZZ", location=LOCATION_DOMESTIC)

    assert calc.passenger_demand(unknown) == round(150 * 0.78)


# --------------------------------------------------------------------
# 6. Unknown capacity -> security wait time None
# --------------------------------------------------------------------

def test_security_wait_time_is_always_none():
    calc = DemandCalculator(MockCapacityResolver(excluded={"C208"}))
    cases = [
        (flights_with_total(5, 500), 5, 500),
        (flights_with_total(50, 9000), 5, 500),
        (flights_with_total(1, 0), 5, None),
        ([], 5, 500),
    ]
    for flights, baseline, passenger_baseline in cases:
        score = security_density_score(
            flights, baseline, demand_fn,
            historical_passenger_baseline=passenger_baseline,
        )
        assert score["estimated_wait_minutes"] is None

    unknown_capacity = [
        departure(9, 0, aircraft="C208", location=LOCATION_DOMESTIC)
    ]
    score = security_density_score(
        unknown_capacity, 1, calc.passenger_demand,
        historical_passenger_baseline=None,
    )
    assert score["estimated_wait_minutes"] is None


def test_security_never_reports_utilization():
    """utilization passport'a ait bir metriktir; security çıktısında yok."""
    score = security_density_score(
        flights_with_total(10, 1000), 10, demand_fn,
        historical_passenger_baseline=1000,
    )
    assert "utilization" not in score


# --------------------------------------------------------------------
# 7. Baseline sıfır -> division by zero yok
# --------------------------------------------------------------------

def test_zero_flight_baseline_does_not_divide_by_zero():
    score = security_density_score(
        flights_with_total(5, 500), 0, demand_fn,
        historical_passenger_baseline=500,
    )
    assert score["risk"] == "UNKNOWN"
    assert score["baseline_ratio"] is None
    assert score["flight_ratio"] is None
    assert score["passenger_ratio"] is None


def test_zero_passenger_baseline_does_not_divide_by_zero():
    score = security_density_score(
        flights_with_total(5, 500), 5, demand_fn,
        historical_passenger_baseline=0,
    )
    assert score["passenger_ratio"] is None
    assert score["flight_ratio"] == 1.0
    assert score["baseline_ratio"] == 1.0


def test_none_baselines_do_not_crash():
    score = security_density_score(
        flights_with_total(5, 500), None, demand_fn,
        historical_passenger_baseline=None,
    )
    assert score["risk"] == "UNKNOWN"


def test_empty_window_with_baselines_does_not_crash():
    score = security_density_score(
        [], 5, demand_fn, historical_passenger_baseline=500,
    )
    assert score["flight_count"] == 0
    assert score["expected_passengers"] == 0
    assert score["flight_ratio"] == 0.0
    assert score["passenger_ratio"] == 0.0
    assert score["risk"] == "LOW"


# --------------------------------------------------------------------
# 8. Erlang-C security'de ÇAĞRILMIYOR
# --------------------------------------------------------------------

def test_erlang_c_is_never_called_by_security(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("Security Erlang-C kullanmamalı!")

    monkeypatch.setattr(
        "app.queue.core.scoring.erlang_c_wait_time", explode
    )

    score = security_density_score(
        flights_with_total(20, 5000), 5, demand_fn,
        historical_passenger_baseline=500,
    )
    assert score["risk"] == "CRITICAL"
    assert score["estimated_wait_minutes"] is None


def test_security_does_not_use_passport_config(monkeypatch):
    """
    Security skorunun imzasında config yok - passport servis hızı,
    gişe sayısı veya efficiency multiplier'a erişimi bile olmamalı.
    """
    monkeypatch.setattr(
        "app.queue.core.scoring.passport_effective_service_rate",
        lambda config: (_ for _ in ()).throw(
            AssertionError("Security passport servis hızını kullanmamalı!")
        ),
    )
    score = security_density_score(
        flights_with_total(10, 1000), 10, demand_fn,
        historical_passenger_baseline=1000,
    )
    assert score["risk"] == "LOW"


# --------------------------------------------------------------------
# 9. Normal security scoring regresyonu
# --------------------------------------------------------------------

@pytest.mark.parametrize(
    "flight_count,baseline,expected_risk",
    [
        (10, 10, "LOW"),        # ratio 1.0
        (11, 10, "LOW"),        # ratio 1.1
        (12, 10, "MEDIUM"),     # ratio 1.2
        (14, 10, "HIGH"),       # ratio 1.4
        (18, 10, "CRITICAL"),   # ratio 1.8
    ],
)
def test_existing_flight_only_bands_unchanged(flight_count, baseline, expected_risk):
    """
    Yolcu baseline'ı yokken bantlar MADDE 7 öncesiyle birebir aynı
    kalmalı - mevcut scoring modeli bozulmadı.
    """
    score = security_density_score(
        flights_with_total(flight_count, 100 * flight_count), baseline, demand_fn,
    )
    assert score["risk"] == expected_risk
    assert score["baseline_ratio"] == pytest.approx(flight_count / baseline)


def test_result_keeps_existing_contract_fields():
    score = security_density_score(
        flights_with_total(5, 500), 5, demand_fn,
        historical_passenger_baseline=500,
    )
    for key in (
        "flight_count", "expected_passengers", "baseline_ratio",
        "risk", "estimated_wait_minutes", "reasons",
    ):
        assert key in score


# --------------------------------------------------------------------
# 10. Yolcu baseline'ının birikmesi (DB katmanı)
# --------------------------------------------------------------------

def test_passenger_baseline_is_recorded_and_read_back(session):
    record_observation(
        session, "AAA", PROCESS_SECURITY, at(9, 0),
        hour_of_day=9, day_of_week=0,
        flight_count=4, expected_passengers=560,
    )

    assert get_baseline(session, "AAA", PROCESS_SECURITY, 9, 0) == 4.0
    assert get_passenger_baseline(session, "AAA", PROCESS_SECURITY, 9, 0) == 560.0


def test_passenger_baseline_is_none_when_never_recorded(session):
    """Yolcu verisi olmadan kaydedilen gözlem sahte yolcu baseline'ı üretmez."""
    record_observation(
        session, "AAA", PROCESS_SECURITY, at(9, 0),
        hour_of_day=9, day_of_week=0,
        flight_count=4,   # expected_passengers verilmedi
    )

    assert get_baseline(session, "AAA", PROCESS_SECURITY, 9, 0) == 4.0
    assert get_passenger_baseline(session, "AAA", PROCESS_SECURITY, 9, 0) is None


def test_passenger_baseline_averages_across_windows(session):
    record_observation(
        session, "AAA", PROCESS_SECURITY, at(9, 0),
        hour_of_day=9, day_of_week=0, flight_count=4, expected_passengers=400,
    )
    record_observation(
        session, "AAA", PROCESS_SECURITY, at(9, 15),
        hour_of_day=9, day_of_week=0, flight_count=6, expected_passengers=800,
    )

    assert get_baseline(session, "AAA", PROCESS_SECURITY, 9, 0) == 5.0
    assert get_passenger_baseline(session, "AAA", PROCESS_SECURITY, 9, 0) == 600.0


def test_passenger_sample_counts_separately_from_flight_sample(session):
    """
    Önce yolcusuz, sonra yolculu gözlem: uçuş örneklemi 2, yolcu
    örneklemi 1 olmalı - olmayan yolcu gözlemi varmış gibi sayılmaz.
    """
    record_observation(
        session, "AAA", PROCESS_SECURITY, at(9, 0),
        hour_of_day=9, day_of_week=0, flight_count=4,
    )
    record_observation(
        session, "AAA", PROCESS_SECURITY, at(9, 15),
        hour_of_day=9, day_of_week=0, flight_count=6, expected_passengers=900,
    )

    assert get_baseline(session, "AAA", PROCESS_SECURITY, 9, 0) == 5.0
    # 900 tek gözlemdir; 0'la ortalanıp 450'ye düşürülmez.
    assert get_passenger_baseline(session, "AAA", PROCESS_SECURITY, 9, 0) == 900.0


def test_processes_keep_separate_passenger_baselines(session):
    record_observation(
        session, "AAA", PROCESS_SECURITY, at(9, 0),
        hour_of_day=9, day_of_week=0, flight_count=4, expected_passengers=400,
    )
    record_observation(
        session, "AAA", PROCESS_PASSPORT, at(9, 0),
        hour_of_day=9, day_of_week=0, flight_count=2, expected_passengers=900,
    )

    assert get_passenger_baseline(session, "AAA", PROCESS_SECURITY, 9, 0) == 400.0
    assert get_passenger_baseline(session, "AAA", PROCESS_PASSPORT, 9, 0) == 900.0
