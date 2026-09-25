"""
Veritabanı bağlantısı.

ADIM (MySQL-Only, Enforced) - bu proje SADECE MySQL (8.x/InnoDB)
kullanır - örnek:
    mysql+pymysql://user:pass@host:3306/dbname?charset=utf8mb4

Bu artık "varsayılan tercih" DEĞİL, KOD SEVİYESİNDE DOĞRULANAN bir
kısıtlamadır: `DATABASE_URL` (1) TANIMSIZ/BOŞ OLAMAZ VE (2) `mysql`
dialect'i DIŞINDA HİÇBİR backend'e (SQLite, Postgres, ya da başka
herhangi bir SQLAlchemy dialect'i) İZİN VERİLMEZ - ikisi de `app.db`
import edilir edilmez (engine/DB'ye HİÇ dokunmadan) açık bir
`RuntimeError` ile durur. Bu kontrol HER environment'ta (local, dev,
test, production, worker/web/script fark etmez - istisna YOK) aynı
şekilde çalışır. `APP_ENV` DEĞİŞMEDEN kalır (bkz. aşağı) ama SADECE
bilgilendiricidir - DB engine seçimini HİÇ ETKİLEMEZ.

SQLite (ve Postgres) desteği - eski ADIM'larda burada "da destekleniyor"
olarak belgelenen, dialect kontrolüyle korunan bir `_migrate_sqlite_
table()` yardımcı fonksiyonu ve `_SQLITE_*_COLUMNS` sabitleri de dahil -
KODDAN TAMAMEN KALDIRILDI (ADIM: MySQL-Only Cleanup). Bu proje artık
tek bir DB motoru tanır; `database.sqlite` gibi bir dosya YARATILMAZ/
OKUNMAZ, hiçbir sqlite-specific kod yolu YOKTUR.
"""

import os

from sqlalchemy import create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

# ADIM (MySQL-Only Database Layer) - artık SADECE bilgilendirici; DB
# engine seçimini ETKİLEMEZ (bkz. modül docstring'i). Başka bir
# yerde (ör. loglama) okunmak istenirse diye KALDIRILMADI, sadece
# `DATABASE_URL` zorunluluğundan BAĞIMSIZ hale getirildi.
APP_ENV = os.environ.get("APP_ENV", "development")

# ADIM (MySQL-Only Database Layer) - eski örtük SQLite fallback'i
# TAMAMEN KALDIRILDI. `DATABASE_URL` yoksa/boşsa engine HİÇ
# OLUŞTURULMAZ - "sessizce yanlış (SQLite) DB'ye bağlanmak" yerine
# "hemen ve anlaşılır şekilde başarısız olmak" HER environment'ta
# (sadece production'da değil) tercih edilir.
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is required. Configure a MySQL database connection "
        "(e.g. mysql+pymysql://user:pass@host:3306/dbname?charset=utf8mb4) - "
        "see deploy/systemd/aircraft-capacity.env.example. There is no "
        "SQLite fallback in any environment."
    )

# ADIM (MySQL-Only, Enforced) - `DATABASE_URL`'in VARLIĞI artık YETERLİ
# DEĞİL, dialect'i de `mysql` OLMAK ZORUNDA. Eskiden bu sadece
# "SQLite'a sessizce düşülmez" anlamına geliyordu (kullanıcı KASITLI
# olarak `sqlite:///...` ya da `postgresql://...` verirse çalışırdı) -
# artık böyle bir URL DB'ye HİÇ dokunmadan (`create_engine()`
# ÇAĞRILMADAN) açık bir `RuntimeError` ile reddedilir. Postgres desteği
# de bilinçli olarak KALDIRILDI - proje tek bir DB motoruna bağlanır.
_backend = make_url(DATABASE_URL).get_backend_name()
if _backend != "mysql":
    raise RuntimeError(
        f"DATABASE_URL must be a MySQL connection (mysql+pymysql://...) - "
        f"got a '{_backend}' URL instead. This project is MySQL-only; "
        "SQLite, Postgres and every other backend are rejected, not just "
        "left unsupported by convention."
    )

# ADIM (MySQL Engine Config) - gerçek bir DB SERVER'ına bağlanıldığı
# artık GARANTİ olduğu için (yukarıdaki dialect kontrolü) bu iki ayar
# KOŞULSUZ uygulanır (eskiden "SQLite değilse" diye dallanıyordu - o
# dal artık anlamsız, SQLite hiçbir zaman buraya kadar gelemez):
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
engine = create_engine(
    DATABASE_URL, echo=False, pool_pre_ping=True, pool_recycle=1800,
)
SessionLocal = sessionmaker(bind=engine)


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


def get_session() -> Session:
    return SessionLocal()
