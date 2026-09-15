"""
MADDE 5 doğrulaması - flight key / operational scheduled time.

Eski problem: build_flight_key() yöne bakmaksızın her zaman
dep_scheduled_utc kullanıyordu. Bu dosya, key'in artık yöne göre
doğru scheduled alanını kullandığını ve estimated/actual
değişikliklerinin key'i etkilemediğini kanıtlar.

Kaynak A kayıtları burada doğrudan sözlük olarak kurulur (gerçek
JSON dosyasına bağımlı değildir) - snake_case şema kullanılır.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.queue.constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    PASSPORT_RELEASE_BUFFER_MINUTES,
)
from app.queue.domain.demand import effective_time
from app.queue.ingestion.sources import build_flight_key, parse_source_a_record

NO_COUNTRIES: dict[str, str] = {}


def to_flight(row: dict) -> SimpleNamespace:
    """Parse edilmiş satırı effective_time()'ın beklediği nesneye çevirir."""
    return SimpleNamespace(**row)


# --------------------------------------------------------------------
# 1-2. Departure -> dep_scheduled_utc, Arrival -> arr_scheduled_utc
# --------------------------------------------------------------------

def test_departure_key_uses_dep_scheduled_utc():
    record = {
        "flight_number": "123", "airline_iata": "TK",
        "dep_iata": "IST", "arr_iata": "JFK",
        "dep_time_utc": "2026-09-15 08:00",
        "arr_time_utc": "2026-09-15 14:00",
        "status": "scheduled",
    }
    row = parse_source_a_record(record, DIRECTION_DEPARTURE, NO_COUNTRIES)
    assert row["flight_key"] == "TK_123_2026-09-15"


def test_arrival_key_uses_arr_scheduled_utc_not_dep():
    """
    Dep ve arr farklı takvim günlerine düşüyor (gece yarısını geçen
    uçuş) - arrival kaydı kesinlikle arr tarihini kullanmalı, dep
    tarihini DEĞİL.
    """
    record = {
        "flight_number": "123", "airline_iata": "TK",
        "dep_iata": "IST", "arr_iata": "JFK",
        "dep_time_utc": "2026-09-15 23:50",
        "arr_time_utc": "2026-09-16 06:30",
        "status": "scheduled",
    }
    dep_row = parse_source_a_record(record, DIRECTION_DEPARTURE, NO_COUNTRIES)
    arr_row = parse_source_a_record(record, DIRECTION_ARRIVAL, NO_COUNTRIES)

    assert dep_row["flight_key"] == "TK_123_2026-09-15"
    assert arr_row["flight_key"] == "TK_123_2026-09-16"
    assert dep_row["flight_key"] != arr_row["flight_key"]


# --------------------------------------------------------------------
# 3. Arrival: dep_scheduled boş, arr_scheduled dolu -> UNKDATE'e düşmemeli
# --------------------------------------------------------------------

def test_arrival_with_missing_dep_scheduled_uses_arr_scheduled():
    record = {
        "flight_number": "456", "airline_iata": "PC",
        "arr_iata": "SAW",
        "arr_time_utc": "2026-09-15 10:00",
        "status": "scheduled",
        # dep_time_utc kasıtlı olarak YOK
    }
    row = parse_source_a_record(record, DIRECTION_ARRIVAL, NO_COUNTRIES)

    assert row["flight_key"] == "PC_456_2026-09-15"
    assert "UNKDATE" not in row["flight_key"]
    assert row["dep_scheduled_utc"] is None
    assert row["arr_scheduled_utc"] == datetime(2026, 9, 15, 10, 0)


# --------------------------------------------------------------------
# 4-5. estimated/actual değişiklikleri key'i etkilemiyor
# --------------------------------------------------------------------

def test_departure_estimated_update_does_not_change_key():
    base = {
        "flight_number": "1", "airline_iata": "TK", "dep_iata": "IST",
        "arr_iata": "CDG", "dep_time_utc": "2026-09-15 18:00",
        "status": "scheduled",
    }
    before = parse_source_a_record(base, DIRECTION_DEPARTURE, NO_COUNTRIES)

    delayed = dict(base, dep_estimated_utc="2026-09-15 18:40")
    after = parse_source_a_record(delayed, DIRECTION_DEPARTURE, NO_COUNTRIES)

    assert before["flight_key"] == after["flight_key"] == "TK_1_2026-09-15"


