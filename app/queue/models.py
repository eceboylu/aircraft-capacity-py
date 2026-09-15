"""
AŞAMA 8 - Kuyruk tahmin motorunun veritabanı modelleri.

Madde 1'in Base'i import edilir, YENİDEN TANIMLANMAZ - böylece
tek metadata, tek veritabanı, tek create_all.

Şişme koruması (YASAK 4):
  - Flight / QueuePrediction / HistoricalFlightCount: her refresh'te
    session.merge() ile UPSERT, yeni satır açılmaz.
  - FlightEvent: SADECE gerçek bir durum değişikliği tespit
    edildiğinde satır eklenir.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, utcnow


class Airport(Base):
    """flight_airports.sql'den bir kere içe aktarılır."""

    __tablename__ = "airports"

    iata_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    icao_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    airport_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    city_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    country_code: Mapped[str | None] = mapped_column(
        String(4), nullable=True, index=True
    )
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Flight(Base):
    """
    Kaynak A (tarife) + Kaynak B (uçak tipi enrichment) birleşimi.

    flight_key benzersizdir; aynı uçuş tekrar geldiğinde INSERT değil
    UPDATE yapılır.
    """

    __tablename__ = "flights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # "{airline_iata}_{flight_number}_{dep_scheduled_date_utc}"
    flight_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # Hangi havalimanının kapsamında olduğu. Çok-havalimanlı
    # filtreleme için ZORUNLU (YASAK 5).
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)

    direction: Mapped[str] = mapped_column(String(16))   # arrival | departure
    location: Mapped[str] = mapped_column(String(16))    # domestic | international

    airline_iata: Mapped[str | None] = mapped_column(String(8), nullable=True)
    flight_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    flight_iata: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True
    )

    # Kaynak B'den enrichment ile gelir; eşleşme yoksa None kalır.
    aircraft_icao: Mapped[str | None] = mapped_column(String(8), nullable=True)
    aircraft_match_found: Mapped[bool] = mapped_column(Boolean, default=False)

    dep_iata: Mapped[str | None] = mapped_column(String(10), nullable=True)
    arr_iata: Mapped[str | None] = mapped_column(String(10), nullable=True)

    dep_scheduled_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dep_estimated_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dep_actual_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arr_scheduled_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arr_estimated_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arr_actual_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    dep_terminal: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dep_gate: Mapped[str | None] = mapped_column(String(16), nullable=True)
    arr_terminal: Mapped[str | None] = mapped_column(String(16), nullable=True)
    arr_gate: Mapped[str | None] = mapped_column(String(16), nullable=True)

    status: Mapped[str] = mapped_column(String(32))
    last_refreshed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FlightEvent(Base):
    """
    SADECE değişiklik olduğunda satır açılır (YASAK 4).
    30 dakikalık refresh'te otomatik kayıt YAZILMAZ.

    Her gerçek değişiklik AYRI bir satırdır (MADDE 8) - aynı uçuşun
    aynı pencerede birden fazla aircraft change'i varsa (A320->A321,
    sonra A321->A330) her ikisi de burada AYRI satır olarak durur,
    hiçbiri üzerine yazılmaz.

    flight_effective_time (additive/nullable kolon): SADECE
    AIRCRAFT_CHANGED event'lerinde doldurulur - değişikliğin ait
    olduğu uçuşun o anki effective_time()'ıdır (dep/arr scheduled
    değil, mevcut sistemin pencere-atama kuralıyla AYNI fonksiyon).
    Bu, event'in hangi 15 dk prediction window'una düştüğünü ve
    duplicate event tespitini (aynı flight+aynı eski/yeni tip+aynı
    effective_time) belirler. Diğer event tiplerinde (CANCELLED,
    DIVERTED, DELAYED) None kalır - MADDE 8 kapsamı sadece aircraft
    change'dir, bu alan onları etkilemez.

    Şema notu: bu kolon sonradan eklendi, nullable'dır - var olan bir
    production dosyasına ALTER TABLE gerekir (create_all() var olan
    tabloyu değiştirmez); bu repo'daki database.sqlite'ta flight_events
    tablosu hiç oluşturulmamıştı, taşınacak veri yok.
    """

    __tablename__ = "flight_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flight_key: Mapped[str] = mapped_column(String(64), index=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    old_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    flight_effective_time: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AirportOperationalConfig(Base):
    """
    Havalimanı bazlı operasyon modeli.

    Varsayılan değerler ÖRNEK/VARSAYIMDIR, ölçülmüş gerçek veri
    DEĞİLDİR. Her havalimanı için bu tablodan override edilebilir -
    mantık tek bir havalimanına bağlanmaz (YASAK 5).
    """

    __tablename__ = "airport_operational_configs"

    airport_iata: Mapped[str] = mapped_column(String(10), primary_key=True)

    # Kanal sayısı (c). Erlang-C'de sabit kalır.
    passport_counter_count: Mapped[int] = mapped_column(Integer, default=4)
    passport_staff_count: Mapped[int] = mapped_column(Integer, default=8)
    passport_staff_per_counter: Mapped[float] = mapped_column(Float, default=2.0)
    # Personel başına dakikada işlenen yolcu.
    passport_service_rate_per_staff: Mapped[float] = mapped_column(Float, default=0.5)
    # AÇIK VARSAYIM: 2.0 değil 1.5 - iş tam paralelleşmiyor.
    passport_efficiency_multiplier: Mapped[float] = mapped_column(Float, default=1.5)

    arrival_bank_threshold: Mapped[int] = mapped_column(Integer, default=5)


