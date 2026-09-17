"""
ADIM (Operational-Day Scope) - genel-proje.md Bölüm 58/59/60/61/66
doğrulaması.

Timezone kaynağı doğrulaması (kod üzerinden, uydurulmadı):
`Airport.timezone` (app/queue/models.py) GERÇEK kaynaktan gelir -
`data/flight_airports.sql`'in `customized` JSON alanındaki IANA
timezone adları (ör. IST için "Europe/Istanbul", doğrudan dosyadan
okunarak doğrulandı), `app/queue/ingestion/airports_import.py` ile
içe aktarılır. Bu alan projede DAHA ÖNCE hiçbir hesaba BAĞLANMAMIŞTI -
bu ADIM'da `app/queue/domain/operational_day.py` üzerinden İLK KEZ
kullanılıyor.

BULUNAN GERÇEK BLOCKER (uydurma DEĞİL, ortamda doğrudan doğrulandı):
Bu Windows geliştirme ortamında `zoneinfo.ZoneInfo("Europe/Istanbul")`
`tzdata` paketi kurulu değilken `ZoneInfoNotFoundError` fırlatıyordu
(IANA veritabanı sistемde yoktu). Uydurma bir offset/mapping
ÜRETİLMEDİ - bunun yerine Python'un resmi `tzdata` paketi
`requirements.txt`'ye eklendi (bkz. o dosyadaki gerekçe yorumu).
Havalimanının `Airport.timezone` alanı boş/tanınmayan bir değerse
`resolve_airport_timezone()` None döner ve `run_predictions()` o
havalimanı için filtreyi UYGULAMAZ (eski davranış) + `timezone_missing_
airports` alanında AÇIKÇA raporlar - bkz. `test_resolve_airport_timezone_*`.
"""

from datetime import date, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.constants import LOCATION_DOMESTIC, LOCATION_INTERNATIONAL
from app.queue.domain.operational_day import (
    filter_flights_for_operational_day,
    flight_reference_time,
    operational_date,
    operational_day_window,
    resolve_airport_timezone,
)
from app.queue.engine import run_predictions
from app.queue.ingestion.refresh import refresh_flights
from app.queue.models import Airport, QueuePrediction

from .factories import MockCapacityResolver

IST_TZ_NAME = "Europe/Istanbul"   # UTC+3, DST yok (2016'dan beri sabit) - basit/güvenilir test tabanı


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _row(
    key, direction, dep_scheduled=None, arr_scheduled=None,
    location=LOCATION_DOMESTIC, airport="IST", aircraft="A320", status="scheduled",
):
    return {
        "flight_key": key, "airport_iata": airport,
        "direction": direction, "location": location,
        "airline_iata": "TK", "flight_number": key, "flight_iata": f"TK{key}",
        "aircraft_icao": aircraft, "aircraft_match_found": True,
        "dep_iata": airport, "arr_iata": "ZZZ",
        "dep_scheduled_utc": dep_scheduled, "dep_estimated_utc": None,
        "dep_actual_utc": None,
        "arr_scheduled_utc": arr_scheduled, "arr_estimated_utc": None,
        "arr_actual_utc": None,
        "dep_terminal": None, "dep_gate": None,
        "arr_terminal": None, "arr_gate": None, "status": status,
    }


# ========================================================================
# Bölüm 59 - timezone kaynağı doğrulama + güvenli fallback (blocker
# UYDURULMADAN raporlanır).
# ========================================================================

def test_resolve_airport_timezone_returns_real_zoneinfo_for_known_name():
    tz = resolve_airport_timezone(IST_TZ_NAME)
    assert tz is not None
    assert str(tz) == IST_TZ_NAME


def test_resolve_airport_timezone_returns_none_for_missing_or_unknown():
    """Güvenilir kaynak YOKSA (boş VEYA tanınmayan) None - UYDURMA fallback YOK."""
    assert resolve_airport_timezone(None) is None
    assert resolve_airport_timezone("") is None
    assert resolve_airport_timezone("Not/A_Real_Zone") is None


