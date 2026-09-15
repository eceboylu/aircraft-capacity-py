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

from ..db import get_session, init_db
from ..models import AircraftCapacity
from ..seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset
from ..service import AircraftCapacityService
from .constants import DIRECTION_ARRIVAL, DIRECTION_DEPARTURE
from .engine import run_predictions
from .ingestion.airports_import import country_lookup, import_airports
from .ingestion.refresh import refresh_flights
from .ingestion.sources import (
    aircraft_match_rate,
    build_aircraft_index,
    load_source_payload,
    parse_source_a,
)
from .models import Airport

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")

AIRPORTS_SQL = "flight_airports.sql"
# Kaynak A - tarife/gecikme beslemesi, yön başına bir dosya.
SOURCE_A_FILES = {
    DIRECTION_ARRIVAL: "Delays - Type Arrivals.json",
    DIRECTION_DEPARTURE: "Delays - Type Departures.json",
}
# Kaynak B - canlı uçuş beslemesi, aircraft_icao'nun kaynağı.
SOURCE_B_FILE = "response-delays.json"


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
    """Örnek dosyalardan okuyan varsayılan Kaynak A sağlayıcısı."""
    def provide(direction: str) -> list[dict]:
        return load_source_payload(_path(data_dir, SOURCE_A_FILES[direction]))
    return provide


def file_source_b(data_dir: str = DATA_DIR):
    """Örnek dosyadan okuyan varsayılan Kaynak B sağlayıcısı."""
    def provide() -> list[dict]:
        return load_source_payload(_path(data_dir, SOURCE_B_FILE))
    return provide


def load_flight_rows(
    session,
    data_dir: str = DATA_DIR,
    source_a=None,
    source_b=None,
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
    """
    countries = country_lookup(session)
    source_a = source_a or file_source_a(data_dir)
    source_b = source_b or file_source_b(data_dir)

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
            parse_source_a(records, direction, countries, aircraft_index)
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
    """
    start = time.monotonic()
    logger.info("pipeline run started")

    init_db()
    session = get_session()
    try:
        airports_loaded = ensure_airports(session, data_dir)
        capacity_seeded = ensure_capacity_reference(session)
        rows = load_flight_rows(session, data_dir, source_a, source_b)
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
