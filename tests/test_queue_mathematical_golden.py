"""
genel-proje.md matematiksel/golden audit.

BU DOSYADAKİ TÜM `expected*` DEĞERLER ELLE HESAPLANMIŞTIR - production
algoritmasının KENDİ çıktısı asla "expected" olarak kullanılmaz (bkz.
görev talimatı: "algoritmanın kendisini kendisiyle test etmek DEĞİL").
Kullanılan tek "formül" - c-sunuculu, tek-servis-süreli, work-conserving
FIFO kuyruğun ders kitabı matematiği (k'ıncı server-dolu dalga = ilk
arrival + k * service_time) - bu, `simulate_fifo_queue()`'nun
DOKÜMANTE ettiği (ve bağımsız olarak DOĞRU olan) davranışın elle
türetilmiş sonucudur, kodun kendisinden okunmuş bir çıktı DEĞİLDİR.

Production kodu bu dosyada DEĞİŞTİRİLMEDİ - sadece pure/saf katmanlar
(`core/event_queue.py`, `domain/demand.py:effective_time`,
`engine.py:floor_to_window`/`predict_airport`) DB'siz çağrılır.
"""

from datetime import datetime, timedelta

import pytest

from app.queue.config import default_config
from app.queue.constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    STATUS_CANCELLED,
)
from app.queue.core.event_queue import (
    ServiceEvent,
    simulate_fifo_queue,
    simulate_international_departure_journey,
    simulate_passport,
    simulate_security,
    total_count,
)
from app.queue.domain.demand import DemandCalculator, effective_time
from app.queue.engine import floor_to_window, predict_airport

from .factories import MockCapacityResolver, MockFlight, arrival, at, departure

DAY = datetime(2026, 9, 18)


def _resolver_config():
    return default_config("XXX")


# ========================================================================
# 1) HOURLY BUCKET FLOOR - half-open [HH:00:00, HH+1:00:00)
# ========================================================================

@pytest.mark.parametrize("moment, expected", [
    (DAY.replace(hour=13, minute=0, second=0), DAY.replace(hour=13)),
    (DAY.replace(hour=13, minute=0, second=1), DAY.replace(hour=13)),
    (DAY.replace(hour=13, minute=27, second=42), DAY.replace(hour=13)),
    (DAY.replace(hour=13, minute=59, second=59), DAY.replace(hour=13)),
    (DAY.replace(hour=14, minute=0, second=0), DAY.replace(hour=14)),
])
def test_hourly_bucket_floor_half_open_boundaries(moment, expected):
    assert floor_to_window(moment, 60) == expected


def test_hourly_bucket_floor_does_not_mutate_or_lose_exact_timestamp():
    """Floor SADECE raporlama anında uygulanır - orijinal exact event zamanı korunur."""
    exact = DAY.replace(hour=13, minute=27, second=42, microsecond=123456)
    bucket = floor_to_window(exact, 60)
    assert exact == DAY.replace(hour=13, minute=27, second=42, microsecond=123456)  # değişmedi
    assert bucket == DAY.replace(hour=13)
    assert bucket != exact


# ========================================================================
# 2) DEPARTURE -120 DK
# ========================================================================

def test_departure_offset_120_minutes_and_bucket_is_derived_from_queue_event_not_flight_time():
    flight_1535 = MockFlight(
        direction=DIRECTION_DEPARTURE, location=LOCATION_INTERNATIONAL,
        dep_scheduled_utc=DAY.replace(hour=15, minute=35),
    )
    moment = effective_time(flight_1535)
    assert moment == DAY.replace(hour=13, minute=35)
    assert floor_to_window(moment, 60) == DAY.replace(hour=13)

    # KANIT: bir uçuş DOĞRUDAN "13:xx'te kalkıyor" diye 13:00 bucket'a
    # düşmez - bucket QUEUE EVENT zamanından (effective_time, -120dk
    # sonrası) türer. Aynı saatte (13:35) PLANLANAN bir uçuş -120dk
    # sonra 11:xx'e düşer, 13:00'e DEĞİL.
    flight_1335 = MockFlight(
        direction=DIRECTION_DEPARTURE, location=LOCATION_INTERNATIONAL,
        dep_scheduled_utc=DAY.replace(hour=13, minute=35),
    )
    moment_2 = effective_time(flight_1335)
    assert moment_2 == DAY.replace(hour=11, minute=35)
    assert floor_to_window(moment_2, 60) == DAY.replace(hour=11)
    assert floor_to_window(moment_2, 60) != DAY.replace(hour=13)


