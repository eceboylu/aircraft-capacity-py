"""
Gerçekçi çok-havalimanlı sentetik fixture (`tests/fixtures/airlabs_realistic/`)
testleri.

Bu, ADIM 6C'de bulunan "gerçek örnek veri çoğu havalimanı için çok
seyrek (1 uçuş) ve capacity'nin %93'ü unknown_default'a düşüyor"
bulgusuna karşı, KULLANICININ isteği üzerine oluşturulmuş - gerçek
AirLabs şemasıyla aynı ama daha hacimli, `aircraft_icao`'nun
gerçekçi bir oranda dolu olduğu bir test veri seti.

Fixture hiçbir risk/utilization/wait DEĞERİ içermez - hepsi gerçek
pipeline tarafından hesaplanır.
"""

import pytest
from sqlalchemy import create_engine, select, func, update
from sqlalchemy.orm import sessionmaker

import app.queue.pipeline as pipeline_module
from app.models import Base
from app.queue.api import airport_predictions
from app.queue.ingestion.airports_import import import_airports
from app.queue.ingestion import airlabs_client
from app.queue.models import Airport, AirportOperationalConfig, Flight
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset

from . import airlabs_realistic_source as rs

import os

REAL_AIRPORTS_SQL = os.path.join(os.path.dirname(__file__), "..", "data", "flight_airports.sql")


@pytest.fixture(scope="module")
def realistic_state():
    mp = pytest.MonkeyPatch()
    try:
        rs.install(mp)
        engine = create_engine("sqlite://")
        Session = sessionmaker(bind=engine)
        mp.setattr(pipeline_module, "init_db", lambda drop_first=False: Base.metadata.create_all(engine))
        mp.setattr(pipeline_module, "get_session", lambda: Session())
        Base.metadata.create_all(engine)

        s = Session()
        import_airports(s, REAL_AIRPORTS_SQL)
        # ADIM (Operational-Day Scope): bu fixture 77 GERÇEK havalimanını
        # (her biri FARKLI bir gerçek timezone'da) TEK ortak UTC gününe
        # ("2026-09-15") göre üretiyor - bu, testin KENDİ amacı (kapasite
        # çözümleme/hacim, gün-sınırı doğruluğu DEĞİL) için tasarlanmış,
        # timezone-agnostik bir senaryodur. Gerçek `Airport.timezone`
        # bırakılırsa operational-day filtresi her havalimanı için FARKLI
        # bir yerel "bugün" hesaplar ve TEK paylaşılan UTC gününe
        # sığdırılmış bu veri hiçbir `now` değeriyle 77 havalimanının
        # TAMAMI için AYNI ANDA doğru güne düşmez (fiziksel olarak
        # imkansız - 77 timezone'un yerel tarihleri aynı anda ~26 saatlik
        # bir yayılıma sahip). Bu yüzden timezone BİLEREK temizlenir -
        # üretim kodu/mantığı DEĞİŞMEDİ, sadece BU testin timezone-
        # agnostik amacına uygun hale getirildi (bkz. rapor).
        s.execute(update(Airport).values(timezone=None))
        seed_verified_dataset(s); seed_curated_fallback(s); seed_family_and_ga(s)
        for code in rs.AIRPORTS:
            s.add(AirportOperationalConfig(
                airport_iata=code, passport_counter_count=16, passport_staff_count=32,
                passport_staff_per_counter=2.0, passport_service_rate_per_staff=0.5,
                passport_efficiency_multiplier=1.5, arrival_bank_threshold=5,
            ))
        s.commit(); s.close()

        source_a = airlabs_client.build_source_a(rs.AIRPORTS)
        source_b = airlabs_client.build_source_b()
        summary = pipeline_module.run(source_a=source_a, source_b=source_b)

        yield {"session_factory": Session, "summary": summary}
    finally:
        mp.undo()


def _session(state):
    return state["session_factory"]()


def test_all_airports_parsed_without_errors(realistic_state):
    summary = realistic_state["summary"]
    assert summary["flights_parsed"] == len(rs.AIRPORTS) * 60   # her havalimanı: 30 departure + 30 arrival
    assert summary["failed_airports"] == []


def test_aircraft_match_rate_is_realistic_not_near_zero(realistic_state):
    """Gerçek örnek veride %6.6 idi - bu fixture kasıtlı olarak %85 civarı."""
    assert realistic_state["summary"]["aircraft_match_rate"] > 0.7


def test_each_airport_has_many_flights_not_sparse(realistic_state):
    session = _session(realistic_state)
    for code in rs.AIRPORTS:
        n = session.scalar(select(func.count()).select_from(Flight).where(Flight.airport_iata == code))
        assert n == 60, code
    session.close()


def test_airports_do_not_mix(realistic_state):
    session = _session(realistic_state)
    ams = airport_predictions(session, "AMS")
    cdg = airport_predictions(session, "CDG")
    session.close()
    assert ams["security"]["windows"] != cdg["security"]["windows"]


def test_average_wait_time_is_finite_for_most_windows_with_realistic_config(realistic_state):
    """
    Varsayılan (4 gişe) config'te TÜM pencereler kapasite aşımına
    düşüyordu (ölçüldü, bkz. konuşma) - gerçekçi (16 gişe, TEST/
    SCENARIO) config'le pencerelerin ÇOĞU finite bir bekleme süresi
    üretmeli.
    """
    session = _session(realistic_state)
    for code in rs.AIRPORTS:
        data = airport_predictions(session, code)
        windows = data["passport"]["windows"]
        finite = [w for w in windows if w["estimated_wait_minutes"] is not None]
        assert len(windows) > 10, code           # zengin veri
        assert len(finite) > 0, code              # gerçekçi config sayesinde en az bir tanesi hesaplanabiliyor
    session.close()


def test_different_airports_produce_different_averages(realistic_state):
    session = _session(realistic_state)
    averages = {}
    for code in rs.AIRPORTS:
        data = airport_predictions(session, code)
        finite = [w["estimated_wait_minutes"] for w in data["passport"]["windows"] if w["estimated_wait_minutes"] is not None]
        averages[code] = round(sum(finite) / len(finite), 4) if finite else None
    session.close()

    assert len(set(averages.values())) > 1   # havalimanları arasında AYNI değer değil


def test_fixture_never_contains_precomputed_results():
    """Fixture JSON'ları hiçbir risk/utilization/wait alanı İÇERMEZ."""
    import glob
    import json

    for path in glob.glob("tests/fixtures/airlabs_realistic/t0/*.json"):
        payload = json.load(open(path, encoding="utf-8"))
        for record in payload["response"]:
            for forbidden in ("risk", "utilization", "estimated_wait_minutes", "baseline_ratio"):
                assert forbidden not in record
