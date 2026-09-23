import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# ADIM (Database Migration Strategy) - Base + TÜM model modülleri
# burada import edilir ki `Base.metadata` autogenerate zamanında
# projenin TAMAMINI (Madde 1 + queue + health) görsün. Bu import'lar
# `app/db.py`/`app/worker.py`/`app/web/server.py`'yi HİÇ tetiklemez -
# sadece SQLAlchemy model sınıflarını (tablo tanımlarını) register eder,
# hiçbir DB bağlantısı açmaz. `app.health`, `app.queue.models`'ın AYNI
# `app.models:Base`'i paylaştığı gibi bu da paylaşır - queue/health
# modüllerinin KENDİ mantığına (risk/wait/demand/worker loop) burada
# HİÇ dokunulmaz, sadece tablo şemaları OKUNUR.
from app.models import Base  # noqa: E402
import app.queue.models  # noqa: E402,F401
import app.health  # noqa: E402,F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ADIM (Database Migration Strategy) - `alembic.ini`'deki
# `sqlalchemy.url` SADECE bir placeholder'dır (repo'da GERÇEK bir DB
# URL'i/secret'ı TUTULMAZ - `app/db.py`'nin "DATABASE_URL HER
# environment'ta zorunlu" kuralıyla AYNI ilke). Gerçek değer HER ZAMAN
# ortam değişkeninden okunur - `app.db.DATABASE_URL`'i TEKRAR YAZMAK
# yerine `app.db`'nin KENDİSİNİ import edip AYNI, tek doğrulanmış
# değeri kullanıyoruz (DATABASE_URL yoksa `app.db` importu zaten AÇIK
# bir RuntimeError fırlatır - burada AYRI bir kontrol İCAT EDİLMEDİ).
from app.db import DATABASE_URL  # noqa: E402

config.set_main_option("sqlalchemy.url", DATABASE_URL)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
