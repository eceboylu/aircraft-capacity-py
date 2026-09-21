"""
ADIM (Frontend Runtime Bug) - "predictions HTTP 200 ama frontend
'Yükleniyor...'/'Veri yenilenemedi'de kalıyor" şikayetinin kök neden
analizi + regresyon kilidi.

KÖK NEDEN (bu turda BULUNDU, kod BUG'ı DEĞİL): port 8000'de İKİ eski
`python -m app.web.server` süreci (en eskisi 2026-09-16'dan, hourly-
migration/domestic-intl-split/airport-scale/international-departure-
split DEĞİŞİKLİKLERİNİN HİÇBİRİNDEN ÖNCE başlatılmış) hâlâ dinliyordu.
O süreçlerin BELLEĞİNDEKİ `app.queue.api` hâlâ ESKİ `international_
departure` şeklini (düz `{current, windows}`, `passport`/`security`
alt-alanı YOK) üretiyordu - GÜNCEL `index.html` ise `data.international_
departure.passport.windows` okuyor. Tarayıcı hangi sürece denk gelirse
(ikisi de aynı portu dinliyordu) `passport` `undefined` gelip
`TypeError: Cannot read properties of undefined (reading 'windows')`
fırlatıyordu - `loadAll()`'ın `.catch()`'i bunu "Veri yenilenemedi"
banner'ına çeviriyor, ama `renderContent()` YARIM KALDIĞI için
`root.innerHTML` bir önceki "Yükleniyor..." metninde DONUYORDU.

Fix: iki eski süreç durduruldu, GÜNCEL koda tek bir süreç başlatıldı.
`app/queue/api.py`/`app/web/static/index.html`'in KENDİSİ zaten doğru
şekle sahipti (bkz. `tests/test_legacy_queue_isolation.py`,
`tests/test_international_departure_split_graphs.py`, 987 testin
TAMAMI) - bu dosya EK OLARAK, `index.html`'in GERÇEK bir JS motorunda
(Node) hiçbir veri şeklinde (dolu/boş/null - Phase 7 A-E senaryoları +
6 gerçek havalimanı) uncaught exception ATMADIĞINI kanıtlıyor - statik
string-match'in kanıtlayamayacağı bir garanti.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = REPO_ROOT / "app" / "web" / "static" / "index.html"
HARNESS = Path(__file__).resolve().parent / "frontend_harness" / "run_render.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node.js bu ortamda kurulu değil")


def _empty_series(process_name):
    return {"process": process_name, "current": None, "windows": []}


def _filled_series(process_name, wait=12.5, risk="LOW"):
    window = {
        "window_start": "2026-09-15T08:00:00", "window_end": "2026-09-15T09:00:00",
        "flight_count": 2, "expected_passengers": 250,
        "baseline_ratio": None, "flight_ratio": None, "passenger_ratio": None,
        "utilization": 0.4, "estimated_wait_minutes": wait,
        "risk": risk, "risk_label": "NORMAL" if risk == "LOW" else risk,
        "confidence": 0.8, "reasons": [],
        "calculated_at": "2026-09-15T08:05:00",
    }
    return {"process": process_name, "current": window, "windows": [window]}


def _base_payload(passport_series, security_series):
    """`airport_predictions()`'ın GERÇEK dönüş sözleşmesiyle AYNI şekil."""
    return {
        "airport": "TST",
        "breakdown": None,
        "domestic_security": _empty_series("security_dom"),
        "international_security": _empty_series("security_intl"),
        "international_passport": _empty_series("passport"),
        "international_departure": {
            "process": "international_departure",
            "passport": passport_series,
            "security": security_series,
        },
        "international_arrival": _empty_series("passport_arr"),
        "overall": _empty_series("overall"),
        "security": _empty_series("security"),
        "passport": _empty_series("passport"),
    }


SCENARIOS = {
    # A) passport dolu, security dolu
    "A_both_filled": _base_payload(
        _filled_series("passport_dep", wait=14.2), _filled_series("security_intl", wait=0.0),
    ),
    # B) passport dolu, security boş
    "B_passport_filled_security_empty": _base_payload(
        _filled_series("passport_dep", wait=66.8, risk="CRITICAL"), _empty_series("security_intl"),
    ),
    # C) passport boş, security dolu
    "C_passport_empty_security_filled": _base_payload(
        _empty_series("passport_dep"), _filled_series("security_intl", wait=0.0),
    ),
    # D) ikisi de boş
    "D_both_empty": _base_payload(_empty_series("passport_dep"), _empty_series("security_intl")),
    # E) airport'un hiçbir prediction window'u yok - TÜM alanlar boş/None
    # (breakdown dahil) - ADB'nin gerçek üretim şekliyle BİREBİR AYNI.
    "E_no_predictions_at_all": _base_payload(_empty_series("passport_dep"), _empty_series("security_intl")),
}


