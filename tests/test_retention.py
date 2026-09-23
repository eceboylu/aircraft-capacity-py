"""
ADIM (Operational Retention) - `genel-proje.md` Bölüm 6-21 testleri.

Tüm DESTRUCTIVE (gerçek DELETE içeren) testler İZOLE, bellek içi
`sqlite://` session kullanır - `tests/incoming_2026_09_19` paylaşılan
DB'sine HİÇ dokunulmaz (bkz. `test_shared_db_is_dry_run_only_no_delete`
- o test paylaşılan DB'yi AÇAR ama SADECE dry-run/inventory yapar,
gerçek `database.sqlite`/`data/*` HİÇ açılmaz).
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue import retention
from app.queue.constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    RISK_LOW,
)
from app.queue.domain.retention_time import (
    USAGE_HORIZON_HOURS,
    canonical_flight_time,
    canonical_operational_time,
)
from app.queue.engine import flights_of_airport
from app.queue.ingestion.sources import parse_source_a
from app.queue.models import BaselineObservation, Flight, FlightEvent, QueuePrediction

NOW = datetime(2026, 9, 21, 12, 0, 0)


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


def make_flight(
    key, airport="AAA", direction=DIRECTION_DEPARTURE, age: timedelta | None = None,
    scheduled: datetime | None = None,
):
    """
    `age` verilirse `scheduled = NOW - age` olur (departure ->
    dep_scheduled_utc, arrival -> arr_scheduled_utc - flight_key ile
    AYNI kural). `scheduled` doğrudan verilirse `age` yok sayılır
    (gelecek flight'lar için).
    """
    when = scheduled if scheduled is not None else (NOW - age if age is not None else None)
    flight = Flight(
        flight_key=key,
        airport_iata=airport,
        direction=direction,
        location="domestic",
        status="scheduled",
        last_refreshed_at=NOW,
    )
    if direction == DIRECTION_DEPARTURE:
        flight.dep_scheduled_utc = when
    else:
        flight.arr_scheduled_utc = when
    return flight


def make_event(key, age: timedelta, airport="AAA", event_type="DELAYED"):
    return FlightEvent(
        flight_key=key,
        airport_iata=airport,
        event_type=event_type,
        detected_at=NOW - age,
    )


def make_prediction(airport, process, age: timedelta, risk=RISK_LOW):
    window_start = NOW - age
    return QueuePrediction(
        airport_iata=airport,
        process=process,
        window_start=window_start,
        window_end=window_start + timedelta(hours=1),
        flight_count=1,
        expected_passengers=100,
        risk=risk,
        calculated_at=NOW,
    )


# ========================================================================
# Bölüm 13 - canonical timestamp seçimi (flight_key ile AYNI kural)
# ========================================================================

def test_canonical_operational_time_uses_scheduled_not_actual_estimated():
    dep_sched = datetime(2026, 9, 18, 8, 0)
    assert canonical_operational_time(DIRECTION_DEPARTURE, dep_sched, None) == dep_sched
    arr_sched = datetime(2026, 9, 18, 9, 0)
    assert canonical_operational_time(DIRECTION_ARRIVAL, None, arr_sched) == arr_sched


def test_canonical_operational_time_none_when_scheduled_unknown():
    assert canonical_operational_time(DIRECTION_DEPARTURE, None, None) is None


def test_canonical_flight_time_matches_build_flight_key_selection():
    from app.queue.ingestion.sources import build_flight_key

    dep_sched = datetime(2026, 9, 18, 8, 0)
    flight = make_flight("K1", direction=DIRECTION_DEPARTURE, scheduled=dep_sched)
    key = build_flight_key("TS", "9001", dep_sched, "AAA", DIRECTION_DEPARTURE)
    # flight_key'in tarih kısmı ile canonical_flight_time'ın tarihi AYNI kaynaktan.
    assert dep_sched.date().isoformat() in key
    assert canonical_flight_time(flight) == dep_sched


# ========================================================================
# Bölüm 20 - RETENTION ISOLATED TEST MATRIX (izole DB, gerçek DELETE)
# ========================================================================

AGE_CASES = [
    ("10d", timedelta(days=10), "removed"),
    ("5d", timedelta(days=5), "removed"),
    ("3d", timedelta(days=3), "removed"),
    ("49h", timedelta(hours=49), "removed"),
    ("48h_exact", timedelta(hours=48), "preserved"),
    ("47h", timedelta(hours=47), "preserved"),
    ("yesterday", timedelta(hours=30), "preserved"),
    ("today", timedelta(hours=2), "preserved"),
    ("future", timedelta(hours=-24), "preserved"),
]


@pytest.mark.parametrize("label,age,expected", AGE_CASES)
def test_flight_retention_matrix(session, label, age, expected):
    key = f"FLIGHT_{label}"
    session.add(make_flight(key, age=age))
    session.commit()

    report = retention.cleanup_expired_operational_data(session, now=NOW, dry_run=False)

    remaining = session.execute(
        select(Flight).where(Flight.flight_key == key)
    ).scalar_one_or_none()
    if expected == "removed":
        assert remaining is None, f"{label}: silinmesi bekleniyordu"
        assert report.flight_deleted >= 1
    else:
        assert remaining is not None, f"{label}: korunması bekleniyordu"


@pytest.mark.parametrize("label,age,expected", AGE_CASES)
def test_flight_event_retention_matrix(session, label, age, expected):
    key = f"EVT_{label}"
    session.add(make_event(key, age=age))
    session.commit()

    retention.cleanup_expired_operational_data(session, now=NOW, dry_run=False)

    remaining = session.execute(
        select(FlightEvent).where(FlightEvent.flight_key == key)
    ).scalar_one_or_none()
    if expected == "removed":
        assert remaining is None, f"{label}: silinmesi bekleniyordu"
    else:
        assert remaining is not None, f"{label}: korunması bekleniyordu"


@pytest.mark.parametrize("label,age,expected", AGE_CASES)
def test_queue_prediction_retention_matrix_when_historical_committed(session, label, age, expected):
    """
    Bölüm 12 - QueuePrediction SADECE `BaselineObservation`'a committed
    ise silinme adayı olur; bu test committed senaryoyu kapsar (silinme
    beklenen satırlar için BaselineObservation ÖNCEDEN eklendi).
    """
    airport, process = "AAA", "passport_dep"
    window_start = NOW - age
    session.add(make_prediction(airport, process, age))
    session.add(BaselineObservation(
        airport_iata=airport, process=process, window_start=window_start,
    ))
    session.commit()

    retention.cleanup_expired_operational_data(session, now=NOW, dry_run=False)

    remaining = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == airport,
            QueuePrediction.window_start == window_start,
        )
    ).scalar_one_or_none()
    if expected == "removed":
        assert remaining is None, f"{label}: silinmesi bekleniyordu"
    else:
        assert remaining is not None, f"{label}: korunması bekleniyordu"


BASELINE_OBSERVATION_AGE_CASES = [
    ("40d", timedelta(days=40), "removed"),
    ("31d", timedelta(days=31), "removed"),
    ("30d_exact", timedelta(days=30), "preserved"),
    ("29d", timedelta(days=29), "preserved"),
    ("2d", timedelta(days=2), "preserved"),
    ("future", timedelta(days=-1), "preserved"),
]


@pytest.mark.parametrize("label,age,expected", BASELINE_OBSERVATION_AGE_CASES)
def test_baseline_observation_retention_matrix(session, label, age, expected):
    """
    ADIM (Health/Retention Audit) - önceki ADIM'da BaselineObservation
    retention'ı eklendi (BASELINE_OBSERVATION_RETENTION_DAYS=30) ama HİÇ
    test edilmemişti (bu ADIM'da tespit edildi) - bu, Flight/FlightEvent
    matrix'iyle AYNI desende, sadece 30 günlük sınırla.
    """
    airport, process = "AAA", "passport_dep"
    window_start = NOW - age
    session.add(BaselineObservation(
        airport_iata=airport, process=process, window_start=window_start,
    ))
    session.commit()

    report = retention.cleanup_expired_operational_data(session, now=NOW, dry_run=False)

    remaining = session.execute(
        select(BaselineObservation).where(
            BaselineObservation.airport_iata == airport,
            BaselineObservation.window_start == window_start,
        )
    ).scalar_one_or_none()
    if expected == "removed":
        assert remaining is None, f"{label}: silinmesi bekleniyordu"
        assert report.baseline_observation_deleted >= 1
    else:
        assert remaining is not None, f"{label}: korunması bekleniyordu"


def test_baseline_observation_retention_does_not_touch_historical_flight_count(session):
    """BaselineObservation silinse bile HistoricalFlightCount'taki running-average ETKİLENMEZ."""
    from app.queue.models import HistoricalFlightCount

    airport, process = "AAA", "passport_dep"
    old_window = NOW - timedelta(days=40)
    session.add(BaselineObservation(airport_iata=airport, process=process, window_start=old_window))
    session.add(HistoricalFlightCount(
        airport_iata=airport, process=process, hour_of_day=8, day_of_week=1,
        average_flight_count=5.0, sample_size=3,
    ))
    session.commit()

    retention.cleanup_expired_operational_data(session, now=NOW, dry_run=False)

    remaining_baseline = session.execute(
        select(BaselineObservation).where(BaselineObservation.window_start == old_window)
    ).scalar_one_or_none()
    assert remaining_baseline is None

    hist = session.execute(
        select(HistoricalFlightCount).where(
            HistoricalFlightCount.airport_iata == airport,
            HistoricalFlightCount.process == process,
        )
    ).scalar_one_or_none()
    assert hist is not None
    assert hist.sample_size == 3, "BaselineObservation cleanup HistoricalFlightCount'a HİÇ dokunmamalı"


# ========================================================================
# Cleanup re-run idempotency (tüm tablolar tek seferde)
# ========================================================================

def test_cleanup_rerun_is_idempotent_across_all_tables(session):
    """İkinci çalıştırma EK bir silme yapmamalı - tüm candidate'lar zaten ilk turda silindi."""
    airport, process = "AAA", "passport_dep"
    old_window = NOW - timedelta(days=10)
    session.add(make_flight("RERUN_FLIGHT", age=timedelta(days=10)))
    session.add(make_event("RERUN_EVT", age=timedelta(days=10)))
    session.add(make_prediction(airport, process, timedelta(days=10)))
    session.add(BaselineObservation(airport_iata=airport, process=process, window_start=old_window))
    session.add(BaselineObservation(airport_iata=airport, process=process, window_start=NOW - timedelta(days=40)))
    session.commit()

    first = retention.cleanup_expired_operational_data(session, now=NOW, dry_run=False)
    assert first.flight_deleted >= 1
    assert first.flight_event_deleted >= 1
    assert first.baseline_observation_deleted >= 1

    second = retention.cleanup_expired_operational_data(session, now=NOW, dry_run=False)
    assert second.flight_candidates == 0
    assert second.flight_deleted == 0
    assert second.flight_event_candidates == 0
    assert second.flight_event_deleted == 0
    assert second.baseline_observation_candidates == 0
    assert second.baseline_observation_deleted == 0
    assert second.errors == []


def test_persistent_tables_untouched_by_cleanup(session):
    """Bölüm 11 - HistoricalFlightCount/BaselineObservation/Airport/AirportOperationalConfig/Aircraft reference SİLİNMEZ."""
    from app.models import AircraftCapacity
    from app.queue.models import Airport, AirportOperationalConfig, HistoricalFlightCount

    session.add(make_flight("OLD_FLIGHT", age=timedelta(days=10)))
    session.add(HistoricalFlightCount(
        airport_iata="AAA", process="passport_dep", hour_of_day=8, day_of_week=1,
        average_flight_count=5.0, sample_size=3,
    ))
    session.add(Airport(iata_code="AAA", timezone="UTC"))
    session.add(AirportOperationalConfig(airport_iata="AAA"))
    session.add(AircraftCapacity(icao_code="A320", capacity=180))
    session.commit()

    before_hist = session.scalar(select(func.count()).select_from(HistoricalFlightCount))
    before_airport = session.scalar(select(func.count()).select_from(Airport))
    before_config = session.scalar(select(func.count()).select_from(AirportOperationalConfig))
    before_aircraft = session.scalar(select(func.count()).select_from(AircraftCapacity))

    retention.cleanup_expired_operational_data(session, now=NOW, dry_run=False)

    assert session.scalar(select(func.count()).select_from(HistoricalFlightCount)) == before_hist
    assert session.scalar(select(func.count()).select_from(Airport)) == before_airport
    assert session.scalar(select(func.count()).select_from(AirportOperationalConfig)) == before_config
    assert session.scalar(select(func.count()).select_from(AircraftCapacity)) == before_aircraft
    # Eski flight GERÇEKTEN silindi - persistent tablo korumasının
    # "hiçbir şey silinmiyor" gibi yanlış bir sonuçla karışmadığını kanıtlar.
    assert session.execute(select(Flight).where(Flight.flight_key == "OLD_FLIGHT")).scalar_one_or_none() is None


# ========================================================================
# Bölüm 12 - HISTORICALFLIGHTCOUNT EXACTLY-ONCE / NO DOUBLE AGGREGATION
# ========================================================================

def test_uncommitted_queue_prediction_is_skipped_not_deleted(session):
    """BaselineObservation YOKSA (henüz committed değil) satır SİLİNMEZ, SKIP edilir."""
    airport, process = "AAA", "passport_dep"
    age = timedelta(days=5)
    window_start = NOW - age
    session.add(make_prediction(airport, process, age))
    session.commit()
    # KASITLI: BaselineObservation eklenmedi - "henüz committed değil" senaryosu.

    report = retention.cleanup_expired_operational_data(session, now=NOW, dry_run=False)

    remaining = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == airport,
            QueuePrediction.window_start == window_start,
        )
    ).scalar_one_or_none()
    assert remaining is not None, "committed olmayan pencere SİLİNMEMELİYDİ"
    assert report.queue_prediction_skipped_uncommitted >= 1
    assert report.queue_prediction_deleted == 0


