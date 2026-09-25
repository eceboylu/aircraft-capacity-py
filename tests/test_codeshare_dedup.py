"""
ADIM (Codeshare Duplicate Physical Flights) - gerçek ZRH verisinde
doğrulanmış: LX972 (operating) + SQ2932/CX6615/AC6766/AZ3844 (aynı
fiziksel uçuşun 4 pazarlama kodu, `cs_flight_iata=LX972`) aynı anda
Source A'da geliyordu ve HER BİRİ ayrı bir passenger-demand ÜRETİYORDU
(demand inflation). `dedupe_codeshares()` bunu TEK canonical noktada
(`parse_source_a()` per-record ayrıştırmadan ÖNCE) düzeltir.
"""
from app.queue.domain.demand import DemandCalculator
from app.queue.ingestion.sources import dedupe_codeshares, parse_source_a

from .conftest import FakeCapacityResult, FakeResolver

_COMMON = {
    "dep_iata": "ZRH", "arr_iata": "BER",
    "dep_time_utc": "2026-09-25 09:50", "arr_time_utc": "2026-09-25 11:15",
    "status": "landed",
}


def _lx972_group():
    return [
        {**_COMMON, "flight_iata": "LX972", "airline_iata": "LX",
         "flight_number": "972", "cs_flight_iata": None},
        {**_COMMON, "flight_iata": "SQ2932", "airline_iata": "SQ",
         "flight_number": "2932", "cs_flight_iata": "LX972"},
        {**_COMMON, "flight_iata": "CX6615", "airline_iata": "CX",
         "flight_number": "6615", "cs_flight_iata": "LX972"},
        {**_COMMON, "flight_iata": "AC6766", "airline_iata": "AC",
         "flight_number": "6766", "cs_flight_iata": "LX972"},
        {**_COMMON, "flight_iata": "AZ3844", "airline_iata": "AZ",
         "flight_number": "3844", "cs_flight_iata": "LX972"},
    ]


def test_operating_plus_four_codeshares_dedupe_to_one_physical_flight():
    result = dedupe_codeshares(_lx972_group())
    assert len(result) == 1
    assert result[0]["flight_iata"] == "LX972"


def test_no_duplicate_ingestion_through_parse_source_a():
    parsed = parse_source_a(_lx972_group(), "departure", {})
    assert len(parsed) == 1
    assert parsed[0]["flight_iata"] == "LX972"


def test_passenger_demand_generated_once_not_five_times():
    """capacity=180 ise toplam demand 180 olmalı, 900 DEĞİL."""
    parsed = parse_source_a(_lx972_group(), "departure", {})
    resolver = FakeResolver({}, default=FakeCapacityResult(capacity=180))

    class _RowFlight:
        def __init__(self, row):
            self.aircraft_icao = row["aircraft_icao"]
            self.airline_iata = row["airline_iata"]

    demand = DemandCalculator(resolver)
    total = sum(demand.passenger_demand(_RowFlight(row)) for row in parsed)
    assert total == 180


def test_only_codeshare_fallback_keeps_one_canonical_record():
    """Operating kayıt hiç yoksa (only-codeshare), fiziksel uçuş KAYBOLMAMALI."""
    only_codeshares = [r for r in _lx972_group() if r["cs_flight_iata"]]
    result = dedupe_codeshares(only_codeshares)
    assert len(result) == 1
    # Deterministic: normalize edilmiş flight_iata'sı alfabetik en küçük olan.
    assert result[0]["flight_iata"] == "AC6766"


def test_genuine_route_time_coincidence_without_cs_field_is_not_deduped():
    """Sadece rota/saat çakışması (cs_flight_iata YOK) tek başına yeterli DEĞİL."""
    records = [
        {**_COMMON, "flight_iata": "XX100", "airline_iata": "XX",
         "flight_number": "100", "cs_flight_iata": None},
        {**_COMMON, "flight_iata": "YY200", "airline_iata": "YY",
         "flight_number": "200", "cs_flight_iata": None},
    ]
    result = dedupe_codeshares(records)
    assert len(result) == 2


def test_unrelated_flights_different_routes_never_merged():
    records = _lx972_group()
    unrelated = {
        "dep_iata": "ZRH", "arr_iata": "LHR",
        "dep_time_utc": "2026-09-25 09:50", "arr_time_utc": "2026-09-25 11:00",
        "status": "landed", "flight_iata": "BA123", "airline_iata": "BA",
        "flight_number": "123", "cs_flight_iata": None,
    }
    result = dedupe_codeshares(records + [unrelated])
    flight_iatas = {r["flight_iata"] for r in result}
    assert flight_iatas == {"LX972", "BA123"}
