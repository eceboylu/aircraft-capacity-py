"""
ADIM (SQL Audit/Traceability Genişletme, Bölüm 16) - `DynamicStaffing
Checkpoint.lookahead_demand`/`needed_servers` normal(5dk)+extended(20dk,
sadece passport_arr) pencerelerin TOPLAMINI/BÜYÜĞÜNÜ taşıyordu; hangi
pencerenin karara yol açtığı artık `normal_lookahead_demand`/`extended_
lookahead_demand`/`normal_needed_servers`/`extended_needed_servers`/
`winning_forecast` ile AYRI görülebiliyor. Production kararı (`new_count`/
`ramp`/`target_operational_level`) bu testlerde DE doğrulanır - yeni
alanlar SADECE gözlem, karar mantığını DEĞİŞTİRMEZ.
"""
from datetime import datetime, timedelta

from app.queue.core.event_queue import DynamicStaffingParams, simulate_fifo_queue_dynamic


def _params(**overrides):
    base = dict(
        default_server_count=5, max_server_count=20,
        control_interval_minutes=5, look_ahead_minutes=5,
        target_utilization=1.0, allowed_levels=(5, 10, 15, 20),
        extended_look_ahead_minutes=20,
    )
    base.update(overrides)
    return DynamicStaffingParams(**base)


def test_extended_forecast_wins_when_demand_is_beyond_normal_window():
    base = datetime(2026, 3, 10, 8, 0)
    arrivals = [
        (base, "arrival", 1.0),
        (base + timedelta(minutes=20), "arrival", 300.0),
    ]
    _events, _schedule, checkpoint_log = simulate_fifo_queue_dynamic(
        arrivals, _params(), service_time_minutes=1.0,
    )
    first = checkpoint_log[0]
    assert first.checkpoint_time == base + timedelta(minutes=5)
    assert first.normal_lookahead_demand == 0.0
    assert first.extended_lookahead_demand == 300.0
    assert first.normal_needed_servers == 0
    assert first.extended_needed_servers == 15
    assert first.winning_forecast == "extended"
    # Kombine (eski/geriye dönük) alanlar HALA normal+extended toplamı -
    # yeni alanlar EKLENDİ, mevcut davranış DEĞİŞMEDİ.
    assert first.lookahead_demand == first.normal_lookahead_demand + first.extended_lookahead_demand
    assert first.needed_servers == max(first.normal_needed_servers, first.extended_needed_servers)


def test_normal_forecast_wins_when_demand_is_inside_short_window():
    base = datetime(2026, 3, 10, 8, 0)
    arrivals = [
        (base, "arrival", 1.0),
        (base + timedelta(minutes=8), "arrival", 300.0),
    ]
    _events, _schedule, checkpoint_log = simulate_fifo_queue_dynamic(
        arrivals, _params(), service_time_minutes=1.0,
    )
    first = checkpoint_log[0]
    assert first.checkpoint_time == base + timedelta(minutes=5)
    assert first.normal_lookahead_demand == 300.0
    assert first.extended_lookahead_demand == 300.0
    assert first.normal_needed_servers == 60
    assert first.extended_needed_servers == 15
    assert first.winning_forecast == "normal"
    assert first.needed_servers == max(first.normal_needed_servers, first.extended_needed_servers)


def test_no_extended_window_always_reports_normal_winning():
    """security_intl/passport_dep - `extended_look_ahead_minutes=None` -
    extended_* alanları hep 0/normal kalmalı (davranış DEĞİŞMEDİ)."""
    base = datetime(2026, 3, 10, 8, 0)
    arrivals = [
        (base, "arrival", 1.0),
        (base + timedelta(minutes=3), "arrival", 20.0),
    ]
    _events, _schedule, checkpoint_log = simulate_fifo_queue_dynamic(
        arrivals, _params(extended_look_ahead_minutes=None), service_time_minutes=1.0,
    )
    first = checkpoint_log[0]
    assert first.extended_lookahead_demand == 0.0
    assert first.extended_needed_servers == 0
    assert first.winning_forecast == "normal"
    assert first.lookahead_demand == first.normal_lookahead_demand
