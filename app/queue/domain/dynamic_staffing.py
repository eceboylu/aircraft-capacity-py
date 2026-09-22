"""
ADIM (Dynamic Capacity / Scoring Consistency) - `core/event_queue.py:
simulate_fifo_queue_dynamic()`'in ürettiği (checkpoint_time, aktif_
server_sayısı) programını, `core/scoring.py:queue_capacity_model()`'in
tükettiği TEK bir "bu saatin efektif server sayısı" değerine çevirir.

SAF KATMAN: veritabanına/dosyaya/ağa dokunmaz - sadece saf zaman/liste
aritmetiği.

NEDEN GEREKLİ: dynamic staffing devredeyken, bir saatlik pencere
İÇİNDE aktif server sayısı değişebilir (ör. 30 → 35 → 40). Queue
simülasyonu bu değişimi GERÇEK zamanında (dakika hassasiyetinde) zaten
uyguluyor - ama `queue_capacity_model()`'in `utilization`/`queue_
pressure`/`capacity` hesapları TEK bir server sayısı bekler. Bu
fonksiyon o saat için ZAMAN-AĞIRLIKLI ortalama aktif server sayısını
üretir, böylece "simüle edilen servis kapasitesi" ile "raporlanan
skorlama kapasitesi" AYNI operasyonel gerçeği temsil eder (Bölüm 9/10).
"""

from __future__ import annotations

from datetime import datetime, timedelta


def effective_capacity_by_hour(
    schedule: list[tuple[datetime, int]],
    window_starts: list[datetime],
    window_minutes: int = 60,
) -> dict[datetime, float]:
    """
    Her `window_starts` penceresi için, `schedule`'dan (sıralı,
    `simulate_fifo_queue_dynamic()`'in döndürdüğü (checkpoint_time,
    aktif_server_sayısı) çiftleri) zaman-ağırlıklı ortalama aktif
    server sayısını hesaplar.

    Örnek (Bölüm 10): bir saat içinde 00-20dk=30, 20-40dk=40, 40-60dk=50
    ise efektif = (20/60*30)+(20/60*40)+(20/60*50) = 40.

    `schedule` boşsa (dynamic hiç devreye girmediyse - ör. hiç arrival
    yoktu) boş sözlük döner - çağıran taraf kendi statik varsayılanına
    düşer (bu fonksiyon UYDURMA bir değer ÜRETMEZ).

    Bir pencerenin BAŞLADIĞI andan ÖNCEKİ son checkpoint, o pencerenin
    başındaki aktif sayıyı belirler ("step fonksiyonu" - checkpoint,
    KENDİ zamanından bir SONRAKİ checkpoint'e kadar geçerlidir);
    `schedule`'ın kapsamadığı (ilk checkpoint'ten önceki) bir an için
    `schedule[0]`'ın sayısı kullanılır - queue hiçbir zaman "server
    sayısı yok" durumuna düşmez (ilk checkpoint = sim başlangıcı =
    `default_server_count`).
    """
    if not schedule:
        return {}

    ordered = sorted(schedule, key=lambda item: item[0])
    result: dict[datetime, float] = {}

    for window_start in window_starts:
        window_end = window_start + timedelta(minutes=window_minutes)
        weighted_sum = 0.0
        # Pencere İÇİNDEKİ tüm checkpoint geçişlerini, her segmentin
        # penceredeki süresiyle ağırlıklandırarak topla.
        segment_start = window_start
        current_count = ordered[0][1]
        for checkpoint_time, count in ordered:
            if checkpoint_time <= window_start:
                current_count = count
                continue
            if checkpoint_time >= window_end:
                break
            segment_minutes = (checkpoint_time - segment_start).total_seconds() / 60.0
            weighted_sum += segment_minutes * current_count
            segment_start = checkpoint_time
            current_count = count

        remaining_minutes = (window_end - segment_start).total_seconds() / 60.0
        weighted_sum += remaining_minutes * current_count

        result[window_start] = weighted_sum / window_minutes

    return result
