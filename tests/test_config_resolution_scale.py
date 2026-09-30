"""
Madde 10/20.5 - scale-derived effective config, YENİ 4-tier contract'ın
exact değerlerini vermeli.
"""
from app.queue.config import default_config, get_config
from app.queue.models import Airport


def test_default_config_mega_scale_exact_values():
    """MEGA dynamic resource policy (source of truth): passport_dep VE
    passport_arr ikisi de base=30/max=60 (kapasite/talep uyumsuzluğu
    tespiti sonrası 45/40'tan yükseltildi - kullanıcının SQL'den yaptığı
    güncelleme + Python varsayılanının senkronizasyonu), security_intl
    dynamic base=20/max=40 (domestic security static kalıyor, 30'dan
    20'ye düşürüldü - kullanıcı talebi)."""
    cfg = default_config("IST", scale="mega")
    assert cfg.domestic_security_lane_count == 20
    assert cfg.passport_departure_server_count == 30
    assert cfg.passport_departure_server_count_max == 60
    assert cfg.international_security_lane_count == 20
    assert cfg.international_security_lane_count_max == 40
    assert cfg.security_intl_dynamic is True
    assert cfg.passport_arrival_server_count == 30
    assert cfg.passport_arrival_server_count_max == 60
    assert cfg.scale == "mega"


def test_default_config_large_scale_exact_values():
    cfg = default_config("ESB", scale="large")
    assert cfg.domestic_security_lane_count == 15
    assert cfg.passport_departure_server_count == 15
    assert cfg.international_security_lane_count == 15
    assert cfg.passport_arrival_server_count == 16


def test_default_config_medium_scale_exact_values():
    cfg = default_config("ADB", scale="medium")
    assert cfg.domestic_security_lane_count == 6
    assert cfg.passport_departure_server_count == 8
    assert cfg.international_security_lane_count == 5
    assert cfg.passport_arrival_server_count == 8


def test_default_config_small_scale_exact_values():
    cfg = default_config("ASR", scale="small")
    assert cfg.domestic_security_lane_count == 3
    assert cfg.passport_departure_server_count == 4
    assert cfg.international_security_lane_count == 3
    assert cfg.passport_arrival_server_count == 4


def test_get_config_reads_mega_scale_from_db(db_session):
    """
    Madde 11/17-F - DB'den okunan gerçek bir MEGA airport, dynamic
    staffing dahil TAM contract'ı vermeli (sadece statik sayılar değil).
    """
    session = db_session
    session.add(Airport(iata_code="IST", icao_code="LTFM", scale="mega"))
    session.commit()

    cfg = get_config(session, "IST")
    assert cfg.domestic_security_lane_count == 20
    assert cfg.passport_departure_server_count == 30
    assert cfg.passport_departure_server_count_max == 60
    assert cfg.passport_departure_dynamic is True
    assert cfg.international_security_lane_count == 20
    assert cfg.international_security_lane_count_max == 40
    assert cfg.security_intl_dynamic is True
    assert cfg.passport_arrival_server_count == 30
    assert cfg.passport_arrival_server_count_max == 60
    assert cfg.passport_arrival_dynamic is True
    assert cfg.is_default is True


def test_get_config_reads_large_medium_small_from_db_as_fully_static(db_session):
    """Madde 17-G - LARGE/MEDIUM/SMALL DB'den okununca da HİÇ dynamic olmamalı."""
    session = db_session
    session.add_all([
        Airport(iata_code="ESB", icao_code="LTAC", scale="large"),
        Airport(iata_code="ADB", icao_code="LTBJ", scale="medium"),
        Airport(iata_code="ASR", icao_code="LTAU", scale="small"),
    ])
    session.commit()

    expected = {
        "ESB": (15, 16, 15, 15),
        "ADB": (8, 8, 6, 5),
        "ASR": (4, 4, 3, 3),
    }
    for iata, (dep, arr, dom, intl) in expected.items():
        cfg = get_config(session, iata)
        assert cfg.passport_departure_server_count == dep
        assert cfg.passport_arrival_server_count == arr
        assert cfg.domestic_security_lane_count == dom
        assert cfg.international_security_lane_count == intl
        assert cfg.passport_departure_server_count_max is None
        assert cfg.passport_arrival_server_count_max is None
        assert cfg.passport_departure_dynamic is False
        assert cfg.passport_arrival_dynamic is False