# ========================================================================
# 3) INTERNATIONAL ARRIVAL +15 DK
# ========================================================================

def test_arrival_offset_plus_15_minutes_and_bucket():
    flight = MockFlight(
        direction=DIRECTION_ARRIVAL, location=LOCATION_INTERNATIONAL,
        arr_scheduled_utc=DAY.replace(hour=13, minute=50),
    )
    moment = effective_time(flight)
    assert moment == DAY.replace(hour=14, minute=5)
    bucket = floor_to_window(moment, 60)
    assert bucket == DAY.replace(hour=14)
    assert bucket != DAY.replace(hour=13)  # +15dk uygulanmadan 13:00'e DÜŞMEMELİ


# ========================================================================
# 4) PASSPORT FIFO - ELLE HESAP (Senaryo 1)
#
# ELLE HESAP TABLOSU:
#   8 server, 1.5 dk/passenger, 16 passenger @ 13:00:00
#   dalga 1 (pax 1-8):  start=13:00:00  completion=13:00:00+1*1.5dk=13:01:30
#   dalga 2 (pax 9-16): start=13:01:30  completion=13:01:30+1.5dk =13:03:00
# ========================================================================

def test_passport_fifo_manual_16_passengers_8_servers_scenario_1():
    t0 = DAY.replace(hour=13, minute=0, second=0)
    events = simulate_fifo_queue([(t0, "intl_dep", 16)], server_count=8, service_time_minutes=1.5)
    events = sorted(events, key=lambda e: e.completion_time)

    assert len(events) == 2
    first, second = events

    assert first.count == 8
    assert first.arrival_time == t0
    assert first.service_start_time == t0
    assert first.completion_time == DAY.replace(hour=13, minute=1, second=30)

    assert second.count == 8
    assert second.arrival_time == t0
    assert second.service_start_time == DAY.replace(hour=13, minute=1, second=30)
    assert second.completion_time == DAY.replace(hour=13, minute=3, second=0)

    assert total_count(events) == 16


# ========================================================================
# 5) PASSPORT -> INTERNATIONAL SECURITY COUPLING
# ========================================================================

def test_passport_completion_feeds_security_arrival_exactly_not_passport_arrival():
    t0 = DAY.replace(hour=13, minute=0, second=0)
    passport = simulate_passport([(t0, 16)], [], departure_server_count=8, arrival_server_count=8, service_time_minutes=1.5)
    departure_events = sorted(passport["departure"], key=lambda e: e.completion_time)

    assert len(departure_events) == 2
    assert departure_events[0].count == 8
    assert departure_events[0].completion_time == DAY.replace(hour=13, minute=1, second=30)
    assert departure_events[1].count == 8
    assert departure_events[1].completion_time == DAY.replace(hour=13, minute=3, second=0)

    # engine.py'nin GERÇEKTEN yaptığı inşa: security arrival = passport completion.
    security_arrivals = [(e.completion_time, e.count) for e in departure_events]
    assert security_arrivals == [
        (DAY.replace(hour=13, minute=1, second=30), 8),
        (DAY.replace(hour=13, minute=3, second=0), 8),
    ]

    # Hiçbir security arrival, passport ARRIVAL anıyla (13:00:00) AYNI değil -
    # completion'dan ÖNCE de olamaz (burada tanım gereği eşit).
    for arr_time, _ in security_arrivals:
        assert arr_time != t0
        assert arr_time >= t0

    assert total_count(departure_events) == 16


# ========================================================================
# 6) PASSPORT VE SECURITY EŞZAMANLI İLERLEME (Senaryo 2)
#
# ELLE HESAP TABLOSU:
#   International security: 2 lane, 1 dk/passenger
#   8 passenger passport'tan 13:01:30'da security'ye gelir.
#   dalga 1 (pax 1-2): start=13:01:30 completion=13:02:30
#   dalga 2 (pax 3-4): start=13:02:30 completion=13:03:30
#   dalga 3 (pax 5-6): start=13:03:30 completion=13:04:30
#   dalga 4 (pax 7-8): start=13:04:30 completion=13:05:30
# ========================================================================

