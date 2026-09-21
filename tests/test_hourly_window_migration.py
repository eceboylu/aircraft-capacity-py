"""
ADIM 6D-2 HOURLY MIGRATION - 15dk -> 60dk backend geçişinin doğrudan
kanıtları.

Diğer testlerde ZATEN dolaylı olarak doğrulanan noktalar burada
TEKRARLANMAZ:
  - A) floor/window sınırları: tests/test_e2e_ist.py, tests/test_engine.py
  - B) current wait effective_time<=now ayrımı: tests/test_passport_current_wait_fix.py
       (bu dosya `window_minutes` PİNLEMİYOR - artık production
       varsayılanı olan 60'ı GERÇEKTEN kullanıyor, ADIM 6D-2 A-E'nin
       matematiği 60dk'da da BİREBİR doğru)
  - C/D/E/L) service_capacity/Erlang-C/c=4/backlog recurrence:
       tests/test_passport_backlog_model.py, tests/test_core_math.py
  - K) security matematiği değişmedi: tests/test_passport_backlog_model.py
       (test_g_security_completely_unaffected_by_passport_backlog)

Burada SADECE bu dosyaların kapsamadığı, hourly migration'a ÖZGÜ yeni
noktalar test edilir: F (security tek gerçek 60dk hesabı), G (baseline
reset karışmayı önlüyor), H (yeni window identity), I (eski 15dk
satırları pruning ile temizleniyor), J (reset sırasında Flight/Airport/
aircraft_capacity korunuyor).
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import AircraftCapacity, Base
from app.queue.baseline import reset_baseline_pool
from app.queue.config import default_config
from app.queue.constants import (
    DEMAND_WINDOW_MINUTES,
    LOCATION_DOMESTIC,
    PROCESS_SECURITY,
)
from app.queue.domain.demand import DemandCalculator
from app.queue.engine import predict_airport, run_predictions
from app.queue.ingestion.refresh import refresh_flights
from app.queue.models import (
    Airport,
    BaselineObservation,
    Flight,
    HistoricalFlightCount,
    QueuePrediction,
)

from .factories import MockCapacityResolver, at, departure


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


# ========================================================================
# H) Yeni window identity - window_start :00'da, window_end = +60dk
# ========================================================================

def test_h_window_identity_is_hourly():
    assert DEMAND_WINDOW_MINUTES == 60

    # ADIM (Departure Show-Up Profile): iki uçuşun talebi artık show-up
    # ile BİRDEN FAZLA saate yayılıyor (DEĞİŞTİ) - ama "window identity
    # HER ZAMAN tam saat (window_start :00'da, window_end +60dk), ASLA
    # 15dk alt-bölünme YOK" contract'ı (bu testin ASIL iddiası)
    # DEĞİŞMEDİ: her saat için EN FAZLA 1 satır üretilir (hiçbir saat
    # tekrarlanmaz/bölünmez).
    flights = [
        departure(10, 5, location=LOCATION_DOMESTIC, key="D1", number="1"),
        departure(10, 45, location=LOCATION_DOMESTIC, key="D2", number="2"),
    ]
    predictions = predict_airport(
        "AAA", flights, default_config("AAA"), DemandCalculator(MockCapacityResolver()),
    )
    security = [p for p in predictions if p.process == PROCESS_SECURITY]
    assert len(security) >= 1
    hours = [w.window_start for w in security]
    assert len(hours) == len(set(hours))   # her saat EN FAZLA 1 satır - hiçbiri tekrarlanmadı/bölünmedi
    for window in security:
        assert window.window_start.minute == 0
        assert window.window_start.second == 0
        assert window.window_end - window.window_start == timedelta(minutes=60)


# ========================================================================
# F) Security TEK gerçek 60dk hesabıdır - 4x15dk post-hoc birleştirme
#    DEĞİLDİR (ADIM_6D2_SPEC.md §7/§F "YASAKLAR").
# ========================================================================

def test_f_security_is_a_single_real_hourly_computation_not_four_merged_windows():
    """
    Aynı dakikada 4 ayrı domestic departure - eskiden (15dk) 4 AYRI
    QueuePrediction satırı (her biri kendi flight_count/demand'i ile)
    üretirdi. Her saat için TEK satır üretmeli - `reporting.py`'nin
    post-hoc "worst/max" birleştirmesi GİBİ DEĞİL, motorun kendisi HER
    saati TEK bir 60dk penceresi olarak hesaplıyor (hiçbir saat 15dk alt-
    bölünmesi almıyor).

    ADIM (Departure Show-Up Profile): 4 flight'ın talebi artık show-up
    ile BİRDEN FAZLA saate yayılıyor (DEĞİŞTİ, flight_count/expected_
    passengers TEK satırda TOPLANMIYOR) - ama conservation (flight_count
    TOPLAMI == 4, expected_passengers TOPLAMI == ham talep) VE "her saat
    TEK satır" contract'ı (bu testin ASIL iddiası) DEĞİŞMEDİ.
    """
    flights = [
        departure(9, 5, location=LOCATION_DOMESTIC, key=f"D{i}", number=str(i), aircraft="A320")
        for i in range(4)
    ]
    demand = DemandCalculator(MockCapacityResolver(capacities={"A320": 180}))
    predictions = predict_airport("AAA", flights, default_config("AAA"), demand)
    security = [p for p in predictions if p.process == PROCESS_SECURITY]

    assert len(security) >= 1
    hours = [w.window_start for w in security]
    assert len(hours) == len(set(hours))   # her saat EN FAZLA 1 satır
    assert sum(w.expected_passengers for w in security) == sum(demand.passenger_demand(f) for f in flights)
    # flight_count HÂLÂ tek effective_time() noktasında (07:00) toplanıyor (DEĞİŞMEDİ).
    peak = next(w for w in security if w.flight_count == 4)
    assert peak.window_start.hour == 7


# ========================================================================
# G) Baseline reset - eski (15dk) ve yeni (60dk) veri AYNI havuzda
#    karışmıyor.
# ========================================================================

def test_g_reset_baseline_pool_clears_only_baseline_tables(session):
    # Eski (15dk döneminden kalmış gibi simüle edilen) baseline verisi.
    session.add(HistoricalFlightCount(
        airport_iata="IST", process="security", hour_of_day=8, day_of_week=1,
        average_flight_count=2.0, sample_size=4,
    ))
    session.add(BaselineObservation(
        airport_iata="IST", process="security", window_start=at(8, 0),
    ))
    session.add(BaselineObservation(
        airport_iata="IST", process="security", window_start=at(8, 15),
    ))
    # Reset'in DOKUNMAMASI gereken production verisi.
    session.add(Airport(
        iata_code="IST", icao_code="LTFM", airport_name="Istanbul Airport",
        city_code="IST", country_code="TR", timezone="Europe/Istanbul",
    ))
    session.add(AircraftCapacity(
        icao_code="A320", name="A320", category="narrow", capacity=180,
        source="verified_dataset", confidence="high",
    ))
    session.commit()
    refresh_flights(session, [_row("AAA_1", at(8, 0))])

    before_flight = session.scalar(select(func.count()).select_from(Flight))
    before_airport = session.scalar(select(func.count()).select_from(Airport))
    before_capacity = session.scalar(select(func.count()).select_from(AircraftCapacity))
    assert before_flight == 1 and before_airport == 1 and before_capacity == 1

    result = reset_baseline_pool(session)

    assert result == {
        "historical_flight_counts_deleted": 1,
        "baseline_observations_deleted": 2,
    }
    assert session.scalar(select(func.count()).select_from(HistoricalFlightCount)) == 0
    assert session.scalar(select(func.count()).select_from(BaselineObservation)) == 0

    # J) Flight/Airport/aircraft_capacity KORUNDU.
    assert session.scalar(select(func.count()).select_from(Flight)) == before_flight
    assert session.scalar(select(func.count()).select_from(Airport)) == before_airport
    assert session.scalar(select(func.count()).select_from(AircraftCapacity)) == before_capacity


def test_g_reset_baseline_pool_is_idempotent(session):
    result_first = reset_baseline_pool(session)   # tablolar zaten boş
    assert result_first == {
        "historical_flight_counts_deleted": 0,
        "baseline_observations_deleted": 0,
    }
    result_second = reset_baseline_pool(session)
    assert result_second == result_first   # tekrar çağrı hata vermez, aynı sonucu verir


def test_g_pipeline_never_calls_reset_baseline_pool():
    """
    Reset SADECE elle (`python -m app.queue.baseline --reset-pool`)
    tetiklenmeli - normal pipeline akışından (ör. `app/queue/pipeline.py`)
    HİÇ çağrılmamalı, yoksa her refresh'te YANLIŞLIKLA baseline silinir.
    """
    import inspect

    import app.queue.pipeline as pipeline_module
    source = inspect.getsource(pipeline_module)
    assert "reset_baseline_pool" not in source


# ========================================================================
# I) Eski 15dk QueuePrediction satırları yeni saatlik serisine
#    karışmıyor - mevcut prune_stale_predictions() bunu hallediyor.
# ========================================================================

def test_i_old_quarter_hour_rows_are_pruned_by_next_run(session):
    # 15dk döneminden kalmış gibi simüle edilen ESKİ satırlar.
    for minute in (0, 15, 30, 45):
        session.add(QueuePrediction(
            airport_iata="AAA", process=PROCESS_SECURITY,
            window_start=at(8, minute),
            window_end=at(8, minute) + timedelta(minutes=15),
            flight_count=1, expected_passengers=100, risk="LOW",
        ))
    session.commit()
    assert session.scalar(select(func.count()).select_from(QueuePrediction)) == 4

    # ADIM (Airport Queue Model V2 - sabit -120dk offset): scheduled=10:05
    # -> effective=08:05 -> pencere 08:00.
    refresh_flights(session, [_row("AAA_1", at(10, 5))])
    resolver = MockCapacityResolver()
    now = at(10, 0)   # pencere kesinlikle kapanmış
    run_predictions(session, resolver, airports=["AAA"], now=now)

    rows = session.execute(
        select(QueuePrediction).where(QueuePrediction.airport_iata == "AAA")
    ).scalars().all()
    # SADECE yeni saatlik pencere (08:00) kalmalı - eski :15/:30/:45
    # satırları prune_stale_predictions() tarafından silinmiş olmalı.
    starts = {(r.process, r.window_start) for r in rows}
    assert (PROCESS_SECURITY, at(8, 0)) in starts
    assert (PROCESS_SECURITY, at(8, 15)) not in starts
    assert (PROCESS_SECURITY, at(8, 30)) not in starts
    assert (PROCESS_SECURITY, at(8, 45)) not in starts


def _row(flight_key: str, dep_scheduled: datetime) -> dict:
    return {
        "flight_key": flight_key, "airport_iata": "AAA",
        "direction": "departure", "location": "domestic",
        "airline_iata": "TK", "flight_number": "1", "flight_iata": "TK1",
        "aircraft_icao": "A320", "aircraft_match_found": True,
        "dep_iata": "AAA", "arr_iata": "ZZZ",
        "dep_scheduled_utc": dep_scheduled, "dep_estimated_utc": None,
        "dep_actual_utc": None,
        "arr_scheduled_utc": dep_scheduled + timedelta(minutes=60),
        "arr_estimated_utc": None, "arr_actual_utc": None,
        "dep_terminal": None, "dep_gate": None,
        "arr_terminal": None, "arr_gate": None, "status": "scheduled",
    }
