"""
Veritabanı bağlantısı.

MySQL (8.x/InnoDB) - BİRİNCİL/TEK application database - örnek:
    mysql+pymysql://user:pass@host:3306/dbname?charset=utf8mb4
Postgres de desteklenir (aynı SQLAlchemy engine deseni):
    postgresql://user:pass@host:5432/dbname

ADIM (MySQL-Only Database Layer) - ÖNCEKİ bir sürümde `DATABASE_URL`
verilmediğinde local/dev/test'te SESSİZCE SQLite'a düşülüyordu (APP_ENV
üzerinden production'da bu kapatılmıştı). Bu ADIM'da o fallback
TAMAMEN KALDIRILDI - `DATABASE_URL` artık HER environment'ta (local,
dev, production, worker, web, script) KOŞULSUZ ZORUNLUDUR; yoksa
`app.db` import edilir edilmez (engine/DB'ye HİÇ dokunmadan) AÇIK bir
`RuntimeError` fırlatılır. `APP_ENV` DEĞİŞMEDEN kalır (bkz. aşağı) ama
artık SADECE bilgilendirici - DB engine seçimini HİÇ ETKİLEMEZ.

SQLite desteği KODDAN TAMAMEN SİLİNMEDİ - `_migrate_sqlite_table()`
(dialect kontrolüyle KENDİ İÇİNDE korunur, MySQL/Postgres'te no-op'tur)
ve `_SQLITE_*_COLUMNS` sabitleri, `tests/test_queue_config_migration.py`
gibi bunları AÇIKÇA import eden araçlar için KASITLI OLARAK bırakıldı -
ama NORMAL uygulama başlangıcı (`import app.db`) artık HİÇBİR KOŞULDA
otomatik/örtük olarak bir SQLite dosyası SEÇMEZ/AÇMAZ.
"""

import os

from sqlalchemy import create_engine, inspect, make_url
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

# ADIM (MySQL-Only Database Layer) - artık SADECE bilgilendirici; DB
# engine seçimini ETKİLEMEZ (bkz. modül docstring'i). Başka bir
# yerde (ör. loglama) okunmak istenirse diye KALDIRILMADI, sadece
# `DATABASE_URL` zorunluluğundan BAĞIMSIZ hale getirildi.
APP_ENV = os.environ.get("APP_ENV", "development")

# ADIM (MySQL-Only Database Layer) - eski `DEFAULT_SQLITE_PATH`/örtük
# SQLite fallback'i TAMAMEN KALDIRILDI. `DATABASE_URL` yoksa/boşsa
# engine HİÇ OLUŞTURULMAZ - "sessizce yanlış (SQLite) DB'ye bağlanmak"
# yerine "hemen ve anlaşılır şekilde başarısız olmak" HER environment'ta
# (sadece production'da değil) tercih edilir.
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is required. Configure a MySQL database connection "
        "(e.g. mysql+pymysql://user:pass@host:3306/dbname?charset=utf8mb4) - "
        "see deploy/systemd/aircraft-capacity.env.example. There is no "
        "SQLite fallback in any environment."
    )

_is_sqlite = make_url(DATABASE_URL).get_backend_name() == "sqlite"

# ADIM (MySQL/Postgres Engine Config) - SQLite dosya bağlantılarına
# HİÇ uygulanmaz (o zaten tek-dosya, pool/ping kavramı YOK) - SADECE
# gerçek bir DB SERVER'ına bağlanan dialect'lerde devreye girer:
#   pool_pre_ping : her checkout'ta ucuz bir "SELECT 1" ile bağlantının
#                    hâlâ canlı olduğunu doğrular - MySQL'in kendi
#                    `wait_timeout`'u (varsayılan 8 saat) VEYA bir
#                    proxy/LB'nin çok daha kısa idle-kill penceresi
#                    yüzünden "MySQL server has gone away" hatasını
#                    SESSİZCE ÇÖKMEK yerine şeffafça yeniden bağlanarak
#                    önler.
#   pool_recycle  : bağlantıları 1800 saniyede (30 dk) bir ZORLA
#                    yeniler - MySQL'in varsayılan `wait_timeout`'undan
#                    (8 saat) BİLİNÇLİ OLARAK çok daha kısa, kör bir
#                    "büyük sayı" DEĞİL - tipik proxy/LB idle-timeout
#                    pencerelerinin (dakikalar) güvenli üstünde ama
#                    gereksiz sık yeniden bağlanmayacak kadar uzun,
#                    ölçülmeden seçilmiş agresif bir değer DEĞİL.
_engine_kwargs: dict = {"echo": False}
if not _is_sqlite:
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["pool_recycle"] = 1800

engine = create_engine(DATABASE_URL, **_engine_kwargs)
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


def _destructive_reset_allowed() -> bool:
    return os.environ.get("ALLOW_DESTRUCTIVE_DB_RESET", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def init_db(drop_first: bool = False) -> None:
    """
    Veritabanı tablları.

    ADIM (CI/MySQL Production Readiness - Database Safety Guard) -
    `drop_first=True` (`Base.metadata.drop_all()` - TÜM tabloları siler)
    HİÇBİR test/script/CI adımı tarafından bugün ÇAĞRILMIYOR (audit ile
    doğrulandı - tek çağıran `app/seed.py:run(reset=True)`, o da hiçbir
    yerden `reset=True` ile invoke edilmiyor) - ama bu fonksiyon TEK
    ortak nokta olduğu için (gelecekte biri buraya `drop_first=True`
    bağlarsa) proje genelinde ZATEN kullanılan AYNI desenle
    (`RETENTION_ENABLED`, `RETENTION_DRY_RUN` - açık env-var opt-in,
    DB adına göre kırılgan bir tahmin YOK) korunuyor: `ALLOW_DESTRUCTIVE_
    DB_RESET=true` AÇIKÇA set edilmeden `drop_first=True` DB'ye HİÇ
    dokunmadan `RuntimeError` fırlatır. `drop_first=False` (varsayılan,
    HER production `pipeline.run()`/retention cycle'ının kullandığı
    yol) bu kontrolden HİÇ etkilenmez.
    """
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
    _migrate_sqlite_table("airport_operational_configs", _SQLITE_OPERATIONAL_CONFIG_COLUMNS)
    _migrate_sqlite_table("airports", _SQLITE_AIRPORTS_COLUMNS)
    _migrate_sqlite_table("queue_predictions", _SQLITE_QUEUE_PREDICTIONS_COLUMNS)


def get_session() -> Session:
    return SessionLocal()
