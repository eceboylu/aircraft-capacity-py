"""
AŞAMA 8 (kalıcılık) + AŞAMA 9 (raporlama) testleri.

Bellek içi SQLite kullanılır; gerçek dosya veya ağ yoktur.
Burada iki şey kanıtlanır:
  1. Tekrarlanan hesap tabloyu ŞİŞİRMEZ (upsert).
  2. Raporlar YENİ HESAP TETİKLEMEZ, sadece yazılmış satırları okur.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.baseline import get_baseline
from app.queue.config import get_config
from app.queue.constants import (
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    RISK_UNKNOWN,
)
from app.queue.engine import run_predictions
from app.queue.models import (
    AirportOperationalConfig,
    Flight,
    QueuePrediction,
)
from app.queue.reporting import hourly_report, summary_report

from .factories import BASE_DAY, MockCapacityResolver, arrival, departure

REPORT_DAY = BASE_DAY.date()


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


def store(db, mock_flights):
    """Mock uçuşları gerçek Flight satırlarına çevirir."""
    columns = {c.name for c in Flight.__table__.columns}
    for mock in mock_flights:
        values = {
            name: value
            for name, value in vars(mock).items()
            if name in columns
        }
        db.add(Flight(**values))
    db.commit()


def intl_departure(hour, minute=0, **kwargs):
    kwargs.setdefault("location", LOCATION_INTERNATIONAL)
    return departure(hour, minute, **kwargs)


def spread_config(db, airport, counters=24):
    """Havalimanına kendi config satırını yazar."""
    db.add(AirportOperationalConfig(
        airport_iata=airport,
        passport_counter_count=counters,
    ))
    db.commit()


# --------------------------------------------------------------------
# Kalıcılık - şişme koruması
# --------------------------------------------------------------------

def test_repeated_runs_do_not_create_duplicate_predictions(session):
    store(session, [
        intl_departure(9, 0, key="R1", number="1", aircraft="E190"),
        intl_departure(9, 5, key="R2", number="2", aircraft="E190"),
    ])
    resolver = MockCapacityResolver()

    first = run_predictions(session, resolver, update_baseline=False)
    count_after_first = session.scalar(
        select(func.count()).select_from(QueuePrediction)
    )

    second = run_predictions(session, resolver, update_baseline=False)
    count_after_second = session.scalar(
        select(func.count()).select_from(QueuePrediction)
    )

    assert first["inserted"] == count_after_first
    assert second["inserted"] == 0          # hepsi güncellendi
    assert second["updated"] == count_after_first
    assert count_after_second == count_after_first


def test_prediction_rows_carry_reasons_as_json(session):
    store(session, [intl_departure(9, 0, key="J1", number="1")])
    run_predictions(session, MockCapacityResolver(), update_baseline=False)

    row = session.execute(select(QueuePrediction)).scalars().first()
    assert row.reasons.startswith("[")
    assert row.confidence > 0


def test_security_rows_store_real_wait_minutes(session):
    store(session, [
        departure(9, m, key=f"S{m}", number=str(m), location=LOCATION_DOMESTIC)
        for m in (0, 3, 6)
    ])
    run_predictions(session, MockCapacityResolver(), update_baseline=False)

    rows = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.process == PROCESS_SECURITY
        )
    ).scalars().all()

    assert rows
    assert all(r.estimated_wait_minutes is not None for r in rows)
    assert all(r.utilization is not None for r in rows)


# --------------------------------------------------------------------
# Baseline birikimi
# --------------------------------------------------------------------

def test_baseline_accumulates_only_after_observation(session):
    store(session, [
        departure(9, m, key=f"B{m}", number=str(m), location=LOCATION_DOMESTIC)
        for m in (0, 3, 6)
    ])
    resolver = MockCapacityResolver()

    # İlk çalıştırmada geçmiş oran yok; queue riski fiziksel kapasiteden gelir.
    run_predictions(session, resolver, update_baseline=True)
    first = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.process == PROCESS_SECURITY
        )
    ).scalars().first()
    assert first.risk == "CRITICAL"
    assert first.baseline_ratio is None

    # Gözlem kaydedildi; ikinci çalıştırma artık karşılaştırabiliyor.
    assert get_baseline(session, "AAA", PROCESS_SECURITY, 8, 0) is not None

    run_predictions(session, resolver, update_baseline=False)
    second = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.process == PROCESS_SECURITY
        )
    ).scalars().first()
    assert second.baseline_ratio is not None
    assert second.risk != RISK_UNKNOWN


# --------------------------------------------------------------------
# Config - havalimanı bazlı
# --------------------------------------------------------------------

def test_config_defaults_are_flagged_as_default(session):
    config = get_config(session, "AAA")
    assert config.is_default is True
    assert config.passport_counter_count == 4


def test_config_row_overrides_defaults(session):
    spread_config(session, "IST", counters=30)
    config = get_config(session, "IST")
    assert config.is_default is False
    assert config.passport_counter_count == 30


def test_new_airport_needs_no_code_change(session):
    """Yeni havalimanı = yeni config satırı. Kod değişmez."""
    spread_config(session, "ADB", counters=9)
    store(session, [
        intl_departure(9, 0, airport="ADB", key="N1", number="1")
    ])
    run_predictions(session, MockCapacityResolver(), update_baseline=False)

    rows = session.execute(
        select(QueuePrediction).where(QueuePrediction.airport_iata == "ADB")
    ).scalars().all()
    assert rows


# --------------------------------------------------------------------
# AŞAMA 9 - Saatlik rapor
# --------------------------------------------------------------------

def test_hourly_report_shape(session):
    spread_config(session, "AAA", counters=24)
    store(session, [
        intl_departure(9, 0, key="H1", number="1", aircraft="E190"),
        departure(9, 5, key="H2", number="2", location=LOCATION_DOMESTIC,
                  aircraft="E190"),
        arrival(9, 30, key="H3", number="3", aircraft="E190"),
    ])
    resolver = MockCapacityResolver()
    run_predictions(session, resolver, update_baseline=False)

    report = hourly_report(session, "AAA", REPORT_DAY, resolver)

    assert report["airport"] == "AAA"
    assert report["date"] == REPORT_DAY.isoformat()
    assert report["hourly"]

    hours = {entry["hour"] for entry in report["hourly"]}
    assert all(len(h) == 5 and h.endswith(":00") for h in hours)


def test_hourly_report_never_lists_domestic_arrival(session):
    store(session, [
        arrival(9, 0, key="DA1", number="1", location=LOCATION_DOMESTIC),
        intl_departure(9, 0, key="ID1", number="2"),
    ])
    resolver = MockCapacityResolver()
    run_predictions(session, resolver, update_baseline=False)

    report = hourly_report(session, "AAA", REPORT_DAY, resolver)
    for entry in report["hourly"]:
        assert "domestic_arrival" not in entry["flows"]
        assert set(entry["flows"]) == {
            "domestic_departure",
            "international_departure",
            "international_arrival",
        }


def test_hourly_report_security_block_has_real_minutes(session):
    store(session, [
        departure(9, m, key=f"M{m}", number=str(m), location=LOCATION_DOMESTIC)
        for m in (0, 3, 6)
    ])
    resolver = MockCapacityResolver()
    run_predictions(session, resolver, update_baseline=False)

    report = hourly_report(session, "AAA", REPORT_DAY, resolver)
    blocks = [
        entry[PROCESS_SECURITY]
        for entry in report["hourly"]
        if entry[PROCESS_SECURITY]
    ]
    assert blocks
    assert all(b["estimated_wait_minutes"] is not None for b in blocks)
    assert all(b["utilization"] is not None for b in blocks)


def test_hourly_report_reasons_are_deduplicated_by_code(session):
    """Aynı neden saatin birden çok penceresinde çıksa da bir kez listelenir."""
    spread_config(session, "AAA", counters=24)
    store(session, [
        intl_departure(9, 0, key="X1", number="1", aircraft="B77W"),
        intl_departure(9, 5, key="X2", number="2", aircraft="B77W"),
        intl_departure(9, 20, key="X3", number="3", aircraft="B77W"),
        intl_departure(9, 25, key="X4", number="4", aircraft="B77W"),
    ])
    resolver = MockCapacityResolver()
    run_predictions(session, resolver, update_baseline=False)

    report = hourly_report(session, "AAA", REPORT_DAY, resolver)
    for entry in report["hourly"]:
        block = entry[PROCESS_PASSPORT]
        if not block:
            continue
        found = [r["code"] for r in block["reasons"]]
        assert len(found) == len(set(found))


def test_hourly_report_does_not_write_anything(session):
    store(session, [intl_departure(9, 0, key="RO1", number="1")])
    resolver = MockCapacityResolver()
    run_predictions(session, resolver, update_baseline=False)

    before = session.scalar(
        select(func.count()).select_from(QueuePrediction)
    )
    hourly_report(session, "AAA", REPORT_DAY, resolver)
    summary_report(session, REPORT_DAY)
    after = session.scalar(select(func.count()).select_from(QueuePrediction))

    assert before == after


# --------------------------------------------------------------------
# AŞAMA 9 - Özet rapor, çok havalimanlı
# --------------------------------------------------------------------

def test_summary_lists_every_airport_separately(session):
    spread_config(session, "SAW", counters=24)
    store(session, [
        intl_departure(9, m, airport="IST", key=f"I{m}", number=str(m),
                       aircraft="B77W")
        for m in (0, 3, 6, 9)
    ] + [
        intl_departure(18, 0, airport="SAW", key="S1", number="1",
                       aircraft="E190"),
    ])
    run_predictions(session, MockCapacityResolver(), update_baseline=False)

    summary = summary_report(session, REPORT_DAY)
    rows = {row["airport"]: row for row in summary["airports"]}

    assert set(rows) == {"IST", "SAW"}
    # IST varsayılan config'le (4 gişe) doygun, SAW kendi config'iyle rahat.
    # ADIM 6D ile GÜNCELLENDİ: rho>=1 artık None DEĞİL, backlog tabanlı
    # sonlu bir dakika üretir (bkz. core/scoring.py passport_queue_model).
    assert rows["IST"]["peak_passport_risk"] == "CRITICAL"
    assert rows["IST"]["peak_passport_wait_minutes"] is not None
    assert rows["IST"]["peak_passport_wait_minutes"] > 0
    assert rows["SAW"]["peak_passport_risk"] == "LOW"
    assert rows["SAW"]["peak_passport_wait_minutes"] is not None
    # Tepe saatler birbirinden bağımsız.
    assert rows["IST"]["peak_passport_hour"] != rows["SAW"]["peak_passport_hour"]


def test_summary_security_peak_has_no_wait_minutes_field_value(session):
    store(session, [
        departure(9, m, key=f"P{m}", number=str(m), location=LOCATION_DOMESTIC)
        for m in (0, 3)
    ])
    run_predictions(session, MockCapacityResolver(), update_baseline=False)

    summary = summary_report(session, REPORT_DAY)
    row = summary["airports"][0]
    assert row["peak_security_hour"] is not None
    # Özet yapısında security dakikası DİYE BİR ALAN YOK.
    assert "peak_security_wait_minutes" not in row


def test_summary_for_day_without_predictions_is_empty(session):
    summary = summary_report(session, date(2020, 1, 1))
    assert summary["airports"] == []


# --------------------------------------------------------------------
# Bayat tahmin satırları
# --------------------------------------------------------------------

def test_delayed_flight_leaves_no_ghost_demand_in_old_window(session):
    """
    Uçuş gecikip başka pencereye taşındığında eski pencerenin tahmini
    ortada kalmamalı - orada artık yolcu yok.
    """
    flight = intl_departure(9, 0, key="G1", number="1", aircraft="E190")
    store(session, [flight])
    resolver = MockCapacityResolver()
    run_predictions(session, resolver, update_baseline=False)

    before = {
        (r.process, r.window_start)
        for r in session.execute(select(QueuePrediction)).scalars()
    }
    assert before

    # Uçuş 3 saat gecikti: efektif saati başka pencereye düştü.
    row = session.execute(select(Flight)).scalars().one()
    row.dep_estimated_utc = row.dep_scheduled_utc + timedelta(hours=3)
    session.commit()

    result = run_predictions(session, resolver, update_baseline=False)

    after = {
        (r.process, r.window_start)
        for r in session.execute(select(QueuePrediction)).scalars()
    }
    assert result["pruned"] == len(before)
    assert after.isdisjoint(before)
    assert len(after) == len(before)


def test_pruning_does_not_touch_other_airports(session):
    store(session, [
        intl_departure(9, 0, airport="IST", key="T1", number="1"),
        intl_departure(9, 0, airport="SAW", key="T2", number="2"),
    ])
    resolver = MockCapacityResolver()
    run_predictions(session, resolver, update_baseline=False)

    saw_before = {
        r.window_start
        for r in session.execute(
            select(QueuePrediction).where(
                QueuePrediction.airport_iata == "SAW"
            )
        ).scalars()
    }

    ist = session.execute(
        select(Flight).where(Flight.airport_iata == "IST")
    ).scalars().one()
    ist.dep_estimated_utc = ist.dep_scheduled_utc + timedelta(hours=5)
    session.commit()
    run_predictions(session, resolver, update_baseline=False)

    saw_after = {
        r.window_start
        for r in session.execute(
            select(QueuePrediction).where(
                QueuePrediction.airport_iata == "SAW"
            )
        ).scalars()
    }
    assert saw_after == saw_before


def test_unchanged_rerun_prunes_nothing(session):
    store(session, [intl_departure(9, 0, key="U1", number="1")])
    resolver = MockCapacityResolver()
    run_predictions(session, resolver, update_baseline=False)
    result = run_predictions(session, resolver, update_baseline=False)

    assert result["pruned"] == 0
    assert result["inserted"] == 0
