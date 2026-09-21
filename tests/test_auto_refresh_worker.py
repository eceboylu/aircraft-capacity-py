"""
ADIM (Production Auto-Refresh Architecture) - `app/worker.py` testleri.

Gerçek `pipeline.run()`/gerçek `time.sleep` HİÇ çağrılmaz - `run_fn`/
`sleep_fn` enjekte edilerek worker'ın döngü/hata-izolasyon MANTIĞI
deterministik olarak test edilir (bkz. `run_forever()` docstring'i).
"""
import logging

import pytest

from app import worker


def test_run_forever_calls_run_fn_max_iterations_times():
    calls = []

    def fake_run():
        calls.append(1)
        return {"predictions": 1}

    sleeps = []
    completed = worker.run_forever(
        interval_seconds=300, run_fn=fake_run, sleep_fn=sleeps.append, max_iterations=3,
    )
    assert len(calls) == 3
    assert completed == 3
    # Son iterasyondan sonra UYUMAZ (gereksiz bekleme yok) - 3 iterasyon -> 2 sleep.
    assert len(sleeps) == 2


def test_run_forever_survives_exception_and_continues_next_cycle(caplog):
    """Bölüm 7/8 - bir turdaki hata worker'ı DURDURMAZ, loglanır, SONRAKİ tur çalışır."""
    calls = {"n": 0}

    def flaky_run():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kaynak dosya okunamadı")
        return {"predictions": 5}

    with caplog.at_level(logging.ERROR, logger="app.worker"):
        completed = worker.run_forever(
            interval_seconds=300, run_fn=flaky_run, sleep_fn=lambda s: None, max_iterations=2,
        )

    assert calls["n"] == 2          # ikinci tur GERÇEKTEN çalıştı
    assert completed == 1           # sadece 1 tur BAŞARIYLA tamamlandı (2. tur hata verdi)
    assert any("BAŞARISIZ" in r.message for r in caplog.records)


def test_run_forever_never_raises_even_if_every_cycle_fails():
    def always_fails():
        raise ValueError("DB kilitli")

    # Worker process'i (bu fonksiyon çağrısı) hiçbir şekilde exception
    # FIRLATMAMALI - hata her turda İÇERİDE yakalanıp loglanır.
    completed = worker.run_forever(
        interval_seconds=300, run_fn=always_fails, sleep_fn=lambda s: None, max_iterations=3,
    )
    assert completed == 0


def test_run_forever_sleeps_the_configured_interval_between_cycles():
    sleep_calls = []

    def instant_run():
        return {}

    worker.run_forever(
        interval_seconds=300, run_fn=instant_run, sleep_fn=sleep_calls.append, max_iterations=2,
    )
    assert len(sleep_calls) == 1
    # Çalıştırma anlık olduğu için (mock) uyku süresi ~tam interval'a yakın olmalı.
    assert sleep_calls[0] == pytest.approx(300, abs=1.0)


def test_default_interval_is_five_minutes():
    assert worker.REFRESH_INTERVAL_SECONDS == 5 * 60
