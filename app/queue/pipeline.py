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
from .domain.airport_scale import SCALE_RESOURCES, resource_view_for_scale
from .domain.retention_time import USAGE_HORIZON_HOURS
from .ingestion.sources import (
    aircraft_match_rate,
    build_aircraft_index,
    codeshare_skip_counts_by_airport,
    load_source_payload,
    parse_source_a,
)
from .models import Airport, AirportOperationalConfig, AirportScaleConfig

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")

AIRPORTS_SQL = "flight_airports.sql"
MEGA_SCALE_TXT = "mega_havaalanlari.txt"
LARGE_SCALE_TXT = "buyuk_olcekli_havaalanlari.txt"
MEDIUM_SCALE_TXT = "orta_olcekli_havaalanlari.txt"
SMALL_SCALE_TXT = "kucuk_olcekli_havaalanlari.txt"
SOURCE_A_FILES = {
    DIRECTION_ARRIVAL: "Delays - Type Arrivals.json",
    DIRECTION_DEPARTURE: "Delays - Type Departures.json",
}
SOURCE_B_FILE = "response-delays.json"

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
    if session.query(Airport).first() is not None:
        return 0
    return import_airports(session, _path(data_dir, AIRPORTS_SQL))


def ensure_airport_scales(session, data_dir: str = DATA_DIR) -> dict | None:
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


def ensure_airport_scale_resource_config(session) -> int:
    """
    `airport_scale_configs` tablosuna, HENÜZ satırı olmayan her scale
    (mega/large/medium/small) için `domain/airport_scale.py:SCALE_
    RESOURCES` Python sabitinin O ANKİ değerlerinin BİR KOPYASINI ekler
    - "phpMyAdmin'den göremiyorum/değiştiremiyorum" sorununu çözer.

    ÖNEMLİ (config.py:_build_config_view ile AYNI, ZATEN VAR OLAN
    precedence): bir scale için satır BİR KEZ oluşturduktan sonra, o
    satır PHP'den/SQL'den DÜZENLENEBİLİR ve `get_config()`/`get_configs()`
    HER ZAMAN bu satırı Python sabitinin ÖNÜNE koyar - `AirportOperational
    Config.is_seeded_default` gibi bir "hâlâ default'u takip ediyor" ayrımı
    BURADA YOK (scale sabitleri, tek bir sistem geneli tanım - havalimanına
    özel override farkı yok). Yani bu fonksiyon SADECE satır YOKSA
    seed eder; satır zaten VARSA (ister bu fonksiyon ister kullanıcı
    oluşturmuş olsun) ASLA üzerine YAZMAZ - `SCALE_RESOURCES` kodda
    değişse bile, DB'de zaten bir satır varsa o satır kalıcı olarak
    kazanır (kullanıcı bunu SQL'den silip/güncelleyerek YÖNETİR).
    """
    existing_scales = {
        row.scale for row in session.query(AirportScaleConfig.scale).all()
    }
    created = 0
    for scale, resources in SCALE_RESOURCES.items():
        if scale in existing_scales:
            continue
        session.add(AirportScaleConfig(
            scale=scale,
            departure_passport_servers=resources["departure_passport_servers"],
            departure_passport_servers_max=resources.get("departure_passport_servers_max"),
            arrival_passport_servers=resources["arrival_passport_servers"],
            arrival_passport_servers_max=resources.get("arrival_passport_servers_max"),
            domestic_security_lanes=resources["domestic_security_lanes"],
            international_security_lanes=resources["international_security_lanes"],
            international_security_lanes_max=resources.get("international_security_lanes_max"),
            security_dynamic_control_interval_minutes=resources.get("security_dynamic_control_interval_minutes"),
            passport_dynamic_control_interval_minutes=resources.get("passport_dynamic_control_interval_minutes"),
            passport_departure_control_interval_minutes=resources.get("passport_departure_control_interval_minutes"),
            passport_arrival_control_interval_minutes=resources.get("passport_arrival_control_interval_minutes"),
            dynamic_target_utilization=resources.get("dynamic_target_utilization"),
            passport_arrival_lookahead_minutes=resources.get("passport_arrival_lookahead_minutes"),
        ))
        created += 1
    if created:
        session.commit()
    return created


def ensure_airport_operational_configs(session) -> dict:
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
            continue
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


