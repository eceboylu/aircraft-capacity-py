"""
Madde 20.1 - 4-tier (mega/large/medium/small) exact resource contract.
"""
from app.queue.domain.airport_scale import (
    SCALE_LARGE,
    SCALE_MEDIUM,
    SCALE_MEGA,
    SCALE_RESOURCES,
    SCALE_SMALL,
    SCALES,
    resource_view_for_scale,
)


def test_scales_are_exactly_four_tiers_in_precedence_order():
    assert SCALES == (SCALE_MEGA, SCALE_LARGE, SCALE_MEDIUM, SCALE_SMALL)


def test_mega_resource_contract():
    resources = SCALE_RESOURCES[SCALE_MEGA]
    assert resources["domestic_security_lanes"] == 30
    assert resources["departure_passport_servers"] == 30
    assert resources["departure_passport_servers_max"] == 45
    assert resources["international_security_lanes"] == 20
    assert resources["arrival_passport_servers"] == 35
    assert resources["arrival_passport_servers_max"] == 45


def test_large_resource_contract():
    resources = SCALE_RESOURCES[SCALE_LARGE]
    assert resources["domestic_security_lanes"] == 8
    assert resources["departure_passport_servers"] == 10
    assert resources["international_security_lanes"] == 6
    assert resources["arrival_passport_servers"] == 12


def test_medium_resource_contract():
    resources = SCALE_RESOURCES[SCALE_MEDIUM]
    assert resources["domestic_security_lanes"] == 3
    assert resources["departure_passport_servers"] == 4
    assert resources["international_security_lanes"] == 2
    assert resources["arrival_passport_servers"] == 4


def test_small_resource_contract():
    resources = SCALE_RESOURCES[SCALE_SMALL]
    assert resources["domestic_security_lanes"] == 2
    assert resources["departure_passport_servers"] == 2
    assert resources["international_security_lanes"] == 2
    assert resources["arrival_passport_servers"] == 2


def test_only_mega_defines_max_dynamic_staffing_keys():
    """
    Madde 2/6 - dynamic staffing tavan anahtarları (`*_servers_max`)
    SADECE MEGA'da bulunmalı; LARGE/MEDIUM/SMALL TAMAMEN STATIC kalır
    (bu üçünde bu anahtarlar hiç YOK - `config.py`'nin dynamic bayrağı
    SADECE bu anahtarın varlığına bakar, bkz. o modülün docstring'i).
    """
    mega_resources = SCALE_RESOURCES[SCALE_MEGA]
    assert "departure_passport_servers_max" in mega_resources
    assert "arrival_passport_servers_max" in mega_resources

    for scale in (SCALE_LARGE, SCALE_MEDIUM, SCALE_SMALL):
        resources = SCALE_RESOURCES[scale]
        assert "departure_passport_servers_max" not in resources
        assert "arrival_passport_servers_max" not in resources


def test_resource_view_for_scale_matches_dict():
    for scale in SCALES:
        assert resource_view_for_scale(scale) == SCALE_RESOURCES[scale]
    assert resource_view_for_scale(None) is None
    assert resource_view_for_scale("unknown-tier") is None
