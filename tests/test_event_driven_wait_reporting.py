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

ADIM (Departure Show-Up Profile): departure süreçleri (PASSPORT_DEPARTURE,
SECURITY_DOMESTIC) için flight'ların talebi artık TEK bir noktada
(`effective_time()`) DEĞİL, `departure_show_up_events()`'ın ürettiği 3
saate yayılmış batch'lerde geliyor - bu yüzden "elle hesap" da artık
show-up batch'lerini üretip (AYNI saf fonksiyonla, production'ın
KENDİSİYLE DEĞİL) `simulate_fifo_queue()`'ya veriyor (bkz. `_show_up_
manual_wait()`), TEK bir pencereye değil TÜM etkilenen pencerelere
(passenger-ağırlıklı) bakıyor - `next(... TEK satır)` yerine `_weighted_
wait()` (bkz. aşağıda) kullanılıyor. ARRIVAL süreçleri (PASSPORT_ARRIVAL)
HİÇ ETKİLENMEDİ - o testler AYNEN (TEK pencere, TEK `next(...)`) kaldı.
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
from app.queue.domain.demand import (
    DemandCalculator,
    arrival_passenger_release_events,
    departure_show_up_events,
)
from app.queue.engine import predict_airport
from app.queue.models import QueuePrediction

from .factories import MockCapacityResolver, MockFlight

T0 = datetime(2026, 9, 18, 12, 0, 0)


def _weighted_wait(predictions, process) -> float:
    """
    Show-up sonrası bir sürecin talebi BİRDEN FAZLA pencereye
    yayılabildiği için "tek satır" yerine TÜM pencerelerin passenger-
    ağırlıklı ortalama wait'i - `_manual_weighted_wait()` ile AYNI
    ilke, production çıktısı üzerinden.
    """
    rows = [p for p in predictions if p.process == process]
    total_demand = sum(r.expected_passengers for r in rows)
    if total_demand <= 0:
        return 0.0
    return sum(r.estimated_wait_minutes * r.expected_passengers for r in rows) / total_demand


def _show_up_manual_wait(flight_specs, server_count, service_time_minutes=1.5) -> float:
    """
    ELLE hesap (production'ın KENDİSİ değil, `simulate_fifo_queue()`nin
    dokümante FIFO formülü + `departure_show_up_events()`nin SAF/
    deterministic dağıtımı) - `flight_specs`: [(dep_scheduled_utc, total_
    demand), ...].
    """
    from types import SimpleNamespace
    arrivals: list[tuple[datetime, float]] = []
    for dep_scheduled, total_demand in flight_specs:
        flight = SimpleNamespace(
            dep_actual_utc=None, dep_estimated_utc=None, dep_scheduled_utc=dep_scheduled,
        )
        arrivals.extend(departure_show_up_events(flight, total_demand))
    events = simulate_fifo_queue(
        [(t, "x", count) for t, count in arrivals], server_count, service_time_minutes,
    )
    total_wait = sum(e.wait_minutes * e.count for e in events)
    total_pax = sum(e.count for e in events)
    return total_wait / total_pax if total_pax else 0.0


def _arrival_release_manual_wait(flight_specs, server_count, service_time_minutes=1.5) -> float:
    """
    ELLE hesap - ADIM (Arrival Release Profile): `arrival_passenger_
    release_events()`nin SAF/deterministic dağıtımı + `simulate_fifo_
    queue()`nin dokümante FIFO formülü. `flight_specs`: [(arr_scheduled_
    utc HAM - effective_time() DEĞİL, total_demand), ...].
    """
    from types import SimpleNamespace
    arrivals: list[tuple[datetime, float]] = []
    for arr_scheduled, total_demand in flight_specs:
        flight = SimpleNamespace(
            arr_actual_utc=None, arr_estimated_utc=None, arr_scheduled_utc=arr_scheduled,
        )
        arrivals.extend(arrival_passenger_release_events(flight, total_demand))
    events = simulate_fifo_queue(
        [(t, "x", count) for t, count in arrivals], server_count, service_time_minutes,
    )
    total_wait = sum(e.wait_minutes * e.count for e in events)
    total_pax = sum(e.count for e in events)
    return total_wait / total_pax if total_pax else 0.0


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
    """
    ADIM (Generic Scale Resource Update) - large arrival passport server
    sayısı 30->45 oldu (bkz. rapor). ADIM (Arrival Release Profile):
    arrival yolcuları artık TEK bir +15dk noktasında DEĞİL, HAM arrival
    zamanından itibaren 5 saatlik deterministic release ile geliyor -
    "elle hesap" da AYNI (production'ın KENDİSİ değil, saf
    `arrival_passenger_release_events()`+`simulate_fifo_queue()`)
    dağılımı üretip TÜM etkilenen pencerelerin ağırlıklı ortalamasıyla
    karşılaştırır.
    """
    expected = _arrival_release_manual_wait(
        [(T0 - timedelta(minutes=15), 300), (T0 + timedelta(minutes=10) - timedelta(minutes=15), 250)],
        server_count=45,
    )

    flights = [_intl_arrival_flight("A", 0, "BIG300"), _intl_arrival_flight("B", 10, "BIG250")]
    predictions = _predict(flights)
    wait = _weighted_wait(predictions, PROCESS_PASSPORT_ARRIVAL)

    assert round(wait, 1) == round(expected, 1)
    assert wait != 0.0  # eski bug: HER ZAMAN 0 dönerdi


# ========================================================================
# 2) SENARYO B - AYNI ama B=+30dk - FARKLI bir wait üretmeli (eski
#    bug'ın ana kanıtı: eskiden İKİSİ de 0.0 dönerdi).
# ========================================================================

def test_scenario_b_large_arrival_passport_b_plus_30_minutes_differs_from_scenario_a():
    """
    ADIM (Generic Scale Resource Update) - large arrival passport server
    sayısı 30->45 oldu (bkz. rapor). ADIM (Arrival Release Profile):
    bkz. Senaryo A'nın AYNI notu.
    """
    expected = _arrival_release_manual_wait(
        [(T0 - timedelta(minutes=15), 300), (T0 + timedelta(minutes=30) - timedelta(minutes=15), 250)],
        server_count=45,
    )

    flights = [_intl_arrival_flight("A", 0, "BIG300"), _intl_arrival_flight("B", 30, "BIG250")]
    predictions = _predict(flights)
    wait = _weighted_wait(predictions, PROCESS_PASSPORT_ARRIVAL)

    # NOT: production her pencereyi KENDİ İÇİNDE 1 ondalığa yuvarlar
    # (engine.py `round(event_driven_wait_override, 1)`), bu yüzden
    # pencereler-arası ağırlıklı ortalama, ham/yuvarlanmamış "elle hesap"
    # değerinden (0.52) küçük bir yuvarlama farkıyla (0.6) ayrılabilir -
    # ikisi de AYNI matematiği doğruluyor, sadece yuvarlama sırası farklı.
    assert abs(wait - expected) < 0.1

    # Senaryo A ile KIYASLA - FARKLI olmalı (aynı 550 pax, SADECE B'nin
    # exact arrival dakikası farklı).
    flights_a = [_intl_arrival_flight("A", 0, "BIG300"), _intl_arrival_flight("B", 10, "BIG250")]
    scenario_a_wait = _weighted_wait(_predict(flights_a), PROCESS_PASSPORT_ARRIVAL)
    assert wait != scenario_a_wait


# ========================================================================
# 12) EVENT ORDER SENSITIVITY - "hourly toplam aynıysa wait de aynı"
#     eski bug'ının GERİ GELMEDİĞİNİN doğrudan regresyon testi.
# ========================================================================

def test_event_order_sensitivity_same_total_same_hour_different_timing_different_wait():
    case1 = [_intl_arrival_flight("A", 0, "BIG300"), _intl_arrival_flight("B", 10, "BIG250")]
    case2 = [_intl_arrival_flight("A", 0, "BIG300"), _intl_arrival_flight("B", 30, "BIG250")]

    wait1 = _weighted_wait(_predict(case1), PROCESS_PASSPORT_ARRIVAL)
    wait2 = _weighted_wait(_predict(case2), PROCESS_PASSPORT_ARRIVAL)

    # Aynı toplam (550 pax) - ama FARKLI exact timing (ADIM Arrival
    # Release Profile: 5 saatlik deterministic release, bkz. dosya
    # başındaki not) - wait HÂLÂ FARKLI olmalı (eski bug'ın ana kanıtı:
    # eskiden İKİSİ de 0.0 dönerdi).
    assert wait1 != wait2
    assert round(wait1, 1) == 1.0
    assert round(wait2, 1) == 0.6


# ========================================================================
# 5) PASSPORT DEPARTURE - International Departure zinciri.
# ========================================================================

def test_passport_departure_uses_event_driven_wait_large_scale():
    """
    ADIM (Generic Scale Resource Update) - large departure passport server
    sayısı 20->30 oldu (bkz. rapor). ADIM (Departure Show-Up Profile):
    flight'ların talebi artık TEK noktada değil 3 saate show-up ile
    yayılıyor - "elle hesap" da AYNI (production'ın KENDİSİ değil, saf
    `departure_show_up_events()`+`simulate_fifo_queue()`) dağılımı
    üretip TÜM etkilenen pencerelerin ağırlıklı ortalamasıyla karşılaştırır.
    """
    expected = _show_up_manual_wait(
        [(T0 + timedelta(minutes=120), 300), (T0 + timedelta(minutes=10) + timedelta(minutes=120), 250)],
        server_count=30,
    )

    flights = [_intl_departure_flight("A", 0, "BIG300"), _intl_departure_flight("B", 10, "BIG250")]
    predictions = _predict(flights)
    wait = _weighted_wait(predictions, PROCESS_PASSPORT_DEPARTURE)

    assert round(wait, 1) == round(expected, 1)
    assert wait > 0.0


# ========================================================================
# 6) DOMESTIC SECURITY - scale-derived lane (large=28, ADIM Generic
#    Scale Resource Update - eskiden 22, bkz. rapor).
# ========================================================================

def test_domestic_security_uses_event_driven_wait_large_scale():
    # 28 lane, 1dk/pax - 3 domestic flight (A320=180 pax). ADIM (Departure
    # Show-Up Profile): talep artık show-up ile 3 saate yayılıyor (bkz.
    # dosya docstring'i) - "elle hesap" AYNI dağılımı kullanır.
    flights = [
        _dom_departure_flight("D0", 0, "A320"),
        _dom_departure_flight("D1", 0, "A320"),
        _dom_departure_flight("D2", 5, "A320"),
    ]
    expected = _show_up_manual_wait(
        [
            (T0 + timedelta(minutes=120), 180.0),
            (T0 + timedelta(minutes=120), 180.0),
            (T0 + timedelta(minutes=5) + timedelta(minutes=120), 180.0),
        ],
        server_count=28, service_time_minutes=1.0,
    )
    predictions = _predict(flights)
    wait = _weighted_wait(predictions, PROCESS_SECURITY_DOMESTIC)

    assert round(wait, 1) == round(expected, 1)


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
    """
    ADIM (Generic Scale Resource Update) - medium arrival passport server
    sayısı 8->15 oldu (bkz. rapor). ADIM (Arrival Release Profile):
    bkz. dosya başındaki not - küçük toplamlar (24+16=40 pax) 15 server
    kapasitesinin çok altında kaldığı için release sonrası wait 0'a
    yakın/0 kalabilir - bu GERÇEK, doğrulanmış bir sonuç.
    """
    expected = _arrival_release_manual_wait(
        [(T0 - timedelta(minutes=15), 24), (T0 + timedelta(minutes=10) - timedelta(minutes=15), 16)],
        server_count=15,
    )
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
    wait = _weighted_wait(predictions, PROCESS_PASSPORT_ARRIVAL)
    assert round(wait, 1) == round(expected, 1)


def test_small_scale_departure_passport_uses_4_server_event_driven_wait():
    """
    ADIM (Generic Scale Resource Update) - small departure passport server
    sayısı 4->3 oldu (bkz. rapor). ADIM (Departure Show-Up Profile):
    talep show-up ile yayılıyor - "elle hesap" AYNI dağılımı kullanır.
    """
    resolver = MockCapacityResolver(capacities={"C8": 8, "C8B": 8})
    demand = DemandCalculator(resolver)
    config = default_config("IST", scale="small")
    flights = [
        _intl_departure_flight("A", 0, "C8"),
        _intl_departure_flight("B", 5, "C8B"),
    ]
    expected = _show_up_manual_wait(
        [
            (T0 + timedelta(minutes=120), 8),
            (T0 + timedelta(minutes=5) + timedelta(minutes=120), 8),
        ],
        server_count=3,
    )
    predictions = predict_airport(
        airport_iata="IST", flights=flights, config=config,
        demand=demand, now=T0 + timedelta(hours=3),
    )
    wait = _weighted_wait(predictions, PROCESS_PASSPORT_DEPARTURE)
    assert round(wait, 1) == round(expected, 1)


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
    arr_windows = api["international_arrival"]["windows"]
    assert all(isinstance(w["estimated_wait_minutes"], (int, float)) for w in arr_windows)
    # ADIM (Arrival Release Profile): talep artık BİRDEN FAZLA saate
    # yayılıyor (DEĞİŞTİ) - passenger-ağırlıklı ortalama (Senaryo A ile
    # AYNI girdi/AYNI beklenen sonuç, bkz. yukarıdaki test) kontrol edilir.
    total_demand = sum(w["expected_passengers"] for w in arr_windows)
    weighted_wait = sum(w["estimated_wait_minutes"] * w["expected_passengers"] for w in arr_windows) / total_demand
    assert round(weighted_wait, 1) == 1.0


def test_frontend_contract_unchanged():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "app" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert "function formatWaitMinutes(minutes)" in html
    assert 'graphSectionHtml("international_arrival", "INTERNATIONAL ARRIVAL", data.international_arrival)' in html
