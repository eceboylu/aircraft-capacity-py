"""
PHASE 5 - `app/queue/core/scoring.py` scale-derived server/lane sayıları
ve referans (sanity-check) kapasite oranları.

ÖNEMLİ (görev talimatı): saatlik kapasite değerleri SADECE referans/
sanity-check'tir - queue wait hesabı bu bölme formülüyle YAPILMAZ,
gerçek progression event-driven FIFO'dur (bkz. core/event_queue.py,
bu dosyada DEĞİŞTİRİLMEDİ).
"""
from app.queue.config import default_config
from app.queue.core.scoring import (
    passport_arrival_capacity_rate,
    passport_arrival_server_count,
    passport_departure_capacity_rate,
    passport_departure_server_count,
    passport_server_count,
    queue_capacity_rate,
)


def _config(scale):
    return default_config("XXX", scale=scale)


def test_large_server_counts():
    """ADIM (Generic Scale Resource Update) - değerler güncellendi (bkz. rapor)."""
    c = _config("large")
    assert passport_departure_server_count(c) == 30
    assert passport_arrival_server_count(c) == 45
    assert c.domestic_security_lane_count == 28
    assert c.international_security_lane_count == 18


def test_medium_server_counts():
    c = _config("medium")
    assert passport_departure_server_count(c) == 10
    assert passport_arrival_server_count(c) == 15
    assert c.domestic_security_lane_count == 12
    assert c.international_security_lane_count == 6


def test_small_server_counts():
    c = _config("small")
    assert passport_departure_server_count(c) == 3
    assert passport_arrival_server_count(c) == 4
    assert c.domestic_security_lane_count == 4
    assert c.international_security_lane_count == 2


def test_pool_parameter_matches_dedicated_functions():
    c = _config("large")
    assert passport_server_count(c, "departure") == passport_departure_server_count(c) == 30
    assert passport_server_count(c, "arrival") == passport_arrival_server_count(c) == 45


# ========================================================================
# Referans saatlik kapasite (SADECE sanity-check - queue wait formülü
# DEĞİL). Passport = 1.5 dk/pax, security = 1 dk/pax.
# ========================================================================

def test_large_reference_hourly_capacity():
    c = _config("large")
    assert round(passport_departure_capacity_rate(c) * 60) == 1200  # 30 * 60/1.5
    assert round(passport_arrival_capacity_rate(c) * 60) == 1800    # 45 * 60/1.5
    assert round(queue_capacity_rate(c.domestic_security_lane_count, c.security_service_time_minutes) * 60) == 1680  # 28*60/1
    assert round(queue_capacity_rate(c.international_security_lane_count, c.security_service_time_minutes) * 60) == 1080  # 18*60/1


def test_medium_reference_hourly_capacity():
    c = _config("medium")
    assert round(passport_departure_capacity_rate(c) * 60) == 400   # 10 * 60/1.5
    assert round(passport_arrival_capacity_rate(c) * 60) == 600     # 15 * 60/1.5
    assert round(queue_capacity_rate(c.domestic_security_lane_count, c.security_service_time_minutes) * 60) == 720  # 12*60


def test_small_reference_hourly_capacity():
    c = _config("small")
    assert round(passport_departure_capacity_rate(c) * 60) == 120   # 3 * 60/1.5
    assert round(passport_arrival_capacity_rate(c) * 60) == 160     # 4 * 60/1.5
    assert round(queue_capacity_rate(c.domestic_security_lane_count, c.security_service_time_minutes) * 60) == 240  # 4*60


def test_departure_and_arrival_capacity_rates_are_independent():
    """large'da departure(30) != arrival(45) - PAYLAŞILAN tek sayı YOK."""
    c = _config("large")
    assert passport_departure_capacity_rate(c) != passport_arrival_capacity_rate(c)
