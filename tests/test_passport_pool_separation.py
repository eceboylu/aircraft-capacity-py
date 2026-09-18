"""
PHASE 6 - Airport-Scale Queue Capacity: departure/arrival passport
havuzlarının fiziksel olarak AYRI olduğunun elle hesaplanmış golden
kanıtı (`core/event_queue.py:simulate_passport()`).

Bu dosya SADECE `event_queue.py`'yi hedefler - `engine.py` entegrasyonu
AYRI bir fazdır (bkz. tests/test_final_realistic_replay.py ve
tests/test_queue_mathematical_golden.py'nin engine-seviyeli testleri,
Phase 7'de düzeltilecek).
"""
from datetime import datetime

from app.queue.core.event_queue import simulate_passport, total_count

T0 = datetime(2026, 9, 18, 13, 0, 0)


def test_large_departure_and_arrival_pools_both_complete_first_wave_at_13_01_30():
    """
    LARGE: 20 departure passenger + 30 arrival passenger, AYNI ANDA
    (13:00). Departure pool (20 server) TÜM 20 kişiyi TEK dalgada
    işler; arrival pool (30 server) TÜM 30 kişiyi TEK dalgada işler -
    HER İKİSİ de 13:00 -> 13:01:30, birbirini BEKLEMEDEN.
    """
    result = simulate_passport(
        departure_arrivals=[(T0, 20)],
        arrival_arrivals=[(T0, 30)],
        departure_server_count=20,
        arrival_server_count=30,
        service_time_minutes=1.5,
    )
    dep_events = result["departure"]
    arr_events = result["arrival"]

    assert len(dep_events) == 1
    assert dep_events[0].count == 20
    assert dep_events[0].service_start_time == T0
    assert dep_events[0].completion_time == datetime(2026, 9, 18, 13, 1, 30)

    assert len(arr_events) == 1
    assert arr_events[0].count == 30
    assert arr_events[0].service_start_time == T0
    assert arr_events[0].completion_time == datetime(2026, 9, 18, 13, 1, 30)

    assert total_count(dep_events) == 20
    assert total_count(arr_events) == 30


def test_medium_departure_and_arrival_pools_independent():
    """MEDIUM: departure=6 server, arrival=8 server."""
    result = simulate_passport(
        departure_arrivals=[(T0, 6)],
        arrival_arrivals=[(T0, 8)],
        departure_server_count=6,
        arrival_server_count=8,
        service_time_minutes=1.5,
    )
    dep_events = result["departure"]
    arr_events = result["arrival"]

    assert len(dep_events) == 1
    assert dep_events[0].count == 6
    assert dep_events[0].completion_time == datetime(2026, 9, 18, 13, 1, 30)

    assert len(arr_events) == 1
    assert arr_events[0].count == 8
    assert arr_events[0].completion_time == datetime(2026, 9, 18, 13, 1, 30)


def test_small_departure_and_arrival_pools_independent():
    """SMALL: departure=4 server, arrival=4 server (eşit ama YİNE DE ayrı havuz)."""
    result = simulate_passport(
        departure_arrivals=[(T0, 4)],
        arrival_arrivals=[(T0, 4)],
        departure_server_count=4,
        arrival_server_count=4,
        service_time_minutes=1.5,
    )
    dep_events = result["departure"]
    arr_events = result["arrival"]

    assert len(dep_events) == 1
    assert dep_events[0].count == 4
    assert dep_events[0].completion_time == datetime(2026, 9, 18, 13, 1, 30)

    assert len(arr_events) == 1
    assert arr_events[0].count == 4
    assert arr_events[0].completion_time == datetime(2026, 9, 18, 13, 1, 30)


def test_arrival_passenger_never_waits_for_departure_passenger():
    """
    LARGE departure havuzu AĞIR yük altında (200 pax, 20 server -> 10
    dalga, ~15dk) iken arrival havuzu (30 server, sadece 30 pax) KENDİ
    serverlarıyla ANINDA (0 wait) işler - departure'ın backlog'u
    arrival'ı HİÇ ETKİLEMEZ.
    """
    result = simulate_passport(
        departure_arrivals=[(T0, 200)],
        arrival_arrivals=[(T0, 30)],
        departure_server_count=20,
        arrival_server_count=30,
        service_time_minutes=1.5,
    )
    # Departure 10 dalgada biter (200/20), son completion = 13:00 + 10*1.5dk = 13:15:00.
    dep_events = result["departure"]
    assert max(e.completion_time for e in dep_events) == datetime(2026, 9, 18, 13, 15, 0)

    # Arrival (30 pax, 30 server) TEK dalgada, HİÇ beklemeden 13:01:30'da biter.
    arr_events = result["arrival"]
    assert len(arr_events) == 1
    assert arr_events[0].service_start_time == T0
    assert arr_events[0].wait_minutes == 0.0
    assert arr_events[0].completion_time == datetime(2026, 9, 18, 13, 1, 30)


def test_departure_passenger_never_waits_for_arrival_passenger():
    """Tersi: arrival ağır yük altındayken departure ETKİLENMEZ."""
    result = simulate_passport(
        departure_arrivals=[(T0, 20)],
        arrival_arrivals=[(T0, 300)],
        departure_server_count=20,
        arrival_server_count=30,
        service_time_minutes=1.5,
    )
    dep_events = result["departure"]
    assert len(dep_events) == 1
    assert dep_events[0].wait_minutes == 0.0
    assert dep_events[0].completion_time == datetime(2026, 9, 18, 13, 1, 30)

    arr_events = result["arrival"]
    # 300/30 = 10 dalga, son completion 13:15:00 - departure'ı hiç ETKİLEMEDİ.
    assert max(e.completion_time for e in arr_events) == datetime(2026, 9, 18, 13, 15, 0)


def test_passenger_conservation_across_both_independent_pools():
    result = simulate_passport(
        departure_arrivals=[(T0, 20), (datetime(2026, 9, 18, 13, 5), 15)],
        arrival_arrivals=[(T0, 30)],
        departure_server_count=20,
        arrival_server_count=30,
        service_time_minutes=1.5,
    )
    assert total_count(result["departure"]) == 35
    assert total_count(result["arrival"]) == 30