def test_departure_actual_update_does_not_change_key():
    """scheduled=18:00 -> estimated=18:40 -> actual=18:45, key hep aynı."""
    base = {
        "flight_number": "1", "airline_iata": "TK", "dep_iata": "IST",
        "arr_iata": "CDG", "dep_time_utc": "2026-09-15 18:00",
        "status": "scheduled",
    }
    scheduled_only = parse_source_a_record(base, DIRECTION_DEPARTURE, NO_COUNTRIES)

    with_estimated = dict(base, dep_estimated_utc="2026-09-15 18:40")
    estimated_row = parse_source_a_record(with_estimated, DIRECTION_DEPARTURE, NO_COUNTRIES)

    with_actual = dict(with_estimated, dep_actual_utc="2026-09-15 18:45")
    actual_row = parse_source_a_record(with_actual, DIRECTION_DEPARTURE, NO_COUNTRIES)

    assert (
        scheduled_only["flight_key"]
        == estimated_row["flight_key"]
        == actual_row["flight_key"]
        == "TK_1_2026-09-15"
    )


def test_arrival_estimated_and_actual_updates_do_not_change_key():
    base = {
        "flight_number": "7", "airline_iata": "LH", "arr_iata": "FRA",
        "dep_iata": "IST", "arr_time_utc": "2026-09-15 12:00",
        "status": "scheduled",
    }
    scheduled_only = parse_source_a_record(base, DIRECTION_ARRIVAL, NO_COUNTRIES)

    updated = dict(
        base,
        arr_estimated_utc="2026-09-15 12:25",
        arr_actual_utc="2026-09-15 12:31",
    )
    updated_row = parse_source_a_record(updated, DIRECTION_ARRIVAL, NO_COUNTRIES)

    assert scheduled_only["flight_key"] == updated_row["flight_key"] == "LH_7_2026-09-15"


# --------------------------------------------------------------------
# 6. Midnight boundary
# --------------------------------------------------------------------

@pytest.mark.parametrize(
    "scheduled,expected_date",
    [
        ("2026-09-15 23:55", "2026-09-15"),
        ("2026-09-16 00:10", "2026-09-16"),
        ("2026-09-15 23:59", "2026-09-15"),
        ("2026-09-16 00:00", "2026-09-16"),
    ],
)
def test_midnight_boundary_departure(scheduled, expected_date):
    record = {
        "flight_number": "9", "airline_iata": "TK", "dep_iata": "IST",
        "arr_iata": "CDG", "dep_time_utc": scheduled, "status": "scheduled",
    }
    row = parse_source_a_record(record, DIRECTION_DEPARTURE, NO_COUNTRIES)
    assert row["flight_key"] == f"TK_9_{expected_date}"


@pytest.mark.parametrize(
    "scheduled,expected_date",
    [
        ("2026-09-15 23:55", "2026-09-15"),
        ("2026-09-16 00:10", "2026-09-16"),
    ],
)
def test_midnight_boundary_arrival(scheduled, expected_date):
    record = {
        "flight_number": "9", "airline_iata": "TK", "arr_iata": "CDG",
        "dep_iata": "IST", "arr_time_utc": scheduled, "status": "scheduled",
    }
    row = parse_source_a_record(record, DIRECTION_ARRIVAL, NO_COUNTRIES)
    assert row["flight_key"] == f"TK_9_{expected_date}"


# --------------------------------------------------------------------
# 7. Missing scheduled time - controlled fallback, silent yanlış tarih yok
# --------------------------------------------------------------------

def test_missing_scheduled_time_falls_back_to_explicit_unkdate_departure():
    record = {
        "flight_number": "1", "airline_iata": "TK", "dep_iata": "IST",
        "status": "scheduled",
        # dep_time_utc YOK
    }
    row = parse_source_a_record(record, DIRECTION_DEPARTURE, NO_COUNTRIES)
    assert row["flight_key"] == "TK_1_UNKDATE"


def test_missing_scheduled_time_falls_back_to_explicit_unkdate_arrival():
    record = {
        "flight_number": "1", "airline_iata": "TK", "arr_iata": "CDG",
        "status": "scheduled",
        # arr_time_utc YOK, dep_time_utc da YOK
    }
    row = parse_source_a_record(record, DIRECTION_ARRIVAL, NO_COUNTRIES)
    assert row["flight_key"] == "TK_1_UNKDATE"


