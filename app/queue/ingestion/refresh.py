"""
AŞAMA 8 - Refresh / upsert katmanı (şişme koruması).

İki kuralı birlikte uygular:
  1. Uçuşlar UPSERT edilir. Aynı flight_key tekrar geldiğinde INSERT
     değil UPDATE olur (YASAK 4).
  2. FlightEvent satırı SADECE gerçek bir durum değişikliğinde
     yazılır. Değişiklik yoksa tablo hiç büyümez.

ADIM (MySQL Performance Regression Fix - Phase 1) - Bölüm 1-10:
  ÖNCEKİ tasarım (satır başına 1 SELECT + satır başına 1 COMMIT,
  "production hardening - hata izolasyonu" gerekçesiyle bilinçli
  eklenmişti) 1k ölçekte ölçülen turun ~%60-80'ini oluşturduğu
  (`MYSQL PERFORMANCE REGRESSION AUDIT - 1K`, `refresh_flights`
  23.6-31.1s/37.2-39.7s toplam) doğrulanan bir regresyon olarak
  tespit edildi. Bu ADIM'da:

  - `flight_key` listesi `REFRESH_CHUNK_SIZE`'lık (500 - `retention.py`
    ile AYNI, mevcut proje konvansiyonu) bounded chunk'lara bölünür,
    her chunk için TEK bir bulk `SELECT ... WHERE flight_key IN (...)`
    ile mevcut satırlar `{flight_key: Flight}` sözlüğüne yüklenir -
    satır başına SELECT YOK.
  - Mevcut bir flight_key için, gelen `row` ile DB'deki mevcut alanlar
    KARŞILAŞTIRILIR (`_row_differs_from_existing`) - HİÇBİR alan
    (CANCELLED/DIVERTED terminal-durum kuralı dahil, AŞAĞIDA DEĞİŞMEDİ)
    gerçekten farklı değilse `existing` nesnesinin HİÇBİR attribute'una
    dokunulmaz - SQLAlchemy'nin kendi dirty-tracking'i sayesinde bu
    satır için flush sırasında HİÇBİR SQL UPDATE üretilmez (ekstra bir
    "skip" bayrağı İCAT EDİLMEDİ, ORM'un ZATEN var olan davranışına
    güvenilir).
  - Normal (hatasız) yol: chunk'ın TAMAMI bellekte işlenir, TEK bir
    `session.flush()` + TEK bir `session.commit()` (chunk başına, satır
    başına DEĞİL).
  - Hata izolasyonu KORUNUR (kaybolmaz) - ama artık normal yolu
    YAVAŞLATMAZ: `flush()` bir hata fırlatırsa (bozuk veri/constraint
    ihlali) TÜM chunk `rollback()` edilir, SONRA SADECE O chunk için
    satır-satır bir fallback'e (her satır kendi SAVEPOINT'inde -
    `session.begin_nested()`) geçilir - iyi satırlar KORUNUR, kötü
    satır(lar) izole edilip loglanır, chunk sonunda TEK bir gerçek
    COMMIT ile kapanır. Bu fallback SADECE bir chunk'ta GERÇEKTEN bir
    hata olduğunda çalışır - normal/başarılı yolun maliyetine HİÇ
    eklenmez.
  - FlightEvent tespiti (`_detect_changes`) DEĞİŞMEDEN, `existing`
    HENÜZ mutasyona UĞRAMADAN ÖNCE (eski davranışla BİREBİR AYNI sırada)
    çağrılır - event semantics (DELAYED eşiği, AIRCRAFT_CHANGED,
    CANCELLED/DIVERTED, duplicate-event koruması) HİÇ değişmedi.

  ÖNEMLİ (geriye dönük uyumluluk - Bölüm 10): `refresh_flights()`'ın
  dönüş sözleşmesi (`inserted`/`updated`/`events_written`/`failed`)
  DEĞİŞMEDİ - `updated`, ESKİ semantiğiyle AYNI şekilde "mevcut bir
  flight_key'e karşılık gelen HER satır" sayılır (satırın DB'de GERÇEK
  bir SQL UPDATE'e yol açıp açmadığından BAĞIMSIZ) - bu, mevcut
  testlerin (`test_e2e_refresh.py`, `test_queue_state_and_refresh_
  delta.py`, `test_airlabs_refresh_timezone_contract.py` vb.) ZATEN bu
  semantiğe göre yazılmış olduğu doğrulandıktan SONRA BİLİNÇLİ olarak
  korunan bir karardır - "her refresh mevcut TÜM flight'ları upsert
  eder" raporlama sözleşmesi DEĞİŞMEDİ, SADECE altındaki GERÇEK SQL
  yazma davranışı optimize edildi.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..constants import (
    DELAY_SIGNIFICANT_MINUTES,
    EVENT_AIRCRAFT_CHANGED,
    EVENT_CANCELLED,
    EVENT_DELAYED,
    EVENT_DIVERTED,
    EXCLUDED_STATUSES,
    STATUS_CANCELLED,
    STATUS_DIVERTED,
)
from ..domain.demand import effective_time
from ..domain.flight_rules import delay_minutes
from ..models import Flight, FlightEvent

logger = logging.getLogger(__name__)

# ADIM (MySQL Performance Regression Fix - Phase 1) - `retention.py:
# BATCH_SIZE` ile AYNI değer/AYNI gerekçe: bir `IN (...)` ifadesine
# flight_key listesini güvenle sığdıracak, gereksiz yere büyük olmayan
# bir batch boyutu - rastgele seçilmiş bir sayı değil, mevcut proje
# konvansiyonu.
REFRESH_CHUNK_SIZE = 500

# Flight'ın `row` (Kaynak A/B birleşimi) dict'inden gelen, karşılaştırma/
# güncelleme kapsamındaki TÜM mutable alanlar - `id`/`flight_key`/
# `last_refreshed_at` HARİÇ (ilk ikisi identity, üçüncüsü HER ZAMAN
# "now"a eşitlenir - "içerik değişti mi" karşılaştırmasının PARÇASI
# DEĞİLDİR, bkz. `_row_differs_from_existing`).
_MUTABLE_FLIGHT_FIELDS = (
    "airport_iata", "direction", "location", "requires_passport",
    "airline_iata", "flight_number",
    "flight_iata", "aircraft_icao", "aircraft_match_found", "dep_iata", "arr_iata",
    "dep_scheduled_utc", "dep_estimated_utc", "dep_actual_utc",
    "arr_scheduled_utc", "arr_estimated_utc", "arr_actual_utc",
    "dep_terminal", "dep_gate", "arr_terminal", "arr_gate",
)


class _RowView:
    """
    delay_minutes()/effective_time() gibi saf fonksiyonları ham sözlük
    üzerinde çalıştırabilmek için hafif sarmalayıcı.
    """

    def __init__(self, row: dict):
        self.__dict__.update(row)


def _detect_changes(
    existing: Flight, row: dict
) -> list[tuple[str, str | None, str | None, datetime | None]]:
    """
    Mevcut kayıt ile yeni veriyi karşılaştırır.
    Sadece GERÇEK değişiklikler döner; aynı veri tekrar gelirse
    boş liste döner ve hiçbir event yazılmaz.

    Dönen 4'lü: (event_type, old_value, new_value, flight_effective_time).
    flight_effective_time SADECE AIRCRAFT_CHANGED için doldurulur
    (MADDE 8 - event'in hangi prediction window'una düştüğünü belirler);
    diğer event tiplerinde None'dır, bu MADDE'nin kapsamı dışındadır.
    """
    events: list[tuple[str, str | None, str | None, datetime | None]] = []

    old_icao = existing.aircraft_icao
    new_icao = row.get("aircraft_icao")
    if old_icao and new_icao and old_icao != new_icao:
        events.append((
            EVENT_AIRCRAFT_CHANGED, old_icao, new_icao,
            effective_time(_RowView(row)),
        ))

    old_status = (existing.status or "").lower()
    new_status = (row.get("status") or "").lower()
    if old_status != new_status:
        if new_status == STATUS_CANCELLED:
            events.append((EVENT_CANCELLED, old_status, new_status, None))
        elif new_status == STATUS_DIVERTED:
            events.append((EVENT_DIVERTED, old_status, new_status, None))

    old_delay = delay_minutes(existing)
    new_delay = delay_minutes(_RowView(row))
    crossed_threshold = (
        new_delay > DELAY_SIGNIFICANT_MINUTES
        and old_delay <= DELAY_SIGNIFICANT_MINUTES
    )
    grew_further = (
        new_delay > DELAY_SIGNIFICANT_MINUTES
        and old_delay > DELAY_SIGNIFICANT_MINUTES
        and new_delay != old_delay
    )
    if crossed_threshold or grew_further:
        events.append((EVENT_DELAYED, str(old_delay), str(new_delay), None))

    return events


def _aircraft_change_already_recorded(
    session,
    flight_key: str,
    old_icao: str | None,
    new_icao: str | None,
    flight_effective_time: datetime | None,
) -> bool:
    """
    MADDE 8 kural 3 - duplicate event koruması.

    Aynı flight + aynı eski/yeni tip + aynı effective_time ile bir
    AIRCRAFT_CHANGED event daha önce yazılmışsa True döner; refresh
    aynı kaynak satırını tekrar işlese bile ikinci bir kayıt açılmaz.
    Session'ın autoflush davranışı sayesinde bu döngü içinde henüz
    commit edilmemiş (ama flush edilmiş) event'ler de bu sorguda görünür.
    """
    existing = session.execute(
        select(FlightEvent.id).where(
            FlightEvent.flight_key == flight_key,
            FlightEvent.event_type == EVENT_AIRCRAFT_CHANGED,
            FlightEvent.old_value == old_icao,
            FlightEvent.new_value == new_icao,
            FlightEvent.flight_effective_time == flight_effective_time,
        )
    ).first()
    return existing is not None


def _resolved_status(existing: Flight | None, row: dict) -> str | None:
    """
    BUG-01 düzeltmesi - DEĞİŞMEDİ: CANCELLED/DIVERTED TERMİNAL bir
    durumdur; daha ESKİ/gecikmeli bir kaynak satırı (ör. active/
    scheduled) bu durumu asla geri ALAMAZ (resurrect edemez).
    """
    new_status = row.get("status")
    if existing is not None:
        old_status = (existing.status or "").lower()
        if old_status in EXCLUDED_STATUSES and (new_status or "").lower() not in EXCLUDED_STATUSES:
            return existing.status
    return new_status


def _row_differs_from_existing(existing: Flight, row: dict, resolved_status) -> bool:
    """
    ADIM (MySQL Performance Regression Fix - Phase 1) - `existing`in
    HİÇBİR attribute'una dokunmadan ÖNCE, gerçekten bir içerik farkı
    olup olmadığını kontrol eder - `last_refreshed_at` bu karşılaştırmanın
    PARÇASI DEĞİLDİR (HER ZAMAN "now"a eşitlenir, "içerik" sayılmaz).
    """
    if (existing.status or None) != resolved_status:
        return True
    for name in _MUTABLE_FLIGHT_FIELDS:
        if getattr(existing, name) != row.get(name):
            return True
    return False


def _apply_row_to_flight(flight: Flight, row: dict, resolved_status, now: datetime) -> None:
    """`flight` (var olan VEYA yeni oluşturulan) nesnesine `row`'un tüm alanlarını yazar."""
    for name in _MUTABLE_FLIGHT_FIELDS:
        setattr(flight, name, row.get(name))
    flight.status = resolved_status
    flight.last_refreshed_at = now


