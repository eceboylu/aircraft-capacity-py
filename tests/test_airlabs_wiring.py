"""
ADIM (AirLabs Production Wiring) - bu dosya SADECE bu ADIM'da eklenen
YENİ kodu test eder:

  - `airlabs_client.tracked_airports_from_env()` (yeni fonksiyon)
  - `app/worker.py:_build_live_run_fn()`/`main()`'in fail-fast config
    doğrulaması (yeni)
  - `app/health.py`'nin `source_mode`/`source_live_ingestion_configured`
    alanlarının AirLabs-configured durumu doğru yansıtması (yeni)

`airlabs_client`'ın KENDİSİNİN (HTTP/pagination/retry/backoff/auth/
malformed-JSON/partial-failure) davranışı `tests/test_airlabs_client.py`
tarafından ZATEN kapsamlı biçimde test ediliyor - burada TEKRARLANMAZ.
T0/T1/T2 gerçek pipeline zincirinin (flight identity/update/aircraft
enrichment) kendisi `tests/test_operational_dataset.py` ve
kardeşlerinde `tests/airlabs_operational_source.py` ile ZATEN
kanıtlanmış - burada SADECE `worker.py`'nin bu makineyi doğru inşa
ettiği (aynı, mevcut, KANITLANMIŞ enjeksiyon yoluyla) doğrulanır.

Gerçek ağa HİÇ çıkılmaz.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.queue.pipeline as pipeline_module
import app.worker as worker_module
from app.models import Base
from app.queue.ingestion import airlabs_client
from app.queue.ingestion.airlabs_client import TrackedAirportsConfigError, tracked_airports_from_env
from app.queue.ingestion.airports_import import import_airports
from app.queue.models import Flight
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset

from . import airlabs_operational_source as ops

REAL_AIRPORTS_SQL = __import__("os").path.join(
    __import__("os").path.dirname(__file__), "..", "data", "flight_airports.sql"
)


# ========================================================================
# tracked_airports_from_env() - Bölüm 5/31
# ========================================================================

def test_tracked_airports_parses_normalizes_dedupes_in_order(monkeypatch):
    monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", " ist , SAW,ist ")
    assert tracked_airports_from_env() == ["IST", "SAW"]


def test_tracked_airports_rejects_invalid_codes(monkeypatch):
    for bad in ("ISTANBUL", "12A", "AB"):
        monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", f"IST,{bad}")
        with pytest.raises(TrackedAirportsConfigError):
            tracked_airports_from_env()


def test_tracked_airports_empty_or_unset_is_config_error(monkeypatch):
    monkeypatch.delenv("AIRLABS_TRACKED_AIRPORTS", raising=False)
    with pytest.raises(TrackedAirportsConfigError):
        tracked_airports_from_env()

    monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", "   ")
    with pytest.raises(TrackedAirportsConfigError):
        tracked_airports_from_env()


def test_tracked_airports_custom_env_var_name(monkeypatch):
    monkeypatch.setenv("OTHER_VAR", "IST")
    assert tracked_airports_from_env("OTHER_VAR") == ["IST"]


# ========================================================================
# worker._build_live_run_fn() - fail-fast, zero HTTP calls - Bölüm 30
# ========================================================================

def test_missing_api_key_fails_fast_with_zero_http_calls(monkeypatch):
    monkeypatch.delenv("AIRLABS_API_KEY", raising=False)
    monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", "IST")

    def _forbidden_urlopen(*args, **kwargs):
        raise AssertionError("HTTP call yapılmamalıydı - API key eksik")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden_urlopen)

    with pytest.raises(worker_module.AirLabsConfigError, match="AIRLABS_API_KEY"):
        worker_module._build_live_run_fn()


def test_missing_tracked_airports_fails_fast_with_zero_http_calls(monkeypatch):
    monkeypatch.setenv("AIRLABS_API_KEY", "fake-key-not-real")
    monkeypatch.delenv("AIRLABS_TRACKED_AIRPORTS", raising=False)

    def _forbidden_urlopen(*args, **kwargs):
        raise AssertionError("HTTP call yapılmamalıydı - tracked airports eksik")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden_urlopen)

    with pytest.raises(worker_module.AirLabsConfigError):
        worker_module._build_live_run_fn()


def test_invalid_tracked_airports_fails_fast(monkeypatch):
    monkeypatch.setenv("AIRLABS_API_KEY", "fake-key-not-real")
    monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", "ISTANBUL")

    with pytest.raises(worker_module.AirLabsConfigError):
        worker_module._build_live_run_fn()


def test_config_error_never_contains_api_key_value(monkeypatch):
    monkeypatch.setenv("AIRLABS_API_KEY", "super-secret-value-must-not-leak")
    monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", "NOTVALID123")

    with pytest.raises(worker_module.AirLabsConfigError) as excinfo:
        worker_module._build_live_run_fn()

    assert "super-secret-value-must-not-leak" not in str(excinfo.value)


def test_valid_config_builds_a_callable_run_fn_without_any_http_call(monkeypatch):
    """Config geçerliyse `_build_live_run_fn()` BAŞARIYLA bir callable döner - ama onu HENÜZ ÇAĞIRMAZ, dolayısıyla burada da hiç HTTP isteği olmamalı."""
    monkeypatch.setenv("AIRLABS_API_KEY", "fake-key-not-real")
    monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", "IST,SAW")

    def _forbidden_urlopen(*args, **kwargs):
        raise AssertionError("run_fn İNŞA edilirken HTTP isteği olmamalı - sadece ÇAĞRILDIĞINDA")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden_urlopen)

    run_fn = worker_module._build_live_run_fn()
    assert callable(run_fn)


# ========================================================================
# worker._build_live_run_fn() - gerçek uçtan uca (mock AirLabs HTTP,
# gerçek pipeline.run(), izole bellek içi DB) - Bölüm 29/34/44
# ========================================================================

@pytest.fixture
def isolated_pipeline_db(monkeypatch):
    """`tests/test_operational_dataset.py:operational_state` ile AYNI, KANITLANMIŞ desen."""
    test_engine = create_engine("sqlite://")
    TestSessionLocal = sessionmaker(bind=test_engine)
    monkeypatch.setattr(
        pipeline_module, "init_db",
        lambda drop_first=False: Base.metadata.create_all(test_engine),
    )
    monkeypatch.setattr(pipeline_module, "get_session", lambda: TestSessionLocal())

    Base.metadata.create_all(test_engine)
    seed_session = TestSessionLocal()
    import_airports(seed_session, REAL_AIRPORTS_SQL)
    seed_verified_dataset(seed_session)
    seed_curated_fallback(seed_session)
    seed_family_and_ga(seed_session)
    seed_session.close()
    return TestSessionLocal


def test_worker_constructed_run_fn_ingests_real_flights_via_mock_airlabs(
    monkeypatch, isolated_pipeline_db,
):
    """
    `worker._build_live_run_fn()`'in İNŞA ETTİĞİ closure'ı GERÇEKTEN
    çağırır - `airlabs_client` HTTP katmanı mock'lanır (gerçek ağa hiç
    çıkılmaz), ama `_build_live_run_fn()`'in kendisi (env okuma,
    `build_source_a`/`build_source_b` inşası, `pipeline.run()`'a
    geçirme) TAMAMEN GERÇEK kod yoludur.

    `_build_live_run_fn()`'in closure'ı - production'da doğru olan -
    her çağrıda GERÇEK `domain_now()`'ı kullanır (Re-Ingest Loop
    Prevention/48h Usage Horizon AKTİF). Bu fixture'ın uçuşları sabit
    (2026-09-15) tarihli olduğu için, `domain_now`'ı `test_operational_
    dataset.py`'nin AYNI referans anına monkeypatch'liyoruz - yoksa
    gerçek duvar saati fixture'dan çok uzakta kalır ve 48h horizon
    filtresi HER kaydı (doğru biçimde) reddeder; bu testin AMACI o
    filtreyi atlatmak DEĞİL, worker'ın inşa ettiği kod yolunun gerçek
    ingestion'a ULAŞTIĞINI kanıtlamaktır.
    """
    monkeypatch.setenv("AIRLABS_API_KEY", "fake-key-not-real")
    monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", ",".join(ops.AIRPORTS))
    source = ops.install(monkeypatch, round_="t0")
    monkeypatch.setattr(worker_module, "domain_now", lambda: datetime(2026, 9, 15, 20, 0))

    run_fn = worker_module._build_live_run_fn()
    summary = run_fn()

    assert summary["flights_parsed"] > 0
    assert summary["failed"] == 0

    session = isolated_pipeline_db()
    try:
        assert session.query(Flight).count() > 0
    finally:
        session.close()


def test_worker_constructed_run_fn_handles_t0_to_t1_update_same_identity(
    monkeypatch, isolated_pipeline_db,
):
    """Bölüm 44 - aynı flight t0->t1'de update edilir, duplicate satır AÇILMAZ."""
    monkeypatch.setenv("AIRLABS_API_KEY", "fake-key-not-real")
    monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", ",".join(ops.AIRPORTS))
    source = ops.install(monkeypatch, round_="t0")
    monkeypatch.setattr(worker_module, "domain_now", lambda: datetime(2026, 9, 15, 20, 0))

    run_fn = worker_module._build_live_run_fn()
    first_summary = run_fn()
    assert first_summary["flights_parsed"] > 0, "test fixture'ı gerçekte hiç uçuş üretmiyorsa aşağıdaki karşılaştırma anlamsız olur"

    session = isolated_pipeline_db()
    try:
        count_after_t0 = session.query(Flight).count()
        keys_after_t0 = {f.flight_key for f in session.query(Flight).all()}
    finally:
        session.close()

    source.round = "t1"
    run_fn()

    session = isolated_pipeline_db()
    try:
        count_after_t1 = session.query(Flight).count()
        keys_after_t1 = [f.flight_key for f in session.query(Flight).all()]
    finally:
        session.close()

    # t1, t0'ın 30dk sonrası gerçekçi bir operasyonel senaryo - yeni
    # uçuşlar görünebilir/bazıları pencereden çıkabilir (bu ADIM'ın
    # amacı bu KÜMENİN birebir aynı kalmasını iddia etmek DEĞİL).
    # Burada GERÇEKTEN doğrulanan: (1) t0'daki uçuşların ÇOĞU t1'de de
    # AYNI identity ile hâlâ mevcut (worker'ın inşa ettiği run_fn ikinci
    # turda önceki turu SIFIRLAMIYOR), (2) flight_key hiçbir zaman
    # duplicate DB satırına yol açmıyor (UNIQUE constraint + upsert).
    overlap = keys_after_t0 & set(keys_after_t1)
    assert len(overlap) > 0, "t0 ve t1 arasında ORTAK hiçbir flight yok - şüpheli, iki tur birbirinden TAMAMEN izole olmamalı"
    assert len(keys_after_t1) == len(set(keys_after_t1)), "flight_key duplicate DB satırı ÜRETMEMELİ (UNIQUE constraint)"
    assert count_after_t1 == len(set(keys_after_t1))


# ========================================================================
# health.py - source_mode AirLabs-configured durumu doğru yansıtıyor mu
# ========================================================================

def test_health_reports_airlabs_configured_when_valid(monkeypatch):
    from app.health import _source_mode

    monkeypatch.setenv("AIRLABS_API_KEY", "fake-key-not-real")
    monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", "IST,SAW")

    mode, configured = _source_mode()
    assert mode == "airlabs"
    assert configured is True


def test_health_reports_airlabs_not_configured_when_tracked_airports_invalid(monkeypatch):
    from app.health import _source_mode

    monkeypatch.setenv("AIRLABS_API_KEY", "fake-key-not-real")
    monkeypatch.setenv("AIRLABS_TRACKED_AIRPORTS", "NOTVALID")

    mode, configured = _source_mode()
    assert mode == "airlabs"
    assert configured is False, "geçersiz tracked-airports ile 'configured=True' YANLIŞ olurdu"


def test_health_falls_back_to_file_based_reporting_without_api_key(monkeypatch):
    from app.health import _source_mode

    monkeypatch.delenv("AIRLABS_API_KEY", raising=False)
    monkeypatch.delenv("QUEUE_LOCAL_SOURCE_MODE", raising=False)

    mode, configured = _source_mode()
    assert mode == "bundled_sample_file"
    assert configured is False
