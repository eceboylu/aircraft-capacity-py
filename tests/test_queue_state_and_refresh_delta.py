"""
ADIM (Queue State Persistence / Refresh Delta) - genel-proje.md Bölüm
13/52/53/54/55/56/57 doğrulaması.

Mevcut mimari analizi (kod üzerinden doğrulandı, tahmin edilmedi):
`run_predictions()` her çağrıda `flights_of_airport()` ile Flight
tablosunun TAMAMINI okuyup event-driven motoru (`predict_airport()` ->
`_event_driven_queue_demand()` -> `core/event_queue.py`) SIFIRDAN,
DETERMİNİSTİK olarak yeniden çalıştırır - Flight tablosu (upsert ile
korunan, gerçek kaynak veri) DIŞINDA ayrı bir "queue state" mutable'ı
YOKTUR. Bu, Bölüm 13/53/54'ün istediği "queue state pipeline run'ları
arasında kaybolmaz" gereksinimini EK bir persisted tablo GEREKMEDEN
karşılar: aynı Flight verisi + ilerleyen gerçek `now` ile her çağrı
AYNI (veya - zaman geçtiği için - daha ilerlemiş) sonucu üretir.
`QueuePrediction` UPSERT (bkz. `persist_predictions()`) + `prune_stale_
predictions()` zaten var olan, bu ADIM'da DEĞİŞTİRİLMEYEN mekanizmalardır.

Bu dosya bu iddiayı GERÇEK `refresh_flights()`/`run_predictions()`
(event-driven motor DAHİL) üzerinden kanıtlar - hiçbir yeni production
kodu veya migration gerekmediği, testlerle DOĞRULANMIŞ bir sonuçtur.
"""

from datetime import datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.constants import (
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY_INTL,
)
from app.queue.engine import run_predictions
from app.queue.ingestion.refresh import refresh_flights
from app.queue.models import Flight, QueuePrediction

from .factories import MockCapacityResolver

DAY = datetime(2026, 9, 15)


def at(hour, minute=0):
    return DAY.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _row(
    key, direction, location, dep_scheduled, arr_scheduled,
    aircraft="A320", status="scheduled", airport="AAA",
    dep_estimated=None, dep_actual=None,
):
    return {
        "flight_key": key, "airport_iata": airport,
        "direction": direction, "location": location,
        "airline_iata": "TK", "flight_number": key, "flight_iata": f"TK{key}",
        "aircraft_icao": aircraft, "aircraft_match_found": True,
        "dep_iata": airport, "arr_iata": "ZZZ",
        "dep_scheduled_utc": dep_scheduled, "dep_estimated_utc": dep_estimated,
        "dep_actual_utc": dep_actual,
        "arr_scheduled_utc": arr_scheduled, "arr_estimated_utc": None,
        "arr_actual_utc": None,
        "dep_terminal": None, "dep_gate": None,
        "arr_terminal": None, "arr_gate": None, "status": status,
    }


def _intl_departure_row(key, dep_scheduled, aircraft="A320", status="scheduled"):
    return _row(
        key, "departure", LOCATION_INTERNATIONAL,
        dep_scheduled, dep_scheduled + timedelta(minutes=210), aircraft, status,
    )


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _resolver():
    return MockCapacityResolver(capacities={"A320": 180, "E190": 100})


def _security_intl_row(session, window_start):
    return session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == "AAA",
            QueuePrediction.process == PROCESS_SECURITY_INTL,
            QueuePrediction.window_start == window_start,
        )
    ).scalar_one_or_none()


def _passport_row(session, window_start):
    return session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == "AAA",
            QueuePrediction.process == PROCESS_PASSPORT,
            QueuePrediction.window_start == window_start,
        )
    ).scalar_one_or_none()


def _passport_departure_row(session, window_start):
    return session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == "AAA",
            QueuePrediction.process == PROCESS_PASSPORT_DEPARTURE,
            QueuePrediction.window_start == window_start,
        )
    ).scalar_one_or_none()


# ========================================================================
# Bölüm 53 - queue, yeni veri gelmeden GEÇEN GERÇEK ZAMANDA ilerlemeye
# devam eder (R0 -> hiçbir değişiklik yok -> R1, sadece `now` ilerliyor).
# ========================================================================

