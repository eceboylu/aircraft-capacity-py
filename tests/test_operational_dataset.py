"""
ADIM 6A - Operasyonel T0/T1/T2 dataset testleri.

Bu ADIM'ın kuralı: "assertion'ları henüz doğrulamaya zorlama, önce
gerçek motorun tepkisini ÖLÇ." Bu yüzden buradaki testler ÇOĞUNLUKLA
yapısal/bütünlük testleridir (A-G, K) - gerçek AirLabs zincirinden
geçiyor mu, havalimanı/süreç ayrımı bozulmuyor mu, duplicate/idempotency
korunuyor mu. Overall aggregation (H, I, J) ayrı bir presentation
katmanı olduğu için KENDİ formülüyle test edilir - bu, security/
passport risk hesaplarını DEĞİŞTİRMEZ.

Sayısal risk/wait/utilization DEĞERLERİ üzerine "olmalı" iddiaları
(örn. "T1'de security kesin HIGH olmalı") BİLEREK buraya YAZILMADI -
bunlar ADIM 6B/6C'nin konusu. Gerçek ölçüm sonuçları raporda.
"""

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.queue.pipeline as pipeline_module
from app.models import Base
from app.queue.api import (
    RISK_TO_UI_LABEL,
    airport_predictions,
    overall_status,
    ui_label_for_risk,
)
from app.queue.constants import (
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
)
from app.queue.ingestion.airports_import import import_airports
from app.queue.ingestion import airlabs_client
from app.queue.models import Flight, FlightEvent, QueuePrediction
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset

from . import airlabs_operational_source as ops

REAL_AIRPORTS_SQL = __import__("os").path.join(
    __import__("os").path.dirname(__file__), "..", "data", "flight_airports.sql"
)


# --------------------------------------------------------------------
# Ortak kurulum: T0 -> T1 -> T2, GERÇEK pipeline.run() zinciriyle,
# bellek içi izole bir veritabanına karşı. module-scope: pahalı kurulum
# (3 tam pipeline turu) tüm testler arasında BİR KEZ çalışır.
# --------------------------------------------------------------------

@pytest.fixture(scope="module")
def operational_state():
    mp = pytest.MonkeyPatch()
    try:
        source = ops.install(mp, round_="t0")

        test_engine = create_engine("sqlite://")
        TestSessionLocal = sessionmaker(bind=test_engine)
        mp.setattr(
            pipeline_module, "init_db",
            lambda drop_first=False: Base.metadata.create_all(test_engine),
        )
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

        summaries = {}
        for round_ in ops.ROUNDS:
            source.round = round_
            summaries[round_] = pipeline_module.run(source_a=source_a, source_b=source_b)

        yield {
            "session_factory": TestSessionLocal,
            "summaries": summaries,
        }
    finally:
        mp.undo()


def _session(state):
    return state["session_factory"]()


# --------------------------------------------------------------------
# TEST A - fixture'lar production parser'dan geçiyor
# --------------------------------------------------------------------

def test_a_all_rounds_parsed_without_errors(operational_state):
    for round_, summary in operational_state["summaries"].items():
        assert summary["flights_parsed"] > 0, round_
        assert summary["failed_airports"] == [], round_


# --------------------------------------------------------------------
# TEST B - fixture DOĞRUDAN DB'ye yazılmadı, gerçek client/parser
# zincirinden geçti (dolaylı kanıt: Flight satırları GERÇEK
# `refresh_flights`'ın ürettiği tüm alanları taşıyor + aircraft_icao
# Kaynak A'nın kendi alanından enrichment ile geldi).
# --------------------------------------------------------------------

def test_b_flights_carry_fields_only_the_real_parser_produces(operational_state):
    session = _session(operational_state)
    flight = session.execute(
        select(Flight).where(Flight.flight_key.like("TK_201_%"))
    ).scalars().first()
    session.close()

    assert flight is not None
    assert flight.aircraft_match_found is True   # Kaynak A'nın kendi alanından
    assert flight.dep_iata == "IST"
    assert flight.location in ("domestic", "international")


# --------------------------------------------------------------------
# TEST C - IST/SAW/ADB birbirine karışmıyor
# --------------------------------------------------------------------