def test_build_flight_key_none_input_is_explicit_not_silent():
    """build_flight_key() sözleşmesi doğrudan da doğrulanır."""
    assert build_flight_key("TK", "1", None) == "TK_1_UNKDATE"


# --------------------------------------------------------------------
# 8. Aynı gün aynı flight number - mevcut uniqueness davranışı
# --------------------------------------------------------------------

def test_same_flight_same_day_produces_same_key_across_refreshes():
    """İki ayrı 'refresh'te aynı kayıt aynı key'i üretmeli (upsert için şart)."""
    record = {
        "flight_number": "1", "airline_iata": "TK", "dep_iata": "IST",
        "arr_iata": "CDG", "dep_time_utc": "2026-09-15 08:00",
        "status": "scheduled",
    }
    first = parse_source_a_record(record, DIRECTION_DEPARTURE, NO_COUNTRIES)
    second = parse_source_a_record(dict(record), DIRECTION_DEPARTURE, NO_COUNTRIES)
    assert first["flight_key"] == second["flight_key"]


def test_different_flight_numbers_same_day_produce_different_keys():
    base = {
        "airline_iata": "TK", "dep_iata": "IST", "arr_iata": "CDG",
        "dep_time_utc": "2026-09-15 08:00", "status": "scheduled",
    }
    row1 = parse_source_a_record(dict(base, flight_number="1"), DIRECTION_DEPARTURE, NO_COUNTRIES)
    row2 = parse_source_a_record(dict(base, flight_number="2"), DIRECTION_DEPARTURE, NO_COUNTRIES)
    assert row1["flight_key"] != row2["flight_key"]


# --------------------------------------------------------------------
# 9. effective_time regresyonu - arrival source mapping (actual>estimated>scheduled)
# --------------------------------------------------------------------

def test_effective_time_regression_departure_precedence_unaffected():
    record = {
        "flight_number": "1", "airline_iata": "TK", "dep_iata": "IST",
        "arr_iata": "CDG", "dep_time_utc": "2026-09-15 18:00",
        "arr_time_utc": "2026-09-15 20:30",
        "dep_estimated_utc": "2026-09-15 18:40",
        "dep_actual_utc": "2026-09-15 18:45",
        "status": "active",
    }
    row = parse_source_a_record(record, DIRECTION_DEPARTURE, NO_COUNTRIES)
    flight = to_flight(row)

    # actual (18:45) kullanılmalı - scheduled (18:00) veya estimated (18:40)
    # değil. Süre 150 dk -> orta menzil uluslararası buffer = 60 dk.
    # 18:45 - 60 dk = 17:45.
    assert effective_time(flight) == datetime(2026, 9, 15, 17, 45)


def test_effective_time_regression_arrival_uses_actual_over_estimated_and_scheduled():
    record = {
        "flight_number": "7", "airline_iata": "LH", "arr_iata": "FRA",
        "dep_iata": "IST", "dep_time_utc": "2026-09-15 09:00",
        "arr_time_utc": "2026-09-15 12:00",
        "arr_estimated_utc": "2026-09-15 12:25",
        "arr_actual_utc": "2026-09-15 12:31",
        "status": "landed",
    }
    row = parse_source_a_record(record, DIRECTION_ARRIVAL, NO_COUNTRIES)
    flight = to_flight(row)

    expected = datetime(2026, 9, 15, 12, 31) + timedelta(
        minutes=PASSPORT_RELEASE_BUFFER_MINUTES
    )
    assert effective_time(flight) == expected


def test_effective_time_regression_arrival_falls_back_scheduled_when_no_actual_estimated():
    record = {
        "flight_number": "7", "airline_iata": "LH", "arr_iata": "FRA",
        "dep_iata": "IST", "dep_time_utc": "2026-09-15 09:00",
        "arr_time_utc": "2026-09-15 12:00",
        "status": "scheduled",
    }
    row = parse_source_a_record(record, DIRECTION_ARRIVAL, NO_COUNTRIES)
    flight = to_flight(row)

    expected = datetime(2026, 9, 15, 12, 0) + timedelta(
        minutes=PASSPORT_RELEASE_BUFFER_MINUTES
    )
    assert effective_time(flight) == expected