def test_cleanup_never_writes_to_historical_flight_count_or_baseline_observation(session):
    """Bölüm 12 - retention cleanup HistoricalFlightCount/BaselineObservation'a HİÇ YAZMAZ (sadece okur)."""
    from app.queue.models import HistoricalFlightCount

    airport, process = "AAA", "passport_dep"
    age = timedelta(days=5)
    window_start = NOW - age
    session.add(make_prediction(airport, process, age))
    session.add(BaselineObservation(airport_iata=airport, process=process, window_start=window_start))
    session.commit()

    before_hist = session.scalar(select(func.count()).select_from(HistoricalFlightCount))
    before_obs = session.scalar(select(func.count()).select_from(BaselineObservation))

    retention.cleanup_expired_operational_data(session, now=NOW, dry_run=False)

    assert session.scalar(select(func.count()).select_from(HistoricalFlightCount)) == before_hist
    assert session.scalar(select(func.count()).select_from(BaselineObservation)) == before_obs


# ========================================================================
# Bölüm 11/16 - DRY RUN (candidate hesapla, DELETE yapma)
# ========================================================================

def test_dry_run_computes_candidates_but_deletes_nothing(session):
    session.add(make_flight("DRY_OLD", age=timedelta(days=10)))
    session.add(make_event("DRY_EVT", age=timedelta(days=10)))
    session.commit()

    report = retention.cleanup_expired_operational_data(session, now=NOW, dry_run=True)

    assert report.flight_candidates >= 1
    assert report.flight_deleted == 0
    assert report.flight_event_candidates >= 1
    assert report.flight_event_deleted == 0
    assert session.execute(select(Flight).where(Flight.flight_key == "DRY_OLD")).scalar_one_or_none() is not None
    assert session.execute(select(FlightEvent).where(FlightEvent.flight_key == "DRY_EVT")).scalar_one_or_none() is not None


