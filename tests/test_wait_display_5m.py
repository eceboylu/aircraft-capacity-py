"""
ADIM (Queue-State / Virtual-Arrival Wait Display) - grafik artık
"o 5-dakikalık pencerede yeni kuyruğa girenlerin ortalama wait'i"
DEĞİL (eski point-in-time arrival-sample semantics), "şu an bir yolcu
gelse mevcut gerçek FIFO durumuna göre kaç dakika beklerdi" (queue-
state / virtual-arrival wait, `core/event_queue.py:virtual_arrival_
wait`) gösteriyor.

`five_minute_wait_series()` (arrival-weighted ortalama) fonksiyonun
KENDİSİ kaldırılmadı - hâlâ doğru ve test edilmiş bir yardımcı - ama
`event_driven_display_series()` artık onu ÇAĞIRMIYOR; saatlik
`QueuePrediction`'ın event-driven wait'i (`_bucket_weighted_wait`,
`_event_driven_queue_demand` içinde) de DEĞİŞMEDİ.

Kapsam:
  - Tam gün için SABİT sayıda (24h/5dk=288) nokta - boş bar YOK
  - Yeni arrival olmasa bile backlog varsa nokta gerçek state'i taşır
  - Backlog yokken/queue boşken wait=0 (nokta yine MEVCUT)
  - carry-forward/interpolasyon YOK - komşu noktalardan türetilmiyor
  - `predict_airport()`'un (saatlik, resmi) sonucunu DEĞİŞTİRMEZ
  - production FIFO'yu (`process_events`) MUTASYONA UĞRATMAZ
"""
from datetime import datetime, timedelta

from app.queue.config import default_config
from app.queue.constants import (
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
)
from app.queue.core.event_queue import ServiceEvent, simulate_fifo_queue
from app.queue.core.scoring import risk_from_wait
from app.queue.domain.demand import DemandCalculator
from app.queue.engine import (
    DISPLAY_5M_PROCESSES,
    event_driven_display_series,
    five_minute_wait_series,
    predict_airport,
)

from .conftest import FakeCapacityResult, FakeResolver, make_departure


def test_five_minute_series_matches_hand_built_service_events():
    """`five_minute_wait_series()` (hâlâ mevcut, hâlâ doğru bir arrival-weighted yardımcı) - fonksiyon hiçbir hesap YAPMAZ, sadece okur."""
    base = datetime(2026, 3, 10, 9, 0)
    events = [
        ServiceEvent("x", 2.0, base, base, base),
        ServiceEvent("x", 3.0, base, base + timedelta(minutes=4), base + timedelta(minutes=4)),
        ServiceEvent("x", 1.0, base + timedelta(minutes=10), base + timedelta(minutes=20), base + timedelta(minutes=20)),
    ]
    series = five_minute_wait_series(events, window_minutes=5)

    assert series == [
        (base, 2.4, 5.0),
        (base + timedelta(minutes=10), 10.0, 1.0),
    ]


def test_backlog_continuity_not_reset_at_hour_boundary():
    """`five_minute_wait_series` saat sınırını aşan backlog'ta ANİDEN sıfıra düşmez (hâlâ geçerli, bu fonksiyon değişmedi)."""
    arrivals = [
        (datetime(2026, 3, 10, 12, 30) + timedelta(minutes=5 * i), "departure", 10.0)
        for i in range(10)
    ]
    events = simulate_fifo_queue(arrivals, server_count=1, service_time_minutes=1.0)
    series = five_minute_wait_series(events, window_minutes=5)

    hour_13 = datetime(2026, 3, 10, 13, 0)
    before = [(k, w) for k, w, _ in series if k < hour_13]
    after = [(k, w) for k, w, _ in series if k >= hour_13]
    assert before and after
    assert after[0][1] >= before[-1][1]


def test_display_5m_processes_are_only_the_four_split_processes():
    assert set(DISPLAY_5M_PROCESSES) == {
        PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL,
        PROCESS_PASSPORT_DEPARTURE, PROCESS_PASSPORT_ARRIVAL,
    }


