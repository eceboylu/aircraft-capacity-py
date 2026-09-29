"""
ADIM (Editable Dynamic Staffing Config) - kullanıcı talebi: phpMyAdmin'den
`airport_scale_configs` üzerinden dynamic staffing'in checkpoint aralığı,
base/max, hedef doluluk oranı ve passport_arr'ın proaktif lookahead
penceresi GERÇEKTEN değiştirilebilsin - "SQL runtime source-of-truth
olsun, seed SADECE eksik değerler için çalışsın" (Bölüm 18).
"""
from app.queue.config import get_config
from app.queue.engine import _dynamic_staffing_params_for, _operational_levels_for
from app.queue.models import Airport, AirportScaleConfig


def _mega_row(session, **overrides):
    row = AirportScaleConfig(
        scale="mega",
        departure_passport_servers=20,
        departure_passport_servers_max=40,
        arrival_passport_servers=30,
        arrival_passport_servers_max=40,
        domestic_security_lanes=30,
        international_security_lanes=20,
        international_security_lanes_max=40,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    session.add(Airport(iata_code="IST", icao_code="LTFM", scale="mega"))
    session.add(row)
    session.commit()


def test_db_base_20_is_read_through_get_config(db_session):
    session = db_session
    _mega_row(session)
    cfg = get_config(session, "IST")
    assert cfg.passport_departure_server_count == 20
    assert cfg.international_security_lane_count == 20


def test_db_interval_10_reflected_in_runtime_params(db_session):
    session = db_session
    _mega_row(session, security_dynamic_control_interval_minutes=10)
    cfg = get_config(session, "IST")
    params = _dynamic_staffing_params_for(cfg, "security_intl")
    assert params.control_interval_minutes == 10
    assert params.look_ahead_minutes == 10


def test_db_interval_5_reflected_in_runtime_params(db_session):
    session = db_session
    _mega_row(session, security_dynamic_control_interval_minutes=5)
    cfg = get_config(session, "IST")
    params = _dynamic_staffing_params_for(cfg, "security_intl")
    assert params.control_interval_minutes == 5


def test_db_passport_departure_interval_independent_of_arrival(db_session):
    """Bölüm 15 - passport_dep/passport_arr AYRI kolonlardan okunmalı."""
    session = db_session
    _mega_row(
        session,
        passport_departure_control_interval_minutes=10,
        passport_arrival_control_interval_minutes=5,
    )
    cfg = get_config(session, "IST")
    dep_params = _dynamic_staffing_params_for(cfg, "departure")
    arr_params = _dynamic_staffing_params_for(cfg, "arrival")
    assert dep_params.control_interval_minutes == 10
    assert arr_params.control_interval_minutes == 5


def test_db_max_30_makes_level_40_unreachable(db_session):
    """Bölüm 16 - DB max=30 -> 20/30 çalışsın, 40'a asla çıkmasın
    (step=10 ile 30, 20'den ULAŞILABİLİR TEK bir ara seviye)."""
    session = db_session
    _mega_row(session, international_security_lanes_max=30)
    cfg = get_config(session, "IST")
    params = _dynamic_staffing_params_for(cfg, "security_intl")
    assert params.max_server_count == 30
    assert params.allowed_levels == (20, 30)
    assert 40 not in params.allowed_levels


def test_db_passport_dep_max_30_keeps_only_two_levels(db_session):
    session = db_session
    _mega_row(session, departure_passport_servers_max=30)
    cfg = get_config(session, "IST")
    params = _dynamic_staffing_params_for(cfg, "departure")
    assert params.allowed_levels == (20, 30)


def test_manual_db_values_not_overwritten_by_seed(db_session):
    """Bölüm 18 - satır zaten VARSA `ensure_airport_scale_resource_config()` ASLA üzerine yazmaz."""
    from app.queue.pipeline import ensure_airport_scale_resource_config

    session = db_session
    _mega_row(session, security_dynamic_control_interval_minutes=10)

    created = ensure_airport_scale_resource_config(session)
    assert created == 3  # large/medium/small YENİ eklendi; mega zaten vardı, dokunulmadı

    row = session.get(AirportScaleConfig, "mega")
    assert row.security_dynamic_control_interval_minutes == 10  # DEĞİŞMEDİ


def test_invalid_interval_zero_falls_back_to_default(db_session):
    """Bölüm 23 - interval=0 geçersiz, sabit DEFAULT'a düşülmeli (crash yok)."""
    session = db_session
    _mega_row(session, security_dynamic_control_interval_minutes=0)
    cfg = get_config(session, "IST")
    params = _dynamic_staffing_params_for(cfg, "security_intl")
    assert params.control_interval_minutes == 5


def test_invalid_target_utilization_out_of_range_falls_back(db_session):
    session = db_session
    _mega_row(session, dynamic_target_utilization=0.0)
    cfg = get_config(session, "IST")
    assert cfg.dynamic_target_utilization == 0.85

    session2_row = session.get(AirportScaleConfig, "mega")
    session2_row.dynamic_target_utilization = 1.5
    session.commit()
    cfg2 = get_config(session, "IST")
    assert cfg2.dynamic_target_utilization == 0.85


def test_max_less_than_base_falls_back_to_fallback_levels():
    """Bölüm 23 - max < base geçersiz durum, `_operational_levels_for` fallback listeye düşmeli."""
    fallback = (20, 25, 30, 35, 40)
    assert _operational_levels_for(default_count=20, maximum=15, fallback_levels=fallback) == fallback


def test_step_invalid_max_not_multiple_of_ten_falls_back():
    """Bölüm 23 - (max-base) 10'un katı DEĞİLSE (ör. max=37) fallback listeye düşülmeli."""
    fallback = (20, 25, 30, 35, 40)
    assert _operational_levels_for(default_count=20, maximum=37, fallback_levels=fallback) == fallback


def test_operational_levels_derivation_matches_base_max_step_formula():
    """Bölüm 17 - geçerli base/max için levels = range(base, max+1, 10)."""
    assert _operational_levels_for(20, 40, ()) == (20, 30, 40)
    assert _operational_levels_for(30, 40, ()) == (30, 40)
    assert _operational_levels_for(20, 30, ()) == (20, 30)


def test_large_scale_static_values_unaffected_by_new_config(db_session):
    """Bölüm 8 - LARGE static security_dom=15/security_intl=15 hiç değişmemeli."""
    session = db_session
    session.add(Airport(iata_code="ZRH", icao_code="LSZH", scale="large"))
    session.commit()
    cfg = get_config(session, "ZRH")
    assert cfg.domestic_security_lane_count == 15
    assert cfg.international_security_lane_count == 15
    assert cfg.security_intl_dynamic is False
    assert _dynamic_staffing_params_for(cfg, "security_intl") is None