def test_retention_dry_run_is_the_safe_default_module_config():
    """Bölüm 10 - unsafe destructive production default YOK: RETENTION_DRY_RUN varsayılan True, RETENTION_ENABLED varsayılan False."""
    assert retention.RETENTION_DRY_RUN is True
    assert retention.RETENTION_ENABLED is False


def test_run_cleanup_cycle_is_noop_when_retention_disabled(session, monkeypatch):
    monkeypatch.setattr(retention, "RETENTION_ENABLED", False)
    result = retention.run_cleanup_cycle()
    assert result == {"enabled": False}


# ========================================================================
# Bölüm 16 - Shared DB: SADECE inventory/dry-run, gerçek DELETE YOK
# ========================================================================

def test_shared_db_is_dry_run_only_no_delete(tmp_path):
    """
    Paylaşılan `tests/incoming_2026_09_19` DB'si SADECE dry-run/candidate
    calculation için kullanılır - satır sayıları öncesi/sonrası BİREBİR
    AYNI kalmalı (gerçek DELETE=0).

    Gerçek dosyayı DOĞRUDAN açmıyoruz: SQLite bir bağlantı açıp SELECT
    çalıştırdığında bile (journal/change-counter mekanikleri yüzünden)
    dosyanın BAYTLARI değişebilir (gerçekte satır silinmese bile) - bu
    testin AMACI "hiç DELETE yok" (satır sayısı), "dosya baytı hiç
    değişmiyor" DEĞİL. Bu yüzden `tmp_path`'e KOPYALANMIŞ bir nüsha
    üzerinde çalışılır - tracked/paylaşılan dosyaya asla bağlantı
    açılmaz, git working tree'de HİÇBİR iz bırakmaz.
    """
    import shutil
    from pathlib import Path

    shared_db_path = Path(__file__).parent / "incoming_2026_09_19" / "incoming_2026_09_19.sqlite"
    if not shared_db_path.exists():
        pytest.skip("paylaşılan DB bu ortamda yok")

    copy_path = tmp_path / "incoming_2026_09_19_readonly_copy.sqlite"
    shutil.copy(shared_db_path, copy_path)

    engine = create_engine(f"sqlite:///{copy_path}")
    maker = sessionmaker(bind=engine)
    session = maker()
    try:
        before_flight = session.scalar(select(func.count()).select_from(Flight))
        before_event = session.scalar(select(func.count()).select_from(FlightEvent))
        before_prediction = session.scalar(select(func.count()).select_from(QueuePrediction))

        report = retention.cleanup_expired_operational_data(
            session, now=datetime(2026, 9, 25, 12, 0), dry_run=True,
        )

        after_flight = session.scalar(select(func.count()).select_from(Flight))
        after_event = session.scalar(select(func.count()).select_from(FlightEvent))
        after_prediction = session.scalar(select(func.count()).select_from(QueuePrediction))

        assert before_flight == after_flight
        assert before_event == after_event
        assert before_prediction == after_prediction
        assert report.dry_run is True
        assert report.flight_deleted == 0
        assert report.flight_event_deleted == 0
        assert report.queue_prediction_deleted == 0
    finally:
        session.close()


