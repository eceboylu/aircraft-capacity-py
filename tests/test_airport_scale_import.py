"""
PHASE 3 - `app/queue/ingestion/airports_import.py:import_airport_scales()`
gerçek 3 txt dosyasına karşı, izole SQLite DB'de doğrulanır. Gerçek
`database.sqlite`'a HİÇ dokunulmaz.
"""
import os

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.ingestion.airports_import import import_airport_scales
from app.queue.models import Airport

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
LARGE_PATH = os.path.join(DATA_DIR, "buyuk_olcekli_havaalanlari.txt")
MEDIUM_PATH = os.path.join(DATA_DIR, "orta_olcekli_havaalanlari.txt")
SMALL_PATH = os.path.join(DATA_DIR, "kucuk_olcekli_havaalanlari.txt")


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def _add_airport(session, iata, icao, name, country="XX"):
    session.add(Airport(
        iata_code=iata, icao_code=icao, airport_name=name,
        city_code=iata, country_code=country,
    ))


def test_one_large_one_medium_one_small_real_airport_resolved(session):
    # IST=large, CBR=medium, AGJ=small - gerçek txt dosyalarından doğrulandı.
    _add_airport(session, "IST", "LTFM", "Istanbul Airport", "TR")
    _add_airport(session, "CBR", "YSCB", "Canberra Airport", "AU")
    _add_airport(session, "AGJ", "RORA", "Aguni", "JP")
    _add_airport(session, "ZZZUNKNOWN", None, "Nonexistent Test Airport", "XX")
    session.commit()

    result = import_airport_scales(session, LARGE_PATH, MEDIUM_PATH, SMALL_PATH)

    assert result["airports_checked"] == 4
    assert result["matched"] == 3
    assert result["unmatched"] == 1
    assert result["conflicted"] == 0
    assert result["iata_conflicts"] == {}
    assert result["icao_conflicts"] == {}

    ist = session.get(Airport, "IST")
    cbr = session.get(Airport, "CBR")
    agj = session.get(Airport, "AGJ")
    unknown = session.get(Airport, "ZZZUNKNOWN")

    assert ist.scale == "large"
    assert cbr.scale == "medium"
    assert agj.scale == "small"
    assert unknown.scale is None  # sessizce eski bir varsayılana DÜŞMEDİ


def test_existing_airport_fields_are_not_clobbered_by_scale_import(session):
    """`session.merge(Airport(...))` KULLANILMADIĞININ kanıtı - diğer alanlar korunur."""
    _add_airport(session, "IST", "LTFM", "Istanbul Airport", "TR")
    session.commit()

    import_airport_scales(session, LARGE_PATH, MEDIUM_PATH, SMALL_PATH)

    ist = session.get(Airport, "IST")
    assert ist.icao_code == "LTFM"
    assert ist.airport_name == "Istanbul Airport"
    assert ist.country_code == "TR"
    assert ist.scale == "large"


def test_import_is_idempotent_when_run_twice(session):
    _add_airport(session, "IST", "LTFM", "Istanbul Airport", "TR")
    session.commit()

    first = import_airport_scales(session, LARGE_PATH, MEDIUM_PATH, SMALL_PATH)
    second = import_airport_scales(session, LARGE_PATH, MEDIUM_PATH, SMALL_PATH)

    assert first["matched"] == second["matched"] == 1
    assert session.get(Airport, "IST").scale == "large"


def test_full_real_airports_table_scale_distribution_report(session):
    """Gerçek `flight_airports.sql`'in TAMAMI import edilip scale dağılımı raporlanır."""
    from app.queue.ingestion.airports_import import import_airports

    real_sql = os.path.join(DATA_DIR, "flight_airports.sql")
    import_airports(session, real_sql)

    result = import_airport_scales(session, LARGE_PATH, MEDIUM_PATH, SMALL_PATH)

    scales = session.execute(select(Airport.scale)).scalars().all()
    large_count = sum(1 for s in scales if s == "large")
    medium_count = sum(1 for s in scales if s == "medium")
    small_count = sum(1 for s in scales if s == "small")
    unknown_count = sum(1 for s in scales if s is None)

    print(
        f"\n[PHASE 3 REPORT] airports_checked={result['airports_checked']} "
        f"matched={result['matched']} unmatched={result['unmatched']} "
        f"conflicted={result['conflicted']} "
        f"large={large_count} medium={medium_count} small={small_count} unknown={unknown_count}"
    )

    assert result["matched"] == large_count + medium_count + small_count
    assert result["unmatched"] + result["conflicted"] == unknown_count
    assert large_count > 0
    assert medium_count > 0
    assert small_count > 0
