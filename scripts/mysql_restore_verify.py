#!/usr/bin/env python3
"""Restore a gzip dump into a new MySQL DB and verify it against the source."""

from __future__ import annotations

import argparse
import gzip
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from _mysql_backup_common import mysql_defaults_file, mysql_url_from_env


def quote_identifier(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def create_empty_database(url: URL) -> None:
    database = url.database or ""
    server_url = URL.create(
        drivername=url.drivername,
        username=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        query=url.query,
    )
    engine = create_engine(server_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            exists = connection.execute(
                text(
                    "SELECT 1 FROM information_schema.schemata "
                    "WHERE schema_name = :database"
                ),
                {"database": database},
            ).first()
            if exists:
                raise RuntimeError(
                    "restore target database already exists; refusing to overwrite it"
                )
            connection.exec_driver_sql(
                f"CREATE DATABASE {quote_identifier(database)} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        engine.dispose()


def restore_dump(backup: Path, target_url: URL) -> None:
    mysql = os.environ.get("MYSQL_BIN", "mysql")
    with mysql_defaults_file(target_url) as defaults_file:
        command = [
            mysql,
            f"--defaults-extra-file={defaults_file}",
            "--binary-mode",
            target_url.database or "",
        ]
        with gzip.open(backup, "rb") as source, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=stderr_file)
            assert process.stdin is not None
            try:
                while chunk := source.read(1024 * 1024):
                    process.stdin.write(chunk)
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
            return_code = process.wait()
            stderr_file.seek(0)
            error_text = stderr_file.read().decode("utf-8", errors="replace").strip()
        if return_code != 0:
            raise RuntimeError(error_text or f"mysql exited with {return_code}")


def table_counts(url: URL) -> dict[str, int]:
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            try:
                tables = connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :database AND table_type = 'BASE TABLE' "
                        "ORDER BY table_name"
                    ),
                    {"database": url.database},
                ).scalars().all()
                return {
                    table: int(
                        connection.exec_driver_sql(
                            f"SELECT COUNT(*) FROM {quote_identifier(table)}"
                        ).scalar_one()
                    )
                    for table in tables
                }
            finally:
                connection.rollback()
    finally:
        engine.dispose()


def alembic_versions(url: URL) -> tuple[str, ...]:
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            return tuple(
                connection.execute(
                    text("SELECT version_num FROM alembic_version ORDER BY version_num")
                ).scalars()
            )
    finally:
        engine.dispose()


def app_basic_query(url: URL) -> None:
    # Importing the model (rather than issuing only SELECT 1) verifies that the
    # restored schema supports a real application ORM query.
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    from app.models import AircraftCapacity

    engine = create_engine(url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            session.execute(select(AircraftCapacity).limit(1)).all()
    finally:
        engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Restore a .sql.gz backup into the non-existing database named by "
            "MYSQL_RESTORE_URL and compare it with DATABASE_URL."
        )
    )
    parser.add_argument("backup", type=Path, help="path to a .sql.gz backup")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        backup = args.backup.expanduser().resolve(strict=True)
        source_url = mysql_url_from_env("DATABASE_URL")
        target_url = mysql_url_from_env("MYSQL_RESTORE_URL")
        if source_url.database == target_url.database and (
            source_url.host or "localhost",
            source_url.port or 3306,
        ) == (
            target_url.host or "localhost",
            target_url.port or 3306,
        ):
            raise ValueError("MYSQL_RESTORE_URL must identify a different database")
        if not backup.name.endswith(".sql.gz"):
            raise ValueError("backup must have a .sql.gz extension")

        create_empty_database(target_url)
        restore_dump(backup, target_url)

        source_counts = table_counts(source_url)
        restored_counts = table_counts(target_url)
        counts_match = source_counts == restored_counts

        alembic_error: str | None = None
        try:
            source_versions = alembic_versions(source_url)
            restored_versions = alembic_versions(target_url)
            versions_match = source_versions == restored_versions and bool(source_versions)
        except Exception as exc:
            versions_match = False
            alembic_error = type(exc).__name__

        app_query_ok = True
        app_query_error: str | None = None
        try:
            app_basic_query(target_url)
        except Exception as exc:
            app_query_ok = False
            app_query_error = type(exc).__name__

        print(f"RESTORE DATABASE: {target_url.database}")
        print(f"TABLE COUNT: {len(restored_counts)}")
        print(f"ROW COUNT MATCH: {'PASS' if counts_match else 'FAIL'}")
        print(f"ALEMBIC VERSION MATCH: {'PASS' if versions_match else 'FAIL'}")
        print(f"APP BASIC QUERY: {'PASS' if app_query_ok else 'FAIL'}")
        if alembic_error:
            print(f"ALEMBIC VERSION ERROR: {alembic_error}")
        if app_query_error:
            print(f"APP BASIC QUERY ERROR: {app_query_error}")
        if not counts_match:
            all_tables = sorted(source_counts.keys() | restored_counts.keys())
            for table in all_tables:
                if source_counts.get(table) != restored_counts.get(table):
                    print(
                        f"ROW COUNT DIFFERENCE: {table}: "
                        f"source={source_counts.get(table)} restored={restored_counts.get(table)}"
                    )
        return 0 if counts_match and versions_match and app_query_ok else 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
