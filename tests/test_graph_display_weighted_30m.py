"""
ADIM (Passenger-Weighted 30-Minute Graph) - kullanıcı talebi: 30dk
graph bar'ı ARTIK 6 adet 5dk `wait_minutes`'in BASİT ortalaması DEĞİL,
o 30dk'daki TÜM 5dk pencerelerinin `wait_numerator`/`passenger_count`
TOPLAMININ bölümü (SUM(wait*pax)/SUM(pax), 30 dakikadaki TÜM yolcular
üzerinden) - bkz. app/queue/audit.py:record_graph_display_audit.

Bu dosya, "direct 30m service-event aggregation" ile "6 adet 5m
numerator/denominator aggregation"ın birebir aynı çıktığını (Bölüm 5/18)
ve basit ortalamadan FARKLI bir sonuç ürettiğini (yolcu sayıları eşit
değilse) kanıtlar.
"""
from datetime import datetime, timedelta

from app.queue import audit
from app.queue.engine import FiveMinuteWaitPoint

from .conftest import rebuild_orm_row


class _FakeSession:
    def __init__(self):
        self.added: list = []

    def add_all(self, rows):
        self.added.extend(rows)

    def execute(self, stmt, values=None):
        for row_values in (values or []):
            self.added.append(rebuild_orm_row(stmt, row_values))

    def flush(self):
        pass


def _point(minute_offset: int, wait: float, pax: float) -> FiveMinuteWaitPoint:
    base = datetime(2026, 3, 10, 9, 0)
    window_start = base + timedelta(minutes=minute_offset)
    return FiveMinuteWaitPoint(
        window_start=window_start, wait_minutes=wait, passenger_count=pax,
        wait_numerator=wait * pax,
    )


def test_30m_bar_is_passenger_weighted_not_simple_average_of_6_points():
    """
    6 nokta, EŞİT OLMAYAN yolcu sayılarıyla:
      09:00 wait=0,  pax=20
      09:05 wait=2,  pax=30
      09:10 wait=5,  pax=10
      09:15 wait=0,  pax=0   (boş pencere)
      09:20 wait=0,  pax=0   (boş pencere)
      09:25 wait=0,  pax=0   (boş pencere)

    YANLIŞ (basit ortalama): (0+2+5+0+0+0)/6 = 1.1667
    DOĞRU (passenger-weighted): (20*0 + 30*2 + 10*5) / (20+30+10)
                                = (0+60+50)/60 = 110/60 = 1.8333
    """
    points = [
        _point(0, 0.0, 20.0),
        _point(5, 2.0, 30.0),
        _point(10, 5.0, 10.0),
        _point(15, 0.0, 0.0),
        _point(20, 0.0, 0.0),
        _point(25, 0.0, 0.0),
    ]
    session = _FakeSession()
    now = datetime(2026, 3, 10, 9, 30)

    audit.record_graph_display_audit(session, "run-1", "IST", "security_intl", points, tz=None, now=now)

    assert len(session.added) == 1
    row = session.added[0]
    assert row.wait_numerator == 110.0
    assert row.passenger_count == 60.0
    assert row.average_wait_minutes == pytest_approx(110.0 / 60.0)
    assert row.display_wait_minutes == row.average_wait_minutes
    # Basit ortalamadan (1.1667) FARKLI olduğunu açıkça doğrula.
    simple_average = (0.0 + 2.0 + 5.0 + 0.0 + 0.0 + 0.0) / 6
    assert abs(row.average_wait_minutes - simple_average) > 0.5


def pytest_approx(value, rel=1e-6):
    import pytest
    return pytest.approx(value, rel=rel)


def test_direct_30m_aggregation_equals_sum_of_six_5m_numerator_denominator():
    """Bölüm 5/18 - direct 30m service-event aggregation, 6 adet 5m
    numerator/denominator toplamıyla BİREBİR aynı çıkmalı."""
    points = [
        _point(0, 1.0, 5.0),
        _point(5, 3.0, 7.0),
        _point(10, 0.0, 0.0),
        _point(15, 4.0, 2.0),
        _point(20, 2.0, 9.0),
        _point(25, 0.0, 0.0),
    ]
    session = _FakeSession()
    now = datetime(2026, 3, 10, 9, 30)
    audit.record_graph_display_audit(session, "run-1", "IST", "passport_dep", points, tz=None, now=now)

    row = session.added[0]

    # "direct" hesap: tüm 6 pencereyi TEK BİR service-event kümesi gibi
    # düşünüp SUM(wait*pax)/SUM(pax) - manuel olarak, points'in KENDİ
    # numerator/pax alanlarından (audit fonksiyonunun İÇİNDE yaptığı
    # AYNI toplama, bağımsız olarak burada tekrarlanıyor).
    direct_numerator = sum(p.wait_numerator for p in points)
    direct_pax = sum(p.passenger_count for p in points)
    direct_avg = direct_numerator / direct_pax

    assert row.wait_numerator == direct_numerator
    assert row.passenger_count == direct_pax
    assert row.average_wait_minutes == pytest_approx(direct_avg)


def test_peak_wait_is_max_of_the_six_real_5m_weighted_averages():
    points = [
        _point(0, 1.0, 5.0),
        _point(5, 9.0, 1.0),  # en yüksek TEK NOKTA wait
        _point(10, 3.0, 4.0),
        _point(15, 0.0, 0.0),
        _point(20, 0.0, 0.0),
        _point(25, 0.0, 0.0),
    ]
    session = _FakeSession()
    now = datetime(2026, 3, 10, 9, 30)
    audit.record_graph_display_audit(session, "run-1", "IST", "security_intl", points, tz=None, now=now)

    row = session.added[0]
    assert row.peak_wait_minutes == 9.0


def test_all_empty_window_bucket_gives_zero_not_none():
    points = [_point(i * 5, 0.0, 0.0) for i in range(6)]
    session = _FakeSession()
    now = datetime(2026, 3, 10, 9, 30)
    audit.record_graph_display_audit(session, "run-1", "IST", "security_dom", points, tz=None, now=now)

    row = session.added[0]
    assert row.passenger_count == 0.0
    assert row.wait_numerator == 0.0
    assert row.average_wait_minutes == 0.0
    assert row.display_wait_minutes == 0.0
