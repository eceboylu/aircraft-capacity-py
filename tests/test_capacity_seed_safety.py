"""
ADIM 6C ÖN-KONTROL - `aircraft_capacity` seed güvenliği.

Kanıtlanan gerçek risk: `pipeline.run()` sadece `init_db()`
(`create_all`) çağırıyordu; DB dosyası silinip yeniden oluşturulunca
`aircraft_capacity` BOŞ kalıyor ve `AircraftCapacityService.resolve()`
HER uçak tipi için sessizce `unknown_default` (150) katmanına
düşüyordu - hiçbir hata/uyarı olmadan.

Bu dosya `ensure_capacity_reference()`'ın (app/queue/pipeline.py):
  - boş tabloyu tespit edip Madde 1'in KENDİ resmi seed
    fonksiyonlarıyla doldurduğunu,
  - dolu tabloyu GEREKSİZ YERE yeniden seed ETMEDİĞİNİ,
  - `app.seed.run()`'ın (drop_first=True, DESTRUCTIVE) HİÇ
    çağrılmadığını - mevcut Flight/QueuePrediction verisi KORUNUYOR,
  - seed başarısız olursa SESSİZCE 150'ye düşülmediğini, açık bir
    `CapacitySeedError` fırlatıldığını

doğrular.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import AircraftCapacity, Base
from app.queue.models import Flight, QueuePrediction
from app.queue.pipeline import CapacitySeedError, ensure_capacity_reference


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


def test_empty_db_gets_seeded(session):
    assert session.query(AircraftCapacity).first() is None

    seeded = ensure_capacity_reference(session)

    assert seeded is True
    assert session.query(AircraftCapacity).first() is not None
    # verified_dataset (101) + curated_fallback (11) - gerçek dosyalardan.
    assert session.scalar(select(AircraftCapacity).where(AircraftCapacity.icao_code == "A320")) is not None


def test_existing_capacity_data_is_not_touched(session):
    """Tablo ZATEN doluysa hiçbir şey değişmemeli - reseed YOK."""
    ensure_capacity_reference(session)
    before = {row.icao_code: (row.capacity, row.updated_at) for row in session.query(AircraftCapacity).all()}

    seeded_again = ensure_capacity_reference(session)

    after = {row.icao_code: (row.capacity, row.updated_at) for row in session.query(AircraftCapacity).all()}
    assert seeded_again is False
    assert before == after   # satırlar TEKRAR YAZILMADI (updated_at bile aynı)


def test_existing_flight_and_prediction_data_survives(session):
    """
    Kanıtlanan senaryonun tersi: aircraft_capacity boş olsa bile,
    mevcut Flight/QueuePrediction verisi (örn. gerçek pipeline'dan
    kalan) SİLİNMEMELİ - `app.seed.run()` (drop_first=True) HİÇ
    çağrılmıyor.
    """
    session.add(Flight(
        flight_key="TK_1_2026-09-15_IST_departure", airport_iata="IST",
        direction="departure", location="domestic", status="scheduled",
    ))
    session.add(QueuePrediction(
        airport_iata="IST", process="security",
        window_start=__import__("datetime").datetime(2026, 9, 15, 8, 0),
        window_end=__import__("datetime").datetime(2026, 9, 15, 8, 15),
        flight_count=1, expected_passengers=100, risk="LOW", reasons="[]", confidence=0.5,
    ))
    session.commit()

    ensure_capacity_reference(session)

    assert session.query(Flight).filter_by(flight_key="TK_1_2026-09-15_IST_departure").first() is not None
    assert session.query(QueuePrediction).filter_by(airport_iata="IST").first() is not None


def test_second_run_is_idempotent(session):
    r1 = ensure_capacity_reference(session)
    count_after_first = session.query(AircraftCapacity).count()
    r2 = ensure_capacity_reference(session)
    count_after_second = session.query(AircraftCapacity).count()

    assert r1 is True
    assert r2 is False
    assert count_after_first == count_after_second


def test_seed_failure_raises_instead_of_silently_falling_back(session, monkeypatch, tmp_path):
    """
    Seed dosyası bulunamazsa (örn. `data/yolcu_ucaklari.json` eksik)
    sessizce boş tablo/unknown_default'a devam EDİLMEZ - açık bir
    hata fırlatılır.
    """
    import app.seed as seed_module

    monkeypatch.setattr(seed_module, "DATA_DIR", str(tmp_path))  # gerçek data/ klasörü YOK

    with pytest.raises(CapacitySeedError):
        ensure_capacity_reference(session)

    # Yarım kalan bir seed veri BIRAKMAMALI (rollback edildi).
    assert session.query(AircraftCapacity).first() is None


def test_capacity_resolver_uses_real_seeded_values_not_default(session):
    from app.service import AircraftCapacityService

    ensure_capacity_reference(session)
    service = AircraftCapacityService(session)

    result = service.resolve("A359", "TK")
    assert result.source == "verified_dataset"
    assert result.capacity == 475   # yolcu_ucaklari.json'daki GERÇEK değer, unknown_default (150) DEĞİL


def test_unknown_aircraft_still_falls_back_to_layer_5_unchanged(session):
    """Bilinmeyen bir tip, seed'den SONRA da hâlâ Katman 5'e (unknown_default) düşmeli - davranış BOZULMADI."""
    from app.service import AircraftCapacityService

    ensure_capacity_reference(session)
    service = AircraftCapacityService(session)

    result = service.resolve("ZZZZ", None)
    assert result.source == "unknown_default"
    assert result.capacity == 150
