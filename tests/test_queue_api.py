"""
ADIM 5H - Minimal read-only API/frontend veri katmanı testleri.

`app/queue/api.py` (fonksiyonlar, doğrudan test edilir) ve
`app/web/server.py` (gerçek HTTP soketi üzerinden, tek bir
entegrasyon testiyle) kapsanır. Hiçbir test yoğunluk/kapasite
matematiği YENİDEN yapmaz - sadece DB'ye yazılmış `QueuePrediction`
satırlarının doğru okunup JSON'a çevrildiğini doğrular.
"""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.queue.api import (
    RISK_TO_UI_LABEL,
    airport_predictions,
    process_series,
    tracked_airports,
    ui_label_for_risk,
)
from app.queue.constants import (
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
)
from app.queue.models import QueuePrediction


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    try:
        yield db
    finally:
        db.close()


def add_prediction(
    session,
    airport_iata="IST",
    process=PROCESS_SECURITY,
    window_start=datetime(2026, 9, 15, 8, 0),
    window_end=datetime(2026, 9, 15, 8, 15),
    flight_count=8,
    expected_passengers=900,
    baseline_ratio=1.0,
    utilization=None,
    estimated_wait_minutes=None,
    risk=RISK_LOW,
    reasons=None,
    confidence=0.75,
):
    row = QueuePrediction(
        airport_iata=airport_iata,
        process=process,
        window_start=window_start,
        window_end=window_end,
        flight_count=flight_count,
        expected_passengers=expected_passengers,
        baseline_ratio=baseline_ratio,
        utilization=utilization,
        estimated_wait_minutes=estimated_wait_minutes,
        risk=risk,
        reasons=json.dumps(reasons or []),
        confidence=confidence,
    )
    session.add(row)
    session.commit()
    return row


# --------------------------------------------------------------------
# TEST A - API gerçek DB kayıtlarını döndürüyor
# --------------------------------------------------------------------

def test_a_process_series_returns_real_db_row(session):
    add_prediction(session, flight_count=14, expected_passengers=2180, risk=RISK_HIGH)

    result = process_series(session, "IST", PROCESS_SECURITY)

    assert result["current"]["flight_count"] == 14
    assert result["current"]["expected_passengers"] == 2180
    assert result["current"]["risk"] == RISK_HIGH


def test_a_reasons_are_parsed_from_json_string(session):
    add_prediction(session, reasons=[{"code": "widebody", "severity": "warning", "message": "x"}])

    result = process_series(session, "IST", PROCESS_SECURITY)

    assert result["current"]["reasons"] == [
        {"code": "widebody", "severity": "warning", "message": "x"}
    ]


# --------------------------------------------------------------------
# TEST B - Havalimanı ayrımı: IST/SAW/ADB karışmıyor
# --------------------------------------------------------------------

def test_b_airport_separation(session):
    add_prediction(session, airport_iata="IST", flight_count=10)
    add_prediction(session, airport_iata="SAW", flight_count=3)
    add_prediction(session, airport_iata="ADB", flight_count=1)

    ist = process_series(session, "IST", PROCESS_SECURITY)
    saw = process_series(session, "SAW", PROCESS_SECURITY)
    adb = process_series(session, "ADB", PROCESS_SECURITY)

    assert ist["current"]["flight_count"] == 10
    assert saw["current"]["flight_count"] == 3
    assert adb["current"]["flight_count"] == 1


def test_b_tracked_airports_lists_all_and_only_real_airports(session):
    add_prediction(session, airport_iata="IST")
    add_prediction(session, airport_iata="SAW")

    assert tracked_airports(session) == ["IST", "SAW"]


def test_b_tracked_airports_empty_when_no_predictions(session):
    assert tracked_airports(session) == []


# --------------------------------------------------------------------
# TEST C - Süreç ayrımı: security ve passport ayrı dönüyor
# --------------------------------------------------------------------

def test_c_process_separation(session):
    add_prediction(
        session, process=PROCESS_SECURITY, flight_count=5,
        utilization=None, estimated_wait_minutes=None,
    )
    add_prediction(
        session, process=PROCESS_PASSPORT, flight_count=9,
        utilization=0.42, estimated_wait_minutes=3.1, risk=RISK_MEDIUM,
    )

    result = airport_predictions(session, "IST")

    assert result["security"]["current"]["flight_count"] == 5
    assert result["security"]["current"]["estimated_wait_minutes"] is None
    assert result["passport"]["current"]["flight_count"] == 9
    assert result["passport"]["current"]["estimated_wait_minutes"] == 3.1


