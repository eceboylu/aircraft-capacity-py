"""
ADIM (Current Operational Day Isolation) - BUG FIX doğrulaması.

BUG: bazı havalimanlarında frontend/API, ÖNCEKİ operasyonel günün
(ör. 20 Eylül) yoğunluk grafiğini "current" gibi göstermeye devam
ediyordu - persistan/çok-günlü bir DB'de (`process_series()` bu
havalimanı/süreç için VERİTABANINDAKİ TÜM satırları, hiçbir tarih
filtresi olmadan çekiyordu) `windows` listesi birden fazla güne ait
GERÇEK satırları KARIŞIK içeriyordu.

ROOT CAUSE (bkz. rapor): `QueuePrediction`'ın hangi operasyonel GÜNÜN
talebinden üretildiğini taşıyan bir alanı YOKTU - `window_start` (event'in
KENDİSİ, `effective_time()`'ın kaydırdığı UTC an) bu ayrımı YAPAMAZ, çünkü
sınır-geçişli/backlog-spillover event'ler (Bölüm 61 - KORUNMASI gereken
davranış) bilerek bu aralığın DIŞINA taşabilir. FIX: `QueuePrediction.
operational_date` (models.py) - `run_predictions()`'ın o havalimanı için
çözdüğü YEREL "bugün" - eklendi; `process_series()` artık "current" (padded)
görünüm için satırları `window_start` ARALIĞI ile DEĞİL, bu alanla filtreler;
`prune_stale_predictions()` de artık SADECE aynı operasyonel güne ait
satırları tarar (böylece ÖNCEKİ günlerin GERÇEK geçmişi silinmez, Bölüm 6).

Bu dosya, Section 10 (A-E) + Section 11 (stale graph regression)
senaryolarını, GERÇEK `Airport.timezone` + zoneinfo üzerinden (hard-code
offset YOK) doğrular.
"""
import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.constants import PROCESS_SECURITY_DOMESTIC
from app.queue.domain.operational_day import operational_date, operational_day_window, resolve_airport_timezone
from app.queue.models import Airport, QueuePrediction


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def _add_row(session, *, airport, process, day_start, hour, flight_count, risk, wait, op_date):
    ws = day_start + timedelta(hours=hour)
    session.add(QueuePrediction(
        airport_iata=airport, process=process,
        window_start=ws, window_end=ws + timedelta(hours=1),
        operational_date=op_date,
        flight_count=flight_count, expected_passengers=flight_count * 150,
        utilization=0.9 if flight_count else 0.0,
        estimated_wait_minutes=wait, risk=risk,
        reasons=json.dumps([]), confidence=0.9,
    ))


# ========================================================================
# Section 11 - STALE GRAPH REGRESSION (en kritik senaryo)
# ========================================================================

def test_stale_previous_day_critical_does_not_leak_into_new_empty_day(session):
    """
    DB: 20 Eylul CRITICAL veri var (IST). now: IST local 21 Eylul, 21
    Eylul icin HICBIR flight yok. API 20 Eylul'u "current" olarak
    KESINLIKLE donmemeli; 21 Eylul grafigi 00..23 sifir-talep olmali.
    """
    tz = resolve_airport_timezone("Europe/Istanbul")
    session.add(Airport(iata_code="IST", airport_name="Istanbul", timezone="Europe/Istanbul"))

    day20_start, _ = operational_day_window(tz, datetime(2026, 9, 20, 10, 0))
    op_date_20 = operational_date(tz, datetime(2026, 9, 20, 10, 0))
    for hour in range(24):
        _add_row(
            session, airport="IST", process=PROCESS_SECURITY_DOMESTIC,
            day_start=day20_start, hour=hour, flight_count=10,
            risk="CRITICAL", wait=120.0, op_date=op_date_20,
        )
    session.commit()

    now_21sep_local = datetime(2026, 9, 21, 10, 0)  # IST local 21 Eylul 13:00
    api = airport_predictions(session, "IST", now=now_21sep_local)
    windows = api["domestic_security"]["windows"]
    current = api["domestic_security"]["current"]

    dates = sorted({w["window_start_local"][:10] for w in windows if w["window_start_local"]})
    assert dates == ["2026-09-21"], f"20 Eylul verisi 21 Eylul grafigine sizdi: {dates}"
    assert all(w["risk"] == "LOW" for w in windows), "21 Eylul (flight=0) icin sifir-talep olmayan satir var"
    assert all(w["flight_count"] == 0 for w in windows)

    assert current is not None
    assert current["risk"] != "CRITICAL", "onceki gunun CRITICAL'i current diye dondu"
    assert current["flight_count"] == 0
    assert current["window_start_local"][:10] == "2026-09-21"


