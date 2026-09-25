"""
Uçtan uca akış: kaynak dosyalar -> veritabanı -> tahminler.

    python -m app.queue.pipeline

Sıra:
  1. Tablolar (Madde 1 + Madde 2/3 aynı Base'i paylaşır)
  2. Havalimanı referansı (flight_airports.sql) - bir kere
  3. Kaynak B indeksi (aircraft_icao)
  4. Kaynak A ayrıştırma + enrichment
  5. Uçuş upsert + değişiklik event'leri
  6. Her havalimanı için tahmin üretimi

Adım 6, Madde 1'in AircraftCapacityService'ini İMPORT EDİP KULLANIR;
o servisin kodunu değiştirmez.

CANLIYA GEÇİŞ: Elimizde tarife dosyası olarak yalnızca örnek JSON'lar
var; canlı sistemde aynı kayıtlar ~30 dakikada bir API'den gelecek.
Bu yüzden `run()` kaynakları DOSYA OLARAK DEĞİL, KAYIT SAĞLAYICI
olarak alır:

    run(source_a=lambda direction: api.fetch(direction),
        source_b=lambda: api.fetch_live())

Sağlayıcı verilmezse örnek dosyalar okunur. Canlıya bağlanmak için
ayrıştırma, hesap, tahmin ve raporlama katmanlarında hiçbir değişiklik
gerekmez; her çalıştırma mevcut satırları upsert eder, yeni satır
açmaz.
"""

import logging
import os
import time
from datetime import datetime, timedelta

from ..db import get_session, init_db
from ..models import AircraftCapacity
from ..seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset
from ..service import AircraftCapacityService
from .constants import DIRECTION_ARRIVAL, DIRECTION_DEPARTURE
from .engine import run_predictions
from .ingestion.airports_import import (
    country_lookup,
    import_airport_scales,
    import_airports,
)
from .ingestion.refresh import refresh_flights
from .config import _scale_resources_from_db
from .domain.airport_scale import resource_view_for_scale
from .domain.retention_time import USAGE_HORIZON_HOURS
from .ingestion.sources import (
    aircraft_match_rate,
    build_aircraft_index,
    load_source_payload,
    parse_source_a,
)
from .models import Airport, AirportOperationalConfig

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")

AIRPORTS_SQL = "flight_airports.sql"
# ADIM (4-Tier Airport Scale) - mega/large/medium/small ölçek referansı.
MEGA_SCALE_TXT = "mega_havaalanlari.txt"
LARGE_SCALE_TXT = "buyuk_olcekli_havaalanlari.txt"
MEDIUM_SCALE_TXT = "orta_olcekli_havaalanlari.txt"
SMALL_SCALE_TXT = "kucuk_olcekli_havaalanlari.txt"
# Kaynak A - tarife/gecikme beslemesi, yön başına bir dosya.
SOURCE_A_FILES = {
    DIRECTION_ARRIVAL: "Delays - Type Arrivals.json",
    DIRECTION_DEPARTURE: "Delays - Type Departures.json",
}
# Kaynak B - canlı uçuş beslemesi, aircraft_icao'nun kaynağı.
SOURCE_B_FILE = "response-delays.json"

# ADIM (Generated Local Source Mode) - local/dev'de gerçek IST departure/
# arrival board'larından türetilmiş generated_delays_*.json dosyalarını
# okumak için opsiyonel geçiş. Varsayılan (env var set DEĞİLSE) davranış
# BİREBİR eskisiyle AYNI kalır - production'ın SOURCE_A_FILES/SOURCE_B_FILE
# okuma yolu DEĞİŞMEDİ. `QUEUE_LOCAL_SOURCE_MODE=generated` AÇIKÇA
# verildiğinde `file_source_a()`/`file_source_b()` bu üç dosyayı okur -
# dosyaların ÜRETİMİ `scripts/generate_local_ist_source.py`'nin işidir,
# bu modülün DEĞİL (SADECE hangi dosyanın okunacağına karar verir).
GENERATED_SOURCE_A_FILES = {
    DIRECTION_ARRIVAL: "generated_delays_arrivals.json",
    DIRECTION_DEPARTURE: "generated_delays_departures.json",
}
GENERATED_SOURCE_B_FILE = "generated_response_delays.json"


