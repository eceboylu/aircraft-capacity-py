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
}


def _migrate_sqlite_operational_config() -> None:
    if engine.dialect.name != "sqlite":
        return

    inspector = inspect(engine)
    table = "airport_operational_configs"
    if table not in inspector.get_table_names():
        return

    existing = {column["name"] for column in inspector.get_columns(table)}
    missing = [name for name in _SQLITE_OPERATIONAL_CONFIG_COLUMNS if name not in existing]
    if not missing:
        return

    with engine.begin() as connection:
        for name in missing:
            definition = _SQLITE_OPERATIONAL_CONFIG_COLUMNS[name]
            connection.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
            )


def init_db(drop_first: bool = False) -> None:
    """Veritabanı tablları ."""
    if drop_first:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    _migrate_sqlite_operational_config()


def get_session() -> Session:
    return SessionLocal()