def _detect_and_stage_events(session, existing: Flight, row: dict, now: datetime) -> int:
    """`_detect_changes`'i (DEĞİŞTİRİLMEDİ) çağırır, duplicate-korumalı FlightEvent'leri session'a ekler. `existing` HENÜZ mutasyona uğramamış olmalı."""
    flight_key = row["flight_key"]
    written = 0
    for event_type, old_value, new_value, flight_eff_time in _detect_changes(existing, row):
        if event_type == EVENT_AIRCRAFT_CHANGED and _aircraft_change_already_recorded(
            session, flight_key, old_value, new_value, flight_eff_time
        ):
            continue
        session.add(FlightEvent(
            flight_key=flight_key,
            airport_iata=row["airport_iata"],
            event_type=event_type,
            old_value=old_value,
            new_value=new_value,
            flight_effective_time=flight_eff_time,
            detected_at=now,
        ))
        written += 1
    return written


def _process_one_row(session, existing_by_key: dict, row: dict, now: datetime) -> dict:
    """
    Tek bir satırı `existing_by_key` (bellek içi cache) karşısında
    işler - HİÇBİR SELECT/COMMIT yapmaz (çağıran taraf bulk-fetch'i ve
    commit'i yönetir). Hem optimistic toplu yol hem de hata-sonrası
    satır-satır fallback tarafından ORTAK kullanılır - iki yerde
    KOPYALANMAMIŞ tek bir mantık.
    """
    flight_key = row["flight_key"]
    existing = existing_by_key.get(flight_key)

    if existing is None:
        flight = Flight(flight_key=flight_key)
        resolved_status = _resolved_status(None, row)
        _apply_row_to_flight(flight, row, resolved_status, now)
        session.add(flight)
        existing_by_key[flight_key] = flight
        return {"inserted": 1, "updated": 0, "events_written": 0}

    resolved_status = _resolved_status(existing, row)
    events_written = _detect_and_stage_events(session, existing, row, now)
    if _row_differs_from_existing(existing, row, resolved_status):
        _apply_row_to_flight(existing, row, resolved_status, now)
    # İçerik gerçekten AYNIYSA `existing`'in HİÇBİR attribute'una
    # dokunulmadı - SQLAlchemy'nin dirty-tracking'i sayesinde flush
    # sırasında bu satır için HİÇBİR SQL UPDATE üretilmez. `updated`
    # sayacı YİNE DE +1 - ESKİ/test edilmiş raporlama sözleşmesi
    # (bkz. modül docstring'i) KORUNUYOR.
    return {"inserted": 0, "updated": 1, "events_written": events_written}