def test_run_predictions_reports_airports_without_reliable_timezone_as_limitation():
    """
    Airport.timezone boşsa (satır YOK veya alan NULL) filtre UYGULANMAZ
    (eski davranış korunur) VE bu AÇIKÇA `timezone_missing_airports`'ta
    raporlanır - sessizce yanlış gün seçilmez (Bölüm 59).
    """
    session = _session()
    # "AAA" için hiç Airport satırı YOK - timezone kaynağı YOK.
    refresh_flights(session, [
        _row("D1", "departure", dep_scheduled=datetime(2020, 1, 1, 10, 0), airport="AAA"),
    ])
    resolver = MockCapacityResolver()
    result = run_predictions(session, resolver, airports=["AAA"], now=datetime(2026, 9, 19, 10, 0))

    assert "AAA" in result["timezone_missing_airports"]
    # Filtre uygulanmadığı için 2020 tarihli uçuş HÂLÂ (eski davranış) hesaba girdi.
    assert result["airports"]["AAA"] > 0


# ========================================================================
# Bölüm 58/66 - ZORUNLU DAILY-SCOPE TESTİ: 18/19/20 Eylül veri içinde
# yalnız 19 Eylül'ün YENİ flight demand kaynağı olduğunu kanıtla.
# ========================================================================

def _seed_ist(session):
    session.add(Airport(
        iata_code="IST", icao_code="LTFM", airport_name="Istanbul Airport",
        city_code="IST", country_code="TR", timezone=IST_TZ_NAME,
    ))
    session.commit()


def test_daily_scope_only_selected_operational_day_flights_are_new_demand_source():
    session = _session()
    _seed_ist(session)

    rows = [
        _row("D18", "departure", dep_scheduled=datetime(2026, 9, 18, 10, 0)),   # yerel 18 Eylül 13:00 - DIŞARIDA
        _row("D19", "departure", dep_scheduled=datetime(2026, 9, 19, 10, 0)),   # yerel 19 Eylül 13:00 - İÇERİDE
        _row("D20", "departure", dep_scheduled=datetime(2026, 9, 20, 10, 0)),   # yerel 20 Eylül 13:00 - DIŞARIDA
    ]
    refresh_flights(session, rows)
    resolver = MockCapacityResolver()

    now = datetime(2026, 9, 19, 10, 0)   # yerel (Istanbul, UTC+3) 19 Eylül 13:00 - "bugün" = 19 Eylül
    result = run_predictions(session, resolver, airports=["IST"], now=now)

    assert "IST" not in result["timezone_missing_airports"]

    # SADECE `PROCESS_SECURITY_DOMESTIC` - domestic kalkış AYNI zamanda
    # birleşik (legacy) `PROCESS_SECURITY`'de de raporlanır (AYNI fiziksel
    # talebin İKİNCİ bir GÖRÜNÜMÜ, double-count DEĞİL) - tek sürece
    # bakılarak "1 flight = 1 talep" doğrulanır.
    predictions = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == "IST",
            QueuePrediction.process == "security_dom",
        )
    ).scalars().all()
    total_demand = sum(p.expected_passengers for p in predictions)

    # SADECE D19'un talebi (180 - A320) hesaba girdi; D18/D20'nin talebi
    # (toplamda 2x180=360 olurdu) YENİ demand kaynağı olarak KULLANILMADI.
    assert total_demand == 180


def test_daily_scope_boundary_departure_minus_120_crossing_into_previous_day_is_not_lost():
    """
    Bölüm 61 örneği: yerel 19 Eylül 00:45 kalkış (kendi operasyonel
    günü = 19 Eylül) -> passenger airport arrival yerel 18 Eylül 22:45'e
    DÜŞER (-120dk). Bu event, flight'ın operasyonel günü SEÇİM
    aşamasında kullanıldığı için (queue event'in KENDİ günü DEĞİL)
    KAYBOLMAMALI.
    """
    session = _session()
    _seed_ist(session)

    # Yerel 19 Eylül 00:45 = UTC 18 Eylül 21:45 (Istanbul UTC+3).
    boundary_dep_utc = datetime(2026, 9, 18, 21, 45)
    rows = [_row("BOUND_DEP", "departure", dep_scheduled=boundary_dep_utc, location=LOCATION_INTERNATIONAL)]
    refresh_flights(session, rows)
    resolver = MockCapacityResolver()

    now = datetime(2026, 9, 19, 10, 0)   # yerel 19 Eylül - flight'ın operasyonel günü
    result = run_predictions(session, resolver, airports=["IST"], now=now)

    assert "IST" not in result["timezone_missing_airports"]
    assert result["airports"]["IST"] > 0   # flight SEÇİLDİ (kendi günü 19 Eylül)

    # Queue event (effective_time = dep - 120dk) GERÇEKTEN 18 Eylül'e
    # düştü - bu, PASSPORT sürecinin demand'inde GÖRÜNMELİ (kaybolmadı).
    passport_rows = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == "IST",
            QueuePrediction.process == "passport",
        )
    ).scalars().all()
    assert any(p.expected_passengers > 0 for p in passport_rows)
    total = sum(p.expected_passengers for p in passport_rows)
    assert total == 180   # A320 - yolcu YARATILMADI/KAYBOLMADI, tam 1 uçuşluk talep