def test_c_airports_do_not_mix(operational_state):
    session = _session(operational_state)
    counts = dict(session.execute(
        select(Flight.airport_iata, func.count())
        .group_by(Flight.airport_iata)
    ).all())
    session.close()

    assert set(counts) == {"IST", "SAW", "ADB"}
    assert counts["IST"] > counts["SAW"] > counts["ADB"]


# --------------------------------------------------------------------
# TEST D/E - T0->T1, T1->T2 refresh duplicate ÜRETMİYOR
# --------------------------------------------------------------------

def test_d_t0_to_t1_refresh_produces_no_duplicate_flights(operational_state):
    session = _session(operational_state)
    total_rows = session.scalar(select(func.count()).select_from(Flight))
    distinct_keys = session.scalar(select(func.count(func.distinct(Flight.flight_key))))
    session.close()

    assert total_rows == distinct_keys


def test_e_t1_to_t2_refresh_still_no_duplicates_and_grew_only_by_new_flights(operational_state):
    """
    T1'de eklenen surge/passport bank YENİ flight_key'lerdir (beklenen
    büyüme); T2 hiçbir YENİ flight_key EKLEMEZ (sadece mevcutları
    günceller) - toplam satır sayısı T1'den sonra SABİT kalmalı.
    """
    t1 = operational_state["summaries"]["t1"]
    t2 = operational_state["summaries"]["t2"]

    assert t1["inserted"] > 0     # surge/passport bank yeni flight_key'ler
    assert t2["inserted"] == 0    # T2 sadece güncelliyor, yeni flight_key YOK
    assert t2["updated"] > 0


# --------------------------------------------------------------------
# TEST F - Aircraft change aynı flight identity üzerinde
# UPDATE + FlightEvent olarak işleniyor
# --------------------------------------------------------------------

def test_f_aircraft_change_produces_single_event_on_same_flight_key(operational_state):
    session = _session(operational_state)
    flight = session.execute(
        select(Flight).where(Flight.flight_key.like("TK_201_%"))
    ).scalars().first()
    events = session.execute(
        select(FlightEvent).where(FlightEvent.flight_key == flight.flight_key)
    ).scalars().all()
    session.close()

    assert flight.aircraft_icao == "B772"   # T1/T2'nin son değeri
    change_events = [e for e in events if e.event_type == "AIRCRAFT_CHANGED"]
    assert len(change_events) == 1
    assert change_events[0].old_value == "A320"
    assert change_events[0].new_value == "B772"


# --------------------------------------------------------------------
# TEST G - Cancellation mevcut demand davranışını koruyor
# --------------------------------------------------------------------

def test_g_cancelled_flights_are_excluded_from_demand(operational_state):
    session = _session(operational_state)
    cancelled = session.execute(
        select(Flight).where(
            Flight.flight_key.like("TK_30%"),
            Flight.status == "cancelled",
        )
    ).scalars().all()
    session.close()

    cancelled_keys = {f.flight_key for f in cancelled}
    assert len(cancelled_keys) == 3   # TK300, TK304, TK309

    # Aynı iddia AŞAMA 4'ün kendi filtresiyle (flights_in_window) de
    # doğrulanabilir - burada sadece DB'ye YANSIYAN statünün doğru
    # olduğu kanıtlanıyor; talep hesabına girmediği ADIM 6D/6B'de
    # (pencere bazlı) ayrıca ölçülecek.


# --------------------------------------------------------------------
# TEST H - Overall aggregation (presentation-only)
# --------------------------------------------------------------------

@pytest.mark.parametrize("security_risk,passport_risk,expected", [
    (RISK_LOW, RISK_LOW, RISK_LOW),
    (RISK_MEDIUM, RISK_LOW, RISK_MEDIUM),
    (RISK_HIGH, RISK_MEDIUM, RISK_HIGH),
    (RISK_CRITICAL, RISK_LOW, RISK_CRITICAL),
    (RISK_UNKNOWN, RISK_UNKNOWN, RISK_UNKNOWN),
    (RISK_HIGH, RISK_UNKNOWN, RISK_HIGH),
    (RISK_UNKNOWN, RISK_HIGH, RISK_HIGH),
    (RISK_LOW, RISK_CRITICAL, RISK_CRITICAL),
    (None, None, None),
    (RISK_LOW, None, RISK_LOW),
    (None, RISK_MEDIUM, RISK_MEDIUM),
])
def test_h_overall_is_the_higher_severity_of_the_two_processes(security_risk, passport_risk, expected):
    assert overall_status(security_risk, passport_risk)["risk"] == expected


