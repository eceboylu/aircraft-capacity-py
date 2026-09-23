"""
ADIM (MySQL Performance Fix - Phase 2) - `persist_predictions()`
bulk-prefetch/no-op-skip davranışını doğrular.

Queue matematiği/`predict_airport()`/`run_predictions()` BURADA HİÇ
YENİDEN YAZILMAZ/TEKRARLANMAZ - `WindowPrediction` nesneleri doğrudan
elle kurulur (10k benchmark'ta ölçülen bottleneck sadece PERSISTENCE
katmanındaydı, hesap katmanında değil), GERÇEK, DEĞİŞTİRİLMEMİŞ
`persist_predictions()` çağrılır, SADECE etrafındaki SQL istatistiklerine
(SELECT/INSERT/UPDATE sayısı) ve DB satırlarına bakılır - `tests/
test_refresh_bulk_performance.py`'nin (Phase 1) AYNI deseni/AYNI gevşek
üst-sınır felsefesi ("Do not overfit fragile exact counts").
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.constants import PROCESS_SECURITY, RISK_LOW, RISK_MEDIUM
from app.queue.engine import WindowPrediction, persist_predictions
from app.queue.models import QueuePrediction


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


def _make_prediction(n: int, airport="IST", **overrides) -> WindowPrediction:
    window_start = datetime(2026, 9, 15, 10, 0) + timedelta(minutes=15 * n)
    kwargs = dict(
        airport_iata=airport,
        process=PROCESS_SECURITY,
        window_start=window_start,
        window_end=window_start + timedelta(minutes=15),
        flight_count=3,
        expected_passengers=300,
        baseline_ratio=1.0,
        utilization=0.5,
        estimated_wait_minutes=4.0,
        risk=RISK_LOW,
        confidence=0.9,
    )
    kwargs.update(overrides)
    return WindowPrediction(**kwargs)


# ========================================================================
# A - 1000 prediction ilk persist -> insert
# ========================================================================

def test_a_bulk_insert_1000_predictions_no_select_per_prediction(session):
    engine = session.get_bind()
    predictions = [_make_prediction(i) for i in range(1000)]

    counter = SqlCounter(engine)
    result = persist_predictions(session, predictions)

    assert result["inserted"] == 1000
    assert result["updated"] == 0
    assert session.query(QueuePrediction).count() == 1000
    # 1000 prediction, PREDICTION_PERSIST_CHUNK_SIZE'lık chunk'larla en
    # az bulk SELECT (chunk sayısı kadar) - 1000'e YAKIN DEĞİL.
    assert counter.select < 50, (
        f"SELECT sayısı ({counter.select}) prediction-başına-SELECT deseni gösteriyor"
    )


# ========================================================================
# B - AYNI 1000 tekrar -> 0 SQL UPDATE
# ========================================================================

def test_b_identical_repeat_zero_queueprediction_update(session):
    engine = session.get_bind()
    predictions = [_make_prediction(i) for i in range(1000)]
    persist_predictions(session, predictions)

    before_count = session.query(QueuePrediction).count()

    # Aynı içerikle, TAMAMEN YENİ WindowPrediction nesneleri (aynı
    # session'daki eski ORM referanslarına YASLANMIYOR - gerçek "aynı
    # payload ikinci kez geldi" senaryosu).
    identical = [_make_prediction(i) for i in range(1000)]

    counter = SqlCounter(engine)
    result = persist_predictions(session, identical)

    assert result["inserted"] == 0
    assert result["updated"] == 1000, "raporlama sözleşmesi DEĞİŞMEDİ - eşleşen HER satır YİNE DE 'updated' sayılır"
    assert session.query(QueuePrediction).count() == before_count
    assert counter.update == 0, (
        f"içerik BİREBİR AYNIYSA QueuePrediction'a HİÇBİR SQL UPDATE gitmemeli - görülen: {counter.update}"
    )


# ========================================================================
# C - %10 değişiklik -> SADECE değişen satırlar UPDATE alır
# ========================================================================

def test_c_ten_percent_changed_only_changed_rows_get_sql_update(session):
    engine = session.get_bind()
    predictions = [_make_prediction(i) for i in range(1000)]
    persist_predictions(session, predictions)

    changed_indices = list(range(0, 1000, 10))  # tam %10
    changed = []
    for i in range(1000):
        if i in changed_indices:
            changed.append(_make_prediction(i, risk=RISK_MEDIUM, estimated_wait_minutes=12.0))
        else:
            changed.append(_make_prediction(i))

    counter = SqlCounter(engine)
    result = persist_predictions(session, changed)

    assert result["inserted"] == 0
    assert result["updated"] == 1000
    assert counter.update <= len(changed_indices) + 10, (
        f"UPDATE sayısı ({counter.update}) değişen satır sayısına (~{len(changed_indices)}) "
        f"yakın olmalı, TÜM satırlara (1000) DEĞİL"
    )
    for i in changed_indices:
        row = session.query(QueuePrediction).filter_by(
            airport_iata="IST", process=PROCESS_SECURITY,
            window_start=changed[i].window_start,
        ).one()
        assert row.risk == RISK_MEDIUM
        assert row.estimated_wait_minutes == 12.0


# ========================================================================
# D - Mevcut 1000 + yeni 50 -> sadece 50 insert
# ========================================================================

def test_d_new_predictions_among_existing_only_new_ones_inserted(session):
    predictions = [_make_prediction(i) for i in range(1000)]
    persist_predictions(session, predictions)

    mixed = [_make_prediction(i) for i in range(1000)] + [_make_prediction(i) for i in range(1000, 1050)]
    result = persist_predictions(session, mixed)

    assert result["inserted"] == 50
    assert result["updated"] == 1000
    assert session.query(QueuePrediction).count() == 1050


# ========================================================================
# E - Unique key ihlali yok
# ========================================================================

def test_e_no_duplicate_unique_keys_after_repeated_persist(session):
    predictions = [_make_prediction(i) for i in range(200)]
    persist_predictions(session, predictions)
    persist_predictions(session, [_make_prediction(i) for i in range(200)])
    persist_predictions(session, [_make_prediction(i, risk=RISK_MEDIUM) for i in range(200)])

    rows = session.query(
        QueuePrediction.airport_iata, QueuePrediction.process, QueuePrediction.window_start
    ).all()
    assert len(rows) == len(set(rows)) == 200


# ========================================================================
# calculated_at no-op'ta donar, gerçek değişiklikte ilerler
# ========================================================================

def test_calculated_at_frozen_on_noop_advances_on_real_change(session):
    predictions = [_make_prediction(0)]
    persist_predictions(session, predictions)
    row = session.query(QueuePrediction).one()
    first_calculated_at = row.calculated_at

    persist_predictions(session, [_make_prediction(0)])
    session.refresh(row)
    assert row.calculated_at == first_calculated_at, "no-op'ta calculated_at DONMALI"

    time.sleep(0.01)  # calculated_at'in GERÇEK bir değişiklikte ölçülebilir şekilde ilerlediğini garanti eder
    persist_predictions(session, [_make_prediction(0, risk=RISK_MEDIUM)])
    session.refresh(row)
    assert row.calculated_at > first_calculated_at, "gerçek değişiklikte calculated_at İLERLEMELİ"
    assert row.risk == RISK_MEDIUM
