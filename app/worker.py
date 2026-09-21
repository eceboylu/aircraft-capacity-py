"""
ADIM (Production Auto-Refresh Architecture) - arka planda, web server'dan
TAMAMEN AYRI bir process olarak çalışan ingestion/prediction worker'ı.

`app/web/server.py` (HTTP, sürekli çalışan servis) ile bu modül İKİ AYRI
process/systemd servisidir (bkz. `deploy/systemd/`) - biri çökerse/
yeniden başlarsa diğeri ETKİLENMEZ:

  - Web server SADECE DB'den OKUR (`app/queue/api.py`) - hiçbir ingestion/
    hesap YAPMAZ, bu yüzden worker'ın durumu web server'ın cevap
    verme yeteneğini hiç etkilemez.
  - Worker SADECE `app/queue/pipeline.py:run()`'ı (mevcut, DEĞİŞTİRİLMEDİ
    production zinciri: load -> parse -> refresh_flights ->
    run_predictions) periyodik çağırır - HTTP/socket açmaz.

Hata izolasyonu: `run_forever()`'daki HER döngü adımı kendi try/except'i
içindedir - `pipeline.run()` içinde YAKALANMAYAN bir hata (ör. kaynak
dosya bozuk, DB erişilemez) worker process'ini ÖLDÜRMEZ, sadece
loglanır (`logger.exception` - traceback dahil) ve bir SONRAKİ
döngüde tekrar denenir. Bu turda hiçbir YENİ veri yazılmadığı için
`QueuePrediction` tablosundaki EN SON başarılı çalışmanın sonucu
(upsert deseni - bkz. `engine.py:persist_predictions`) DEĞİŞMEDEN
kalır - "son başarılı prediction gösterilmeye devam etsin" (görev
Bölüm 8) bu yüzden EK bir mekanizma gerektirmez, mevcut upsert/
kalıcılık tasarımının doğal bir sonucudur.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from .queue import pipeline, retention
from .queue.engine import domain_now

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 5 * 60

# ADIM (Retention Cleanup Schedule) - Bölüm 9: 5 dakikalık refresh'ten
# TAMAMEN BAĞIMSIZ, AYRI bir cadence (48 saat). Bu sabit SADECE
# `run_forever()`'ın kendi retention-tetikleme zamanlayıcısı için -
# `app/queue/retention.py`'nin FLIGHT_RETENTION_DAYS/vb. config'iyle
# KARIŞTIRILMAZ (biri "ne sıklıkla temizleme dene", diğeri "hangi
# satırlar silinmeye uygun" - bkz. genel-proje.md Bölüm 8).
RETENTION_INTERVAL_SECONDS = 48 * 60 * 60


def run_forever(
    interval_seconds: float = REFRESH_INTERVAL_SECONDS,
    run_fn: Callable[[], dict] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    max_iterations: int | None = None,
    retention_interval_seconds: float = RETENTION_INTERVAL_SECONDS,
    retention_fn: Callable[[], dict] | None = None,
) -> int:
    """
    Sonsuz döngü - production'da `max_iterations=None` (varsayılan,
    HİÇBİR ZAMAN kendiliğinden durmaz). `run_fn`/`sleep_fn`/
    `max_iterations`/`retention_fn`/`retention_interval_seconds` SADECE
    test edilebilirlik için enjekte edilebilir (testler gerçek
    pipeline'ı/gerçek `time.sleep`'i/gerçek 48 saati çağırmadan, sınırlı
    sayıda döngüyü deterministik biçimde doğrulayabilir) - production
    çağrısı (`main()`) bunların HİÇBİRİNİ vermez, eski/gerçek davranış
    birebir korunur.

    `run_fn` verilmezse varsayılan `pipeline.run(now=domain_now(),
    apply_usage_horizon=True)`'dır (ADIM Re-Ingest Loop Prevention -
    Bölüm 15 + 48h Usage Horizon - Bölüm 8: gerçek "an" AÇIKÇA geçilir
    ki `load_flight_rows()`'un 48h ingestion-horizon reddi VE
    `flights_of_airport()`'un 48h usage-horizon filtresi production'da
    AKTİF olsun) - `run_forever()`'ın kendi döngü/sleep mantığı
    DEĞİŞMEDİ, her turda hâlâ TEK bir sıfır-argümanlı `run()` çağrısı
    yapılıyor (mevcut `run_fn` test-enjeksiyon sözleşmesi BOZULMADI).
    Her İKİ koruma da varsayılan `False`/`None` (devre dışı) - SADECE
    bu production çağrısı açıkça etkinleştirir, mevcut `pipeline.run()`/
    `run_predictions()` çağıranlarının (testler dahil) davranışı
    DEĞİŞMEDEN kalır.

    Retention (Bölüm 6/9): `run_fn`'in try/except'inden TAMAMEN AYRI,
    KENDİ try/except'i içinde çağrılır - retention hatası refresh
    loop'unu/bir SONRAKİ refresh turunu ASLA etkilemez. `retention_fn`
    verilmezse varsayılan `retention.run_cleanup_cycle` - bu fonksiyon
    `RETENTION_ENABLED=false` (varsayılan) iken DB'ye hiç dokunmadan
    hemen döner (bkz. `retention.py`). Cadence 5 dakikalık refresh
    sleep/interval hesabını HİÇ DEĞİŞTİRMEZ - ayrı bir monotonic sayaçla
    izlenir.

    Döner: bu çalıştırmada kaç REFRESH döngüsünün BAŞARIYLA (exception
    fırlatmadan) tamamlandığı (test/gözlemlenebilirlik amaçlı;
    production'da `max_iterations=None` olduğu için pratikte hiç
    dönmez). Retention çalıştırma sayısı bu sayaca dahil DEĞİLDİR.
    """
    run = run_fn or (lambda: pipeline.run(now=domain_now(), apply_usage_horizon=True))
    sleep = sleep_fn or time.sleep
    cleanup = retention_fn or retention.run_cleanup_cycle

    logger.info(
        "auto-refresh worker started (interval=%.0fs) - web server'dan AYRI process",
        interval_seconds,
    )

    completed = 0
    iteration = 0
    last_cleanup_monotonic = time.monotonic()
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        started = time.monotonic()
        try:
            summary = run()
            completed += 1
            logger.info("worker refresh #%d ok: %s", iteration, summary)
        except Exception:
            # BİLİNÇLİ geniş except - bu, worker'ın TEK hata izolasyon
            # sınırıdır (görev Bölüm 7/8): hangi türden bir hata geleceği
            # önceden bilinemez (ağ, disk, bozuk JSON, DB kilidi...),
            # hiçbiri worker process'ini SONLANDIRMAMALI. Web server
            # ayrı process olduğu için bundan HİÇ etkilenmez; DB'deki
            # en son başarılı `QueuePrediction` satırları (upsert
            # deseni) okunmaya devam eder.
            logger.exception(
                "worker refresh #%d BAŞARISIZ - son başarılı prediction'lar "
                "DB'de değişmeden kalıyor, %.0f sn sonra yeniden denenecek",
                iteration, interval_seconds,
            )

        # ADIM (Retention) - Bölüm 6/9: refresh'in başarılı/başarısız
        # olmasından TAMAMEN BAĞIMSIZ, KENDİ try/except'i - retention
        # hatası burada asla dışarı sızıp refresh loop'unu/`completed`
        # sayacını etkilemez.
        if time.monotonic() - last_cleanup_monotonic >= retention_interval_seconds:
            try:
                cleanup_summary = cleanup()
                logger.info("retention cleanup #%d ok: %s", iteration, cleanup_summary)
            except Exception:
                logger.exception(
                    "retention cleanup #%d BAŞARISIZ - refresh loop ETKİLENMEDİ, "
                    "bir sonraki retention cycle'da tekrar denenecek",
                    iteration,
                )
            finally:
                last_cleanup_monotonic = time.monotonic()

        elapsed = time.monotonic() - started
        remaining = max_iterations - iteration if max_iterations is not None else None
        if remaining == 0:
            break
        sleep(max(0.0, interval_seconds - elapsed))

    return completed


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