# --------------------------------------------------------------------
# TEST I - Overall UI mapping
# --------------------------------------------------------------------

@pytest.mark.parametrize("risk,label", [
    (RISK_LOW, "NORMAL"),
    (RISK_MEDIUM, "YOĞUN"),
    (RISK_HIGH, "ÇOK YOĞUN"),
    (RISK_CRITICAL, "ÇOK YOĞUN"),
    (RISK_UNKNOWN, "BİLİNMİYOR"),
])
def test_i_overall_ui_label_uses_the_same_single_mapping(risk, label):
    assert overall_status(risk, risk)["label"] == label
    assert RISK_TO_UI_LABEL[risk] == label   # tek mapping, iki yerde tanımlı DEĞİL


def test_i_overall_no_data_maps_to_none_label_not_normal():
    result = overall_status(None, None)
    assert result["risk"] is None
    assert result["label"] is None
    assert ui_label_for_risk(result["risk"]) is None


# --------------------------------------------------------------------
# TEST J - API yeni overall alanını okuyabiliyor
# --------------------------------------------------------------------

def test_j_api_response_carries_overall_alongside_existing_fields(operational_state):
    """
    ADIM G ile GÜNCELLENDİ: `overall` artık `{process, current, windows}`
    (diğer 3 grafikle AYNI şekil - click-to-detail için tam seri) -
    eski `{risk, label}` şekli DEĞİL. Eski `security`/`passport` alanları
    (ve şekilleri) KALDIRILMADI/DEĞİŞMEDİ - geriye dönük uyumlu.
    """
    session = _session(operational_state)
    result = airport_predictions(session, "IST")
    session.close()

    assert "overall" in result
    assert set(result["overall"]) == {"process", "current", "windows"}
    # Mevcut alanlar KALDIRILMADI/yeniden adlandırılmadı.
    assert "security" in result and "passport" in result
    assert set(result["security"]) == {"process", "current", "windows"}
    # ADIM G - yeni alanlar EKLENDİ.
    assert set(result["domestic_security"]) == {"process", "current", "windows"}
    assert set(result["international_security"]) == {"process", "current", "windows"}
    assert result["international_passport"] == result["passport"]


def test_j_overall_reflects_current_window_only_not_the_whole_series_max(operational_state):
    """
    §16 KRİTİK (ADIM G ile GÜNCELLENDİ): overall.current, artık
    domestic_security + international_security + passport'un AYNI
    (current'ın kendi) SAATİNDEKİ pencerelerinden türetilmeli - serideki
    geçmiş/gelecek MAKSİMUM riski DEĞİL.

    BUG-02 düzeltmesinden SONRA: her sürecin KENDİ ayrı seçtiği "current"
    (farklı saatlere düşebilir - bkz. `_pick_current`'ın geçmiş/gelecek
    fallback'i) artık birbirleriyle KARŞILAŞTIRILMIYOR; SADECE overall'ın
    KENDİ seçtiği saatteki (window_start eşleşen) süreç pencereleri
    karşılaştırılıyor. Bu yüzden burada da AYNI yöntemle (o saate ait
    pencere var mı) `expected_current` türetilmeli - her sürecin KENDİ
    (potansiyel olarak farklı saatlere düşen) `current`'ını DEĞİL.
    """
    session = _session(operational_state)
    result = airport_predictions(session, "IST")
    session.close()

    overall_current = result["overall"]["current"]

    def _risk_at(section, window_start):
        for w in section["windows"]:
            if w["window_start"] == window_start:
                return w["risk"]
        return None

    if overall_current is None:
        expected_current = {"risk": None, "label": None}
    else:
        window_start = overall_current["window_start"]
        expected_current = overall_status(
            _risk_at(result["domestic_security"], window_start),
            _risk_at(result["international_security"], window_start),
            _risk_at(result["passport"], window_start),
        )
    actual_current = (
        {"risk": result["overall"]["current"]["risk"], "label": result["overall"]["current"]["risk_label"]}
        if result["overall"]["current"] else {"risk": None, "label": None}
    )
    assert actual_current == expected_current


