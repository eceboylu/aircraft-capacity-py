"""
Madde 3/5.12 - risk threshold regresyon kilidi. Bu görev risk'e
DOKUNMADI - mevcut production sabitleri ve `risk_from_wait()` sınır
davranışı burada AYNEN kilitlenir.
"""
from app.queue.constants import (
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
    WAIT_RISK_HIGH_MINUTES,
    WAIT_RISK_LOW_MINUTES,
    WAIT_RISK_MEDIUM_MINUTES,
)
from app.queue.core.scoring import risk_from_wait


def test_current_risk_thresholds_unchanged():
    assert WAIT_RISK_LOW_MINUTES == 10
    assert WAIT_RISK_MEDIUM_MINUTES == 15
    assert WAIT_RISK_HIGH_MINUTES == 30


def test_risk_from_wait_none_is_unknown_not_low():
    assert risk_from_wait(None) == RISK_UNKNOWN


def test_risk_from_wait_boundaries():
    assert risk_from_wait(0.0) == RISK_LOW
    assert risk_from_wait(9.9) == RISK_LOW
    assert risk_from_wait(WAIT_RISK_LOW_MINUTES) == RISK_MEDIUM
    assert risk_from_wait(14.9) == RISK_MEDIUM
    assert risk_from_wait(WAIT_RISK_MEDIUM_MINUTES) == RISK_HIGH
    assert risk_from_wait(29.9) == RISK_HIGH
    assert risk_from_wait(WAIT_RISK_HIGH_MINUTES) == RISK_CRITICAL
    assert risk_from_wait(1000.0) == RISK_CRITICAL
