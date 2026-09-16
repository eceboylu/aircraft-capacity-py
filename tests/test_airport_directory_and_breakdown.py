"""
ADIM 6A-UI-2 - Havalimanı dizini (isim), operasyon dağılımı
(departure/arrival, domestic/international) ve frontend'in bu
verileri okuyabildiğinin ön koşul testleri.

Bu dosya `app.queue.engine`/`core`/`domain` içindeki hiçbir
hesaplamayı YENİDEN test ETMEZ - sadece `app/queue/api.py`'nin YENİ
iki fonksiyonunu (`airport_directory`, `traffic_breakdown`) ve
bunların `airport_predictions()`/sunucu uç noktalarına doğru
kablolandığını doğrular.

Not (dürüst sınırlama - testler G/H, prompt'un "frontend seçim
kalıcılığı"/"seçili airport değişince grafik değişiyor" maddeleri):
bu repoda bir JS test koşucusu (jest/vitest) YOK ve "minimum
dependency" kuralı yeni bir tane eklemeyi haklı çıkarmıyor (bkz.
ADIM 6A-UI raporu, Test G notu - aynı sınırlama burada da geçerli).
Bu testler bu davranışın ARKA PLANDAKİ backend ön koşulunu
kanıtlıyor: her havalimanı için farklı, doğru veri geliyor mu.
"""

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timedelta

import app.queue.pipeline as pipeline_module
from app.models import Base
from app.queue.api import (
    airport_directory,
    airport_predictions,
    tracked_airports,
    traffic_breakdown,
)
from app.queue.constants import DIRECTION_ARRIVAL, DIRECTION_DEPARTURE, LOCATION_DOMESTIC, LOCATION_INTERNATIONAL
from app.queue.ingestion.airports_import import import_airports
from app.queue.ingestion import airlabs_client
from app.queue.models import Flight, QueuePrediction
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset

from . import airlabs_operational_source as ops

import os

REAL_AIRPORTS_SQL = os.path.join(os.path.dirname(__file__), "..", "data", "flight_airports.sql")


@pytest.fixture(scope="module")
def operational_state():
    """`tests/test_operational_dataset.py`'deki AYNI T0->T1->T2 kurulumu (ayrı bir modül-scope kopyası)."""
    mp = pytest.MonkeyPatch()
    try:
        source = ops.install(mp, round_="t0")
        test_engine = create_engine("sqlite://")
        TestSessionLocal = sessionmaker(bind=test_engine)
        mp.setattr(pipeline_module, "init_db", lambda drop_first=False: Base.metadata.create_all(test_engine))
        mp.setattr(pipeline_module, "get_session", lambda: TestSessionLocal())

        Base.metadata.create_all(test_engine)
        seed_session = TestSessionLocal()
        import_airports(seed_session, REAL_AIRPORTS_SQL)
        seed_verified_dataset(seed_session)
        seed_curated_fallback(seed_session)
        seed_family_and_ga(seed_session)
        seed_session.close()

        source_a = airlabs_client.build_source_a(ops.AIRPORTS)
        source_b = airlabs_client.build_source_b()
        for round_ in ops.ROUNDS:
            source.round = round_
            pipeline_module.run(source_a=source_a, source_b=source_b)

        yield {"session_factory": TestSessionLocal}
    finally:
        mp.undo()


def _session(state):
    return state["session_factory"]()


# --------------------------------------------------------------------
# TEST A/B/C - IST /api/airports'ta var, prediction endpoint çalışıyor,
# frontend'in seçebilmesi için gereken ön koşul (directory + predictions
# ikisi de dolu) sağlanıyor.
# --------------------------------------------------------------------

def test_a_ist_is_in_tracked_airports(operational_state):
    session = _session(operational_state)
    codes = tracked_airports(session)
    session.close()
    assert "IST" in codes


def test_b_ist_predictions_endpoint_returns_real_data(operational_state):
    session = _session(operational_state)
    result = airport_predictions(session, "IST")
    session.close()
    assert result["airport"] == "IST"
    assert result["security"]["windows"] or result["passport"]["windows"]


def test_c_ist_selectability_precondition_directory_and_predictions_agree(operational_state):
    """
    Frontend'in IST'yi seçebilmesi için ÖN KOŞUL: `directory`'nin IST
    listelemesi VE `/predictions`'ın IST için gerçekten veri döndürmesi.
    (Asıl tıklama/seçim davranışı JS'tir, üstteki modül notuna bkz.)
    """
    session = _session(operational_state)
    directory_codes = {e["iata"] for e in airport_directory(session)}
    predictions = airport_predictions(session, "IST")
    session.close()

    assert "IST" in directory_codes
    assert predictions["airport"] == "IST"


# --------------------------------------------------------------------
# TEST D/E - Havalimanı adı DB'den geliyor, hardcoded map YOK
# --------------------------------------------------------------------

