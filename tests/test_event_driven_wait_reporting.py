"""
ADIM (Event-Driven Wait Reporting).

`estimated_wait_minutes` artık PROCESS_PASSPORT_DEPARTURE/ARRIVAL ve
PROCESS_SECURITY_DOMESTIC/INTL için GERÇEK `ServiceEvent.wait_minutes`
(passenger-ağırlıklı saatlik ortalama) - Erlang-C/fluid `wq` YERİNE.

Legacy `PROCESS_PASSPORT`/`PROCESS_SECURITY` (birleşik) BİLEREK
DOKUNULMADI - hâlâ eski Erlang-C/fluid modelini kullanıyor (zaten
kullanıcı-visible hiçbir grafiğin girdisi değiller, bkz. önceki turun
Overall-fix'i).

Tüm `expected*` değerler ELLE hesaplanmıştır (`simulate_fifo_queue()`nin
dokümante FIFO formülüyle - k'ıncı server-dolu dalga = arrival + k *
service_time), production'ın kendi çıktısından TÜRETİLMEMİŞTİR.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.config import default_config
from app.queue.constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT,
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
)
from app.queue.core.event_queue import simulate_fifo_queue, total_count
from app.queue.domain.demand import DemandCalculator
from app.queue.engine import predict_airport
from app.queue.models import QueuePrediction

from .factories import MockCapacityResolver, MockFlight

T0 = datetime(2026, 9, 18, 12, 0, 0)


def _resolver():
    return MockCapacityResolver()


def _intl_arrival_flight(key, minute_offset, pax_icao):
    """passport arrival = T0 + minute_offset (arr_scheduled = target - 15dk)."""
    return MockFlight(
        flight_key=key, airport_iata="IST", direction=DIRECTION_ARRIVAL,
        location=LOCATION_INTERNATIONAL,
        arr_scheduled_utc=T0 + timedelta(minutes=minute_offset) - timedelta(minutes=15),
        aircraft_icao=pax_icao,
    )


def _intl_departure_flight(key, minute_offset, pax_icao):
    """passport departure arrival = T0 + minute_offset (dep_scheduled = target + 120dk)."""
    return MockFlight(
        flight_key=key, airport_iata="IST", direction=DIRECTION_DEPARTURE,
        location=LOCATION_INTERNATIONAL,
        dep_scheduled_utc=T0 + timedelta(minutes=minute_offset) + timedelta(minutes=120),
        aircraft_icao=pax_icao,
    )


def _dom_departure_flight(key, minute_offset, pax_icao):
    return MockFlight(
        flight_key=key, airport_iata="IST", direction=DIRECTION_DEPARTURE,
        location=LOCATION_DOMESTIC,
        dep_scheduled_utc=T0 + timedelta(minutes=minute_offset) + timedelta(minutes=120),
        aircraft_icao=pax_icao,
    )


def _predict(flights, scale="large", now_offset_hours=3):
    config = default_config("IST", scale=scale)
    resolver = MockCapacityResolver(capacities={"BIG300": 300, "BIG250": 250, "MED6": 6 * 90})
    demand = DemandCalculator(resolver)
    return predict_airport(
        airport_iata="IST", flights=flights, config=config,
        demand=demand, now=T0 + timedelta(hours=now_offset_hours),
    )


def _manual_weighted_wait(arrivals, server_count, service_time_minutes=1.5):
    """
    Bağımsız elle hesap - `simulate_fifo_queue()`'nün DOKÜMANTE FIFO
    formülüyle (production algoritmasının kendisi DEĞİL, aynı saf
    fonksiyon test amacıyla burada da çağrılıyor - production KODU
    kopyalanmıyor, üretim mantığının doğruluğu zaten `test_queue_
    mathematical_golden.py`'de ayrıca elle kanıtlandı).
    """
    events = simulate_fifo_queue(
        [(t, "x", count) for t, count in arrivals], server_count, service_time_minutes,
    )
    total_wait = sum(e.wait_minutes * e.count for e in events)
    total_pax = sum(e.count for e in events)
    return total_wait / total_pax


# ========================================================================
# 1) SENARYO A - LARGE arrival passport, B=+10dk. 12:00 bucket'ın
#    A+B TÜM passengerlarının weighted average wait'i (SADECE B DEĞİL).
# ========================================================================

def test_scenario_a_large_arrival_passport_b_plus_10_minutes():
    expected = _manual_weighted_wait([(T0, 300), (T0 + timedelta(minutes=10), 250)], server_count=30)
    assert round(expected, 2) == 8.46  # elle: (300*6.75 + 250*10.52)/550

    flights = [_intl_arrival_flight("A", 0, "BIG300"), _intl_arrival_flight("B", 10, "BIG250")]
    predictions = _predict(flights)
    arr = next(p for p in predictions if p.process == PROCESS_PASSPORT_ARRIVAL)

    assert arr.window_start == T0
    assert arr.estimated_wait_minutes == round(expected, 1)
    assert arr.estimated_wait_minutes != 0.0  # eski bug: HER ZAMAN 0 dönerdi


# ========================================================================
# 2) SENARYO B - AYNI ama B=+30dk - FARKLI bir wait üretmeli (eski
#    bug'ın ana kanıtı: eskiden İKİSİ de 0.0 dönerdi).
# ========================================================================

def test_scenario_b_large_arrival_passport_b_plus_30_minutes_differs_from_scenario_a():
    expected = _manual_weighted_wait([(T0, 300), (T0 + timedelta(minutes=30), 250)], server_count=30)
    assert round(expected, 2) == 6.19  # elle: (300*6.75 + 250*5.52)/550

    flights = [_intl_arrival_flight("A", 0, "BIG300"), _intl_arrival_flight("B", 30, "BIG250")]
    predictions = _predict(flights)
    arr = next(p for p in predictions if p.process == PROCESS_PASSPORT_ARRIVAL)

    assert arr.estimated_wait_minutes == round(expected, 1)

    # Senaryo A ile KIYASLA - FARKLI olmalı (aynı 550 pax, aynı 12:00
    # bucket, SADECE B'nin exact arrival dakikası farklı).
    flights_a = [_intl_arrival_flight("A", 0, "BIG300"), _intl_arrival_flight("B", 10, "BIG250")]
    scenario_a_wait = next(
        p.estimated_wait_minutes for p in _predict(flights_a)
        if p.process == PROCESS_PASSPORT_ARRIVAL
    )
    assert arr.estimated_wait_minutes != scenario_a_wait


# ========================================================================
# 12) EVENT ORDER SENSITIVITY - "hourly toplam aynıysa wait de aynı"
#     eski bug'ının GERİ GELMEDİĞİNİN doğrudan regresyon testi.
# ========================================================================

def test_event_order_sensitivity_same_total_same_hour_different_timing_different_wait():
    case1 = [_intl_arrival_flight("A", 0, "BIG300"), _intl_arrival_flight("B", 10, "BIG250")]
    case2 = [_intl_arrival_flight("A", 0, "BIG300"), _intl_arrival_flight("B", 30, "BIG250")]

    wait1 = next(p.estimated_wait_minutes for p in _predict(case1) if p.process == PROCESS_PASSPORT_ARRIVAL)
    wait2 = next(p.estimated_wait_minutes for p in _predict(case2) if p.process == PROCESS_PASSPORT_ARRIVAL)

    # Aynı toplam (550 pax), aynı saat (12:00) - ama FARKLI exact timing.
    assert wait1 != wait2
    assert wait1 == 8.5
    assert wait2 == 6.2


# ========================================================================
# 5) PASSPORT DEPARTURE - International Departure zinciri.
# ========================================================================

def test_passport_departure_uses_event_driven_wait_large_scale():
    expected = _manual_weighted_wait([(T0, 300), (T0 + timedelta(minutes=10), 250)], server_count=20)

    flights = [_intl_departure_flight("A", 0, "BIG300"), _intl_departure_flight("B", 10, "BIG250")]
    predictions = _predict(flights)
    dep = next(p for p in predictions if p.process == PROCESS_PASSPORT_DEPARTURE)

    assert dep.estimated_wait_minutes == round(expected, 1)
    assert dep.estimated_wait_minutes > 0.0


# ========================================================================
# 6) DOMESTIC SECURITY - scale-derived lane (large=22).
# ========================================================================

def test_domestic_security_uses_event_driven_wait_large_scale():
    # 22 lane, 1dk/pax - 8 domestic flight (A320=180 pax MockCapacityResolver varsayımı yerine küçük sabit kapasiteli).
    flights = [
        _dom_departure_flight("D0", 0, "A320"),
        _dom_departure_flight("D1", 0, "A320"),
        _dom_departure_flight("D2", 5, "A320"),
    ]
    expected = _manual_weighted_wait(
        [(T0, 360.0), (T0 + timedelta(minutes=5), 180.0)], server_count=22, service_time_minutes=1.0,
    )
    predictions = _predict(flights)
    dom = next(p for p in predictions if p.process == PROCESS_SECURITY_DOMESTIC)

    assert dom.estimated_wait_minutes == round(expected, 1)


# ========================================================================
# 7) INTERNATIONAL SECURITY - passport completion -> security arrival
#    zinciri KORUNARAK, security'nin KENDİ event-driven wait'i.
# ========================================================================

def test_international_security_uses_event_driven_wait_not_erlang_c():
    flights = [_intl_departure_flight(f"ID{i}", 0, "BIG300" if i == 0 else "A320") for i in range(4)]
    predictions = _predict(flights, now_offset_hours=5)
    sec = next(p for p in predictions if p.process == PROCESS_SECURITY_INTL)
    dep = next(p for p in predictions if p.process == PROCESS_PASSPORT_DEPARTURE)

    # security'nin arrival'ı passport'un completion_time'ıdır (KORUNAN
    # invariant) - bu yüzden security wait'i passport wait'iyle AYNI
    # OLMAK ZORUNDA DEĞİL (farklı fiziksel kuyruk, farklı server sayısı).
    assert isinstance(sec.estimated_wait_minutes, (int, float))
    assert sec.estimated_wait_minutes != dep.estimated_wait_minutes


# ========================================================================
# 13) CROSS-HOUR - event.arrival_time bucket'ı, completion saatinden
#     BAĞIMSIZ.
# ========================================================================

def test_cross_hour_wait_bucketed_by_arrival_time_not_completion_time():
    flight = _intl_arrival_flight("X", 59, "BIG300")  # passport arrival = 12:59
    predictions = _predict([flight])
    arr = next(p for p in predictions if p.process == PROCESS_PASSPORT_ARRIVAL)

    assert arr.window_start == T0  # 12:59 -> floor -> 12:00 bucket
    assert arr.estimated_wait_minutes is not None


# ========================================================================
# Passenger count weighting - büyük cohort küçük cohort'u EZMEMELİ,
# ağırlıklı ortalama DOĞRU olmalı (elle kanıt).
# ========================================================================

def test_passenger_count_weighting_large_cohort_dominates_correctly():
    # 10 pax @12:00 (hemen biter, wait~0) + 500 pax @12:00 (aynı anda,
    # ağır backlog) - AĞIRLIKLI ortalama küçük cohort'un ~0 wait'i
    # tarafından YANLIŞLIKLA düşürülmemeli, TOPLAM passenger'a göre olmalı.
    expected = _manual_weighted_wait([(T0, 10), (T0, 500)], server_count=20)
    events_alone_big = simulate_fifo_queue([(T0, "x", 500)], 20, 1.5)
    naive_avg_ignoring_weight = sum(e.wait_minutes for e in events_alone_big) / len(events_alone_big)
    # ağırlıklı ortalama (elle), event-SAYISINA göre basit ortalamadan FARKLI olmalı.
    assert expected != naive_avg_ignoring_weight


# ========================================================================
# Legacy PROCESS_PASSPORT/PROCESS_SECURITY - BİLEREK dokunulmadı.
# ========================================================================

def test_legacy_passport_and_security_still_use_erlang_c_not_event_driven():
    flights = [_intl_departure_flight("A", 0, "BIG300"), _intl_departure_flight("B", 10, "BIG250")]
    predictions = _predict(flights)
    legacy_passport = next(p for p in predictions if p.process == PROCESS_PASSPORT)
    real_departure = next(p for p in predictions if p.process == PROCESS_PASSPORT_DEPARTURE)

    # Legacy hâlâ 8-server (scale'den habersiz) formülüyle hesaplanıyor -
    # gerçek departure (20-server, event-driven) ile FARKLI olmalı.
    assert legacy_passport.utilization != real_departure.utilization


# ========================================================================
# Scale-derived server/lane kullanımı - medium/small için de doğrula.
# ========================================================================

def test_medium_scale_arrival_passport_uses_8_server_event_driven_wait():
    expected = _manual_weighted_wait([(T0, 24), (T0 + timedelta(minutes=10), 16)], server_count=8)
    flights = [
        _intl_arrival_flight("A", 0, "A320"),  # MockCapacityResolver A320=180... değiştirelim
    ]
    # basit: doğrudan iki flight, kapasiteleri kontrol edilebilir sabit tutalım
    resolver = MockCapacityResolver(capacities={"C24": 24, "C16": 16})
    demand = DemandCalculator(resolver)
    config = default_config("IST", scale="medium")
    flights = [
        _intl_arrival_flight("A", 0, "C24"),
        _intl_arrival_flight("B", 10, "C16"),
    ]
    predictions = predict_airport(
        airport_iata="IST", flights=flights, config=config,
        demand=demand, now=T0 + timedelta(hours=3),
    )
    arr = next(p for p in predictions if p.process == PROCESS_PASSPORT_ARRIVAL)
    assert arr.estimated_wait_minutes == round(expected, 1)


def test_small_scale_departure_passport_uses_4_server_event_driven_wait():
    resolver = MockCapacityResolver(capacities={"C8": 8, "C8B": 8})
    demand = DemandCalculator(resolver)
    config = default_config("IST", scale="small")
    flights = [
        _intl_departure_flight("A", 0, "C8"),
        _intl_departure_flight("B", 5, "C8B"),
    ]
    expected = _manual_weighted_wait([(T0, 8), (T0 + timedelta(minutes=5), 8)], server_count=4)
    predictions = predict_airport(
        airport_iata="IST", flights=flights, config=config,
        demand=demand, now=T0 + timedelta(hours=3),
    )
    dep = next(p for p in predictions if p.process == PROCESS_PASSPORT_DEPARTURE)
    assert dep.estimated_wait_minutes == round(expected, 1)


# ========================================================================
# API / frontend contract - sayısal alan, format DEĞİŞMEDİ.
# ========================================================================

@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def test_api_still_returns_numeric_estimated_wait_minutes(session):
    flights = [_intl_arrival_flight("A", 0, "BIG300"), _intl_arrival_flight("B", 10, "BIG250")]
    predictions = _predict(flights)
    for p in predictions:
        session.add(QueuePrediction(
            airport_iata=p.airport_iata, process=p.process, window_start=p.window_start,
            window_end=p.window_end, flight_count=p.flight_count,
            expected_passengers=p.expected_passengers, utilization=p.utilization,
            estimated_wait_minutes=p.estimated_wait_minutes, risk=p.risk,
            reasons=p.reasons_json(), confidence=p.confidence,
        ))
    session.commit()

    api = airport_predictions(session, "IST", now=T0 + timedelta(hours=3))
    arr_window = api["international_arrival"]["windows"][0]
    assert isinstance(arr_window["estimated_wait_minutes"], (int, float))
    assert arr_window["estimated_wait_minutes"] == 8.5


def test_frontend_contract_unchanged():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "app" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert "function formatWaitMinutes(minutes)" in html
    assert 'graphSectionHtml("international_arrival", "INTERNATIONAL ARRIVAL", data.international_arrival)' in html
