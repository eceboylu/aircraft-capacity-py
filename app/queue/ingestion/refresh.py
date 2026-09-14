"""
AŞAMA 8 - Refresh / upsert katmanı (şişme koruması).

İki kuralı birlikte uygular:
  1. Uçuşlar session.merge() ile UPSERT edilir. Aynı flight_key
     tekrar geldiğinde INSERT değil UPDATE olur (YASAK 4).
  2. FlightEvent satırı SADECE gerçek bir durum değişikliğinde
     yazılır. Değişiklik yoksa tablo hiç büyümez.
"""

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
from ..domain.flight_rules import delay_minutes
from ..models import Flight, FlightEvent


class _RowView:
    """
    delay_minutes() gibi saf fonksiyonları ham sözlük üzerinde
    çalıştırabilmek için hafif sarmalayıcı.
    """

    def __init__(self, row: dict):
        self.__dict__.update(row)


def _detect_changes(existing: Flight, row: dict) -> list[tuple[str, str | None, str | None]]:
    """
    Mevcut kayıt ile yeni veriyi karşılaştırır.
    Sadece GERÇEK değişiklikler döner; aynı veri tekrar gelirse
    boş liste döner ve hiçbir event yazılmaz.
    """
    events: list[tuple[str, str | None, str | None]] = []

    old_icao = existing.aircraft_icao
    new_icao = row.get("aircraft_icao")
    if old_icao and new_icao and old_icao != new_icao:
        events.append((EVENT_AIRCRAFT_CHANGED, old_icao, new_icao))

    old_status = (existing.status or "").lower()
    new_status = (row.get("status") or "").lower()
    if old_status != new_status:
        if new_status == STATUS_CANCELLED:
            events.append((EVENT_CANCELLED, old_status, new_status))
        elif new_status == STATUS_DIVERTED:
            events.append((EVENT_DIVERTED, old_status, new_status))

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
        events.append((EVENT_DELAYED, str(old_delay), str(new_delay)))

    return events


def refresh_flights(session, rows: list[dict]) -> dict:
    """
    Uçuşları upsert eder ve tespit edilen değişiklikleri
    FlightEvent olarak kaydeder.

    Dönen özet: kaç satır eklendi/güncellendi, kaç event yazıldı.
    """
    now = datetime.now(timezone.utc)
    inserted = 0
    updated = 0
    events_written = 0

    for row in rows:
        flight_key = row["flight_key"]
        existing = session.execute(
            select(Flight).where(Flight.flight_key == flight_key)
        ).scalar_one_or_none()

        if existing is None:
            inserted += 1
        else:
            updated += 1
            for event_type, old_value, new_value in _detect_changes(existing, row):
                session.add(FlightEvent(
                    flight_key=flight_key,
                    airport_iata=row["airport_iata"],
                    event_type=event_type,
                    old_value=old_value,
                    new_value=new_value,
                    detected_at=now,
                ))
                events_written += 1

        payload = dict(row)
        payload["last_refreshed_at"] = now
        if existing is not None:
            payload["id"] = existing.id

        session.merge(Flight(**payload))

    session.commit()
    return {
        "inserted": inserted,
        "updated": updated,
        "events_written": events_written,
    }


def aircraft_changes_for_airport(session, airport_iata: str) -> dict[str, tuple[str | None, str | None]]:
    """
    Neden 8 girdisi: bu havalimanındaki AIRCRAFT_CHANGED olayları.
    Aynı uçuşun birden çok değişimi varsa en sonuncusu kullanılır.
    """
    rows = session.execute(
        select(FlightEvent)
        .where(
            FlightEvent.airport_iata == airport_iata,
            FlightEvent.event_type == EVENT_AIRCRAFT_CHANGED,
        )
        .order_by(FlightEvent.detected_at)
    ).scalars().all()

    return {e.flight_key: (e.old_value, e.new_value) for e in rows}