class QueuePrediction(Base):
    """
    Current-state tablosu: her (havalimanı, süreç, pencere) için
    TEK satır. Refresh'te upsert ile güncellenir (YASAK 4).
    """

    __tablename__ = "queue_predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))   # security | passport
    window_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime)

    flight_count: Mapped[int] = mapped_column(Integer, default=0)
    expected_passengers: Mapped[int] = mapped_column(Integer, default=0)
    # MADDE 7: security'de baseline_ratio, flight_ratio ile
    # passenger_ratio'nun ağırlıklı ortalamasıdır. Bileşenler ayrıca
    # saklanır - raporlamada hangi sinyalin tetiklediği görülebilsin.
    # passenger_ratio, geçmiş yolcu verisi yoksa None kalır.
    baseline_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    flight_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    passenger_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Aşağıdaki iki alan SADECE passport için doldurulur.
    # Security'de her ikisi de None kalır (YASAK 1).
    utilization: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_wait_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)

    risk: Mapped[str] = mapped_column(String(16))
    # DetectedReason listesi, JSON string
    reasons: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float, default=0.1)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "airport_iata", "process", "window_start",
            name="uq_queue_prediction_window",
        ),
    )


class HistoricalFlightCount(Base):
    """
    Neden 1 (clustering) ve MADDE 7 (security flight/passenger ratio)
    baseline'ı. Geçmiş veri birikmediyse satır yoktur ve sahte baseline
    ÜRETİLMEZ.

    Yolcu ortalaması AYRI bir örneklem sayacıyla (passenger_sample_size)
    tutulur: bu sütunlar sonradan eklendiği için eski satırlarda yolcu
    verisi YOKTUR. Uçuş örneklemi ile yolcu örneklemini aynı sayaca
    bağlamak, olmayan yolcu gözlemlerini varmış gibi göstererek
    ortalamayı bozardı. passenger_sample_size == 0 iken yolcu baseline'ı
    None'dır ve passenger_ratio hesaplanmaz (uydurulmaz).
    """

    __tablename__ = "historical_flight_counts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))
    hour_of_day: Mapped[int] = mapped_column(Integer)   # 0-23
    day_of_week: Mapped[int] = mapped_column(Integer)   # 0-6
    average_flight_count: Mapped[float] = mapped_column(Float)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    average_expected_passengers: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    passenger_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    last_updated: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "airport_iata", "process", "hour_of_day", "day_of_week",
            name="uq_historical_flight_count",
        ),
    )


class BaselineObservation(Base):
    """
    AŞAMA 3 (MADDE 3) - idempotency defteri.

    HistoricalFlightCount (hour_of_day, day_of_week) bazlı bir HAVUZ
    tutar - aynı saat dilimine düşen birçok farklı tarihin ortalamasını
    biriktirir. Bu tablo ise tek bir somut pencere örneğinin (belirli
    bir airport + process + window_start) o havuza DAHA ÖNCE eklenip
    eklenmediğini tutar.

    UNIQUE constraint bu üçlü üzerindedir - aynı pencere ikinci kez
    kaydedilmeye çalışıldığında DB seviyesinde reddedilir (race/duplicate
    refresh'lere karşı da güvenlidir, sadece application-level kontrol
    değildir).
    """

    __tablename__ = "baseline_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))
    window_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "airport_iata", "process", "window_start",
            name="uq_baseline_observation_window",
        ),
    )
