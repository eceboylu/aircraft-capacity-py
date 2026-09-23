"""
ADIM (MySQL Performance Regression Fix - Phase 1) - `refresh_flights()`
bulk/chunk/no-op-skip davranışını VE `HistoricalFlightCount` baseline
cache'inin GERÇEKTEN `get_baseline()`/`get_passenger_baseline()` ile
AYNI sonucu ürettiğini doğrular.

Queue matematiği/domain fonksiyonları (`_detect_changes`,
`_aircraft_change_already_recorded`, `get_baseline`, `get_passenger_
baseline`) BURADA HİÇ YENİDEN YAZILMAZ/TEKRARLANMAZ - GERÇEK,
DEĞİŞTİRİLMEMİŞ fonksiyonlar çağrılır, SADECE etraflarındaki SQL
istatistiklerine (SELECT/UPDATE/COMMIT sayısı) bakılır.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.baseline import get_baseline, get_passenger_baseline
from app.queue.constants import DIRECTION_DEPARTURE, EVENT_AIRCRAFT_CHANGED
from app.queue.engine import _db_baseline_fn, _db_passenger_baseline_fn
from app.queue.ingestion.refresh import REFRESH_CHUNK_SIZE, refresh_flights
from app.queue.models import Flight, FlightEvent, HistoricalFlightCount


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


class SqlCounter:
    def __init__(self, engine):
        self.select = self.insert = self.update = self.delete = 0
        self.commit = 0

        @event.listens_for(engine, "before_cursor_execute")
        def _before(conn, cursor, statement, parameters, context, executemany):
            head = statement.strip().split(None, 1)[0].upper() if statement.strip() else ""
            if head == "SELECT":
                self.select += 1
            elif head == "INSERT":
                self.insert += 1
            elif head == "UPDATE":
                self.update += 1
            elif head == "DELETE":
                self.delete += 1

        @event.listens_for(engine, "commit")
        def _commit(conn):
            self.commit += 1


def _make_row(n: int, dep_iata="IST", arr_iata="CDG", **overrides) -> dict:
    base_time = datetime(2026, 9, 15, 10, 0) + timedelta(minutes=n)
    row = {
        "flight_key": f"TK_{n}_2026-09-15_{dep_iata}_departure",
        "airport_iata": dep_iata,
        "direction": DIRECTION_DEPARTURE,
        "location": "international",
        "airline_iata": "TK",
        "flight_number": str(n),
        "flight_iata": f"TK{n}",
        "aircraft_icao": "A320",
        "aircraft_match_found": True,
        "dep_iata": dep_iata,
        "arr_iata": arr_iata,
        "dep_scheduled_utc": base_time,
        "dep_estimated_utc": None,
        "dep_actual_utc": None,
        "arr_scheduled_utc": base_time + timedelta(hours=3),
        "arr_estimated_utc": None,
        "arr_actual_utc": None,
        "dep_terminal": "1",
        "dep_gate": "B1",
        "arr_terminal": "1",
        "arr_gate": "E1",
        "status": "scheduled",
    }
    row.update(overrides)
    return row


# ========================================================================
# Bölüm 19A - 1000 yeni flight -> SELECT-per-flight pattern YOK
# ========================================================================

def test_a_bulk_insert_does_not_select_per_flight(session):
    engine = session.get_bind()
    rows = [_make_row(i) for i in range(1000)]

    counter = SqlCounter(engine)
    result = refresh_flights(session, rows)

    assert result["inserted"] == 1000
    assert result["failed"] == 0
    # 1000 flight, REFRESH_CHUNK_SIZE'lık chunk'larla en az 2 bulk SELECT
    # (chunk sayısı kadar) - kesinlikle 1000'e YAKIN DEĞİL.
    expected_chunks = (1000 + REFRESH_CHUNK_SIZE - 1) // REFRESH_CHUNK_SIZE
    assert counter.select <= expected_chunks + 2, (
        f"SELECT sayısı ({counter.select}) satır-başına-SELECT deseni gösteriyor "
        f"(chunk sayısı ~{expected_chunks} olmalıydı)"
    )
    assert counter.select < 50, "1000 flight için SELECT sayısı flight sayısıyla LİNEER büyümemeli"


def test_a_commit_count_bounded_by_chunks_not_flights(session):
    engine = session.get_bind()
    rows = [_make_row(i) for i in range(1000)]

    counter = SqlCounter(engine)
    refresh_flights(session, rows)

    expected_chunks = (1000 + REFRESH_CHUNK_SIZE - 1) // REFRESH_CHUNK_SIZE
    assert counter.commit <= expected_chunks + 1, (
        f"COMMIT sayısı ({counter.commit}) chunk sayısına ({expected_chunks}) YAKIN olmalı, 1000'e DEĞİL"
    )


# ========================================================================
# Bölüm 19B - AYNI 1000 tekrar -> 0 yeni Flight, 0 FlightEvent, 0 UPDATE
# ========================================================================

def test_b_identical_repeat_zero_new_rows_zero_events_zero_updates(session):
    engine = session.get_bind()
    rows = [_make_row(i) for i in range(1000)]
    refresh_flights(session, rows)

    before_flights = session.query(Flight).count()
    before_events = session.query(FlightEvent).count()

    counter = SqlCounter(engine)
    result = refresh_flights(session, [dict(r) for r in rows])  # bağımsız kopyalar - AYNI içerik

    assert result["inserted"] == 0
    assert result["updated"] == 1000, "raporlama sözleşmesi DEĞİŞMEDİ - her mevcut satır YİNE DE 'updated' sayılır"
    assert result["events_written"] == 0
    assert session.query(Flight).count() == before_flights
    assert session.query(FlightEvent).count() == before_events
    assert counter.update == 0, (
        f"içerik BİREBİR AYNIYSA Flight tablosuna HİÇBİR SQL UPDATE gitmemeli - görülen: {counter.update}"
    )


# ========================================================================
# Bölüm 19C - %10 değişiklik -> SADECE değişen satırlar UPDATE alır
# ========================================================================

def test_c_ten_percent_changed_only_changed_rows_get_sql_update(session):
    engine = session.get_bind()
    rows = [_make_row(i) for i in range(1000)]
    refresh_flights(session, rows)

    changed_rows = [dict(r) for r in rows]
    changed_indices = list(range(0, 1000, 10))  # tam %10
    for i in changed_indices:
        changed_rows[i]["aircraft_icao"] = "A321"

    counter = SqlCounter(engine)
    result = refresh_flights(session, changed_rows)

    assert result["inserted"] == 0
    assert result["updated"] == 1000
    # Gerçek SQL UPDATE sayısı ~100 (değişenler) olmalı, ~1000 (hepsi) DEĞİL.
    # ORM'un flush stratejisine göre tam sayı satır-başına 1 UPDATE'ten
    # az olabilir (ör. bazı backend'ler tek executemany'e sığdırabilir) -
    # bu yüzden ÜST SINIR gevşek ama 1000'in ÇOK altında tutulur.
    assert counter.update <= len(changed_indices) + 10, (
        f"UPDATE sayısı ({counter.update}) değişen satır sayısına (~{len(changed_indices)}) "
        f"yakın olmalı, TÜM satırlara (1000) DEĞİL"
    )
    for i in changed_indices:
        flight = session.execute(
            select(Flight).where(Flight.flight_key == changed_rows[i]["flight_key"])
        ).scalar_one()
        assert flight.aircraft_icao == "A321"


# ========================================================================
# Bölüm 19D - chunk içinde 1 bozuk satır -> iyi satırlar HAYATTA KALIR
# ========================================================================

def test_d_one_bad_row_in_chunk_preserves_good_rows(session):
    rows = [_make_row(i) for i in range(20)]
    # KASITLI bozuk satır: flight_key None - Flight.flight_key NOT NULL/UNIQUE,
    # flush sırasında constraint ihlali üretir.
    rows[10]["flight_key"] = None

    result = refresh_flights(session, rows)

    assert result["failed"] == 1, "SADECE bozuk satır 'failed' sayılmalı"
    assert result["inserted"] == 19, "diğer 19 GEÇERLİ satır İYİ satırlar olarak eklenmeli"

    good_keys = [rows[i]["flight_key"] for i in range(20) if i != 10]
    present = {
        f.flight_key for f in session.execute(select(Flight)).scalars().all()
    }
    assert set(good_keys) <= present, "chunk fallback'i sonrası TÜM iyi satırlar DB'de olmalı"
    assert len(present) == 19


def test_d_bad_row_isolation_works_even_when_bad_row_is_first_in_chunk(session):
    rows = [_make_row(i) for i in range(15)]
    rows[0]["flight_key"] = None

    result = refresh_flights(session, rows)

    assert result["failed"] == 1
    assert result["inserted"] == 14


# ========================================================================
# Bölüm 19E - duplicate flight_key (AYNI batch içinde) -> duplicate row YOK
# ========================================================================

def test_e_duplicate_flight_key_within_same_batch_no_duplicate_row(session):
    rows = [_make_row(0), _make_row(0)]  # AYNI flight_key iki kez, AYNI batch
    rows[1]["aircraft_icao"] = "A321"  # ikinci tekrar farklı bir alan taşısın

    result = refresh_flights(session, rows)

    matches = session.execute(
        select(Flight).where(Flight.flight_key == rows[0]["flight_key"])
    ).scalars().all()
    assert len(matches) == 1, "AYNI batch içindeki duplicate flight_key DUPLICATE DB satırı ÜRETMEMELİ"
    assert matches[0].aircraft_icao == "A321", "batch içindeki İKİNCİ (son) satır KAZANMALI - eski satır-satır davranışıyla TUTARLI"
    assert result["inserted"] == 1
    assert result["updated"] == 1


def test_e_duplicate_flight_key_across_chunks_no_duplicate_row(session, monkeypatch):
    """AYNI flight_key, chunk sınırının İKİ tarafında (bir chunk'ta INSERT, sonrakinde UPDATE görmeli)."""
    monkeypatch.setattr("app.queue.ingestion.refresh.REFRESH_CHUNK_SIZE", 5)
    rows = [_make_row(i) for i in range(10)]
    rows[7] = dict(rows[2])  # index 2'nin flight_key'i chunk 2'de (index 5-9) tekrar geliyor
    rows[7]["aircraft_icao"] = "A359"

    result = refresh_flights(session, rows)

    matches = session.execute(
        select(Flight).where(Flight.flight_key == rows[2]["flight_key"])
    ).scalars().all()
    assert len(matches) == 1
    assert matches[0].aircraft_icao == "A359"


# ========================================================================
# Bölüm 21 - Baseline cache: `get_baseline`/`get_passenger_baseline` ile
# GERÇEKTEN AYNI sonucu üretiyor mu (multi-airport izolasyon dahil)
# ========================================================================

def test_baseline_cache_matches_direct_lookup_multi_airport(session):
    """Bölüm 15/21 - iki farklı havalimanı, farklı baseline verisi, cross-airport sızıntı YOK."""
    session.add_all([
        HistoricalFlightCount(
            airport_iata="IST", process="passport_dep", hour_of_day=10, day_of_week=1,
            average_flight_count=12.5, sample_size=8,
            average_expected_passengers=1800.0, passenger_sample_size=8,
        ),
        HistoricalFlightCount(
            airport_iata="IST", process="security_dom", hour_of_day=14, day_of_week=1,
            average_flight_count=5.0, sample_size=3,
            average_expected_passengers=None, passenger_sample_size=0,
        ),
        HistoricalFlightCount(
            airport_iata="CDG", process="passport_dep", hour_of_day=10, day_of_week=1,
            average_flight_count=30.0, sample_size=20,
            average_expected_passengers=4200.0, passenger_sample_size=20,
        ),
    ])
    session.commit()

    ist_window = datetime(2026, 9, 15, 10, 0)  # Tuesday, hour=10, weekday=1
    security_window = datetime(2026, 9, 15, 14, 0)

    ist_baseline_fn = _db_baseline_fn(session, "IST")
    ist_passenger_fn = _db_passenger_baseline_fn(session, "IST")
    cdg_baseline_fn = _db_baseline_fn(session, "CDG")
    cdg_passenger_fn = _db_passenger_baseline_fn(session, "CDG")

    # --- IST passport_dep: cache sonucu == direkt get_baseline() sonucu ---
    assert ist_baseline_fn("passport_dep", ist_window) == get_baseline(
        session, "IST", "passport_dep", 10, 1,
    ) == 12.5
    assert ist_passenger_fn("passport_dep", ist_window) == get_passenger_baseline(
        session, "IST", "passport_dep", 10, 1,
    ) == 1800.0

    # --- IST security_dom: passenger_sample_size=0 -> None (sahte veri YOK) ---
    assert ist_baseline_fn("security_dom", security_window) == get_baseline(
        session, "IST", "security_dom", 14, 1,
    ) == 5.0
    assert ist_passenger_fn("security_dom", security_window) == get_passenger_baseline(
        session, "IST", "security_dom", 14, 1,
    ) is None

    # --- CDG: AYNI (process, hour, day) IST'ten TAMAMEN FARKLI değer - sızıntı YOK ---
    assert cdg_baseline_fn("passport_dep", ist_window) == get_baseline(
        session, "CDG", "passport_dep", 10, 1,
    ) == 30.0
    assert cdg_passenger_fn("passport_dep", ist_window) == get_passenger_baseline(
        session, "CDG", "passport_dep", 10, 1,
    ) == 4200.0

    # --- CDG'nin cache'i IST'in olmayan bucket'ını GÖRMEMELİ ---
    assert cdg_baseline_fn("security_dom", security_window) is None


def test_baseline_cache_missing_bucket_returns_none_no_fake_fallback(session):
    """Bölüm 13 - hiç HistoricalFlightCount satırı yoksa None (uydurma baseline YOK) - cache öncesi/sonrası AYNI."""
    baseline_fn = _db_baseline_fn(session, "ZZZ")
    passenger_fn = _db_passenger_baseline_fn(session, "ZZZ")
    window = datetime(2026, 9, 15, 10, 0)

    assert baseline_fn("passport_dep", window) is None
    assert passenger_fn("passport_dep", window) is None
    assert get_baseline(session, "ZZZ", "passport_dep", 10, 1) is None
    assert get_passenger_baseline(session, "ZZZ", "passport_dep", 10, 1) is None


def test_baseline_cache_shared_between_flight_and_passenger_fn_is_single_bulk_load(session):
    """Bölüm 12 - `cache=` paylaşıldığında `_db_baseline_fn`/`_db_passenger_baseline_fn` İKİ AYRI bulk SELECT yapmamalı."""
    from app.queue.engine import _load_historical_flight_count_cache

    session.add(HistoricalFlightCount(
        airport_iata="IST", process="passport_dep", hour_of_day=10, day_of_week=1,
        average_flight_count=12.5, sample_size=8,
    ))
    session.commit()

    engine = session.get_bind()
    counter = SqlCounter(engine)
    cache = _load_historical_flight_count_cache(session, "IST")
    baseline_fn = _db_baseline_fn(session, "IST", cache=cache)
    passenger_fn = _db_passenger_baseline_fn(session, "IST", cache=cache)

    window = datetime(2026, 9, 15, 10, 0)
    for _ in range(50):  # 50 farklı pencere sorgusu simüle edilsin
        baseline_fn("passport_dep", window)
        passenger_fn("passport_dep", window)

    assert counter.select == 1, f"paylaşılan cache SADECE 1 bulk SELECT yapmalı, {counter.select} yapıldı"
