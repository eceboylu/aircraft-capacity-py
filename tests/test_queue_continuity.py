"""
Madde 5.9 - kuyruk saat başında resetlenmez: bir saatte bırakılan
backlog bir SONRAKİ saate GERÇEKTEN taşınmalı (discrete-event FIFO
core'un kendi doğal davranışı - core/event_queue.py DEĞİŞTİRİLMEDİ, bu
test SADECE production fonksiyonunu (`simulate_fifo_queue` +
`_event_derived_backlog_by_hour`) çağırıp bu garantiyi kilitliyor).
"""
from datetime import datetime

from app.queue.core.event_queue import simulate_fifo_queue
from app.queue.engine import _event_derived_backlog_by_hour


def test_backlog_at_hour_boundary_is_not_reset_to_zero():
    # 12:55'te 50 kişilik bir yığın, 1 sunucu, 1 dk/servis -> 13:00'a
    # kadar sadece ~5 kişi servise başlayabilir; kalanı 13:00 sınırında
    # HÂLÂ kuyrukta/bekliyor olmalı, sıfıra düşmemeli.
    arrivals = [(datetime(2026, 3, 10, 12, 55), "departure", 50.0)]
    events = simulate_fifo_queue(arrivals, server_count=1, service_time_minutes=1.0)

    assert sum(e.count for e in events) == 50.0

    boundary_13 = datetime(2026, 3, 10, 13, 0)
    backlog = _event_derived_backlog_by_hour(events, [boundary_13])[boundary_13]

    assert backlog > 0
    # En fazla 5 kişi (12:55, 12:56, ..., 12:59 servis başlangıçları)
    # 13:00'dan ÖNCE servise başlamış olabilir - geri kalan >= 45 kişi
    # hâlâ backlog'da olmalı.
    assert backlog >= 44


def test_backlog_continues_into_next_hour_not_just_disappears():
    """
    13:00'da yeni arrival olmasa bile önceki backlog GEÇ tamamlanmaya
    devam eder - kuyruk "saat değişti" diye sıfırlanmaz.
    """
    arrivals = [(datetime(2026, 3, 10, 12, 55), "departure", 20.0)]
    events = simulate_fifo_queue(arrivals, server_count=1, service_time_minutes=1.0)

    completions_after_13 = [e for e in events if e.completion_time > datetime(2026, 3, 10, 13, 0)]
    assert completions_after_13, "backlog 13:00'dan sonra da işlenmeye devam etmeli"

    # Toplam conservation - hiçbir passenger kaybolmadı.
    assert sum(e.count for e in events) == 20.0
