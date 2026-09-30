"""
`app/queue/audit.py` - READ-ONLY calculation audit trail.

Kapsam (bkz. audit.py modül docstring'i):
  - Audit tabloları hesaplamayı ASLA etkilemez (calculation -> audit,
    tersi YOK).
  - Cohort/contribution/routing/country satırları GERÇEK sayılarla
    (departure_show_up_events_detailed/arrival_passenger_release_events_
    detailed - production'ın KENDİ fonksiyonları) eşleşir.
  - Multi-flight merge'de per-flight yolcu payı ÇİFTE SAYILMAZ.
"""
from datetime import datetime

import pytest

from app.queue import audit
from app.queue.constants import PROCESS_SECURITY_DOMESTIC
from app.queue.core.event_queue import simulate_fifo_queue
from app.queue.domain.demand import DemandCalculator

from .conftest import FakeCapacityResult, FakeResolver, make_departure, rebuild_orm_row


# --------------------------------------------------------------------------
# A) Structural guarantee: production code never imports/reads audit tables
# --------------------------------------------------------------------------

_AUDIT_MODEL_CLASS_NAMES = [
    "QueueCalculationHourlyAudit", "QueueFlightHourContributionAudit", "QueueCohortAudit",
    "QueueServiceEventAudit", "QueueRoutingSummaryAudit", "QueueCountryRoutingAudit",
    "QueueResourceConfigAudit", "QueueDynamicStaffingAudit", "QueueGraphDisplayAudit",
]


def test_engine_module_does_not_import_or_query_audit_model_classes():
    """
    `engine.py` `app.queue.audit`'in (write-only) fonksiyonlarını
    çağırır ama HİÇBİR audit MODEL sınıfını (`QueueCalculationHourlyAudit`
    vb.) doğrudan import/select ETMEZ - bu isimler engine.py kaynağında
    hiç geçmemeli (sadece `audit.py`'nin İÇİNDE, INSERT için geçerler).
    `QueueWaitDisplay5m` (production tablosu, audit DEĞİL) buna dahil
    değil - engine.py onu zaten meşru şekilde yazıyor.
    """
    import inspect

    import app.queue.engine as engine_module

    source = inspect.getsource(engine_module)
    for class_name in _AUDIT_MODEL_CLASS_NAMES:
        assert class_name not in source, f"engine.py audit model sınıfını referanslıyor: {class_name!r}"


def test_demand_module_never_imports_audit_module():
    """`demand.py` metinsel olarak `audit.py`'den BAHSEDEBİLİR (docstring/
    açıklama), ama GERÇEK bir `import ... audit` satırı OLAMAZ."""
    import inspect

    import app.queue.domain.demand as demand_module

    source = inspect.getsource(demand_module)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            assert "audit" not in stripped.lower(), f"demand.py audit'i import ediyor: {stripped!r}"


def test_audit_module_only_inserts_never_selects_from_its_own_tables():
    """`audit.py`'nin KENDİSİ de kendi tablolarından `select`/`query` YAPMAZ (retention'ın DELETE'i hariç)."""
    import inspect

    source = inspect.getsource(audit)
    # `.query(Model)` sadece `purge_expired_audit_rows()` içinde, sadece
    # `.delete(...)` ile bitmeli - bir SELECT/fetch YOK.
    assert "session.execute(select(Queue" not in source
    assert ".scalars()" not in source
    assert ".all()" not in source or "purge_expired_audit_rows" in source


# --------------------------------------------------------------------------
# B) audit_enabled / retention config
# --------------------------------------------------------------------------

def test_audit_enabled_defaults_true(monkeypatch):
    monkeypatch.delenv("QUEUE_CALCULATION_AUDIT_ENABLED", raising=False)
    assert audit.audit_enabled() is True


def test_audit_can_be_disabled(monkeypatch):
    monkeypatch.setenv("QUEUE_CALCULATION_AUDIT_ENABLED", "false")
    assert audit.audit_enabled() is False


def test_retention_defaults_to_7_days(monkeypatch):
    monkeypatch.delenv("QUEUE_AUDIT_RETENTION_DAYS", raising=False)
    assert audit.audit_retention_days() == 7


