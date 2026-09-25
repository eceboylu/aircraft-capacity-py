"""
Madde 7.4/20.8 - scale/config refresh idempotent olmalı: aynı komut
iki kez çalıştırılınca değerler değişmemeli/duplicate oluşmamalı.
"""
import os

from app.queue.ingestion.airports_import import refresh_airport_scales
from app.queue.models import Airport, AirportOperationalConfig
from app.queue.pipeline import (
    DATA_DIR,
    LARGE_SCALE_TXT,
    MEDIUM_SCALE_TXT,
    MEGA_SCALE_TXT,
    SMALL_SCALE_TXT,
    ensure_airport_operational_configs,
)


def _real_paths():
    return (
        os.path.join(DATA_DIR, MEGA_SCALE_TXT),
        os.path.join(DATA_DIR, LARGE_SCALE_TXT),
        os.path.join(DATA_DIR, MEDIUM_SCALE_TXT),
        os.path.join(DATA_DIR, SMALL_SCALE_TXT),
    )


def test_refresh_and_config_resync_are_idempotent_across_two_runs(db_session):
    session = db_session
    session.add_all([
        Airport(iata_code="IST", icao_code="LTFM"),
        Airport(iata_code="ESB", icao_code="LTAC"),
        Airport(iata_code="ADB", icao_code="LTBJ"),
        Airport(iata_code="ASR", icao_code="LTAF"),
    ])
    session.commit()

    mega_path, large_path, medium_path, small_path = _real_paths()

    first_scale = refresh_airport_scales(session, mega_path, large_path, medium_path, small_path)
    first_config = ensure_airport_operational_configs(session)

    assert first_scale["updated"] >= 1
    assert first_config["created"] >= 1

    scales_after_first_run = {
        row.iata_code: row.scale for row in session.query(Airport).all()
    }
    config_rows_after_first_run = {
        row.airport_iata: (
            row.domestic_security_lane_count,
            row.international_security_lane_count,
        )
        for row in session.query(AirportOperationalConfig).all()
    }

    # İKİNCİ çalıştırma - hiçbir şey değişmemeli, hiçbir yeni satır
    # oluşmamalı.
    second_scale = refresh_airport_scales(session, mega_path, large_path, medium_path, small_path)
    second_config = ensure_airport_operational_configs(session)

    assert second_scale["updated"] == 0
    assert second_config["created"] == 0
    assert second_config["resynced"] == 0

    scales_after_second_run = {
        row.iata_code: row.scale for row in session.query(Airport).all()
    }
    config_rows_after_second_run = {
        row.airport_iata: (
            row.domestic_security_lane_count,
            row.international_security_lane_count,
        )
        for row in session.query(AirportOperationalConfig).all()
    }

    assert scales_after_first_run == scales_after_second_run
    assert config_rows_after_first_run == config_rows_after_second_run

    # Duplicate satır yok - her airport/iata TEK satır (primary key
    # zaten garanti eder, ama toplam sayının da değişmediğini kilitler).
    assert session.query(Airport).count() == 4
    assert session.query(AirportOperationalConfig).count() == len(
        [s for s in scales_after_second_run.values() if s is not None]
    )
