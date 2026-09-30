"""
ADIM (Concurrency - Canonical Single-Writer Lock) - kullanıcı talebi:
"aynı anda sadece 1 writer", worker/manuel `pipeline.run()`/CLI/test
FARK ETMEKSİZİN AYNI mekanizmayı (MySQL `GET_LOCK`/`RELEASE_LOCK`,
cross-platform) kullanmalı. `GET_LOCK` saf bir DB-seviyesi named-lock
mekanizması olduğu için `db_session` (schema drop/create) fixture'ı
GEREKMİYOR - doğrudan gerçek `app.db.engine`'e karşı test ediliyor.
"""
from sqlalchemy import text

from app.db import engine as _mysql_engine
from app.queue.pipeline import (
    PIPELINE_LOCK_NAME,
    PipelineLockError,
    _pipeline_writer_lock,
)


def _lock_held_by_other_connection() -> bool:
    """Ayrı, uzun ömürlü bir connection'da lock'u tutar - test bitince
    connection kapatılınca MySQL otomatik serbest bırakır."""
    holder = _mysql_engine.connect()
    got = holder.execute(
        text("SELECT GET_LOCK(:name, 0)"), {"name": PIPELINE_LOCK_NAME}
    ).scalar()
    assert got == 1, "test setup: lock'un ÖNCEDEN tutulması gerekiyordu"
    return holder


def test_first_run_acquires_lock_successfully():
    entered = False
    with _pipeline_writer_lock(timeout_seconds=0):
        entered = True
    assert entered


def test_second_concurrent_run_cannot_acquire_lock():
    holder = _lock_held_by_other_connection()
    try:
        try:
            with _pipeline_writer_lock(timeout_seconds=0):
                raise AssertionError("lock BAŞKA bir connection'da tutulurken alınmamalıydı")
        except PipelineLockError:
            pass  # beklenen: fail-fast
    finally:
        holder.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": PIPELINE_LOCK_NAME})
        holder.close()


def test_lock_releases_after_successful_run():
    with _pipeline_writer_lock(timeout_seconds=0):
        pass
    # Release sonrası BAŞKA bir "run" hemen lock'u alabilmeli.
    with _pipeline_writer_lock(timeout_seconds=0):
        pass


def test_lock_releases_after_exception_inside_run():
    class _Boom(Exception):
        pass

    try:
        with _pipeline_writer_lock(timeout_seconds=0):
            raise _Boom("simulated calculation failure")
    except _Boom:
        pass

    # Exception sonrası lock GERÇEKTEN serbest kalmış olmalı - yeni bir
    # run hemen almayı başarabilmeli (stale lock KALMAMALI).
    entered_again = False
    with _pipeline_writer_lock(timeout_seconds=0):
        entered_again = True
    assert entered_again


def test_no_double_writer_only_one_of_two_concurrent_attempts_succeeds():
    """Bölüm 20 - en kritik acceptance: iki eşzamanlı 'run' aynı anda
    write yapamaz, sadece biri kazanır."""
    holder = _lock_held_by_other_connection()
    second_attempt_succeeded = False
    try:
        try:
            with _pipeline_writer_lock(timeout_seconds=0):
                second_attempt_succeeded = True
        except PipelineLockError:
            second_attempt_succeeded = False
    finally:
        holder.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": PIPELINE_LOCK_NAME})
        holder.close()

    assert second_attempt_succeeded is False


def test_worker_module_has_no_dead_fcntl_lock():
    """Bölüm 18 - eski Windows-uyumsuz `fcntl` tabanlı kilit TAMAMEN
    kaldırılmış olmalı, dead/duplicate bir lock sistemi kalmamalı."""
    import app.worker as worker_module

    assert not hasattr(worker_module, "_acquire_worker_lock")
    assert not hasattr(worker_module, "WorkerLockError")
    assert not hasattr(worker_module, "fcntl")