# ========================================================================
# Section 10-A - IST: now=20 Eylul -> current 20 Eylul; now=21 Eylul (flight yok) -> 21 Eylul empty
# ========================================================================

def test_a_ist_current_day_switches_with_local_now_not_hardcoded(session):
    tz = resolve_airport_timezone("Europe/Istanbul")
    session.add(Airport(iata_code="IST", airport_name="Istanbul", timezone="Europe/Istanbul"))

    day19_start, _ = operational_day_window(tz, datetime(2026, 9, 19, 10, 0))
    day20_start, _ = operational_day_window(tz, datetime(2026, 9, 20, 10, 0))
    op_date_19 = operational_date(tz, datetime(2026, 9, 19, 10, 0))
    op_date_20 = operational_date(tz, datetime(2026, 9, 20, 10, 0))

    for hour in range(24):
        _add_row(session, airport="IST", process=PROCESS_SECURITY_DOMESTIC,
                 day_start=day19_start, hour=hour, flight_count=3, risk="LOW", wait=2.0, op_date=op_date_19)
        _add_row(session, airport="IST", process=PROCESS_SECURITY_DOMESTIC,
                 day_start=day20_start, hour=hour, flight_count=7, risk="HIGH", wait=40.0, op_date=op_date_20)
    session.commit()

    api_20 = airport_predictions(session, "IST", now=datetime(2026, 9, 20, 10, 0))
    dates_20 = sorted({w["window_start_local"][:10] for w in api_20["domestic_security"]["windows"] if w["window_start_local"]})
    assert dates_20 == ["2026-09-20"]
    assert all(w["flight_count"] == 7 for w in api_20["domestic_security"]["windows"])

    api_21 = airport_predictions(session, "IST", now=datetime(2026, 9, 21, 10, 0))
    dates_21 = sorted({w["window_start_local"][:10] for w in api_21["domestic_security"]["windows"] if w["window_start_local"]})
    assert dates_21 == ["2026-09-21"]
    assert all(w["flight_count"] == 0 for w in api_21["domestic_security"]["windows"]), "21 Eylul icin flight yokken 20 Eylul'un demand'i fallback olarak gosterildi"


# ========================================================================
# Section 10-B - Yeni gunde flight ingest edilince ayni request GERCEK data gostermeli
# ========================================================================

def test_b_new_day_flight_arriving_makes_current_graph_show_real_data(session):
    tz = resolve_airport_timezone("Europe/Istanbul")
    session.add(Airport(iata_code="IST", airport_name="Istanbul", timezone="Europe/Istanbul"))
    session.commit()

    now_21 = datetime(2026, 9, 21, 10, 0)
    api_before = airport_predictions(session, "IST", now=now_21)
    assert all(w["flight_count"] == 0 for w in api_before["domestic_security"]["windows"])

    day21_start, _ = operational_day_window(tz, now_21)
    op_date_21 = operational_date(tz, now_21)
    _add_row(session, airport="IST", process=PROCESS_SECURITY_DOMESTIC,
             day_start=day21_start, hour=9, flight_count=5, risk="MEDIUM", wait=15.0, op_date=op_date_21)
    session.commit()

    api_after = airport_predictions(session, "IST", now=now_21)
    windows = api_after["domestic_security"]["windows"]
    nonzero = [w for w in windows if w["flight_count"] > 0]
    assert len(nonzero) == 1
    assert nonzero[0]["risk"] == "MEDIUM"
    assert nonzero[0]["window_start_local"][:10] == "2026-09-21"