def test_c_security_never_carries_wait_minutes(session):
    """Security'de estimated_wait_minutes hiçbir koşulda üretilmez (backend kuralı) - API bunu bozmaz."""
    add_prediction(session, process=PROCESS_SECURITY, estimated_wait_minutes=None)
    result = process_series(session, "IST", PROCESS_SECURITY)
    assert result["current"]["estimated_wait_minutes"] is None


# --------------------------------------------------------------------
# TEST D - Kronolojik sıra
# --------------------------------------------------------------------

def test_d_windows_are_chronologically_ordered(session):
    add_prediction(session, window_start=datetime(2026, 9, 15, 9, 0), window_end=datetime(2026, 9, 15, 9, 15))
    add_prediction(session, window_start=datetime(2026, 9, 15, 8, 0), window_end=datetime(2026, 9, 15, 8, 15))
    add_prediction(session, window_start=datetime(2026, 9, 15, 8, 30), window_end=datetime(2026, 9, 15, 8, 45))

    result = process_series(session, "IST", PROCESS_SECURITY)
    starts = [w["window_start"] for w in result["windows"]]

    assert starts == sorted(starts)
    assert starts[0] == "2026-09-15T08:00:00"
    assert starts[-1] == "2026-09-15T09:00:00"


def test_d_current_is_the_most_recent_window(session):
    add_prediction(session, window_start=datetime(2026, 9, 15, 8, 0), flight_count=1)
    add_prediction(session, window_start=datetime(2026, 9, 15, 9, 0), flight_count=2)
    add_prediction(session, window_start=datetime(2026, 9, 15, 8, 30), flight_count=3)

    result = process_series(session, "IST", PROCESS_SECURITY)
    assert result["current"]["flight_count"] == 2   # 09:00 - en yeni pencere


# --------------------------------------------------------------------
# TEST E - Veri yoksa NORMAL gösterilmiyor
# --------------------------------------------------------------------

def test_e_no_predictions_returns_none_not_normal(session):
    result = process_series(session, "IST", PROCESS_SECURITY)

    assert result["current"] is None
    assert result["windows"] == []


def test_e_airport_with_predictions_in_one_process_only(session):
    """SAW sadece security'de tahmine sahip - passport 'Veri bulunamadı' olmalı, NORMAL değil."""
    add_prediction(session, airport_iata="SAW", process=PROCESS_SECURITY)

    result = airport_predictions(session, "SAW")

    assert result["security"]["current"] is not None
    assert result["passport"]["current"] is None


def test_e_unknown_risk_is_not_relabeled_as_normal(session):
    add_prediction(session, risk=RISK_UNKNOWN, baseline_ratio=None)

    result = process_series(session, "IST", PROCESS_SECURITY)

    assert result["current"]["risk"] == RISK_UNKNOWN
    assert result["current"]["risk_label"] != "NORMAL"
    assert result["current"]["risk_label"] == "BİLİNMİYOR"


# --------------------------------------------------------------------
# TEST F - Risk mapping
# --------------------------------------------------------------------

@pytest.mark.parametrize("risk,expected_label", [
    (RISK_LOW, "NORMAL"),
    (RISK_MEDIUM, "YOĞUN"),
    (RISK_HIGH, "ÇOK YOĞUN"),
    (RISK_CRITICAL, "ÇOK YOĞUN"),
    (RISK_UNKNOWN, "BİLİNMİYOR"),
])
def test_f_risk_label_mapping_matches_backend_values(risk, expected_label):
    assert ui_label_for_risk(risk) == expected_label


def test_f_no_data_maps_to_none_not_a_label():
    assert ui_label_for_risk(None) is None


def test_f_mapping_is_defined_in_exactly_one_place():
    """Tüm 5 backend risk değeri RISK_TO_UI_LABEL'de - eşleme dağınık değil."""
    assert set(RISK_TO_UI_LABEL) == {
        RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL, RISK_UNKNOWN,
    }


def test_f_unmapped_risk_value_passes_through_unchanged():
    """İleride yeni bir risk değeri eklenirse sessizce NORMAL'e düşmez."""
    assert ui_label_for_risk("SOME_NEW_RISK") == "SOME_NEW_RISK"


# --------------------------------------------------------------------
# TEST G - Dinamik yenileme (polling'in ARKA PLANDAKİ ön koşulu):
# API her çağrıldığında GÜNCEL DB durumunu yansıtıyor, bir önceki
# çağrının sonucunu CACHE'lemiyor. Gerçek 30 saniye beklenmez;
# T0 -> T1 geçişi doğrudan aynı satırı güncelleyip TEKRAR okuyarak
# simüle edilir (tıpkı pipeline'ın 30 dakikalık upsert'i gibi).
# --------------------------------------------------------------------

