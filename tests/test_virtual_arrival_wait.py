"""
`virtual_arrival_wait()` - graph için "queue-state / virtual-arrival
wait" sampler'ı. Gerçek, ÖNCEDEN hesaplanmış `ServiceEvent` listesini
READ-ONLY olarak okur (production FIFO'yu yeniden simüle etmez,
mutasyona uğratmaz) - "şu an bir yolcu gelse ne kadar beklerdi"
sorusuna gerçek backlog + gerçek server availability'e göre cevap verir.
"""
from datetime import datetime, timedelta

from app.queue.core.event_queue import (
    ServiceEvent,
    simulate_fifo_queue,
    virtual_arrival_wait,
)


def test_empty_queue_zero_wait():
    assert virtual_arrival_wait([], datetime(2026, 3, 10, 12, 0), 8, 1.0) == 0.0


def test_matches_hand_computed_8_desk_24_passenger_scenario():
    """
    8 desk, 1dk/pax, 24 pax hepsi 12:00'da geliyor (bkz. section 10/20
    walkthrough). 12:00 anında (tüm 24 kişi hâlâ backlog/service'te)
    virtual 25. kişi 3 dakika beklemeli (24/8 = 3 tur x 1 dk).
    """
    base = datetime(2026, 3, 10, 12, 0)
    events = simulate_fifo_queue([(base, "x", 24.0)], server_count=8, service_time_minutes=1.0)

    assert virtual_arrival_wait(events, base, 8, 1.0) == 3.0
    # Tam completion anında (12:03) - queue tamamen boşalmış, wait=0.
    assert virtual_arrival_wait(events, base + timedelta(minutes=3), 8, 1.0) == 0.0
    # Ara bir an (12:02:30): son grup hâlâ 12:03'e kadar meşgul, backlog yok.
    mid = base + timedelta(minutes=2, seconds=30)
    assert virtual_arrival_wait(events, mid, 8, 1.0) == 0.5


def test_zero_backlog_immediate_availability_gives_zero_wait():
    """Ne arrival ne backlog, server'lar hemen müsait -> wait=0, ama nokta yine üretilebilir olmalı (None DEĞİL)."""
    base = datetime(2026, 3, 10, 12, 0)
    events = simulate_fifo_queue([(base, "x", 4.0)], server_count=8, service_time_minutes=1.0)
    later = base + timedelta(hours=2)
    assert virtual_arrival_wait(events, later, 8, 1.0) == 0.0


def test_real_drain_not_fake_decay():
    """
    Gerçek backlog var, YENİ arrival yok - ama sunucular çalışmaya devam
    ettiği için virtual wait ZAMANLA GERÇEKTEN azalmalı (hardcoded/lineer
    bir azalma DEĞİL, gerçek FIFO state'ine göre).
    """
    base = datetime(2026, 3, 10, 12, 0)
    # 1 server, 1dk/pax, 40 pax tek seferde -> 40 dakikalık backlog.
    events = simulate_fifo_queue([(base, "x", 40.0)], server_count=1, service_time_minutes=1.0)

    wait_at_0 = virtual_arrival_wait(events, base, 1, 1.0)
    wait_at_10 = virtual_arrival_wait(events, base + timedelta(minutes=10), 1, 1.0)
    wait_at_20 = virtual_arrival_wait(events, base + timedelta(minutes=20), 1, 1.0)

    assert wait_at_0 == 40.0  # ilk anda tüm 40 kişi önde
    assert wait_at_10 == 30.0  # 10 dakika geçti, 10 kişi işlendi
    assert wait_at_20 == 20.0
    # Gerçek azalma - sabit kopyalama/lineer olmayan gerçek state.
    assert wait_at_0 > wait_at_10 > wait_at_20


def test_does_not_mutate_input_events():
    base = datetime(2026, 3, 10, 12, 0)
    events = simulate_fifo_queue([(base, "x", 24.0)], server_count=8, service_time_minutes=1.0)
    before = list(events)
    virtual_arrival_wait(events, base, 8, 1.0)
    assert events == before


def test_no_interpolation_between_two_real_points():
    """
    Ortadaki bir nokta, iki komşu noktanın ORTALAMASI/interpolasyonu
    OLARAK DEĞİL, kendi gerçek FIFO durumundan hesaplanmalı. Bunu
    kanıtlamak için backlog'un DOĞRUSAL AZALMADIĞI asimetrik bir
    senaryo kullanılıyor (ikinci bir cohort 12:08'de araya giriyor) -
    eğer fonksiyon interpolasyon yapıyor olsaydı orta nokta komşularının
    ortalamasına eşit çıkardı; gerçek FIFO'da eşit ÇIKMAZ.
    """
    base = datetime(2026, 3, 10, 12, 0)
    events = simulate_fifo_queue(
        [(base, "x", 40.0), (base + timedelta(minutes=8), "x", 40.0)],
        server_count=1, service_time_minutes=1.0,
    )
    w5 = virtual_arrival_wait(events, base + timedelta(minutes=5), 1, 1.0)
    w10 = virtual_arrival_wait(events, base + timedelta(minutes=10), 1, 1.0)
    w15 = virtual_arrival_wait(events, base + timedelta(minutes=15), 1, 1.0)

    assert w10 != (w5 + w15) / 2


def test_currently_busy_servers_block_virtual_arrival():
    """Backlog=0 ama TÜM serverlar meşgulse virtual arrival, ilk boşalana kadar bekler."""
    base = datetime(2026, 3, 10, 12, 0)
    events = [
        ServiceEvent("x", 1.0, base, base, base + timedelta(minutes=5)),
        ServiceEvent("x", 1.0, base, base, base + timedelta(minutes=3)),
    ]
    probe = base + timedelta(minutes=1)
    # 2 server, ikisi de meşgul (biri 12:03'e, biri 12:05'e kadar) - en
    # erken boşalan 12:03 -> 12:01'de gelen virtual kişi 2 dk bekler.
    assert virtual_arrival_wait(events, probe, 2, 4.0) == 2.0