def _local_generated_source_mode() -> bool:
    return os.environ.get("QUEUE_LOCAL_SOURCE_MODE", "").strip().lower() == "generated"


def _path(data_dir: str, filename: str) -> str:
    return os.path.join(data_dir, filename)


def ensure_airports(session, data_dir: str = DATA_DIR) -> int:
    """
    Havalimanı referansını bir kere yükler. Tablo doluysa tekrar
    ayrıştırma yapılmaz (dosya ~2 MB).
    """
    if session.query(Airport).first() is not None:
        return 0
    return import_airports(session, _path(data_dir, AIRPORTS_SQL))


def ensure_airport_scales(session, data_dir: str = DATA_DIR) -> dict | None:
    """
    Airport-scale referansını bir kere yükler (bkz. `ensure_airports()`
    ile AYNI idempotent bootstrap deseni) - her ~5dk'lık refresh'te 4
    txt dosyasını YENİDEN parse ETMEZ.

    En az bir `Airport.scale IS NOT NULL` satırı varsa "zaten import
    edilmiş" sayılır ve atlanır (skip) - bu fonksiyon SADECE İLK/boş
    bootstrap içindir. Zaten çözülmüş bir veritabanını YENİ bir scale
    contract'ına göre GÜNCELLEMEK (ör. 3-tier'dan 4-tier'a geçiş)
    `scripts/update_airport_scale_resources.py`'nin işidir - bu
    fonksiyon bilinçli olarak "skip" davranışını DEĞİŞTİRMEZ (aksi
    halde HER prediction turunda/worker restart'ında 4 dosya sessizce
    yeniden parse edilip DB'ye yazılırdı). Airport referansı (`ensure_
    airports`) HENÜZ çalışmadıysa (tablo boşsa) yapacak bir şey yoktur,
    None döner - `import_airport_scales()` zaten var olan `Airport`
    satırlarını GÜNCELLER, kendi satırını YARATMAZ.
    """
    if session.query(Airport).first() is None:
        return None
    if session.query(Airport).filter(Airport.scale.isnot(None)).first() is not None:
        return None
    return import_airport_scales(
        session,
        _path(data_dir, MEGA_SCALE_TXT),
        _path(data_dir, LARGE_SCALE_TXT),
        _path(data_dir, MEDIUM_SCALE_TXT),
        _path(data_dir, SMALL_SCALE_TXT),
    )