# ========================================================================
# Section 10-C - CBR farkli local gune gecmis olsa da KENDI gununu kullanmali
# ========================================================================

def test_c_airports_are_not_forced_onto_the_same_global_day(session):
    ist_tz = resolve_airport_timezone("Europe/Istanbul")   # UTC+3
    cbr_tz = resolve_airport_timezone("Australia/Sydney")  # UTC+10 (Eylul'de DST baslamis olabilir - zoneinfo kendi hesaplar)
    session.add(Airport(iata_code="IST", airport_name="Istanbul", timezone="Europe/Istanbul"))
    session.add(Airport(iata_code="CBR", airport_name="Canberra", timezone="Australia/Sydney"))

    # AYNI TEK UTC an: 2026-09-20 18:00 UTC. IST (UTC+3) hala yerel 20 Eylul
    # 21:00'da - gunu henuz gecmedi. CBR (UTC+10) yerel 21 Eylul 04:00'da -
    # gunu CBR icin COK once gecti (CBR yerel gece yarisi = 14:00 UTC).
    shared_utc_now = datetime(2026, 9, 20, 18, 0)

    ist_day_start, _ = operational_day_window(ist_tz, shared_utc_now)
    cbr_day_start, _ = operational_day_window(cbr_tz, shared_utc_now)
    ist_local_date = operational_date(ist_tz, shared_utc_now)
    cbr_local_date = operational_date(cbr_tz, shared_utc_now)

    # Bu ikisi FARKLI takvim gunu olmali (IST hala 20 Eylul aksami, CBR
    # zaten 21 Eylul sabahi) - zoneinfo'dan GERCEK, hesaplanmis deger.
    assert ist_local_date != cbr_local_date, "test varsayimi gecersiz - iki havalimani ayni anda ayni local gunde"

    for hour in range(24):
        _add_row(session, airport="IST", process=PROCESS_SECURITY_DOMESTIC,
                 day_start=ist_day_start, hour=hour, flight_count=4, risk="LOW", wait=3.0, op_date=ist_local_date)
        _add_row(session, airport="CBR", process=PROCESS_SECURITY_DOMESTIC,
                 day_start=cbr_day_start, hour=hour, flight_count=9, risk="HIGH", wait=50.0, op_date=cbr_local_date)
    session.commit()

    api_ist = airport_predictions(session, "IST", now=shared_utc_now)
    api_cbr = airport_predictions(session, "CBR", now=shared_utc_now)

    ist_dates = {w["window_start_local"][:10] for w in api_ist["domestic_security"]["windows"] if w["window_start_local"]}
    cbr_dates = {w["window_start_local"][:10] for w in api_cbr["domestic_security"]["windows"] if w["window_start_local"]}

    assert ist_dates == {str(ist_local_date)}
    assert cbr_dates == {str(cbr_local_date)}
    assert ist_dates != cbr_dates
    # Her biri KENDI gercek verisini gosteriyor - biri digerinin gunune/
    # demand'ine ZORLANMADI.
    assert all(w["flight_count"] == 4 for w in api_ist["domestic_security"]["windows"])
    assert all(w["flight_count"] == 9 for w in api_cbr["domestic_security"]["windows"])


# ========================================================================
# Section 10-D - Negatif UTC offset (LAX benzeri, America/Los_Angeles) -
# UTC takvim gunu ile local takvim gunu FARKLI olmali, local kullanilmali.
# ========================================================================

