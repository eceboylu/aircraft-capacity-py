"""
Bu görev için eklenen test yardımcıları.

`FakeFlight`/`FakeResolver` gerçek SQLAlchemy modelleri DEĞİLDİR - queue
motorunun saf katmanları (`domain/demand.py`, `domain/flows.py`,
`engine.py`) flight/resolver'ı duck-typing ile okur (bkz. o modüllerin
kendi docstring'leri: "mock bir çözümleyiciyle veritabanısız test
edilebilir"). Bu, DB kurmadan gerçek production fonksiyonlarını
(kopyalamadan, doğrudan çağırarak) test etmeyi sağlayan, KASITLI
enjeksiyon noktasıdır.
"""
"""
ADIM (MySQL-Only Test Suite) - bu test suite SQLite KULLANMAZ.
`DATABASE_URL` bu projede HER environment'ta (bkz. `app/db.py`) MySQL
olmak ZORUNDA - testler de istisna DEĞİL: `db_session` fixture'ı
(aşağıda) gerçek `app.db.engine`'i (yani gerçek `DATABASE_URL`'in
gösterdiği MySQL sunucusunu) kullanır, KENDİ ayrı bir SQLite engine'i
İCAT ETMEZ.

Bu, `db_session` kullanan testleri çalıştırmak için (`test_scale_
import.py`, `test_manual_override.py`, `test_config_resolution_scale.
py`, `test_scale_update_idempotent.py`) ÖNCEDEN erişilebilir bir MySQL
sunucusu VE `DATABASE_URL` ortam değişkeninin set edilmiş olmasını
GEREKTİRİR - `app.db` bunu import anında zorunlu kılar (SQLite/Postgres
FARK ETMEKSİZİN reddeder). Lokal geliştirme için repo kökündeki
`docker-compose.yml`'ın `local_mysql` servisi + ayrı, gerçek dev
verisinden İZOLE bir `aircraft_capacity_test` veritabanı kullanılır
(bkz. `tests/README.md` - tam kurulum komutları).

`db_session` kullanmayan testler (`FakeFlight`/`FakeResolver` ile saf
fonksiyon testleri - `domain/demand.py`, `domain/flows.py`, `engine.py`)
DB'ye HİÇ ihtiyaç duymaz, ama `app.queue.pipeline`'ı import eden test
modülleri OLDUĞU İÇİN (transitively `app.db`'yi import eder) `DATABASE_
URL` yine de PYTEST'İN KENDİSİ başlarken set edilmiş olmalı - aksi halde
collection aşamasında `RuntimeError` ile durur (bu KASITLI: "tests
MySQL-only" - sessiz/örtük bir DB seçimi hiçbir aşamada YOK).
"""
from dataclasses import dataclass
from datetime import datetime

import pytest

from app.db import engine as _mysql_engine
from app.db import get_session as _mysql_get_session
from app.models import Base
import app.queue.models  # noqa: F401 - Base.metadata'ya airports/queue tablolarını KAYDETMEK için (side-effect import).