def ensure_airport_operational_configs(session) -> dict:
    """
    ADIM (Airport Operational Config Materialization) - `ensure_
    airport_scales()`'in HEMEN SONRASI çalışacak şekilde tasarlandı
    (bkz. `run()` - scale'ler çözülmeden bu fonksiyonun yapacağı bir
    şey yoktur). ÖNCEDEN: bir havalimanı için `airport_operational_
    configs`'ta satır YOKSA `config.py:get_config()` her çağrıda
    `Airport.scale`'den (`airport_scale.py:SCALE_RESOURCES`) sessizce
    türetiyordu - satır SADECE elle override edilince oluşuyordu. Bu,
    "bu havalimanı için hangi kapasiteyi kullanıyoruz" sorusunu MySQL'de
    GÖREMEMEK anlamına geliyordu (bkz. rapor - kullanıcı talebi).

    Artık `Airport.scale IS NOT NULL` olan (yani mega/large/medium/
    small çözülebilen) HER havalimanı için, henüz satırı YOKSA, `SCALE_
    RESOURCES`'ın O ANKİ değerlerinin BİR KOPYASI `is_seeded_
    default=True` ile eklenir - `AirportConfigView.is_default`/
    confidence cezası (bkz. `config.py:_build_config_view`, `core/
    scoring.py:confidence_score`) bu satırlar için AYNEN eskisi gibi
    "default" sayılmaya devam eder; SADECE satırın KENDİSİ artık
    MySQL'de GÖRÜNÜR/DÜZENLENEBİLİR.

    ADIM (Live Scale Read) - `config.py:_resolve_security_lane_counts()`
    artık `is_seeded_default=True` satırların KENDİ kolon değerini HİÇ
    OKUMAZ, HER ZAMAN `SCALE_RESOURCES`'ın O ANKİ (güncel) değerinden
    canlı türetir (bkz. o fonksiyonun docstring'i - kullanıcı talebi:
    "büyük ölçekliyse büyük ölçekli için kullandığımız KAPASİTEYİ
    alacak", dondurulmuş bir kopya DEĞİL). Bu fonksiyon YİNE DE
    `is_seeded_default=True` satırların `domestic_security_lane_count`/
    `international_security_lane_count` kolonlarını her çalışmada
    GÜNCEL `resources`'a RESYNC eder (sadece GERÇEKTEN farklıysa
    UPDATE - gereksiz yazma YOK) - bu SADECE phpMyAdmin'deki gösterimin
    (kod zaten canlı okusa da) YANILTICI/stale görünmemesi içindir,
    doğruluk BUNA bağlı DEĞİLDİR.

    BİLİNÇLİ İSTİSNA - `passport_departure_server_count`/`passport_
    arrival_server_count` seed/resync edilirken KASITLI OLARAK `None`
    (NULL) bırakılır, `SCALE_RESOURCES`'ın sayısal değeri YAZILMAZ: bu
    iki kolonun `NULL` OLMASI, `config.py:_resolve_passport_server_
    counts()` için "override YOK, scale'den HER ÇAĞRIDA CANLI türet"
    anlamına gelir (bkz. o fonksiyon + `_build_config_view`'ın
    `departure_is_override` mantığı) - `SCALE_RESOURCES`'taki sayılar
    (ör. MEGA'nın 30/35'i) ileride TEKRAR değişirse, bu kolonlar NULL
    kaldığı sürece MySQL'e HİÇBİR migration/update GEREKMEDEN tüm
    scale-derived havalimanları otomatik yeni değeri kullanır. Eğer
    buraya scale'in sayısını LİTERAL yazsaydık, kolon artık `NULL`
    OLMAYACAĞI için sistem bunu "explicit override" sanıp o havalimanını
    SESSİZCE gelecekteki `SCALE_RESOURCES` güncellemelerinden İZOLE
    ederdi - bu YANLIŞ, istenmeyen bir yan etki olurdu. (Eski not: bu
    izolasyon önceden "LARGE'ın dynamic staffing'ini kapatır" olarak da
    zarar verirdi - 4-tier static contract'ta dynamic staffing zaten
    hiçbir scale için aktif değil, bkz. `domain/airport_scale.py:
    SCALE_RESOURCES` docstring'i, ama "canlı okuma" gerekçesi AYNEN
    geçerli.)

    Bir havalimanı için satır `is_seeded_default=False` (elle override
    edildi) İSE bu fonksiyon ONU HİÇ dokunmaz/üzerine YAZMAZ -
    "eğer havaalanına özel veri belirlediysek override olup onu almalı"
    (kullanıcı talebi) tam olarak bu şekilde korunuyor.

    `ensure_airports()`/`ensure_airport_scales()` ile AYNI idempotent
    bootstrap deseni - HENÜZ hiçbir havalimanının scale'i çözülmediyse
    (tablo boş/hepsi None) yapacak bir şey yoktur.

    Döner: `{"created": int, "resynced": int}`.
    """
    resolved = (
        session.query(Airport)
        .filter(Airport.scale.isnot(None))
        .all()
    )
    if not resolved:
        return {"created": 0, "resynced": 0}

    existing_rows = {
        row.airport_iata: row
        for row in session.query(AirportOperationalConfig).all()
    }

    # ADIM (DB-Editable Scale Resource Contract) - `airport_scale_
    # configs` tablosunda bir scale için satır VARSA, aşağıdaki resync
    # ONU kullanır (`SCALE_RESOURCES` Python sabiti yerine) - böylece
    # phpMyAdmin'den değiştirilen bir sayı bu "görünürlük kopyası"na
    # da yansır, `config.py:get_config()`'ün canlı hesabıyla TUTARLI
    # kalır. TEK sorgu, tüm döngü boyunca paylaşılır.
    db_scale_resources = _scale_resources_from_db(session)

    created = 0
    resynced = 0
    for airport in resolved:
        resources = db_scale_resources.get(airport.scale) or resource_view_for_scale(airport.scale)
        if resources is None:
            continue
        row = existing_rows.get(airport.iata_code)
        if row is None:
            session.add(AirportOperationalConfig(
                airport_iata=airport.iata_code,
                domestic_security_lane_count=resources["domestic_security_lanes"],
                international_security_lane_count=resources["international_security_lanes"],
                passport_departure_server_count=None,
                passport_arrival_server_count=None,
                is_seeded_default=True,
            ))
            created += 1
            continue
        if not row.is_seeded_default:
            continue  # insan eliyle override edilmiş satır - HİÇ dokunulmaz.
        if (
            row.domestic_security_lane_count != resources["domestic_security_lanes"]
            or row.international_security_lane_count != resources["international_security_lanes"]
        ):
            row.domestic_security_lane_count = resources["domestic_security_lanes"]
            row.international_security_lane_count = resources["international_security_lanes"]
            resynced += 1
    if created or resynced:
        session.commit()
    return {"created": created, "resynced": resynced}


