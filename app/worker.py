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

from .queue import pipeline

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 5 * 60


def run_forever(
    interval_seconds: float = REFRESH_INTERVAL_SECONDS,
    run_fn: Callable[[], dict] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    max_iterations: int | None = None,
) -> int:
    """
    Sonsuz döngü - production'da `max_iterations=None` (varsayılan,
    HİÇBİR ZAMAN kendiliğinden durmaz). `run_fn`/`sleep_fn`/
    `max_iterations` SADECE test edilebilirlik için enjekte edilebilir
    (testler gerçek pipeline'ı/gerçek `time.sleep`'i çağırmadan, sınırlı
    sayıda döngüyü deterministik biçimde doğrulayabilir) - production
    çağrısı (`main()`) bunların HİÇBİRİNİ vermez, eski/gerçek davranış
    birebir korunur.

    Döner: bu çalıştırmada kaç döngünün BAŞARIYLA (exception fırlatmadan)
    tamamlandığı (test/gözlemlenebilirlik amaçlı; production'da
    `max_iterations=None` olduğu için pratikte hiç dönmez).
    """
    run = run_fn or pipeline.run
    sleep = sleep_fn or time.sleep

    logger.info(
        "auto-refresh worker started (interval=%.0fs) - web server'dan AYRI process",
        interval_seconds,
    )

    completed = 0
    iteration = 0
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
