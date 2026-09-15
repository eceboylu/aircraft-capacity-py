"""
AŞAMA 8 - Refresh / upsert katmanı (şişme koruması).

İki kuralı birlikte uygular:
  1. Uçuşlar session.merge() ile UPSERT edilir. Aynı flight_key
     tekrar geldiğinde INSERT değil UPDATE olur (YASAK 4).
  2. FlightEvent satırı SADECE gerçek bir durum değişikliğinde
     yazılır. Değişiklik yoksa tablo hiç büyümez.

Production hardening - hata izolasyonu:
  Her satır KENDİ transaction'ında commit edilir (tek satırlık işlem
  birimi). Bir satır işlenirken herhangi bir hata oluşursa (bozuk
  veri, beklenmeyen tip, DB constraint ihlali) SADECE o satır
  session.rollback() ile geri alınır ve loglanır; önceki satırlar
  zaten commit edilmiş olduğu için ETKİLENMEZ, kalan satırların
  işlenmesine devam edilir. Bu, tek bozuk bir kaydın tüm refresh
  turunu düşürmesini engeller.
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
    STATUS_CANCELLED,
    STATUS_DIVERTED,
)
from ..domain.demand import effective_time
from ..domain.flight_rules import delay_minutes
from ..models import Flight, FlightEvent

logger = logging.getLogger(__name__)


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


def _refresh_one_row(session, row: dict, now: datetime) -> dict:
    """
    Tek bir satırı upsert eder + event tespiti yapar ve COMMIT eder.

    Bu fonksiyonun kendi commit'i olması bilinçlidir: satır bazlı hata
    izolasyonu, ancak her satır kendi transaction sınırına sahipse
    mümkündür - aksi halde bir satırdaki hata için yapılan
    session.rollback() önceki (henüz commit edilmemiş) satırları da
    geri alırdı.
    """
    flight_key = row["flight_key"]
    existing = session.execute(
        select(Flight).where(Flight.flight_key == flight_key)
    ).scalar_one_or_none()

    events_written = 0

    if existing is None:
        inserted = 1
        updated = 0
    else:
        inserted = 0
        updated = 1
        for event_type, old_value, new_value, flight_eff_time in _detect_changes(
            existing, row
        ):
            if event_type == EVENT_AIRCRAFT_CHANGED and (
                _aircraft_change_already_recorded(
                    session, flight_key, old_value, new_value, flight_eff_time
                )
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
            events_written += 1

    payload = dict(row)
    payload["last_refreshed_at"] = now
    if existing is not None:
        payload["id"] = existing.id

    session.merge(Flight(**payload))
    session.commit()

    return {"inserted": inserted, "updated": updated, "events_written": events_written}


def refresh_flights(session, rows: list[dict]) -> dict:
    """
    Uçuşları upsert eder ve tespit edilen değişiklikleri
    FlightEvent olarak kaydeder.

    Hata izolasyonu (production hardening): her satır AYRI commit
    edilir. Bir satırın işlenmesi sırasında herhangi bir hata
    (bozuk/eksik alan, beklenmeyen tip, DB constraint ihlali vb.)
    oluşursa:
      - session.rollback() ile SADECE o satırın işlemi geri alınır
        (önceki satırlar zaten commit edildiği için etkilenmez),
      - hata logger.exception() ile (traceback dahil) loglanır,
      - o satır "failed" sayacına eklenip atlanır,
      - kalan satırların işlenmesine devam edilir.

    Burada bilinçli olarak geniş bir `except Exception` kullanılır -
    bu satır işleme sınırı için TASARLANMIŞ bir izolasyon noktasıdır
    (hangi türden bir hata geleceği önceden bilinemez), sessizce
    yutulmaz (loglanır + sayılır) ve DB transaction'ını bozulmadan
    bırakır. Kodun geri kalanındaki spesifik except yaklaşımı
    (IntegrityError, ValueError, ...) burada DEĞİŞTİRİLMEDİ.

    Dönen özet: kaç satır eklendi/güncellendi, kaç event yazıldı,
    kaç satır atlandı (failed).
    """
    now = datetime.now(timezone.utc)
    inserted = 0
    updated = 0
    events_written = 0
    failed = 0

    for row in rows:
        try:
            result = _refresh_one_row(session, row, now)
        except Exception:
            session.rollback()
            failed += 1
            logger.exception(
                "Uçuş kaydı işlenemedi, bu satır atlanıyor (flight_key=%s)",
                row.get("flight_key", "?"),
            )
            continue

        inserted += result["inserted"]
        updated += result["updated"]
        events_written += result["events_written"]

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
