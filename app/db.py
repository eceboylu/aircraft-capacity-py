"""
Veritabanı bağlantısı.

Canlıya geçince: DATABASE_URL ortam değişkenini değiştir
    postgresql://user:pass@host:5432/dbname
    mysql+pymysql://user:pass@host:3306/dbname

"""

import os

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DEFAULT_SQLITE_PATH = os.path.join(os.path.dirname(__file__), "..", "database.sqlite")
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DEFAULT_SQLITE_PATH}")

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)


# Minimum, additive SQLite migration. `Base.metadata.create_all()` mevcut
# tabloya yeni kolon eklemez; production DB'yi drop/reset etmeden yeni açık
# service-time config'ini taşımanın güvenli yolu eksik kolonları tek tek
# eklemektir. Derived capacity değerleri persist edilmez.
_SQLITE_OPERATIONAL_CONFIG_COLUMNS = {
    "passport_service_time_minutes": "FLOAT NOT NULL DEFAULT 1.5",
    "security_lane_count": "INTEGER NOT NULL DEFAULT 8",
    "security_service_time_minutes": "FLOAT NOT NULL DEFAULT 1.0",
    # ADIM (Domestic/International Security Lane Ayrımı): mevcut
    # `security_lane_count` ile AYNI varsayılan (8) - mevcut satırlar
    # için davranış değişmez, sadece airport-bazlı ayrı ayarlanabilir
    # yeni kolonlar eklenir.
    "domestic_security_lane_count": "INTEGER NOT NULL DEFAULT 8",
    "international_security_lane_count": "INTEGER NOT NULL DEFAULT 8",
    # ADIM (Airport-Scale Queue Capacity) - NULLABLE, DEFAULT YOK: None,
    # "bu airport için özellikle set edilmedi" anlamına gelir (bkz.
    # models.py) - mevcut satırlar NULL alır, scale-derived değere
    # düşer, davranışları DEĞİŞMEZ.
    "passport_departure_server_count": "INTEGER",
    "passport_arrival_server_count": "INTEGER",
}

# ADIM (Operational-Day Scope) `Airport.timezone` bu tabloya SONRADAN
# eklendi - `create_all()` var olan `airports` tablosuna yeni kolon
# eklemediği için, önceki bir şemadan gelen (bu kolon olmadan
# oluşturulmuş) bir `airports` tablosu bu kolon olmadan kalır ve
# `Airport.timezone` okuyan her sorgu ("no such column") ile çöker.
# Nullable olduğu için DEFAULT gerekmez - mevcut satırlar NULL alır,
# `resolve_airport_timezone()` bunu zaten açıkça ele alıyor.
_SQLITE_AIRPORTS_COLUMNS = {
    "timezone": "VARCHAR(64)",
    # ADIM (Airport-Scale Queue Capacity) - large/medium/small, nullable
    # (bkz. models.py `Airport.scale` docstring'i).
    "scale": "VARCHAR(16)",
}

# ADIM (Current Operational Day Isolation) - `queue_predictions` tablosuna
# SONRADAN eklenen kolon (bkz. models.py `QueuePrediction.operational_date`
# docstring'i). Nullable/DEFAULT YOK: mevcut (bu ADIM'dan önce yazılmış)
# satırlar NULL alır - `process_series()`/`prune_stale_predictions()` bunu
# açıkça ele alıp ESKİ (window_start tabanlı) davranışa geri döner.
_SQLITE_QUEUE_PREDICTIONS_COLUMNS = {
    "operational_date": "DATE",
}


def _migrate_sqlite_table(table: str, columns: dict[str, str], target_engine=None) -> None:
    """
    `target_engine` verilmezse bu modülün global (production) `engine`'i
    kullanılır - GERİYE DÖNÜK UYUMLU. Verilirse (ör. test harness'ının
    KENDİ, izole sqlite dosyası) SADECE o engine üzerinde çalışır - bu
    fonksiyon production `engine`'e HİÇ dokunmaz.
    """
    target_engine = target_engine if target_engine is not None else engine
    if target_engine.dialect.name != "sqlite":
        return

    inspector = inspect(target_engine)
    if table not in inspector.get_table_names():
        return

    existing = {column["name"] for column in inspector.get_columns(table)}
    missing = [name for name in columns if name not in existing]
    if not missing:
        return

    with target_engine.begin() as connection:
        for name in missing:
            definition = columns[name]
            connection.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
            )


def init_db(drop_first: bool = False) -> None:
    """Veritabanı tablları ."""
    if drop_first:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    _migrate_sqlite_table("airport_operational_configs", _SQLITE_OPERATIONAL_CONFIG_COLUMNS)
    _migrate_sqlite_table("airports", _SQLITE_AIRPORTS_COLUMNS)
    _migrate_sqlite_table("queue_predictions", _SQLITE_QUEUE_PREDICTIONS_COLUMNS)


def get_session() -> Session:
    return SessionLocal()
