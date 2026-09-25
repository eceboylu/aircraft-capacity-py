"""
Madde 6/20.7 - LARGE/MEDIUM/SMALL scale-derived passport kaynakları
için dynamic staffing KULLANILMAMALI: effective server count HER ZAMAN
config'ten çözülen statik sayı olmalı, max'e ramp OLMAMALI.

MEGA artık dynamic (bkz. `tests/test_dynamic_staffing.py` - bu dosya
SADECE static kalması gereken 3 tier'ı kapsar, MEGA'nın dynamic
davranışı kasıtlı olarak burada test EDİLMEZ).
"""
import pytest

from app.queue.config import default_config

STATIC_SCALES = ("large", "medium", "small")

EXPECTED = {
    "mega": {"departure": 30, "arrival": 35},
    "large": {"departure": 10, "arrival": 12},
    "medium": {"departure": 4, "arrival": 4},
    "small": {"departure": 2, "arrival": 2},
}


@pytest.mark.parametrize("scale", STATIC_SCALES)
def test_no_dynamic_staffing_flags_for_static_scales(scale):
    cfg = default_config("XXX", scale=scale)
    assert cfg.passport_departure_dynamic is False
    assert cfg.passport_arrival_dynamic is False
    assert cfg.passport_departure_server_count_max is None
    assert cfg.passport_arrival_server_count_max is None


@pytest.mark.parametrize("scale", ("mega", *STATIC_SCALES))
def test_effective_server_count_default_matches_contract(scale):
    """
    `passport_departure_server_count`/`passport_arrival_server_count`
    HER ZAMAN scale'in TABAN/DEFAULT sayısını taşır - MEGA için bile
    (dynamic ramp SADECE bir canlı simülasyon SIRASINDA, `simulate_
    fifo_queue_dynamic()`'in kendi iç durumunda olur; bu alan config
    seviyesinde HER ZAMAN sabit taban değeridir, ramp'in KENDİSİ
    değildir).
    """
    cfg = default_config("XXX", scale=scale)
    assert cfg.passport_departure_server_count == EXPECTED[scale]["departure"]
    assert cfg.passport_arrival_server_count == EXPECTED[scale]["arrival"]


@pytest.mark.parametrize("scale", STATIC_SCALES)
def test_static_scales_never_activate_dynamic_staffing_via_engine(scale):
    from app.queue.engine import _dynamic_staffing_params_for

    cfg = default_config("XXX", scale=scale)
    assert _dynamic_staffing_params_for(cfg, "departure") is None
    assert _dynamic_staffing_params_for(cfg, "arrival") is None
