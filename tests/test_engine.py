"""
AŞAMA 10 - Senaryo testleri (motor seviyesi).

Dokümandaki 14 senaryonun tamamı burada. Sadece GERÇEK veriden
türetilebilen durumlar test edilir; personel, ekipman, e-gate, hava
durumu, passport sistem arızası için test YOKTUR - bu özellikler
sistemde yok.
"""

from datetime import timedelta

import pytest

from app.queue.config import AirportConfigView, default_config
from app.queue.constants import (
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    REASON_AIRCRAFT_CHANGE,
    REASON_ARRIVAL_BANK,
    REASON_CANCELLATION,
    REASON_CAPACITY_EXCEEDED,
    REASON_CLUSTERING,
    REASON_DELAY_COMPRESSION,
    REASON_DIVERSION,
    REASON_NO_BASELINE,
    REASON_NORMAL,
    REASON_SINGLE_DELAY,
    REASON_WIDEBODY,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_UNKNOWN,
)
from app.queue.domain.demand import DemandCalculator
from app.queue.domain.flight_rules import (
    route_based_load_factor,
    security_arrival_buffer_minutes,
)
from app.queue.engine import (
    floor_to_window,
    predict_airport,
    predict_window,
    window_starts,
)
from app.queue.ingestion.sources import normalize_flight_number

from .factories import MockCapacityResolver, arrival, at, departure


# --------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------

def make_demand(resolver=None) -> DemandCalculator:
    return DemandCalculator(resolver or MockCapacityResolver())


def roomy_config(airport="AAA") -> AirportConfigView:
    """
    Talebi rahat karşılayan passport config'i (düşük rho üretir).

    Gişe sayısı bilinçli olarak yüksek: 15 dakikalık pencerede tek bir
    dar gövde uçağın yolcusu bile 4 gişelik varsayılan config'i doyurur.
    """
    return AirportConfigView(
        airport_iata=airport,
        passport_counter_count=24,
        passport_staff_count=24,
        passport_staff_per_counter=2.0,
        passport_service_rate_per_staff=0.5,
        passport_efficiency_multiplier=1.5,
        arrival_bank_threshold=5,
        is_default=False,
    )


def run_window(
    flights,
    process=PROCESS_SECURITY,
    *,
    airport="AAA",
    config=None,
    baseline=None,
    resolver=None,
    aircraft_changes=None,
    match_rate=1.0,
):
    """Tek pencereyi, o pencereye düşen uçuşlarla hesaplar."""
    from app.queue.engine import _PROCESS_FLIGHTS

    relevant = _PROCESS_FLIGHTS[process](flights)
    starts = window_starts(relevant)
    assert starts, "test uçuşları hiçbir pencereye düşmedi"
    return predict_window(
        airport_iata=airport,
        process=process,
        window_start=starts[0],
        process_flights=relevant,
        config=config or roomy_config(airport),
        demand=make_demand(resolver),
        historical_baseline=baseline,
        aircraft_match_rate=match_rate,
        aircraft_changes=aircraft_changes,
    )


def codes(prediction) -> set[str]:
    return {r.code for r in prediction.reasons}


def intl_departure(hour, minute=0, **kwargs):
    kwargs.setdefault("location", LOCATION_INTERNATIONAL)
    return departure(hour, minute, **kwargs)


# --------------------------------------------------------------------
# Pencere hizalama
# --------------------------------------------------------------------

def test_floor_to_window_aligns_to_quarter_hour():
    assert floor_to_window(at(8, 14)) == at(8, 0)
    assert floor_to_window(at(8, 15)) == at(8, 15)
    assert floor_to_window(at(8, 59)) == at(8, 45)


def test_window_starts_are_sorted_and_unique():
    flights = [departure(9, 0), departure(9, 5), departure(11, 0)]
    starts = window_starts(flights)
    assert starts == sorted(starts)
    assert len(starts) == len(set(starts))


def test_empty_hours_produce_no_prediction_rows():
    """Uçuşu olmayan pencereye satır yazılmaz - tablo şişmez."""
    predictions = predict_airport(
        airport_iata="AAA",
        flights=[departure(9, 0, location=LOCATION_INTERNATIONAL)],
        config=roomy_config(),
        demand=make_demand(),
    )
    # Bir uçuş iki süreci de besler: security + passport, ikişer değil.
    assert len(predictions) == 2
    assert {p.process for p in predictions} == {
        PROCESS_SECURITY, PROCESS_PASSPORT
    }


