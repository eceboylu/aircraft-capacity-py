"""
Security service time FINAL CONTRACT: 60 saniye/yolcu/lane (=1.0
dakika, 60 pax/saat/lane) - bkz. `constants.py: SECURITY_PASSENGERS_PER_
HOUR_PER_LANE`. Bu değer önce 0.4 -> 1.5 -> 1.0 -> 50sn'ye (72/saat),
şimdi 50sn -> 60sn'ye (60/saat) bilinçli olarak YAVAŞLATILDI; BU DOSYA
HER SEFERİNDE O ANKİ AKTİF/FINAL kontratı kilitler.

Passport service time (1.0 dk) ve passport FIFO davranışı bu görevde
DEĞİŞMEDİ - burada sadece kilitlendi (regression guard).
"""
from datetime import datetime, timedelta

import pytest

from app.queue.config import default_config
from app.queue.constants import SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES
from app.queue.core.event_queue import simulate_fifo_queue
from app.queue.core.scoring import queue_capacity_rate, security_capacity_rate


def test_security_service_time_is_60_seconds():
    assert SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES == pytest.approx(1.0)


def test_security_capacity_per_lane_is_60_per_hour():
    cfg = default_config("XXX")
    rate_per_minute = queue_capacity_rate(1, cfg.security_service_time_minutes)
    assert rate_per_minute * 60 == pytest.approx(60.0)


def test_security_capacity_rate_scales_linearly_with_lane_count():
    cfg = default_config("XXX")
    cfg.security_lane_count = 7
    assert round(security_capacity_rate(cfg) * 60, 6) == pytest.approx(420.0)  # 7 x 60

    cfg.security_lane_count = 20
    assert round(security_capacity_rate(cfg) * 60, 6) == pytest.approx(1200.0)  # 20 x 60


def test_passport_service_time_unchanged_at_1_minute():
    cfg = default_config("XXX")
    assert cfg.passport_service_time_minutes == 1.0


def test_passport_static_fifo_8_desks_24_passengers():
    """PASSPORT FIFO davranışı bu görevde DEĞİŞMEDİ - regression guard."""
    base = datetime(2026, 3, 10, 12, 0)
    events = simulate_fifo_queue(
        [(base, "arrival", 24.0)], server_count=8, service_time_minutes=1.0,
    )
    by_wait = {round(e.wait_minutes, 2): e.count for e in events}
    assert by_wait == {0.0: 8.0, 1.0: 8.0, 2.0: 8.0}


def test_security_synthetic_1_lane_1_min_completion():
    """1 security lane, 12:00 arrival -> completion 12:01, ikinci passenger wait=1 min."""
    base = datetime(2026, 3, 10, 12, 0)
    events = simulate_fifo_queue(
        [(base, "security", 2.0)], server_count=1, service_time_minutes=1.0,
    )
    by_wait = {round(e.wait_minutes, 2): e for e in events}
    assert set(by_wait) == {0.0, 1.0}
    assert by_wait[0.0].completion_time == base + timedelta(minutes=1)
    assert by_wait[1.0].completion_time == base + timedelta(minutes=2)


def test_security_static_fifo_15_lanes_16_passengers_1_minute():
    """
    15 lane, 1.0 dk/pax, 16 pax aynı anda: ilk 15 wait=0, 16. kişi
    earliest_lane_free = arrival+1dk bekler.
    """
    base = datetime(2026, 3, 10, 12, 0)
    events = simulate_fifo_queue(
        [(base, "security", 16.0)], server_count=15, service_time_minutes=1.0,
    )
    by_wait = {round(e.wait_minutes, 2): e.count for e in events}
    assert by_wait == {0.0: 15.0, 1.0: 1.0}

    last = [e for e in events if e.wait_minutes > 0][0]
    assert last.completion_time == base + timedelta(minutes=2.0)


def test_security_lane_continuity_no_reset_on_new_cohort():
    """
    12:20'de 5 pax (7 lane) servise girer - 5 lane 1dk meşgul olur.
    12:20:30'da 8 pax daha gelirse, mevcut 5 lane'in availability'si
    SIFIRLANMAZ - yeni 8 kişi gerçek lane state'inin ÜZERİNE eklenir.
    """
    t0 = datetime(2026, 3, 10, 12, 20, 0)
    t1 = datetime(2026, 3, 10, 12, 20, 30)
    arrivals = [
        (t0, "security", 5.0),
        (t1, "security", 8.0),
    ]
    events = simulate_fifo_queue(arrivals, server_count=7, service_time_minutes=1.0)

    first_cohort = [e for e in events if e.arrival_time == t0]
    assert sum(e.count for e in first_cohort) == 5
    assert all(e.wait_minutes == 0 for e in first_cohort)
    assert all(e.completion_time == t0 + timedelta(minutes=1.0) for e in first_cohort)

    second_cohort = [e for e in events if e.arrival_time == t1]
    assert sum(e.count for e in second_cohort) == 8
    # 7 lane, ilk cohort 5 lane kullanıyor (1dk boyunca) -> t1 anında
    # sadece 2 lane hemen boş; kalan 6 kişi eski (t0 kaynaklı) lane
    # availability'lerini beklemek zorunda - reset olsaydı hepsi wait=0
    # olurdu.
    waits = sorted(e.wait_minutes for e in second_cohort for _ in range(int(e.count)))
    assert waits[0] == 0 and waits[1] == 0  # 2 boş lane hemen kullanılır
    assert any(w > 0 for w in waits)  # geri kalanlar gerçek lane state'ini bekler