def test_security_concurrent_with_passport_manual_scenario_2():
    security_arrival_time = DAY.replace(hour=13, minute=1, second=30)
    events = simulate_security(
        [(security_arrival_time, 8)], lane_count=2, service_time_minutes=1.0,
    )
    events = sorted(events, key=lambda e: e.completion_time)

    expected = [
        (2, DAY.replace(hour=13, minute=1, second=30), DAY.replace(hour=13, minute=2, second=30)),
        (2, DAY.replace(hour=13, minute=2, second=30), DAY.replace(hour=13, minute=3, second=30)),
        (2, DAY.replace(hour=13, minute=3, second=30), DAY.replace(hour=13, minute=4, second=30)),
        (2, DAY.replace(hour=13, minute=4, second=30), DAY.replace(hour=13, minute=5, second=30)),
    ]
    assert len(events) == len(expected)
    for event, (count, start, completion) in zip(events, expected):
        assert event.count == count
        assert event.arrival_time == security_arrival_time
        assert event.service_start_time == start
        assert event.completion_time == completion
    assert total_count(events) == 8


def test_security_does_not_wait_for_passport_queue_to_fully_drain():
    """
    security'nin ilk dalgası (13:01:30) çalışırken passport'un İKİNCİ
    dalgası (16 passenger'lık) HENÜZ tamamlanmamış (13:03:00'da biter) -
    security passport'un TAMAMEN bitmesini BEKLEMİYOR, kendi bağımsız
    server havuzunda PARALEL ilerliyor (AŞAMA 10).
    """
    t0 = DAY.replace(hour=13, minute=0, second=0)
    passport = simulate_passport([(t0, 16)], [], departure_server_count=8, arrival_server_count=8, service_time_minutes=1.5)
    dep_events = sorted(passport["departure"], key=lambda e: e.completion_time)
    wave1_completion = dep_events[0].completion_time  # 13:01:30
    wave2_completion = dep_events[1].completion_time  # 13:03:00

    security_events = sorted(
        simulate_security([(wave1_completion, 8)], lane_count=2, service_time_minutes=1.0),
        key=lambda e: e.completion_time,
    )
    # security passport wave2 TAMAMLANMADAN (13:03:00) ÖNCE zaten en az
    # bir alt-dalgayı bitirmiş olmalı (13:02:30 <= 13:03:00).
    assert any(e.completion_time <= wave2_completion for e in security_events)
    # security'nin SON dalgası passport wave2'den SONRA biter (paralel,
    # ama security kendi 2-lane hızıyla daha YAVAŞ ilerliyor - beklenen).
    assert security_events[-1].completion_time > wave2_completion
    # security SADECE passport'tan çıkmış (wave1) passengerları aldı.
    assert total_count(security_events) == 8


# ========================================================================
# 7) CROSS-HOUR PASSPORT -> SECURITY (Senaryo 3)
# ========================================================================

def test_cross_hour_passport_completion_shifts_security_bucket_manual_scenario_3():
    t_arrival = DAY.replace(hour=12, minute=59, second=30)
    passport = simulate_passport([(t_arrival, 5)], [], departure_server_count=8, arrival_server_count=8, service_time_minutes=1.5)
    dep_events = passport["departure"]

    assert len(dep_events) == 1
    event = dep_events[0]
    assert event.arrival_time == t_arrival
    assert event.service_start_time == t_arrival  # 8 server boş, hemen başlar
    assert event.completion_time == DAY.replace(hour=13, minute=1, second=0)

    passport_bucket = floor_to_window(event.arrival_time, 60)
    assert passport_bucket == DAY.replace(hour=12)

    security_arrival = event.completion_time
    security_bucket = floor_to_window(security_arrival, 60)
    assert security_bucket == DAY.replace(hour=13)

    assert passport_bucket != security_bucket
    assert total_count(dep_events) == 5


# ========================================================================
# 8) AYNI UÇUŞUN FARKLI GRAPH SAATLERİ
#
# ELLE HESAP: 200 passenger @ 13:35:00 (flight 15:35 - 120dk), 8 server,
# 1.5dk. k'ıncı dalganın (8'li grup) completion'ı = 13:35:00 + k*1.5dk.
#   k=16 -> 13:35 + 24dk    = 13:59:00 -> bucket 13:00
#   k=17 -> 13:35 + 25.5dk  = 14:00:30 -> bucket 14:00
# Passport arrival TEK bir an (13:35:00) olduğu için TÜM 200 passenger'ın
# passport demand bucket'ı HER ZAMAN 13:00'dür - sadece security
# completion'ları (k arttıkça) 14:00'e taşar.
# ========================================================================