def test_g_api_reflects_db_change_between_two_calls_without_caching(session):
    row = add_prediction(session, flight_count=8, expected_passengers=900, risk=RISK_LOW)

    t0 = process_series(session, "IST", PROCESS_SECURITY)
    assert t0["current"]["flight_count"] == 8
    assert t0["current"]["risk_label"] == "NORMAL"

    # T1: pipeline aynı pencereyi upsert etti - departure surge senaryosu.
    row.flight_count = 18
    row.expected_passengers = 2600
    row.risk = RISK_CRITICAL
    session.commit()

    t1 = process_series(session, "IST", PROCESS_SECURITY)
    assert t1["current"]["flight_count"] == 18
    assert t1["current"]["expected_passengers"] == 2600
    assert t1["current"]["risk_label"] == "ÇOK YOĞUN"


def test_g_new_window_appended_becomes_new_current(session):
    """Delay compression senaryosu: yeni bir pencere eklendiğinde 'current' ona kayar."""
    add_prediction(session, window_start=datetime(2026, 9, 15, 8, 0), flight_count=5)

    before = process_series(session, "IST", PROCESS_SECURITY)
    assert len(before["windows"]) == 1

    add_prediction(session, window_start=datetime(2026, 9, 15, 8, 15), flight_count=11)

    after = process_series(session, "IST", PROCESS_SECURITY)
    assert len(after["windows"]) == 2
    assert after["current"]["flight_count"] == 11


# --------------------------------------------------------------------
# TEST H - Mevcut prediction matematiği testleri bozulmadı
# (bu dosyanın kendisi hiçbir engine/service kodunu import etmiyor;
# gerçek doğrulama full pytest çalıştırmasıyla yapılır, bkz. rapor)
# --------------------------------------------------------------------

def test_h_api_module_does_not_import_calculation_engine():
    """
    Statik bir güvence: `app/queue/api.py` motor modüllerini
    (engine/core/domain/reasons) import ETMEZ - sadece models+constants.
    Bu, "frontend matematiği kopyalamıyor" kuralının kod seviyesinde
    de doğru olduğunun ucuz bir kanıtı.
    """
    import app.queue.api as api_module

    forbidden_prefixes = (
        "core.erlang", "core.scoring", "domain.demand",
        "domain.flight_rules", "domain.flows", "reasons.detector", "engine",
    )
    source_lines = open(api_module.__file__, encoding="utf-8").readlines()
    import_lines = [
        line for line in source_lines
        if line.strip().startswith("from .") or line.strip().startswith("import ")
    ]
    for line in import_lines:
        for forbidden in forbidden_prefixes:
            assert forbidden not in line, f"yasak import: {line.strip()}"


# --------------------------------------------------------------------
# Sunucu entegrasyonu - GERÇEK HTTP soketi (tek test, port çakışmasını
# önlemek için 0 portu = OS'in seçtiği boş port kullanılır)
# --------------------------------------------------------------------

def test_server_serves_real_predictions_over_http(monkeypatch):
    from http.server import ThreadingHTTPServer

    import app.web.server as server_module

    # `ThreadingHTTPServer` her isteği YENİ bir thread'de işler; SQLite
    # `:memory:` varsayılan olarak bağlantı başına AYRI bir veritabanı
    # verir - StaticPool ile TÜM thread'ler AYNI tek bağlantıyı (ve
    # dolayısıyla aynı tabloları/satırları) paylaşır. Bu SADECE test
    # altyapısı içindir, gerçek `app/db.py` DEĞİŞMEDİ.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)

    session = maker()
    add_prediction(session, airport_iata="IST", flight_count=7)
    session.close()

    monkeypatch.setattr(server_module, "get_session", maker)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.QueueMonitorHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/airports", timeout=5) as resp:
            airports = json.loads(resp.read())
        assert airports == ["IST"]

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/airports/IST/predictions", timeout=5
        ) as resp:
            payload = json.loads(resp.read())
        assert payload["airport"] == "IST"
        assert payload["security"]["current"]["flight_count"] == 7

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            assert resp.status == 200
            assert b"Airport Queue Monitor" in resp.read()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_server_rejects_post(monkeypatch):
    """CRUD YOK - bu sunucu POST/PUT/DELETE desteklemiyor (BaseHTTPRequestHandler varsayılanı: 501)."""
    from http.server import ThreadingHTTPServer

    import app.web.server as server_module

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.QueueMonitorHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/airports", method="POST", data=b"{}"
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(req, timeout=5)
        assert excinfo.value.code == 501
    finally:
        httpd.shutdown()
        thread.join(timeout=5)

