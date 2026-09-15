"""
MADDE 6 doğrulaması - gerçek MySQL dump formatındaki flight_airports.sql
dosyasının güvenilir parse/import edilmesi.

Eski problem: _unquote() sıralı/bağımsız .replace() çağrılarıyla SQL
katmanının escape'ini JSON katmanının escape'iyle karıştırıyordu.
Örneğin JSON'un kendi `\n` kaçışı (bir JSON string içinde), SQL dump
tarafından backslash'ı ikiye katlanmış halde (`\\n`) saklanıyor;
sıralı replace bunu gerçek bir newline karakterine çevirip
json.loads()'u kırabiliyordu. Bu dosya hem gerçek dosya üzerinde hem
de sentetik (TEST) fixture'larla bu sınıf hataların düzeldiğini
kanıtlar.

Sentetik SQL parçaları AÇIKÇA TEST verisidir - gerçek bir havalimanı
kaydını temsil etmezler.
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.ingestion.airports_import import (
    country_lookup,
    import_airports,
    parse_airports_sql,
)
from app.queue.models import Airport

REAL_SQL_PATH = "data/flight_airports.sql"

BS = chr(92)   # backslash - testlerde ham kaçış dizilerini netleştirmek için
SQ = chr(39)   # tek tırnak


def write_sql(tmp_path, *row_tuples: str, table="flight_airports"):
    """
    Verilen VALUES satırlarını gerçek bir phpMyAdmin dump'ının
    kabuğuna (CREATE TABLE + INSERT INTO ... VALUES ...;) sararak
    geçici bir .sql dosyasına yazar.
    """
    body = ",\n".join(row_tuples)
    content = (
        "-- phpMyAdmin SQL Dump (TEST fixture)\n"
        "SET SQL_MODE = \"NO_AUTO_VALUE_ON_ZERO\";\n"
        "START TRANSACTION;\n\n"
        f"DROP TABLE IF EXISTS `{table}`;\n"
        f"CREATE TABLE IF NOT EXISTS `{table}` ("
        "`id` bigint, `iata_code` varchar(50), `icao_code` varchar(50), "
        "`airport_name` varchar(500), `customized` text);\n\n"
        f"INSERT INTO `{table}` "
        "(`id`, `iata_code`, `icao_code`, `airport_name`, `customized`) VALUES\n"
        f"{body};\n"
    )
    path = tmp_path / "fixture_airports.sql"
    path.write_text(content, encoding="utf-8")
    return str(path)


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


# --------------------------------------------------------------------
# 1. Gerçek SQL fixture - production-format dosya üzerinde
# --------------------------------------------------------------------

def test_real_sql_file_parses_without_crashing():
    rows = parse_airports_sql(REAL_SQL_PATH)
    assert len(rows) == 9766


def test_real_sql_file_known_airport_with_slash_escaped_timezone():
    """
    Gerçek satır: 'Pacific\\/Port_Moresby' (JSON'un kendi \\/ kaçışı,
    SQL tarafından ayrıca backslash-escape edilmiş). Doğru çözülmeli.
    """
    rows = {r["iata_code"]: r for r in parse_airports_sql(REAL_SQL_PATH)}
    assert rows["SXW"]["timezone"] == "Pacific/Port_Moresby"
    assert BS not in rows["SXW"]["timezone"]


def test_real_sql_file_known_airport_with_escaped_apostrophe():
    rows = {r["iata_code"]: r for r in parse_airports_sql(REAL_SQL_PATH)}
    assert rows["SAH"]["airport_name"] == "Sana'a International Airport"
    assert rows["SZX"]["airport_name"] == "Shenzhen Bao'an International Airport"


def test_real_sql_file_no_leftover_backslash_anywhere():
    rows = parse_airports_sql(REAL_SQL_PATH)
    leaked = [
        r for r in rows
        if any(isinstance(v, str) and BS in v for v in r.values())
    ]
    assert leaked == []


def test_real_sql_file_known_airport_full_fields():
    rows = {r["iata_code"]: r for r in parse_airports_sql(REAL_SQL_PATH)}
    ist = rows["IST"]
    assert ist["icao_code"] == "LTFM"
    assert ist["airport_name"] == "Istanbul Airport"
    assert ist["country_code"] == "TR"
    assert ist["timezone"] == "Europe/Istanbul"


# --------------------------------------------------------------------
# 2. Escape karakter testleri (TEST fixture)
# --------------------------------------------------------------------

def test_escaped_apostrophe_backslash_style(tmp_path):
    row = (
        f"(1, 'ZZ1', 'ZZZZ', 'O{BS}{SQ}Hare TEST Field', NULL)"
    )
    path = write_sql(tmp_path, row)
    rows = parse_airports_sql(path)
    assert rows[0]["airport_name"] == "O'Hare TEST Field"


def test_escaped_backslash_in_plain_field(tmp_path):
    """Ham içerikte gerçek bir backslash karakteri: \\\\ -> \\ """
    row = f"(1, 'ZZ2', 'ZZZZ', 'Back{BS}{BS}Slash TEST', NULL)"
    path = write_sql(tmp_path, row)
    rows = parse_airports_sql(path)
    assert rows[0]["airport_name"] == "Back" + BS + "Slash TEST"


def test_escaped_double_quote_inside_json(tmp_path):
    customized = (
        f"'{{{BS}\"timezone{BS}\":{BS}\"UTC{BS}\",{BS}\"city_code{BS}\":"
        f"{BS}\"ZZ3{BS}\",{BS}\"country_code{BS}\":{BS}\"XX{BS}\"}}'"
    )
    row = f"(1, 'ZZ3', 'ZZZZ', 'TEST Field', {customized})"
    path = write_sql(tmp_path, row)
    rows = parse_airports_sql(path)
    assert rows[0]["timezone"] == "UTC"
    assert rows[0]["country_code"] == "XX"


def test_json_own_newline_escape_survives_sql_layer_without_crashing(tmp_path):
    """
    KRİTİK REGRESYON: JSON'un kendi \\n kaçışı (bir JSON string
    içinde), SQL dump'ında backslash'ı ikiye katlanmış halde saklanır
    (ham SQL metninde \\\\n görünür). Eski sıralı .replace() bunu
    gerçek bir newline'a çevirip json.loads()'u kırıyordu.
    """
    customized = (
        f"'{{{BS}\"timezone{BS}\":{BS}\"Line1{BS}{BS}nLine2{BS}\","
        f"{BS}\"city_code{BS}\":{BS}\"ZZ4{BS}\","
        f"{BS}\"country_code{BS}\":{BS}\"XX{BS}\"}}'"
    )
    row = f"(1, 'ZZ4', 'ZZZZ', 'TEST Field', {customized})"
    path = write_sql(tmp_path, row)

    rows = parse_airports_sql(path)   # crash etmemeli

    assert rows[0]["timezone"] == "Line1\nLine2"


def test_escaped_carriage_return_in_plain_field(tmp_path):
    row = f"(1, 'ZZ5', 'ZZZZ', 'Before{BS}rAfter TEST', NULL)"
    path = write_sql(tmp_path, row)
    rows = parse_airports_sql(path)
    assert rows[0]["airport_name"] == "Before\rAfter TEST"


def test_doubled_single_quote_escape_style(tmp_path):
    """
    NO_BACKSLASH_ESCAPES modu (bu dosyada kullanılmıyor ama parser
    dayanıklı olmalı): '' -> tek tırnak.
    """
    row = f"(1, 'ZZ6', 'ZZZZ', 'O{SQ}{SQ}Hare TEST Field', NULL)"
    path = write_sql(tmp_path, row)
    rows = parse_airports_sql(path)
    assert rows[0]["airport_name"] == "O'Hare TEST Field"


# --------------------------------------------------------------------
# 3. Bozuk/geçersiz JSON - crash yok, alan None kalır
# --------------------------------------------------------------------

def test_invalid_json_does_not_crash_and_fields_stay_none(tmp_path):
    row = "(1, 'ZZ7', 'ZZZZ', 'TEST Field', '{bu gecersiz json degil}')"
    path = write_sql(tmp_path, row)

    rows = parse_airports_sql(path)   # crash ETMEMELİ

    assert len(rows) == 1
    assert rows[0]["city_code"] is None
    assert rows[0]["country_code"] is None
    assert rows[0]["timezone"] is None
    # veri UYDURULMAMALI
    assert rows[0]["airport_name"] == "TEST Field"


def test_truncated_json_does_not_crash(tmp_path):
    row = "(1, 'ZZ8', 'ZZZZ', 'TEST Field', '{\"timezone\":\"UTC\"')"
    path = write_sql(tmp_path, row)
    rows = parse_airports_sql(path)
    assert len(rows) == 1
    assert rows[0]["timezone"] is None


# --------------------------------------------------------------------
# 4. Eksik alanlar - mevcut business rule korunuyor, tahmin yok
# --------------------------------------------------------------------

def test_missing_customized_field_leaves_derived_fields_none(tmp_path):
    row = "(1, 'ZZ9', 'ZZZZ', 'TEST Field', NULL)"
    path = write_sql(tmp_path, row)
    rows = parse_airports_sql(path)

    assert rows[0]["city_code"] is None
    assert rows[0]["country_code"] is None
    assert rows[0]["timezone"] is None


def test_empty_icao_becomes_none_not_empty_string(tmp_path):
    """Gerçek dosyada da görülen desen: ICAO bilinmiyorsa '' (boş string)."""
    row = "(1, 'ZA1', '', 'TEST Field', NULL)"
    path = write_sql(tmp_path, row)
    rows = parse_airports_sql(path)
    assert rows[0]["icao_code"] is None


def test_partial_customized_json_only_fills_present_keys(tmp_path):
    customized = f"'{{{BS}\"timezone{BS}\":{BS}\"UTC{BS}\"}}'"
    row = f"(1, 'ZA2', 'ZZZZ', 'TEST Field', {customized})"
    path = write_sql(tmp_path, row)
    rows = parse_airports_sql(path)

    assert rows[0]["timezone"] == "UTC"
    assert rows[0]["city_code"] is None       # kaynakta yoktu, tahmin edilmedi
    assert rows[0]["country_code"] is None


# --------------------------------------------------------------------
# 5. Duplicate IATA - deterministic davranış
# --------------------------------------------------------------------

def test_duplicate_iata_keeps_first_occurrence_deterministically(tmp_path):
    row1 = "(1, 'DUP', 'AAAA', 'First TEST Airport', NULL)"
    row2 = "(2, 'DUP', 'BBBB', 'Second TEST Airport', NULL)"
    path = write_sql(tmp_path, row1, row2)

    rows = parse_airports_sql(path)

    matching = [r for r in rows if r["iata_code"] == "DUP"]
    assert len(matching) == 1
    assert matching[0]["airport_name"] == "First TEST Airport"
    assert matching[0]["icao_code"] == "AAAA"


def test_duplicate_iata_deterministic_across_repeated_parses(tmp_path):
    """Rastgele seçim değil - her parse'ta AYNI kayıt kazanmalı."""
    row1 = "(1, 'DUP', 'AAAA', 'First TEST Airport', NULL)"
    row2 = "(2, 'DUP', 'BBBB', 'Second TEST Airport', NULL)"
    path = write_sql(tmp_path, row1, row2)

    first_parse = parse_airports_sql(path)
    second_parse = parse_airports_sql(path)

    assert first_parse == second_parse


