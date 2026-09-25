"""
ADIM (Aircraft Match Safety Guard) - kullanıcı talebi: flight number tek
başına eşleşme için YETERSİZ - gerçek veride TK2025 için Kaynak B'deki
TEK aday 16 GÜN eskiydi ve önceki (sınırsız/rota-kontrolsüz) mantık bunu
sessizce kabul ediyordu. Artık eşleşme için ÜÇÜ BİRDEN zorunlu:
  A) normalize edilmiş flight identity eşleşmesi
  B) dep_iata eşleşmesi
  C) arr_iata eşleşmesi
  D) `updated` (ADS-B gözlem zamanı - TARİFE SAATİ DEĞİL) `operational_
     scheduled`'a `AIRCRAFT_MATCH_MAX_AGE_HOURS` içinde olmalı
Birden fazla eşit derecede yakın aday ÇELİŞEN ICAO taşıyorsa tahmin
yürütülmez (None).
"""
from datetime import datetime, timedelta, timezone

from app.queue.ingestion.sources import (
    AIRCRAFT_MATCH_MAX_AGE_HOURS,
    build_aircraft_index,
    parse_source_a_record,
)


def _source_a_record(**overrides):
    record = {
        "flight_iata": "TK2025",
        "airline_iata": "TK",
        "flight_number": "2025",
        "dep_iata": "ASR",
        "arr_iata": "IST",
        "dep_time_utc": "2026-09-25T13:45:00Z",
        "arr_time_utc": "2026-09-25T15:20:00Z",
        "status": "scheduled",
    }
    record.update(overrides)
    return record


def _source_b_record(*, hours_before_scheduled, dep_iata="ASR", arr_iata="IST",
                      icao="B739", flight_iata="TK2025"):
    # direction="arrival" -> operational_scheduled = arr_time_utc (15:20), bkz.
    # `canonical_operational_time()`. Adayların yaşı BUNA göre hesaplanmalı.
    scheduled = datetime(2026, 9, 25, 15, 20, tzinfo=timezone.utc)
    updated = scheduled - timedelta(hours=hours_before_scheduled)
    return {
        "flight_iata": flight_iata,
        "aircraft_icao": icao,
        "dep_iata": dep_iata,
        "arr_iata": arr_iata,
        "updated": int(updated.timestamp()),
    }


# A) Kaynak A kendi aircraft_icao'sunu taşıyorsa Kaynak B hiç denenmemeli.
def test_own_source_a_icao_is_preserved():
    index = build_aircraft_index([_source_b_record(hours_before_scheduled=1, icao="B739")])
    result = parse_source_a_record(
        _source_a_record(aircraft_icao="A21N"), "arrival", {}, index
    )
    assert result["aircraft_icao"] == "A21N"


# B) aynı flight + aynı rota + son zamanlı aday -> eşleşme.
def test_same_flight_same_route_recent_candidate_matches():
    index = build_aircraft_index([_source_b_record(hours_before_scheduled=2)])
    result = parse_source_a_record(_source_a_record(), "arrival", {}, index)
    assert result["aircraft_icao"] == "B739"
    assert result["aircraft_match_found"] is True


# C) aynı flight + farklı dep_iata -> reddedilir.
def test_same_flight_different_dep_is_rejected():
    index = build_aircraft_index([
        _source_b_record(hours_before_scheduled=2, dep_iata="AYT")
    ])
    result = parse_source_a_record(_source_a_record(), "arrival", {}, index)
    assert result["aircraft_icao"] is None
    assert result["aircraft_match_found"] is False


# D) aynı flight + farklı arr_iata -> reddedilir.
def test_same_flight_different_arr_is_rejected():
    index = build_aircraft_index([
        _source_b_record(hours_before_scheduled=2, arr_iata="AYT")
    ])
    result = parse_source_a_record(_source_a_record(), "arrival", {}, index)
    assert result["aircraft_icao"] is None
    assert result["aircraft_match_found"] is False


# E) aynı flight + aynı rota + 11 gün (>> max age) eski -> reddedilir.
def test_same_flight_same_route_11_days_old_is_rejected():
    index = build_aircraft_index([
        _source_b_record(hours_before_scheduled=11 * 24)
    ])
    result = parse_source_a_record(_source_a_record(), "arrival", {}, index)
    assert result["aircraft_icao"] is None
    assert result["aircraft_match_found"] is False


# F) birden fazla geçerli aday -> en yakın timestamp kazanır.
def test_multiple_valid_candidates_nearest_timestamp_wins():
    index = build_aircraft_index([
        _source_b_record(hours_before_scheduled=20, icao="A320"),
        _source_b_record(hours_before_scheduled=2, icao="B739"),
    ])
    result = parse_source_a_record(_source_a_record(), "arrival", {}, index)
    assert result["aircraft_icao"] == "B739"


# G) eşit derecede yakın adaylar ÇELİŞEN ICAO taşıyorsa -> None.
def test_equally_close_conflicting_icaos_yield_no_match():
    index = build_aircraft_index([
        _source_b_record(hours_before_scheduled=2, icao="A320"),
        _source_b_record(hours_before_scheduled=2, icao="B739"),
    ])
    result = parse_source_a_record(_source_a_record(), "arrival", {}, index)
    assert result["aircraft_icao"] is None
    assert result["aircraft_match_found"] is False


# H) hiç aday yok -> None / fallback.
def test_no_candidate_falls_back_to_none():
    result = parse_source_a_record(_source_a_record(), "arrival", {}, aircraft_index={})
    assert result["aircraft_icao"] is None
    assert result["aircraft_match_found"] is False


def test_max_age_threshold_is_36_hours():
    """Eşiğin kendisi - `AIRCRAFT_MATCH_MAX_AGE_HOURS` beklenmedik şekilde değişmemeli."""
    assert AIRCRAFT_MATCH_MAX_AGE_HOURS == 36.0


def test_candidate_exactly_at_threshold_boundary_matches():
    """Sınırda (<=) hâlâ kabul edilmeli, sadece sınırın ÜZERİ reddedilir."""
    index = build_aircraft_index([
        _source_b_record(hours_before_scheduled=AIRCRAFT_MATCH_MAX_AGE_HOURS)
    ])
    result = parse_source_a_record(_source_a_record(), "arrival", {}, index)
    assert result["aircraft_icao"] == "B739"


def test_candidate_just_over_threshold_is_rejected():
    index = build_aircraft_index([
        _source_b_record(hours_before_scheduled=AIRCRAFT_MATCH_MAX_AGE_HOURS + 0.01)
    ])
    result = parse_source_a_record(_source_a_record(), "arrival", {}, index)
    assert result["aircraft_icao"] is None