def test_d_airport_names_come_from_the_airports_table(operational_state):
    session = _session(operational_state)
    directory = {e["iata"]: e["name"] for e in airport_directory(session)}
    session.close()

    # flight_airports.sql'deki GERÇEK isimler (import_airports ile
    # yüklendi) - burada UYDURULMADI, sadece gerçek satırla karşılaştırılıyor.
    assert directory["IST"] == "Istanbul Airport"
    assert directory["SAW"] == "Sabiha Gokcen International Airport"
    assert directory["ADB"] == "Izmir Adnan Menderes Airport"


def test_d_missing_airport_name_is_none_not_fabricated(operational_state):
    """Airport tablosunda satırı/ad'ı olmayan bir kod için sahte ad ÜRETİLMEZ."""
    session = _session(operational_state)
    session.add(QueuePrediction(
        airport_iata="ZZZ", process="security",
        window_start=datetime(2026, 9, 15, 8, 0), window_end=datetime(2026, 9, 15, 8, 15),
        flight_count=1, expected_passengers=100, risk="LOW", reasons="[]", confidence=0.5,
    ))
    session.commit()

    directory = {e["iata"]: e["name"] for e in airport_directory(session)}
    session.close()

    assert "ZZZ" in directory
    assert directory["ZZZ"] is None


def test_e_no_hardcoded_airport_name_map_in_source(operational_state):
    """
    §3 YASAK: `const airportNames = {IST: "...", ...}` gibi bir sözlük
    ne frontend'de ne backend'de olmalı. Gerçek isimler (Istanbul
    Airport vb.) kaynak dosyalarında literal olarak GEÇMEMELİ - sadece
    DB'den (flight_airports.sql importu) gelmeli.
    """
    html = open("app/web/static/index.html", encoding="utf-8").read()
    api_source = open("app/queue/api.py", encoding="utf-8").read()

    forbidden_literals = ("Istanbul Airport", "Sabiha Gokcen", "Adnan Menderes")
    for literal in forbidden_literals:
        assert literal not in html, f"frontend'de hardcoded isim bulundu: {literal}"
        assert literal not in api_source, f"api.py'de hardcoded isim bulundu: {literal}"

    assert "airportNames" not in html
    assert "IST:" not in html and '"IST"' not in html.split("<script>")[0]


# --------------------------------------------------------------------
# TEST F/G - Yeni havalimanı otomatik eklenir, seçili havalimanı
# listeden kaybolmadıkça YERİNDE kalır (backend ön koşulu: yeni kod
# eklenince eskiler KAYBOLMAZ, sadece büyür).
# --------------------------------------------------------------------

def test_f_new_airport_appears_in_directory_without_code_change(operational_state):
    session = _session(operational_state)
    before = {e["iata"] for e in airport_directory(session)}
    assert "AYT" not in before

    session.add(QueuePrediction(
        airport_iata="AYT", process="security",
        window_start=datetime(2026, 9, 15, 8, 0), window_end=datetime(2026, 9, 15, 8, 15),
        flight_count=1, expected_passengers=100, risk="LOW", reasons="[]", confidence=0.5,
    ))
    session.commit()

    after_entries = airport_directory(session)
    after = {e["iata"] for e in after_entries}
    session.close()

    assert "AYT" in after
    assert before <= after   # eski havalimanları KAYBOLMADI


def test_g_existing_airports_are_unaffected_when_a_new_one_is_added(operational_state):
    """Backend ön koşulu: IST'nin verisi AYT eklenince DEĞİŞMEZ (frontend'in seçimi koruyabilmesinin temeli)."""
    session = _session(operational_state)
    ist_before = airport_predictions(session, "IST")
    session.close()

    session = _session(operational_state)
    session.add(QueuePrediction(
        airport_iata="ZAYT", process="security",
        window_start=datetime(2026, 9, 15, 9, 0), window_end=datetime(2026, 9, 15, 9, 15),
        flight_count=1, expected_passengers=100, risk="LOW", reasons="[]", confidence=0.5,
    ))
    session.commit()
    ist_after = airport_predictions(session, "IST")
    session.close()

    assert ist_before["security"]["windows"] == ist_after["security"]["windows"]


# --------------------------------------------------------------------
# TEST H/I - Havalimanı değişince veri gerçekten değişiyor / karışmıyor
# --------------------------------------------------------------------

def test_h_different_airports_return_different_prediction_data(operational_state):
    session = _session(operational_state)
    ist = airport_predictions(session, "IST")
    saw = airport_predictions(session, "SAW")
    session.close()

    assert ist["airport"] != saw["airport"]
    assert ist["security"]["windows"] != saw["security"]["windows"]