# ========================================================================
# Bölüm 15/17 - RE-INGEST LOOP PREVENTION
# ========================================================================

def test_expired_flight_not_reinserted_after_cleanup_when_source_still_returns_it(session):
    """
    Bölüm 15/17 - retention Flight'ı sildikten SONRA upstream aynı eski
    flight'ı tekrar döndürürse, ingestion-horizon filtresi (Bölüm 15)
    onu tekrar üretmemeli - `parse_source_a()`'ya `min_operational_time`
    verilerek AYNI korumanın gerçek ingestion katmanında da çalıştığı
    doğrulanır.
    """
    old_time = NOW - timedelta(days=10)
    session.add(make_flight("OLD_REINGEST", age=timedelta(days=10)))
    session.commit()

    retention.cleanup_expired_operational_data(session, now=NOW, dry_run=False)
    assert session.execute(
        select(Flight).where(Flight.flight_key == "OLD_REINGEST")
    ).scalar_one_or_none() is None

    # Upstream'in HÂLÂ aynı (artık DB'de olmayan) eski flight'ı döndürdüğü senaryo.
    stale_record = {
        "dep_iata": "AAA", "arr_iata": "BBB",
        "airline_iata": "TS", "flight_number": "9999",
        "dep_time_utc": old_time.strftime("%Y-%m-%d %H:%M"),
        "status": "scheduled",
    }
    min_operational_time = NOW - timedelta(hours=USAGE_HORIZON_HOURS)
    rows = parse_source_a(
        [stale_record], DIRECTION_DEPARTURE, country_by_iata={},
        min_operational_time=min_operational_time,
    )
    assert rows == [], "48 saatten eski flight ingestion tarafından REDDEDİLMELİYDİ"