def _bulk_fetch_existing(session, flight_keys: list[str]) -> dict:
    if not flight_keys:
        return {}
    rows = session.execute(
        select(Flight).where(Flight.flight_key.in_(flight_keys))
    ).scalars().all()
    return {f.flight_key: f for f in rows}


def _process_chunk_serial_with_savepoints(session, chunk: list[dict], now: datetime) -> dict:
    """
    ADIM (MySQL Performance Regression Fix - Phase 1) - Bölüm 6/7 - SADECE
    optimistic toplu yol (aşağıda) bir hata fırlattığında devreye girer
    (nadir yol). Bu chunk'ı satır satır, her satırı KENDİ SAVEPOINT'inde
    (`session.begin_nested()`) işler - kötü satır sadece KENDİ savepoint'i
    geri alınarak izole edilir, iyi satırlar KORUNUR - ama YİNE DE
    chunk sonunda TEK bir GERÇEK `session.commit()` yeterlidir (satır
    başına gerçek COMMIT'e GERİ DÖNÜLMEZ - SAVEPOINT gerçek commit
    DEĞİLDİR).
    """
    keys = [row["flight_key"] for row in chunk]
    existing_by_key = _bulk_fetch_existing(session, keys)

    inserted = updated = events_written = failed = 0
    for row in chunk:
        savepoint = session.begin_nested()
        try:
            result = _process_one_row(session, existing_by_key, row, now)
            session.flush()
        except Exception:
            savepoint.rollback()
            failed += 1
            logger.exception(
                "Uçuş kaydı işlenemedi, bu satır atlanıyor (flight_key=%s)",
                row.get("flight_key", "?"),
            )
            continue
        savepoint.commit()
        inserted += result["inserted"]
        updated += result["updated"]
        events_written += result["events_written"]

    session.commit()
    return {"inserted": inserted, "updated": updated, "events_written": events_written, "failed": failed}


