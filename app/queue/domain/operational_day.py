"""
ADIM (Operational-Day Scope) - Bölüm 58/59/60/61.

SAF KATMAN: veritabanına/dosyaya/ağa dokunmaz, sadece havalimanı
timezone'u + uçuş zaman alanları üzerinde çalışır.

KAVRAM AYRIMI (Bölüm 61 - KARIŞTIRILMAMASI GEREKEN iki şey):
  - FLIGHT SELECTION DATE : bu uçuş HANGİ operasyonel güne ait (kendi
    dep/arr referans zamanının havalimanı YEREL tarihine göre).
    `-120dk`/`+15dk` offset'i UYGULANMADAN, `effective_time()` ile
    KARIŞTIRILMADAN hesaplanır.
  - QUEUE EVENT TIMESTAMP : `effective_time()`'ın ürettiği, offset
    uygulanmış gerçek kuyruk varış anı - bu, flight'ın kendi
    operasyonel gününden farklı bir takvim gününe (önceki veya sonraki)
    taşabilir. Bu modül QUEUE EVENT'İ HİÇ FİLTRELEMEZ - sadece FLIGHT
    SEÇİMİNİ günceller; offset sonrası hangi güne düştüğü `effective_time()`
    zaten var olan, DEĞİŞTİRİLMEYEN mantığıyla serbestçe hesaplanmaya
    devam eder.

BİLİNEN, AÇIKÇA RAPORLANAN SINIRLAMA (gizlenmiyor - bkz. rapor):
  bu filtre SADECE "hangi flight'lar YENİ demand kaynağı" sorusuna
  cevap verir (Bölüm 60 - NEW FLIGHT DEMAND SOURCE). Sistemde şu an
  saatler arası/güne özel PERSISTED bir backlog state'i YOK (önceki
  ADIM'da bilinçli olarak eklenmedi - bkz. o ADIM'ın raporu); tüm
  simülasyon her `run_predictions()` çağrısında verilen flight
  listesinden SIFIRDAN deterministik olarak üretiliyor. Bu, "bugünün
  flight'ı, servisi gece yarısını geçse bile kesilmez" (Bölüm 60,
  QUEUE CARRY - aynı simülasyon çalışması İÇİNDE, DEĞİŞMEDİ) garantisini
  korur; AMA "dünün flight'ının backlog'u BUGÜNE, dünün flight'ı artık
  seçim kapsamı DIŞINA çıktığı için taşınmaz" durumunu TAM olarak
  ÇÖZMEZ - bu, eski "24 saatlik ufuk" sınırlamasıyla AYNI SINIFTAN bir
  risktir ve gerçek çözümü (persisted backlog state) bu ADIM'ın kapsamı
  dışında bırakılmıştır (bkz. rapor).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..constants import DIRECTION_DEPARTURE


def resolve_airport_timezone(timezone_name: str | None) -> ZoneInfo | None:
    """
    Havalimanının IANA timezone adını (`Airport.timezone`, gerçek
    kaynak: `data/flight_airports.sql`'in `customized` JSON alanı)
    çözer.

    Güvenilir bir kaynak YOKSA (alan boş VEYA `zoneinfo` veritabanında
    tanınmıyor - ör. `tzdata` paketi kurulu değilse) None döner;
    UYDURMA bir UTC/offset varsayımı ÜRETİLMEZ - çağıran taraf bunu
    açık bir limitation olarak ele almalı (bkz. `run_predictions`).
    """
    if not timezone_name:
        return None
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def flight_reference_time(flight) -> datetime | None:
    """
    Bu uçuşun KENDİ operasyonel günü hangi zamana göre belirlenir.

    `effective_time()` ile KARIŞTIRILMAZ (Bölüm 61) - burada HİÇBİR
    -120dk/+15dk offset UYGULANMAZ; sadece flight'ın kendi yön'üne
    (departure->dep_*, arrival->arr_*) ait actual>estimated>scheduled
    önceliğidir (delay_minutes()/effective_time() ile AYNI zaman
    kaynağı önceliği - yeni bir kural İCAT EDİLMEDİ).
    """
    if flight.direction == DIRECTION_DEPARTURE:
        return flight.dep_actual_utc or flight.dep_estimated_utc or flight.dep_scheduled_utc
    return flight.arr_actual_utc or flight.arr_estimated_utc or flight.arr_scheduled_utc


def operational_day_window(tz: ZoneInfo, now_utc: datetime, day_offset: int = 0) -> tuple[datetime, datetime]:
    """
    Havalimanının YEREL takvim gününün [start, end) sınırlarını, naive
    UTC datetime olarak döner (Flight tablosundaki alanlarla AYNI
    birim - bkz. `domain_now()` docstring'i, sistemde tüm zaman
    karşılaştırmaları naive UTC üzerinden yapılır).

    `now_utc` naive UTC kabul edilir (mevcut `domain_now()`/`run_
    predictions(now=...)` sözleşmesiyle AYNI). `day_offset`: 0=bugün,
    -1=dün, +1=yarın - sınır-geçişli (Bölüm 61) uçuşları test etmek
    için kullanılır, production çağrısı hep 0 kullanır.
    """
    aware_now = now_utc.replace(tzinfo=timezone.utc)
    local_now = aware_now.astimezone(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_midnight += timedelta(days=day_offset)
    local_next_midnight = local_midnight + timedelta(days=1)

    start_utc = local_midnight.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = local_next_midnight.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def operational_date(tz: ZoneInfo, now_utc: datetime) -> date:
    """Havalimanının YEREL 'bugün' tarihi - loglama/raporlama için."""
    aware_now = now_utc.replace(tzinfo=timezone.utc)
    return aware_now.astimezone(tz).date()


def filter_flights_for_operational_day(flights, tz: ZoneInfo, now_utc: datetime) -> list:
    """
    Bölüm 58 - SADECE bu havalimanının BUGÜNKÜ (yerel) operasyonel
    gününe ait uçuşlar.

    Seçim `flight_reference_time()` (flight'ın KENDİ, offset'siz
    referans zamanı) ile yapılır - `effective_time()`'ın ürettiği
    kaydırılmış kuyruk zaman damgası DEĞİL (Bölüm 61). Bu sayede bir
    uçuş "bugünün uçuşu" olarak seçilir seçilmez, `effective_time()`'ın
    onu -120dk/+15dk ile önceki/sonraki takvim gününe kaydırması
    HİÇ ENGELLENMEZ - sadece SEÇİM aşamasında flight'ın KENDİ günü
    kullanılır.

    Referans zamanı olmayan (hem actual/estimated/scheduled hepsi
    None) uçuşlar güvenli şekilde ATLANIR (mevcut `effective_time()`
    None davranışıyla TUTARLI - crash/uydurma tarih YOK).
    """
    start_utc, end_utc = operational_day_window(tz, now_utc)
    selected = []
    for flight in flights:
        reference = flight_reference_time(flight)
        if reference is None:
            continue
        if start_utc <= reference < end_utc:
            selected.append(flight)
    return selected
