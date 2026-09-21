"""
ADIM (24-Hour Graph Timezone Display Fix) doğrulaması.

Internal UTC contract (`QueuePrediction.window_start`, `effective_time()`,
`floor_to_window()`, `operational_day_window()`) BU ADIM'DA DEĞİŞMEDİ -
SADECE `app/queue/api.py`'nin JSON çıktısına, GERÇEK `zoneinfo` ile
hesaplanan `window_start_local`/`window_end_local` alanları EKLENDİ ve
`app/web/static/index.html`'in grafik label'ı bu yeni alanı okuyacak
şekilde güncellendi (`window_start` CANONICAL/UTC alanı hiç DEĞİŞMEDİ).
"""
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.models import Airport, QueuePrediction

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = REPO_ROOT / "app" / "web" / "static" / "index.html"
RENDER_HARNESS = Path(__file__).resolve().parent / "frontend_harness" / "run_render.js"
NODE = shutil.which("node")


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def _add_row(session, airport_iata, timezone_name, window_start, process="security_dom",
             risk="CRITICAL", wait=33.9, flight_count=8, expected_passengers=1620):
    if session.get(Airport, airport_iata) is None:
        session.add(Airport(iata_code=airport_iata, airport_name=f"Test {airport_iata}", timezone=timezone_name))
    session.add(QueuePrediction(
        airport_iata=airport_iata, process=process,
        window_start=window_start, window_end=window_start.replace(minute=0) if False else window_start,
        flight_count=flight_count, expected_passengers=expected_passengers,
        utilization=0.5, estimated_wait_minutes=wait, risk=risk,
        reasons=json.dumps([]), confidence=0.7,
    ))
    session.commit()


# ------------------------------------------------------------------
# A/B/C/D - IST/SAW/CDG/ZRH: UTC bucket -> local display doğru mu?
# ------------------------------------------------------------------

@pytest.mark.parametrize("airport_iata,timezone_name,utc_hour,expected_local", [
    ("IST", "Europe/Istanbul", 1, "04:00"),   # A
    ("SAW", "Europe/Istanbul", 1, "04:00"),   # B
    ("CDG", "Europe/Paris", 6, "08:00"),      # C (DST/CEST, Eylül)
    ("ZRH", "Europe/Zurich", 6, "08:00"),     # D (DST/CEST, Eylül)
])
def test_utc_bucket_converts_to_correct_local_display(session, airport_iata, timezone_name, utc_hour, expected_local):
    window_start = datetime(2026, 9, 19, utc_hour, 0)
    _add_row(session, airport_iata, timezone_name, window_start)

    api = airport_predictions(session, airport_iata, now=datetime(2026, 9, 19, utc_hour, 30))
    windows = api["domestic_security"]["windows"]
    row = next(w for w in windows if w["window_start"] == window_start.isoformat())

    # H - raw UTC window_start DEĞİŞMEDİ.
    assert row["window_start"] == window_start.isoformat()
    # A-D - local display doğru.
    assert row["window_start_local"] is not None
    assert row["window_start_local"][11:16] == expected_local
    assert row["window_start_local"].endswith(("+03:00", "+02:00"))


# ------------------------------------------------------------------
# E - 24 local label 00..23.
# ------------------------------------------------------------------

def test_ist_24_local_labels_are_00_through_23(session):
    _add_row(session, "IST", "Europe/Istanbul", datetime(2026, 9, 19, 1, 0))
    api = airport_predictions(session, "IST", now=datetime(2026, 9, 19, 10, 0))
    windows = api["domestic_security"]["windows"]
    assert len(windows) >= 24
    local_hours = {w["window_start_local"][11:16] for w in windows if w["window_start_local"]}
    expected = {f"{h:02d}:00" for h in range(24)}
    assert expected <= local_hours


# ------------------------------------------------------------------
# F/G - padded (zero) bucket VE gerçek satır AYNI contract'ı taşır.
# ------------------------------------------------------------------

def test_padded_zero_bucket_has_local_label(session):
    _add_row(session, "IST", "Europe/Istanbul", datetime(2026, 9, 19, 1, 0))
    api = airport_predictions(session, "IST", now=datetime(2026, 9, 19, 10, 0))
    windows = api["domestic_security"]["windows"]
    zero_rows = [w for w in windows if w["flight_count"] == 0]
    assert zero_rows
    for w in zero_rows:
        assert w["window_start_local"] is not None
        assert w["window_end_local"] is not None


def test_real_row_has_local_label_matching_padded_contract_shape(session):
    _add_row(session, "IST", "Europe/Istanbul", datetime(2026, 9, 19, 1, 0))
    api = airport_predictions(session, "IST", now=datetime(2026, 9, 19, 10, 0))
    real_row = next(w for w in api["domestic_security"]["windows"] if w["flight_count"] == 8)
    zero_row = next(w for w in api["domestic_security"]["windows"] if w["flight_count"] == 0)
    assert set(real_row.keys()) == set(zero_row.keys())   # frontend real/padded farkını BİLMEZ


