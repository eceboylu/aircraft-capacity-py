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
import os
import time
from typing import Callable

from .db import get_session
from .health import record_successful_refresh
from .queue import pipeline, retention
from .queue.engine import domain_now

# ADIM (Worker Single-Instance Lock) - `fcntl` POSIX-only (Linux/systemd
# production hedefi - bkz. deploy/systemd/). Windows gibi fcntl'siz bir
# platformda modülün KENDİSİ (import app.worker) hâlâ sorunsuz import
# edilebilmeli - mevcut testler (`tests/test_auto_refresh_worker.py`,
# `tests/test_auto_ingestion_worker_integration.py`) SADECE `run_forever()`'ı
# doğrudan çağırır, `main()`'e hiç dokunmaz - bu yüzden import-time bir
# ImportError yerine BURADA sessizce None'a düşülür; gerçek kilitleme
# denemesi (`_acquire_worker_lock()`, SADECE `main()` içinden çağrılır)
# fcntl yoksa AÇIKÇA `WorkerLockError` fırlatır (aşağıya bkz.) - "sessizce
# korumasız devam etme" kuralı bu şekilde korunur.
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows/non-POSIX dev ortamı
    fcntl = None

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 5 * 60

# ADIM (Worker Single-Instance Lock) - Bölüm: aynı host'ta yanlışlıkla
# ikinci bir `python -m app.worker` başlatılırsa iki worker'ın AYNI ANDA
# DB'ye yazmasını engeller (bkz. deployment audit raporu - cold-start
# `database is locked` çökmesi VE steady-state `flight_key` upsert race'i
# GERÇEKTEN reproduce edildi). `/run` systemd'nin `RuntimeDirectory=`
# ile OLUŞTURDUĞU, servis user'ına (`ProtectSystem=strict` altında bile)
# yazılabilir tmpfs dizinidir (bkz. deploy/systemd/airport-queue-worker.service)
# - reboot'ta da otomatik temizlenir, ekstra bir "eski kilit" riski katmaz.
WORKER_LOCK_PATH = os.environ.get("WORKER_LOCK_PATH", "/run/airport-queue/worker.lock")


class WorkerLockError(RuntimeError):
    """
    Kilit dosyası/dizini HİÇ AÇILAMADI (izin, eksik RuntimeDirectory,
    fcntl bu platformda YOK, vb.) - Bölüm (Fail-Safe): bu durumda worker
    SESSİZCE korumasız (kilitsiz) çalışmaya asla DEVAM ETMEZ; `main()`
    bunu AÇIK bir başlangıç hatası olarak ele alıp süreci BAŞLATMADAN
    durdurmalı - "startup should fail visibly rather than run unprotected".
    """


def _acquire_worker_lock(path: str = WORKER_LOCK_PATH):
    """
    Bu host üzerinde AYNI ANDA yalnızca TEK bir worker'ın DB'ye
    yazmasını garanti eden POSIX dosya kilidi (`flock(2)`, exclusive,
    non-blocking - `LOCK_EX | LOCK_NB`).

    NEDEN PID-DOSYASI DEĞİL: bir PID dosyası process beklenmedik
    şekilde (kill -9, OOM, segfault) ölürse STALE kalabilir - kernel
    onu OTOMATİK temizlemez, bir sonraki başlatma "zaten çalışıyor"
    sanıp YANLIŞLIKLA çıkabilir (deadlock). `flock` ise dosyayı tutan
    file descriptor'ı process SONLANDIĞINDA (normal çıkış, kill -9,
    crash - HEPSİ dahil) kernel tarafından OTOMATİK serbest bırakılır -
    stale-lock riski YAPISAL OLARAK yok (bkz. Senaryo C doğrulaması).

    Döner:
      (lock_file, None)        - kilit BAŞARIYLA alındı. `lock_file`
                                  process ömrü boyunca AÇIK TUTULMALI -
                                  kapatılırsa/GC edilirse kilit HEMEN
                                  serbest kalır (bkz. `main()`).
      (None, "duplicate")      - kilit BAŞKA bir process'te - normal,
                                  beklenen "ikinci worker" durumu.

    fcntl bu platformda YOKSA VEYA kilit dosyası/dizini (izin, eksik
    dizin) hiç AÇILAMIYORSA `WorkerLockError` fırlatır - bkz. o
    sınıfın docstring'i.
    """
    if fcntl is None:
        raise WorkerLockError(
            "worker single-instance lock requires POSIX flock() (fcntl module) "
            "- not available on this platform/interpreter."
        )

    try:
        lock_dir = os.path.dirname(path)
        if lock_dir:
            os.makedirs(lock_dir, exist_ok=True)
        lock_file = open(path, "a+")
    except OSError as exc:
        raise WorkerLockError(
            f"worker lock file could not be opened at {path!r}: {exc}"
        ) from exc

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None, "duplicate"

    return lock_file, None

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

            # ADIM (Health / Stale-Data Visibility) - refresh'in KENDİSİNDEN
            # TAMAMEN AYRI bir try/except: heartbeat-yazma hatası (ör. bu
            # tek DB round-trip'i başarısız olursa) ASLA "refresh başarılı
            # oldu" sonucunu maskelemez/geri almaz - `completed` sayacı ve
            # yukarıdaki log zaten kesinleşti. Sadece GET /health'in
            # göreceği "son başarılı refresh" bilgisi bu turda
            # güncellenmemiş olur, bir sonraki başarılı turda düzelir.
            try:
                status_session = get_session()
                try:
                    record_successful_refresh(status_session)
                finally:
                    status_session.close()
            except Exception:
                logger.exception(
                    "worker refresh #%d heartbeat yazılamadı (refresh'in KENDİSİ "
                    "başarılıydı, sadece /health durumu bu turda güncellenmedi)",
                    iteration,
                )
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

    # ADIM (Worker Single-Instance Lock) - SADECE `main()` (gerçek process
    # giriş noktası) kilit alır - `run_forever()`'ın kendisi DEĞİŞMEDİ,
    # mevcut testler (`tests/test_auto_refresh_worker.py`,
    # `tests/test_auto_ingestion_worker_integration.py`) `run_forever()`'ı
    # DOĞRUDAN çağırdığı için kilitten hiç ETKİLENMEZ.
    try:
        lock_file, reason = _acquire_worker_lock()
    except WorkerLockError:
        logger.exception(
            "worker lock could not be acquired at %s - refusing to start "
            "unprotected (fail-safe)", WORKER_LOCK_PATH,
        )
        return 1

    if lock_file is None:
        logger.info("Another airport queue worker is already running; exiting.")
        return 0

    logger.info("worker lock acquired (%s)", WORKER_LOCK_PATH)
    try:
        run_forever()
    finally:
        lock_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
