"""
ADIM (4-Graph API Contract) - genel-proje.md Bölüm 15/16/17/26/35/46/
50/51 doğrulaması.

İki katman test edilir:
  1) `app/queue/api.py` - DB'ye elle yazılmış `QueuePrediction`
     satırlarıyla, SADECE birleştirme/JSON-şekli mantığı (gerçek
     Erlang-C/event-driven hesap YOK, bkz. test_queue_api.py'nin AYNI
     deseni).
  2) `app/queue/engine.py:predict_airport()` - GERÇEK event-driven
     motor üzerinden, passport cohort breakdown'ın (passport_dep/
     passport_arr) double-count ETMEDİĞİNİ ve conservation'ı sayısal
     olarak kanıtlar.
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
    PROCESS_SECURITY,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
)
from app.queue.domain.demand import DemandCalculator
from app.queue.engine import predict_airport
from app.queue.models import QueuePrediction

from .factories import MockCapacityResolver, arrival, departure


def _intl_departure(hour, minute=0, **kwargs):
    kwargs.setdefault("location", LOCATION_INTERNATIONAL)
    return departure(hour, minute, **kwargs)


def _intl_arrival(hour, minute=0, **kwargs):
    kwargs.setdefault("location", LOCATION_INTERNATIONAL)
    return arrival(hour, minute, **kwargs)


def _demand():
    return DemandCalculator(MockCapacityResolver())


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
    session, airport_iata="IST", process=PROCESS_SECURITY,
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
# Bölüm 15/26 - API artık TAM 4 ana grafik contract'ını taşıyor.
# ========================================================================

def test_api_response_carries_exactly_four_main_graph_keys(session):
    add_prediction(session, process=PROCESS_SECURITY_DOMESTIC)
    result = airport_predictions(session, "IST", now=datetime(2026, 9, 15, 8, 30))

    for key in ("overall", "domestic_security", "international_departure", "international_arrival"):
        assert key in result

    # Geriye dönük uyumlu eski alanlar da KALDIRILMADI.
    for key in ("security", "passport", "international_security", "international_passport"):
        assert key in result


# ========================================================================
# Bölüm 15/51 - International Departure: passport + international
# security breakdown, sahte wait ödünç alma YOK.
# ========================================================================

def test_international_departure_carries_passport_and_security_breakdown(session):
    window_start = datetime(2026, 9, 15, 8, 0)
    window_end = datetime(2026, 9, 15, 9, 0)
    add_prediction(
        session, process=PROCESS_PASSPORT_DEPARTURE,
        window_start=window_start, window_end=window_end,
        risk=RISK_HIGH, estimated_wait_minutes=21.4, expected_passengers=300,
    )
    add_prediction(
        session, process=PROCESS_SECURITY_INTL,
        window_start=window_start, window_end=window_end,
        risk=RISK_CRITICAL, estimated_wait_minutes=None, expected_passengers=280,
    )

    result = airport_predictions(session, "IST", now=datetime(2026, 9, 15, 8, 30))
    intl_dep = result["international_departure"]

    assert intl_dep["process"] == "international_departure"
    window = intl_dep["windows"][0]

    # Breakdown - frontend HİÇBİR hesap yapmadan iki aşamayı da okuyabilir.
    assert window["passport"]["estimated_wait_minutes"] == 21.4
    assert window["passport"]["risk"] == RISK_HIGH
    assert window["international_security"]["risk"] == RISK_CRITICAL
    assert window["international_security"]["estimated_wait_minutes"] is None

    # Üst seviye: EN YÜKSEK severity (CRITICAL) kazanır - ama CRITICAL
    # olan security_intl'in wait'i None; DÜŞÜK severity'deki (HIGH)
    # passport'un 21.4'ü SAHTE ÖDÜNÇ ALINMAZ (Bölüm 15).
    assert window["risk"] == RISK_CRITICAL
    assert window["estimated_wait_minutes"] is None


def test_international_departure_uses_real_wait_when_the_worst_severity_has_one(session):
    """İki süreç de AYNI (en yüksek) severity'deyse ve BİRİ finite wait taşıyorsa, o wait KULLANILIR (uydurma DEĞİL, gerçek satırdan)."""
    window_start = datetime(2026, 9, 15, 8, 0)
    window_end = datetime(2026, 9, 15, 9, 0)
    add_prediction(
        session, process=PROCESS_PASSPORT_DEPARTURE,
        window_start=window_start, window_end=window_end,
        risk=RISK_CRITICAL, estimated_wait_minutes=18.3, expected_passengers=300,
    )
    add_prediction(
        session, process=PROCESS_SECURITY_INTL,
        window_start=window_start, window_end=window_end,
        risk=RISK_CRITICAL, estimated_wait_minutes=None, expected_passengers=280,
    )

    result = airport_predictions(session, "IST", now=datetime(2026, 9, 15, 8, 30))
    window = result["international_departure"]["windows"][0]

    assert window["risk"] == RISK_CRITICAL
    assert window["estimated_wait_minutes"] == 18.3   # security_intl'in null'ı KAZANMADI


