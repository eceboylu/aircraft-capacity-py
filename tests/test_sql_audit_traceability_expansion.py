"""
ADIM (SQL Audit/Traceability Genişletme) - "Bu sayı nereden geldi? Formül
hangi girdileri kullandı? Sonuç ne çıktı?" sorularının SADECE SQL'den
cevaplanabilmesi için eklenen `queue_flight_resolution_audit` tablosu +
diğer tablolara eklenen ek kolonlar (Bölüm 1/16/18). Gerçek DB'ye ihtiyaç
YOK - `_bulk_add()` sadece `session.add_all()`/`.flush()` çağırır (bkz.
tests/test_queue_audit.py `_FakeSession` deseni), flight/config nesneleri
de düz Python instance'ları.

NOT (Passenger-Weighted Display Wait) - `queue_virtual_wait_trace_audit`
ve onu test eden bölüm bu dosyadan KALDIRILDI - `virtual_arrival_wait()`
mekanizmasının kendisi (production-critical olmadığı kanıtlandıktan
sonra) tamamen kaldırıldı, bkz. `tests/test_five_minute_wait_series.py`
(yeni passenger-weighted 5dk/30dk display metriği testleri).
"""
from datetime import datetime, timedelta

import pytest

from app.queue import audit
from app.queue.config import AirportConfigView
from app.queue.domain.demand import DemandCalculator
from app.queue.models import Flight

from .conftest import FakeCapacityResult, FakeResolver


class _FakeSession:
    def __init__(self):
        self.added: list = []

    def add(self, row):
        self.added.append(row)

    def add_all(self, rows):
        self.added.extend(rows)

    def flush(self):
        pass


def _flight(**overrides) -> Flight:
    base = dict(
        flight_key="F1", airport_iata="IST", direction="departure", location="international",
        requires_passport=True, airline_iata="TK", flight_number="1", flight_iata="TK1",
        aircraft_icao="A321", aircraft_match_found=True,
        dep_iata="IST", arr_iata="JFK",
        dep_scheduled_utc=datetime(2026, 3, 10, 12, 0),
        status="scheduled",
    )
    base.update(overrides)
    return Flight(**base)


COUNTRIES = {"IST": "TR", "JFK": "US", "CDG": "FR", "SAW": "TR", "AMS": "NL"}


# ---------------------------------------------------------------------------
# Table 10: queue_flight_resolution_audit
# ---------------------------------------------------------------------------

def test_resolution_audit_prefers_actual_over_estimated_over_scheduled():
    flight = _flight(
        dep_scheduled_utc=datetime(2026, 3, 10, 12, 0),
        dep_estimated_utc=datetime(2026, 3, 10, 12, 5),
        dep_actual_utc=datetime(2026, 3, 10, 12, 7),
    )
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180, source="airline_fleet_exact", confidence="high")})
    demand = DemandCalculator(resolver)
    session = _FakeSession()

    audit.record_flight_resolution_audit(session, "run-1", "IST", [flight], demand, COUNTRIES)

    assert len(session.added) == 1
    row = session.added[0]
    assert row.dep_time_source == "actual"
    assert row.dep_effective_utc == datetime(2026, 3, 10, 12, 7)
    assert row.capacity_source == "airline_fleet_exact"
    assert row.capacity_confidence == "high"
    assert row.resolved_capacity == 180


def test_resolution_audit_falls_back_to_scheduled_when_only_scheduled_present():
    flight = _flight(dep_scheduled_utc=datetime(2026, 3, 10, 12, 0))
    demand = DemandCalculator(FakeResolver({}))
    session = _FakeSession()

    audit.record_flight_resolution_audit(session, "run-1", "IST", [flight], demand, COUNTRIES)

    row = session.added[0]
    assert row.dep_time_source == "scheduled"
    assert row.dep_effective_utc == datetime(2026, 3, 10, 12, 0)


def test_resolution_audit_skips_excluded_status_and_zero_capacity_flights():
    excluded = _flight(flight_key="F-cancelled", status="cancelled")
    zero_capacity = _flight(flight_key="F-ga", aircraft_icao="GA1")
    resolver = FakeResolver({"GA1": FakeCapacityResult(capacity=0, counts_toward_passenger_total=False)})
    demand = DemandCalculator(resolver)
    session = _FakeSession()

    audit.record_flight_resolution_audit(
        session, "run-1", "IST", [excluded, zero_capacity], demand, COUNTRIES,
    )
    assert session.added == []


