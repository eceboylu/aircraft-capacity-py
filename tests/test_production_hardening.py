"""
Production hardening doğrulaması.

Kapsam:
  - refresh_flights(): bir satırdaki hata tüm batch'i düşürmüyor
  - run_predictions(): bir havalimanındaki hata diğerlerini etkilemiyor
  - Kaynak B okunamadığında uyarı loglanıyor (sessiz yutulmuyor)

Hepsi bellek içi (`sqlite://`) izole session kullanır.
"""

import logging
from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue import engine as engine_module
from app.queue.engine import run_predictions
from app.queue.ingestion.refresh import refresh_flights
from app.queue.models import Flight, FlightEvent

from .factories import MockCapacityResolver


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    try:
        yield db
    finally:
        db.close()


def make_row(flight_key, airport="AAA", aircraft="A320", **overrides):
    row = {
        "flight_key": flight_key,
        "airport_iata": airport,
        "direction": "departure",
        "location": "domestic",
        "airline_iata": "TK",
        "flight_number": "1",
        "flight_iata": "TK1",
        "aircraft_icao": aircraft,
        "aircraft_match_found": True,
        "dep_iata": airport,
        "arr_iata": "ZZZ",
        "dep_scheduled_utc": datetime(2026, 9, 15, 10, 0),
        "dep_estimated_utc": None,
        "dep_actual_utc": None,
        "arr_scheduled_utc": datetime(2026, 9, 15, 11, 0),
        "arr_estimated_utc": None,
        "arr_actual_utc": None,
        "dep_terminal": None, "dep_gate": None,
        "arr_terminal": None, "arr_gate": None,
        "status": "scheduled",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------
# refresh_flights() - satır bazlı hata izolasyonu
# --------------------------------------------------------------------

def test_refresh_flights_bad_row_is_skipped_good_rows_survive(session, caplog):
    """
    ADIM (MySQL Performance Regression Fix - Phase 1) - bozuk satır
    enjeksiyon mekanizması güncellendi: ESKİ `refresh_flights()`
    `Flight(**payload)` kullandığı için beklenmeyen bir dict anahtarı
    (`this_column_does_not_exist`) bir Python `TypeError` üretiyordu.
    YENİ implementasyon SADECE bilinen alanlara `setattr()` yapar
    (bkz. `_MUTABLE_FLIGHT_FIELDS`) - fazladan/bilinmeyen bir anahtar
    artık SESSİZCE YOK SAYILIR (hata ÜRETMEZ, çünkü ZATEN hiç
    okunmuyor). Bu, TEST İÇİN daha az "bozuk" bir enjeksiyon aracı
    olduğu anlamına gelir - GERÇEK bir hata senaryosu (DB seviyesinde
    GERÇEKTEN reddedilecek bir değer - burada geçersiz bir DateTime
    tipi, SQLite/MySQL ikisinde de `flush()` sırasında GERÇEKTEN hata
    üretir) ile DEĞİŞTİRİLDİ - test edilen KONTRAT (bozuk satır izole
    edilir, iyi satırlar hayatta kalır) DEĞİŞMEDİ.
    """
    good1 = make_row("TK_1_2026-09-15")
    bad = make_row("TK_BAD_2026-09-15")
    bad["dep_scheduled_utc"] = object()  # flush() sırasında StatementError/TypeError - GERÇEK bir DB-seviyesi hata
    good2 = make_row("TK_2_2026-09-15")

    with caplog.at_level(logging.ERROR, logger="app.queue.ingestion.refresh"):
        summary = refresh_flights(session, [good1, bad, good2])

    assert summary["inserted"] == 2
    assert summary["failed"] == 1
    assert session.scalar(select(func.count()).select_from(Flight)) == 2

    stored_keys = {
        f.flight_key for f in session.execute(select(Flight)).scalars().all()
    }
    assert stored_keys == {"TK_1_2026-09-15", "TK_2_2026-09-15"}

    assert any(
        "TK_BAD_2026-09-15" in record.getMessage() for record in caplog.records
    )
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_refresh_flights_bad_row_does_not_break_session_for_later_calls(session):
    good1 = make_row("TK_3_2026-09-15")
    bad = make_row("TK_BAD2_2026-09-15")
    bad["arr_scheduled_utc"] = object()  # bkz. yukarıdaki test - gerçek DB-seviyesi hata

    refresh_flights(session, [good1, bad])

    # Session bozulmadı - başka bir işlem sorunsuz çalışmalı.
    another = make_row("TK_4_2026-09-15")
    summary = refresh_flights(session, [another])
    assert summary["inserted"] == 1
    assert session.scalar(select(func.count()).select_from(Flight)) == 2


def test_refresh_flights_bad_row_rolls_back_its_own_pending_event(session):
    """
    Var olan bir uçuşun GÜNCELLEMESİ sırasında hata olursa (event zaten
    session'a eklenmiş ama commit edilmemişken), o event de geri
    alınmalı - yarım kalmış bir FlightEvent DB'de kalmamalı.
    """
    refresh_flights(session, [make_row("TK_5_2026-09-15", aircraft="A320")])

    updated_bad = make_row("TK_5_2026-09-15", aircraft="A321")  # aircraft change tetikler
    updated_bad["dep_scheduled_utc"] = object()                   # ama flush() patlayacak (gerçek DB-seviyesi hata)

    summary = refresh_flights(session, [updated_bad])

    assert summary["failed"] == 1
    assert summary["events_written"] == 0
    # Hiçbir FlightEvent kalıcı olmamalı - hem event hem flight update
    # AYNI satırın transaction'ında, ikisi birden rollback edildi.
    assert session.scalar(select(func.count()).select_from(FlightEvent)) == 0
    stored = session.execute(select(Flight)).scalar_one()
    assert stored.aircraft_icao == "A320"   # değişmedi


def test_refresh_flights_no_bad_rows_returns_zero_failed(session):
    summary = refresh_flights(session, [make_row("TK_6_2026-09-15")])
    assert summary["failed"] == 0


# --------------------------------------------------------------------
# run_predictions() - havalimanı bazlı hata izolasyonu
# --------------------------------------------------------------------

def test_run_predictions_isolates_one_failing_airport(session, monkeypatch, caplog):
    """IST başarılı, ATL başarılı, ZZZ kontrollü failure - diğerleri etkilenmemeli."""
    refresh_flights(session, [
        make_row("TK_IST_2026-09-15", airport="IST"),
        make_row("TK_ATL_2026-09-15", airport="ATL"),
        make_row("TK_ZZZ_2026-09-15", airport="ZZZ"),
    ])

    real_predict_airport = engine_module.predict_airport

    def flaky_predict_airport(*, airport_iata, **kwargs):
        if airport_iata == "ZZZ":
            raise RuntimeError("kontrollü test failure - ZZZ")
        return real_predict_airport(airport_iata=airport_iata, **kwargs)

    monkeypatch.setattr(engine_module, "predict_airport", flaky_predict_airport)

    with caplog.at_level(logging.ERROR, logger="app.queue.engine"):
        result = run_predictions(
            session, MockCapacityResolver(),
            airports=["IST", "ATL", "ZZZ"],
            now=datetime(2026, 9, 15, 23, 0),
        )

    assert result["airports"]["IST"] > 0
    assert result["airports"]["ATL"] > 0
    assert "ZZZ" not in result["airports"]
    assert result["failed_airports"] == ["ZZZ"]
    assert any("ZZZ" in r.getMessage() for r in caplog.records)

    # Session hâlâ kullanılabilir, başarılı havalimanlarının tahminleri kalıcı.
    from app.queue.models import QueuePrediction
    persisted_airports = {
        row.airport_iata
        for row in session.execute(select(QueuePrediction)).scalars().all()
    }
    assert "IST" in persisted_airports
    assert "ATL" in persisted_airports
    assert "ZZZ" not in persisted_airports


def test_run_predictions_no_failures_returns_empty_failed_list(session):
    refresh_flights(session, [make_row("TK_7_2026-09-15", airport="IST")])
    result = run_predictions(
        session, MockCapacityResolver(), airports=["IST"],
        now=datetime(2026, 9, 15, 23, 0),
    )
    assert result["failed_airports"] == []


# --------------------------------------------------------------------
# Kaynak B silent fallback -> artık warning logluyor
# --------------------------------------------------------------------

def test_source_b_read_failure_logs_warning_not_silent(caplog, tmp_path):
    from app.queue.pipeline import load_flight_rows

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    def broken_source_a(direction):
        return []

    def broken_source_b():
        raise OSError("dosya yok (TEST)")

    with caplog.at_level(logging.WARNING, logger="app.queue.pipeline"):
        rows = load_flight_rows(
            db, data_dir=str(tmp_path),
            source_a=broken_source_a, source_b=broken_source_b,
        )

    assert rows == []
    assert any(
        r.levelno == logging.WARNING and "Kaynak B" in r.getMessage()
        for r in caplog.records
    )
    db.close()