def test_queue_progresses_with_real_time_when_no_new_data_arrives():
    """
    ADIM (Departure Show-Up Profile): bu test ESKİDEN legacy `PROCESS_
    PASSPORT`'un (Erlang-C/fluid, `now`'a göre KISMİ/elapsed-tabanlı,
    zaman-BAĞIMLI) wait'inin zaman ilerledikçe AZALDIĞINI kontrol
    ediyordu. Show-up sonrası bu süreç için `demand_override` artık
    TEK bir noktada DEĞİL, saat içinde dağınık show-up batch'lerinden
    geliyor - fluid modelin "an itibariyle görünen kısmi talep"
    yaklaşıklığı artık MONOTONİK azalan DEĞİL (bazı batch'ler `now`
    ilerledikçe YENİ görünür hale gelip anlık görüntüyü değiştirebilir) -
    bu, GERÇEK ve BEKLENEN bir davranış değişikliği (legacy/görünmez
    bir grafiğin dahili yaklaşıklığı, production contract'ı DEĞİL).

    Asıl doğrulanması gereken contract (Bölüm 53) DEĞİŞMEDİ: idempotent
    refresh + SADECE zamanın ilerlemesi YENİ/duplicate demand YARATMAZ.
    Bunu artık GERÇEK, kullanıcı-visible, zaman-BAĞIMSIZ event-driven
    süreç (`PROCESS_PASSPORT_DEPARTURE`) üzerinden - iki farklı `now`
    değerinde AYNI (STABİL) sonucu üretmeli - doğruluyoruz.
    """
    session = _session()
    # 6 x E190 (100 her biri = 600 toplam), passport kapasitesi 320/saat -
    # ağır backlog, window 10:00-11:00 (effective_time = 12:00-120dk=10:00).
    rows = [
        _intl_departure_row(f"D{i}", at(12, 0), aircraft="E190")
        for i in range(6)
    ]
    refresh_flights(session, rows)
    resolver = _resolver()

    # R0 - pencere henüz YENİ açıldı (now = window_start'a çok yakın).
    run_predictions(session, resolver, airports=["AAA"], now=at(10, 1))
    r0 = _passport_departure_row(session, at(10, 0))
    assert r0 is not None

    # R1 - HİÇBİR yeni/değişen veri YOK (aynı payload tekrar refresh
    # edildi - idempotent, 0 insert/0 event), SADECE gerçek zaman ilerledi.
    refresh = refresh_flights(session, rows)
    assert refresh["inserted"] == 0
    assert refresh["events_written"] == 0

    run_predictions(session, resolver, airports=["AAA"], now=at(10, 31))
    r1 = _passport_departure_row(session, at(10, 0))
    assert r1 is not None

    # Event-driven süreç TAM simülasyon sonucudur (`now`'a bakmaz) -
    # queue "sıfırlanıp" farklı bir sonuca SIÇRAMADI, tamamen STABİL.
    assert r1.expected_passengers == r0.expected_passengers
    assert r1.estimated_wait_minutes == r0.estimated_wait_minutes


# ========================================================================
# Bölüm 55/56 - aynı payload tekrar geldiğinde duplicate passenger
# demand OLUŞMAZ (hem Flight hem QueuePrediction seviyesinde).
# ========================================================================

def test_identical_payload_refreshed_twice_does_not_duplicate_demand():
    session = _session()
    rows = [_intl_departure_row("D1", at(12, 0))]  # effective_time=10:00, kapalı pencere
    refresh_flights(session, rows)
    resolver = _resolver()

    run_predictions(session, resolver, airports=["AAA"], now=at(23, 0))
    row_first = _passport_row(session, at(10, 0))
    first_demand = row_first.expected_passengers

    # Aynı payload İKİNCİ kez (source her 5 dakikada aynı kaydı tekrar
    # gönderiyor gibi) - hiçbir alan değişmedi.
    refresh_again = refresh_flights(session, rows)
    assert refresh_again["inserted"] == 0
    assert refresh_again["updated"] == 1

    run_predictions(session, resolver, airports=["AAA"], now=at(23, 5))
    row_second = _passport_row(session, at(10, 0))

    # ADIM (Departure Show-Up Profile): 12:00 kalkış artık show-up ile
    # 09:00/10:00/11:00'e (%20/%60/%20) yayılıyor - 10:00 zirve payı
    # 180*0.6=108 (TAMAMI DEĞİL, DEĞİŞTİ) - ama "İKİYE KATLANMADI" iddiası
    # (bu testin ASIL konusu) DEĞİŞMEDİ.
    assert row_second.expected_passengers == first_demand == 108
    total_flight_rows = session.scalar(select(func.count()).select_from(Flight))
    assert total_flight_rows == 1
    total_prediction_rows = session.scalar(
        select(func.count()).select_from(QueuePrediction).where(
            QueuePrediction.airport_iata == "AAA",
            QueuePrediction.process == PROCESS_PASSPORT,
            QueuePrediction.window_start == at(10, 0),
        )
    )
    assert total_prediction_rows == 1   # UPSERT - ikinci satır AÇILMADI


