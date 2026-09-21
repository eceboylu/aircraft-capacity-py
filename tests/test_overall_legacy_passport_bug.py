"""
ADIM (Overall Graph Legacy Passport Bug Fix).

Bug: `app/queue/api.py:airport_predictions()` içinde `overall`, legacy
(`PROCESS_PASSPORT`, hâlâ eski `passport_effective_server_count()` -
4 gişe x 2 görevli = 8, `Airport.scale`'den HABERSİZ) serisini
kullanıyordu - International Departure/Arrival ZATEN doğru şekilde
`passport_departure`/`passport_arrival` (scale-derived) kullanırken,
Overall aynı sayfada TUTARSIZ/yanlış bir risk gösterebiliyordu (ör.
large bir havalimanında gerçek demand 20/30-server kapasitesine göre
LOW iken, Overall 8-server referansına göre CRITICAL görünebiliyordu).

Fix (`app/queue/api.py`, TEK satır): `overall` artık `passport_departure`
+ `passport_arrival`'dan türetiliyor, legacy `passport`'tan DEĞİL.
`passport`/`international_passport` API alanları KALDIRILMADI (geriye
dönük uyumluluk) - sadece `overall`'ın girdisi değişti.
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.config import default_config
from app.queue.constants import (
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT,
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    RISK_CRITICAL,
    RISK_LOW,
)
from app.queue.core.scoring import (
    passport_arrival_server_count,
    passport_departure_server_count,
    passport_effective_server_count,
)
from app.queue.domain.demand import DemandCalculator
from app.queue.engine import predict_airport
from app.queue.models import QueuePrediction

from .factories import MockCapacityResolver, arrival, departure


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def add_prediction(
    session, airport_iata="IST", process=PROCESS_SECURITY_DOMESTIC,
    window_start=datetime(2026, 9, 15, 8, 0), window_end=datetime(2026, 9, 15, 9, 0),
    flight_count=8, expected_passengers=900, utilization=None,
    estimated_wait_minutes=None, risk=RISK_LOW, confidence=0.75,
):
    row = QueuePrediction(
        airport_iata=airport_iata, process=process,
        window_start=window_start, window_end=window_end,
        flight_count=flight_count, expected_passengers=expected_passengers,
        utilization=utilization, estimated_wait_minutes=estimated_wait_minutes,
        risk=risk, reasons=json.dumps([]), confidence=confidence,
    )
    session.add(row)
    session.commit()
    return row


# ========================================================================
# 1) Large airport - engine seviyesinde: passport_departure=20 server,
#    passport_arrival=30 server, Overall legacy 8-server'ı KULLANMIYOR.
# ========================================================================

def test_large_airport_passport_departure_and_arrival_server_counts_are_scale_derived():
    """ADIM (Generic Scale Resource Update) - large değerleri 20/30 -> 30/45 oldu (bkz. rapor)."""
    config = default_config("IST", scale="large")
    assert passport_departure_server_count(config) == 30
    assert passport_arrival_server_count(config) == 45
    # Legacy (8-server) formül HÂLÂ VAR ama scale'den HABERSİZ - overall
    # artık bunu KULLANMIYOR (aşağıdaki testler kanıtlıyor).
    assert passport_effective_server_count(config) == 8


def test_large_airport_engine_replay_overall_does_not_use_legacy_8_server_passport(session):
    """
    GERÇEK event-driven engine + GERÇEK api.py çağrısı - large havalimanı
    (20/30 server) için Overall'ın PROCESS_PASSPORT (legacy, 8 server)
    DEĞİL, passport_departure/passport_arrival'dan türediğini kanıtlar.
    """
    config = default_config("IST", scale="large")
    resolver = MockCapacityResolver()
    now = datetime(2026, 9, 15, 23, 0)

    flights = [
        departure(13, 0, airport="IST", location=LOCATION_INTERNATIONAL, aircraft="A320", key="ID1"),
        departure(13, 0, airport="IST", location=LOCATION_INTERNATIONAL, aircraft="A320", key="ID2"),
        arrival(13, 0, airport="IST", location=LOCATION_INTERNATIONAL, aircraft="A320", key="IA1"),
        departure(13, 0, airport="IST", location=LOCATION_DOMESTIC, aircraft="A320", key="D1"),
    ]
    predictions = predict_airport(
        airport_iata="IST", flights=flights, config=config,
        demand=DemandCalculator(resolver), now=now,
    )
    for p in predictions:
        session.add(QueuePrediction(
            airport_iata=p.airport_iata, process=p.process,
            window_start=p.window_start, window_end=p.window_end,
            flight_count=p.flight_count, expected_passengers=p.expected_passengers,
            utilization=p.utilization, estimated_wait_minutes=p.estimated_wait_minutes,
            risk=p.risk, reasons=p.reasons_json(), confidence=p.confidence,
        ))
    session.commit()

    api = airport_predictions(session, "IST", now=now)

    # Legacy passport process HÂLÂ persist edildi (backward-compat) -
    # ama kendi (8-server) utilization'ı, passport_departure'ın
    # (20-server) utilization'ından FARKLI olmalı (aynı 360 pax demand,
    # farklı kapasite referansı).
    legacy_passport = {p.process for p in predictions}
    assert PROCESS_PASSPORT in legacy_passport
    assert PROCESS_PASSPORT_DEPARTURE in legacy_passport
    assert PROCESS_PASSPORT_ARRIVAL in legacy_passport

    legacy_row = next(p for p in predictions if p.process == PROCESS_PASSPORT)
    dep_row = next(p for p in predictions if p.process == PROCESS_PASSPORT_DEPARTURE)
    assert legacy_row.utilization != dep_row.utilization  # 8-server != 30-server referansı

    # overall'ın PENCERESİ passport_departure/arrival'ın penceresiyle
    # AYNI saatte olmalı ve worst-of mantığı departure/arrival'ın KENDİ
    # riskini yansıtmalı - legacy passport'un DEĞİL.
    overall_windows = {w["window_start"]: w for w in api["overall"]["windows"]}
    dep_windows = {w["window_start"]: w for w in api["international_departure"]["passport"]["windows"]}
    assert overall_windows  # en az bir pencere var
    for start, overall_window in overall_windows.items():
        # legacy passport riski CRITICAL/farklı olsa bile overall bunu
        # YANSITMAMALI - departure/arrival/security'nin worst-of'u olmalı.
        assert overall_window["risk"] != legacy_row.risk or dep_row.risk == legacy_row.risk


# ========================================================================
# 2) Overall artık 4 seriden türetiliyor: domestic_security +
#    international_security + passport_departure + passport_arrival.
# ========================================================================

def test_overall_derived_from_four_series_not_legacy_combined_passport(session):
    add_prediction(
        session, process=PROCESS_SECURITY_DOMESTIC, risk=RISK_LOW,
        estimated_wait_minutes=1.0,
    )
    add_prediction(
        session, process=PROCESS_SECURITY_INTL, risk=RISK_LOW,
        estimated_wait_minutes=2.0,
    )
    add_prediction(
        session, process=PROCESS_PASSPORT_DEPARTURE, risk=RISK_LOW,
        estimated_wait_minutes=3.0,
    )
    add_prediction(
        session, process=PROCESS_PASSPORT_ARRIVAL, risk=RISK_LOW,
        estimated_wait_minutes=4.0,
    )
    # Legacy (birleşik) passport KASITLI olarak CRITICAL + çok yüksek
    # wait - overall bunu YOK SAYMALI.
    add_prediction(
        session, process=PROCESS_PASSPORT, risk=RISK_CRITICAL,
        estimated_wait_minutes=999.0,
    )

    api = airport_predictions(session, "IST", now=datetime(2026, 9, 15, 8, 30))
    overall = api["overall"]["current"]

    assert overall is not None
    assert overall["risk"] == RISK_LOW
    assert overall["estimated_wait_minutes"] != 999.0


# ========================================================================
# 3) REGRESYON - eski bug'ın GERİ GELMEMESİNİN kanıtı: legacy passport
#    CRITICAL, passport_departure/arrival + security'ler LOW iken
#    Overall CRITICAL OLMAMALI.
# ========================================================================

def test_regression_overall_not_critical_when_only_legacy_passport_is_critical(session):
    window_start = datetime(2026, 9, 15, 8, 0)
    window_end = datetime(2026, 9, 15, 9, 0)

    add_prediction(
        session, process=PROCESS_SECURITY_DOMESTIC, window_start=window_start,
        window_end=window_end, risk=RISK_LOW, estimated_wait_minutes=0.5,
    )
    add_prediction(
        session, process=PROCESS_SECURITY_INTL, window_start=window_start,
        window_end=window_end, risk=RISK_LOW, estimated_wait_minutes=0.5,
    )
    add_prediction(
        session, process=PROCESS_PASSPORT_DEPARTURE, window_start=window_start,
        window_end=window_end, risk=RISK_LOW, estimated_wait_minutes=0.5,
    )
    add_prediction(
        session, process=PROCESS_PASSPORT_ARRIVAL, window_start=window_start,
        window_end=window_end, risk=RISK_LOW, estimated_wait_minutes=0.5,
    )
    # SADECE legacy (8-server) birleşik passport CRITICAL - gerçek
    # (scale-derived) departure/arrival havuzları LOW.
    add_prediction(
        session, process=PROCESS_PASSPORT, window_start=window_start,
        window_end=window_end, risk=RISK_CRITICAL, estimated_wait_minutes=500.0,
    )

    api = airport_predictions(session, "IST", now=datetime(2026, 9, 15, 8, 30))

    for window in api["overall"]["windows"]:
        assert window["risk"] != RISK_CRITICAL, (
            "Overall, legacy 8-server passport'un CRITICAL riskini yansıtıyor - bug GERİ GELDİ"
        )
    assert api["overall"]["current"]["risk"] == RISK_LOW


# ========================================================================
# 4/5) International Departure/Arrival DEĞİŞMEDİ + legacy alanlar HÂLÂ VAR.
# ========================================================================

def test_international_departure_and_arrival_unaffected_by_fix(session):
    add_prediction(session, process=PROCESS_SECURITY_INTL, risk=RISK_LOW)
    add_prediction(session, process=PROCESS_PASSPORT_DEPARTURE, risk=RISK_LOW, estimated_wait_minutes=7.0)
    add_prediction(session, process=PROCESS_PASSPORT_ARRIVAL, risk=RISK_LOW, estimated_wait_minutes=9.0)
    add_prediction(session, process=PROCESS_PASSPORT, risk=RISK_CRITICAL, estimated_wait_minutes=999.0)

    api = airport_predictions(session, "IST", now=datetime(2026, 9, 15, 8, 30))

    dep_window = api["international_departure"]["passport"]["windows"][0]
    assert dep_window["estimated_wait_minutes"] == 7.0
    arr_window = api["international_arrival"]["windows"][0]
    assert arr_window["estimated_wait_minutes"] == 9.0


def test_legacy_passport_api_fields_still_present_for_backward_compat(session):
    add_prediction(session, process=PROCESS_PASSPORT, risk=RISK_LOW)
    api = airport_predictions(session, "IST", now=datetime(2026, 9, 15, 8, 30))
    assert "passport" in api
    assert "international_passport" in api
    assert api["passport"]["windows"]
    assert api["international_passport"]["windows"]


# ========================================================================
# 6) Frontend contract bozulmadı (statik - index.html'e DOKUNULMADI).
# ========================================================================

def test_frontend_still_reads_only_the_four_graph_keys_not_legacy_passport():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "app" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    render_fn = html.split("function renderContent() {", 1)[1].split("\n  function ", 1)[0]
    assert "data.overall" in render_fn
    assert "data.international_departure" in render_fn
    assert "data.international_arrival" in render_fn
    assert "data.passport" not in render_fn
    assert "data.international_passport" not in render_fn