def test_daily_scope_boundary_arrival_plus_15_crossing_into_next_day_is_not_lost():
    """
    Bölüm 61 örneği: yerel 19 Eylül 23:55 varış (kendi operasyonel günü
    = 19 Eylül) -> passport event yerel 20 Eylül 00:10'a DÜŞER (+15dk).
    Bu event de kaybolmamalı.
    """
    session = _session()
    _seed_ist(session)

    # Yerel 19 Eylül 23:55 = UTC 19 Eylül 20:55 (Istanbul UTC+3).
    boundary_arr_utc = datetime(2026, 9, 19, 20, 55)
    rows = [_row(
        "BOUND_ARR", "arrival", arr_scheduled=boundary_arr_utc,
        location=LOCATION_INTERNATIONAL,
    )]
    refresh_flights(session, rows)
    resolver = MockCapacityResolver()

    # `now` yerel 19 Eylül İÇİNDE kalmalı (flight'ın kendi operasyonel
    # günü) - "an itibariyle" hesabı için flight'ın kendi zamanından
    # SONRA olması yeterli, gece yarısını GEÇMESİ gerekmiyor (SEÇİM
    # `now`'ın kendi operasyonel gününe göre yapılır, flight bu güne
    # ait olmalı).
    now = datetime(2026, 9, 19, 12, 0)   # yerel 19 Eylül 15:00
    result = run_predictions(session, resolver, airports=["IST"], now=now)

    assert "IST" not in result["timezone_missing_airports"]
    assert result["airports"]["IST"] > 0   # flight SEÇİLDİ (kendi günü 19 Eylül)

    passport_rows = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == "IST",
            QueuePrediction.process == "passport",
        )
    ).scalars().all()
    total = sum(p.expected_passengers for p in passport_rows)
    assert total == 180   # varış yolcusu kaybolmadı


# ========================================================================
# Bölüm 60 - gece yarısında mevcut queue/backlog sıfırlanmaz: AYNI
# operasyonel gün içindeki servis, gece yarısını geçerken kesilmez.
# ========================================================================

def test_midnight_does_not_reset_queue_service_within_same_operational_day():
    """
    Bugünün (19 Eylül) bir flight cohort'u passport'ta ağır backlog
    yaratıyor; servisi yerel gece yarısını (Istanbul, UTC 21:00) GEÇEREK
    sürüyor. Backlog fiziksel olarak KESİLMEMELİ - event-driven motor
    zaten takvim günü FARKINDA DEĞİL (bkz. core/event_queue.py), bu
    test bunun operational-day filtresiyle BOZULMADIĞINI VE gece
    yarısını geçen saatlerin QueuePrediction'da GERÇEKTEN VAR olduğunu
    kanıtlar.
    """
    session = _session()
    _seed_ist(session)

    # 20 x E190 (100 her biri = 2000), passport kapasitesi 320/saat ->
    # ~6.25 saat sürüyor boşalması. Yerel 19 Eylül 22:00 kalkış (UTC
    # 19:00) -> effective_time UTC 17:00 (yerel 20:00, hâlâ 19 Eylül) ->
    # servis ~17:00'dan ~23:15 UTC'ye kadar sürer - yerel gece yarısı
    # sınırını (UTC 21:00 = yerel 20 Eylül 00:00) AÇIKÇA GEÇER.
    dep_utc = datetime(2026, 9, 19, 19, 0)   # yerel 22:00, 19 Eylül - flight'ın KENDİ günü hâlâ 19 Eylül
    rows = [
        _row(f"MN{i}", "departure", dep_scheduled=dep_utc, location=LOCATION_INTERNATIONAL, aircraft="E190")
        for i in range(20)
    ]
    refresh_flights(session, rows)
    resolver = MockCapacityResolver(capacities={"E190": 100})

    # `now` yerel 19 Eylül İÇİNDE kalmalı - flight'ın SEÇİLMESİ için
    # (Bölüm 58: "bugün" = flight'ın kendi operasyonel günü, "an
    # itibariyle" hesabın hangi ana kadar ilerlediği DEĞİL).
    now = datetime(2026, 9, 19, 20, 0)   # yerel 23:00, 19 Eylül
    result = run_predictions(session, resolver, airports=["IST"], now=now)
    assert "IST" not in result["timezone_missing_airports"]

    passport_rows = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == "IST",
            QueuePrediction.process == "passport",
        )
    ).scalars().all()
    assert sum(p.expected_passengers for p in passport_rows) == 2000

    # PASSPORT'un KENDİ demand'i tek pencerede (17:00 - flight'ların hepsi
    # AYNI anda geldi) raporlanır (backlog_end o pencerenin İÇİNDE taşınır,
    # DEĞİŞMEDİ) - servisin GERÇEKTEN gece yarısını geçerek sürdüğünün
    # kanıtı `security_intl`'in (passport completion -> security arrival,
    # AŞAMA 9) saat saat yayılan seri sindedir.
    security_intl_rows = session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == "IST",
            QueuePrediction.process == "security_intl",
        )
    ).scalars().all()
    assert sum(p.expected_passengers for p in security_intl_rows) == 2000

    # Gece yarısını (UTC 21:00, yerel 20 Eylül 00:00) GEÇEN en az bir
    # pencere GERÇEKTEN persist edilmiş olmalı - backlog "takvim günü
    # bitti" diye sessizce sıfırlanmadı/kesilmedi.
    assert any(p.window_start >= datetime(2026, 9, 19, 21, 0) for p in security_intl_rows)