def test_same_flight_passport_and_security_land_in_different_hour_buckets():
    flight = MockFlight(
        direction=DIRECTION_DEPARTURE, location=LOCATION_INTERNATIONAL,
        dep_scheduled_utc=DAY.replace(hour=15, minute=35),
    )
    queue_arrival = effective_time(flight)
    assert queue_arrival == DAY.replace(hour=13, minute=35)

    passport = simulate_passport(
        [(queue_arrival, 200)], [], departure_server_count=8, arrival_server_count=8, service_time_minutes=1.5,
    )
    dep_events = passport["departure"]

    # Passport demand: TÜM olaylar AYNI arrival_time'a sahip -> TEK bucket.
    passport_buckets = {floor_to_window(e.arrival_time, 60) for e in dep_events}
    assert passport_buckets == {DAY.replace(hour=13)}

    # Security demand: completion zamanına göre 13:00 VE 14:00 bucket'ları
    # İKİSİ de gerçekleşmeli (elle hesap: dalga 16 -> 13:59, dalga 17 -> 14:00:30).
    security_buckets = {floor_to_window(e.completion_time, 60) for e in dep_events}
    assert DAY.replace(hour=13) in security_buckets
    assert DAY.replace(hour=14) in security_buckets

    wave16 = next(e for e in dep_events if e.completion_time == DAY.replace(hour=13, minute=59, second=0))
    wave17 = next(e for e in dep_events if e.completion_time == DAY.replace(hour=14, minute=0, second=30))
    assert wave16.count == 8
    assert wave17.count == 8
    assert total_count(dep_events) == 200


# ========================================================================
# 9) PASSENGER CONSERVATION
# ========================================================================

def test_passenger_conservation_across_full_international_departure_chain():
    t0 = DAY.replace(hour=13, minute=0, second=0)
    result = simulate_international_departure_journey(
        departure_arrivals=[(t0, 16)],
        arrival_arrivals=[(t0, 9)],
        passport_departure_server_count=8,
        passport_arrival_server_count=8,
        passport_service_time_minutes=1.5,
        international_security_lane_count=2,
        security_service_time_minutes=1.0,
    )
    assert total_count(result["passport_departure"]) == 16
    assert total_count(result["passport_arrival"]) == 9
    # SADECE departure-kökenli passengerlar security'ye girdi (arrival'lar HİÇ).
    assert total_count(result["security"]) == 16


# ========================================================================
# 10) DOMESTIC FLOW
# ========================================================================

def test_domestic_departure_feeds_only_domestic_security():
    flight = departure(9, 0, location=LOCATION_DOMESTIC, aircraft="A320")
    resolver = MockCapacityResolver()
    predictions = predict_airport(
        airport_iata="AAA", flights=[flight], config=_resolver_config(),
        demand=DemandCalculator(resolver), now=at(23, 0),
    )
    processes = {p.process for p in predictions}
    assert PROCESS_SECURITY_DOMESTIC in processes
    assert PROCESS_PASSPORT not in processes
    assert PROCESS_SECURITY_INTL not in processes

    # ADIM (Departure Show-Up Profile): flight_count HÂLÂ tek effective_
    # time() noktasında (07:00 - dep 09:00-120dk) toplanıyor (DEĞİŞMEDİ).
    dom = next(p for p in predictions if p.process == PROCESS_SECURITY_DOMESTIC and p.window_start.hour == 7)
    assert dom.flight_count == 1
    assert dom.expected_passengers == 108  # A320=180, show-up zirvesi 180*0.6


def test_domestic_arrival_feeds_no_queue_graph_at_all():
    flight = arrival(9, 0, location=LOCATION_DOMESTIC, aircraft="A320")
    resolver = MockCapacityResolver()
    predictions = predict_airport(
        airport_iata="AAA", flights=[flight], config=_resolver_config(),
        demand=DemandCalculator(resolver), now=at(23, 0),
    )
    assert predictions == []


