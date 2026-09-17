"""
ADIM 5A - scheduler öncesi observability doğrulaması.

Kapsam:
  - pipeline.run()'ın döndürdüğü özet: `failed_airports`,
    `duration_seconds` (yeni alanlar, mevcut alanlar bozulmadı)
  - pipeline.run()'ın rutin (INFO seviye) ilerleme logları ürettiği
  - pipeline.main() - CLI exit-code kararı (run() monkeypatch edilerek,
    GERÇEK dosya sistemine/`database.sqlite`'a hiç dokunmadan)

Hepsi bellek içi (`sqlite://`) izole session kullanır; hiçbir test
`database.sqlite`'a veya gerçek AirLabs API'ye dokunmaz.
"""

import logging
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.queue.pipeline as pipeline_module
from app.models import Base
from app.queue import engine as engine_module


def _raw_record(flight_iata, number, dep_iata, arr_iata, hour):
    """AirLabs `schedules` şemasında minimal, geçerli bir Kaynak A kaydı."""
    dep = datetime(2026, 9, 15, hour, 0)
    arr = datetime(2026, 9, 15, hour + 1, 0)
    return {
        "airline_iata": flight_iata[:2],
        "flight_iata": flight_iata,
        "flight_number": number,
        "dep_iata": dep_iata,
        "arr_iata": arr_iata,
        "dep_time_utc": dep.strftime("%Y-%m-%d %H:%M"),
        "arr_time_utc": arr.strftime("%Y-%m-%d %H:%M"),
        "status": "scheduled",
        "aircraft_icao": "A320",
    }


@pytest.fixture
def isolated_pipeline(monkeypatch):
    """
    `pipeline.init_db`/`get_session`'ı bellek içi bir engine'e
    yönlendirir - gerçek `database.sqlite`'a HİÇ dokunulmaz. İki
    havalimanı (AAA, ZZZ) arasında bir kalkış/varış çifti üreten
    minimal source_a/source_b sağlar; bu, GERÇEK `pipeline.run()` ->
    `load_flight_rows` -> `parse_source_a` -> `refresh_flights` ->
    `run_predictions` akışından geçer, hiçbir katman atlanmaz.
    """
    test_engine = create_engine("sqlite://")
    TestSessionLocal = sessionmaker(bind=test_engine)

    monkeypatch.setattr(
        pipeline_module, "init_db",
        lambda drop_first=False: Base.metadata.create_all(test_engine),
    )
    monkeypatch.setattr(pipeline_module, "get_session", lambda: TestSessionLocal())

    def source_a(direction):
        if direction == "departure":
            return [_raw_record("AA1", "1", "AAA", "ZZZ", 10)]
        return [_raw_record("AA1", "1", "AAA", "ZZZ", 10)]

    def source_b():
        return []

    return {"source_a": source_a, "source_b": source_b}


def test_summary_includes_failed_airports_and_duration_fields(isolated_pipeline):
    """
    `failed_airports` artık run() özetine yansıyor; `duration_seconds`
    negatif olmayan bir sayı. Mevcut alanlar (inserted/updated/
    predictions/...) hâlâ mevcut - hiçbiri kaldırılmadı.
    """
    summary = pipeline_module.run(
        source_a=isolated_pipeline["source_a"],
        source_b=isolated_pipeline["source_b"],
    )

    assert "failed_airports" in summary
    assert summary["failed_airports"] == []   # bu senaryoda hiçbir havalimanı başarısız değil

    assert "duration_seconds" in summary
    assert isinstance(summary["duration_seconds"], float)
    assert summary["duration_seconds"] >= 0

    # Mevcut alanlar hâlâ orada (regresyon değil, sadece EKLEME yapıldı).
    for key in ("inserted", "updated", "events_written", "failed",
                "predictions", "pruned", "airports_predicted", "flights_parsed"):
        assert key in summary


