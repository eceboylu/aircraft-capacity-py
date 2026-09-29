"""
LARGE scale international_security_lanes: 6 -> 15 (ZRH'nin bağlı olduğu
scale). Domestic zaten 15'ti, artık ikisi de 15 ama HÂLÂ AYRI resource
pool - lane sayısının eşit olması ortak havuz olduğu anlamına GELMEZ.

Bu değişiklik SADECE processing capacity'yi etkilemeli - passenger
demand/timing/FIFO formülü DEĞİŞMEMELİ.
"""
from datetime import datetime

from app.queue.config import default_config
from app.queue.constants import SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES
from app.queue.core.event_queue import simulate_fifo_queue
from app.queue.core.scoring import international_security_capacity_rate
from app.queue.domain.demand import DemandCalculator, departure_show_up_events
from app.queue.engine import predict_airport
from app.queue.constants import PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL

from .conftest import FakeCapacityResult, FakeResolver, make_departure


def test_large_international_security_lanes_is_15():
    cfg = default_config("XXX", scale="large")
    assert cfg.international_security_lane_count == 15


def test_large_domestic_security_lanes_is_15():
    cfg = default_config("XXX", scale="large")
    assert cfg.domestic_security_lane_count == 15


def test_security_service_time_is_50_seconds():
    import pytest
    assert SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES == pytest.approx(50.0 / 60.0)


def test_large_international_capacity_is_1080_per_hour():
    cfg = default_config("XXX", scale="large")
    assert round(international_security_capacity_rate(cfg) * 60, 6) == 1080  # 15 x 72


def test_zrh_like_config_resolves_to_15_intl_lanes():
    """`get_config`'e gitmeden `default_config` üzerinden aynı 'ZRH scale=large' senaryosunu doğrular (DB'siz, hızlı)."""
    cfg = default_config("ZRH", scale="large")
    assert cfg.international_security_lane_count == 15
    assert cfg.domestic_security_lane_count == 15


def test_lane_count_change_does_not_alter_passenger_totals_or_timestamps():
    """
    Aynı flight/demand ile, lane sayısı DEĞİŞSE BİLE (6 vs 15) her
    passenger'ın arrival_time'ı ve toplam sayısı AYNI kalmalı - lane
    sadece servis kapasitesini (wait) etkiler, event üretimini DEĞİL.
    """
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=900)})
    demand = DemandCalculator(resolver)
    flight = make_departure(
        when=datetime(2026, 3, 10, 15, 0), location="international",
        requires_passport=False, aircraft_icao="A321", flight_key="SCH-1",
    )
    total_demand = demand.passenger_demand(flight)
    show_up = departure_show_up_events(flight, total_demand)

    tagged = [(t, "security", c) for t, c in show_up]
    events_old_6_lanes = simulate_fifo_queue(tagged, server_count=6, service_time_minutes=1.0)
    events_new_15_lanes = simulate_fifo_queue(tagged, server_count=15, service_time_minutes=1.0)

    def _count_by_arrival_time(events):
        totals: dict = {}
        for e in events:
            totals[e.arrival_time] = totals.get(e.arrival_time, 0.0) + e.count
        return totals

    assert sum(c for _, c in show_up) == total_demand
    assert sum(e.count for e in events_old_6_lanes) == sum(e.count for e in events_new_15_lanes)
    # Lane sayısı service_start/merge gruplamasını değiştirebilir (bkz.
    # `_merge_adjacent_events`), ama HER arrival_time'a düşen TOPLAM
    # yolcu sayısı (event üretiminin/timing'in kendisi) AYNI kalmalı.
    assert _count_by_arrival_time(events_old_6_lanes) == _count_by_arrival_time(events_new_15_lanes)


def test_lane_count_increase_reduces_wait_under_same_demand():
    """15 lane, 6 lane'e göre AYNI talep altında wait'i azaltmalı (artırmamalı/aynı bırakmamalı, gerçek overload senaryosunda)."""
    base = datetime(2026, 3, 10, 12, 0)
    heavy_arrivals = [(base, "security", 900.0)]  # tek anlık büyük dalga - her iki lane sayısı için de overload

    events_6 = simulate_fifo_queue(heavy_arrivals, server_count=6, service_time_minutes=1.0)
    events_15 = simulate_fifo_queue(heavy_arrivals, server_count=15, service_time_minutes=1.0)

    max_wait_6 = max(e.wait_minutes for e in events_6)
    max_wait_15 = max(e.wait_minutes for e in events_15)
    assert max_wait_15 < max_wait_6


def test_security_dom_and_security_intl_remain_isolated_after_lane_change():
    """dom ve intl lane sayısı ARTIK İKİSİ DE 15 olsa bile, hâlâ AYRI resource pool/FIFO/backlog - bir tanesine demand vermek diğerini etkilememeli."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("XXX", scale="large")

    domestic_only = [
        make_departure(when=datetime(2026, 3, 10, 15, 0), location="domestic", flight_key="DOM-ONLY"),
    ]
    preds = predict_airport("XXX", domestic_only, config, demand)
    intl_rows = [p for p in preds if p.process == PROCESS_SECURITY_INTL]
    dom_rows = [p for p in preds if p.process == PROCESS_SECURITY_DOMESTIC]

    assert any(p.expected_passengers > 0 for p in dom_rows)
    assert all(p.expected_passengers == 0 for p in intl_rows)
