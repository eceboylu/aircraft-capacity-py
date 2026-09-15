"""
MADDE 1 - Seed script

Veri kaynakları:
  - data/yolcu_ucaklari.json
      -> ANA KAYNAK, doğrulanmış yolcu uçakları
  - data/curated_fallback.json
      -> yaygın ama ana listede eksik varyantlar

GÜVENLİK:
  - Normal çalışmada mevcut veritabanı tabloları SİLİNMEZ.
  - Veritabanını sıfırlamak gerekiyorsa açıkça --reset verilmelidir:
        python -m app.seed --reset

UYARI:
  --reset canlı/production veritabanında KULLANILMAMALIDIR.
  Çünkü mevcut tabloları ve içlerindeki verileri siler.
"""

import argparse
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import delete

from .db import get_session, init_db
from .models import (
    AircraftCapacity,
    AircraftCapacityFamily,
)

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def load_json(filename: str):
    path = os.path.join(DATA_DIR, filename)

    with open(path, encoding="utf-8") as f:
        return json.load(f)


def seed_verified_dataset(session):
    """aircraft_capacity tablosunun ana verisi: yolcu_ucaklari.json."""

    aircraft_list = load_json("yolcu_ucaklari.json")
    now = datetime.now(timezone.utc)
    count = 0

    for item in aircraft_list:
        session.merge(
            AircraftCapacity(
                icao_code=item["icao"].upper(),
                name=item["name"],
                category=item["category"],
                capacity=item["typicalCapacity"],
                source="verified_dataset",
                confidence="high",
                recalculated_at=now,
                created_at=now,
                updated_at=now,
            )
        )

        count += 1

    session.commit()

    logger.info("aircraft_capacity (verified_dataset): %d satır", count)


def seed_curated_fallback(session):
    """Ana JSON'da olmayan uçak tiplerini fallback JSON'dan ekler."""

    existing_codes = {
        row.icao_code
        for row in session.query(
            AircraftCapacity.icao_code
        ).all()
    }

    fallback_list = load_json("curated_fallback.json")

    now = datetime.now(timezone.utc)
    added = 0

    for item in fallback_list:
        icao = item["icao"].upper()

        if icao in existing_codes:
            continue

        session.merge(
            AircraftCapacity(
                icao_code=icao,
                name=None,
                category=None,
                capacity=item["capacity"],
                source="curated_fallback",
                confidence="medium",
                recalculated_at=now,
                created_at=now,
                updated_at=now,
            )
        )

        added += 1

    session.commit()

    logger.info("aircraft_capacity (curated_fallback, ek): %d satır", added)


def seed_family_and_ga(session):
    """
    Kategori/önek fallback + genel havacılık kuralları.

    Bunlar ana uçak kapasite verisi değil,
    çözümleme sırasında kullanılacak fallback kurallarıdır.
    """

    commercial = {
        "A38": ("super_wide", 480),
        "A35": ("widebody_large", 330),
        "A34": ("widebody_large", 320),
        "A33": ("widebody_medium", 280),
        "A32": ("narrowbody", 175),
        "A31": ("narrowbody_small", 130),
        "A22": ("narrowbody_small", 130),
        "A21": ("narrowbody_large", 200),
        "A20": ("narrowbody", 165),
        "A19": ("narrowbody_small", 140),
        "B78": ("widebody_medium", 280),
        "B77": ("widebody_large", 350),
        "B76": ("widebody_medium", 240),
        "B74": ("widebody_large", 400),
        "B73": ("narrowbody", 170),
        "B38": ("narrowbody", 180),
        "CRJ": ("regional_jet", 80),
        "E1": ("regional_jet", 90),
        "E2": ("regional_jet", 110),
        "SU9": ("regional_jet", 98),
        "AT4": ("turboprop", 48),
        "AT7": ("turboprop", 70),
        "DH8": ("turboprop", 60),
        "BCS": ("narrowbody_small", 120),
    }

    general_aviation = [
        "C1",
        "C2",
        "C3",
        "C4",
        "C5",
        "C6",
        "C7",
        "C8",
        "C9",
        "CE",
        "LJ",
        "GL",
        "GA",
        "BE",
        "PA",
        "P2",
        "P3",
        "F2",
        "F9",
        "FA",
        "S7",
        "S9",
        "EC",
        "DA",
        "DV",
        "SR",
        "HDJT",
        "PC",
        "PIVI",
        "HA4",
        "D228",
        "CL",
        "G2",
        "G1",
        "GX",
        "E5",
        "B35",
        "E35",
        "H25",
        "SW4",
        "A139",
    ]

    # Bu tablo yalnızca fallback kurallarından oluştuğu için
    # yeniden seed edildiğinde eski kuralları temizleyip
    # güncel kuralları oluşturuyoruz.
    session.execute(delete(AircraftCapacityFamily))

    for prefix, (category, capacity) in commercial.items():
        session.add(
            AircraftCapacityFamily(
                icao_prefix=prefix,
                category=category,
                default_capacity=capacity,
                counts_toward_passenger_total=True,
            )
        )

    for prefix in general_aviation:
        session.add(
            AircraftCapacityFamily(
                icao_prefix=prefix,
                category="general_aviation",
                default_capacity=0,
                counts_toward_passenger_total=False,
            )
        )

    session.commit()

    logger.info(
        "aircraft_capacity_family: %d satır",
        len(commercial) + len(general_aviation),
    )


def run(reset: bool = False):
    """
    Seed işlemini çalıştırır.

    Normal:
        python -m app.seed

    Sıfırlayarak:
        python -m app.seed --reset
    """

    if reset:
        logger.warning(
            "--reset kullanıldı: mevcut veritabanı tabloları silinip "
            "yeniden oluşturulacak."
        )

    # Normal çalışmada drop_first=False.
    # --reset verilirse bilinçli olarak veritabanı sıfırlanır.
    init_db(drop_first=reset)

    session = get_session()

    try:
        seed_verified_dataset(session)
        seed_curated_fallback(session)
        seed_family_and_ga(session)

    finally:
        session.close()

    logger.info("Seed tamamlandı.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Aircraft capacity seed işlemi"
    )

    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Veritabanını sıfırlar. "
            "SADECE development/test ortamında kullanın."
        ),
    )

    return parser.parse_args()


if __name__ == "__main__":
    from .logging_config import configure_logging

    configure_logging()
    args = parse_args()
    run(reset=args.reset)