def resync_security_service_time(session) -> int:
    from .constants import SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES

    # ADIM (Security Service Time - 50 saniye) - MySQL `FLOAT` kolonu
    # (4 byte, ~7 anlamlı basamak) tekrarlayan ondalıkları (50/60=
    # 0.8333333333333334...) TAM saklayamıyor; SQL-taraflı `!=` filtresi
    # bu yüzden (stored≈0.833333 vs Python sabiti) HER ZAMAN "farklı"
    # görüp gereksiz yere resync ediyordu (1.0 gibi "temiz" değerlerle
    # bu görülmüyordu). Karşılaştırma artık Python tarafında, küçük bir
    # tolerans ile yapılıyor - şema/kolon tipi DEĞİŞMEDİ (migration yok),
    # sadece "gerçekten farklı mı" testi artık float rounding noise'una
    # duyarsız.
    rows = (
        session.query(AirportOperationalConfig)
        .filter(AirportOperationalConfig.is_seeded_default.is_(True))
        .all()
    )
    changed = [
        row for row in rows
        if abs(row.security_service_time_minutes - SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES) > 1e-6
    ]
    for row in changed:
        row.security_service_time_minutes = SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES
    if changed:
        session.commit()
    return len(changed)


class CapacitySeedError(RuntimeError):
    pass


def ensure_capacity_reference(session) -> bool:
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
    except Exception as exc:  # noqa: BLE001
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
    def provide(direction: str) -> list[dict]:
        files = GENERATED_SOURCE_A_FILES if _local_generated_source_mode() else SOURCE_A_FILES
        return load_source_payload(_path(data_dir, files[direction]))
    return provide


def file_source_b(data_dir: str = DATA_DIR):
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
) -> tuple[list[dict], dict[str, int]]:
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
    # ADIM (Codeshare Audit Attribution) - `queue_routing_summary_audit.
    # codeshare_records_removed`'ı havalimanı başına doldurabilmek için;
    # `parse_source_a()`'nın KENDİSİ değiştirilmedi, sadece AYNI dedup
    # sonucunu (`codeshare_skip_counts_by_airport`) tekrar kullanan
    # salt-okunur bir sayım - iki kez HTTP/dosya okuması YAPILMAZ (aynı
    # `records` listesi üzerinde, bellek içi).
    codeshare_removed_by_airport: dict[str, int] = {}
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
        for airport, count in codeshare_skip_counts_by_airport(records, direction).items():
            codeshare_removed_by_airport[airport] = (
                codeshare_removed_by_airport.get(airport, 0) + count
            )

    logger.info(
        "ingestion sonucu: %d uçuş satırı ayrıştırıldı (Kaynak A ham kayıt=%d, Kaynak B ham kayıt=%d)",
        len(rows), source_a_total, len(source_b_records),
    )
    return rows, codeshare_removed_by_airport


def run(
    data_dir: str = DATA_DIR,
    update_baseline: bool = True,
    source_a=None,
    source_b=None,
    now=None,
    apply_usage_horizon: bool = False,
) -> dict:
    start = time.monotonic()
    logger.info("pipeline run started")

    init_db()
    session = get_session()
    try:
        airports_loaded = ensure_airports(session, data_dir)
        scales_imported = ensure_airport_scales(session, data_dir)
        scale_resource_configs_seeded = ensure_airport_scale_resource_config(session)
        operational_configs = ensure_airport_operational_configs(session)
        security_service_time_resynced = resync_security_service_time(session)
        capacity_seeded = ensure_capacity_reference(session)
        rows, codeshare_removed_by_airport = load_flight_rows(
            session, data_dir, source_a, source_b, now=now,
        )
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
            codeshare_removed_by_airport=codeshare_removed_by_airport,
        )
        logger.info(
            "prediction completed: predictions=%d pruned=%d airports_ok=%d airports_failed=%d",
            predicted["predictions"], predicted["pruned"],
            len(predicted["airports"]), len(predicted["failed_airports"]),
        )
        if predicted["failed_airports"]:
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
        "scale_resource_configs_seeded": scale_resource_configs_seeded,
        "operational_configs_seeded": operational_configs["created"],
        "operational_configs_resynced": operational_configs["resynced"],
        "security_service_time_resynced": security_service_time_resynced,
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
