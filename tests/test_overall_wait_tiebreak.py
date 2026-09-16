"""
ADIM 6D-2 H2 - GENEL (overall) wait tie-break düzeltmesi.

Kök sorun (H1 bulgusu): aynı 60dk pencerede birden fazla süreç AYNI
en-yüksek severity'yi taşıdığında (ör. security_intl=CRITICAL VE
passport=CRITICAL), eski `_merge_overall_series._worst_of()` sadece
argüman sırasındaki İLK max'ı tutuyordu - security ilk sırada olduğu
için onun `estimated_wait_minutes=None`'ı overall'a taşınıyor, aynı
saatte passport'un GERÇEK bir wait değeri olmasına rağmen "Hesaplanmıyor"
gösteriliyordu.

Düzeltme SADECE `_worst_of()`'un wait KAYNAĞINI seçme sırasını
değiştirir: önce en yüksek severity, SONRA o severity'deki süreçler
arasında finite wait'i olan tercih edilir. Risk seçimi (en yüksek
severity kazanır) DEĞİŞMEDİ - bu dosya risk matematiğine DOKUNMAZ,
sadece zaten var olan `QueuePrediction` satırlarını (bu testte doğrudan
DB'ye yazılan) doğru şekilde birleştirdiğini doğrular.
"""

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.constants import (
    PROCESS_PASSPORT,
    PROCESS_SECURITY_INTL,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
)
from app.queue.models import QueuePrediction

WINDOW_START = datetime(2026, 9, 15, 8, 0)
WINDOW_END = datetime(2026, 9, 15, 9, 0)
NOW = datetime(2026, 9, 15, 8, 30)


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


def add_row(session, process, risk, estimated_wait_minutes, utilization=None):
    session.add(QueuePrediction(
        airport_iata="IST",
        process=process,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        flight_count=5,
        expected_passengers=500,
        baseline_ratio=1.0,
        utilization=utilization,
        estimated_wait_minutes=estimated_wait_minutes,
        risk=risk,
        reasons=json.dumps([]),
        confidence=0.8,
    ))
    session.commit()


# --------------------------------------------------------------------
# 1) CRITICAL security (wait=null) + CRITICAL passport (wait=18.3)
#    -> overall CRITICAL + 18.3 (aynı en-yüksek severity içinde finite
#    wait'i olan tercih edilir)
# --------------------------------------------------------------------

def test_1_equal_critical_severity_prefers_the_finite_wait(session):
    add_row(session, PROCESS_SECURITY_INTL, RISK_CRITICAL, None)
    add_row(session, PROCESS_PASSPORT, RISK_CRITICAL, 18.3, utilization=1.4)

    result = airport_predictions(session, "IST", now=NOW)

    assert result["overall"]["current"]["risk"] == RISK_CRITICAL
    assert result["overall"]["current"]["risk_label"] == "ÇOK YOĞUN"
    assert result["overall"]["current"]["estimated_wait_minutes"] == 18.3


# --------------------------------------------------------------------
# 2) CRITICAL security (wait=null) + HIGH passport (wait=40)
#    -> overall CRITICAL + null (DÜŞÜK severity'den wait ÖDÜNÇ ALINMAZ)
# --------------------------------------------------------------------

def test_2_lower_severity_wait_is_never_borrowed(session):
    add_row(session, PROCESS_SECURITY_INTL, RISK_CRITICAL, None)
    add_row(session, PROCESS_PASSPORT, RISK_HIGH, 40.0)

    result = airport_predictions(session, "IST", now=NOW)

    assert result["overall"]["current"]["risk"] == RISK_CRITICAL
    assert result["overall"]["current"]["estimated_wait_minutes"] is None


# --------------------------------------------------------------------
# 3) HIGH security (wait=null) + HIGH passport (wait=12)
#    -> overall HIGH + 12
# --------------------------------------------------------------------

def test_3_equal_high_severity_prefers_the_finite_wait(session):
    add_row(session, PROCESS_SECURITY_INTL, RISK_HIGH, None)
    add_row(session, PROCESS_PASSPORT, RISK_HIGH, 12.0)

    result = airport_predictions(session, "IST", now=NOW)

    assert result["overall"]["current"]["risk"] == RISK_HIGH
    assert result["overall"]["current"]["estimated_wait_minutes"] == 12.0


# --------------------------------------------------------------------
# 4) LOW security (wait=null) + LOW passport (wait=null)
#    -> overall LOW + null (hiçbirinde gerçek wait yok - sahte üretilmez)
# --------------------------------------------------------------------

def test_4_no_finite_wait_anywhere_stays_null(session):
    add_row(session, PROCESS_SECURITY_INTL, RISK_LOW, None)
    add_row(session, PROCESS_PASSPORT, RISK_LOW, None)

    result = airport_predictions(session, "IST", now=NOW)

    assert result["overall"]["current"]["risk"] == RISK_LOW
    assert result["overall"]["current"]["estimated_wait_minutes"] is None


# --------------------------------------------------------------------
# 5) current VE series AYNI tie-break kuralını kullanıyor
# --------------------------------------------------------------------

def test_5_current_and_series_window_use_the_same_tiebreak_rule(session):
    add_row(session, PROCESS_SECURITY_INTL, RISK_CRITICAL, None)
    add_row(session, PROCESS_PASSPORT, RISK_CRITICAL, 18.3, utilization=1.4)

    result = airport_predictions(session, "IST", now=NOW)

    series_window = result["overall"]["windows"][0]
    assert series_window["window_start"] == WINDOW_START.isoformat()
    assert series_window["risk"] == RISK_CRITICAL
    assert series_window["estimated_wait_minutes"] == 18.3

    assert series_window["risk"] == result["overall"]["current"]["risk"]
    assert series_window["estimated_wait_minutes"] == result["overall"]["current"]["estimated_wait_minutes"]


# --------------------------------------------------------------------
# Risk matematiği DEĞİŞMEDİ - sadece wait kaynağı seçimi düzeldi.
# --------------------------------------------------------------------

def test_risk_selection_itself_is_unchanged_still_the_max_severity():
    import inspect

    from app.queue import api

    source = inspect.getsource(api._merge_overall_series)
    assert "RISK_ORDER" in source
    # Hâlâ tek bir yerde severity karşılaştırması var - yeni bir
    # skor/ortalama formülü YOK.
    assert "sum(" not in source
    assert "/ len(" not in source
