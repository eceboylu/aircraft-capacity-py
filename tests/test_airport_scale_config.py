"""
PHASE 4 - `app/queue/config.py` scale-aware resolution.

Öncelik: (1) explicit AirportOperationalConfig override (alan-bazlı,
sadece passport dep/arr için) > (2) Airport.scale-derived > (3) unknown
scale -> eski sabit (8/8/8), sessizce farklı bir sayıya DÜŞMEZ.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.config import default_config, get_config, get_configs
from app.queue.models import Airport, AirportOperationalConfig


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


# ========================================================================
# Scale-derived (satır YOK) - large/medium/small.
# ========================================================================

def test_large_scale_no_row_config(session):
    session.add(Airport(iata_code="IST", scale="large"))
    session.commit()
    c = get_config(session, "IST")
    assert c.passport_departure_server_count == 20
    assert c.passport_arrival_server_count == 30
    assert c.domestic_security_lane_count == 22
    assert c.international_security_lane_count == 22
    assert c.is_default is True


def test_medium_scale_no_row_config(session):
    session.add(Airport(iata_code="CBR", scale="medium"))
    session.commit()
    c = get_config(session, "CBR")
    assert c.passport_departure_server_count == 6
    assert c.passport_arrival_server_count == 8
    assert c.domestic_security_lane_count == 7
    assert c.international_security_lane_count == 7


def test_small_scale_no_row_config(session):
    session.add(Airport(iata_code="AGJ", scale="small"))
    session.commit()
    c = get_config(session, "AGJ")
    assert c.passport_departure_server_count == 4
    assert c.passport_arrival_server_count == 4
    assert c.domestic_security_lane_count == 2
    assert c.international_security_lane_count == 2


# ========================================================================
# Unknown scale (null/tanınmayan) -> eski sabit 8, sessizce FARKLI
# bir sayıya DÜŞMEZ.
# ========================================================================

def test_unknown_scale_uses_legacy_default_not_a_new_silent_value(session):
    session.add(Airport(iata_code="ZZZ", scale=None))
    session.commit()
    c = get_config(session, "ZZZ")
    assert c.passport_departure_server_count == 8
    assert c.passport_arrival_server_count == 8
    assert c.domestic_security_lane_count == 8
    assert c.international_security_lane_count == 8


def test_airport_row_completely_missing_also_falls_back_to_legacy_default(session):
    """Airport tablosunda HİÇ satırı olmayan bir kod (ör. henüz import edilmemiş)."""
    c = get_config(session, "NOTEXIST")
    assert c.passport_departure_server_count == 8
    assert c.passport_arrival_server_count == 8


def test_default_config_without_scale_arg_keeps_old_behavior():
    """Eski çağıranlar (scale hiç vermeyen) - ESKİ sabit davranış korunur."""
    c = default_config("XXX")
    assert c.passport_departure_server_count == 8
    assert c.passport_arrival_server_count == 8
    assert c.scale is None


# ========================================================================
# Explicit override > scale (alan-bazlı, diğer alanı KAYBETMEZ).
# ========================================================================

def test_explicit_departure_override_does_not_lose_scale_derived_arrival(session):
    session.add(Airport(iata_code="IST", scale="large"))
    session.add(AirportOperationalConfig(
        airport_iata="IST", passport_departure_server_count=16,
        # arrival KASITLI olarak verilmedi -> NULL -> scale-derived (30) KALIR.
    ))
    session.commit()
    c = get_config(session, "IST")
    assert c.passport_departure_server_count == 16  # override
    assert c.passport_arrival_server_count == 30    # scale-derived, KAYBOLMADI
    assert c.is_default is False


def test_explicit_arrival_override_does_not_lose_scale_derived_departure(session):
    session.add(Airport(iata_code="CBR", scale="medium"))
    session.add(AirportOperationalConfig(
        airport_iata="CBR", passport_arrival_server_count=99,
    ))
    session.commit()
    c = get_config(session, "CBR")
    assert c.passport_departure_server_count == 6   # scale-derived, KAYBOLMADI
    assert c.passport_arrival_server_count == 99     # override


def test_row_level_security_lane_override_still_wins_when_row_exists(session):
    """Security lane'ler satır-seviyeli - mevcut davranış, DEĞİŞMEDİ."""
    session.add(Airport(iata_code="IST", scale="large"))
    session.add(AirportOperationalConfig(
        airport_iata="IST", domestic_security_lane_count=99,
        international_security_lane_count=77,
    ))
    session.commit()
    c = get_config(session, "IST")
    assert c.domestic_security_lane_count == 99
    assert c.international_security_lane_count == 77


def test_row_without_scale_row_present_but_airport_scale_is_none(session):
    """Satır VAR ama passport dep/arr override edilmedi VE Airport.scale de yok -> eski sabit 8."""
    session.add(Airport(iata_code="ZZZ", scale=None))
    session.add(AirportOperationalConfig(airport_iata="ZZZ"))
    session.commit()
    c = get_config(session, "ZZZ")
    assert c.passport_departure_server_count == 8
    assert c.passport_arrival_server_count == 8


# ========================================================================
# get_configs (toplu) - AYNI mantık, airport isolation.
# ========================================================================

def test_get_configs_resolves_each_airport_independently(session):
    session.add(Airport(iata_code="IST", scale="large"))
    session.add(Airport(iata_code="CBR", scale="medium"))
    session.add(Airport(iata_code="AGJ", scale="small"))
    session.commit()

    configs = get_configs(session, ["IST", "CBR", "AGJ", "NOTEXIST"])
    assert configs["IST"].passport_departure_server_count == 20
    assert configs["IST"].passport_arrival_server_count == 30
    assert configs["CBR"].passport_departure_server_count == 6
    assert configs["CBR"].passport_arrival_server_count == 8
    assert configs["AGJ"].passport_departure_server_count == 4
    assert configs["AGJ"].passport_arrival_server_count == 4
    assert configs["NOTEXIST"].passport_departure_server_count == 8
    # bir havalimanının değeri diğerini ETKİLEMEDİ.
    assert configs["IST"].domestic_security_lane_count == 22
    assert configs["CBR"].domestic_security_lane_count == 7
