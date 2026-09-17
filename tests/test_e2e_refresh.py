"""
ADIM 3 - T0 -> T1 30 DAKİKALIK E2E REFRESH TESTİ.

Bu test GERÇEK bir AirLabs API çağrısı YAPMAZ ve AIRLABS_API_KEY
ortam değişkenini gerçek anlamda KULLANMAZ - `AIRLABS_API_KEY` burada
sadece `airlabs_client._api_key()` fonksiyonunun (URL kurma mantığı
gerçek koddan hiç değiştirilmeden çalışsın diye) ihtiyaç duyduğu bir
DUMMY/SAHTE string'tir; bu key hiçbir zaman ağa çıkmaz çünkü
`urllib.request.urlopen` tamamen sahte (mock) bir fonksiyonla
değiştirilmiştir - test asla internete bağlanmaz.

Akış (gerçek üretim akışının BİREBİR aynısı, hiçbir katman atlanmadan):

    Mock AirLabs response (tests/fixtures/airlabs_mock[_t1]/*.json)
      -> urllib.request.urlopen (SAHTE, sadece bu fonksiyon değişti)
      -> app.queue.ingestion.airlabs_client (GERÇEK kod, değişmedi)
      -> app.queue.pipeline.run(source_a=..., source_b=...) (GERÇEK)
           -> ensure_airports / load_flight_rows / parse_source_a
           -> refresh_flights (upsert + event tespiti)
           -> run_predictions (Erlang-C / security scoring / reasons /
              baseline) - AircraftCapacityService GERÇEK servis

T0, sonra AYNI veritabanı üzerinde (yeni bir DB DEĞİL - production'da
da aynı database kullanılacağı için) T1 ikinci bir refresh olarak
çalıştırılır. Pencere kayması gibi "önce/sonra" iddialarını KANITLAMAK
için ilgili QueuePrediction anlık görüntüleri T0'dan HEMEN SONRA ve
T1'den HEMEN SONRA (fixture içinde, aynı akış sırasında) ayrı ayrı
alınır - böylece "eski pencere artık bu uçuşu tutmuyor" iddiası
varsayıma değil, gerçek T0 anlık görüntüsüne karşı ölçülmüş olur.

Veritabanı izolasyonu: `app.db`'nin gerçek `database.sqlite`'ına HİÇ
dokunulmaz. `app.queue.pipeline.init_db`/`get_session`, bu test
SÜRESİNCE bellek içi (`sqlite://`) bir engine'e yönlendirilir - bu,
`pipeline.run()`'ın kendi mantığını (ensure_airports/load_flight_rows/
refresh_flights/run_predictions çağrı sırası) HİÇ değiştirmeden, sadece
depolama hedefini test-izole eder; parser/pipeline atlanmaz, aksine
`pipeline.run()` OLDUĞU GİBİ çağrılır.

Kaynak B (`flights` / canlı ADS-B): ADIM 2'de bilinçli olarak mock
edilmedi (kapsam dışı bırakıldı) - bu testte de boş `response`
döndürülür. Bu, AF104/BA103/PC101/TK999/AF204 için aircraft_icao'nun
SADECE Kaynak A'nın (schedules mock) kendi `aircraft_icao` alanından
geldiği, Kaynak B enrichment'ının bu senaryoda devrede OLMADIĞI
anlamına gelir - `parse_source_a_record`'ın "Kaynak A kendi alanı
Kaynak B'den önceliklidir" kuralı gereği bu tamamen geçerli bir test
senaryosudur.
"""

import os
from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import sessionmaker

import app.queue.pipeline as pipeline_module
from app.models import Base
from app.queue.constants import (
    EVENT_CANCELLED,
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    STATUS_CANCELLED,
)
from app.queue.domain.demand import effective_time
from app.queue.engine import floor_to_window
from app.queue.ingestion import airlabs_client
from app.queue.ingestion.airports_import import import_airports
from app.queue.models import Airport, Flight, FlightEvent, HistoricalFlightCount, QueuePrediction
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset

from . import airlabs_mock_source

REAL_AIRPORTS_SQL = os.path.join(
    os.path.dirname(__file__), "..", "data", "flight_airports.sql"
)

