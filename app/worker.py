from __future__ import annotations

import logging
import os
import time
from typing import Callable

from .db import get_session
from .health import record_successful_refresh
from .queue import pipeline, retention
from .queue.engine import domain_now
from .queue.ingestion import airlabs_client
from .queue.ingestion.airlabs_client import TrackedAirportsConfigError, tracked_airports_from_env

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 5 * 60

# ADIM (Concurrency - Canonical Single-Writer Lock) - kullanıcı talebi:
# eski `fcntl.flock()` tabanlı worker-process kilidi (SADECE POSIX'te
# çalışıyordu, Windows'ta `WorkerLockError` fırlatıp worker'ı hiç
# BAŞLATAMIYORDU) KALDIRILDI - dead/duplicate bir lock sistemi olarak
# bırakılmadı. Artık TEK canonical write-lock `pipeline.py:
# _pipeline_writer_lock()` (MySQL `GET_LOCK`/`RELEASE_LOCK`, cross-
# platform) - `pipeline.run()`'ın KENDİSİNİN içinde, worker/manuel
# çağrı/CLI/test FARK ETMEKSİZİN. İki worker instance'ı (veya bir worker
# + manuel bir `pipeline.run()`) aynı anda çalışırsa, `run_forever()`'ın
# zaten var olan `try/except Exception` döngüsü (aşağıda) ikinci
# tarafın `PipelineLockError`'ını GÜVENLİ şekilde yakalayıp bir sonraki
# interval'da tekrar dener - crash/duplicate write YOK.
RETENTION_INTERVAL_SECONDS = 48 * 60 * 60


def run_forever(
    interval_seconds: float = REFRESH_INTERVAL_SECONDS,
    run_fn: Callable[[], dict] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    max_iterations: int | None = None,
    retention_interval_seconds: float = RETENTION_INTERVAL_SECONDS,
    retention_fn: Callable[[], dict] | None = None,
) -> int:
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
            logger.exception(
                "worker refresh #%d BAŞARISIZ - son başarılı prediction'lar "
                "DB'de değişmeden kalıyor, %.0f sn sonra yeniden denenecek",
                iteration, interval_seconds,
            )

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


class AirLabsConfigError(RuntimeError):
    pass


def _build_live_run_fn():
    if not os.environ.get("AIRLABS_API_KEY"):
        raise AirLabsConfigError(
            "AIRLABS_API_KEY tanımlı değil - production worker AirLabs "
            "olmadan başlatılamaz (generated/static JSON fallback YOK)."
        )
    try:
        airports = tracked_airports_from_env()
    except TrackedAirportsConfigError as exc:
        raise AirLabsConfigError(str(exc)) from exc

    logger.info(
        "AirLabs production source configured: tracked_airports=%s",
        airports,
    )
    source_a = airlabs_client.build_source_a(airports)
    source_b = airlabs_client.build_source_b()
    return lambda: pipeline.run(
        now=domain_now(), apply_usage_horizon=True,
        source_a=source_a, source_b=source_b,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        production_run_fn = _build_live_run_fn()
    except AirLabsConfigError:
        logger.exception(
            "AirLabs production config geçersiz - worker BAŞLATILMIYOR "
            "(fail-fast, generated/static JSON'a fallback YOK, hiçbir "
            "HTTP isteği yapılmadı)"
        )
        return 1

    # ADIM (Concurrency) - write-lock artık `pipeline.run()`'ın kendi
    # içinde (bkz. `pipeline.py:_pipeline_writer_lock`) - burada ayrı bir
    # worker-process kilidi YOK (kaldırıldı, yukarıdaki not).
    run_forever(run_fn=production_run_fn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