def _process_chunk(session, chunk: list[dict], now: datetime) -> dict:
    """
    ADIM (MySQL Performance Regression Fix - Phase 1) - Bölüm 1/5/6 -
    normal (hatasız) yol: TEK bulk SELECT + bellekte diff/upsert + TEK
    flush + TEK commit. `flush()` bir hata fırlatırsa (bozuk veri/
    constraint ihlali - hangi türden bilinemez, bu yüzden bilinçli
    geniş `except Exception`) TÜM chunk `rollback()` edilir ve SADECE
    O ZAMAN, SADECE bu chunk için satır-satır SAVEPOINT fallback'ine
    geçilir (`_process_chunk_serial_with_savepoints`) - böylece hem
    normal yol hızlı kalır HEM DE hata izolasyonu (eski davranışla AYNI
    garanti: bir kötü satır diğer satırları asla düşürmez) korunur.
    """
    keys = [row["flight_key"] for row in chunk]
    existing_by_key = _bulk_fetch_existing(session, keys)

    inserted = updated = events_written = 0
    try:
        for row in chunk:
            result = _process_one_row(session, existing_by_key, row, now)
            inserted += result["inserted"]
            updated += result["updated"]
            events_written += result["events_written"]
        session.flush()
    except Exception:
        session.rollback()
        logger.warning(
            "refresh chunk toplu yol başarısız oldu (%d satır) - satır-satır "
            "SAVEPOINT fallback'ine geçiliyor (izolasyon korunuyor, hız düşüyor)",
            len(chunk),
        )
        return _process_chunk_serial_with_savepoints(session, chunk, now)

    session.commit()
    return {"inserted": inserted, "updated": updated, "events_written": events_written, "failed": 0}