def test_summary_reports_partial_airport_failure_without_crashing(
    isolated_pipeline, monkeypatch
):
    """
    Bir havalimanının tahmin üretimi başarısız olursa (izole hata,
    bkz. engine.py:run_predictions), run() ÇÖKMEZ ve bu havalimanı
    `failed_airports` içinde görünür.
    """
    real_predict_airport = engine_module.predict_airport

    def flaky_predict_airport(*, airport_iata, **kwargs):
        if airport_iata == "AAA":
            raise RuntimeError("kasıtlı test hatası")
        return real_predict_airport(airport_iata=airport_iata, **kwargs)

    monkeypatch.setattr(engine_module, "predict_airport", flaky_predict_airport)

    # ADIM (Operational-Day Scope): "AAA" TESADÜFEN gerçek bir IATA
    # kodu (Anaa Airport, Fransız Polinezyası, Pacific/Tahiti UTC-10) -
    # `ensure_airports()` gerçek flight_airports.sql'i içe aktardığı
    # için artık GERÇEK bir timezone'a sahip. Bu testin amacı timezone
    # DEĞİL (izole hata/failed_airports davranışı) - `now`'ı flight'ın
    # KENDİ tarihiyle (2026-09-15) AÇIKÇA hizalıyoruz ki AAA'nın flight'ı
    # operational-day filtresi tarafından YANLIŞLIKLA elenmesin (bu
    # olmazsa predict_airport hiç çağrılmaz, kasıtlı hata hiç tetiklenmez).
    summary = pipeline_module.run(
        source_a=isolated_pipeline["source_a"],
        source_b=isolated_pipeline["source_b"],
        now=datetime(2026, 9, 15, 12, 0),
    )

    assert summary["failed_airports"] == ["AAA"]
    # ZZZ (başarısız olmayan havalimanı) yine de işlendi.
    assert summary["airports_predicted"] >= 1


def test_run_emits_routine_info_logs(isolated_pipeline, caplog):
    """
    Rutin ilerleme logları (run başladı/tamamlandı, ingestion/refresh/
    prediction özetleri) INFO seviyesinde üretiliyor. Log METNİNİN tam
    biçimini değil, sadece anahtar aşamaların gerçekten loglandığını
    doğrular (brittle string-eşitliği YOK).
    """
    with caplog.at_level(logging.INFO, logger="app.queue.pipeline"):
        pipeline_module.run(
            source_a=isolated_pipeline["source_a"],
            source_b=isolated_pipeline["source_b"],
        )

    messages = " | ".join(r.getMessage() for r in caplog.records)
    for expected_fragment in (
        "run started",
        "ingestion sonucu",
        "refresh sonucu",
        "prediction started",
        "prediction completed",
        "run completed",
    ):
        assert expected_fragment in messages, f"eksik log parçası: {expected_fragment}"


def test_main_returns_zero_on_success(monkeypatch):
    """CLI: run() normal döndüğünde (kısmi hata olsa bile) exit code 0."""
    monkeypatch.setattr(
        pipeline_module, "run",
        lambda *a, **k: {
            "airports_loaded": 0, "flights_parsed": 1, "aircraft_match_rate": 1.0,
            "inserted": 1, "updated": 0, "events_written": 0, "failed": 0,
            "predictions": 1, "pruned": 0, "airports_predicted": 1,
            "failed_airports": [], "duration_seconds": 0.01,
        },
    )
    assert pipeline_module.main() == 0


def test_main_returns_zero_when_only_partial_airport_failure(monkeypatch):
    """
    CLI: bazı havalimanları başarısız olsa bile (izole hata), bu TEK
    BAŞINA process'i başarısız saymaz - exit code hâlâ 0 (bkz. main()
    docstring'indeki gerekçe: sürekli kısmi-hata alarmı yerine gerçek/
    kritik kesintiye odaklanmak).
    """
    monkeypatch.setattr(
        pipeline_module, "run",
        lambda *a, **k: {
            "airports_loaded": 0, "flights_parsed": 1, "aircraft_match_rate": 1.0,
            "inserted": 1, "updated": 0, "events_written": 0, "failed": 0,
            "predictions": 1, "pruned": 0, "airports_predicted": 1,
            "failed_airports": ["ZZZ"], "duration_seconds": 0.01,
        },
    )
    assert pipeline_module.main() == 0


def test_main_returns_nonzero_on_critical_failure(monkeypatch):
    """CLI: run() içinden yakalanmamış (top-level/kritik) bir hata -> exit code 1."""
    def broken_run(*a, **k):
        raise RuntimeError("DB'ye hiç bağlanılamadı (kasıtlı test hatası)")

    monkeypatch.setattr(pipeline_module, "run", broken_run)
    assert pipeline_module.main() == 1