# ------------------------------------------------------------------
# H - raw UTC window_start değişmedi (backward compat).
# ------------------------------------------------------------------

def test_canonical_utc_window_start_field_unchanged_shape(session):
    window_start = datetime(2026, 9, 19, 1, 0)
    _add_row(session, "IST", "Europe/Istanbul", window_start)
    api = airport_predictions(session, "IST", now=datetime(2026, 9, 19, 10, 0))
    row = next(w for w in api["domestic_security"]["windows"] if w["flight_count"] == 8)
    assert row["window_start"] == "2026-09-19T01:00:00"   # naive UTC ISO, offset YOK


# ------------------------------------------------------------------
# I - cross-midnight: local label doğru, event yanlış güne taşınmadı.
# ------------------------------------------------------------------

def test_cross_midnight_event_gets_correct_local_label_not_shifted_to_wrong_day(session):
    """
    19 Sep 00:45 local IST departure -> effective_time (-120dk) UTC'de
    18 Sep 19:45'e düşer (yerelde hâlâ 18 Sep 22:45). Bu satırın LOCAL
    label'ı "22:00" (18 Sep) OLMALI, "19 Sep"e YANLIŞLIKLA taşınmamalı.
    """
    # UTC 18 Sep 19:00 -> Istanbul yerel 18 Sep 22:00.
    window_start = datetime(2026, 9, 18, 19, 0)
    _add_row(session, "IST", "Europe/Istanbul", window_start, expected_passengers=150, flight_count=1)

    api = airport_predictions(session, "IST", now=datetime(2026, 9, 19, 10, 0))
    windows = api["domestic_security"]["windows"]
    row = next(w for w in windows if w["window_start"] == window_start.isoformat())

    assert row["window_start_local"].startswith("2026-09-18T22:00:00")
    assert row["flight_count"] == 1   # gerçek satır KAYBOLMADI/silinmedi


# ------------------------------------------------------------------
# J - current bucket local label doğru.
# ------------------------------------------------------------------

def test_current_bucket_has_correct_local_label():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Airport(iata_code="IST", airport_name="Istanbul Airport", timezone="Europe/Istanbul"))
    session.add(QueuePrediction(
        airport_iata="IST", process="security_dom",
        window_start=datetime(2026, 9, 19, 11, 0), window_end=datetime(2026, 9, 19, 12, 0),
        flight_count=2, expected_passengers=300, utilization=0.3,
        estimated_wait_minutes=2.0, risk="LOW", reasons=json.dumps([]), confidence=0.7,
    ))
    session.commit()

    # now = 2026-09-19 14:37 UTC -> Istanbul local 17:37 -> current bucket UTC 14:00 -> local 17:00.
    api = airport_predictions(session, "IST", now=datetime(2026, 9, 19, 14, 37))
    current = api["domestic_security"]["current"]
    session.close()

    assert current is not None
    assert current["window_start"] == "2026-09-19T14:00:00"
    assert current["window_start_local"][11:16] == "17:00"


# ------------------------------------------------------------------
# 5 görünür grafiğin TAMAMI local label taşır.
# ------------------------------------------------------------------

def test_all_five_visible_graphs_carry_local_fields(session):
    for process in ("security_dom", "security_intl", "passport_dep", "passport_arr"):
        _add_row(session, "IST", "Europe/Istanbul", datetime(2026, 9, 19, 5, 0), process=process)
    api = airport_predictions(session, "IST", now=datetime(2026, 9, 19, 10, 0))

    assert api["overall"]["windows"][0]["window_start_local"] is not None
    assert api["domestic_security"]["windows"][0]["window_start_local"] is not None
    assert api["international_departure"]["passport"]["windows"][0]["window_start_local"] is not None
    assert api["international_departure"]["security"]["windows"][0]["window_start_local"] is not None
    assert api["international_arrival"]["windows"][0]["window_start_local"] is not None


def test_legacy_raw_fields_do_not_carry_local_fields(session):
    """
    Bölüm 5/14 - legacy passport/security alanları için değişiklik
    GEREKMİYOR: `process_series()` bunlara `tz` VERMEDİĞİ için (bkz.
    `airport_predictions()`), `window_start_local` HER ZAMAN `None`
    kalır - anahtar `_window_to_dict`'in sabit şekli gereği (backward
    compatible, geriye dönük tüketiciler eksik anahtar yerine `None`
    gördüğünde daha güvenli davranır) yine de MEVCUTTUR, ama DEĞERİ
    hiçbir zaman gerçek bir yerel saat TAŞIMAZ - pad de edilmiyor.
    """
    _add_row(session, "IST", "Europe/Istanbul", datetime(2026, 9, 19, 5, 0), process="security_dom")
    from app.queue.constants import PROCESS_SECURITY
    session.add(QueuePrediction(
        airport_iata="IST", process=PROCESS_SECURITY,
        window_start=datetime(2026, 9, 19, 5, 0), window_end=datetime(2026, 9, 19, 6, 0),
        flight_count=1, expected_passengers=100, utilization=0.2,
        estimated_wait_minutes=1.0, risk="LOW", reasons=json.dumps([]), confidence=0.7,
    ))
    session.commit()
    api = airport_predictions(session, "IST", now=datetime(2026, 9, 19, 10, 0))
    assert api["security"]["windows"][0]["window_start_local"] is None
    assert len(api["security"]["windows"]) == 1   # pad edilmedi (legacy, 24 saate tamamlanmadı)


