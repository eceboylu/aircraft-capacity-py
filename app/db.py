"""
Veritabanı bağlantısı.

Canlıya geçince: DATABASE_URL ortam değişkenini değiştir
    postgresql://user:pass@host:5432/dbname
    mysql+pymysql://user:pass@host:3306/dbname

"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DEFAULT_SQLITE_PATH = os.path.join(os.path.dirname(__file__), "..", "database.sqlite")
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DEFAULT_SQLITE_PATH}")

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)


def init_db(drop_first: bool = False) -> None:
    """Veritabanı tablları ."""
    if drop_first:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def get_session() -> Session:
    return SessionLocal()