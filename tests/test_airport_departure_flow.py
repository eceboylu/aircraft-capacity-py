"""
ADIM (Airport Departure Passenger Flow) - QUEUE WAIT DEĞİL. "Bu zaman
aralığında havalimanına kaç departure yolcusu GELİYOR (show-up)?"
sorusunun cevabı - security/passport/backlog/remaining-wait hesaplarına
HİÇ dokunulmuyor. Source of truth: mevcut `departure_show_up_events_
detailed()` (production'ın KENDİ kullandığı fonksiyon) - farklı bir
yolcu-üretim formülü İCAT EDİLMEDİ, SADECE `queue_cohort_audit`'i
besleyen AYNI (cohort_time, count) çiftleri, sınıflandırma (domestic/
schengen/non_schengen) bazında yeniden toplanıyor.
"""
from datetime import datetime

import pytest

from app.queue import audit
from app.queue.domain.demand import DemandCalculator, departure_show_up_events_detailed
from app.queue.models import Flight

from .conftest import FakeCapacityResult, FakeResolver


class _FakeSession:
    def __init__(self):
        self.added: list = []

    def add_all(self, rows):
        self.added.extend(rows)

    def flush(self):
        pass


COUNTRIES = {"IST": "TR", "JFK": "US", "CDG": "FR", "SAW": "TR", "AMS": "NL"}


def _departure(**overrides) -> Flight:
    base = dict(
        flight_key="F1", airport_iata="IST", direction="departure", location="domestic",
        requires_passport=True, airline_iata="TK", flight_number="1", flight_iata="TK1",
        aircraft_icao="A321", aircraft_match_found=True,
        dep_iata="IST", arr_iata="SAW",
        dep_scheduled_utc=datetime(2026, 3, 10, 14, 0),
        status="scheduled",
    )
    base.update(overrides)
    return Flight(**base)


def _arrival(**overrides) -> Flight:
    base = dict(
        flight_key="A1", airport_iata="IST", direction="arrival", location="domestic",
        requires_passport=True, airline_iata="TK", flight_number="2", flight_iata="TK2",
        aircraft_icao="A321", aircraft_match_found=True,
        dep_iata="SAW", arr_iata="IST",
        arr_scheduled_utc=datetime(2026, 3, 10, 14, 0),
        status="scheduled",
    )
    base.update(overrides)
    return Flight(**base)


def _run(flights, run_id="run-1", airport_iata="IST", capacity=180):
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=capacity)})
    demand = DemandCalculator(resolver)
    session = _FakeSession()
    audit.record_flight_cohort_and_contribution_audit(
        session, run_id, airport_iata, flights, demand, tz=None,
        country_by_iata=COUNTRIES, coupling={"process_events": {}},
    )
    flow_rows = [r for r in session.added if type(r).__name__ == "QueueAirportDepartureFlow5m"]
    return flow_rows, session


def test_only_departure_flights_included_arrivals_excluded():
    """Bölüm 1/2 - sadece departure; arrival flight'lar dahil DEĞİL."""
    departure = _departure(flight_key="DEP1")
    arrival = _arrival(flight_key="ARR1")
    flow_rows, _ = _run([departure, arrival])

    total_pax_from_flow = sum(r.total_departure_pax for r in flow_rows)
    # Sadece departure'ın 180 kapasitesi sayılmalı - arrival'ın 180'i DAHİL DEĞİL.
    assert total_pax_from_flow == 180.0


def test_domestic_departure_counted_as_domestic_only():
    flight = _departure(flight_key="DOM1", location="domestic", dep_iata="IST", arr_iata="SAW")
    flow_rows, _ = _run([flight])
    assert sum(r.domestic_departure_pax for r in flow_rows) == 180.0
    assert sum(r.schengen_departure_pax for r in flow_rows) == 0.0
    assert sum(r.non_schengen_departure_pax for r in flow_rows) == 0.0


def test_schengen_departure_counted_as_schengen_only():
    flight = _departure(
        flight_key="SCH1", location="international", dep_iata="IST", arr_iata="AMS",
        requires_passport=False,
    )
    flow_rows, _ = _run([flight])
    assert sum(r.schengen_departure_pax for r in flow_rows) == 180.0
    assert sum(r.domestic_departure_pax for r in flow_rows) == 0.0
    assert sum(r.non_schengen_departure_pax for r in flow_rows) == 0.0


def test_non_schengen_departure_counted_as_non_schengen_only():
    flight = _departure(
        flight_key="NSC1", location="international", dep_iata="IST", arr_iata="JFK",
        requires_passport=True,
    )
    flow_rows, _ = _run([flight])
    assert sum(r.non_schengen_departure_pax for r in flow_rows) == 180.0
    assert sum(r.domestic_departure_pax for r in flow_rows) == 0.0
    assert sum(r.schengen_departure_pax for r in flow_rows) == 0.0