# --------------------------------------------------------------------
# TEST K - mevcut prediction suite'i bu ADIM'da BOZULMADI
# (gerçek doğrulama full pytest ile yapılır, bkz. rapor)
# --------------------------------------------------------------------

def test_k_engine_math_modules_not_imported_by_fixture_generator():
    """Üretici script yoğunluk/kapasite hesabı İMPORT ETMİYOR - sadece uçuş verisi üretiyor."""
    import tests.fixtures.airlabs_operational.generate as gen

    source = open(gen.__file__, encoding="utf-8").read()
    import_lines = [
        line for line in source.splitlines()
        if line.strip().startswith("from ") or line.strip().startswith("import ")
    ]
    forbidden = ("app.queue.engine", "app.queue.core", "app.queue.domain", "app.service")
    for line in import_lines:
        for name in forbidden:
            assert name not in line, f"üretici motor kodunu import ediyor: {line}"


# --------------------------------------------------------------------
# ADIM 6A-UI - "current" artık max(window_start) DEĞİL (regresyon).
#
# ADIM 6A raporunda bulunan gerçek hata: tam-günlük fixture'da
# `current` günün EN SON penceresini (akşam, surge'den habersiz)
# seçiyordu. Bu testler, `now`'ı DETERMİNİSTİK olarak surge'ün
# içine enjekte ederek düzeltmenin gerçekten çalıştığını kanıtlar -
# gerçek duvar saatine (bu sandbox'ta akşam, yani fixture'ın TÜMÜNDEN
# sonra) güvenilerek YANLIŞLIKLA "eski davranışla aynı sonucu"
# üretmeyi ÖNLER.
# --------------------------------------------------------------------

def test_current_is_not_the_last_window_of_the_day_anymore(operational_state):
    """
    T1'de IST security'nin GERÇEK tepkisi (ADIM 6A'da doğrudan sorguyla
    ölçülmüştü): 08:15 penceresi baseline_ratio=3.43, risk=CRITICAL.
    `now=08:20` ile `current` bu pencereyi seçmeli - günün son
    (ilgisiz, akşam) penceresini DEĞİL.
    """
    session = _session(operational_state)
    now = datetime(2026, 9, 15, 8, 20)

    # T1 turunun sonundaki DB durumunu görmek için state zaten T0->T1->T2
    # sırayla çalıştı (module-scope fixture); son durum T2'dir, bu
    # yüzden burada T1'in kendi anlık görüntüsünü AYRI bir pipeline
    # turunda değil, T2 SONRASI DB'de T1'de eklenen pencerelerin hâlâ
    # var olduğunu (T2 onları GÜNCELLEDİ, SİLMEDİ) doğruluyoruz.
    result = airport_predictions(session, "IST", now=now)
    session.close()

    assert result["security"]["current"]["window_start"] == "2026-09-15T08:00:00"
    assert result["security"]["current"]["window_start"] != result["security"]["windows"][-1]["window_start"]


def test_current_selects_window_containing_now(operational_state):
    session = _session(operational_state)
    result = airport_predictions(session, "IST", now=datetime(2026, 9, 15, 8, 5))
    session.close()

    current = result["security"]["current"]
    assert current["window_start"] <= "2026-09-15T08:05:00" < current["window_end"]


