"""
Madde 7.2/20.6 - explicit/manual `AirportOperationalConfig` override,
scale default'unu (yeni 4-tier contract dahil) ezmeye devam etmeli;
`ensure_airport_operational_configs()` bu satırlara HİÇ dokunmamalı.
"""
from app.queue.config import get_config
from app.queue.models import Airport, AirportOperationalConfig
from app.queue.pipeline import ensure_airport_operational_configs


def test_explicit_override_beats_mega_scale_default(db_session):
    session = db_session
    session.add(Airport(iata_code="IST", icao_code="LTFM", scale="mega"))
    session.add(AirportOperationalConfig(
        airport_iata="IST",
        domestic_security_lane_count=999,
        international_security_lane_count=999,
        passport_departure_server_count=777,
        passport_arrival_server_count=888,
        is_seeded_default=False,
    ))
    session.commit()

    cfg = get_config(session, "IST")
    assert cfg.domestic_security_lane_count == 999
    assert cfg.international_security_lane_count == 999
    assert cfg.passport_departure_server_count == 777
    assert cfg.passport_arrival_server_count == 888
    assert cfg.is_default is False


def test_ensure_operational_configs_never_touches_manual_override(db_session):
    session = db_session
    session.add(Airport(iata_code="SAW", icao_code="LTFJ", scale="mega"))
    session.add(AirportOperationalConfig(
        airport_iata="SAW",
        domestic_security_lane_count=1,
        international_security_lane_count=1,
        passport_departure_server_count=1,
        passport_arrival_server_count=1,
        is_seeded_default=False,
    ))
    session.commit()

    result = ensure_airport_operational_configs(session)

    row = session.get(AirportOperationalConfig, "SAW")
    assert row.domestic_security_lane_count == 1
    assert row.international_security_lane_count == 1
    assert result["created"] == 0
    assert result["resynced"] == 0


def test_seeded_default_row_is_resynced_to_new_scale_contract(db_session):
    """
    Bir `is_seeded_default=True` satır ESKİ (3-tier) sayılarla DB'de
    duruyorsa, `ensure_airport_operational_configs()` onu YENİ contract'a
    resync etmeli - manuel override İSE bu davranıştan MUAF.
    """
    session = db_session
    session.add(Airport(iata_code="IST", icao_code="LTFM", scale="mega"))
    session.add(AirportOperationalConfig(
        airport_iata="IST",
        domestic_security_lane_count=14,  # eski LARGE değeri
        international_security_lane_count=30,  # eski LARGE değeri
        passport_departure_server_count=None,
        passport_arrival_server_count=None,
        is_seeded_default=True,
    ))
    session.commit()

    result = ensure_airport_operational_configs(session)

    row = session.get(AirportOperationalConfig, "IST")
    assert row.domestic_security_lane_count == 30  # yeni MEGA değeri
    assert row.international_security_lane_count == 20  # yeni MEGA değeri
    assert result["resynced"] == 1