# --------------------------------------------------------------------
# 6. Idempotent re-import
# --------------------------------------------------------------------

def test_reimporting_same_file_does_not_duplicate_rows(session, tmp_path):
    row1 = "(1, 'ZB1', 'AAAA', 'TEST Airport One', NULL)"
    row2 = "(2, 'ZB2', 'BBBB', 'TEST Airport Two', NULL)"
    path = write_sql(tmp_path, row1, row2)

    first_count = import_airports(session, path)
    total_after_first = session.query(Airport).count()

    second_count = import_airports(session, path)
    total_after_second = session.query(Airport).count()

    assert first_count == 2
    assert second_count == 2
    assert total_after_first == 2
    assert total_after_second == 2      # şişme yok


def test_reimport_updates_changed_fields_without_new_rows(session, tmp_path):
    v1 = "(1, 'ZB3', 'AAAA', 'Old TEST Name', NULL)"
    path_v1 = write_sql(tmp_path, v1)
    import_airports(session, path_v1)

    v2 = "(1, 'ZB3', 'AAAA', 'New TEST Name', NULL)"
    path_v2 = write_sql(tmp_path, v2)
    import_airports(session, path_v2)

    assert session.query(Airport).count() == 1
    airport = session.get(Airport, "ZB3")
    assert airport.airport_name == "New TEST Name"


# --------------------------------------------------------------------
# 7. Mevcut airport lookup davranışının regresyonu
# --------------------------------------------------------------------

def test_country_lookup_regression_on_real_data(session):
    import_airports(session, REAL_SQL_PATH)
    lookup = country_lookup(session)

    assert lookup["IST"] == "TR"
    assert lookup["CDG"] == "FR"
    assert lookup["JFK"] == "US"
    assert len(lookup) > 9000


def test_country_lookup_excludes_rows_without_country_code(session, tmp_path):
    with_country = f"(1, 'ZC1', 'ZZZZ', 'TEST A', '{{{BS}\"country_code{BS}\":{BS}\"XX{BS}\"}}')"
    without_country = "(2, 'ZC2', 'ZZZZ', 'TEST B', NULL)"
    path = write_sql(tmp_path, with_country, without_country)

    import_airports(session, path)
    lookup = country_lookup(session)

    assert lookup.get("ZC1") == "XX"
    assert "ZC2" not in lookup
