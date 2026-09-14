"""
Neden 1 (clustering) için geçmiş ortalama uçuş sayısı.

Geçmiş veri birikmemişse None döner. SAHTE BASELINE ÜRETİLMEZ -
bu durumda security riski "UNKNOWN" olur, clustering nedeni atlanır.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from .models import HistoricalFlightCount


def get_baseline(
    session,
    airport_iata: str,
    process: str,
    hour_of_day: int,
    day_of_week: int,
) -> float | None:
    """
    Bu havalimanı + süreç + saat + gün için geçmiş ortalama.
    Kayıt yoksa veya örneklem boşsa None.
    """
    row = session.execute(
        select(HistoricalFlightCount).where(
            HistoricalFlightCount.airport_iata == airport_iata,
            HistoricalFlightCount.process == process,
            HistoricalFlightCount.hour_of_day == hour_of_day,
            HistoricalFlightCount.day_of_week == day_of_week,
        )
    ).scalar_one_or_none()

    if row is None or row.sample_size <= 0:
        return None
    return row.average_flight_count


def record_observation(
    session,
    airport_iata: str,
    process: str,
    hour_of_day: int,
    day_of_week: int,
    flight_count: int,
) -> float:
    """
    Gözlemi hareketli ortalamaya ekler ve yeni ortalamayı döndürür.

    Satır UPSERT edilir; her gözlem için yeni satır AÇILMAZ (YASAK 4).
    """
    existing = session.execute(
        select(HistoricalFlightCount).where(
            HistoricalFlightCount.airport_iata == airport_iata,
            HistoricalFlightCount.process == process,
            HistoricalFlightCount.hour_of_day == hour_of_day,
            HistoricalFlightCount.day_of_week == day_of_week,
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)

    if existing is None:
        session.merge(HistoricalFlightCount(
            airport_iata=airport_iata,
            process=process,
            hour_of_day=hour_of_day,
            day_of_week=day_of_week,
            average_flight_count=float(flight_count),
            sample_size=1,
            last_updated=now,
        ))
        session.commit()
        return float(flight_count)

    total = existing.average_flight_count * existing.sample_size + flight_count
    new_sample_size = existing.sample_size + 1
    new_average = total / new_sample_size

    existing.average_flight_count = new_average
    existing.sample_size = new_sample_size
    existing.last_updated = now
    session.commit()

    return new_average
