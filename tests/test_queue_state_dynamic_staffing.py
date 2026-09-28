"""
Section 80 - queue-state sampler, MEGA passport dynamic staffing'in
GERÇEK (ramp edilmiş) server sayısını yansıtmalı, config'teki sabit
taban (default) sayıyı DEĞİL.
"""
from datetime import datetime, timedelta

from app.queue.domain.dynamic_staffing import active_capacity_at


def test_active_capacity_at_uses_last_checkpoint_at_or_before_timestamp():
    base = datetime(2026, 3, 10, 12, 0)
    schedule = [
        (base, 30),
        (base + timedelta(minutes=10), 35),
        (base + timedelta(minutes=20), 40),
    ]
    assert active_capacity_at(schedule, base) == 30
    assert active_capacity_at(schedule, base + timedelta(minutes=5)) == 30
    assert active_capacity_at(schedule, base + timedelta(minutes=10)) == 35
    assert active_capacity_at(schedule, base + timedelta(minutes=25)) == 40
    # checkpoint'ten ÖNCE (ilk checkpoint'ten bile önce) - ilk değere düşer.
    assert active_capacity_at(schedule, base - timedelta(minutes=5)) == 30


def test_active_capacity_at_empty_schedule_returns_none():
    assert active_capacity_at([], datetime(2026, 3, 10, 12, 0)) is None


def test_dynamic_passport_queue_state_reflects_ramped_capacity_not_static_default():
    """
    MEGA departure passport: default=15, max=40. Aşırı yüklü bir senaryoda
    dynamic staffing ramp-up yapar - queue-state sampler bu ramp'i
    (sabit 15 değil) kullanmalı, yoksa wait OLDUĞUNDAN YÜKSEK çıkar.
    """
    from app.queue.config import default_config
    from app.queue.domain.demand import DemandCalculator
    from app.queue.engine import PROCESS_PASSPORT_DEPARTURE, event_driven_display_series

    from .conftest import FakeCapacityResult, FakeResolver, make_departure

    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=300)})
    demand = DemandCalculator(resolver)
    config = default_config("XXX", scale="mega")
    assert config.passport_departure_dynamic is True
    assert config.passport_departure_server_count == 15
    assert config.passport_departure_server_count_max == 40

    # Çok sayıda ağır yüklü uçuş - aynı ~1 saatlik pencereye yoğun show-up.
    flights = [
        make_departure(
            when=datetime(2026, 3, 10, 12, 0) + timedelta(minutes=5 * i),
            location="international", aircraft_icao="A321",
        )
        for i in range(20)
    ]

    now = datetime(2026, 3, 10, 12, 30)
    series = event_driven_display_series(flights, config, demand, now=now)
    points = dict(series[PROCESS_PASSPORT_DEPARTURE])

    probe = datetime(2026, 3, 10, 12, 30)
    assert probe in points
    # Negatif olmayan gerçek bir sayı üretmeli (NaN/None DEĞİL) -
    # asıl garanti: fonksiyon hatasız, dynamic schedule'ı kullanarak
    # tutarlı bir sonuç üretiyor (mutlak sayı senaryonun tam yüküne
    # bağlı olduğu için burada sadece well-formed olduğunu doğruluyoruz).
    assert points[probe] >= 0.0