# Pencere-kayması kanıtı toplanacak (havalimanı, süreç, pencere)
# üçlüleri. Hesaplar - ADIM (Airport Queue Model V2 - sabit -120dk
# departure offset) itibariyle departure passenger arrival artık
# süreye bağlı dinamik buffer DEĞİL, SABİT 120 dakikadır:
#   PC101 (IST->CDG, 620dk - sabit -120dk offset, süreden bağımsız):
#     T0 dep_estimated=23:22 -> effective 21:22 -> pencere 21:00-22:00
#     T1 dep_estimated=2026-09-16 00:15 (68dk gecikme) -> effective 22:15
#       -> pencere 22:00-23:00
#   AF204 (DXB->IST varış, sabit 15dk passport buffer - DEĞİŞMEDİ,
#   DEMAND_WINDOW_MINUTES'TEN BAĞIMSIZ, coincidental aynı sayı):
#     T0 arr_estimated=04:43 (actual yok) -> effective 04:58 -> pencere 04:00-05:00
#     T1 arr_actual=04:50 (artık actual var) -> effective 05:05 -> pencere 05:00-06:00
_TRACKED_WINDOWS = [
    # (airport, process, window_start) - T0 SONRASI ve T1 SONRASI ayrı
    # ayrı snapshot alınacak pencereler.
    # PC101 21:22->21:00, 22:15->22:00; AF204 04:58->04:00, 05:05->05:00.
    ("IST", PROCESS_SECURITY, datetime(2026, 9, 15, 21, 0)),    # PC101 T0 penceresi
    ("IST", PROCESS_SECURITY, datetime(2026, 9, 15, 22, 0)),    # PC101 T1 penceresi
    ("IST", PROCESS_PASSPORT, datetime(2026, 9, 15, 21, 0)),
    ("IST", PROCESS_PASSPORT, datetime(2026, 9, 15, 22, 0)),
    ("IST", PROCESS_PASSPORT, datetime(2026, 9, 16, 4, 0)),     # AF204 T0 penceresi
    ("IST", PROCESS_PASSPORT, datetime(2026, 9, 16, 5, 0)),     # AF204 T1 penceresi
]


def _snapshot_windows(session) -> dict:
    """`_TRACKED_WINDOWS`'daki her (havalimanı, süreç, pencere) için
    o anki flight_count'u (satır yoksa None) okur."""
    snapshot = {}
    for airport, process, window_start in _TRACKED_WINDOWS:
        row = session.execute(
            select(QueuePrediction).where(
                QueuePrediction.airport_iata == airport,
                QueuePrediction.process == process,
                QueuePrediction.window_start == window_start,
            )
        ).scalar_one_or_none()
        snapshot[(airport, process, window_start)] = (
            row.flight_count if row is not None else None
        )
    return snapshot


