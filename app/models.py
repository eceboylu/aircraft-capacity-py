"""
MADDE 1 - Veritabanı modelleri (SQLAlchemy)
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AircraftCapacity(Base):
    """
    Önbelleklenmiş nihai kapasite değeri. Resolver, AeroLOPA
    katmanlarında sonuç bulamazsa buraya bakar.

    source değerleri:
      - 'verified_dataset'  -> yolcu_ucaklari.json'dan (ana kaynak)
      - 'curated_fallback'  -> elle kürasyon edilmiş yedek değer
    """
    __tablename__ = "aircraft_capacity"

    icao_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="verified_dataset")
    confidence: Mapped[str] = mapped_column(String(16), default="high")
    recalculated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AirlineFleetSeatConfig(Base):

    __tablename__ = "airline_fleet_seat_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    icao_code: Mapped[str] = mapped_column(String(8), index=True)
    airline_iata: Mapped[str | None] = mapped_column(String(3), nullable=True, index=True)
    airline_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    variant_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seats: Mapped[int] = mapped_column(Integer, nullable=False)
    fleet_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_updated_at: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("idx_afsc_icao_airline", "icao_code", "airline_iata"),
    )


class AircraftCapacityFamily(Base):
    """
    Kategori/önek fallback tablosu. Genel havacılık da dahil
    (counts_toward_passenger_total=False olan satırlar).
    """
    __tablename__ = "aircraft_capacity_family"

    icao_prefix: Mapped[str] = mapped_column(String(8), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    default_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    counts_toward_passenger_total: Mapped[bool] = mapped_column(Boolean, default=True)


class UnknownAircraftType(Base):
    """
    Bilinmeyen/tahmini kod takip tablosu. Atomic upsert ile
    beslenir (bkz. service.py flag_unknown), dosya yazma YOK.
    """
    __tablename__ = "unknown_aircraft_types"

    icao_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)