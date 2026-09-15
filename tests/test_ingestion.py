"""
Adım 5 doğrulaması - kaynak ayrıştırma, eşleştirme ve upsert.

Veritabanı gerektiren testler in-memory SQLite kullanır; diskteki
database.sqlite'a DOKUNULMAZ.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.ingestion.refresh import (
    aircraft_changes_for_airport,
    refresh_flights,
)
from app.queue.ingestion.sources import (
    aircraft_match_rate,
    build_aircraft_index,
    build_flight_key,
    normalize_flight_number,
    parse_source_a,
    parse_source_a_record,
    parse_utc,
    resolve_location,
)
from app.queue.models import Flight, FlightEvent


COUNTRIES = {"IST": "TR", "ESB": "TR", "SAW": "TR", "CDG": "FR", "JFK": "US"}


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    yield db
    db.close()


# --------------------------------------------------------------------
# Uçuş numarası normalizasyonu - Senaryo 14
# --------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AC 72", "AC72"),
        ("ac 72", "AC72"),
        ("AC-72", "AC72"),
        ("  AC72  ", "AC72"),
        ("TK1", "TK1"),
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_normalize_flight_number(raw, expected):
    assert normalize_flight_number(raw) == expected


def test_aircraft_index_matches_spaced_flight_number():
    source_b = [{"flight_iata": "AC72", "aircraft_icao": "B77W"}]
    index = build_aircraft_index(source_b)
    assert index[normalize_flight_number("AC 72")] == "B77W"


def test_aircraft_index_skips_records_without_type():
    source_b = [
        {"flight_iata": "TK1", "aircraft_icao": None},
        {"flight_iata": "TK2", "aircraft_icao": ""},
        {"flight_iata": "TK3", "aircraft_icao": "A321"},
    ]
    index = build_aircraft_index(source_b)
    assert "TK1" not in index and "TK2" not in index
    assert index["TK3"] == "A321"


# --------------------------------------------------------------------
# Enrichment - Senaryo 14
# --------------------------------------------------------------------

def test_enrichment_fills_aircraft_from_source_b():
    record = {
        "flight_iata": "AC 72", "flight_number": "72", "airline_iata": "AC",
        "dep_iata": "CDG", "arr_iata": "JFK", "dep_time_utc": "2026-09-14 08:00",
        "arr_time_utc": "2026-09-14 14:00", "status": "scheduled",
        "aircraft_icao": None,
    }
    index = build_aircraft_index([{"flight_iata": "AC72", "aircraft_icao": "B77W"}])
    row = parse_source_a_record(record, "departure", COUNTRIES, index)

    assert row["aircraft_icao"] == "B77W"
    assert row["aircraft_match_found"] is True


def test_no_match_leaves_aircraft_none():
    """Eşleşme yoksa uydurma tip ÜRETİLMEZ."""
    record = {
        "flight_iata": "XX999", "flight_number": "999", "airline_iata": "XX",
        "dep_iata": "CDG", "arr_iata": "JFK", "dep_time_utc": "2026-09-14 08:00",
        "status": "scheduled", "aircraft_icao": None,
    }
    row = parse_source_a_record(record, "departure", COUNTRIES, {})
    assert row["aircraft_icao"] is None
    assert row["aircraft_match_found"] is False


def test_source_a_own_aircraft_wins_over_source_b():
    record = {
        "flight_iata": "AC72", "flight_number": "72", "airline_iata": "AC",
        "dep_iata": "CDG", "arr_iata": "JFK", "dep_time_utc": "2026-09-14 08:00",
        "status": "scheduled", "aircraft_icao": "A359",
    }
    index = build_aircraft_index([{"flight_iata": "AC72", "aircraft_icao": "B77W"}])
    row = parse_source_a_record(record, "departure", COUNTRIES, index)
    assert row["aircraft_icao"] == "A359"


def test_match_rate_calculation():
    rows = [
        {"aircraft_match_found": True},
        {"aircraft_match_found": True},
        {"aircraft_match_found": False},
        {"aircraft_match_found": False},
    ]
    assert aircraft_match_rate(rows) == 0.5
    assert aircraft_match_rate([]) == 0.0


# --------------------------------------------------------------------
# location / direction / airport_iata türetimi
# --------------------------------------------------------------------

def test_location_domestic_when_same_country():
    assert resolve_location("IST", "ESB", COUNTRIES) == "domestic"


def test_location_international_when_different_country():
    assert resolve_location("IST", "CDG", COUNTRIES) == "international"


def test_location_international_when_country_unknown():
    """Bilinmiyorsa international - passport yükünü eksik saymamak için."""
    assert resolve_location("IST", "ZZZ", COUNTRIES) == "international"


def test_airport_iata_follows_direction():
    """YASAK 5: kapsam havalimanı yöne göre belirlenir, sabit değil."""
    record = {
        "flight_iata": "TK1", "flight_number": "1", "airline_iata": "TK",
        "dep_iata": "IST", "arr_iata": "CDG", "dep_time_utc": "2026-09-14 08:00",
        "status": "scheduled",
    }
    dep_row = parse_source_a_record(record, "departure", COUNTRIES, {})
    arr_row = parse_source_a_record(record, "arrival", COUNTRIES, {})
    assert dep_row["airport_iata"] == "IST"
    assert arr_row["airport_iata"] == "CDG"


def test_record_without_relevant_airport_is_skipped():
    record = {"flight_number": "1", "airline_iata": "TK", "status": "scheduled"}
    assert parse_source_a_record(record, "departure", COUNTRIES, {}) is None


def test_parse_source_a_skips_invalid_rows():
    records = [
        {"dep_iata": "IST", "flight_number": "1", "airline_iata": "TK",
         "dep_time_utc": "2026-09-14 08:00", "status": "scheduled"},
        {"flight_number": "2", "airline_iata": "TK", "status": "scheduled"},
    ]
    rows = parse_source_a(records, "departure", COUNTRIES, {})
    assert len(rows) == 1


# --------------------------------------------------------------------
# Zaman ve anahtar
# --------------------------------------------------------------------

def test_parse_utc():
    assert parse_utc("2026-09-14 08:30") == datetime(2026, 9, 14, 8, 30)
    assert parse_utc(None) is None
    assert parse_utc("") is None
    assert parse_utc("bozuk-veri") is None


def test_flight_key_is_stable_across_refreshes():
    key1 = build_flight_key("TK", "1", datetime(2026, 9, 14, 8, 0))
    key2 = build_flight_key("TK", "1", datetime(2026, 9, 14, 23, 59))
    assert key1 == key2 == "TK_1_2026-09-14"


def test_flight_key_differs_across_days():
    a = build_flight_key("TK", "1", datetime(2026, 9, 14, 8, 0))
    b = build_flight_key("TK", "1", datetime(2026, 9, 15, 8, 0))
    assert a != b


# --------------------------------------------------------------------
# YASAK 4 - upsert, satır şişmesi yok
# --------------------------------------------------------------------

def base_row(**overrides):
    row = {
        "flight_key": "TK_1_2026-09-14",
        "airport_iata": "IST",
        "direction": "departure",
        "location": "international",
        "airline_iata": "TK",
        "flight_number": "1",
        "flight_iata": "TK1",
        "aircraft_icao": "A321",
        "aircraft_match_found": True,
        "dep_iata": "IST",
        "arr_iata": "CDG",
        "dep_scheduled_utc": datetime(2026, 9, 14, 8, 0),
        "dep_estimated_utc": None,
        "dep_actual_utc": None,
        "arr_scheduled_utc": datetime(2026, 9, 14, 11, 0),
        "arr_estimated_utc": None,
        "arr_actual_utc": None,
        "dep_terminal": None, "dep_gate": None,
        "arr_terminal": None, "arr_gate": None,
        "status": "scheduled",
    }
    row.update(overrides)
    return row


def count(session, model):
    return session.execute(select(func.count()).select_from(model)).scalar()


def test_repeated_refresh_does_not_create_new_rows(session):
    rows = [base_row()]
    for _ in range(5):
        refresh_flights(session, rows)
    assert count(session, Flight) == 1


def test_unchanged_refresh_writes_no_events(session):
    rows = [base_row()]
    refresh_flights(session, rows)
    for _ in range(4):
        summary = refresh_flights(session, rows)
        assert summary["events_written"] == 0
    assert count(session, FlightEvent) == 0


def test_aircraft_change_writes_single_event(session):
    refresh_flights(session, [base_row(aircraft_icao="A321")])
    summary = refresh_flights(session, [base_row(aircraft_icao="B77W")])

    assert summary["events_written"] == 1
    event = session.execute(select(FlightEvent)).scalar_one()
    assert event.event_type == "AIRCRAFT_CHANGED"
    assert event.old_value == "A321"
    assert event.new_value == "B77W"
    assert count(session, Flight) == 1


def test_cancellation_writes_event(session):
    refresh_flights(session, [base_row()])
    refresh_flights(session, [base_row(status="cancelled")])

    events = session.execute(select(FlightEvent)).scalars().all()
    assert [e.event_type for e in events] == ["CANCELLED"]


def test_diversion_writes_event(session):
    refresh_flights(session, [base_row()])
    refresh_flights(session, [base_row(status="diverted")])

    events = session.execute(select(FlightEvent)).scalars().all()
    assert [e.event_type for e in events] == ["DIVERTED"]


def test_delay_event_only_after_threshold(session):
    refresh_flights(session, [base_row()])
    # 5 dakika gecikme - eşik altı, event yok
    refresh_flights(session, [base_row(dep_actual_utc=datetime(2026, 9, 14, 8, 5))])
    assert count(session, FlightEvent) == 0

    # 40 dakika - eşiği aşıyor, event yazılır
    refresh_flights(session, [base_row(dep_actual_utc=datetime(2026, 9, 14, 8, 40))])
    events = session.execute(select(FlightEvent)).scalars().all()
    assert [e.event_type for e in events] == ["DELAYED"]
    assert events[0].new_value == "40"


def test_same_delay_repeated_writes_no_duplicate_event(session):
    delayed = base_row(dep_actual_utc=datetime(2026, 9, 14, 8, 40))
    refresh_flights(session, [base_row()])
    refresh_flights(session, [delayed])
    before = count(session, FlightEvent)

    for _ in range(3):
        refresh_flights(session, [delayed])

    assert count(session, FlightEvent) == before


def test_multiple_airports_stay_independent(session):
    """YASAK 5: farklı havalimanları birbirine karışmaz."""
    refresh_flights(session, [
        base_row(flight_key="TK_1_2026-09-14", airport_iata="IST"),
        base_row(flight_key="PC_9_2026-09-14", airport_iata="SAW"),
    ])
    refresh_flights(session, [
        base_row(flight_key="TK_1_2026-09-14", airport_iata="IST", aircraft_icao="B77W"),
        base_row(flight_key="PC_9_2026-09-14", airport_iata="SAW"),
    ])

    # base_row: dep 08:00, arr 11:00 -> 180 dk (orta menzil) -> buffer 60 dk
    # -> effective_time 07:00.
    assert aircraft_changes_for_airport(session, "IST") == {
        "TK_1_2026-09-14": [("A321", "B77W", datetime(2026, 9, 14, 7, 0))]
    }
    assert aircraft_changes_for_airport(session, "SAW") == {}


def test_refresh_updates_field_values(session):
    refresh_flights(session, [base_row(dep_gate="A1")])
    refresh_flights(session, [base_row(dep_gate="B7")])

    flight = session.execute(select(Flight)).scalar_one()
    assert flight.dep_gate == "B7"
