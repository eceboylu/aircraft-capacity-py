"""
genel-proje.md matematiksel/golden audit - Madde 17 (GRAPH/API BUCKET
DOĞRULAMA).

`tests/test_queue_mathematical_golden.py`'nin pure-fonksiyon (DB'siz)
kanıtlarını, GERÇEK reporting/API katmanından (`run_predictions()` ->
`airport_predictions()`, izole `sqlite://` DB) geçirerek tekrar
doğrular - exact event timestamp'in sadece raporlama ANINDA saatlik
bucket'a yuvarlandığını, DB'ye YAZILAN/API'den DÖNEN `window_start`
üzerinden kanıtlar.

Expected `window_start` değerleri (13:00, 14:00, 08:00) YİNE elle
hesaplanmıştır (bkz. `test_queue_mathematical_golden.py`'deki Madde
2/3 ile AYNI -120dk/+15dk aritmetiği) - production'ın kendi çıktısı
"expected" olarak kullanılmadı.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import AircraftCapacity, Base
from app.queue.api import airport_predictions
from app.queue.engine import run_predictions
from app.queue.ingestion.refresh import refresh_flights
from app.queue.models import Airport
from app.service import AircraftCapacityService

DAY = datetime(2026, 9, 18)
NOW = DAY.replace(hour=23)


def _flight_row(flight_key, direction, location, dep=None, arr=None, aircraft="A320", status="scheduled"):
    return {
        "flight_key": flight_key,
        "airport_iata": "IST",
        "direction": direction,
        "location": location,
        "airline_iata": "TK",
        "flight_number": flight_key,
        "flight_iata": f"TK{flight_key}",
        "aircraft_icao": aircraft,
        "aircraft_match_found": True,
        "dep_iata": "IST" if direction == "departure" else "CDG",
        "arr_iata": "CDG" if direction == "departure" else "IST",
        "dep_scheduled_utc": dep,
        "dep_estimated_utc": None,
        "dep_actual_utc": None,
        "arr_scheduled_utc": arr,
        "arr_estimated_utc": None,
        "arr_actual_utc": None,
        "dep_terminal": None,
        "dep_gate": None,
        "arr_terminal": None,
        "arr_gate": None,
        "status": status,
    }


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.add(Airport(
            iata_code="IST", icao_code="LTFM", airport_name="Istanbul Airport",
            city_code="IST", country_code="TR", timezone="UTC",
        ))
        db.add(AircraftCapacity(
            icao_code="A320", name="A320", category="narrow",
            capacity=150, source="verified_dataset", confidence="high",
        ))
        db.commit()
        yield db
    finally:
        db.close()


@pytest.fixture()
def api_result(session):
    rows = [
        # Madde 2: departure 15:35 -> effective 13:35 -> bucket 13:00.
        _flight_row("DEP1", "departure", "international", dep=DAY.replace(hour=15, minute=35)),
        # Madde 3: arrival 13:50 -> effective 14:05 -> bucket 14:00.
        _flight_row("ARR1", "arrival", "international", arr=DAY.replace(hour=13, minute=50)),
        # domestic departure 10:00 -> effective 08:00 -> bucket 08:00.
        _flight_row("DOM1", "departure", "domestic", dep=DAY.replace(hour=10, minute=0)),
    ]
    refresh_flights(session, rows)
    run_predictions(
        session, AircraftCapacityService(session), airports=["IST"],
        update_baseline=False, now=NOW,
    )
    return airport_predictions(session, "IST", now=NOW)


def _hours(section_windows):
    return {w["window_start"] for w in section_windows}


def _window_at(section_windows, hour, minute=0):
    target = DAY.replace(hour=hour, minute=minute).isoformat()
    for w in section_windows:
        if w["window_start"] == target:
            return w
    return None


def test_international_departure_bucket_is_13_00_not_flight_time(api_result):
    """
    ADIM (24-Hour Graph) ile GÜNCELLENDİ: artık TÜM 24 saat bucket
    listesinde YER ALIR (bkz. api.py `_pad_series_to_24_hours`) - asıl
    iddia DEĞİŞMEDİ: GERÇEK flight, KENDİ saati (15:00) DEĞİL, queue
    event saatinde (13:00, -120dk offset) GERÇEK talep taşır; 15:00
    sıfır-talep (flight_count=0) bucket'ı olarak kalır.
    """
    windows = api_result["international_departure"]["passport"]["windows"]
    hours = _hours(windows)
    assert DAY.replace(hour=13).isoformat() in hours
    assert _window_at(windows, 13)["flight_count"] > 0
    assert _window_at(windows, 15)["flight_count"] == 0   # flight'ın KENDİ saati - gerçek talep YOK
    assert _window_at(windows, 15, minute=35) is None      # 15:35 saatlik bucket sınırı değil, hiç yok


def test_international_arrival_bucket_is_14_00_not_13_00(api_result):
    windows = api_result["international_arrival"]["windows"]
    hours = _hours(windows)
    assert DAY.replace(hour=14).isoformat() in hours
    assert _window_at(windows, 14)["flight_count"] > 0
    assert _window_at(windows, 13)["flight_count"] == 0


def test_domestic_security_bucket_is_08_00(api_result):
    hours = _hours(api_result["domestic_security"]["windows"])
    assert DAY.replace(hour=8).isoformat() in hours


def test_window_start_values_are_exact_hour_boundaries(api_result):
    """Her section'daki HER window_start dakika/saniye=0 olmalı (saatlik floor)."""
    all_windows = (
        api_result["overall"]["windows"]
        + api_result["domestic_security"]["windows"]
        + api_result["international_departure"]["passport"]["windows"]
        + api_result["international_departure"]["security"]["windows"]
        + api_result["international_arrival"]["windows"]
    )
    for w in all_windows:
        ts = datetime.fromisoformat(w["window_start"])
        assert ts.minute == 0
        assert ts.second == 0
        assert ts.microsecond == 0