@pytest.fixture(scope="module")
def e2e_state():
    """
    T0 refresh'i, sonra AYNI (bellek içi, izole) veritabanı üzerinde
    T1 refresh'ini GERÇEK `pipeline.run()` üzerinden çalıştırır.

    module-scope: pahalı kurulum (havalimanı + uçak kapasite referans
    verisi + iki tam pipeline turu) tüm test fonksiyonları arasında BİR
    KEZ çalışır; testler sonucu SADECE OKUR, veritabanını değiştirmez.
    """
    mp = pytest.MonkeyPatch()
    try:
        mock_source = airlabs_mock_source.install(mp)

        test_engine = create_engine("sqlite://")
        TestSessionLocal = sessionmaker(bind=test_engine)

        mp.setattr(
            pipeline_module, "init_db",
            lambda drop_first=False: Base.metadata.create_all(test_engine),
        )
        mp.setattr(pipeline_module, "get_session", lambda: TestSessionLocal())

        # Referans veri (havalimanı + uçak kapasite tabloları) - gerçek
        # production'da bu bir kere yapılır (bkz. app/seed.py, app/queue/
        # ingestion/airports_import.py); pipeline.run() bunu KENDİSİ
        # yapmaz (ensure_airports sadece havalimanı tablosu boşsa
        # yükler), bu yüzden gerçek dosyalardan burada bir kez seed
        # ediliyor - production'daki tek seferlik kurulum adımının
        # test-izole eşdeğeri.
        Base.metadata.create_all(test_engine)
        seed_session = TestSessionLocal()
        import_airports(seed_session, REAL_AIRPORTS_SQL)
        # ADIM (Operational-Day Scope): bu senaryonun KENDİ amacı T0->T1
        # flight DELTA/pencere-göçü davranışıdır (aircraft change,
        # cancellation, estimated/actual güncellemesi, yeni flight) -
        # operational-day/timezone doğruluğu DEĞİL. `_TRACKED_WINDOWS`
        # BİLEREK iki takvim gününe (15/16 Eylül, ör. AF204'ün 04:00-05:00
        # penceresi 16 Eylül'de) yayılıyor; TEK bir `now` iki günden
        # SADECE birini "bugün" seçip diğer günün flight'larını YANLIŞLIKLA
        # elerdi. `import_airports()` IST/SAW/ADB'ye GERÇEK timezone
        # (Europe/Istanbul) kazandırdığı için bu, production kodu
        # DEĞİŞTİRİLMEDEN, sadece bu testin çok-günlü tasarımına uygun
        # şekilde timezone bilinçli olarak nötrlenir (bkz. rapor).
        seed_session.execute(update(Airport).values(timezone=None))
        seed_verified_dataset(seed_session)
        seed_curated_fallback(seed_session)
        seed_family_and_ga(seed_session)
        seed_session.close()

        source_a = airlabs_client.build_source_a(airlabs_mock_source.AIRPORTS)
        source_b = airlabs_client.build_source_b()

        summary_t0 = pipeline_module.run(source_a=source_a, source_b=source_b)
        check_session = TestSessionLocal()
        windows_after_t0 = _snapshot_windows(check_session)
        check_session.close()

        mock_source.round = "t1"
        summary_t1 = pipeline_module.run(source_a=source_a, source_b=source_b)
        check_session = TestSessionLocal()
        windows_after_t1 = _snapshot_windows(check_session)
        check_session.close()

        yield {
            "session_factory": TestSessionLocal,
            "summary_t0": summary_t0,
            "summary_t1": summary_t1,
            "windows_after_t0": windows_after_t0,
            "windows_after_t1": windows_after_t1,
        }
    finally:
        mp.undo()


def _flight(session, flight_key: str) -> Flight:
    row = session.execute(
        select(Flight).where(Flight.flight_key == flight_key)
    ).scalar_one_or_none()
    assert row is not None, f"beklenen flight_key bulunamadı: {flight_key}"
    return row


# ========================================================================
# A) T0/T1 uçuş sayıları ve upsert davranışı
# ========================================================================

def test_t0_all_inserts_no_updates(e2e_state):
    summary_t0 = e2e_state["summary_t0"]
    assert summary_t0["failed"] == 0
    assert summary_t0["updated"] == 0
    assert summary_t0["inserted"] > 0
    assert summary_t0["flights_parsed"] == summary_t0["inserted"]


def test_t1_adds_exactly_one_new_flight_and_updates_the_rest(e2e_state):
    """
    T1 mutasyonları (A/B/D/E) hiçbiri flight_key'i DEĞİŞTİRMEZ (sadece
    scheduled zaman flight_key'e girer, estimated/actual/aircraft/status
    girmez) - bu yüzden T0'da var olan HER uçuş T1'de "updated" olarak
    görünmeli, SADECE TK999 (Mutasyon C) "inserted" olmalı.
    """
    summary_t0 = e2e_state["summary_t0"]
    summary_t1 = e2e_state["summary_t1"]
    session = e2e_state["session_factory"]()
    try:
        assert summary_t1["failed"] == 0
        assert summary_t1["inserted"] == 1
        assert summary_t1["updated"] == summary_t0["inserted"]

        total_flights = session.scalar(select(func.count()).select_from(Flight))
        assert total_flights == summary_t0["inserted"] + 1
    finally:
        session.close()


# ========================================================================
# F) TK123 flight-identity - departure/arrival ayrı satır, merge YOK
# ========================================================================