@pytest.fixture
def db_session():
    """
    Gerçek `app.db.engine`'e (yani gerçek `DATABASE_URL`'in gösterdiği
    MySQL veritabanına) bağlı, bu test için İZOLE bir oturum.

    İZOLASYON: MySQL'de sqlite'daki gibi ücretsiz bir "her teste yeni
    in-memory dosya" numarası YOK - bunun yerine her testten ÖNCE
    `Base.metadata.drop_all()` + `create_all()` ile şema SIFIRDAN
    kurulur. Bu, `import_airport_scales`/`refresh_airport_scales`/
    `ensure_airport_operational_configs` gibi fonksiyonların kendi
    içinde `session.commit()` ÇAĞIRMASI yüzünden (bir dış transaction'ı
    sarıp sonunda rollback etmek İŞE YARAMAZ - commit zaten kalıcı hale
    getirir) GEREKLİ - production kodu burada KOPYALANMADAN, GERÇEK
    haliyle (gerçek MySQL'e karşı) test edilir.

    ÖNEMLİ: bu, gerçek dev verisini (ör. `aircraft_capacity` DB'sindeki
    IST/SAW gibi GERÇEK airport satırları) SİLMEZ - `DATABASE_URL` test
    çalıştırılırken AYRI bir veritabanına (ör. `aircraft_capacity_test`)
    işaret etmelidir; bu fixture o veritabanının İÇİNİ her testten önce
    temizler, `aircraft_capacity`'ye HİÇ DOKUNMAZ (hangi DB'ye
    bağlandığı tamamen `DATABASE_URL`'e bağlıdır - bkz. tests/README.md).
    """
    db_name = _mysql_engine.url.database or ""
    if not db_name.endswith("_test"):
        raise RuntimeError(
            f"db_session GUVENLIK KILIDI: DATABASE_URL '{db_name}' isimli "
            "veritabanina isaret ediyor, bu isim '_test' ile bitmiyor. "
            "Bu fixture drop_all()/create_all() calistirir - GERCEK dev "
            "verisini (ör. aircraft_capacity) SESSIZCE silebilir. pytest'i "
            "SADECE DATABASE_URL'in bir '..._test' veritabanini gosterdigi "
            "bir shell'de calistirin (bkz. tests/README.md)."
        )
    Base.metadata.drop_all(_mysql_engine)
    Base.metadata.create_all(_mysql_engine)
    session = _mysql_get_session()
    try:
        yield session
    finally:
        session.close()


@dataclass
class FakeFlight:
    direction: str
    location: str
    status: str = "scheduled"
    airport_iata: str | None = None
    aircraft_icao: str | None = None
    airline_iata: str | None = None
    dep_scheduled_utc: datetime | None = None
    dep_estimated_utc: datetime | None = None
    dep_actual_utc: datetime | None = None
    arr_scheduled_utc: datetime | None = None
    arr_estimated_utc: datetime | None = None
    arr_actual_utc: datetime | None = None
    # ADIM (Schengen-Aware Passport Routing) - varsayılan True: mevcut
    # testlerin BÜYÜK ÇOĞUNLUĞU bunu hiç bilmez/set etmez, `flows.py:
    # _requires_passport()`'un `getattr(..., True)` varsayılanıyla AYNI
    # "eski davranışı koru" ilkesi.
    requires_passport: bool = True


def make_departure(
    *,
    when: datetime,
    location: str = "international",
    airport_iata: str = "IST",
    status: str = "scheduled",
    aircraft_icao: str | None = "A321",
    requires_passport: bool = True,
) -> FakeFlight:
    return FakeFlight(
        direction="departure",
        location=location,
        status=status,
        airport_iata=airport_iata,
        aircraft_icao=aircraft_icao,
        dep_scheduled_utc=when,
        requires_passport=requires_passport,
    )


def make_arrival(
    *,
    when: datetime,
    location: str = "international",
    airport_iata: str = "IST",
    status: str = "scheduled",
    aircraft_icao: str | None = "A321",
    requires_passport: bool = True,
) -> FakeFlight:
    return FakeFlight(
        direction="arrival",
        location=location,
        status=status,
        airport_iata=airport_iata,
        aircraft_icao=aircraft_icao,
        arr_scheduled_utc=when,
        requires_passport=requires_passport,
    )


@dataclass
class FakeCapacityResult:
    capacity: int
    counts_toward_passenger_total: bool = True


class FakeResolver:
    """
    `DemandCalculator`'ın beklediği `.resolve(icao, airline_iata) ->
    CapacityResult`-benzeri nesne dönen arayüzü taklit eder
    (`AircraftCapacityService`'in KENDİSİ DEĞİŞTİRİLMEDİ/test edilmiyor
    - bu sadece onun enjekte edildiği noktanın bir test double'ı).
    """

    def __init__(self, capacity_by_icao: dict, default: FakeCapacityResult | None = None):
        self._capacity_by_icao = capacity_by_icao
        self._default = default if default is not None else FakeCapacityResult(capacity=180)

    def resolve(self, icao_code, airline_iata=None):
        return self._capacity_by_icao.get(icao_code, self._default)