def test_international_arrival_section_has_no_security_breakdown():
    """International Arrival SADECE passport wait gösterir - security alt-nesnesi YOK (bkz. api.py: international_arrival = passport_arrival, breakdown değil)."""
    import inspect

    from app.queue import api as api_module
    source = inspect.getsource(api_module.airport_predictions)
    assert '"international_arrival": passport_arrival' in source
    assert "international_security" not in source.split('"international_arrival"')[1].split("\n")[0]


def test_international_departure_has_independent_passport_and_security_series(api_result):
    """
    ADIM (International Departure Split Graphs): tek pencerenin İÇİNDE
    `passport`/`international_security` alt-nesneleri YOK artık - passport
    ve security KENDİ BAĞIMSIZ `windows` listesini taşıyan iki AYRI seri.
    """
    intl_dep = api_result["international_departure"]
    assert intl_dep["passport"]["windows"]
    assert intl_dep["security"]["windows"]
    for w in intl_dep["passport"]["windows"]:
        assert "passport" not in w
        assert "international_security" not in w


def test_domestic_security_shows_only_domestic_departure_demand_not_international(api_result):
    dom_windows = {
        w["window_start"]: w for w in api_result["domestic_security"]["windows"]
    }
    window_08 = dom_windows[DAY.replace(hour=8).isoformat()]
    assert window_08["flight_count"] == 1  # sadece DOM1
    assert window_08["expected_passengers"] == 150  # A320


def test_backend_estimated_wait_minutes_field_is_numeric_or_null_never_a_formatted_string(api_result):
    all_windows = (
        api_result["overall"]["windows"]
        + api_result["domestic_security"]["windows"]
        + api_result["international_departure"]["passport"]["windows"]
        + api_result["international_departure"]["security"]["windows"]
        + api_result["international_arrival"]["windows"]
    )
    for w in all_windows:
        value = w["estimated_wait_minutes"]
        assert value is None or isinstance(value, (int, float))