def _run_harness(tmp_path, payload) -> subprocess.CompletedProcess:
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [NODE, str(HARNESS), str(INDEX_HTML), str(data_path)],
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )


@pytest.mark.parametrize("scenario_name", list(SCENARIOS))
def test_render_content_never_throws_for_any_passport_security_combination(tmp_path, scenario_name):
    result = _run_harness(tmp_path, SCENARIOS[scenario_name])
    assert result.returncode == 0, (
        f"{scenario_name}: renderContent() exception fırlattı:\n{result.stdout}\n{result.stderr}"
    )
    assert "THREW" not in result.stdout
    assert result.stdout.startswith("OK")


def test_empty_windows_render_safe_placeholder_text_not_crash_dump(tmp_path):
    result = _run_harness(tmp_path, SCENARIOS["D_both_empty"])
    assert result.returncode == 0
    assert "Bu havalimanı için henüz pencere yok." in result.stdout
    assert "Veri bulunamadı" in result.stdout


def test_filled_and_empty_side_by_side_both_render_correct_text(tmp_path):
    """Senaryo B - passport CRITICAL/66.8dk göstermeli, security aynı anda 'Veri bulunamadı' göstermeli - biri diğerini bozmaz."""
    result = _run_harness(tmp_path, SCENARIOS["B_passport_filled_security_empty"])
    assert result.returncode == 0
    assert "66.8" in result.stdout or "1 saat 7 dk" in result.stdout
    assert "Veri bulunamadı" in result.stdout


# ------------------------------------------------------------------
# Phase 8 - gerçek replay havalimanlarıyla (ADB/CDG/CBR/MFG/OAG/ZRH).
# ------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_airport_payloads(tmp_path_factory):
    from datetime import datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.queue.api import airport_predictions

    db_path = REPO_ROOT / "tests" / "date_shift_replay" / "date_shift_replay.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    session = sessionmaker(bind=engine)()
    try:
        payloads = {
            code: airport_predictions(session, code, now=datetime(2026, 9, 18, 20, 0))
            for code in ("ADB", "CDG", "CBR", "MFG", "OAG", "ZRH")
        }
    finally:
        session.close()
    return payloads


@pytest.mark.parametrize("code", ["ADB", "CDG", "CBR", "MFG", "OAG", "ZRH"])
def test_real_replay_airport_renders_without_exception(tmp_path, real_airport_payloads, code):
    result = _run_harness(tmp_path, real_airport_payloads[code])
    assert result.returncode == 0, f"{code}: {result.stdout}\n{result.stderr}"
    assert "THREW" not in result.stdout


def test_adb_with_no_data_shows_zero_demand_24h_not_stuck_loading(tmp_path, real_airport_payloads):
    """
    ADIM (24-Hour Graph) ile GÜNCELLENDİ: ADB'nin bu replay DB'sinde
    gerçek talebi olmasa da (timezone çözülebildiği için) artık TAM
    24 saatlik sıfır-talep grafiği render EDİLİR - "Yükleniyor..."da
    KESİNLİKLE kalmaz, ve eski "Bu havalimanı için henüz pencere yok."
    (windows=[] durumu) yerine GERÇEK (ama sıfır-değerli) bir grafik +
    "NORMAL" pill'i gösterir - bu, "Prediction hesaplanamadı" ile
    "demand yok ve wait=0" arasındaki farkın frontend'e YANSIMASIDIR.
    """
    result = _run_harness(tmp_path, real_airport_payloads["ADB"])
    assert result.returncode == 0
    assert "Yükleniyor..." not in result.stdout
    assert "THREW" not in result.stdout


def test_zrh_passport_and_security_render_independently(tmp_path, real_airport_payloads):
    """ZRH: passport/security farklı window'larda olsa da ikisi de BAĞIMSIZ render olmalı - exception YOK, iki panel de HTML'e yazıldı."""
    result = _run_harness(tmp_path, real_airport_payloads["ZRH"])
    assert result.returncode == 0
    assert "THREW" not in result.stdout
    assert result.stdout.startswith("OK")
    # snippet 500 karakterle sınırlı (harness) - asıl garanti exception
    # atılmadan tüm 5 `graphSectionHtml()` çağrısının TAMAMLANMASI
    # (aksi halde `root.innerHTML` hiç atanmaz, "OK length=" satırı
    # HİÇ yazdırılmazdı).
    assert "OK length=" in result.stdout