# ========================================================================
# Bölüm 54/55 - UPDATED (delay): eski pencerenin yanında YENİ bir demand
# yaratılmaz; demand doğru pencereye TAŞINIR.
# ========================================================================

def test_delay_moves_demand_to_new_window_without_duplicating_old_one():
    session = _session()
    # R0: kalkış 12:00 -> effective_time 10:00 (pencere 10:00-11:00).
    row_r0 = _intl_departure_row("D1", at(12, 0))
    refresh_flights(session, [row_r0])
    resolver = _resolver()
    run_predictions(session, resolver, airports=["AAA"], now=at(23, 0))

    before = _passport_row(session, at(10, 0))
    assert before is not None
    # ADIM (Departure Show-Up Profile): 10:00 zirve payı 180*0.6=108 (DEĞİŞTİ).
    assert before.expected_passengers == 108

    # R1: AYNI uçuş 2 saat gecikiyor -> kalkış 14:00 -> effective_time
    # 12:00 (pencere 12:00-13:00). flight_key AYNI - UPSERT.
    row_r1 = _intl_departure_row("D1", at(14, 0))
    refresh_result = refresh_flights(session, [row_r1])
    assert refresh_result["inserted"] == 0
    assert refresh_result["updated"] == 1

    run_predictions(session, resolver, airports=["AAA"], now=at(23, 0))

    old_window = _passport_row(session, at(10, 0))
    new_window = _passport_row(session, at(12, 0))

    # Eski pencere ARTIK YOK (prune_stale_predictions tarafından
    # temizlendi - bir daha üretilmiyor) - stale demand YANINDA kalmadı.
    assert old_window is None
    assert new_window is not None
    # ADIM (Departure Show-Up Profile): YENİ show-up penceresinin
    # (11:00/12:00/13:00, %20/%60/%20) zirve payı - AYNI yolcu, İKİNCİ
    # kez YARATILMADI, formül DEĞİŞMEDİ.
    assert new_window.expected_passengers == 108

    total_flight_rows = session.scalar(select(func.count()).select_from(Flight))
    assert total_flight_rows == 1   # hâlâ TEK uçuş satırı


# ========================================================================
# Bölüm 54/55 - CANCELLED: demand kalkar, terminal-status resurrection
# fix'i (BUG-01) korunur.
# ========================================================================

def test_cancellation_removes_demand_and_resists_later_non_terminal_row():
    session = _session()
    row_r0 = _intl_departure_row("D1", at(12, 0))
    refresh_flights(session, [row_r0])
    resolver = _resolver()
    run_predictions(session, resolver, airports=["AAA"], now=at(23, 0))
    # ADIM (Departure Show-Up Profile): 10:00 zirve payı 180*0.6=108 (DEĞİŞTİ).
    assert _passport_row(session, at(10, 0)).expected_passengers == 108

    row_cancelled = _intl_departure_row("D1", at(12, 0), status="cancelled")
    refresh_flights(session, [row_cancelled])
    run_predictions(session, resolver, airports=["AAA"], now=at(23, 0))

    after_cancel = _passport_row(session, at(10, 0))
    # Pencere hâlâ var olabilir (flight_count/neden tespiti için) ama
    # TALEP sıfır - cancelled uçuş demand'e hiç girmiyor.
    if after_cancel is not None:
        assert after_cancel.expected_passengers == 0

    # BUG-01: daha ESKİ/gecikmeli bir "scheduled" satırı cancelled
    # durumu GERİ ALAMAZ.
    stale_resurrect_attempt = _intl_departure_row("D1", at(12, 0), status="scheduled")
    refresh_flights(session, [stale_resurrect_attempt])
    flight = session.execute(
        select(Flight).where(Flight.flight_key == "D1")
    ).scalar_one()
    assert flight.status == "cancelled"


# ========================================================================
# Bölüm 13/54 - TAMAMLANMIŞ (kapalı pencere) servis, refresh sonrası
# YENİDEN YARATILMAZ - deterministik recompute, phantom değişiklik yok.
# ========================================================================