# ========================================================================
# 11) INTERNATIONAL ARRIVAL FLOW
# ========================================================================

def test_international_arrival_feeds_only_passport_never_security():
    flight = arrival(9, 0, location=LOCATION_INTERNATIONAL, aircraft="A320")
    resolver = MockCapacityResolver()
    predictions = predict_airport(
        airport_iata="AAA", flights=[flight], config=_resolver_config(),
        demand=DemandCalculator(resolver), now=at(23, 0),
    )
    processes = {p.process for p in predictions}
    assert PROCESS_PASSPORT in processes
    assert PROCESS_SECURITY_INTL not in processes
    assert PROCESS_SECURITY_DOMESTIC not in processes

    passport = next(p for p in predictions if p.process == PROCESS_PASSPORT)
    assert passport.flight_count == 1
    assert passport.expected_passengers == 180


# ========================================================================
# 12) CANCELLED EXCLUSION
# ========================================================================

def test_cancelled_international_departure_produces_zero_demand_everywhere():
    flight = departure(
        13, 0, location=LOCATION_INTERNATIONAL, aircraft="A320", status=STATUS_CANCELLED,
    )
    resolver = MockCapacityResolver()
    predictions = predict_airport(
        airport_iata="AAA", flights=[flight], config=_resolver_config(),
        demand=DemandCalculator(resolver), now=at(23, 0),
    )
    # NOT: `predict_airport()` iptal edilmiş uçuş için YİNE DE bir pencere
    # satırı üretebilir (bkz. reason detector - Neden 7: "1 uçuş iptal
    # edildi" bilgilendirmesi raporlanır) - bu bir bug DEĞİL, kasıtlı
    # görünürlüktür. Asıl invariant (bu testin kanıtladığı): DEMAND'ın
    # (flight_count/expected_passengers) HER süreçte KESİN 0 olması.
    assert predictions != []  # reason satırı üretilmiş olabilir
    for p in predictions:
        assert p.flight_count == 0
        assert p.expected_passengers == 0


# ========================================================================
# 13) SERVICE START SAFETY
# ========================================================================

def test_no_passenger_ever_starts_service_before_their_own_arrival():
    arrivals = [
        (DAY.replace(hour=9, minute=0), "a", 20),
        (DAY.replace(hour=9, minute=5), "a", 3),
        (DAY.replace(hour=9, minute=5, second=1), "a", 1),
        (DAY.replace(hour=9, minute=30), "a", 50),
        (DAY.replace(hour=10, minute=0), "a", 1),
    ]
    events = simulate_fifo_queue(arrivals, server_count=8, service_time_minutes=1.5)
    assert events  # üretildi
    for event in events:
        assert event.service_start_time >= event.arrival_time
        assert event.completion_time >= event.service_start_time


# ========================================================================
# 14) FIFO ORDER
# ========================================================================

def test_fifo_order_preserved_single_server_no_passenger_skipped():
    t_a = DAY.replace(hour=13, minute=0)
    t_b = DAY.replace(hour=13, minute=1)
    events = simulate_fifo_queue(
        [(t_a, "A", 1), (t_b, "B", 1)], server_count=1, service_time_minutes=5.0,
    )
    events_by_origin = {e.origin: e for e in events}

    a = events_by_origin["A"]
    b = events_by_origin["B"]
    assert a.service_start_time == t_a
    assert a.completion_time == DAY.replace(hour=13, minute=5)
    # B, A bitmeden (server meşgulken) servise GİREMEZ - tek server.
    assert b.service_start_time == DAY.replace(hour=13, minute=5)
    assert b.completion_time == DAY.replace(hour=13, minute=10)
    assert b.service_start_time >= a.completion_time
    assert total_count(events) == 2


def test_fifo_same_timestamp_tie_break_is_deterministic_no_passenger_lost():
    """Aynı timestamp'te gelen iki farklı origin - HİÇBİRİ kaybolmaz, sıra `arrivals` listesindeki sırayla (stabil)."""
    t = DAY.replace(hour=13, minute=0)
    events = simulate_fifo_queue(
        [(t, "B", 1), (t, "A", 1)], server_count=1, service_time_minutes=2.0,
    )
    assert total_count(events) == 2
    events_by_origin = {e.origin: e for e in events}
    # Listede ÖNCE verilen ("B") ÖNCE servis alır (stabil sıra).
    assert events_by_origin["B"].service_start_time == t
    assert events_by_origin["A"].service_start_time == DAY.replace(hour=13, minute=2)