def test_exactly_48h_flight_still_accepted_by_ingestion_filter():
    """Bölüm 7/15 sınır: timestamp == cutoff -> KABUL edilir (sıkı `<`, `<=` DEĞİL)."""
    min_operational_time = NOW - timedelta(hours=USAGE_HORIZON_HOURS)
    exact_time = min_operational_time  # tam sınırda
    record = {
        "dep_iata": "AAA", "arr_iata": "BBB",
        "airline_iata": "TS", "flight_number": "1000",
        "dep_time_utc": exact_time.strftime("%Y-%m-%d %H:%M"),
        "status": "scheduled",
    }
    rows = parse_source_a(
        [record], DIRECTION_DEPARTURE, country_by_iata={},
        min_operational_time=min_operational_time,
    )
    assert len(rows) == 1


def test_future_flight_accepted_by_ingestion_filter():
    min_operational_time = NOW - timedelta(hours=USAGE_HORIZON_HOURS)
    future_time = NOW + timedelta(days=30)
    record = {
        "dep_iata": "AAA", "arr_iata": "BBB",
        "airline_iata": "TS", "flight_number": "1001",
        "dep_time_utc": future_time.strftime("%Y-%m-%d %H:%M"),
        "status": "scheduled",
    }
    rows = parse_source_a(
        [record], DIRECTION_DEPARTURE, country_by_iata={},
        min_operational_time=min_operational_time,
    )
    assert len(rows) == 1