def test_current_falls_back_to_most_recent_past_window_when_now_is_after_everything(operational_state):
    """
    `now`, TÜM pencerelerden sonraysa (kapsayan pencere yok) en yakın
    GEÇMİŞ pencere - yani günün SON penceresi - seçilmeli. Bu tek
    başına ESKİ `max(window_start)` davranışıyla AYNI sonucu verir,
    ama artık bir YAN ETKİ değil, `_pick_current`'ın üç kollu
    mantığının (kapsayan / geçmiş / gelecek) BİLİNÇLİ üçüncü kolu.
    """
    session = _session(operational_state)
    last_window_end = airport_predictions(
        session, "IST", now=datetime(2026, 9, 15, 23, 0)
    )["security"]["windows"][-1]["window_end"]
    session.close()

    session = _session(operational_state)
    # ADIM (Passport->Security zaman-kuplajı): security'nin serisi artık
    # passport backlog'u boşalırken en geç 24 saat (bkz.
    # `_passport_security_hourly_coupling`'in ufuk sınırı) daha ileri
    # uzayabiliyor - "tüm pencerelerden kesin sonra" artık `last_window_
    # end`'in KENDİSİNDEN türetilmeli, keyfi/sabit bir tarih DEĞİL.
    far_future = datetime.fromisoformat(last_window_end) + timedelta(days=2)
    result = airport_predictions(session, "IST", now=far_future)
    session.close()

    assert result["security"]["current"]["window_end"] == last_window_end


def test_current_falls_back_to_nearest_future_window_when_all_data_is_ahead(operational_state):
    session = _session(operational_state)
    far_past = datetime(2020, 1, 1, 0, 0)   # tüm pencerelerden kesin önce
    result = airport_predictions(session, "IST", now=far_past)
    session.close()

    first_window_start = result["security"]["windows"][0]["window_start"]
    assert result["security"]["current"]["window_start"] == first_window_start


def test_overall_at_08_20_reflects_08_15_window_not_the_evening(operational_state):
    """
    Kök neden testinin özeti: 08:20'de security/passport/overall'ın
    08:15 penceresinden (surge bölgesi) geldiğini kanıtlar - günün
    sonundaki ilgisiz bir pencereden DEĞİL.

    `operational_state` T0->T1->T2 sırasıyla çalıştığı için DB burada
    ARTIK T2 (recovery) durumundadır - T1'in ADIM 6A raporundaki
    CRITICAL anlık görüntüsü DEĞİL, T2'nin dağılmış/recovery değeri
    beklenir (bkz. ADIM 6A raporu bölüm I: T2 08:15 = 2 uçuş, risk=LOW).
    Bu yüzden burada sabit bir risk DEĞERİ değil, DOĞRU PENCERENİN
    seçildiği ve overall'ın o pencereyle TUTARLI olduğu doğrulanıyor.
    """
    session = _session(operational_state)
    result = airport_predictions(session, "IST", now=datetime(2026, 9, 15, 8, 20))
    session.close()

    assert result["security"]["current"]["window_start"] == "2026-09-15T08:00:00"
    assert result["security"]["current"]["window_start"] != result["security"]["windows"][-1]["window_start"]

    # ADIM G: overall artık domestic_security/international_security/passport'tan türer.
    expected_overall = overall_status(
        result["domestic_security"]["current"]["risk"] if result["domestic_security"]["current"] else None,
        result["international_security"]["current"]["risk"] if result["international_security"]["current"] else None,
        result["passport"]["current"]["risk"] if result["passport"]["current"] else None,
    )
    actual_overall = (
        {"risk": result["overall"]["current"]["risk"], "label": result["overall"]["current"]["risk_label"]}
        if result["overall"]["current"] else {"risk": None, "label": None}
    )
    assert actual_overall == expected_overall


# --------------------------------------------------------------------
# Yeni havalimanı otomatik eklenir - hardcoded liste YOK (regresyon,
# ADIM 6A-UI §13). `tracked_airports()` DOĞRUDAN QueuePrediction'dan
# okur; buraya elle bir satır eklemek YETERLİ - kod değişikliği YOK.
# --------------------------------------------------------------------

def test_new_airport_appears_without_any_code_change(operational_state):
    from app.queue.api import tracked_airports
    from app.queue.models import QueuePrediction

    session = _session(operational_state)
    before = tracked_airports(session)
    assert "XYZ" not in before

    session.add(QueuePrediction(
        airport_iata="XYZ", process="security",
        window_start=datetime(2026, 9, 15, 8, 0), window_end=datetime(2026, 9, 15, 8, 15),
        flight_count=1, expected_passengers=100, risk="LOW", reasons="[]", confidence=0.5,
    ))
    session.commit()

    after = tracked_airports(session)
    session.close()

    assert "XYZ" in after
    assert set(after) - set(before) == {"XYZ"}