def test_i_breakdown_does_not_mix_airports(operational_state):
    session = _session(operational_state)
    ist = traffic_breakdown(session, "IST", now=datetime(2026, 9, 15, 8, 20))
    saw = traffic_breakdown(session, "SAW", now=datetime(2026, 9, 15, 8, 20))
    session.close()

    # IST'de bu pencerede kesinlikle uçuş var (surge bank); SAW'ın
    # kendi (farklı) uçuşları IST'in sayımına HİÇ karışmamalı.
    assert ist is not None
    assert ist != saw


# --------------------------------------------------------------------
# TEST J/K/L - departure/arrival + domestic/international doğru
# hesaplanıyor, ve doğru ("current") pencereye ait
# --------------------------------------------------------------------

def test_j_and_k_breakdown_matches_real_flight_rows_directly_counted(operational_state):
    """
    `traffic_breakdown`'ın döndürdüğü sayıları, AYNI pencere için
    `Flight` tablosunu DOĞRUDAN (production kodunu ATLAYARAK) sayarak
    çapraz doğrular - iki bağımsız sayım örtüşmeli.
    """
    session = _session(operational_state)
    now = datetime(2026, 9, 15, 8, 20)
    breakdown = traffic_breakdown(session, "IST", now=now)
    assert breakdown is not None

    window_start = datetime.fromisoformat(breakdown["window_start"])
    window_end = datetime.fromisoformat(breakdown["window_end"])

    rows = session.execute(
        select(Flight).where(
            Flight.airport_iata == "IST",
            Flight.status.notin_(("cancelled", "diverted")),
        )
    ).scalars().all()
    session.close()

    expected_dep = expected_arr = expected_dom = expected_intl = 0
    for f in rows:
        moment = f.dep_scheduled_utc if f.direction == DIRECTION_DEPARTURE else f.arr_scheduled_utc
        if moment is None or not (window_start <= moment < window_end):
            continue
        if f.direction == DIRECTION_DEPARTURE:
            expected_dep += 1
        else:
            expected_arr += 1
        if f.location == LOCATION_DOMESTIC:
            expected_dom += 1
        elif f.location == LOCATION_INTERNATIONAL:
            expected_intl += 1

    assert breakdown["departures"] == expected_dep
    assert breakdown["arrivals"] == expected_arr
    assert breakdown["domestic"] == expected_dom
    assert breakdown["international"] == expected_intl


def test_l_breakdown_window_contains_now_when_possible(operational_state):
    session = _session(operational_state)
    now = datetime(2026, 9, 15, 8, 20)
    breakdown = traffic_breakdown(session, "IST", now=now)
    session.close()

    window_start = datetime.fromisoformat(breakdown["window_start"])
    window_end = datetime.fromisoformat(breakdown["window_end"])
    assert window_start <= now < window_end
    assert window_end - window_start == timedelta(minutes=60)


def test_l_breakdown_is_none_when_airport_has_no_dated_flights(operational_state):
    """Hiç zaman damgalı uçuşu olmayan bir havalimanı için sahte pencere/sıfır ÜRETİLMEZ."""
    session = _session(operational_state)
    session.add(QueuePrediction(
        airport_iata="NODATA", process="security",
        window_start=datetime(2026, 9, 15, 8, 0), window_end=datetime(2026, 9, 15, 8, 15),
        flight_count=1, expected_passengers=100, risk="LOW", reasons="[]", confidence=0.5,
    ))
    session.commit()

    result = traffic_breakdown(session, "NODATA")
    session.close()
    assert result is None


# --------------------------------------------------------------------
# TEST M - Yolcu sayısı frontend'de YOK (statik kontrol)
# --------------------------------------------------------------------

def test_m_frontend_never_renders_passenger_or_flight_counts():
    html = open("app/web/static/index.html", encoding="utf-8").read()
    script = html.split("<script>", 1)[1]

    for forbidden in ("expected_passengers", "flight_count", " pax", "yolcu say"):
        assert forbidden not in script, f"frontend hâlâ '{forbidden}' gösteriyor"


# --------------------------------------------------------------------
# TEST N/O - security/passport davranışı ve current semantiği
# BOZULMADI (bu dosyanın YENİ kodu security/passport/current'a hiç
# dokunmuyor - statik kanıt)
# --------------------------------------------------------------------

def test_n_and_o_new_functions_do_not_touch_security_passport_or_current_logic():
    """
    `traffic_breakdown`/`airport_directory` security/passport/current
    hesabına dokunmuyor - AST üzerinden gerçek fonksiyon ÇAĞRILARI
    (docstring/yorumdaki metin referansları DEĞİL) incelenir.
    """
    import ast

    tree = ast.parse(open("app/queue/api.py", encoding="utf-8").read())
    forbidden = {"_pick_current", "process_series", "overall_status"}

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("traffic_breakdown", "airport_directory"):
            called = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            assert not (called & forbidden), f"{node.name} yasaklı çağrı yapıyor: {called & forbidden}"