# --------------------------------------------------------------------
# Senaryo 1 - Normal trafik
# --------------------------------------------------------------------

def test_scenario_01_normal_traffic_security_unknown_passport_low():
    flights = [
        intl_departure(9, 0, key="A1", number="101", aircraft="E190"),
        intl_departure(9, 5, key="A2", number="102", aircraft="E190"),
    ]

    security = run_window(flights, PROCESS_SECURITY)
    assert security.risk == RISK_UNKNOWN          # baseline yok
    assert security.baseline_ratio is None
    assert REASON_NO_BASELINE in codes(security)

    passport = run_window(flights, PROCESS_PASSPORT)
    assert passport.risk == RISK_LOW
    assert passport.estimated_wait_minutes is not None


def test_scenario_01_quiet_window_reason_is_normal_only():
    """
    Hiçbir eşik geçilmediğinde tek bir "normal" maddesi yazılır.
    Baseline var, yoksa no_baseline notu düşerdi.
    """
    flights = [
        departure(9, 0, key="Q1", number="1", aircraft="E190"),
        departure(9, 5, key="Q2", number="2", aircraft="E190"),
    ]
    security = run_window(flights, PROCESS_SECURITY, baseline=10.0)
    assert codes(security) == {REASON_NORMAL}


# --------------------------------------------------------------------
# Senaryo 2 - Departure clustering
# --------------------------------------------------------------------

def test_scenario_02_departure_clustering_high_risk():
    flights = [
        departure(9, m, key=f"C{m}", number=str(m), aircraft="A320")
        for m in range(0, 10)
    ]
    security = run_window(flights, PROCESS_SECURITY, baseline=6.0)

    assert security.flight_count == 10
    assert security.baseline_ratio == pytest.approx(1.67, abs=0.01)
    assert security.risk == RISK_HIGH
    assert REASON_CLUSTERING in codes(security)


def test_scenario_02_clustering_message_carries_ratio():
    flights = [departure(9, m, key=f"C{m}", number=str(m)) for m in range(8)]
    security = run_window(flights, PROCESS_SECURITY, baseline=4.0)
    message = next(
        r.message for r in security.reasons if r.code == REASON_CLUSTERING
    )
    assert "8 uçuş" in message and "2.0 katı" in message


# --------------------------------------------------------------------
# Senaryo 3 - Wide-body dalgası
# --------------------------------------------------------------------

def test_scenario_03_widebody_wave_triggers_reason():
    flights = [
        intl_departure(9, 0, key="W1", number="201", aircraft="B77W"),
        intl_departure(9, 5, key="W2", number="202", aircraft="A388"),
        intl_departure(9, 10, key="W3", number="203", aircraft="A320"),
    ]
    passport = run_window(flights, PROCESS_PASSPORT)

    assert REASON_WIDEBODY in codes(passport)
    widebody = next(
        r for r in passport.reasons if r.code == REASON_WIDEBODY
    )
    assert widebody.metric_value == 2.0


# --------------------------------------------------------------------
# Senaryo 4 ve 5 - Gecikme
# --------------------------------------------------------------------

def test_scenario_04_single_delay_is_info_and_does_not_raise_risk():
    # D1 gecikince efektif saati D2'ninkiyle aynı pencereye düşer.
    flights = [
        intl_departure(9, 0, key="D1", number="301", delay=40),
        intl_departure(9, 40, key="D2", number="302"),
    ]
    passport = run_window(flights, PROCESS_PASSPORT)
    assert passport.flight_count == 2

    assert REASON_SINGLE_DELAY in codes(passport)
    assert REASON_DELAY_COMPRESSION not in codes(passport)
    single = next(
        r for r in passport.reasons if r.code == REASON_SINGLE_DELAY
    )
    assert single.severity == "info"