class CapacitySeedError(RuntimeError):
    """
    `aircraft_capacity` boş olduğu halde resmi seed fonksiyonları
    çalıştırılamadı - AÇIK bir hata; çağıran taraf bunu bir health
    failure olarak ele almalı. Sessizce `unknown_default`e (150)
    düşülmesi TERCİH EDİLMEZ - bu, ADIM 6C'de kanıtlanan gerçek bir
    üretim riskidir (bkz. queue_prediction_spec.md / ADIM 6C raporu).
    """


def ensure_capacity_reference(session) -> bool:
    """
    ADIM 6C - Madde 1'in kapasite referans tablosu (`aircraft_capacity`)
    BOŞSA, `AircraftCapacityService.resolve()` HER uçak tipi için
    sessizce `unknown_default` (150) katmanına düşer - bu, gerçek
    verinin bile yanlış hesaplanmasına yol açan KANITLANMIŞ bir
    üretim riskidir (bir önceki DB dosyası silme/yeniden oluşturma
    olayında bu sessizce oldu).

    Bu fonksiyon `ensure_airports()` ile AYNI desendedir: tablo
    doluysa HİÇBİR ŞEY yapmaz (gereksiz reseed YOK, idempotent).
    Boşsa Madde 1'in KENDİ resmi seed fonksiyonlarını (ikinci bir
    resolver/seed YAZILMADI) çağırır - `app.seed.run()` KULLANILMAZ,
    çünkü o `init_db(drop_first=True)` ile TÜM tabloları (Flight,
    QueuePrediction dahil) siler; burada production verisine
    DOKUNULMAZ, sadece referans tablosu doldurulur.

    Seed başarısız olursa (örn. `data/yolcu_ucaklari.json` bulunamadı)
    hata YUTULMAZ - `CapacitySeedError` fırlatılır, `run()` bunu
    kritik/top-level hata olarak yukarı taşır (bkz. `main()`'in
    exit-code kararı) - sistem sessizce yanlış (150-varsayılan)
    sonuçlar üretmeye BAŞLAMAZ.
    """
    if session.query(AircraftCapacity).first() is not None:
        return False

    logger.warning(
        "aircraft_capacity referans tablosu BOŞ - Madde 1'in resmi seed "
        "fonksiyonları (verified_dataset + curated_fallback + family/GA) "
        "çalıştırılıyor. Bu, kapasite hesabının şu ana kadar sessizce "
        "unknown_default'a (150) düştüğü anlamına gelir."
    )
    try:
        seed_verified_dataset(session)
        seed_curated_fallback(session)
        seed_family_and_ga(session)
    except Exception as exc:  # noqa: BLE001 - kasıtlı: her türlü seed
        # hatası aşağıda AÇIK bir CapacitySeedError'a çevrilip yukarı
        # taşınmalı; burada session.rollback() ile yarım kalan bir
        # seed'in kısmi veri bırakması da önlenir.
        session.rollback()
        raise CapacitySeedError(
            "aircraft_capacity seed edilemedi - pipeline DURDURULDU "
            "(sessizce unknown_default'a düşülmedi)."
        ) from exc

    if session.query(AircraftCapacity).first() is None:
        raise CapacitySeedError(
            "aircraft_capacity seed sonrası HÂLÂ boş - beklenmeyen durum."
        )

    logger.info("aircraft_capacity referans tablosu seed edildi.")
    return True


