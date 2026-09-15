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

from ..db import get_session, init_db
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
        aircraft_index = build_aircraft_index(source_b())
    except (OSError, ValueError) as exc:
        logger.warning(
            "Kaynak B (canlı uçuş / aircraft_icao beslemesi) okunamadı "
            "(%s); fallback olarak boş enrichment index kullanılıyor - "
            "bu turda aircraft_icao eşleşmesi eksik kalabilir.",
            type(exc).__name__,
        )
        aircraft_index = {}

    rows: list[dict] = []
    for direction in SOURCE_A_FILES:
        records = source_a(direction)
        rows.extend(
            parse_source_a(records, direction, countries, aircraft_index)
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
    """
    init_db()
    session = get_session()
    try:
        airports_loaded = ensure_airports(session, data_dir)
        rows = load_flight_rows(session, data_dir, source_a, source_b)
        match_rate = aircraft_match_rate(rows)
        refreshed = refresh_flights(session, rows)

        predicted = run_predictions(
            session,
            resolver=AircraftCapacityService(session),
            update_baseline=update_baseline,
        )
    finally:
        session.close()

    return {
        "airports_loaded": airports_loaded,
        "flights_parsed": len(rows),
        "aircraft_match_rate": round(match_rate, 3),
        **refreshed,
        "predictions": predicted["predictions"],
        "pruned": predicted["pruned"],
        "airports_predicted": len(predicted["airports"]),
    }


if __name__ == "__main__":
    from ..logging_config import configure_logging

    configure_logging()
    summary = run()
    for key, value in summary.items():
        print(f"{key}: {value}")