def test_completed_window_values_are_stable_across_reruns():
    session = _session()
    rows = [_intl_departure_row(f"D{i}", at(9, 0), aircraft="E190") for i in range(3)]
    refresh_flights(session, rows)
    resolver = _resolver()

    # Pencere KESİNLİKLE kapalı (now çok ileride).
    run_predictions(session, resolver, airports=["AAA"], now=at(23, 0))
    first = _passport_row(session, at(7, 0))
    snapshot = (
        first.expected_passengers, first.utilization,
        first.estimated_wait_minutes, first.risk,
    )

    # Aynı flight'lar tekrar refresh edildi (değişiklik yok) - run_predictions
    # yine (daha da ileri bir "now" ile) tekrar çağrıldı.
    refresh_flights(session, rows)
    run_predictions(session, resolver, airports=["AAA"], now=at(23, 30))
    second = _passport_row(session, at(7, 0))
    snapshot_2 = (
        second.expected_passengers, second.utilization,
        second.estimated_wait_minutes, second.risk,
    )

    # KAPALI pencerenin sonucu iki çalıştırma arasında BİREBİR AYNI -
    # "tamamlanmış" servis farklı bir sayıyla yeniden yaratılmadı.
    assert snapshot == snapshot_2


# ========================================================================
# Bölüm 56 - refresh atomicity: bozuk/yarım satır DB'yi inconsistent
# bırakmaz, sağlam satırlar etkilenmeden kalır (mevcut per-satır
# transaction izolasyonu - refresh.py, DEĞİŞTİRİLMEDİ).
# ========================================================================

def test_one_broken_row_does_not_break_or_duplicate_other_rows():
    session = _session()
    good = _intl_departure_row("GOOD", at(12, 0))
    broken = dict(_intl_departure_row("BROKEN", at(12, 0)))
    broken["dep_scheduled_utc"] = "not-a-real-datetime"   # bozuk alan

    result = refresh_flights(session, [good, broken])
    assert result["inserted"] == 1
    assert result["failed"] == 1

    rows = session.execute(select(Flight)).scalars().all()
    assert len(rows) == 1
    assert rows[0].flight_key == "GOOD"

    # Aynı iyi satır TEKRAR gönderilirse (source bir dahaki turda hâlâ
    # bozuk satırı taşıyor olabilir) - iyi satır duplicate OLMAZ.
    result_2 = refresh_flights(session, [good, broken])
    assert result_2["inserted"] == 0
    assert result_2["updated"] == 1
    assert result_2["failed"] == 1
    rows_2 = session.execute(select(Flight)).scalars().all()
    assert len(rows_2) == 1


# ========================================================================
# Bölüm 57 - üst üste binen ("overlapping") refresh: aynı payload'ın
# ARKA ARKAYA (ara gecikme olmadan) iki kez işlenmesi duplicate state
# ÜRETMEZ - unique constraint + upsert zaten koruma sağlıyor.
# ========================================================================

def test_back_to_back_refresh_of_same_payload_produces_no_duplicate_rows():
    session = _session()
    rows = [_intl_departure_row(f"D{i}", at(12, 0)) for i in range(3)]

    # İki "refresh" arka arkaya, aralarında BAŞKA hiçbir işlem yok -
    # üst üste binen/çakışan refresh'in en yakın simülasyonu (gerçek
    # deployment tek-process/senkron olduğu için gerçek thread'ler
    # arası yarış senaryosu YOK - bkz. rapor).
    r_a = refresh_flights(session, rows)
    r_b = refresh_flights(session, rows)

    assert r_a["inserted"] == 3
    assert r_b["inserted"] == 0
    assert r_b["updated"] == 3

    total = session.scalar(select(func.count()).select_from(Flight))
    assert total == 3   # 6 DEĞİL - duplicate YOK

    resolver = _resolver()
    run_predictions(session, resolver, airports=["AAA"], now=at(23, 0))
    run_predictions(session, resolver, airports=["AAA"], now=at(23, 0))  # ikinci ardışık çağrı
    prediction_rows = session.scalar(
        select(func.count()).select_from(QueuePrediction).where(
            QueuePrediction.airport_iata == "AAA",
            QueuePrediction.process == PROCESS_PASSPORT,
            QueuePrediction.window_start == at(10, 0),
        )
    )
    assert prediction_rows == 1   # UPSERT - ikinci run_predictions() İKİNCİ satır AÇMADI
