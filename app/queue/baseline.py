"""
Neden 1 (clustering) için geçmiş ortalama uçuş sayısı.

Geçmiş veri birikmemişse None döner. SAHTE BASELINE ÜRETİLMEZ -
bu durumda security riski "UNKNOWN" olur, clustering nedeni atlanır.

MADDE 3 - idempotency:
HistoricalFlightCount, (airport, process, hour_of_day, day_of_week)
bazlı bir HAVUZDUR - aynı saat dilimine düşen birçok farklı tarihin
ortalamasını biriktirir. Bu havuza aynı somut pencerenin (aynı
airport+process+window_start) birden fazla kez eklenmesini önlemek
için BaselineObservation deftere ayrı bir kayıt düşülür; defterde
zaten varsa bu çağrı NO-OP'tur (havuz bir daha güncellenmez).
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .models import BaselineObservation, HistoricalFlightCount


def _bucket(
    session,
    airport_iata: str,
    process: str,
    hour_of_day: int,
    day_of_week: int,
) -> HistoricalFlightCount | None:
    return session.execute(
        select(HistoricalFlightCount).where(
            HistoricalFlightCount.airport_iata == airport_iata,
            HistoricalFlightCount.process == process,
            HistoricalFlightCount.hour_of_day == hour_of_day,
            HistoricalFlightCount.day_of_week == day_of_week,
        )
    ).scalar_one_or_none()


def get_baseline(
    session,
    airport_iata: str,
    process: str,
    hour_of_day: int,
    day_of_week: int,
) -> float | None:
    """
    Bu havalimanı + süreç + saat + gün için geçmiş ortalama UÇUŞ sayısı.
    Kayıt yoksa veya örneklem boşsa None.
    """
    row = _bucket(session, airport_iata, process, hour_of_day, day_of_week)

    if row is None or row.sample_size <= 0:
        return None
    return row.average_flight_count


def get_passenger_baseline(
    session,
    airport_iata: str,
    process: str,
    hour_of_day: int,
    day_of_week: int,
) -> float | None:
    """
    MADDE 7 - geçmiş ortalama YOLCU talebi.

    Yolcu örneklemi uçuş örnekleminden AYRI sayılır: bu sütunlar
    sisteme sonradan eklendiği için eski satırlarda yolcu verisi
    yoktur. Böyle bir satırda passenger_sample_size 0'dır ve burada
    None döner - sahte geçmiş yolcu verisi ÜRETİLMEZ. Çağıran taraf
    bu durumda passenger_ratio'yu hesaplamaz.
    """
    row = _bucket(session, airport_iata, process, hour_of_day, day_of_week)

    if row is None or row.passenger_sample_size <= 0:
        return None
    return row.average_expected_passengers


def record_observation(
    session,
    airport_iata: str,
    process: str,
    window_start: datetime,
    hour_of_day: int,
    day_of_week: int,
    flight_count: int,
    expected_passengers: int | None = None,
) -> float:
    """
    Bu somut pencereyi (airport+process+window_start) hareketli
    ortalamaya ekler ve güncel UÇUŞ ortalamasını döndürür.

    expected_passengers verilirse yolcu ortalaması da AYRI bir
    örneklem sayacıyla güncellenir (bkz. get_passenger_baseline).
    None ise yolcu havuzuna hiç dokunulmaz - o pencere için yolcu
    talebi bilinmiyordur, sıfır sayılmaz.

    Idempotency: window_start için BaselineObservation defterine önce
    bir satır yazılmaya çalışılır. Aynı üçlü (airport, process,
    window_start) DAHA ÖNCE kaydedilmişse - unique constraint DB
    seviyesinde reddeder (IntegrityError) - havuz GÜNCELLENMEZ, mevcut
    ortalama olduğu gibi döner. Bu, aynı sorguyu aynı anda gönderen iki
    refresh (race condition) durumunda da tekilliği garanti eder;
    sadece uygulama seviyesinde bir "var mı?" kontrolü değildir.
    """
    now = datetime.now(timezone.utc)

    session.add(BaselineObservation(
        airport_iata=airport_iata,
        process=process,
        window_start=window_start,
        recorded_at=now,
    ))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = _bucket(
            session, airport_iata, process, hour_of_day, day_of_week
        )
        return existing.average_flight_count if existing else float(flight_count)

    existing = _bucket(session, airport_iata, process, hour_of_day, day_of_week)

    if existing is None:
        session.merge(HistoricalFlightCount(
            airport_iata=airport_iata,
            process=process,
            hour_of_day=hour_of_day,
            day_of_week=day_of_week,
            average_flight_count=float(flight_count),
            sample_size=1,
            average_expected_passengers=(
                float(expected_passengers)
                if expected_passengers is not None else None
            ),
            passenger_sample_size=1 if expected_passengers is not None else 0,
            last_updated=now,
        ))
        session.commit()
        return float(flight_count)

    total = existing.average_flight_count * existing.sample_size + flight_count
    new_sample_size = existing.sample_size + 1
    new_average = total / new_sample_size

    existing.average_flight_count = new_average
    existing.sample_size = new_sample_size

    if expected_passengers is not None:
        old_passenger_total = (
            (existing.average_expected_passengers or 0.0)
            * existing.passenger_sample_size
        )
        new_passenger_sample = existing.passenger_sample_size + 1
        existing.average_expected_passengers = (
            (old_passenger_total + expected_passengers) / new_passenger_sample
        )
        existing.passenger_sample_size = new_passenger_sample

    existing.last_updated = now
    session.commit()

    return new_average