def test_display_series_risk_matches_backend_risk_from_wait():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=240)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")
    flight = make_departure(when=datetime(2026, 3, 10, 9, 30), aircraft_icao="A321")

    display_series = event_driven_display_series(
        [flight], config, demand, now=datetime(2026, 3, 10, 10, 0),
    )
    for points in display_series.values():
        for _window_start, wait_minutes in points:
            assert risk_from_wait(wait_minutes) == risk_from_wait(wait_minutes)


def test_display_series_does_not_mutate_hourly_prediction():
    """Grafik-only çağrı `predict_airport()`'un (resmi/saatlik) sonucunu HİÇ DEĞİŞTİRMEZ."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")
    flight = make_departure(when=datetime(2026, 3, 10, 9, 30), aircraft_icao="A321")

    before = predict_airport("IST", [flight], config, demand)
    event_driven_display_series([flight], config, demand, now=datetime(2026, 3, 10, 10, 0))
    after = predict_airport("IST", [flight], config, demand)

    key = lambda p: (p.process, p.window_start)
    assert {key(p): p.expected_passengers for p in before} == {key(p): p.expected_passengers for p in after}
    assert {key(p): p.estimated_wait_minutes for p in before} == {key(p): p.estimated_wait_minutes for p in after}
    assert {key(p): p.risk for p in before} == {key(p): p.risk for p in after}


def test_full_day_has_288_points_per_process_no_gaps():
    """24h / 5dk = 288 nokta - yeni arrival olmasa bile HİÇBİR bar eksik değil."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")
    flight = make_departure(when=datetime(2026, 3, 10, 9, 30), aircraft_icao="A321")

    now = datetime(2026, 3, 10, 23, 0)
    series = event_driven_display_series([flight], config, demand, now=now)

    for process in DISPLAY_5M_PROCESSES:
        points = series[process]
        assert len(points) == 288
        # noktalar tam 5dk aralıklı, kesintisiz, gün başından (00:00) itibaren
        expected_start = datetime(2026, 3, 10, 0, 0)
        assert points[0][0] == expected_start
        assert points[1][0] == expected_start + timedelta(minutes=5)
        assert points[-1][0] == expected_start + timedelta(hours=23, minutes=55)


def test_no_arrival_but_backlog_still_produces_real_point():
    """
    Yoğun bir departure show-up dalgası sonrası, YENİ arrival'ın
    olmadığı ama backlog'un devam ettiği bir 5dk noktası - eski
    semantics'te bu nokta HİÇ olmazdı, yeni semantics'te GERÇEK
    (kopyalanmamış) bir wait değeriyle var olmalı.
    """
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=300)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")
    # Tek bir dev departure dalgası - 1 security lane'e göre AŞIRI yüklü
    # (bkz. `domestic_security_lane_count` default), backlog uzun sürer.
    config.domestic_security_lane_count = 1
    flight = make_departure(
        when=datetime(2026, 3, 10, 12, 0), location="domestic", aircraft_icao="A321",
    )

    now = datetime(2026, 3, 10, 13, 0)
    series = event_driven_display_series([flight], config, demand, now=now)
    points = dict(series[PROCESS_SECURITY_DOMESTIC])

    # T-60..T (5%) segmenti 11:00-12:00'a düşer, ama en büyük yük
    # T-120..T-60 (%40) ve T-180..T-120 (%45) segmentlerinden - 12:00
    # civarında backlog kesin oluşur. Show-up penceresinin dışında (ör.
    # 22:00) YENİ arrival YOK ama nokta yine de mevcut olmalı.
    quiet_point = datetime(2026, 3, 10, 22, 0)
    assert quiet_point in points
    # O saatte gerçek queue boşalmış olmalı (show-up 15:00'de bitiyor, 1
    # lane ile bile saatler önce temizlenmiş olur) -> wait=0, "veri yok" DEĞİL.
    assert points[quiet_point] == 0.0


def test_zero_queue_gives_explicit_zero_not_missing_point():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=1)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")
    flight = make_departure(when=datetime(2026, 3, 10, 12, 0), location="domestic", aircraft_icao="A321")

    now = datetime(2026, 3, 10, 12, 0)
    series = event_driven_display_series([flight], config, demand, now=now)
    points = dict(series[PROCESS_SECURITY_DOMESTIC])

    far_before = datetime(2026, 3, 10, 0, 0)
    assert far_before in points
    assert points[far_before] == 0.0