def test_total_equals_sum_of_breakdown():
    """Bölüm 4/7 - domestic+schengen+non_schengen = total, HER satırda."""
    flights = [
        _departure(flight_key="D1", location="domestic", dep_iata="IST", arr_iata="SAW"),
        _departure(flight_key="S1", location="international", dep_iata="IST", arr_iata="AMS", requires_passport=False),
        _departure(flight_key="N1", location="international", dep_iata="IST", arr_iata="JFK", requires_passport=True),
    ]
    flow_rows, _ = _run(flights)
    for row in flow_rows:
        assert row.total_departure_pax == pytest.approx(
            row.domestic_departure_pax + row.schengen_departure_pax + row.non_schengen_departure_pax
        )
        assert row.international_departure_pax == pytest.approx(
            row.schengen_departure_pax + row.non_schengen_departure_pax
        )


def test_5m_bucket_sum_matches_manual_cohort_recomputation():
    """Bölüm 7/8/14-A/B - 5m flow row'u, `departure_show_up_events_
    detailed()`'in ÜRETTİĞİ ham cohort'ların manuel toplamıyla BİREBİR eşleşmeli."""
    flight = _departure(flight_key="F16", dep_scheduled_utc=datetime(2026, 3, 10, 16, 0))
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=800)})
    demand = DemandCalculator(resolver)
    total_demand = demand.passenger_demand(flight)
    manual_detailed = departure_show_up_events_detailed(flight, total_demand)
    manual_by_bucket: dict = {}
    for cohort_time, count, _seg in manual_detailed:
        manual_by_bucket[cohort_time] = manual_by_bucket.get(cohort_time, 0.0) + count

    session = _FakeSession()
    audit.record_flight_cohort_and_contribution_audit(
        session, "run-1", "IST", [flight], demand, tz=None,
        country_by_iata=COUNTRIES, coupling={"process_events": {}},
    )
    flow_rows = {r.window_start_utc: r for r in session.added if type(r).__name__ == "QueueAirportDepartureFlow5m"}

    assert set(flow_rows.keys()) == set(manual_by_bucket.keys())
    for window_start, manual_pax in manual_by_bucket.items():
        assert flow_rows[window_start].total_departure_pax == pytest.approx(manual_pax)
        assert flow_rows[window_start].calculation_method == "departure_showup_cohort_sum"


def test_flight_count_reflects_contributing_flights_per_bucket():
    flight_a = _departure(flight_key="A")
    flight_b = _departure(flight_key="B")
    flow_rows, _ = _run([flight_a, flight_b])
    # En az bir bucket'ta HER İKİ flight de aynı anda katkı yapmalı (aynı show-up zamanı).
    assert any(r.flight_count == 2 for r in flow_rows)


def test_airport_isolation_does_not_mix_airports():
    """Bölüm 14-E - iki farklı havalimanı için ayrı ayrı çağrılan
    accumulator birbirine KARIŞMAMALI (her çağrı kendi flow'unu üretir)."""
    ist_flight = _departure(flight_key="IST1", airport_iata="IST", dep_iata="IST", arr_iata="SAW")
    saw_flight = _departure(flight_key="SAW1", airport_iata="SAW", dep_iata="SAW", arr_iata="IST")

    ist_rows, _ = _run([ist_flight], run_id="run-1", airport_iata="IST")
    saw_rows, _ = _run([saw_flight], run_id="run-1", airport_iata="SAW")

    assert all(r.airport_iata == "IST" for r in ist_rows)
    assert all(r.airport_iata == "SAW" for r in saw_rows)
    assert sum(r.total_departure_pax for r in ist_rows) == 180.0
    assert sum(r.total_departure_pax for r in saw_rows) == 180.0


def test_run_isolation_tags_every_row_with_its_own_run_id():
    """Bölüm 14-G."""
    flight = _departure(flight_key="F1")
    rows, _ = _run([flight], run_id="run-XYZ")
    assert rows
    assert all(r.run_id == "run-XYZ" for r in rows)


def test_cancelled_flight_excluded_from_flow():
    flight = _departure(flight_key="CANC1", status="cancelled")
    flow_rows, _ = _run([flight])
    assert sum(r.total_departure_pax for r in flow_rows) == 0.0


def test_no_changes_to_queue_calculations():
    """Bölüm 14 (kural) - bu audit fonksiyonu çağrılırken/sonrasında
    `queue_cohort_audit` satırlarının toplam yolcu sayısı HİÇ
    ETKİLENMEMELİ (departure flow SADECE bir yan-ürün toplama, mevcut
    cohort üretimine ek/başka bir hesap DEĞİL)."""
    flight = _departure(flight_key="F1")
    _flow_rows, session = _run([flight])
    cohort_rows = [r for r in session.added if type(r).__name__ == "QueueCohortAudit"]
    total_cohort_pax = sum(r.passenger_count for r in cohort_rows)
    assert total_cohort_pax == 180.0  # değişmedi, hâlâ aynı production formülü
