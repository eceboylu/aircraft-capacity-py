"""
Arrival passenger release: point-batch modelinden 1-dakikalık
interval-flow modeline geçiş. Departure show-up ile AYNI overlap +
deterministic cumulative rounding motorunu (`_bucketed_profile_events`)
paylaşır - conservation ve non-aligned davranış AYNI garantilere sahip.
"""
from datetime import datetime, timedelta

import pytest

from app.queue.constants import ARRIVAL_RELEASE_PROFILE
from app.queue.domain.demand import arrival_passenger_release_events

from .conftest import make_arrival


@pytest.mark.parametrize("total_demand", [300, 220, 1, 2, 3, 7, 30, 47, 5])
def test_arrival_conservation_for_various_passenger_counts(total_demand):
    flight = make_arrival(when=datetime(2026, 3, 10, 12, 40))
    events = arrival_passenger_release_events(flight, total_demand)

    assert sum(count for _, count in events) == total_demand
    assert all(count > 0 for _, count in events)


def test_12_40_300_matches_1_minute_flow_acceptance_example():
    """Görevin kabul kriteri: 12:40/300 pax, 1-dakikalık akış."""
    flight = make_arrival(when=datetime(2026, 3, 10, 12, 40))
    events = arrival_passenger_release_events(flight, 300)

    by_minute = {t: c for t, c in events}

    def five_minute_bucket(start_minute: int) -> float:
        start = datetime(2026, 3, 10, 12, start_minute) if start_minute < 60 else \
            datetime(2026, 3, 10, 13, start_minute - 60)
        return sum(
            c for t, c in events
            if start <= t < start + timedelta(minutes=5)
        )

    assert five_minute_bucket(50) == 45   # 12:50-12:55
    assert five_minute_bucket(55) == 105  # 12:55-13:00
    assert five_minute_bucket(60) == 90   # 13:00-13:05
    assert five_minute_bucket(65) == 45   # 13:05-13:10
    assert five_minute_bucket(70) == 15   # 13:10-13:15

    # 105 pax / 5 dakika tam bölünüyor -> 21,21,21,21,21 (rounding artifact YOK)
    assert [by_minute[datetime(2026, 3, 10, 12, m)] for m in (55, 56, 57, 58, 59)] == [21, 21, 21, 21, 21]

    assert sum(events_count for _, events_count in events) == 300


def test_arrival_is_no_longer_point_batch():
    """Eski davranış: her profil noktası TEK bir anlık event üretirdi. Yeni davranış: her segment birden fazla 1-dk event'e yayılır."""
    flight = make_arrival(when=datetime(2026, 3, 10, 12, 40))
    events = arrival_passenger_release_events(flight, 300)
    # Eski point-batch modelde tam 5 event olurdu (bir profil noktası =
    # bir event). Yeni interval-flow modelde HER 5dk segment 5 ayrı
    # 1-dakikalık event'e bölündüğü için toplam event sayısı > 5.
    assert len(events) > 5


def test_non_aligned_arrival_minute_still_conserves():
    """12:43 gibi 5'in katı olmayan bir arrival - global 1-dk bucket'lar mutlak saate göre hizalı, conservation korunuyor."""
    base = datetime(2026, 3, 10, 12, 43)
    flight = make_arrival(when=base)
    total_demand = 300
    events = arrival_passenger_release_events(flight, total_demand)

    assert sum(count for _, count in events) == total_demand
    # Hiçbir passenger kendi bandının başlangıcından (T+10) önce release edilmiyor.
    earliest_allowed = base + timedelta(minutes=ARRIVAL_RELEASE_PROFILE[0][0])
    assert all(moment >= earliest_allowed for moment, _ in events)
    # Son bant T+30..T+35'te bitiyor.
    latest_allowed = base + timedelta(minutes=ARRIVAL_RELEASE_PROFILE[-1][1])
    assert all(moment < latest_allowed for moment, _ in events)


def test_no_reference_time_returns_empty():
    flight = make_arrival(when=None)
    assert arrival_passenger_release_events(flight, 100) == []


def test_zero_or_negative_demand_returns_empty():
    flight = make_arrival(when=datetime(2026, 3, 10, 12, 0))
    assert arrival_passenger_release_events(flight, 0) == []
    assert arrival_passenger_release_events(flight, -5) == []


def test_arrival_profile_is_final_contract():
    assert ARRIVAL_RELEASE_PROFILE == (
        (10, 15, 0.15),
        (15, 20, 0.35),
        (20, 25, 0.30),
        (25, 30, 0.15),
        (30, 35, 0.05),
    )