def test_international_departure_current_also_carries_breakdown(session):
    window_start = datetime(2026, 9, 15, 8, 0)
    window_end = datetime(2026, 9, 15, 9, 0)
    add_prediction(
        session, process=PROCESS_PASSPORT_DEPARTURE,
        window_start=window_start, window_end=window_end, risk=RISK_LOW,
    )
    add_prediction(
        session, process=PROCESS_SECURITY_INTL,
        window_start=window_start, window_end=window_end, risk=RISK_LOW,
    )

    result = airport_predictions(session, "IST", now=datetime(2026, 9, 15, 8, 30))
    current = result["international_departure"]["current"]

    assert current is not None
    assert "passport" in current and "international_security" in current


# ========================================================================
# Bölüm 15/17 - International Arrival: SADECE arrival-kökenli passport.
# ========================================================================

def test_international_arrival_is_arrival_origin_passport_only(session):
    add_prediction(
        session, process=PROCESS_PASSPORT_ARRIVAL,
        window_start=datetime(2026, 9, 15, 13, 0), window_end=datetime(2026, 9, 15, 14, 0),
        risk=RISK_LOW, expected_passengers=150,
    )
    result = airport_predictions(session, "IST", now=datetime(2026, 9, 15, 13, 30))
    intl_arr = result["international_arrival"]

    assert intl_arr["process"] == PROCESS_PASSPORT_ARRIVAL
    assert intl_arr["windows"][0]["expected_passengers"] == 150
    assert intl_arr["current"] is not None


def test_international_arrival_has_no_security_stage():
    """Bölüm 15 Graph4: Security YOK - sadece process_series şekli (breakdown alt-nesnesi yok)."""
    # Yapısal kanıt: international_arrival tek-aşamalı process_series
    # döner (`process`/`current`/`windows` - domestic_security ile AYNI
    # şekil), international_departure'ın 2-aşamalı breakdown'ı YOK.
    import inspect

    from app.queue import api as api_module
    source = inspect.getsource(api_module.airport_predictions)
    assert '"international_arrival": passport_arrival' in source


# ========================================================================
# Bölüm 35 - overall fiziksel yeni bir queue değil, mevcut sonuçların
# özeti - DEĞİŞMEDİ.
# ========================================================================

def test_overall_still_derives_from_existing_domestic_intl_passport_series(session):
    window_start = datetime(2026, 9, 15, 8, 0)
    window_end = datetime(2026, 9, 15, 9, 0)
    add_prediction(
        session, process=PROCESS_SECURITY_DOMESTIC,
        window_start=window_start, window_end=window_end, risk=RISK_LOW,
    )
    add_prediction(
        session, process=PROCESS_SECURITY_INTL,
        window_start=window_start, window_end=window_end, risk=RISK_HIGH,
    )
    add_prediction(
        session, process=PROCESS_PASSPORT,
        window_start=window_start, window_end=window_end, risk=RISK_CRITICAL,
        estimated_wait_minutes=12.0,
    )

    result = airport_predictions(session, "IST", now=datetime(2026, 9, 15, 8, 30))
    overall_window = result["overall"]["windows"][0]

    assert overall_window["risk"] == RISK_CRITICAL
    assert overall_window["estimated_wait_minutes"] == 12.0
    # overall hâlâ sadece {window_start,window_end,risk,risk_label,
    # estimated_wait_minutes} taşır - international_departure'ın
    # breakdown'ı overall'a SIZMADI (yeni bir fiziksel queue/alan yok).
    assert "passport" not in overall_window
    assert "international_security" not in overall_window


# ========================================================================
# Mimari sınır - api.py hesap motorunu import ETMİYOR (DEĞİŞMEDİ).
# ========================================================================