def test_scenario_05_delayed_flights_compress_into_one_window():
    """
    Üç uçuş farklı saatlere planlı ama hepsi 09:00'a kaydı.
    effective_time actual'ı kullandığı için compression kendiliğinden
    yakalanır - ayrı bir dedektör yok.
    """
    flights = [
        intl_departure(8, 0, key="P1", number="401", delay=60),
        intl_departure(8, 30, key="P2", number="402", delay=30),
        intl_departure(8, 45, key="P3", number="403", delay=15),
    ]
    from app.queue.engine import _PROCESS_FLIGHTS

    relevant = _PROCESS_FLIGHTS[PROCESS_PASSPORT](flights)
    starts = window_starts(relevant)
    # Üçünün de efektif saati aynı pencereye düştü.
    assert len(starts) == 1

    passport = predict_window(
        airport_iata="AAA",
        process=PROCESS_PASSPORT,
        window_start=starts[0],
        process_flights=relevant,
        config=roomy_config(),
        demand=make_demand(),
        historical_baseline=None,
        aircraft_match_rate=1.0,
    )
    assert passport.flight_count == 3
    assert REASON_DELAY_COMPRESSION in codes(passport)


# --------------------------------------------------------------------
# Senaryo 6 ve 7 - İptal ve diversion
# --------------------------------------------------------------------

def test_scenario_06_cancelled_flight_leaves_demand_but_is_noted():
    active = intl_departure(9, 0, key="K1", number="501")
    cancelled = intl_departure(
        9, 5, key="K2", number="502", status="cancelled"
    )
    passport = run_window([active, cancelled], PROCESS_PASSPORT)

    assert passport.flight_count == 1                  # iptal sayılmadı
    assert REASON_CANCELLATION in codes(passport)

    only_active = run_window([active], PROCESS_PASSPORT)
    assert passport.expected_passengers == only_active.expected_passengers


def test_scenario_07_diverted_flight_leaves_demand_but_is_noted():
    active = intl_departure(9, 0, key="V1", number="601")
    diverted = intl_departure(
        9, 5, key="V2", number="602", status="diverted"
    )
    passport = run_window([active, diverted], PROCESS_PASSPORT)

    assert passport.flight_count == 1
    assert REASON_DIVERSION in codes(passport)
    note = next(r for r in passport.reasons if r.code == REASON_DIVERSION)
    assert "V2" in note.message


# --------------------------------------------------------------------
# Senaryo 8 - Aircraft change
# --------------------------------------------------------------------

def test_scenario_08_aircraft_change_reports_capacity_delta():
    flight = intl_departure(9, 0, key="CH1", number="701", aircraft="B77W")
    passport = run_window(
        [flight],
        PROCESS_PASSPORT,
        aircraft_changes={"CH1": ("A320", "B77W")},
    )

    assert REASON_AIRCRAFT_CHANGE in codes(passport)
    change = next(
        r for r in passport.reasons if r.code == REASON_AIRCRAFT_CHANGE
    )
    assert change.metric_value == 170.0     # 350 - 180
    assert "A320→B77W" in change.message


def test_scenario_08_change_of_other_window_is_not_reported():
    """Uçak değişikliği sadece kendi penceresinde görünür."""
    flight = intl_departure(9, 0, key="CH1", number="701")
    passport = run_window(
        [flight],
        PROCESS_PASSPORT,
        aircraft_changes={"BASKA_UCUS": ("A320", "B77W")},
    )
    assert REASON_AIRCRAFT_CHANGE not in codes(passport)


# --------------------------------------------------------------------
# Senaryo 9 - Arrival bank
# --------------------------------------------------------------------

def test_scenario_09_arrival_bank_triggers_for_passport():
    flights = [
        arrival(9, m, key=f"AB{m}", number=str(m), aircraft="A320")
        for m in (0, 2, 4, 6, 8)
    ]
    passport = run_window(flights, PROCESS_PASSPORT)

    assert REASON_ARRIVAL_BANK in codes(passport)
    bank = next(r for r in passport.reasons if r.code == REASON_ARRIVAL_BANK)
    assert bank.metric_value == 5.0


def test_scenario_09_arrival_bank_never_reaches_security():
    flights = [
        arrival(9, m, key=f"AB{m}", number=str(m)) for m in (0, 2, 4, 6, 8)
    ]
    from app.queue.engine import _PROCESS_FLIGHTS

    # Varışlar security akışını hiç beslemiyor.
    assert _PROCESS_FLIGHTS[PROCESS_SECURITY](flights) == []


