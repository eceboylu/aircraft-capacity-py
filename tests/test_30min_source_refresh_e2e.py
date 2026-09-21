"""
ADIM 6D-2 (30 dk kaynak refresh E2E) - GERÇEK production zincirinin
kendisini doğrular:

    30 dk'da bir dış sistemden yeni veri
      -> ingestion (parse_source_a)
      -> Flight INSERT/UPDATE (refresh_flights)
      -> prediction OTOMATİK yeniden hesaplama (run_predictions,
         pipeline.run()'ın KENDİSİ tarafından, manuel çağrı YOK)
      -> QueuePrediction UPDATE
      -> API (airport_predictions)
      -> frontend'in bir sonraki polling'de göreceği veri

Bu dosyada TEK bir fonksiyon çağrılıyor: `pipeline.run()` - AYNI
fonksiyon `python -m app.queue.pipeline`'ın 30 dakikada bir çağırdığı
fonksiyonun ta kendisi. `engine.run_predictions()` burada HİÇ DOĞRUDAN
çağrılmıyor - pipeline ne yapıyorsa (ensure_airports -> ensure_capacity
-> load_flight_rows -> refresh_flights -> run_predictions ->
prune_stale_predictions) SADECE o test ediliyor.

`database.sqlite` bu dosyada hiç açılmaz: `pipeline.init_db`/
`get_session` bellek içi bir engine'e monkeypatch edilir - AYNI, daha
önce onaylanmış desen (`tests/test_pipeline_observability.py`).
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.queue.pipeline as pipeline_module
from app.models import Base
from app.queue.api import airport_predictions
from app.queue.constants import (
    EVENT_AIRCRAFT_CHANGED,
    EVENT_CANCELLED,
    RISK_CRITICAL,
)
from app.queue.models import Flight, FlightEvent, QueuePrediction

DAY = datetime(2026, 9, 15)  # sistem "bugün"ü 2026-09-16 - bu tarih HER ZAMAN geçmişte kalır


def hour_start(hour: int) -> datetime:
    return DAY.replace(hour=hour, minute=0, second=0, microsecond=0)


# ADIM (Operational-Day Scope): `run_predictions()` artık havalimanının
# GERÇEK yerel timezone'una göre "bugün" filtresi uyguluyor (bkz.
# app/queue/domain/operational_day.py). Bu dosyadaki gerçek pipeline
# (`pipeline_module.run()`) çağrıları artık `now`'ı AÇIKÇA vermeli -
# aksi halde `domain_now()` (GERÇEK duvar saati) kullanılır ve DAY
# (2026-09-15) hiçbir zaman "bugün" olarak seçilmez. 20:00 UTC hem tüm
# test pencerelerinin (en geç window_start=14, window_end=15:00)
# KAPANMIŞ olmasını hem de IST/ADB'nin (Europe/Istanbul, UTC+3) yerel
# tarihinin hâlâ AYNI gün (15 Eylül 23:00 yerel) olmasını garanti eder.
PIPELINE_NOW = hour_start(20)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def rec(flight_iata, number, airline, dep_iata, arr_iata, dep_dt, arr_dt, aircraft_icao, status="scheduled"):
    return {
        "flight_iata": flight_iata, "flight_number": number, "airline_iata": airline,
        "dep_iata": dep_iata, "arr_iata": arr_iata,
        "dep_time_utc": _fmt(dep_dt), "arr_time_utc": _fmt(arr_dt),
        "status": status, "aircraft_icao": aircraft_icao,
    }


def dom_dep(number, window, status="scheduled", airport="IST", partner="ESB"):
    """
    Domestic kalkış: 70dk süre. ADIM (Airport Queue Model V2 - sabit
    -120dk offset): dep=window+125dk -> effective_time=window+5dk.
    """
    dep_dt = window + timedelta(minutes=125)
    return rec(f"TK{number}", number, "TK", airport, partner, dep_dt, dep_dt + timedelta(minutes=70), "A320", status)


def intl_dep(number, window, status="scheduled", airport="IST", partner="CDG"):
    """
    Uluslararası kalkış: 210dk süre. ADIM (Airport Queue Model V2 - sabit
    -120dk offset): dep=window+125dk -> effective_time=window+5dk (domestic
    ile AYNI sabit formül, lokasyon/süreden bağımsız).
    """
    dep_dt = window + timedelta(minutes=125)
    return rec(f"TK{number}", number, "TK", airport, partner, dep_dt, dep_dt + timedelta(minutes=210), "A320", status)


def intl_arr(number, window, aircraft="A320", airport="IST", partner="CDG", arr_offset_min=-10):
    """Uluslararası varış: sabit 15dk buffer -> arr=window-10dk -> effective=window+5dk."""
    arr_dt = window + timedelta(minutes=arr_offset_min)
    dep_dt = arr_dt - timedelta(minutes=180)
    return rec(f"TK{number}", number, "TK", partner, airport, dep_dt, arr_dt, aircraft)


@pytest.fixture
def isolated_pipeline(monkeypatch):
    """
    `tests/test_pipeline_observability.py::isolated_pipeline` ile AYNI
    desen: `pipeline.init_db`/`get_session` bellek içi bir engine'e
    yönlendirilir, gerçek `database.sqlite`'a HİÇ dokunulmaz.
    """
    test_engine = create_engine("sqlite://")
    TestSessionLocal = sessionmaker(bind=test_engine)

    monkeypatch.setattr(
        pipeline_module, "init_db",
        lambda drop_first=False: Base.metadata.create_all(test_engine),
    )
    monkeypatch.setattr(pipeline_module, "get_session", lambda: TestSessionLocal())
    return TestSessionLocal


# ------------------------------------------------------------------
# R0 - ilk 30 dakikalık çekim
# ------------------------------------------------------------------
#
#   D1  domestic departure  IST->ESB   08:00   sabit (R0=R1, dup testi)
#   D2  intl departure      IST->CDG   09:00   R0=scheduled, R1=cancelled
#   A1  intl arrival        CDG->IST   10:00   R0=A320, R1=B77W (aircraft change)
#   A2  intl arrival        CDG->IST   11:00   R0 penceresi 11:00, R1'de
#                                               arr_estimated ile 12:00'a kayar
#   A3  intl arrival        CDG->IST   14:00   sabit baseline (1x A320)
#   R1'de A3'ün YANINA 4x B77W eklenir (passport surge)
#   C1  (ADB, kontrol havalimanı) domestic departure  08:00  sabit

def r0_sources():
    dep = [
        dom_dep("101", hour_start(8)),
        intl_dep("102", hour_start(9)),
        dom_dep("201", hour_start(8), airport="ADB", partner="ESB"),
    ]
    arr = [
        intl_arr("103", hour_start(10), aircraft="A320"),
        intl_arr("104", hour_start(11), aircraft="A320"),
        intl_arr("105", hour_start(14), aircraft="A320"),
    ]

    def source_a(direction):
        return dep if direction == "departure" else arr

    def source_b():
        return []

    return source_a, source_b


def r1_sources():
    dep = [
        dom_dep("101", hour_start(8)),                              # DEĞİŞMEDİ
        intl_dep("102", hour_start(9), status="cancelled"),          # İPTAL
        dom_dep("201", hour_start(8), airport="ADB", partner="ESB"),  # DEĞİŞMEDİ (kontrol)
    ]
    arr = [
        intl_arr("103", hour_start(10), aircraft="B77W"),   # UÇAK DEĞİŞTİ (A320->B77W)
        # A2 artık 12:00'a kayıyor (arr_estimated_utc ile, PENCERE GÖÇÜ)
        {**intl_arr("104", hour_start(11)), "arr_estimated_utc": _fmt(hour_start(12) - timedelta(minutes=10))},
        intl_arr("105", hour_start(14), aircraft="A320"),   # DEĞİŞMEDİ
        intl_arr("106", hour_start(14), aircraft="B77W"),   # YENİ - surge
        intl_arr("107", hour_start(14), aircraft="B77W"),
        intl_arr("108", hour_start(14), aircraft="B77W"),
        intl_arr("109", hour_start(14), aircraft="B77W"),
    ]

    def source_a(direction):
        return dep if direction == "departure" else arr

    def source_b():
        return []

    return source_a, source_b


@pytest.fixture
def r0_r1(isolated_pipeline):
    source_a_r0, source_b_r0 = r0_sources()
    r0_summary = pipeline_module.run(source_a=source_a_r0, source_b=source_b_r0, now=PIPELINE_NOW)

    source_a_r1, source_b_r1 = r1_sources()
    r1_summary = pipeline_module.run(source_a=source_a_r1, source_b=source_b_r1, now=PIPELINE_NOW)

    return {
        "SessionLocal": isolated_pipeline,
        "r0_summary": r0_summary,
        "r1_summary": r1_summary,
    }


def _session(ctx):
    return ctx["SessionLocal"]()


# ==================================================================
# 1) Duplicate Flight oluşmuyor
# ==================================================================

def test_no_duplicate_flights_after_r1(r0_r1):
    session = _session(r0_r1)
    try:
        total = session.scalar(select(func.count()).select_from(Flight))
        # D1(IST) + D2(IST) + A1 + A2 + A3 + C1(ADB) = 6 BENZERSİZ uçuş.
        # R1'de A2'nin arr_estimated değişmesi/A1'in aircraft'ı değişmesi
        # YENİ satır AÇMAZ - aynı flight_key UPSERT edilir. R1'de eklenen
        # 4 yeni surge uçuşu (106-109) + 6 = 10.
        assert total == 10
    finally:
        session.close()


def test_r1_summary_reports_upserts_not_pure_inserts(r0_r1):
    # R1 turu, D1/D2/A1/A2/A3/C1 için INSERTED değil UPDATED üretmeli
    # (aynı flight_key zaten R0'da vardı); sadece 4 yeni surge uçuşu
    # INSERTED olmalı.
    assert r0_r1["r1_summary"]["inserted"] == 4
    assert r0_r1["r1_summary"]["updated"] == 6


# ==================================================================
# 2) Cancellation doğru
# ==================================================================

def test_cancellation_is_applied_to_the_same_row_not_a_new_one(r0_r1):
    session = _session(r0_r1)
    try:
        rows = session.execute(
            select(Flight).where(Flight.flight_iata == "TK102")
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "cancelled"

        events = session.execute(
            select(FlightEvent).where(
                FlightEvent.flight_key == rows[0].flight_key,
                FlightEvent.event_type == EVENT_CANCELLED,
            )
        ).scalars().all()
        assert len(events) == 1
    finally:
        session.close()


def test_cancelled_flight_excluded_from_security_demand_after_r1(r0_r1):
    result_r1 = airport_predictions(_session(r0_r1), "IST", now=PIPELINE_NOW)
    w = _window(result_r1["international_security"], hour_start(9))
    # Pencere hâlâ VAR (bucket, iptal/yönlendirme nedenlerinin
    # bağlanabilmesi için cancelled uçuşu da içeriyor - bkz.
    # `_bucket_flights_by_window` docstring'i) ama TALEP sıfır: cancelled
    # uçuş demand'e hiç girmiyor.
    assert w is not None
    assert w["flight_count"] == 0
    assert w["expected_passengers"] == 0


# ==================================================================
# 3) Aircraft change ICAO kapasitesine yansıyor
# ==================================================================

def test_aircraft_change_reflected_in_flight_row_and_event(r0_r1):
    session = _session(r0_r1)
    try:
        flight = session.execute(
            select(Flight).where(Flight.flight_iata == "TK103")
        ).scalar_one()
        assert flight.aircraft_icao == "B77W"

        events = session.execute(
            select(FlightEvent).where(
                FlightEvent.flight_key == flight.flight_key,
                FlightEvent.event_type == EVENT_AIRCRAFT_CHANGED,
            )
        ).scalars().all()
        assert len(events) == 1
        assert events[0].old_value == "A320"
        assert events[0].new_value == "B77W"
    finally:
        session.close()


def test_aircraft_change_increases_passport_demand_at_its_window(r0_r1):
    session = _session(r0_r1)
    # R0'ın kendi anlık görüntüsünü almak için motoru elle ÇAĞIRMIYORUZ -
    # R1 aynı pencereyi UPSERT ederken A320(180)'i B77W(350)'ye çevirdiği
    # için pencere talebinin artmış olması gerekiyor; bu R1 sonrası DB'de
    # zaten kalıcı olan sonuçtan doğrudan okunuyor.
    w_r1 = _window(airport_predictions(session, "IST", now=PIPELINE_NOW)["international_passport"], hour_start(10))
    assert w_r1["expected_passengers"] == _real_capacity(session, "B77W")
    assert w_r1["expected_passengers"] > _real_capacity(session, "A320")
    session.close()


# ==================================================================
# 4) estimated/actual time değişikliği doğru 60dk pencereye taşınıyor
# ==================================================================

def test_estimated_time_change_moves_flight_to_the_new_60min_window(r0_r1):
    session = _session(r0_r1)
    try:
        result = airport_predictions(session, "IST", now=PIPELINE_NOW)
        w11 = _window(result["international_passport"], hour_start(11))
        w12 = _window(result["international_passport"], hour_start(12))
        # A2 artık 11:00'da değil - o pencerede A2'nin talebi YOK.
        assert w11 is None
        # 12:00 penceresi A2'nin talebini (A320) taşıyor.
        assert w12 is not None
        assert w12["expected_passengers"] == _real_capacity(session, "A320")
    finally:
        session.close()


# ==================================================================
# 5) Passport risk/utilization/wait R0->R1 değişiyor
# ==================================================================

def test_passport_risk_utilization_wait_change_between_r0_and_r1(r0_r1):
    session = _session(r0_r1)
    try:
        result_r1 = airport_predictions(session, "IST", now=PIPELINE_NOW)
        w_r1 = _window(result_r1["international_passport"], hour_start(14))
        assert w_r1["risk"] == RISK_CRITICAL
        assert w_r1["utilization"] > 1.0
        assert w_r1["estimated_wait_minutes"] > 0
    finally:
        session.close()


# ==================================================================
# 6) 4 grafik API'de R1'i görüyor + 7) overall güncelleniyor
# ==================================================================

def test_all_four_graphs_reflect_r1_in_the_api(r0_r1):
    session = _session(r0_r1)
    try:
        result = airport_predictions(session, "IST", now=PIPELINE_NOW)
        for key in ("overall", "domestic_security", "international_security", "international_passport"):
            assert key in result

        overall_14 = _window(result["overall"], hour_start(14))
        assert overall_14["risk"] == RISK_CRITICAL

        # ADIM (Overall Graph Legacy Passport Bug Fix): Overall artık
        # `domestic_security` + `international_security` + `passport_
        # departure` + `passport_arrival`'dan (event-driven, gerçek
        # ServiceEvent wait'i) türüyor - eski, legacy `international_
        # passport` (= birleşik PROCESS_PASSPORT, hâlâ 8-server Erlang-C
        # formülü) ile eşitlik ARTIK beklenmiyor (14:00'te bu ikisi
        # GERÇEKTEN farklı: 39.5 vs 241.9 - legacy 8-server referansı
        # bu saatteki gerçek talebi olduğundan çok daha kötü gösteriyor).
        #
        # Bu saatte TEK gerçek katkı sağlayan süreç international_arrival
        # (= passport_arrival) - overall_14'ün wait'i BUNUNLA birebir
        # eşleşmeli (domestic_security/international_security/
        # international_departure bu saatte hiç pencereye sahip değil).
        contributing = _window(result["international_arrival"], hour_start(14))
        assert contributing is not None
        assert overall_14["estimated_wait_minutes"] == contributing["estimated_wait_minutes"]

        legacy = _window(result["international_passport"], hour_start(14))
        assert overall_14["estimated_wait_minutes"] != legacy["estimated_wait_minutes"]
    finally:
        session.close()


# ==================================================================
# 8) Aynı R1 tekrar çalıştırılırsa idempotent
# ==================================================================

def test_running_r1_again_is_idempotent(r0_r1):
    """
    `r0_r1` fixture'ı R0 sonra R1'i ZATEN bir kez çalıştırdı. R1'in
    kendi ilk çalışması, o turda YENİ görünen pencereler (ör. 12:00)
    için `record_baseline_observations()` ile bir kerelik bir
    "baseline ısınması" tetikler (bkz. baseline.py) - bu YENİ bir
    örnektir, `confidence_score()`'un `historical_baseline_available`
    girdisini değiştirebilir. Bu GERÇEK ve KASITLI bir davranış (sistem
    aynı pencereyi ikinci kez gördüğünde daha güvenli hale gelir),
    hata DEĞİLDİR - bu yüzden idempotency aynı payload'ın İKİ KEZ ÜST
    ÜSTE (ısınma zaten tamamlanmışken) çalıştırılmasıyla ölçülür: R1'
    (ısınmayı zaten içeren R1'in kendisi) ile R1'' (bir sonraki
    identik çağrı) birebir AYNI olmalı - `BaselineObservation` deferi
    idempotent olduğu için R1'' hiçbir yeni örnek EKLEMEZ.
    """
    source_a_r1, source_b_r1 = r1_sources()

    pipeline_module.run(source_a=source_a_r1, source_b=source_b_r1, now=PIPELINE_NOW)
    session_1 = _session(r0_r1)
    run_1 = airport_predictions(session_1, "IST")
    flight_count_1 = session_1.scalar(select(func.count()).select_from(Flight))
    session_1.close()

    pipeline_module.run(source_a=source_a_r1, source_b=source_b_r1, now=PIPELINE_NOW)
    session_2 = _session(r0_r1)
    run_2 = airport_predictions(session_2, "IST")
    flight_count_2 = session_2.scalar(select(func.count()).select_from(Flight))
    session_2.close()

    assert flight_count_2 == flight_count_1
    for key in ("overall", "domestic_security", "international_security", "international_passport"):
        assert _windows_without_timestamp(run_1[key]) == _windows_without_timestamp(run_2[key])


def test_baseline_ledger_prevents_the_same_window_from_being_recorded_twice(r0_r1):
    """
    `BaselineObservation` (airport, PROCESS, window_start) üzerinde
    UNIQUE - R1'in aynı payload'ı N kere çalıştırılsa bile 12:00
    penceresi için HER SÜREÇTE sadece TEK bir gözlem defterde kalmalı
    (yukarıdaki "baseline ısınması" notunun idempotent bir tek-seferlik
    olay olduğunun kanıtı).

    ADIM (4-Graph API Contract): 12:00 penceresini besleyen A2 varış-
    kökenli bir uçuş (intl arrival) - artık birleşik `passport`'un
    YANINDA (`_passport_cohort_breakdown()`, engine.py) `passport_arr`
    süreci de KENDİ ayrı `BaselineObservation` defterini besliyor (Bölüm
    17: AYNI fiziksel talebin cohort/kaynak bazlı raporlama görünümü -
    double-count DEĞİL, iki AYRI process_id). Bu yüzden 12:00 için
    TOPLAM satır sayısı artık 2'dir (1 `passport` + 1 `passport_arr`) -
    ama HER SÜRECİN KENDİ İÇİNDE hâlâ TAM OLARAK 1 (idempotency
    BOZULMADI - doğrudan DB'den doğrulandı, bkz. rapor). Eski `count==1`
    (process filtrelenmeden) varsayımı bu YENİ, doğru süreç ayrımından
    ÖNCE yazılmıştı ve artık iki AYRI, GERÇEK gözlemi "duplicate" gibi
    yanlış yorumluyordu - production'da hiçbir duplicate YOK.
    """
    from app.queue.models import BaselineObservation

    source_a_r1, source_b_r1 = r1_sources()
    pipeline_module.run(source_a=source_a_r1, source_b=source_b_r1, now=PIPELINE_NOW)
    pipeline_module.run(source_a=source_a_r1, source_b=source_b_r1, now=PIPELINE_NOW)

    session = _session(r0_r1)
    try:
        rows = session.execute(
            select(BaselineObservation).where(
                BaselineObservation.airport_iata == "IST",
                BaselineObservation.window_start == hour_start(12),
            )
        ).scalars().all()

        processes = sorted(r.process for r in rows)
        assert processes == ["passport", "passport_arr"]

        for process in processes:
            per_process_count = session.scalar(
                select(func.count()).select_from(BaselineObservation).where(
                    BaselineObservation.airport_iata == "IST",
                    BaselineObservation.process == process,
                    BaselineObservation.window_start == hour_start(12),
                )
            )
            assert per_process_count == 1, (
                f"{process} süreci için {per_process_count} gözlem - "
                "idempotency BOZULDU (aynı payload iki kez çalıştırıldı, "
                "ikinci çalıştırma YENİ bir satır AÇMAMALIYDI)"
            )
    finally:
        session.close()


# ==================================================================
# 9) IST değişikliği başka airport'u etkilemiyor
# ==================================================================

def test_ist_changes_do_not_affect_control_airport_adb(r0_r1):
    session = _session(r0_r1)
    try:
        result = airport_predictions(session, "IST", now=PIPELINE_NOW)
        adb_result = airport_predictions(session, "ADB", now=PIPELINE_NOW)
        assert result["airport"] != adb_result["airport"]

        adb_window = _window(adb_result["domestic_security"], hour_start(8))
        assert adb_window is not None
        assert adb_window["flight_count"] == 1

        adb_flight_count = session.scalar(
            select(func.count()).select_from(Flight).where(Flight.airport_iata == "ADB")
        )
        assert adb_flight_count == 1
    finally:
        session.close()


# ==================================================================
# 10) frontend/source API ekstra çağırmıyor + 11) polling sadece DB okur
# ==================================================================

def test_server_predictions_endpoint_only_reads_the_api_layer():
    import inspect
    from app.web import server

    source = inspect.getsource(server)
    for forbidden in (
        "run_predictions(", "predict_airport(", "predict_window(",
        "pipeline.run", "AircraftCapacityService(",
    ):
        assert forbidden not in source
    assert "self._send_json(airport_predictions(session, iata))" in source


def test_frontend_polling_only_refetches_the_same_predictions_endpoint():
    import os
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "web", "static", "index.html",
    )
    with open(path, "r", encoding="utf-8") as fh:
        html = fh.read()
    # ADIM (Frontend 4-Graph Contract) - Bölüm 26/52: source refresh
    # artık ~5 dakika (30 dakika DEĞİL) - queue window/prediction
    # window/service interval'dan bağımsız, SADECE dış veri yenilenme
    # sıklığı.
    assert "var POLL_MS = 5 * 60 * 1000;" in html
    assert "setInterval(loadAll, POLL_MS)" in html
    # loadAll SADECE mevcut REST uçlarını (directory + predictions)
    # çağırıyor - başka bir kaynağa (AirLabs vb.) hiç istek yok.
    load_all = html.split("function loadAll() {", 1)[1].split("\n  }", 1)[0]
    assert "/api/airports/directory" in load_all
    assert "/predictions" in load_all
    assert "airlabs" not in load_all.lower()


# ------------------------------------------------------------------

def _window(series: dict, window_start: datetime) -> dict | None:
    for w in series["windows"]:
        if w["window_start"] == window_start.isoformat():
            return w
    return None


def _windows_without_timestamp(series: dict) -> list[dict]:
    """
    `calculated_at` her UPSERT'te (değer aynı kalsa bile) YENİLENİR -
    bu KASITLI (satırın "en son ne zaman hesaplandığı" bilgisi); bu
    yüzden idempotency karşılaştırması bu alanı GÖZ ARDI eder, geri
    kalan HER alan (risk/wait/flight_count/...) birebir aynı kalmalı.
    """
    return [{k: v for k, v in w.items() if k != "calculated_at"} for w in series["windows"]]


def _real_capacity(session, icao: str) -> int:
    """Gerçek `AircraftCapacityService` üzerinden - hiçbir kapasite sayısı burada UYDURULMAZ."""
    from app.service import AircraftCapacityService
    return AircraftCapacityService(session).resolve(icao, "TK").capacity