def test_api_module_still_does_not_import_calculation_engine():
    import ast

    tree = ast.parse(open("app/queue/api.py", encoding="utf-8").read())
    forbidden_modules = {
        "app.queue.engine", "app.queue.core", "app.queue.core.scoring",
        "app.queue.core.event_queue", "app.queue.core.erlang",
        "app.queue.domain", "app.queue.domain.demand", "app.queue.reasons",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module not in forbidden_modules, node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden_modules, alias.name


# ========================================================================
# GERÇEK engine üzerinden - double-count YOK, conservation kanıtı.
# ========================================================================

def test_engine_passport_cohort_split_conserves_total_and_does_not_double_count():
    """
    Aynı anda hem departure hem arrival-kökenli passport talebi olan
    GERÇEK bir senaryo - `passport_dep` + `passport_arr` toplamı,
    birleşik `passport`'un AYNI penceresiyle BİREBİR eşleşmeli (Bölüm 17:
    paylaşılan fiziksel havuz iki kere SAYILMAZ).
    """
    flights = [
        _intl_departure(9, 0, key="D1", number="1", aircraft="A320"),
        _intl_arrival(9, 5, key="A1", number="2", aircraft="A320"),
    ]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    by_process_window = {(p.process, p.window_start): p for p in predictions}

    combined = {
        w: p for (proc, w), p in by_process_window.items() if proc == PROCESS_PASSPORT
    }
    dep = {
        w: p for (proc, w), p in by_process_window.items() if proc == PROCESS_PASSPORT_DEPARTURE
    }
    arr = {
        w: p for (proc, w), p in by_process_window.items() if proc == PROCESS_PASSPORT_ARRIVAL
    }

    assert combined   # en az bir pencere var
    for window_start, combined_row in combined.items():
        dep_demand = dep[window_start].expected_passengers if window_start in dep else 0
        arr_demand = arr[window_start].expected_passengers if window_start in arr else 0
        # CONSERVATION: dep + arr == combined, ne fazla ne eksik.
        assert dep_demand + arr_demand == combined_row.expected_passengers

        # DOUBLE-COUNT YOK: her iki alt-görünüm de AYNI paylaşılan
        # fiziksel kuyruğun utilization/wait/risk'ini taşır - biri
        # diğerinden DAHA DÜŞÜK bir kapasiteyle hesaplanmış SAHTE bir
        # sonuç üretmiyor.
        if window_start in dep:
            assert dep[window_start].utilization == combined_row.utilization
            assert dep[window_start].estimated_wait_minutes == combined_row.estimated_wait_minutes
            assert dep[window_start].risk == combined_row.risk
        if window_start in arr:
            assert arr[window_start].utilization == combined_row.utilization
            assert arr[window_start].estimated_wait_minutes == combined_row.estimated_wait_minutes
            assert arr[window_start].risk == combined_row.risk


def test_engine_passport_departure_only_flight_produces_no_arrival_row():
    """Sadece departure-kökenli talep varsa `passport_arr` için satır HİÇ üretilmez (sahte sıfır-yolcu satırı YOK)."""
    flights = [_intl_departure(9, 0, key="D1", number="1", aircraft="A320")]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    assert any(p.process == PROCESS_PASSPORT_DEPARTURE for p in predictions)
    assert not any(p.process == PROCESS_PASSPORT_ARRIVAL for p in predictions)


def test_engine_international_security_lane_count_used_in_departure_breakdown():
    """
    `international_security_lane_count` (önceki ADIM'da bağlandı) gerçekten
    `PROCESS_SECURITY_INTL`'in (International Departure'ın 2. aşaması)
    utilization'ına yansıyor - farklı lane sayısı farklı sonuç üretir.
    """
    from app.queue.config import AirportConfigView

    flights = [
        _intl_departure(9, 0, key=f"D{i}", number=str(i), aircraft="E190")
        for i in range(6)
    ]

    def _config(lanes):
        base = default_config("AAA")
        return AirportConfigView(
            airport_iata="AAA",
            passport_counter_count=base.passport_counter_count,
            passport_staff_count=base.passport_staff_count,
            passport_service_time_minutes=base.passport_service_time_minutes,
            security_lane_count=base.security_lane_count,
            domestic_security_lane_count=base.domestic_security_lane_count,
            international_security_lane_count=lanes,
            security_service_time_minutes=base.security_service_time_minutes,
            passport_staff_per_counter=base.passport_staff_per_counter,
            passport_service_rate_per_staff=base.passport_service_rate_per_staff,
            passport_efficiency_multiplier=base.passport_efficiency_multiplier,
            arrival_bank_threshold=base.arrival_bank_threshold,
            is_default=False,
        )

    narrow = predict_airport("AAA", flights, _config(4), _demand())
    wide = predict_airport("AAA", flights, _config(20), _demand())

    narrow_intl = next(p for p in narrow if p.process == PROCESS_SECURITY_INTL)
    wide_intl = next(p for p in wide if p.process == PROCESS_SECURITY_INTL)
    assert narrow_intl.utilization > wide_intl.utilization
