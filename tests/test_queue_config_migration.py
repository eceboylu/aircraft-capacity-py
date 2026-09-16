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
    db_module._migrate_sqlite_operational_config()
    db_module._migrate_sqlite_operational_config()  # idempotent

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
    }.issubset(columns)

    with isolated_engine.connect() as connection:
        row = connection.execute(text(
            "SELECT airport_iata, passport_counter_count, passport_staff_count, "
            "passport_service_time_minutes, security_lane_count, "
            "security_service_time_minutes "
            "FROM airport_operational_configs WHERE airport_iata='IST'"
        )).one()
    assert tuple(row) == ("IST", 4, 8, 1.5, 8, 1.0)

