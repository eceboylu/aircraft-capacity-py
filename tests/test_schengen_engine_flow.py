"""
ADIM (Schengen-Aware Passport Routing) - engine-seviyesi entegrasyon:
Schengen->Schengen kalkış passport'u ATLAR ama international security'ye
show-up zamanıyla DOĞRUDAN girer (passport completion_time'ından
TÜRETİLMEZ - Bölüm 23) ve HİÇBİR yolcu iki kez sayılmaz (Bölüm 24).
"""
from datetime import datetime

from app.queue.config import default_config
from app.queue.constants import PROCESS_PASSPORT_DEPARTURE, PROCESS_SECURITY_INTL
from app.queue.domain.demand import DemandCalculator, departure_show_up_events
from app.queue.engine import _event_driven_queue_demand, predict_airport

from .conftest import FakeCapacityResult, FakeResolver, make_departure

WHEN = datetime(2026, 3, 10, 9, 0)


def _large_config():
    return default_config("ZRH", scale="large")


def test_schengen_departure_generates_zero_passport_demand():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    flight = make_departure(
        when=WHEN, location="international", requires_passport=False, aircraft_icao="A321"
    )

    predictions = predict_airport("ZRH", [flight], _large_config(), demand)
    passport_departure_total = sum(
        p.expected_passengers for p in predictions if p.process == PROCESS_PASSPORT_DEPARTURE
    )
    assert passport_departure_total == 0


def test_schengen_departure_full_demand_reaches_international_security():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    flight = make_departure(
        when=WHEN, location="international", requires_passport=False, aircraft_icao="A321"
    )
    total_demand = demand.passenger_demand(flight)

    predictions = predict_airport("ZRH", [flight], _large_config(), demand)
    security_intl_total = sum(
        p.expected_passengers for p in predictions if p.process == PROCESS_SECURITY_INTL
    )
    assert security_intl_total == total_demand


def test_non_schengen_departure_demand_conserved_through_passport_and_security():
    """Non-Schengen: mevcut zincir (show-up -> passport -> security) demand'i KORUR."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    flight = make_departure(
        when=WHEN, location="international", requires_passport=True, aircraft_icao="A321"
    )
    total_demand = demand.passenger_demand(flight)

    predictions = predict_airport("ZRH", [flight], _large_config(), demand)
    passport_departure_total = sum(
        p.expected_passengers for p in predictions if p.process == PROCESS_PASSPORT_DEPARTURE
    )
    security_intl_total = sum(
        p.expected_passengers for p in predictions if p.process == PROCESS_SECURITY_INTL
    )
    assert passport_departure_total == total_demand
    assert security_intl_total == total_demand


def test_schengen_direct_security_arrival_time_equals_show_up_not_passport_completion():
    """
    ADIM Bölüm 23 - Schengen yolcuların security arrival_time'ı KENDİ
    show-up event zamanı OLMALI, passport completion_time'ından ASLA
    TÜRETİLMEMELİ (var olmayan bir passport gecikmesi icat edilmez).
    Bu, `_event_driven_queue_demand()`'ın (private, ADIM (MySQL...) ile
    AYNI şekilde `test_dynamic_staffing.py` da özel fonksiyon test eder)
    ham `process_events` çıktısı üzerinden DOĞRUDAN doğrulanır.
    """
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    flight = make_departure(
        when=WHEN, location="international", requires_passport=False, aircraft_icao="A321"
    )
    total_demand = demand.passenger_demand(flight)
    expected_show_up_times = {t for t, _ in departure_show_up_events(flight, total_demand)}

    coupling = _event_driven_queue_demand(
        [flight], _large_config(), demand, now=datetime(2026, 3, 11, 0, 0)
    )
    security_intl_events = coupling["process_events"][PROCESS_SECURITY_INTL]
    actual_arrival_times = {e.arrival_time for e in security_intl_events}

    assert actual_arrival_times == expected_show_up_times


def test_no_double_security_demand_for_schengen_departure():
    """
    Bölüm 24 - Schengen yolcu security'ye SADECE BİR KEZ (doğrudan)
    girmeli; passport completion'ından İKİNCİ bir security event'i
    ASLA türetilmemeli (çünkü passport'a hiç girmiyor).
    """
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    flight = make_departure(
        when=WHEN, location="international", requires_passport=False, aircraft_icao="A321"
    )
    total_demand = demand.passenger_demand(flight)

    coupling = _event_driven_queue_demand(
        [flight], _large_config(), demand, now=datetime(2026, 3, 11, 0, 0)
    )
    security_intl_events = coupling["process_events"][PROCESS_SECURITY_INTL]
    passport_departure_events = coupling["process_events"][PROCESS_PASSPORT_DEPARTURE]

    assert passport_departure_events == []
    assert sum(e.count for e in security_intl_events) == total_demand