def test_ingestion_filter_inactive_by_default_backward_compatible():
    """`min_operational_time=None` (varsayılan) -> hiçbir filtre uygulanmaz, eski davranış."""
    ancient_record = {
        "dep_iata": "AAA", "arr_iata": "BBB",
        "airline_iata": "TS", "flight_number": "1002",
        "dep_time_utc": "2020-01-01 00:00",
        "status": "scheduled",
    }
    rows = parse_source_a([ancient_record], DIRECTION_DEPARTURE, country_by_iata={})
    assert len(rows) == 1


# ========================================================================
# Bölüm 8 - 48H USAGE HORIZON (flights_of_airport)
# ========================================================================

def test_flights_of_airport_without_now_returns_everything_backward_compatible(session):
    session.add(make_flight("ANY_AGE", age=timedelta(days=365)))
    session.commit()
    flights = flights_of_airport(session, "AAA")
    assert len(flights) == 1


def test_flights_of_airport_with_now_excludes_older_than_48h(session):
    session.add(make_flight("OLD_49H", age=timedelta(hours=49)))
    session.add(make_flight("RECENT_2H", age=timedelta(hours=2)))
    session.add(make_flight("FUTURE", age=timedelta(hours=-24)))
    session.commit()

    flights = flights_of_airport(session, "AAA", now=NOW)
    keys = {f.flight_key for f in flights}
    assert "OLD_49H" not in keys
    assert "RECENT_2H" in keys
    assert "FUTURE" in keys


def test_flights_of_airport_keeps_rows_with_unknown_scheduled_time(session):
    """Canonical zaman None ise (scheduled bilinmiyor) satır KORUNUR - bilinmeyen bir zaman 'eski' sayılmaz."""
    flight = Flight(
        flight_key="UNKNOWN_TIME", airport_iata="AAA", direction=DIRECTION_DEPARTURE,
        location="domestic", status="scheduled", last_refreshed_at=NOW,
    )
    session.add(flight)
    session.commit()

    flights = flights_of_airport(session, "AAA", now=NOW)
    assert any(f.flight_key == "UNKNOWN_TIME" for f in flights)


# ========================================================================
# Bölüm 18 - BATCH DELETE
# ========================================================================

def test_batch_delete_handles_more_rows_than_batch_size(session, monkeypatch):
    monkeypatch.setattr(retention, "BATCH_SIZE", 3)
    for i in range(10):
        session.add(make_flight(f"BATCH_{i}", age=timedelta(days=10)))
    session.commit()

    report = retention.cleanup_expired_operational_data(session, now=NOW, dry_run=False)

    assert report.flight_deleted == 10
    remaining = session.scalar(select(func.count()).select_from(Flight))
    assert remaining == 0


# ========================================================================
# Bölüm 19 - SQLITE MAINTENANCE (salt okunur envanter)
# ========================================================================

def test_inspect_sqlite_maintenance_settings_is_read_only(session):
    before = session.scalar(select(func.count()).select_from(Flight))
    settings = retention.inspect_sqlite_maintenance_settings(session)
    assert "auto_vacuum" in settings
    assert "journal_mode" in settings
    assert "freelist_count" in settings
    after = session.scalar(select(func.count()).select_from(Flight))
    assert before == after