# ========================================================================
# operational_day.py - saf fonksiyon birim testleri
# ========================================================================

def test_operational_day_window_returns_correct_local_midnight_boundaries_in_utc():
    tz = resolve_airport_timezone(IST_TZ_NAME)
    now_utc = datetime(2026, 9, 19, 10, 0)   # yerel 13:00, 19 Eylül
    start, end = operational_day_window(tz, now_utc)
    # Yerel 19 Eylül 00:00 = UTC 18 Eylül 21:00; yerel 20 Eylül 00:00 = UTC 19 Eylül 21:00.
    assert start == datetime(2026, 9, 18, 21, 0)
    assert end == datetime(2026, 9, 19, 21, 0)


def test_operational_date_matches_local_calendar_date():
    tz = resolve_airport_timezone(IST_TZ_NAME)
    assert operational_date(tz, datetime(2026, 9, 19, 10, 0)) == date(2026, 9, 19)
    assert operational_date(tz, datetime(2026, 9, 19, 20, 59)) == date(2026, 9, 19)
    assert operational_date(tz, datetime(2026, 9, 19, 21, 0)) == date(2026, 9, 20)  # yerel gece yarısı sınırı


def test_flight_reference_time_uses_own_direction_not_effective_time_offset():
    """
    `flight_reference_time()` `effective_time()` DEĞİLDİR - hiçbir
    -120dk/+15dk offset UYGULANMAZ, sadece flight'ın KENDİ yönünün
    actual>estimated>scheduled önceliği.
    """
    from dataclasses import dataclass

    @dataclass
    class _F:
        direction: str
        dep_scheduled_utc: datetime | None = None
        dep_estimated_utc: datetime | None = None
        dep_actual_utc: datetime | None = None
        arr_scheduled_utc: datetime | None = None
        arr_estimated_utc: datetime | None = None
        arr_actual_utc: datetime | None = None

    dep = _F(direction="departure", dep_scheduled_utc=datetime(2026, 9, 19, 10, 0))
    assert flight_reference_time(dep) == datetime(2026, 9, 19, 10, 0)   # offset YOK

    arr = _F(direction="arrival", arr_scheduled_utc=datetime(2026, 9, 19, 10, 0))
    assert flight_reference_time(arr) == datetime(2026, 9, 19, 10, 0)   # offset YOK


def test_filter_flights_for_operational_day_skips_flights_without_reference_time():
    from dataclasses import dataclass

    @dataclass
    class _F:
        direction: str = "departure"
        dep_scheduled_utc: datetime | None = None
        dep_estimated_utc: datetime | None = None
        dep_actual_utc: datetime | None = None

    tz = resolve_airport_timezone(IST_TZ_NAME)
    broken = _F()
    result = filter_flights_for_operational_day([broken], tz, datetime(2026, 9, 19, 10, 0))
    assert result == []   # crash/uydurma tarih YOK, güvenli şekilde atlandı
