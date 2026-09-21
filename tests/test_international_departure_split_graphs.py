"""
ADIM (International Departure Split Graphs) - Phase 9-19/21.6-8/21.16
doğrulaması.

International Departure artık İKİ AYRI grafik/zaman serisi:
  - PASSPORT  : SADECE `PROCESS_PASSPORT_DEPARTURE`
  - SECURITY  : SADECE `PROCESS_SECURITY_INTL`

Biri diğerinin `current` saatini ASLA EZMEZ (eski "worst-of" tek
pencere birleştirmesi KALDIRILDI, bkz. `app/queue/api.py:
_international_departure_split`). Security event'in arrival zamanı
passport'un `completion_time`'ı olmaya DEVAM EDİYOR (bkz.
`engine.py:_event_driven_queue_demand` - bu coupling DEĞİŞMEDİ, sadece
API/frontend'in bunu İKİ ayrı seri olarak SUNMA şekli değişti).
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.constants import (
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY_INTL,
    RISK_CRITICAL,
    RISK_LOW,
)
from app.queue.models import QueuePrediction


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def add_row(session, process, *, airport="ZRH", window_start, window_end,
            risk, wait, utilization=0.5, flight_count=3, expected_passengers=320):
    row = QueuePrediction(
        airport_iata=airport, process=process,
        window_start=window_start, window_end=window_end,
        flight_count=flight_count, expected_passengers=expected_passengers,
        utilization=utilization, estimated_wait_minutes=wait,
        risk=risk, reasons=json.dumps([]), confidence=0.8,
    )
    session.add(row)
    session.commit()
    return row


# ========================================================================
# ZRH regresyon testi (Phase 16/21.8) - passport 06:00'da backlog dolduruyor,
# security ancak 08:00'da (passport'un completion_time'ı) ulaşıyor.
# ========================================================================

def test_zrh_passport_shows_own_hour_not_dash_because_security_current_is_elsewhere(session):
    """
    Bu senaryo, ZRH'de GERÇEKTEN gözlenen davranışın yapısal bir
    tekrarıdır: passport_dep 06:00 penceresinde ~66.8 dk bekleme
    üretirken, security_intl (passport completion_time'ından beslendiği
    için) ancak 08:00'da event alıyor ve orada wait=0 gösteriyor.

    Eski (birleşik "worst-of") tasarımda security'nin 08:00 current'ı
    passport'un current seçimini de EZİYORDU - passport paneli "—"
    gösterebiliyordu (Phase 16'nın işaret ettiği kafa karıştırıcı bug).
    Artık her ikisi KENDİ current'ını taşıyor.
    """
    add_row(
        session, PROCESS_PASSPORT_DEPARTURE,
        window_start=datetime(2026, 9, 15, 6, 0), window_end=datetime(2026, 9, 15, 7, 0),
        risk=RISK_CRITICAL, wait=66.8,
    )
    add_row(
        session, PROCESS_SECURITY_INTL,
        window_start=datetime(2026, 9, 15, 8, 0), window_end=datetime(2026, 9, 15, 9, 0),
        risk=RISK_LOW, wait=0.0,
    )

    # "now" 06:xx - passport'un GERÇEK current penceresi içinde.
    api = airport_predictions(session, "ZRH", now=datetime(2026, 9, 15, 6, 30))
    intl_dep = api["international_departure"]

    passport_current = intl_dep["passport"]["current"]
    security_current = intl_dep["security"]["current"]

    # Passport current'ı KENDİ 06:00 penceresi, "—" DEĞİL.
    assert passport_current is not None
    assert passport_current["window_start"] == datetime(2026, 9, 15, 6, 0).isoformat()
    assert passport_current["estimated_wait_minutes"] == 66.8
    assert passport_current["risk"] == RISK_CRITICAL

    # Security current'ı KENDİ 08:00 penceresi (henüz gelecek, ama en
    # yakın gelecek pencere olarak seçilir) - passport'un değerini
    # ASLA almaz.
    assert security_current is not None
    assert security_current["window_start"] == datetime(2026, 9, 15, 8, 0).isoformat()
    assert security_current["estimated_wait_minutes"] == 0.0


def test_zrh_security_current_shifts_forward_independently_once_its_own_hour_arrives(session):
    """`now` security'nin 08:00 penceresine girince, security current'ı 08:00'a geçer - passport'unki (06:00, artık geçmiş) KENDİ en yakın geçmiş penceresi olarak kalır, birbirini etkilemezler."""
    add_row(
        session, PROCESS_PASSPORT_DEPARTURE,
        window_start=datetime(2026, 9, 15, 6, 0), window_end=datetime(2026, 9, 15, 7, 0),
        risk=RISK_CRITICAL, wait=66.8,
    )
    add_row(
        session, PROCESS_SECURITY_INTL,
        window_start=datetime(2026, 9, 15, 8, 0), window_end=datetime(2026, 9, 15, 9, 0),
        risk=RISK_LOW, wait=0.0,
    )

    api = airport_predictions(session, "ZRH", now=datetime(2026, 9, 15, 8, 30))
    intl_dep = api["international_departure"]

    assert intl_dep["passport"]["current"]["window_start"] == datetime(2026, 9, 15, 6, 0).isoformat()
    assert intl_dep["security"]["current"]["window_start"] == datetime(2026, 9, 15, 8, 0).isoformat()


# ========================================================================
# Phase 10/11 - kaynak izolasyonu: passport SADECE passport_dep, security
# SADECE security_intl'den beslenir (legacy/diğer süreçler HİÇ girmez).
# ========================================================================

def test_passport_graph_never_reads_legacy_or_security_rows(session):
    add_row(
        session, PROCESS_PASSPORT_DEPARTURE,
        window_start=datetime(2026, 9, 15, 6, 0), window_end=datetime(2026, 9, 15, 7, 0),
        risk=RISK_LOW, wait=3.0,
    )
    api = airport_predictions(session, "ZRH", now=datetime(2026, 9, 15, 6, 30))
    assert api["international_departure"]["passport"]["process"] == PROCESS_PASSPORT_DEPARTURE


def test_security_graph_never_reads_legacy_or_passport_rows(session):
    add_row(
        session, PROCESS_SECURITY_INTL,
        window_start=datetime(2026, 9, 15, 8, 0), window_end=datetime(2026, 9, 15, 9, 0),
        risk=RISK_LOW, wait=0.0,
    )
    api = airport_predictions(session, "ZRH", now=datetime(2026, 9, 15, 8, 30))
    assert api["international_departure"]["security"]["process"] == PROCESS_SECURITY_INTL


# ========================================================================
# Phase 14 - LOW risk'te de wait numeric olarak GÖRÜNÜR (gizlenmez).
# ========================================================================

def test_low_risk_wait_visible_on_both_panels(session):
    add_row(
        session, PROCESS_PASSPORT_DEPARTURE,
        window_start=datetime(2026, 9, 15, 6, 0), window_end=datetime(2026, 9, 15, 7, 0),
        risk=RISK_LOW, wait=3.0,
    )
    add_row(
        session, PROCESS_SECURITY_INTL,
        window_start=datetime(2026, 9, 15, 6, 0), window_end=datetime(2026, 9, 15, 7, 0),
        risk=RISK_LOW, wait=0.0,
    )
    api = airport_predictions(session, "ZRH", now=datetime(2026, 9, 15, 6, 30))
    intl_dep = api["international_departure"]
    assert intl_dep["passport"]["current"]["estimated_wait_minutes"] == 3.0
    assert intl_dep["security"]["current"]["estimated_wait_minutes"] == 0.0