def test_retention_reads_env_override(monkeypatch):
    monkeypatch.setenv("QUEUE_AUDIT_RETENTION_DAYS", "14")
    assert audit.audit_retention_days() == 14


def test_run_id_is_unique_per_call():
    a = audit.new_run_id()
    b = audit.new_run_id()
    assert a != b
    assert len(a) == 36  # uuid4 hex-with-dashes format


# --------------------------------------------------------------------------
# C) Percentage/cohort attribution correctness (pure, no DB needed - these
#    functions only build row OBJECTS in plain lists, `session` is never
#    touched by `_emit_*` helpers themselves).
# --------------------------------------------------------------------------

def test_departure_12_13_contribution_matches_known_example():
    """
    Görev örneği: T=16:00, capacity=800, T-240->T-180 (%10) segmenti
    tamamen 12:00-13:00'a düşüyor -> passengers_contributed_to_this_hour=80.
    """
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=800)})
    demand = DemandCalculator(resolver)
    flight = make_departure(when=datetime(2026, 3, 10, 16, 0), location="domestic", flight_key="F16", aircraft_icao="A321")
    total_demand = demand.passenger_demand(flight)
    assert total_demand == 800

    from app.queue.domain.demand import departure_show_up_events_detailed

    detailed = departure_show_up_events_detailed(flight, total_demand)

    cohort_rows, contribution_rows = [], []
    common = dict(
        run_id="test-run", airport_iata="XXX", flight_db_id=None, flight_key="F16",
        flight_iata=None, flight_icao=None, airline_iata=None, airline_icao=None,
        direction="departure", dep_iata=None, arr_iata=None,
        dep_time_utc=None, dep_time_local=None, arr_time_utc=None, arr_time_local=None,
        is_domestic=True, is_international=False, is_schengen=None, requires_passport=True,
        aircraft_icao="A321", resolved_aircraft_capacity=800,
    )
    audit._emit_departure_cohorts_and_contributions(
        cohort_rows, contribution_rows, common, detailed,
        process=PROCESS_SECURITY_DOMESTIC, tz=None,
        routing_source="departure_show_up", routing_destination=PROCESS_SECURITY_DOMESTIC,
        security_arrival_source="direct_show_up",
    )

    hour_12_rows = [
        r for r in contribution_rows
        if r.window_start_utc == datetime(2026, 3, 10, 12, 0)
    ]
    assert len(hour_12_rows) == 1
    row = hour_12_rows[0]
    assert row.profile_percentage == pytest.approx(10.0)
    assert row.passengers_contributed_to_this_hour == 80.0
    assert row.passengers_from_segment == 80.0


