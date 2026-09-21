"""
ADIM 6D-1 — LEGACY QUEUE ISOLATION.

Legacy `PROCESS_PASSPORT`/`PROCESS_SECURITY` (birleşik) satırları
geriye dönük uyumluluk için DB/API'de kalabilir (bkz. `engine.py`,
`api.py`), ama hiçbir kullanıcı-visible sonucu (wait/risk/current/
overall/graph/summary/frontend'in okuduğu API alanı) ETKİLEMEMELİ.

Bu dosya, SENTINEL değerlerle (legacy = risk CRITICAL + wait 9999/8888
dk, gerçek = LOW + makul dakika) `app/queue/api.py`'nin okuma/birleştirme
katmanını (`_merge_overall_series`, `_international_departure_series`,
`process_series`) DOĞRUDAN `QueuePrediction` satırları üzerinden test
eder - engine'in bu değerleri NASIL ürettiğinden bağımsız, sadece API
katmanının doğru KAYNAKTAN okuduğunu kanıtlar (motor tarafı zaten
`tests/test_overall_legacy_passport_bug.py`,
`tests/test_passport_pool_separation.py`,
`tests/test_passport_security_coupling.py`,
`tests/test_event_driven_wait_reporting.py` ile ayrıca kanıtlanmış).
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
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    RISK_CRITICAL,
    RISK_LOW,
)
from app.queue.models import QueuePrediction

WINDOW_START = datetime(2026, 9, 15, 8, 0)
WINDOW_END = datetime(2026, 9, 15, 9, 0)
NOW = datetime(2026, 9, 15, 8, 30)

LEGACY_PASSPORT_WAIT = 9999.0
LEGACY_SECURITY_WAIT = 8888.0
LEGACY_LOW_600_WAIT = 603.0

REAL_LOW_WAIT = 4.2


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def add_row(
    session, process, *, airport="CDG", risk=RISK_LOW, wait=REAL_LOW_WAIT,
    utilization=0.3, window_start=WINDOW_START, window_end=WINDOW_END,
    flight_count=5, expected_passengers=500, confidence=0.8,
):
    row = QueuePrediction(
        airport_iata=airport, process=process,
        window_start=window_start, window_end=window_end,
        flight_count=flight_count, expected_passengers=expected_passengers,
        utilization=utilization, estimated_wait_minutes=wait,
        risk=risk, reasons=json.dumps([]), confidence=confidence,
    )
    session.add(row)
    session.commit()
    return row


def _sentinel_state(session, airport="CDG"):
    """
    Kontrollü fixture (Bölüm 12): legacy satırlar SENTINEL (görmezse
    hemen fark edilecek) değerlerle; gerçek dört süreç makul/LOW.
    """
    add_row(session, PROCESS_PASSPORT, airport=airport,
            risk=RISK_CRITICAL, wait=LEGACY_PASSPORT_WAIT, utilization=50.0)
    add_row(session, PROCESS_SECURITY, airport=airport,
            risk=RISK_CRITICAL, wait=LEGACY_SECURITY_WAIT, utilization=40.0)

    add_row(session, PROCESS_PASSPORT_DEPARTURE, airport=airport,
            risk=RISK_LOW, wait=REAL_LOW_WAIT, utilization=0.31)
    add_row(session, PROCESS_PASSPORT_ARRIVAL, airport=airport,
            risk=RISK_LOW, wait=REAL_LOW_WAIT, utilization=0.28)
    add_row(session, PROCESS_SECURITY_DOMESTIC, airport=airport,
            risk=RISK_LOW, wait=REAL_LOW_WAIT, utilization=0.25)
    add_row(session, PROCESS_SECURITY_INTL, airport=airport,
            risk=RISK_LOW, wait=REAL_LOW_WAIT, utilization=0.22)


# ------------------------------------------------------------------
# Sentinel regresyon kilidi (Bölüm 12 - "en önemli regresyon kilidi")
# ------------------------------------------------------------------

def _assert_no_sentinel_leak(node) -> None:
    """
    `node`'un altındaki hiçbir sayısal alan (dict/list, herhangi bir
    derinlik) sentinel değerlerden birini İÇERMEMELİ.
    """
    forbidden = {LEGACY_PASSPORT_WAIT, LEGACY_SECURITY_WAIT, 50.0, 40.0}

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, (int, float)):
            assert value not in forbidden, f"sentinel leaked: {value!r} in {node}"

    walk(node)


def test_sentinel_overall_never_shows_legacy_9999_or_8888(session):
    _sentinel_state(session)
    api = airport_predictions(session, "CDG", now=NOW)

    _assert_no_sentinel_leak(api["overall"])
    assert api["overall"]["current"]["risk"] == RISK_LOW
    assert api["overall"]["current"]["estimated_wait_minutes"] == REAL_LOW_WAIT


def test_sentinel_international_departure_never_shows_legacy_passport(session):
    _sentinel_state(session)
    api = airport_predictions(session, "CDG", now=NOW)

    intl_dep = api["international_departure"]
    _assert_no_sentinel_leak(intl_dep)
    assert intl_dep["passport"]["current"]["risk"] == RISK_LOW
    assert intl_dep["passport"]["current"]["estimated_wait_minutes"] == REAL_LOW_WAIT
    assert intl_dep["security"]["current"]["risk"] == RISK_LOW
    assert intl_dep["security"]["current"]["estimated_wait_minutes"] == REAL_LOW_WAIT


def test_sentinel_international_arrival_never_shows_legacy_passport(session):
    _sentinel_state(session)
    api = airport_predictions(session, "CDG", now=NOW)

    _assert_no_sentinel_leak(api["international_arrival"])
    assert api["international_arrival"]["current"]["risk"] == RISK_LOW
    assert api["international_arrival"]["current"]["estimated_wait_minutes"] == REAL_LOW_WAIT


def test_sentinel_domestic_security_never_shows_legacy_security(session):
    _sentinel_state(session)
    api = airport_predictions(session, "CDG", now=NOW)

    _assert_no_sentinel_leak(api["domestic_security"])
    assert api["domestic_security"]["current"]["risk"] == RISK_LOW
    assert api["domestic_security"]["current"]["estimated_wait_minutes"] == REAL_LOW_WAIT


def test_sentinel_real_process_values_are_preserved_exactly(session):
    """Legacy izolasyonu, GERÇEK süreçlerin kendi doğru değerlerini BOZMAMALI."""
    _sentinel_state(session)
    api = airport_predictions(session, "CDG", now=NOW)

    assert api["international_departure"]["passport"]["current"]["estimated_wait_minutes"] == REAL_LOW_WAIT
    assert api["international_departure"]["security"]["current"]["estimated_wait_minutes"] == REAL_LOW_WAIT
    assert api["international_arrival"]["current"]["estimated_wait_minutes"] == REAL_LOW_WAIT
    assert api["domestic_security"]["current"]["estimated_wait_minutes"] == REAL_LOW_WAIT


def test_sentinel_current_selection_ignores_legacy(session):
    """`_pick_current`/`_pick_current_window` legacy pencerelerden ETKİLENMEZ."""
    _sentinel_state(session)
    api = airport_predictions(session, "CDG", now=NOW)

    for surface in ("overall", "domestic_security", "international_arrival"):
        current = api[surface]["current"]
        assert current is not None
        _assert_no_sentinel_leak(current)

    for stage in ("passport", "security"):
        current = api["international_departure"][stage]["current"]
        assert current is not None
        _assert_no_sentinel_leak(current)


# ------------------------------------------------------------------
# Bölüm 18 - LOW risk + 600+ dk legacy wait (backlog senaryosu)
# ------------------------------------------------------------------

def test_sentinel_legacy_low_risk_with_603_minutes_never_surfaces(session):
    """
    Legacy'nin backlog-tabanlı modeli LOW risk + YÜKSEK wait üretebilir
    (rho<1 ama önceki pencereden kalan backlog hâlâ boşalıyor - bkz.
    ADIM 6D). Risk LOW olduğu için severity-tabanlı worst-of bunu hiç
    ELEMEZ - sadece SÜREÇ İZOLASYONU (legacy hiçbir merge'e girmez)
    koruma sağlar. Bu test spesifik olarak BUNU doğrular.
    """
    add_row(session, PROCESS_PASSPORT, risk=RISK_LOW, wait=LEGACY_LOW_600_WAIT, utilization=0.5)
    add_row(session, PROCESS_SECURITY, risk=RISK_LOW, wait=LEGACY_LOW_600_WAIT, utilization=0.5)
    add_row(session, PROCESS_PASSPORT_DEPARTURE, risk=RISK_LOW, wait=3.0, utilization=0.2)
    add_row(session, PROCESS_PASSPORT_ARRIVAL, risk=RISK_LOW, wait=2.0, utilization=0.2)
    add_row(session, PROCESS_SECURITY_DOMESTIC, risk=RISK_LOW, wait=1.5, utilization=0.2)
    add_row(session, PROCESS_SECURITY_INTL, risk=RISK_LOW, wait=1.0, utilization=0.2)

    api = airport_predictions(session, "CDG", now=NOW)

    currents = [
        api["overall"]["current"],
        api["domestic_security"]["current"],
        api["international_arrival"]["current"],
        api["international_departure"]["passport"]["current"],
        api["international_departure"]["security"]["current"],
    ]
    for current in currents:
        assert current["estimated_wait_minutes"] != LEGACY_LOW_600_WAIT
        walk_values = json.dumps(current, default=str)
        assert "603.0" not in walk_values


# ------------------------------------------------------------------
# Bölüm 10/14 - legacy raw API alanları KALABİLİR (backward-compat)
# ------------------------------------------------------------------

def test_legacy_raw_api_fields_still_present_for_backward_compatibility(session):
    _sentinel_state(session)
    api = airport_predictions(session, "CDG", now=NOW)

    assert api["passport"]["current"]["estimated_wait_minutes"] == LEGACY_PASSPORT_WAIT
    assert api["security"]["current"]["estimated_wait_minutes"] == LEGACY_SECURITY_WAIT
    assert api["international_passport"]["current"]["estimated_wait_minutes"] == LEGACY_PASSPORT_WAIT


def test_api_contract_fields_present(session):
    """Bölüm 14 - dört gerçek grafik alanı + overall her zaman mevcut."""
    _sentinel_state(session)
    api = airport_predictions(session, "CDG", now=NOW)

    for key in ("overall", "domestic_security", "international_arrival"):
        assert key in api
        assert "current" in api[key]
        assert "windows" in api[key]

    assert "international_departure" in api
    for stage in ("passport", "security"):
        assert stage in api["international_departure"]
        assert "current" in api["international_departure"][stage]
        assert "windows" in api["international_departure"][stage]


# ------------------------------------------------------------------
# Bölüm 13 - frontend static dosyada aktif render path legacy alan okumuyor
# ------------------------------------------------------------------

def test_frontend_does_not_read_legacy_fields_in_active_render_path():
    import os
    path = os.path.join(
        os.path.dirname(__file__), "..", "app", "web", "static", "index.html",
    )
    with open(path, encoding="utf-8") as fh:
        html = fh.read()

    # graphSectionHtml/drawChart çağrıları (aktif render path) SADECE
    # gerçek dört yüzeyi + breakdown'ı kullanmalı.
    assert 'data.overall' in html
    assert 'data.domestic_security' in html
    assert 'data.international_departure' in html
    assert 'data.international_arrival' in html

    # Legacy top-level alanlar (`data.passport`, `data.security`,
    # `data.international_passport`) aktif render path'te HİÇ
    # referans edilmemeli.
    for forbidden in ("data.passport", "data.security", "data.international_passport"):
        assert forbidden not in html, f"{forbidden} frontend'de kullanılıyor"