def file_source_a(data_dir: str = DATA_DIR):
    """
    Örnek dosyalardan okuyan varsayılan Kaynak A sağlayıcısı.

    `QUEUE_LOCAL_SOURCE_MODE=generated` verilirse (local/dev opsiyonu,
    bkz. `GENERATED_SOURCE_A_FILES`) generated_delays_*.json okunur;
    verilmezse (varsayılan) production'ın SOURCE_A_FILES'ı DEĞİŞMEDEN
    okunmaya devam eder.
    """
    def provide(direction: str) -> list[dict]:
        files = GENERATED_SOURCE_A_FILES if _local_generated_source_mode() else SOURCE_A_FILES
        return load_source_payload(_path(data_dir, files[direction]))
    return provide


def file_source_b(data_dir: str = DATA_DIR):
    """
    Örnek dosyadan okuyan varsayılan Kaynak B sağlayıcısı.

    `QUEUE_LOCAL_SOURCE_MODE=generated` verilirse `GENERATED_SOURCE_B_FILE`
    okunur; verilmezse (varsayılan) production'ın SOURCE_B_FILE'ı
    DEĞİŞMEDEN okunmaya devam eder.
    """
    def provide() -> list[dict]:
        filename = GENERATED_SOURCE_B_FILE if _local_generated_source_mode() else SOURCE_B_FILE
        return load_source_payload(_path(data_dir, filename))
    return provide


def load_flight_rows(
    session,
    data_dir: str = DATA_DIR,
    source_a=None,
    source_b=None,
    now: datetime | None = None,
) -> list[dict]:
    """
    Kaynak A + Kaynak B birleşimi (AŞAMA 0).

    source_a : (direction) -> kayıt listesi
    source_b : () -> kayıt listesi
    Verilmezse örnek dosyalar okunur. Canlı feed bağlanırken burada
    değişen tek şey bu iki sağlayıcıdır.

    Kaynak B okunamazsa enrichment'sız devam edilir: aircraft_icao
    None kalır, Madde 1 bunu unknown_default ile karşılar, confidence
    düşer - sistem ÇÖKMEZ.

    now : ADIM (Re-Ingest Loop Prevention) - Bölüm 15. Verilirse,
          `now - USAGE_HORIZON_HOURS` (48 saat, `domain/retention_
          time.py` - `engine.py:flights_of_airport()`'un usage-horizon
          filtresiyle AYNI sabit) cutoff'undan ESKİ canonical operasyonel
          zamanlı kayıtlar `parse_source_a()` tarafından HİÇ üretilmez -
          retention cleanup'ın sildiği eski bir flight'ı upstream hâlâ
          döndürüyorsa sonsuz silme/yeniden-ekleme döngüsü önlenir.
          Verilmezse (None, varsayılan) HİÇ filtre uygulanmaz - eski
          davranış birebir korunur.
    """
    countries = country_lookup(session)
    source_a = source_a or file_source_a(data_dir)
    source_b = source_b or file_source_b(data_dir)
    min_operational_time = (
        now - timedelta(hours=USAGE_HORIZON_HOURS) if now is not None else None
    )

    try:
        source_b_records = source_b()
        aircraft_index = build_aircraft_index(source_b_records)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Kaynak B (canlı uçuş / aircraft_icao beslemesi) okunamadı "
            "(%s); fallback olarak boş enrichment index kullanılıyor - "
            "bu turda aircraft_icao eşleşmesi eksik kalabilir.",
            type(exc).__name__,
        )
        source_b_records = []
        aircraft_index = {}
    logger.info("Kaynak B: %d kayıt alındı", len(source_b_records))

    rows: list[dict] = []
    source_a_total = 0
    for direction in SOURCE_A_FILES:
        try:
            records = source_a(direction)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Kaynak A (%s tarifesi) okunamadı (%s); bu yön için "
                "bu turda hiç kayıt işlenmeyecek - diğer yön/kaynaklar "
                "etkilenmeden devam ediyor.",
                direction, type(exc).__name__,
            )
            continue
        source_a_total += len(records)
        logger.info("Kaynak A (%s): %d kayıt alındı", direction, len(records))
        rows.extend(
            parse_source_a(
                records, direction, countries, aircraft_index,
                min_operational_time=min_operational_time,
            )
        )

    logger.info(
        "ingestion sonucu: %d uçuş satırı ayrıştırıldı (Kaynak A ham kayıt=%d, Kaynak B ham kayıt=%d)",
        len(rows), source_a_total, len(source_b_records),
    )
    return rows