def test_non_round_flight_15_20_splits_across_two_hours():
    """Görev örneği: T=15:20, capacity=180, T-180->T-120 (%45)=81 pax -> 12-13'e 54, 13-14'e 27."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    flight = make_departure(when=datetime(2026, 3, 10, 15, 20), location="domestic", flight_key="F1520", aircraft_icao="A321")
    total_demand = demand.passenger_demand(flight)

    from app.queue.domain.demand import departure_show_up_events_detailed
    detailed = departure_show_up_events_detailed(flight, total_demand)

    cohort_rows, contribution_rows = [], []
    common = dict(
        run_id="test-run", airport_iata="XXX", flight_db_id=None, flight_key="F1520",
        flight_iata=None, flight_icao=None, airline_iata=None, airline_icao=None,
        direction="departure", dep_iata=None, arr_iata=None,
        dep_time_utc=None, dep_time_local=None, arr_time_utc=None, arr_time_local=None,
        is_domestic=True, is_international=False, is_schengen=None, requires_passport=True,
        aircraft_icao="A321", resolved_aircraft_capacity=180,
    )
    audit._emit_departure_cohorts_and_contributions(
        cohort_rows, contribution_rows, common, detailed,
        process=PROCESS_SECURITY_DOMESTIC, tz=None,
        routing_source="departure_show_up", routing_destination=PROCESS_SECURITY_DOMESTIC,
        security_arrival_source="direct_show_up",
    )

    seg2_rows = {
        r.window_start_utc: r for r in contribution_rows
        if r.profile_segment == "T-180->T-120"
    }
    assert seg2_rows[datetime(2026, 3, 10, 12, 0)].passengers_contributed_to_this_hour == 54.0
    assert seg2_rows[datetime(2026, 3, 10, 13, 0)].passengers_contributed_to_this_hour == 27.0
    assert seg2_rows[datetime(2026, 3, 10, 12, 0)].passengers_from_segment == 81.0


def test_arrival_1_minute_cohort_example_12_40_300():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=300)})
    demand = DemandCalculator(resolver)
    from .conftest import make_arrival
    flight = make_arrival(when=datetime(2026, 3, 10, 12, 40), location="international", requires_passport=True, flight_key="ARR1", aircraft_icao="A321")
    total_demand = demand.passenger_demand(flight)

    from app.queue.domain.demand import arrival_passenger_release_events_detailed
    detailed = arrival_passenger_release_events_detailed(flight, total_demand)

    minute_1255_to_1259 = [
        (t, c, seg) for t, c, seg in detailed
        if datetime(2026, 3, 10, 12, 55) <= t <= datetime(2026, 3, 10, 12, 59)
    ]
    assert [c for _, c, _ in minute_1255_to_1259] == [21.0, 21.0, 21.0, 21.0, 21.0]
    assert all(seg == (15, 20, 0.35) for _, _, seg in minute_1255_to_1259)


# --------------------------------------------------------------------------
# D) No passenger duplication through multi-flight merge (the bug this
#    task's audit work actually found and fixed).
# --------------------------------------------------------------------------

def test_multi_flight_merge_does_not_duplicate_passengers_in_audit_shares():
    """
    Flight A (5 pax) ve Flight B (3 pax) AYNI (arrival,service_start,
    completion) noktasında merge olduğunda, `source_flight_keys` HER
    flight'ın GERÇEK payını taşımalı - toplamı event.count'u AŞMAMALI.
    """
    base = datetime(2026, 3, 10, 12, 20)
    events = simulate_fifo_queue(
        [(base, "x", 5.0, "A"), (base, "x", 3.0, "B")],
        server_count=100, service_time_minutes=1.5,
    )
    assert len(events) == 1
    event = events[0]
    assert event.count == 8.0
    assert sum(event.source_flight_keys.values()) == pytest.approx(8.0)
    assert event.source_flight_keys == {"A": 5.0, "B": 3.0}


def test_passport_to_security_conservation_per_flight_with_shared_merge():
    """
    İki non-Schengen flight'ın passport completion'ları AYNI merged
    event'te birleşse bile, her flight'ın security_intl'e taşınan payı
    KENDİ show-up toplamına EŞİT olmalı (fazla/az DEĞİL).
    """
    from app.queue.core.event_queue import simulate_passport, simulate_security

    base = datetime(2026, 3, 10, 12, 0)
    # İki flight, AYNI show-up anında, bol server (hepsi anında servise girsin, AYNI completion'da merge olsunlar).
    departure_arrivals = [
        (base, 5.0, "FLIGHT-A"),
        (base, 3.0, "FLIGHT-B"),
    ]
    passport_result = simulate_passport(
        departure_arrivals, [], departure_server_count=100, arrival_server_count=100,
        service_time_minutes=1.0,
    )
    dep_events = passport_result["departure"]
    assert len(dep_events) == 1  # hepsi aynı anda, aynı completion -> tek merged event
    assert dep_events[0].count == 8.0
    assert dep_events[0].source_flight_keys == {"FLIGHT-A": 5.0, "FLIGHT-B": 3.0}

    security_arrivals = [
        (e.completion_time, e.count, e.source_flight_keys) for e in dep_events
    ]
    security_events = simulate_security(security_arrivals, lane_count=100, service_time_minutes=1.0)
    assert sum(e.count for e in security_events) == 8.0
    total_shares: dict = {}
    for e in security_events:
        for k, v in (e.source_flight_keys or {}).items():
            total_shares[k] = total_shares.get(k, 0.0) + v
    assert total_shares == {"FLIGHT-A": 5.0, "FLIGHT-B": 3.0}


# --------------------------------------------------------------------------
# D) `record_hourly_calculation_audit` backlog_end - GLOBAL BACKLOG AUDIT fix.
#
#    backlog_end artık `backlog_start + expected_passengers - total_capacity`
#    (clamp'siz, negatife düşebilen) formülüyle YENİDEN HESAPLANMIYOR.
#    Onun yerine GERÇEK production FIFO state'inden (`coupling[
#    "backlog_start_by_hour"][process]`) bir sonraki saatin backlog_start
#    değeri okunuyor - bu değer zaten `_hourly_backlog_chain()` içinde
#    `max(0.0, ...)` ile clamp'lenmiş, yani asla negatif olamaz.
# --------------------------------------------------------------------------

from datetime import timedelta

from app.queue.config import AirportConfigView
from app.queue.constants import PROCESS_PASSPORT_ARRIVAL, PROCESS_PASSPORT_DEPARTURE, PROCESS_SECURITY_INTL
from app.queue.engine import WindowPrediction, _hourly_backlog_chain


class _FakeSession:
    def __init__(self):
        self.added: list = []

    def add_all(self, rows):
        self.added.extend(rows)

    def execute(self, stmt, values=None):
        for row_values in (values or []):
            self.added.append(rebuild_orm_row(stmt, row_values))

    def flush(self):
        pass


def _make_config(**overrides) -> AirportConfigView:
    base = dict(
        airport_iata="XXX", passport_counter_count=8, passport_staff_count=8,
        passport_service_time_minutes=2.0, security_lane_count=10,
        domestic_security_lane_count=10, international_security_lane_count=10,
        security_service_time_minutes=1.0, passport_staff_per_counter=1.0,
        passport_service_rate_per_staff=30.0, passport_efficiency_multiplier=1.0,
        arrival_bank_threshold=1, is_default=True,
        passport_departure_server_count=10, passport_arrival_server_count=10,
        scale="large",
    )
    base.update(overrides)
    return AirportConfigView(**base)


def _make_prediction(process: str, window_start: datetime, expected_passengers: int) -> WindowPrediction:
    return WindowPrediction(
        airport_iata="XXX", process=process, window_start=window_start,
        window_end=window_start + timedelta(hours=1),
        flight_count=1, expected_passengers=expected_passengers,
        baseline_ratio=None, utilization=None, estimated_wait_minutes=0.0,
        risk="low", confidence=1.0,
    )


def _run_audit(process: str, demand_by_hour: dict, capacity_per_hour: float, config=None, airport_iata: str = "XXX"):
    """Gerçek `_hourly_backlog_chain()` (production FIFO state) ile coupling
    kur, `record_hourly_calculation_audit`'i çalıştır, eklenen satırları döndür.

    `_hourly_backlog_chain` capacity_rate'i dakika-başına bekliyor
    (`service_capacity = capacity_rate * window_minutes`), bu yüzden
    burada saatlik kapasiteyi 60'a bölüyoruz.
    """
    config = config or _make_config(airport_iata=airport_iata)
    capacity_rate_per_minute = capacity_per_hour / 60.0
    backlog_start_by_hour = {process: _hourly_backlog_chain(demand_by_hour, capacity_rate_per_minute, window_minutes=60)}
    coupling = {"backlog_start_by_hour": backlog_start_by_hour}
    predictions = [
        _make_prediction(process, hour, int(demand_by_hour.get(hour, 0)))
        for hour in sorted(demand_by_hour)
    ]
    session = _FakeSession()
    audit.record_hourly_calculation_audit(
        session, run_id="test-run", airport_iata=airport_iata, config=config, tz=None,
        predictions=predictions, coupling=coupling, calculation_date=None,
    )
    return {row.window_start_utc: row for row in session.added}


H0 = datetime(2026, 3, 10, 6, 0)
H1 = H0 + timedelta(hours=1)
H2 = H0 + timedelta(hours=2)


def test_capacity_greater_than_demand_backlog_end_is_zero():
    """1. capacity > demand -> audit backlog_end = 0."""
    rows = _run_audit(PROCESS_SECURITY_DOMESTIC, {H0: 100.0}, capacity_per_hour=600.0)
    assert rows[H0].backlog_start == 0.0
    assert rows[H0].backlog_end == 0.0


def test_capacity_equal_to_demand_backlog_end_is_zero():
    """2. capacity == demand -> audit backlog_end = 0."""
    rows = _run_audit(PROCESS_SECURITY_DOMESTIC, {H0: 600.0}, capacity_per_hour=600.0)
    assert rows[H0].backlog_start == 0.0
    assert rows[H0].backlog_end == 0.0


def test_demand_greater_than_capacity_backlog_end_is_positive_and_matches_fifo():
    """3. demand > capacity -> pozitif backlog, GERÇEK production FIFO ile aynı."""
    rows = _run_audit(PROCESS_SECURITY_INTL, {H0: 900.0}, capacity_per_hour=600.0)
    assert rows[H0].backlog_start == 0.0
    assert rows[H0].backlog_end == pytest.approx(300.0)


def test_backlog_carries_forward_correctly_across_hours():
    """4. Önceki saatin backlog_end'i, sonraki saatin backlog_start'ı olur (zincir doğru)."""
    demand_by_hour = {H0: 900.0, H1: 900.0, H2: 100.0}
    rows = _run_audit(PROCESS_SECURITY_INTL, demand_by_hour, capacity_per_hour=600.0)
    assert rows[H0].backlog_start == 0.0
    assert rows[H0].backlog_end == pytest.approx(300.0)
    assert rows[H1].backlog_start == pytest.approx(300.0)
    assert rows[H1].backlog_end == pytest.approx(600.0)
    assert rows[H2].backlog_start == pytest.approx(600.0)
    # H2, verilen demand_by_hour'daki SON saat; zincir H2'den sonra da
    # backlog>0 olduğu sürece production'da devam eder (_MAX_BACKLOG_DRAIN_
    # EXTENSION_HOURS'a kadar) - audit da bu GERÇEK devam eden değeri okur,
    # asla 0'a "yuvarlamaz".
    assert rows[H2].backlog_end == pytest.approx(600.0 + 100.0 - 600.0)


def test_backlog_end_never_negative_including_final_edge_with_no_next_hour():
    """5. backlog_end hiçbir zaman negatif değildir - zincirin tam sonunda
    (backlog tükenip bir sonraki saat artık coupling dict'inde hiç
    yoksa) bile, 'icat edilmiş' bir negatif sayı DEĞİL, güvenli 0.0
    production-eşdeğeri end-state kullanılır."""
    rows = _run_audit(PROCESS_SECURITY_DOMESTIC, {H0: 100.0, H1: 50.0}, capacity_per_hour=600.0)
    for row in rows.values():
        assert row.backlog_end is not None
        assert row.backlog_end >= 0.0
    # H1, dict'teki son saat ve backlog burada tükeniyor (max(0, ..) clamp'i
    # ile) -> zincir H1+1 saat için hiç genişlemiyor -> coupling dict'inde
    # H2 anahtarı YOK -> final-edge fallback devreye girip 0.0 dönmeli.
    assert rows[H1].backlog_end == 0.0


@pytest.mark.parametrize("process", [
    PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL,
    PROCESS_PASSPORT_DEPARTURE, PROCESS_PASSPORT_ARRIVAL,
])
def test_audit_backlog_end_matches_real_fifo_backlog_start_for_all_processes(process):
    """6 + 7. Her 4 process için: audit backlog_end == bir sonraki saatin
    GERÇEK production backlog_start_by_hour değeri (formül tekrar
    HESAPLANMIYOR, doğrudan okunuyor)."""
    demand_by_hour = {H0: 900.0, H1: 100.0}
    rows = _run_audit(process, demand_by_hour, capacity_per_hour=600.0)
    real_backlog_start_by_hour = _hourly_backlog_chain(demand_by_hour, 600.0 / 60.0, window_minutes=60)
    assert rows[H0].backlog_end == pytest.approx(real_backlog_start_by_hour[H1])


@pytest.mark.parametrize("airport_iata", ["IST", "SAW", "ZRH"])
def test_fix_is_airport_agnostic_no_special_case(airport_iata):
    """8. IST/SAW/ZRH (ve dolaylı olarak her gelecekteki airport) için
    TAMAMEN AYNI davranış - airport_iata sadece pass-through, hiçbir
    airport'a özel dallanma yok."""
    demand_by_hour = {H0: 900.0}
    rows = _run_audit(PROCESS_SECURITY_INTL, demand_by_hour, capacity_per_hour=600.0, airport_iata=airport_iata)
    assert rows[H0].airport_iata == airport_iata
    assert rows[H0].backlog_end == pytest.approx(300.0)
