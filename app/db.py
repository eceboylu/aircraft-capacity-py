
import os

from sqlalchemy import create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

APP_ENV = os.environ.get("APP_ENV", "development")

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is required. Configure a MySQL database connection "
        "(e.g. mysql+pymysql://user:pass@host:3306/dbname?charset=utf8mb4) - "
        "see deploy/systemd/aircraft-capacity.env.example. There is no "
        "SQLite fallback in any environment."
    )

_backend = make_url(DATABASE_URL).get_backend_name()
if _backend != "mysql":
    raise RuntimeError(
        f"DATABASE_URL must be a MySQL connection (mysql+pymysql://...) - "
        f"got a '{_backend}' URL instead. This project is MySQL-only; "
        "SQLite, Postgres and every other backend are rejected, not just "
        "left unsupported by convention."
    )

engine = create_engine(
    DATABASE_URL, echo=False, pool_pre_ping=True, pool_recycle=1800,
)
SessionLocal = sessionmaker(bind=engine)


def _destructive_reset_allowed() -> bool:
    return os.environ.get("ALLOW_DESTRUCTIVE_DB_RESET", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def init_db(drop_first: bool = False) -> None:
    if drop_first:
        if not _destructive_reset_allowed():
            raise RuntimeError(
                "init_db(drop_first=True) refused: this drops every table "
                "in the database configured by DATABASE_URL. Set "
                "ALLOW_DESTRUCTIVE_DB_RESET=true explicitly to allow this "
                "(only ever appropriate for a disposable/local/test "
                "database - never production)."
            )
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def get_session() -> Session:
    return SessionLocal()