def run(
    data_dir: str = DATA_DIR,
    update_baseline: bool = True,
    source_a=None,
    source_b=None,
    now=None,
    apply_usage_horizon: bool = False,
) -> dict:
    """
    Tüm akışı çalıştırır ve özet döndürür.

    Canlı sistemde bu fonksiyon ~30 dakikada bir çağrılır; uçuşlar
    upsert edilir, sadece gerçek değişiklikler FlightEvent olarak
    yazılır, tahminler güncellenir ve bayat pencereler temizlenir.

    ADIM 5A - scheduler öncesi observability: bu fonksiyon artık
    rutin (INFO seviye) ilerleme logları üretir ve dönen özete
    `failed_airports` (run_predictions()'ın zaten ürettiği ama önceden
    dışarı yansıtılmayan alan) + `duration_seconds` eklenir. Mevcut
    anahtarların hiçbiri kaldırılmadı/yeniden adlandırılmadı - sadece
    eklendi.

    now : ADIM (Operational-Day Scope) - Bölüm 59: "Testlerde 'now'
          inject edilebilir/deterministik olmalıdır". `run_predictions()`'a
          VE `load_flight_rows()`'a (ADIM Re-Ingest Loop Prevention -
          Bölüm 15: 48 saatlik ingestion-horizon reddi) AYNEN geçilir.
          Verilmezse (None) davranış BİREBİR eskisiyle AYNI kalır:
          `load_flight_rows(now=None)` hiçbir ingestion-horizon filtresi
          UYGULAMAZ (mevcut, geriye dönük uyumlu varsayılan - bkz. o
          fonksiyonun docstring'i), `run_predictions()` kendi
          `domain_now()` varsayılanını kullanır (DEĞİŞMEDİ). Gerçek
          production periyodik döngüsü (`worker.py:run_forever()`)
          Bölüm 15'in re-ingest-loop korumasını AKTİFLEŞTİRMEK için
          `now=domain_now()`'ı AÇIKÇA geçer - bu fonksiyonun kendi
          varsayılanı DEĞİŞMEDİ, sadece TEK bir çağıran artık `now`'ı
          açıkça veriyor.
    apply_usage_horizon : ADIM (48h Usage Horizon) - `run_predictions()`'a
          AYNEN geçilir (bkz. o fonksiyonun docstring'i). Varsayılan
          `False` - mevcut davranış korunur. SADECE `worker.py`'nin
          gerçek production çağrısı `True` geçer.
    """
    start = time.monotonic()
    logger.info("pipeline run started")

    init_db()
    session = get_session()
    try:
        airports_loaded = ensure_airports(session, data_dir)
        scales_imported = ensure_airport_scales(session, data_dir)
        operational_configs = ensure_airport_operational_configs(session)
        capacity_seeded = ensure_capacity_reference(session)
        rows = load_flight_rows(session, data_dir, source_a, source_b, now=now)
        match_rate = aircraft_match_rate(rows)
        refreshed = refresh_flights(session, rows)
        logger.info(
            "refresh sonucu: inserted=%d updated=%d events_written=%d failed=%d",
            refreshed["inserted"], refreshed["updated"],
            refreshed["events_written"], refreshed["failed"],
        )

        logger.info("prediction started")
        predicted = run_predictions(
            session,
            resolver=AircraftCapacityService(session),
            update_baseline=update_baseline,
            now=now,
            apply_usage_horizon=apply_usage_horizon,
        )
        logger.info(
            "prediction completed: predictions=%d pruned=%d airports_ok=%d airports_failed=%d",
            predicted["predictions"], predicted["pruned"],
            len(predicted["airports"]), len(predicted["failed_airports"]),
        )
        if predicted["failed_airports"]:
            # Havalimanı-bazlı izolasyon zaten run_predictions() içinde
            # uygulanıyor (bkz. engine.py) - bu SADECE görünürlük için,
            # akışı DEĞİŞTİRMEZ, kritik hata SAYILMAZ (bkz. __main__
            # bloğundaki exit-code kararı).
            logger.warning(
                "bazı havalimanları için tahmin üretilemedi (izole edildi, "
                "diğer havalimanları etkilenmedi): %s",
                predicted["failed_airports"],
            )
    finally:
        session.close()

    elapsed = time.monotonic() - start
    summary = {
        "airports_loaded": airports_loaded,
        "scales_imported": scales_imported,
        "operational_configs_seeded": operational_configs["created"],
        "operational_configs_resynced": operational_configs["resynced"],
        "capacity_seeded": capacity_seeded,
        "flights_parsed": len(rows),
        "aircraft_match_rate": round(match_rate, 3),
        **refreshed,
        "predictions": predicted["predictions"],
        "pruned": predicted["pruned"],
        "airports_predicted": len(predicted["airports"]),
        "failed_airports": predicted["failed_airports"],
        "duration_seconds": round(elapsed, 2),
    }
    logger.info("pipeline run completed duration=%.2fs", elapsed)
    return summary