# --------------------------------------------------------------------
# Senaryo 10 - Uzun mesafe load factor + buffer
# --------------------------------------------------------------------

def test_scenario_10_long_haul_uses_higher_load_factor_and_buffer():
    long_haul = intl_departure(
        12, 0, key="L1", number="801", duration_minutes=600
    )
    short_haul = intl_departure(
        12, 0, key="S1", number="802", duration_minutes=90
    )

    assert route_based_load_factor(long_haul) == 0.88
    assert route_based_load_factor(short_haul) == 0.82
    assert security_arrival_buffer_minutes(long_haul) == 90
    assert security_arrival_buffer_minutes(short_haul) == 45


def test_scenario_10_buffer_separates_flights_into_different_windows():
    """
    Aynı saatte kalkan iki uçuş, buffer farkı yüzünden farklı
    security penceresine düşer.
    """
    long_haul = intl_departure(
        12, 0, key="L1", number="801", duration_minutes=600
    )
    short_haul = intl_departure(
        12, 0, key="S1", number="802", duration_minutes=90
    )
    starts = window_starts([long_haul, short_haul])
    assert starts == [at(10, 30), at(11, 15)]


# --------------------------------------------------------------------
# Senaryo 11 - ÇOK HAVALİMANLI bağımsızlık
# --------------------------------------------------------------------

def test_scenario_11_two_airports_are_computed_independently():
    ist_flights = [
        intl_departure(
            9, m, airport="IST", key=f"IST{m}", number=f"9{m}",
            aircraft="B77W",
        )
        for m in (0, 3, 6, 9, 12)
    ]
    saw_flights = [
        intl_departure(
            9, 0, airport="SAW", key="SAW1", number="1", aircraft="E190"
        )
    ]

    ist = predict_airport(
        airport_iata="IST",
        flights=ist_flights,
        config=default_config("IST"),      # 4 gişe
        demand=make_demand(),
    )
    saw = predict_airport(
        airport_iata="SAW",
        flights=saw_flights,
        config=roomy_config("SAW"),        # 12 gişe
        demand=make_demand(),
    )

    ist_passport = next(p for p in ist if p.process == PROCESS_PASSPORT)
    saw_passport = next(p for p in saw if p.process == PROCESS_PASSPORT)

    assert {p.airport_iata for p in ist} == {"IST"}
    assert {p.airport_iata for p in saw} == {"SAW"}
    assert ist_passport.flight_count == 5
    assert saw_passport.flight_count == 1
    # Aynı config ile hesaplanmadılar: IST dolu, SAW rahat.
    assert ist_passport.risk == RISK_CRITICAL
    assert saw_passport.risk == RISK_LOW


def test_scenario_11_airport_config_override_changes_only_its_own_result():
    flights = [
        intl_departure(9, m, key=f"F{m}", number=str(m), aircraft="A321")
        for m in (0, 3, 6)
    ]
    tight = run_window(
        flights, PROCESS_PASSPORT, config=default_config("AAA")
    )
    roomy = run_window(flights, PROCESS_PASSPORT, config=roomy_config())

    assert tight.expected_passengers == roomy.expected_passengers
    assert tight.utilization > roomy.utilization


# --------------------------------------------------------------------
# Senaryo 12 - Baseline yokken sahte değer üretilmiyor
# --------------------------------------------------------------------

def test_scenario_12_security_without_baseline_is_unknown():
    security = run_window(
        [departure(9, 0), departure(9, 5)], PROCESS_SECURITY, baseline=None
    )
    assert security.risk == RISK_UNKNOWN
    assert security.baseline_ratio is None
    assert security.estimated_wait_minutes is None
    assert REASON_CLUSTERING not in codes(security)


def test_scenario_12_security_never_produces_wait_minutes():
    """Her baseline değerinde security dakikası None kalmalı."""
    flights = [departure(9, m, key=f"N{m}", number=str(m)) for m in range(12)]
    for baseline in (None, 1.0, 5.0, 20.0):
        security = run_window(flights, PROCESS_SECURITY, baseline=baseline)
        assert security.estimated_wait_minutes is None
        assert security.utilization is None


# --------------------------------------------------------------------
# Senaryo 13 - Passport doygunluğu
# --------------------------------------------------------------------