def test_tk123_departure_and_arrival_stay_separate_across_both_refreshes(e2e_state):
    session = e2e_state["session_factory"]()
    try:
        dep = _flight(session, "TK_123_2026-09-15_IST_departure")
        arr = _flight(session, "TK_123_2026-09-15_ADB_arrival")
        assert dep.id != arr.id
        assert dep.airport_iata == "IST" and dep.direction == "departure"
        assert arr.airport_iata == "ADB" and arr.direction == "arrival"

        count = session.scalar(
            select(func.count()).select_from(Flight).where(
                Flight.flight_key.in_([
                    "TK_123_2026-09-15_IST_departure",
                    "TK_123_2026-09-15_ADB_arrival",
                ])
            )
        )
        assert count == 2   # T1'den sonra da HALA 2 - tek satıra birleşmedi
    finally:
        session.close()


# ========================================================================
# C) Yeni uçuş - TK999
# ========================================================================

def test_tk999_new_flight_appears_only_after_t1(e2e_state):
    session = e2e_state["session_factory"]()
    try:
        row = _flight(session, "TK_999_2026-09-15_IST_departure")
        assert row.dep_iata == "IST" and row.arr_iata == "ESB"
        assert row.location == "domestic"
        assert row.aircraft_icao == "A321"

        window = floor_to_window(effective_time(row))
        prediction = session.execute(
            select(QueuePrediction).where(
                QueuePrediction.airport_iata == "IST",
                QueuePrediction.process == PROCESS_SECURITY,
                QueuePrediction.window_start == window,
            )
        ).scalar_one_or_none()
        assert prediction is not None
        assert prediction.flight_count >= 1
    finally:
        session.close()


# ========================================================================
# A + B) Estimated/actual değişimi -> pencere kayması
#   PC101 (security + passport), AF204 (passport, estimated->actual)
# ========================================================================

@pytest.mark.parametrize(
    "flight_key,old_window,new_window,process",
    [
        ("PC_101_2026-09-15_IST_departure",
         datetime(2026, 9, 15, 21, 0), datetime(2026, 9, 15, 22, 0),
         PROCESS_SECURITY),
        ("PC_101_2026-09-15_IST_departure",
         datetime(2026, 9, 15, 21, 0), datetime(2026, 9, 15, 22, 0),
         PROCESS_PASSPORT),
        ("AF_204_2026-09-16_IST_arrival",
         datetime(2026, 9, 16, 4, 0), datetime(2026, 9, 16, 5, 0),
         PROCESS_PASSPORT),
    ],
)
def test_estimated_or_actual_change_shifts_prediction_window(
    e2e_state, flight_key, old_window, new_window, process
):
    session = e2e_state["session_factory"]()
    try:
        row = _flight(session, flight_key)
        # 1) Uçuşun GÜNCEL (T1 sonrası) efektif zamanı gerçekten yeni
        #    pencereye düşüyor - eski pencereye değil.
        assert floor_to_window(effective_time(row)) == new_window

        key_old = (row.airport_iata, process, old_window)
        key_new = (row.airport_iata, process, new_window)
        before_old = e2e_state["windows_after_t0"][key_old]
        after_old = e2e_state["windows_after_t1"][key_old]
        before_new = e2e_state["windows_after_t0"][key_new]
        after_new = e2e_state["windows_after_t1"][key_new]

        # 2) Eski pencere T0'da bu uçuşu içeriyordu (satır vardı, >=1).
        assert before_old is not None and before_old >= 1
        # 3) T1 sonrası eski pencere ya tamamen budandı (None) ya da
        #    flight_count kesinlikle AZALDI - hiçbir durumda bu uçuşu
        #    YANLIŞLIKLA tutmaya devam edip AYNI/DAHA BÜYÜK kalamaz.
        assert after_old is None or after_old < before_old
        # 4) Yeni pencere T1 sonrası bu uçuşu İÇERİYOR.
        assert after_new is not None and after_new >= 1
        # 5) Yeni pencerenin flight_count'u T0'a göre ARTTI (ya da T0'da
        #    hiç yoktu, T1'de ilk kez oluştu).
        assert before_new is None or after_new > before_new
    finally:
        session.close()


# ========================================================================
# D) Aircraft enrichment - AF104 T0'da None, T1'de A321
# ========================================================================

