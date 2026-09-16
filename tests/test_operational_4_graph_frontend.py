"""
ADIM 6D-2 G - 4 SAATLİK OPERASYONEL GRAFİK (frontend'in dayandığı API
sözleşmesi). Frontend'in kendisi (index.html) pytest ile çalıştırılamaz;
burada doğrulanan, frontend'in üzerine inşa edildiği GERÇEK veri
kaynağıdır: `airport_predictions()`'ın döndürdüğü `domestic_security`,
`international_security`, `international_passport`, `overall` alanları.

Hiçbir test burada yeni bir risk/wait FORMÜLÜ doğrulamaz - sadece
zaten üretilmiş `QueuePrediction` satırlarının doğru sürece/grafiğe
yönlendirildiğini ve hiçbir sahte veri (fabricated wait, domestic
passport, 15/30/45 dk pencere) üretilmediğini kontrol eder.
"""

import json
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.constants import (
    DEMAND_WINDOW_MINUTES,
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
)
from app.queue.models import QueuePrediction

INDEX_HTML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "web", "static", "index.html",
)


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


def add_prediction(
    session,
    airport_iata="IST",
    process=PROCESS_SECURITY,
    window_start=datetime(2026, 9, 15, 8, 0),
    flight_count=8,
    expected_passengers=900,
    utilization=None,
    estimated_wait_minutes=None,
    risk=RISK_LOW,
):
    window_end = window_start + timedelta(minutes=DEMAND_WINDOW_MINUTES)
    row = QueuePrediction(
        airport_iata=airport_iata,
        process=process,
        window_start=window_start,
        window_end=window_end,
        flight_count=flight_count,
        expected_passengers=expected_passengers,
        baseline_ratio=1.0,
        utilization=utilization,
        estimated_wait_minutes=estimated_wait_minutes,
        risk=risk,
        reasons=json.dumps([]),
        confidence=0.8,
    )
    session.add(row)
    session.commit()
    return row


NOW = datetime(2026, 9, 15, 8, 30)


def test_security_dom_feeds_domestic_security_graph(session):
    add_prediction(session, process=PROCESS_SECURITY_DOMESTIC, risk=RISK_HIGH)
    result = airport_predictions(session, "IST", now=NOW)
    assert result["domestic_security"]["process"] == PROCESS_SECURITY_DOMESTIC
    assert result["domestic_security"]["current"]["risk"] == RISK_HIGH


def test_security_intl_feeds_international_security_graph(session):
    add_prediction(session, process=PROCESS_SECURITY_INTL, risk=RISK_MEDIUM)
    result = airport_predictions(session, "IST", now=NOW)
    assert result["international_security"]["process"] == PROCESS_SECURITY_INTL
    assert result["international_security"]["current"]["risk"] == RISK_MEDIUM


def test_passport_feeds_international_passport_graph(session):
    add_prediction(session, process=PROCESS_PASSPORT, risk=RISK_LOW, estimated_wait_minutes=4.0)
    result = airport_predictions(session, "IST", now=NOW)
    assert result["international_passport"]["process"] == PROCESS_PASSPORT
    assert result["international_passport"] == result["passport"]


def test_no_domestic_passport_process_exists(session):
    add_prediction(session, process=PROCESS_PASSPORT, risk=RISK_LOW)
    result = airport_predictions(session, "IST", now=NOW)
    assert "domestic_passport" not in result
    for key in ("domestic_security", "international_security", "international_passport", "overall", "security", "passport"):
        assert key in result


def test_windows_are_real_60_minute_windows_not_15_30_45(session):
    add_prediction(session, process=PROCESS_SECURITY_DOMESTIC)
    add_prediction(session, process=PROCESS_SECURITY_INTL)
    add_prediction(session, process=PROCESS_PASSPORT)
    result = airport_predictions(session, "IST", now=NOW)
    for key in ("domestic_security", "international_security", "international_passport", "overall"):
        for w in result[key]["windows"]:
            start = datetime.fromisoformat(w["window_start"])
            end = datetime.fromisoformat(w["window_end"])
            assert start.minute == 0
            assert (end - start).total_seconds() / 60 == DEMAND_WINDOW_MINUTES


