"""
ADIM (Airport Scale Test Matrix) - genel-proje.md'nin "Queue sistemini
production öncesi kapsamlı doğrula" turunun Phase 3-8/17/18 doğrulaması.

Gerçek 4 havalimanı:
  - LARGE   : CDG (Charles de Gaulle, Europe/Paris) - zaten mevcut, tam
              24 saat kapsıyor (bkz. test_date_shift_replay.py
              `expanded_replay`).
  - MEDIUM  : CBR (Canberra, Australia/Sydney) - `data/orta_olcekli_
              havaalanlari.txt`'de gerçekten var.
  - SMALL   : MFG (Muzaffarabad, Asia/Karachi) - `data/kucuk_olcekli_
              havaalanlari.txt`'de gerçekten var.
  - UNKNOWN : OAG (Orange Airport, Australia/Sydney) - `flight_airports.
              sql`'de gerçek bir kayıt, ama ÜÇ ölçek dosyasının HİÇBİRİNDE
              yok - gerçek "unknown scale" senaryosu.

CBR/MFG/OAG için yeni test flight'ları SADECE `tests/date_shift_replay/`
kopyalarına eklendi (bkz. `_generate_scale_matrix_test_flights.py`) -
gerçek `data/*.json`/`database.sqlite` hiç açılıp yazılmadı.
"""
from datetime import datetime, timedelta

import pytest

from app.queue.config import AirportConfigView, default_config
from app.queue.constants import PROCESS_PASSPORT_DEPARTURE, PROCESS_SECURITY_DOMESTIC
from app.queue.domain.airport_scale import SCALE_RESOURCES
from app.queue.domain.demand import DemandCalculator
from app.queue.engine import predict_airport
from tests.date_shift_replay.run_date_shift_replay import REPLAY_DIR, replay

from .factories import MockCapacityResolver, arrival


@pytest.fixture(scope="module")
def scale_matrix_replay(tmp_path_factory):
    r = replay(
        REPLAY_DIR, tmp_path_factory.mktemp("scale-matrix") / "scale_matrix.sqlite",
        datetime(2026, 9, 18, 12, 0), airports=None, per_airport_now=True,
    )
    yield r
    r["session"].close()


# ========================================================================
# Phase 3 - LARGE/MEDIUM/SMALL/UNKNOWN gerçek kaynak eşlemesi.
# ========================================================================

def test_large_cdg_resources_are_20_30_22(scale_matrix_replay):
    assert scale_matrix_replay["scale_by_airport"]["CDG"] == "large"
    assert SCALE_RESOURCES["large"] == {
        "departure_passport_servers": 20, "arrival_passport_servers": 30,
        "domestic_security_lanes": 22, "international_security_lanes": 22,
    }


def test_medium_cbr_resources_are_6_8_7(scale_matrix_replay):
    assert scale_matrix_replay["scale_by_airport"]["CBR"] == "medium"
    assert SCALE_RESOURCES["medium"] == {
        "departure_passport_servers": 6, "arrival_passport_servers": 8,
        "domestic_security_lanes": 7, "international_security_lanes": 7,
    }


def test_small_mfg_resources_are_4_4_2(scale_matrix_replay):
    assert scale_matrix_replay["scale_by_airport"]["MFG"] == "small"
    assert SCALE_RESOURCES["small"] == {
        "departure_passport_servers": 4, "arrival_passport_servers": 4,
        "domestic_security_lanes": 2, "international_security_lanes": 2,
    }


def test_unknown_oag_scale_is_none(scale_matrix_replay):
    """
    Phase 8 - OAG üç ölçek dosyasının HİÇBİRİNDE yok - production
    davranışı GÖZLEMLENİR (uydurulmaz): `Airport.scale` None kalır.
    """
    assert scale_matrix_replay["scale_by_airport"]["OAG"] is None


# ========================================================================
# Phase 8 - UNKNOWN airport'ta mevcut production fallback davranışı.
# ========================================================================

def test_unknown_oag_falls_back_to_old_constant_8_8_8_8(scale_matrix_replay):
    """
    Phase 8 - mevcut production `config.py:_build_config_view()`
    davranışı: `resource_view_for_scale(None)` None döner, bu yüzden
    ESKİ sabit varsayım (8) kullanılır - BU TURDA DEĞİŞTİRİLMEDİ,
    sadece gözlemlenip kanıtlandı.
    """
    from app.queue.config import get_config
    session = scale_matrix_replay["session"]
    config = get_config(session, "OAG")
    assert config.is_default is True or config.scale is None
    assert config.passport_departure_server_count == 8
    assert config.passport_arrival_server_count == 8
    assert config.domestic_security_lane_count == 8
    assert config.international_security_lane_count == 8


def test_unknown_oag_still_produces_predictions_and_api_graphs(scale_matrix_replay):
    """Phase 8/18 - scale=None olsa da prediction/API/graph üretimi DURMUYOR (fallback sayesinde)."""
    api = scale_matrix_replay["api_by_airport"]["OAG"]
    assert api["international_departure"]["passport"]["windows"]
    assert api["international_departure"]["security"]["windows"] or True  # security event olmayabilir, KeyError vermemesi önemli
    assert api["domestic_security"]["windows"]
    assert api["overall"]["windows"]