def main() -> int:
    """
    CLI giriş noktasının gövdesi - `run()`'ı çağırır, özeti basar ve
    scheduler'ın (systemd/cron) okuyabileceği bir exit code döndürür.

    Ayrı bir fonksiyon olarak tutulması (doğrudan `if __name__` içine
    yazmak yerine) SADECE test edilebilirlik içindir: testler `run()`'ı
    monkeypatch edip `main()`'i çağırarak gerçek DB/dosya sistemine hiç
    dokunmadan exit-code mantığını doğrulayabilir; `python -m
    app.queue.pipeline` çalıştırıldığındaki davranış DEĞİŞMEDİ.

    Exit-code kararı (ADIM 5A): SADECE run() dışına sızan (top-level/
    kritik) bir hata non-zero (1) exit üretir - ör. DB'ye hiç
    bağlanılamadı, beklenmeyen bir programlama hatası. `failed_airports`
    (kısmi, havalimanı-bazlı izole hata) TEK BAŞINA process'i başarısız
    SAYMAZ: run_predictions() bunu zaten izole edip loglayarak devam
    ediyor (bkz. engine.py) - 3 havalimanından 1'i başarısız olsa bile
    diğer 2'sinin tahminleri kalıcı ve doğru. Kısmi hatayı da non-zero
    sayıp her 30 dakikada bir sürekli "FAILED" alarmı üretmek, gerçek/
    kritik kesintileri (API tamamen düştü, DB erişilemez) gürültüde
    kaybettirir - bu yüzden kısmi hata sadece WARNING olarak loglanır,
    exit code'u ETKİLEMEZ.
    """
    try:
        summary = run()
    except Exception:
        logger.exception("pipeline run başarısız oldu (kritik/top-level hata)")
        return 1

    for key, value in summary.items():
        print(f"{key}: {value}")

    if summary["failed_airports"]:
        logger.warning(
            "run tamamlandı ama bazı havalimanları başarısız oldu "
            "(izole edildi, kritik değil): %s",
            summary["failed_airports"],
        )

    return 0


if __name__ == "__main__":
    import sys

    from ..logging_config import configure_logging

    configure_logging()
    sys.exit(main())