def test_overall_picks_highest_real_risk_at_same_hour(session):
    add_prediction(session, process=PROCESS_SECURITY_DOMESTIC, risk=RISK_LOW)
    add_prediction(session, process=PROCESS_SECURITY_INTL, risk=RISK_CRITICAL)
    add_prediction(session, process=PROCESS_PASSPORT, risk=RISK_MEDIUM)
    result = airport_predictions(session, "IST", now=NOW)
    assert result["overall"]["current"]["risk"] == RISK_CRITICAL


def test_overall_window_carries_real_estimated_wait_not_average(session):
    add_prediction(session, process=PROCESS_SECURITY_DOMESTIC, risk=RISK_LOW)
    add_prediction(
        session, process=PROCESS_PASSPORT, risk=RISK_HIGH, estimated_wait_minutes=17.0
    )
    result = airport_predictions(session, "IST", now=NOW)
    assert result["overall"]["current"]["risk"] == RISK_HIGH
    assert result["overall"]["current"]["estimated_wait_minutes"] == 17.0


def test_security_processes_never_carry_a_fabricated_wait(session):
    add_prediction(session, process=PROCESS_SECURITY_DOMESTIC, risk=RISK_CRITICAL)
    add_prediction(session, process=PROCESS_SECURITY_INTL, risk=RISK_CRITICAL)
    result = airport_predictions(session, "IST", now=NOW)
    for key in ("domestic_security", "international_security"):
        for w in result[key]["windows"]:
            assert w["estimated_wait_minutes"] is None


def test_passport_window_wait_matches_its_own_row(session):
    add_prediction(
        session,
        process=PROCESS_PASSPORT,
        window_start=datetime(2026, 9, 15, 7, 0),
        estimated_wait_minutes=3.0,
    )
    add_prediction(
        session,
        process=PROCESS_PASSPORT,
        window_start=datetime(2026, 9, 15, 8, 0),
        estimated_wait_minutes=21.0,
    )
    result = airport_predictions(session, "IST", now=NOW)
    windows = result["international_passport"]["windows"]
    by_start = {w["window_start"]: w["estimated_wait_minutes"] for w in windows}
    assert by_start[datetime(2026, 9, 15, 7, 0).isoformat()] == 3.0
    assert by_start[datetime(2026, 9, 15, 8, 0).isoformat()] == 21.0


def test_airports_do_not_mix_across_split_processes(session):
    add_prediction(session, airport_iata="IST", process=PROCESS_SECURITY_DOMESTIC, risk=RISK_CRITICAL)
    add_prediction(session, airport_iata="ADB", process=PROCESS_SECURITY_DOMESTIC, risk=RISK_LOW)
    ist = airport_predictions(session, "IST", now=NOW)
    adb = airport_predictions(session, "ADB", now=NOW)
    assert ist["domestic_security"]["current"]["risk"] == RISK_CRITICAL
    assert adb["domestic_security"]["current"]["risk"] == RISK_LOW


def test_combined_security_process_still_present_backward_compatible(session):
    add_prediction(session, process=PROCESS_SECURITY, risk=RISK_MEDIUM)
    result = airport_predictions(session, "IST", now=NOW)
    assert result["security"]["current"]["risk"] == RISK_MEDIUM


def test_api_module_still_does_not_import_calculation_engine():
    import inspect

    from app.queue import api

    source = inspect.getsource(api)
    forbidden = ["core.erlang", "core.scoring", "domain.demand", "domain.flight_rules", "domain.flows", "reasons.detector", "import engine", "from . import engine", "from .engine"]
    for token in forbidden:
        assert token not in source


def test_frontend_never_renders_passenger_or_flight_counts_in_graph_blocks():
    with open(INDEX_HTML, "r", encoding="utf-8") as fh:
        html = fh.read()
    start = html.index("function graphSectionHtml")
    end = html.index("\n  }", start)
    block = html[start:end]
    assert "expected_passengers" not in block
    assert "flight_count" not in block