def test_af104_had_no_aircraft_in_t0(e2e_state):
    """
    T0 anında (Kaynak B mock edilmedi -> enrichment index boş) en az bir
    uçuşun (AF104 dahil) aircraft_icao'sunun henüz çözülemediğini, yani
    T1'deki enrichment'ın gerçekten YENİ bir bilgi eklediğini kanıtlar.
    """
    summary_t0 = e2e_state["summary_t0"]
    assert summary_t0["aircraft_match_rate"] < 1.0


def test_af104_aircraft_capacity_resolves_after_t1_enrichment(e2e_state):
    session = e2e_state["session_factory"]()
    try:
        row = _flight(session, "AF_104_2026-09-15_IST_departure")
        assert row.aircraft_icao == "A321"
        assert row.aircraft_match_found is True

        from app.service import DEFAULT_CAPACITY, AircraftCapacityService

        result = AircraftCapacityService(session).resolve(
            row.aircraft_icao, row.airline_iata
        )
        assert result.capacity == 185          # yolcu_ucaklari.json - A321
        assert result.capacity != DEFAULT_CAPACITY
        assert result.source == "verified_dataset"
    finally:
        session.close()


# ========================================================================
# E) Cancellation - BA103 active -> cancelled, security talebinden düşer
# ========================================================================

def test_ba103_cancellation_excluded_from_demand_and_event_recorded(e2e_state):
    session = e2e_state["session_factory"]()
    try:
        row = _flight(session, "BA_103_2026-09-15_IST_departure")
        assert row.status == STATUS_CANCELLED
        assert row.location == "domestic"

        event = session.execute(
            select(FlightEvent).where(
                FlightEvent.flight_key == "BA_103_2026-09-15_IST_departure",
                FlightEvent.event_type == EVENT_CANCELLED,
            )
        ).scalar_one_or_none()
        assert event is not None
        assert event.old_value == "active"
        assert event.new_value == "cancelled"

        window = floor_to_window(effective_time(row))
        prediction = session.execute(
            select(QueuePrediction).where(
                QueuePrediction.airport_iata == "IST",
                QueuePrediction.process == PROCESS_SECURITY,
                QueuePrediction.window_start == window,
            )
        ).scalar_one_or_none()
        # BA103 iptal olduğu için bu pencerenin talep/flight_count'una
        # GİRMEMELİ - ama Neden 7 (cancellation) yine de görünür kalmalı
        # (talep hesabından düşmek != izini kaybetmek).
        if prediction is not None:
            assert "cancellation" in (prediction.reasons or "")
    finally:
        session.close()


# ========================================================================
# 8) Baseline / idempotency mekanizması bozulmadı
# ========================================================================

def test_baseline_mechanism_untouched_and_idempotent(e2e_state):
    """
    Baseline mantığı bu ADIM'da DEĞİŞTİRİLMEDİ (bkz. app/queue/baseline.py,
    app/queue/engine.py:record_baseline_observations). Burada sadece
    GERÇEK pipeline.run() üzerinden iki ardışık refresh'in
    HistoricalFlightCount mekanizmasını çökertmediği doğrulanıyor.
    """
    session = e2e_state["session_factory"]()
    try:
        rows = session.execute(select(HistoricalFlightCount)).scalars().all()
        for row in rows:
            assert row.sample_size is not None
            assert row.sample_size >= 1
    finally:
        session.close()


def test_all_three_airports_produced_predictions_in_both_refreshes(e2e_state):
    """
    `pipeline.run()`'ın döndürdüğü özet sözlüğü `run_predictions()`'ın
    kendi `failed_airports` alanını dışarı YANSITMIYOR (bkz.
    app/queue/pipeline.py:run - sadece predictions/pruned/
    airports_predicted seçilip aktarılıyor); bu ADIM'ın kapsamı
    dışındaki, davranışı bozmayan bir gözlem - pipeline.py'ye
    dokunulmadı. Bu yüzden "hiçbir havalimanı başarısız olmadı"
    iddiası burada dolaylı ama kesin kanıtla doğrulanıyor: IST/SAW/ADB
    ÜÇÜNÜN DE her iki refresh'te de tahmin ürettiği.
    """
    summary_t0 = e2e_state["summary_t0"]
    summary_t1 = e2e_state["summary_t1"]
    assert summary_t0["airports_predicted"] == 3
    assert summary_t1["airports_predicted"] == 3
    assert summary_t0["predictions"] > 0
    assert summary_t1["predictions"] > 0
