"""
ADIM (DB-Editable Scale Resource Contract) - `airport_scale_configs`
tablosu artık `config.py:get_config()`/`get_configs()` için CANLI bir
override kaynağı - kullanıcı talebi: "phpMyAdmin'den bir değeri
değiştirince gerçekten sistemi etkilesin".

Öncelik: (1) DB'de o `scale` için satır VARSA onu kullan, (2) YOKSA
`domain/airport_scale.py:SCALE_RESOURCES` (Python sabiti) GÜVENLİ
YEDEK olarak kalır - "hepsi ya da hiçbiri" DEĞİL.
"""
from app.queue.config import default_config, get_config, get_configs
from app.queue.models import Airport, AirportScaleConfig


def test_db_row_overrides_hardcoded_scale_resources(db_session):
    session = db_session
    session.add(Airport(iata_code="ESB", icao_code="LTAC", scale="large"))
    session.add(AirportScaleConfig(
        scale="large",
        departure_passport_servers=777,
        departure_passport_servers_max=None,
        arrival_passport_servers=888,
        arrival_passport_servers_max=None,
        domestic_security_lanes=111,
        international_security_lanes=222,
    ))
    session.commit()

    cfg = get_config(session, "ESB")
    assert cfg.passport_departure_server_count == 777
    assert cfg.passport_arrival_server_count == 888
    assert cfg.domestic_security_lane_count == 111
    assert cfg.international_security_lane_count == 222


def test_db_row_can_activate_dynamic_staffing_for_any_scale(db_session):
    """
    DB satırı bir `_max` değeri taşıyorsa, o scale (MEGA olmasa bile)
    dynamic staffing'e açılabilmeli - kontrol scale-agnostik.
    """
    session = db_session
    session.add(Airport(iata_code="ESB", icao_code="LTAC", scale="large"))
    session.add(AirportScaleConfig(
        scale="large",
        departure_passport_servers=20,
        departure_passport_servers_max=40,
        arrival_passport_servers=20,
        arrival_passport_servers_max=40,
        domestic_security_lanes=8,
        international_security_lanes=6,
    ))
    session.commit()

    cfg = get_config(session, "ESB")
    assert cfg.passport_departure_dynamic is True
    assert cfg.passport_departure_server_count_max == 40
    assert cfg.passport_arrival_dynamic is True
    assert cfg.passport_arrival_server_count_max == 40


def test_missing_db_row_falls_back_to_hardcoded_scale_resources(db_session):
    """Tabloda hiçbir scale için satır yoksa (boş tablo) - SCALE_RESOURCES AYNEN kullanılmalı."""
    session = db_session
    session.add(Airport(iata_code="ADB", icao_code="LTBJ", scale="medium"))
    session.commit()
    # AirportScaleConfig tablosu KASITLI olarak BOŞ bırakıldı.

    cfg = get_config(session, "ADB")
    assert cfg.passport_departure_server_count == 4
    assert cfg.passport_arrival_server_count == 4
    assert cfg.domestic_security_lane_count == 3
    assert cfg.international_security_lane_count == 2


def test_partial_db_table_only_overrides_the_scale_that_has_a_row(db_session):
    """Tabloda SADECE mega için satır varsa, medium hâlâ hardcoded değerini kullanmalı."""
    session = db_session
    session.add_all([
        Airport(iata_code="IST", icao_code="LTFM", scale="mega"),
        Airport(iata_code="ADB", icao_code="LTBJ", scale="medium"),
    ])
    session.add(AirportScaleConfig(
        scale="mega",
        departure_passport_servers=999,
        departure_passport_servers_max=999,
        arrival_passport_servers=999,
        arrival_passport_servers_max=999,
        domestic_security_lanes=999,
        international_security_lanes=999,
    ))
    session.commit()

    ist_cfg = get_config(session, "IST")
    assert ist_cfg.passport_departure_server_count == 999

    adb_cfg = get_config(session, "ADB")
    assert adb_cfg.passport_departure_server_count == 4  # hardcoded MEDIUM, DB'den ETKİLENMEDİ


def test_get_configs_batch_uses_db_override_without_n_plus_1(db_session):
    """`get_configs()` (çoklu) de aynı DB override'ı kullanmalı, TEK sorguyla."""
    session = db_session
    session.add_all([
        Airport(iata_code="ESB", icao_code="LTAC", scale="large"),
        Airport(iata_code="ADB", icao_code="LTBJ", scale="large"),
    ])
    session.add(AirportScaleConfig(
        scale="large",
        departure_passport_servers=55,
        departure_passport_servers_max=None,
        arrival_passport_servers=66,
        arrival_passport_servers_max=None,
        domestic_security_lanes=7,
        international_security_lanes=9,
    ))
    session.commit()

    configs = get_configs(session, ["ESB", "ADB"])
    assert configs["ESB"].passport_departure_server_count == 55
    assert configs["ADB"].passport_departure_server_count == 55
    assert configs["ESB"].international_security_lane_count == 9


def test_default_config_without_session_never_touches_db_table():
    """
    `default_config()` (session'sız, pure/test-edilebilir yol) HER ZAMAN
    hardcoded `SCALE_RESOURCES`'ı kullanmalı - DB'ye hiç bakmaz (zaten
    session'ı YOK).
    """
    cfg = default_config("XXX", scale="large")
    assert cfg.passport_departure_server_count == 10
    assert cfg.domestic_security_lane_count == 8
