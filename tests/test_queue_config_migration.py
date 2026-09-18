"""Additive queue config migration; no production DB is opened or reset."""

from sqlalchemy import create_engine, inspect, text

import app.db as db_module


def test_existing_sqlite_config_table_gets_only_missing_queue_columns(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "legacy.sqlite"
    isolated_engine = create_engine(f"sqlite:///{database_path}")
    with isolated_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE airport_operational_configs (
                airport_iata VARCHAR(10) PRIMARY KEY,
                passport_counter_count INTEGER NOT NULL DEFAULT 4,
                passport_staff_count INTEGER NOT NULL DEFAULT 8
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO airport_operational_configs "
            "(airport_iata, passport_counter_count, passport_staff_count) "
            "VALUES ('IST', 4, 8)"
        )

    monkeypatch.setattr(db_module, "engine", isolated_engine)
    db_module._migrate_sqlite_table(
        "airport_operational_configs", db_module._SQLITE_OPERATIONAL_CONFIG_COLUMNS
    )
    db_module._migrate_sqlite_table(
        "airport_operational_configs", db_module._SQLITE_OPERATIONAL_CONFIG_COLUMNS
    )  # idempotent

    columns = {
        column["name"]
        for column in inspect(isolated_engine).get_columns(
            "airport_operational_configs"
        )
    }
    assert {
        "passport_service_time_minutes",
        "security_lane_count",
        "security_service_time_minutes",
        "domestic_security_lane_count",
        "international_security_lane_count",
        "passport_departure_server_count",
        "passport_arrival_server_count",
    }.issubset(columns)

    with isolated_engine.connect() as connection:
        row = connection.execute(text(
            "SELECT airport_iata, passport_counter_count, passport_staff_count, "
            "passport_service_time_minutes, security_lane_count, "
            "security_service_time_minutes, domestic_security_lane_count, "
            "international_security_lane_count, "
            "passport_departure_server_count, passport_arrival_server_count "
            "FROM airport_operational_configs WHERE airport_iata='IST'"
        )).one()
    # ADIM (Domestic/International Security Lane Ayrımı): mevcut satır
    # için yeni kolonlar `security_lane_count` ile AYNI güvenli
    # varsayılanı (8) alır - additive migration mevcut davranışı BOZMAZ.
    # ADIM (Airport-Scale): yeni passport departure/arrival server
    # kolonları NULL alır (explicit override YOK demektir - uydurma
    # bir sayı YAZILMAZ, config.py scale-derived değere düşer).
    assert tuple(row) == ("IST", 4, 8, 1.5, 8, 1.0, 8, 8, None, None)


def test_existing_sqlite_airports_table_gets_missing_timezone_column(
    tmp_path, monkeypatch
):
    """
    ADIM (Airport.timezone Additive Migration): `Airport.timezone` bu
    kolonun modele eklenmesinden ÖNCE oluşturulmuş bir `airports`
    tablosuna (`create_all()` mevcut tabloya kolon eklemediği için)
    sonradan, veri kaybı olmadan eklenmeli - aksi halde
    `resolve_airport_timezone()`'u kullanan her sorgu
    "no such column: airports.timezone" ile çöker.
    """
    database_path = tmp_path / "legacy-airports.sqlite"
    isolated_engine = create_engine(f"sqlite:///{database_path}")
    with isolated_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE airports (
                iata_code VARCHAR(10) PRIMARY KEY,
                icao_code VARCHAR(10),
                airport_name VARCHAR(500),
                city_code VARCHAR(10),
                country_code VARCHAR(4)
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO airports (iata_code, icao_code, airport_name, city_code, country_code) "
            "VALUES ('IST', 'LTFM', 'Istanbul Airport', 'IST', 'TR')"
        )

    monkeypatch.setattr(db_module, "engine", isolated_engine)
    db_module._migrate_sqlite_table("airports", db_module._SQLITE_AIRPORTS_COLUMNS)
    db_module._migrate_sqlite_table("airports", db_module._SQLITE_AIRPORTS_COLUMNS)  # idempotent

    columns = {
        column["name"] for column in inspect(isolated_engine).get_columns("airports")
    }
    assert "timezone" in columns

    with isolated_engine.connect() as connection:
        row = connection.execute(text(
            "SELECT iata_code, icao_code, airport_name, city_code, country_code, timezone "
            "FROM airports WHERE iata_code='IST'"
        )).one()
    # Mevcut satır verisi (iata/icao/name/city/country) KORUNUR, yeni
    # `timezone` kolonu nullable olduğu için NULL alır - uydurma bir
    # varsayılan zaman dilimi YAZILMAZ.
    assert tuple(row) == ("IST", "LTFM", "Istanbul Airport", "IST", "TR", None)


def test_existing_sqlite_airports_table_gets_missing_scale_column(
    tmp_path, monkeypatch
):
    """
    ADIM (Airport-Scale Queue Capacity): `Airport.scale` bu kolonun
    modele eklenmesinden ÖNCE oluşturulmuş bir `airports` tablosuna
    (timezone ile AYNI additive-migration deseni) sonradan, veri
    kaybı olmadan eklenmeli.
    """
    database_path = tmp_path / "legacy-airports-scale.sqlite"
    isolated_engine = create_engine(f"sqlite:///{database_path}")
    with isolated_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE airports (
                iata_code VARCHAR(10) PRIMARY KEY,
                icao_code VARCHAR(10),
                airport_name VARCHAR(500),
                city_code VARCHAR(10),
                country_code VARCHAR(4),
                timezone VARCHAR(64)
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO airports (iata_code, icao_code, airport_name, city_code, country_code, timezone) "
            "VALUES ('IST', 'LTFM', 'Istanbul Airport', 'IST', 'TR', 'Europe/Istanbul')"
        )

    monkeypatch.setattr(db_module, "engine", isolated_engine)
    db_module._migrate_sqlite_table("airports", db_module._SQLITE_AIRPORTS_COLUMNS)
    db_module._migrate_sqlite_table("airports", db_module._SQLITE_AIRPORTS_COLUMNS)  # idempotent

    columns = {
        column["name"] for column in inspect(isolated_engine).get_columns("airports")
    }
    assert "scale" in columns

    with isolated_engine.connect() as connection:
        row = connection.execute(text(
            "SELECT iata_code, timezone, scale FROM airports WHERE iata_code='IST'"
        )).one()
    # Mevcut satır (iata/timezone) KORUNUR, yeni `scale` NULL alır -
    # uydurma bir ölçek ATANMAZ.
    assert tuple(row) == ("IST", "Europe/Istanbul", None)


def test_init_db_runs_both_additive_migrations(tmp_path, monkeypatch):
    """`init_db()` her iki tabloyu da (operational_config + airports) migrate eder."""
    database_path = tmp_path / "fresh-then-migrated.sqlite"
    isolated_engine = create_engine(f"sqlite:///{database_path}")
    with isolated_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE airports (iata_code VARCHAR(10) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE airport_operational_configs (airport_iata VARCHAR(10) PRIMARY KEY)"
        )

    monkeypatch.setattr(db_module, "engine", isolated_engine)
    db_module.init_db(drop_first=False)

    airport_columns = {
        column["name"] for column in inspect(isolated_engine).get_columns("airports")
    }
    config_columns = {
        column["name"]
        for column in inspect(isolated_engine).get_columns("airport_operational_configs")
    }
    assert "timezone" in airport_columns
    assert "scale" in airport_columns
    assert "domestic_security_lane_count" in config_columns
    assert "passport_departure_server_count" in config_columns
    assert "passport_arrival_server_count" in config_columns