def refresh_flights(session, rows: list[dict]) -> dict:
    """
    Uçuşları upsert eder ve tespit edilen değişiklikleri FlightEvent
    olarak kaydeder - ADIM (MySQL Performance Regression Fix - Phase 1)
    SONRASI: `REFRESH_CHUNK_SIZE`'lık bounded chunk'lar halinde, bulk
    SELECT + normal yolda chunk başına TEK commit (bkz. modül
    docstring'i ve `_process_chunk`).

    Hata izolasyonu (production hardening - DEĞİŞMEDİ, sadece
    UYGULANMA ŞEKLİ değişti): bir chunk'ın toplu işlenmesi sırasında
    HERHANGİ bir hata oluşursa, o chunk satır-satır SAVEPOINT'li
    fallback'e düşer - önceki chunk'lar zaten commit edildiği için
    ETKİLENMEZ, bu chunk içindeki iyi satırlar KORUNUR, sadece gerçekten
    bozuk olan satır(lar) "failed" sayacına eklenip atlanır, kalan
    chunk'ların işlenmesine devam edilir.

    Dönen özet ESKİ sözleşmeyle BİREBİR AYNI: kaç satır eklendi/
    güncellendi (bkz. modül docstring'i - `updated` HER mevcut satırı
    sayar, sadece GERÇEK SQL UPDATE alanları DEĞİL), kaç event yazıldı,
    kaç satır atlandı (failed).
    """
    now = datetime.now(timezone.utc)
    inserted = updated = events_written = failed = 0

    for start in range(0, len(rows), REFRESH_CHUNK_SIZE):
        chunk = rows[start:start + REFRESH_CHUNK_SIZE]
        result = _process_chunk(session, chunk, now)
        inserted += result["inserted"]
        updated += result["updated"]
        events_written += result["events_written"]
        failed += result["failed"]

    return {
        "inserted": inserted,
        "updated": updated,
        "events_written": events_written,
        "failed": failed,
    }


def aircraft_changes_for_airport(
    session, airport_iata: str
) -> dict[str, list[tuple[str | None, str | None, datetime | None]]]:
    """
    Neden 8 girdisi: bu havalimanındaki AIRCRAFT_CHANGED olayları.

    MADDE 8: aynı uçuşun TÜM değişimleri korunur - hiçbiri üzerine
    yazılmaz. Değer: {flight_key: [(old, new, flight_effective_time), ...]}
    kronolojik sırayla (effective_time, sonra tespit sırası ile).
    """
    rows = session.execute(
        select(FlightEvent)
        .where(
            FlightEvent.airport_iata == airport_iata,
            FlightEvent.event_type == EVENT_AIRCRAFT_CHANGED,
        )
        .order_by(FlightEvent.flight_effective_time, FlightEvent.detected_at)
    ).scalars().all()

    changes: dict[str, list[tuple[str | None, str | None, datetime | None]]] = {}
    for event in rows:
        changes.setdefault(event.flight_key, []).append(
            (event.old_value, event.new_value, event.flight_effective_time)
        )
    return changes