def test_scenario_13_saturated_passport_returns_none_wait_and_critical():
    flights = [
        intl_departure(9, m, key=f"X{m}", number=str(m), aircraft="A388")
        for m in (0, 3, 6, 9)
    ]
    passport = run_window(
        flights, PROCESS_PASSPORT, config=default_config("AAA")
    )

    assert passport.risk == RISK_CRITICAL
    assert passport.estimated_wait_minutes is None
    assert passport.utilization >= 1.0
    assert REASON_CAPACITY_EXCEEDED in codes(passport)


# --------------------------------------------------------------------
# Senaryo 14 - Uçuş no normalizasyonu ve confidence
# --------------------------------------------------------------------

def test_scenario_14_flight_number_normalization():
    assert normalize_flight_number("AC 72") == "AC72"
    assert normalize_flight_number("ac-72") == "AC72"
    assert normalize_flight_number(None) is None


def test_scenario_14_unmatched_aircraft_lowers_confidence():
    matched = intl_departure(9, 0, key="M1", number="901", aircraft="A320")
    unmatched = intl_departure(9, 5, key="M2", number="902", aircraft=None)

    good = run_window([matched], PROCESS_PASSPORT, match_rate=1.0)
    poor = run_window(
        [matched, unmatched], PROCESS_PASSPORT, match_rate=0.5
    )

    assert poor.confidence < good.confidence
    # 1.00 - 0.10 (load factor) - 0.10 (buffer) - 0.15 (match rate)
    #      - 0.10 (null aircraft) - 0.10 (baseline yok)
    assert poor.confidence == pytest.approx(0.45)


def test_scenario_14_unmatched_aircraft_still_produces_demand():
    """Eşleşme yoksa sistem çökmez: Madde 1 varsayılanı devreye girer."""
    unmatched = intl_departure(9, 0, key="M2", number="902", aircraft=None)
    passport = run_window([unmatched], PROCESS_PASSPORT, match_rate=0.0)

    assert passport.flight_count == 1
    assert passport.expected_passengers > 0


# --------------------------------------------------------------------
# Akış kuralları motor seviyesinde (AŞAMA 2)
# --------------------------------------------------------------------

def test_domestic_arrival_feeds_neither_process():
    flights = [
        arrival(9, 0, location=LOCATION_DOMESTIC, key="DA1", number="1")
    ]
    predictions = predict_airport(
        airport_iata="AAA",
        flights=flights,
        config=roomy_config(),
        demand=make_demand(),
    )
    assert predictions == []


def test_domestic_departure_feeds_security_only():
    flights = [
        departure(9, 0, location=LOCATION_DOMESTIC, key="DD1", number="1")
    ]
    predictions = predict_airport(
        airport_iata="AAA",
        flights=flights,
        config=roomy_config(),
        demand=make_demand(),
    )
    assert [p.process for p in predictions] == [PROCESS_SECURITY]


def test_international_departure_feeds_both_processes():
    flights = [intl_departure(9, 0, key="ID1", number="1")]
    predictions = predict_airport(
        airport_iata="AAA",
        flights=flights,
        config=roomy_config(),
        demand=make_demand(),
    )
    assert sorted(p.process for p in predictions) == [
        PROCESS_PASSPORT, PROCESS_SECURITY
    ]


def test_general_aviation_flight_adds_no_passengers():
    resolver = MockCapacityResolver(excluded={"C172"})
    passport = run_window(
        [intl_departure(9, 0, key="GA1", number="1", aircraft="C172")],
        PROCESS_PASSPORT,
        resolver=resolver,
    )
    assert passport.expected_passengers == 0


# --------------------------------------------------------------------
# Çıktı sözleşmesi
# --------------------------------------------------------------------

def test_prediction_serializes_reasons_as_json():
    passport = run_window(
        [intl_departure(9, 0, key="J1", number="1")], PROCESS_PASSPORT
    )
    import json

    parsed = json.loads(passport.reasons_json())
    assert parsed and all(
        set(item) == {"code", "severity", "message", "metric_value"}
        for item in parsed
    )


def test_window_end_is_exactly_one_window_long():
    passport = run_window(
        [intl_departure(9, 0, key="W1", number="1")], PROCESS_PASSPORT
    )
    assert passport.window_end - passport.window_start == timedelta(minutes=15)
