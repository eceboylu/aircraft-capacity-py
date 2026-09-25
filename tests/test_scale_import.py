"""
Madde 3/20.4 - gerçek `data/` scale dosyalarının conflict-free olduğunu
ve `import_airport_scales()`/`refresh_airport_scales()`'in `Airport.
scale`'i doğru yazdığını doğrular.
"""
import os

from app.queue.domain.airport_scale import find_cross_scale_conflicts, parse_scale_list
from app.queue.ingestion.airports_import import import_airport_scales, refresh_airport_scales
from app.queue.models import Airport
from app.queue.pipeline import (
    DATA_DIR,
    LARGE_SCALE_TXT,
    MEDIUM_SCALE_TXT,
    MEGA_SCALE_TXT,
    SMALL_SCALE_TXT,
)


def _real_paths():
    return (
        os.path.join(DATA_DIR, MEGA_SCALE_TXT),
        os.path.join(DATA_DIR, LARGE_SCALE_TXT),
        os.path.join(DATA_DIR, MEDIUM_SCALE_TXT),
        os.path.join(DATA_DIR, SMALL_SCALE_TXT),
    )


def test_real_data_scale_files_have_no_cross_scale_conflicts():
    """
    Görev talimatı: "hiçbir IATA/ICAO birden fazla scale listesinde
    bulunmasın... duplicate/conflict varsa fail et". Bu, kullanıcının
    GÜNCEL 4 dosyasının GERÇEK içeriği üzerinde çalışan bir regresyon
    kilididir - listeler ileride değişirse ve bir çakışma girerse bu
    test FAIL eder.
    """
    mega_path, large_path, medium_path, small_path = _real_paths()
    mega = parse_scale_list(mega_path)
    large = parse_scale_list(large_path)
    medium = parse_scale_list(medium_path)
    small = parse_scale_list(small_path)

    conflicts = find_cross_scale_conflicts(mega, large, medium, small)
    assert conflicts == {"iata": {}, "icao": {}}, (
        f"Scale dosyalarında çakışma bulundu: {conflicts}"
    )


def test_real_mega_list_is_non_empty_and_has_expected_members():
    mega_path, *_ = _real_paths()
    mega = parse_scale_list(mega_path)
    assert len(mega.iata_codes) > 0
    # Görev örneğinde/kullanıcı dosyasında AÇIKÇA MEGA'ya taşınan
    # havalimanları - hardcode edilen airport membership DEĞİL, sadece
    # gerçek dosyanın BEKLENEN bir alt kümesini doğrulayan bir smoke
    # check.
    assert "IST" in mega.iata_codes
    assert "AMS" in mega.iata_codes


def test_import_airport_scales_writes_mega_scale(db_session):
    session = db_session
    session.add(Airport(iata_code="IST", icao_code="LTFM"))
    session.add(Airport(iata_code="ESB", icao_code="LTAC"))  # LARGE listesinde
    session.commit()

    mega_path, large_path, medium_path, small_path = _real_paths()
    result = import_airport_scales(session, mega_path, large_path, medium_path, small_path)

    assert result["conflicted"] == 0
    ist = session.get(Airport, "IST")
    assert ist.scale == "mega"


def test_refresh_airport_scales_reclassifies_from_large_to_mega(db_session):
    """
    Görev senaryosu: eski 3-tier import'tan `scale='large'` olarak
    işaretlenmiş bir MEGA havalimanı, refresh sonrası `scale='mega'`
    olmalı.
    """
    session = db_session
    session.add(Airport(iata_code="IST", icao_code="LTFM", scale="large"))
    session.commit()

    mega_path, large_path, medium_path, small_path = _real_paths()
    result = refresh_airport_scales(session, mega_path, large_path, medium_path, small_path)

    assert result["updated"] >= 1
    ist = session.get(Airport, "IST")
    assert ist.scale == "mega"


def test_refresh_airport_scales_preserves_unmatched_airport_scale(db_session):
    """
    Hiçbir scale listesinde olmayan bir IATA - mevcut (uydurma olmayan,
    başka bir yoldan atanmış) scale değeri KORUNMALI, None'a
    düşürülmemeli.
    """
    session = db_session
    session.add(Airport(iata_code="ZZZZ", icao_code="ZZZZ", scale="medium"))
    session.commit()

    mega_path, large_path, medium_path, small_path = _real_paths()
    refresh_airport_scales(session, mega_path, large_path, medium_path, small_path)

    airport = session.get(Airport, "ZZZZ")
    assert airport.scale == "medium"
