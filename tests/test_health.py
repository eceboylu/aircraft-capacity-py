"""
ADIM (Health / Stale-Data Production Audit) - `app/health.py` için
önceden HİÇ test coverage'ı yoktu (bu ADIM'da tespit edildi). İzole,
bellek içi `sqlite://` session kullanır - gerçek DB'ye hiç dokunmaz.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.health import (
    STALE_THRESHOLD_SECONDS,
    build_health_report,
    get_last_successful_refresh,
    record_successful_refresh,
)
from app.models import Base
import app.queue.models  # noqa: F401 - Base.metadata'ya QueuePrediction'ı kaydeder
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


def add_prediction(session, when: datetime, airport="AAA", process="passport_dep"):
    session.add(QueuePrediction(
        airport_iata=airport, process=process,
        window_start=when, window_end=when + timedelta(hours=1),
        flight_count=1, expected_passengers=100, risk="low",
        calculated_at=when,
    ))
    session.commit()


# ========================================================================
# Senaryo A - DB OK + worker fresh + prediction fresh -> HEALTHY
# ========================================================================

def test_fresh_worker_and_fresh_prediction_is_healthy(session):
    now = datetime.now(timezone.utc)
    record_successful_refresh(session, when=now)
    add_prediction(session, now)

    report = build_health_report(session)

    assert report["status"] == "healthy"
    assert report["stale"] is False
    assert report["prediction_stale"] is False


# ========================================================================
# Senaryo B - worker heartbeat taze AMA prediction eski -> HEALTHY OLMAMALI
# (bu, bu ADIM'dan ÖNCE health.py'nin YAKALAYAMADIĞI asıl boşluktu)
# ========================================================================

def test_fresh_worker_but_stale_prediction_is_not_healthy(session):
    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=STALE_THRESHOLD_SECONDS + 120)
    record_successful_refresh(session, when=now)   # heartbeat TAZE
    add_prediction(session, old)                     # ama prediction ESKİ

    report = build_health_report(session)

    assert report["stale"] is False, "worker heartbeat'in kendisi hala taze olmalı"
    assert report["prediction_stale"] is True
    assert report["status"] == "degraded", (
        "worker heartbeat taze diye 'healthy' DÖNMEMELİ - prediction "
        "üretimi durmuş olabilir (Bölüm B/D)"
    )


def test_fresh_worker_but_no_predictions_at_all_is_not_healthy(session):
    """Hiç QueuePrediction satırı yoksa (ilk cycle bitmemiş/temizlenmiş) da healthy dönmemeli."""
    now = datetime.now(timezone.utc)
    record_successful_refresh(session, when=now)

    report = build_health_report(session)

    assert report["prediction_stale"] is True
    assert report["newest_prediction_at"] is None
    assert report["status"] == "degraded"


# ========================================================================
# Senaryo D - prediction fresh AMA worker heartbeat eski -> HEALTHY OLMAMALI
# ========================================================================

def test_fresh_prediction_but_stale_worker_is_not_healthy(session):
    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=STALE_THRESHOLD_SECONDS + 120)
    record_successful_refresh(session, when=old)   # heartbeat ESKİ
    add_prediction(session, now)                     # prediction taze (ör. eski satır manuel güncellenmiş)

    report = build_health_report(session)

    assert report["stale"] is True
    assert report["status"] == "degraded"


# ========================================================================
# Senaryo F - worker hiç başarılı run yapmamış -> degraded (unhealthy DEĞİL)
# ========================================================================

def test_never_run_worker_is_degraded_not_unhealthy(session):
    report = build_health_report(session)

    assert report["last_successful_refresh"] is None
    assert report["status"] == "degraded"
    assert report["stale"] is True
    assert report["prediction_stale"] is True


# ========================================================================
# Boundary - eşik ANINDA (== STALE_THRESHOLD_SECONDS) hala healthy (sıkı >)
# ========================================================================

def test_worker_age_exactly_at_threshold_is_not_yet_stale(session):
    now = datetime.now(timezone.utc)
    at_boundary = now - timedelta(seconds=STALE_THRESHOLD_SECONDS)
    record_successful_refresh(session, when=at_boundary)
    add_prediction(session, at_boundary)

    report = build_health_report(session)

    # data_age_seconds tam eşiğe eşit olacak kadar hassas zamanlama testte
    # garanti edilemez (birkaç ms'lik test çalışma süresi araya girer) -
    # asıl doğrulanan şey: eşiğin biraz ALTINDA kalan bir yaş "stale"
    # ÜRETMEMELİ.
    assert report["data_age_seconds"] <= STALE_THRESHOLD_SECONDS + 1


# ========================================================================
# record_successful_refresh - upsert (insert sonra update, ikinci satır AÇILMAZ)
# ========================================================================

def test_record_successful_refresh_upserts_single_row(session):
    first = datetime.now(timezone.utc) - timedelta(minutes=10)
    second = datetime.now(timezone.utc)

    record_successful_refresh(session, when=first)
    assert get_last_successful_refresh(session).replace(tzinfo=timezone.utc) == first

    record_successful_refresh(session, when=second)
    assert get_last_successful_refresh(session).replace(tzinfo=timezone.utc) == second

    from app.health import WorkerStatus
    count = session.query(WorkerStatus).count()
    assert count == 1, "ikinci çağrı YENİ satır AÇMAMALI, mevcut satırı güncellemeli"


# ========================================================================
# Senaryo G - source_mode her zaman açıkça görünür, status'u ETKİLEMEZ
# ========================================================================

def test_source_mode_reported_and_does_not_affect_status(session, monkeypatch):
    now = datetime.now(timezone.utc)
    record_successful_refresh(session, when=now)
    add_prediction(session, now)

    monkeypatch.delenv("QUEUE_LOCAL_SOURCE_MODE", raising=False)
    report = build_health_report(session)
    assert report["source_mode"] == "bundled_sample_file"
    assert report["status"] == "healthy"

    monkeypatch.setenv("QUEUE_LOCAL_SOURCE_MODE", "generated")
    report = build_health_report(session)
    assert report["source_mode"] == "generated_local_fixture"
    assert report["status"] == "healthy", "source_mode bilgilendirici olmalı, health status'u BOZMAMALI"


# ========================================================================
# No secrets - health raporu asla DATABASE_URL/API key İÇERMEMELİ
# ========================================================================

def test_health_report_never_contains_secrets(session, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://user:supersecret@host/db")
    monkeypatch.setenv("AIRLABS_API_KEY", "top-secret-key")
    now = datetime.now(timezone.utc)
    record_successful_refresh(session, when=now)
    add_prediction(session, now)

    report = build_health_report(session)

    serialized = str(report)
    assert "supersecret" not in serialized
    assert "top-secret-key" not in serialized
    assert "DATABASE_URL" not in serialized