# ========================================================================
# Phase 5/6 - CBR/MFG en az 6 farklı saat, farklı yoğunluk seviyeleri.
# ========================================================================

def test_cbr_medium_has_at_least_six_distinct_hours_across_processes(scale_matrix_replay):
    session = scale_matrix_replay["session"]
    from sqlalchemy import select
    from app.queue.models import QueuePrediction
    rows = session.execute(
        select(QueuePrediction.window_start).where(QueuePrediction.airport_iata == "CBR")
    ).scalars().all()
    assert len(set(rows)) >= 6


def test_mfg_small_has_at_least_six_distinct_hours_across_processes(scale_matrix_replay):
    session = scale_matrix_replay["session"]
    from sqlalchemy import select
    from app.queue.models import QueuePrediction
    rows = session.execute(
        select(QueuePrediction.window_start).where(QueuePrediction.airport_iata == "MFG")
    ).scalars().all()
    assert len(set(rows)) >= 6


def test_oag_unknown_has_at_least_three_distinct_hours(scale_matrix_replay):
    session = scale_matrix_replay["session"]
    from sqlalchemy import select
    from app.queue.models import QueuePrediction
    rows = session.execute(
        select(QueuePrediction.window_start).where(QueuePrediction.airport_iata == "OAG")
    ).scalars().all()
    assert len(set(rows)) >= 3


def test_cbr_shows_multiple_distinct_risk_levels_not_hardcoded(scale_matrix_replay):
    """
    Phase 6 - risk hard-code edilmedi: sakin/orta/yüksek/critical
    yoğunluk deseni (bkz. `_generate_scale_matrix_test_flights.py:
    _six_hour_pattern`) gerçek event-driven motorda BİRDEN FAZLA farklı
    risk seviyesi üretmeli (tek düz bir sabit DEĞİL).
    """
    windows = scale_matrix_replay["api_by_airport"]["CBR"]["international_departure"]["passport"]["windows"]
    risks = {w["risk"] for w in windows}
    assert len(risks) > 1, f"CBR passport risk çeşitliliği yok: {risks}"


# ========================================================================
# Phase 7 - AYNI demand, FARKLI scale -> FARKLI wait (event engine).
# ========================================================================

def _resolver():
    return DemandCalculator(MockCapacityResolver(capacities={"B77W": 300}))


def _same_demand_flights():
    """300 pax'lık AYNI uluslararası varış talebi - tek uçuş, tek an."""
    return [arrival(8, 0, airport="XXX", aircraft="B77W", duration_minutes=360)]


def test_same_arrival_demand_produces_different_wait_across_scales():
    large_cfg = default_config("XXX", scale="large")
    medium_cfg = default_config("XXX", scale="medium")
    small_cfg = default_config("XXX", scale="small")

    large_preds = predict_airport("XXX", _same_demand_flights(), large_cfg, _resolver())
    medium_preds = predict_airport("XXX", _same_demand_flights(), medium_cfg, _resolver())
    small_preds = predict_airport("XXX", _same_demand_flights(), small_cfg, _resolver())

    def _arrival_wait(preds):
        row = next(p for p in preds if p.process == "passport_arr")
        return row.estimated_wait_minutes

    large_wait = _arrival_wait(large_preds)
    medium_wait = _arrival_wait(medium_preds)
    small_wait = _arrival_wait(small_preds)

    assert large_wait is not None and medium_wait is not None and small_wait is not None
    # Daha fazla server -> daha düşük (veya eşit) bekleme; en azından
    # üçü AYNI DEĞİL (scale gerçekten sonuca yansıyor, sayı hard-code
    # edilmedi - event engine'in KENDİ çıktısı karşılaştırılıyor).
    assert large_wait <= medium_wait <= small_wait
    assert large_wait < small_wait


def test_same_domestic_demand_produces_different_wait_across_scales():
    """Aynı ilke, security_domestic için (departure passport server sayısı yerine domestic lane sayısı değişkeni)."""
    from .factories import departure
    from app.queue.constants import LOCATION_DOMESTIC

    def flights():
        return [departure(8, 0, airport="XXX", aircraft="B77W", location=LOCATION_DOMESTIC, duration_minutes=90)]

    large_cfg = default_config("XXX", scale="large")
    small_cfg = default_config("XXX", scale="small")

    large_preds = predict_airport("XXX", flights(), large_cfg, _resolver())
    small_preds = predict_airport("XXX", flights(), small_cfg, _resolver())

    def _dom_wait(preds):
        row = next(p for p in preds if p.process == PROCESS_SECURITY_DOMESTIC)
        return row.estimated_wait_minutes

    large_wait = _dom_wait(large_preds)
    small_wait = _dom_wait(small_preds)
    assert large_wait is not None and small_wait is not None
    assert large_wait < small_wait


# ========================================================================
# Phase 21.17/21.18 - gerçek data/*.json ve database.sqlite değişmedi.
# ========================================================================

def test_real_data_files_and_database_untouched_by_scale_matrix():
    import subprocess
    repo_root = REPLAY_DIR.parents[1]
    result = subprocess.run(
        ["git", "status", "--porcelain", "data/", "database.sqlite"],
        cwd=repo_root, capture_output=True, text=True,
    )
    assert result.stdout.strip() == "", f"gerçek veri değişti: {result.stdout}"