# ------------------------------------------------------------------
# Fallback - timezone çözülemeyen havalimanı.
# ------------------------------------------------------------------

def test_unresolvable_timezone_falls_back_to_none_local_field(session):
    session.add(Airport(iata_code="ZZZ", airport_name="Unknown TZ", timezone=None))
    session.add(QueuePrediction(
        airport_iata="ZZZ", process="security_dom",
        window_start=datetime(2026, 9, 19, 8, 0), window_end=datetime(2026, 9, 19, 9, 0),
        flight_count=1, expected_passengers=100, utilization=0.2,
        estimated_wait_minutes=1.0, risk="LOW", reasons=json.dumps([]), confidence=0.7,
    ))
    session.commit()
    api = airport_predictions(session, "ZZZ", now=datetime(2026, 9, 19, 8, 30))
    row = api["domestic_security"]["windows"][0]
    assert row["window_start_local"] is None
    assert row["window_start"] == "2026-09-19T08:00:00"


# ------------------------------------------------------------------
# Bölüm 13 - Frontend runtime testi (GERÇEK Node execution).
# ------------------------------------------------------------------

pytestmark_node = pytest.mark.skipif(NODE is None, reason="node.js bu ortamda kurulu değil")


def _run_render(tmp_path, payload):
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run(
        [NODE, str(RENDER_HARNESS), str(INDEX_HTML), str(data_path)],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "THREW" not in result.stdout
    return result.stdout


def _window(window_start_utc, window_start_local, risk="LOW", wait=3.0):
    return {
        "window_start": window_start_utc, "window_end": window_start_utc,
        "window_start_local": window_start_local, "window_end_local": window_start_local,
        "flight_count": 1, "expected_passengers": 150,
        "baseline_ratio": None, "flight_ratio": None, "passenger_ratio": None,
        "utilization": 0.3, "estimated_wait_minutes": wait,
        "risk": risk, "risk_label": risk, "confidence": 0.8, "reasons": [],
        "calculated_at": "2026-09-19T00:00:00",
    }


def _series(process_name, window):
    return {"process": process_name, "current": window, "windows": [window]}


@pytest.mark.skipif(NODE is None, reason="node.js bu ortamda kurulu değil")
def test_frontend_renders_local_label_not_utc_label(tmp_path):
    """Bölüm 13 - window_start=01:00 UTC + window_start_local=04:00+03:00 verildiğinde ekranda '04:00' yazmalı, '01:00' YAZMAMALI."""
    w = _window("2026-09-19T01:00:00", "2026-09-19T04:00:00+03:00", risk="CRITICAL", wait=33.9)
    payload = {
        "airport": "IST", "breakdown": None,
        "domestic_security": _series("security_dom", w),
        "international_security": _series("security_intl", w),
        "international_passport": _series("passport", None),
        "international_departure": {"process": "international_departure", "passport": _series("passport_dep", w), "security": _series("security_intl", w)},
        "international_arrival": _series("passport_arr", w),
        "overall": _series("overall", w),
        "security": _series("security", None),
        "passport": _series("passport", None),
    }
    stdout = _run_render(tmp_path, payload)
    assert "04:00" in stdout
    assert "01:00" not in stdout


@pytest.mark.skipif(NODE is None, reason="node.js bu ortamda kurulu değil")
def test_frontend_falls_back_to_utc_label_when_local_field_missing(tmp_path):
    """window_start_local YOKSA (None) frontend ham window_start'a (UTC) güvenli şekilde düşer - crash YOK."""
    w = _window("2026-09-19T01:00:00", None, risk="LOW", wait=0.0)
    payload = {
        "airport": "ZZZ", "breakdown": None,
        "domestic_security": _series("security_dom", w),
        "international_security": _series("security_intl", w),
        "international_passport": _series("passport", None),
        "international_departure": {"process": "international_departure", "passport": _series("passport_dep", w), "security": _series("security_intl", w)},
        "international_arrival": _series("passport_arr", w),
        "overall": _series("overall", w),
        "security": _series("security", None),
        "passport": _series("passport", None),
    }
    stdout = _run_render(tmp_path, payload)
    assert "01:00" in stdout
    assert "THREW" not in stdout
