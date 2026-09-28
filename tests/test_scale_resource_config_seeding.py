"""
`airport_scale_configs` tablosu artık `ensure_airport_scale_resource_
config()` ile MEGA/LARGE/MEDIUM/SMALL için otomatik seed ediliyor -
kullanıcı talebi: "large/medium/mega gibi belirlediklerimizi SQL'den
görmek ve istediğim zaman oradan değiştirmek istiyorum".

Precedence (config.py:_build_config_view ile AYNI, DEĞİŞMEDİ): satır
BİR KEZ oluştuktan sonra HER ZAMAN Python sabitinin (`SCALE_RESOURCES`)
ÖNÜNE geçer - bu fonksiyon SADECE satır YOKSA seed eder, VARSA
(kullanıcı SQL'den değiştirmiş olsa bile) ASLA üzerine YAZMAZ.
"""
from app.queue.config import get_config
from app.queue.domain.airport_scale import SCALE_RESOURCES, SCALES
from app.queue.models import Airport, AirportScaleConfig
from app.queue.pipeline import ensure_airport_scale_resource_config


def test_seeds_all_four_scales_from_scale_resources(db_session):
    session = db_session
    created = ensure_airport_scale_resource_config(session)
    assert created == 4

    rows = {row.scale: row for row in session.query(AirportScaleConfig).all()}
    assert set(rows) == set(SCALES)
    for scale, resources in SCALE_RESOURCES.items():
        row = rows[scale]
        assert row.departure_passport_servers == resources["departure_passport_servers"]
        assert row.arrival_passport_servers == resources["arrival_passport_servers"]
        assert row.domestic_security_lanes == resources["domestic_security_lanes"]
        assert row.international_security_lanes == resources["international_security_lanes"]
        assert row.departure_passport_servers_max == resources.get("departure_passport_servers_max")
        assert row.arrival_passport_servers_max == resources.get("arrival_passport_servers_max")


def test_is_idempotent_and_never_overwrites_existing_row(db_session):
    session = db_session
    ensure_airport_scale_resource_config(session)

    # Kullanıcı SQL'den LARGE'ı manuel değiştirmiş gibi simüle et.
    large_row = session.get(AirportScaleConfig, "large")
    large_row.international_security_lanes = 999
    session.commit()

    second_run_created = ensure_airport_scale_resource_config(session)
    assert second_run_created == 0

    session.expire_all()
    assert session.get(AirportScaleConfig, "large").international_security_lanes == 999


def test_db_edit_immediately_takes_effect_via_get_config(db_session):
    """phpMyAdmin'den bir değeri değiştirmek, sonraki `get_config()` çağrısına ANINDA yansımalı."""
    session = db_session
    ensure_airport_scale_resource_config(session)
    session.add(Airport(iata_code="ZZZ", icao_code="ZZZZ", scale="large"))
    session.commit()

    before = get_config(session, "ZZZ")
    assert before.international_security_lane_count == 15

    row = session.get(AirportScaleConfig, "large")
    row.international_security_lanes = 42
    session.commit()

    after = get_config(session, "ZZZ")
    assert after.international_security_lane_count == 42