def test_d_negative_utc_offset_airport_uses_local_date_not_utc_date(session):
    tz = resolve_airport_timezone("America/Los_Angeles")  # UTC-7/-8
    session.add(Airport(iata_code="MFR", airport_name="Medford", timezone="America/Los_Angeles"))

    # UTC 2026-09-20 03:00 -> Los Angeles local 2026-09-19 20:00 (PDT, UTC-7).
    # UTC takvim gunu (20 Eylul) ile local takvim gunu (19 Eylul) FARKLI.
    utc_now = datetime(2026, 9, 20, 3, 0)
    local_date = operational_date(tz, utc_now)
    assert local_date == date(2026, 9, 19), "PDT offset yanlis hesaplandi (hard-code UTC varsayimi supheli)"

    day_start, _ = operational_day_window(tz, utc_now)
    for hour in range(24):
        _add_row(session, airport="MFR", process=PROCESS_SECURITY_DOMESTIC,
                 day_start=day_start, hour=hour, flight_count=2, risk="LOW", wait=1.0, op_date=local_date)
    session.commit()

    api = airport_predictions(session, "MFR", now=utc_now)
    dates = {w["window_start_local"][:10] for w in api["domestic_security"]["windows"] if w["window_start_local"]}
    assert dates == {"2026-09-19"}, f"negatif offset havalimani icin local tarih beklenmedik: {dates}"


# ========================================================================
# Section 10-E - DST-capable (Europe/Zurich) - zoneinfo GERCEK offset kullanmali.
# ========================================================================

def test_e_dst_capable_airport_resolves_via_zoneinfo_not_fixed_offset(session):
    tz = resolve_airport_timezone("Europe/Zurich")
    session.add(Airport(iata_code="ZRH", airport_name="Zurich", timezone="Europe/Zurich"))

    # 2026-09-20 hala Avrupa yaz saati (DST) donemi - Zurich UTC+2 olmali (UTC+1 DEGIL).
    utc_now = datetime(2026, 9, 20, 10, 0)
    local_now = utc_now.replace(tzinfo=__import__("zoneinfo").ZoneInfo("UTC")).astimezone(tz)
    assert local_now.utcoffset().total_seconds() / 3600 == 2, "Zurich Eylul'de DST (UTC+2) olmali - zoneinfo yanlis offset uretti"

    local_date = operational_date(tz, utc_now)
    day_start, _ = operational_day_window(tz, utc_now)
    for hour in range(24):
        _add_row(session, airport="ZRH", process=PROCESS_SECURITY_DOMESTIC,
                 day_start=day_start, hour=hour, flight_count=6, risk="MEDIUM", wait=8.0, op_date=local_date)
    session.commit()

    api = airport_predictions(session, "ZRH", now=utc_now)
    dates = {w["window_start_local"][:10] for w in api["domestic_security"]["windows"] if w["window_start_local"]}
    assert dates == {str(local_date)}
    assert len(api["domestic_security"]["windows"]) == 24


# ========================================================================
# History korunuyor mu - eski gunun QueuePrediction satirlari DB'den
# SILINMEDI (sadece "current" gorunumden disarida), Section 6.
# ========================================================================

def test_old_day_rows_are_not_deleted_from_db_only_excluded_from_current_view(session):
    tz = resolve_airport_timezone("Europe/Istanbul")
    session.add(Airport(iata_code="IST", airport_name="Istanbul", timezone="Europe/Istanbul"))

    day20_start, _ = operational_day_window(tz, datetime(2026, 9, 20, 10, 0))
    op_date_20 = operational_date(tz, datetime(2026, 9, 20, 10, 0))
    for hour in range(24):
        _add_row(session, airport="IST", process=PROCESS_SECURITY_DOMESTIC,
                 day_start=day20_start, hour=hour, flight_count=10, risk="CRITICAL", wait=120.0, op_date=op_date_20)
    session.commit()

    # 21 Eylul sorgusu bu satirlari "current" gorunumden HARIC tutar (yukaridaki
    # stale-leak testi) - AMA satirlar veritabaninda fiziksel olarak KALMALI.
    airport_predictions(session, "IST", now=datetime(2026, 9, 21, 10, 0))

    remaining = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == "IST",
            QueuePrediction.operational_date == op_date_20,
        )
    ).scalars().all()
    assert len(remaining) == 24, "20 Eylul'un GERCEK gecmis satirlari read-only bir sorgu sirasinda silindi"