@pytest.mark.parametrize("direction,dep_iata,arr_iata,location,requires_passport,expected_path", [
    ("departure", "IST", "SAW", "domestic", True, "domestic_departure->security_dom"),
    ("departure", "IST", "AMS", "international", False, "schengen_departure->security_intl_direct"),
    ("departure", "IST", "JFK", "international", True, "non_schengen_departure->passport_dep->security_intl"),
    ("arrival", "AMS", "IST", "international", False, "schengen_arrival->bypass_passport"),
    ("arrival", "JFK", "IST", "international", True, "non_schengen_arrival->passport_arr"),
    ("arrival", "SAW", "IST", "domestic", True, "domestic_arrival->none"),
])
def test_resolution_audit_routing_path_covers_all_branches(
    direction, dep_iata, arr_iata, location, requires_passport, expected_path,
):
    kwargs = dict(
        direction=direction, dep_iata=dep_iata, arr_iata=arr_iata, location=location,
        requires_passport=requires_passport,
    )
    if direction == "departure":
        kwargs["dep_scheduled_utc"] = datetime(2026, 3, 10, 12, 0)
    else:
        kwargs["arr_scheduled_utc"] = datetime(2026, 3, 10, 12, 0)
    flight = _flight(**kwargs)
    demand = DemandCalculator(FakeResolver({}))
    session = _FakeSession()

    audit.record_flight_resolution_audit(session, "run-1", "IST", [flight], demand, COUNTRIES)

    assert session.added[0].routing_path == expected_path


# ---------------------------------------------------------------------------
# Table 5 additions: allowed_levels_snapshot + *_dynamic_enabled flags
# ---------------------------------------------------------------------------

def _mega_dynamic_config(**overrides) -> AirportConfigView:
    base = dict(
        airport_iata="IST", passport_counter_count=8, passport_staff_count=8,
        passport_service_time_minutes=1.5, security_lane_count=20,
        domestic_security_lane_count=30, international_security_lane_count=20,
        security_service_time_minutes=1.0, passport_staff_per_counter=1.0,
        passport_service_rate_per_staff=30.0, passport_efficiency_multiplier=1.0,
        arrival_bank_threshold=5, is_default=True, scale="mega",
        passport_departure_server_count=30, passport_arrival_server_count=30,
        passport_departure_dynamic=True, passport_arrival_dynamic=True,
        passport_departure_server_count_max=60, passport_arrival_server_count_max=60,
        security_intl_dynamic=True, international_security_lane_count_max=40,
    )
    base.update(overrides)
    return AirportConfigView(**base)


def test_resource_config_audit_reports_dynamic_enabled_flags_and_levels():
    session = _FakeSession()
    config = _mega_dynamic_config()

    audit.record_resource_config_audit(session, "run-1", "IST", config)

    row = session.added[0]
    assert row.security_intl_dynamic_enabled is True
    assert row.passport_departure_dynamic_enabled is True
    assert row.passport_arrival_dynamic_enabled is True
    assert "security_intl=20,30,40" in row.allowed_levels_snapshot
    assert "passport_dep=30,40,50,60" in row.allowed_levels_snapshot
    assert "passport_arr=30,40,50,60" in row.allowed_levels_snapshot


def test_resource_config_audit_reports_static_scale_as_all_disabled():
    session = _FakeSession()
    config = _mega_dynamic_config(
        scale="large", passport_departure_dynamic=False, passport_arrival_dynamic=False,
        security_intl_dynamic=False, passport_departure_server_count_max=None,
        passport_arrival_server_count_max=None, international_security_lane_count_max=None,
    )

    audit.record_resource_config_audit(session, "run-1", "IST", config)

    row = session.added[0]
    assert row.security_intl_dynamic_enabled is False
    assert row.passport_departure_dynamic_enabled is False
    assert row.passport_arrival_dynamic_enabled is False
    assert row.allowed_levels_snapshot is None