# ========================================================================
# 15) MIDNIGHT CARRY
# ========================================================================

def test_midnight_carry_no_passenger_lost_across_calendar_day_boundary():
    t_arrival = DAY.replace(hour=23, minute=59, second=30)
    passport = simulate_passport([(t_arrival, 1)], [], departure_server_count=8, arrival_server_count=8, service_time_minutes=1.5)
    event = passport["departure"][0]

    assert event.arrival_time == t_arrival
    assert event.completion_time == DAY.replace(day=19, hour=0, minute=1, second=0)

    passport_bucket = floor_to_window(event.arrival_time, 60)
    assert passport_bucket == DAY.replace(hour=23)  # ÖNCEKİ takvim günü, 23:00

    security_arrival = event.completion_time
    security_bucket = floor_to_window(security_arrival, 60)
    assert security_bucket == DAY.replace(day=19, hour=0)  # SONRAKİ takvim günü, 00:00

    assert total_count(passport["departure"]) == 1  # gece yarısı nedeniyle KAYBOLMADI


# ========================================================================
# 16) AIRPORT ISOLATION
# ========================================================================

def test_airport_isolation_no_shared_state_between_predict_airport_calls():
    resolver = MockCapacityResolver()
    ist_flight = departure(13, 0, airport="IST", location=LOCATION_INTERNATIONAL, aircraft="A388")  # 500 pax
    saw_flight = departure(13, 0, airport="SAW", location=LOCATION_DOMESTIC, aircraft="A320")  # 180 pax

    # SAW'ı TEK BAŞINA çalıştır.
    saw_alone = predict_airport(
        airport_iata="SAW", flights=[saw_flight], config=_resolver_config(),
        demand=DemandCalculator(resolver), now=at(23, 0),
    )

    # Şimdi ÖNCE ağır IST çağrısını, SONRA SAW'ı çalıştır - resolver/demand
    # PAYLAŞILIYOR olsa bile (kasıtlı - gerçek engine.py de aynı resolver'ı
    # havalimanları arasında paylaşır) SAW'ın sonucu DEĞİŞMEMELİ.
    _ = predict_airport(
        airport_iata="IST", flights=[ist_flight], config=_resolver_config(),
        demand=DemandCalculator(resolver), now=at(23, 0),
    )
    saw_after_ist = predict_airport(
        airport_iata="SAW", flights=[saw_flight], config=_resolver_config(),
        demand=DemandCalculator(resolver), now=at(23, 0),
    )

    assert saw_alone == saw_after_ist
    assert all(p.airport_iata == "SAW" for p in saw_after_ist)
    # ADIM (Departure Show-Up Profile): dep 13:00 -> show-up zirvesi (%60)
    # 11:00'e düşer (T-120..T-105 saat aralığı) - 180*0.6=108 (TAMAMI DEĞİL).
    dom = next(p for p in saw_after_ist if p.process == PROCESS_SECURITY_DOMESTIC and p.window_start.hour == 11)
    assert dom.expected_passengers == 108  # IST'nin 500'lük A388'i SIZMADI


# ========================================================================
# 18) WAIT VALUE SEMANTİĞİ - backend dakika değeri STRING/formatlanmış DEĞİL.
# ========================================================================

def test_backend_wait_minutes_remain_raw_numeric_not_display_formatted():
    flight = departure(13, 0, location=LOCATION_DOMESTIC, aircraft="A320")
    resolver = MockCapacityResolver()
    predictions = predict_airport(
        airport_iata="AAA", flights=[flight], config=_resolver_config(),
        demand=DemandCalculator(resolver), now=at(23, 0),
    )
    dom = next(p for p in predictions if p.process == PROCESS_SECURITY_DOMESTIC)
    if dom.estimated_wait_minutes is not None:
        assert isinstance(dom.estimated_wait_minutes, (int, float))
        assert not isinstance(dom.estimated_wait_minutes, str)
        # "saat"/"dk" gibi bir gösterim STRING'i SIZMAMALI.
        assert "saat" not in str(dom.estimated_wait_minutes)
        assert "dk" not in str(dom.estimated_wait_minutes)
