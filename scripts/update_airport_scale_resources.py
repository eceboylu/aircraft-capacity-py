#!/usr/bin/env python3
"""
ADIM (4-Tier Airport Scale) - MySQL data refresh.

Bu script kod-seviyesi 3-tier -> 4-tier (mega/large/medium/small)
geçişini MEVCUT bir veritabanına uygular. Yeni kurulan (boş) bir
veritabanı için buna gerek YOK - `app.queue.pipeline.run()` zaten
`ensure_airport_scales()`/`ensure_airport_operational_configs()`
üzerinden 4-tier contract'ı ilk seferde doğru kurar. Bu script SADECE
ÖNCEDEN 3-tier ile scale'i çözülmüş satırları güncellemek içindir -
`pipeline.ensure_airport_scales()` "en az bir Airport.scale IS NOT
NULL satırı varsa atla" kuralı yüzünden bu güncellemeyi KENDİLİĞİNDEN
yapmaz (bilinçli tasarım - her 5dk'lık refresh döngüsünde 4 txt
dosyasını sessizce yeniden parse edip yazmasın diye).

Yapılan İKİ adım:
  1) `Airport.scale` GÜNCEL 4 txt dosyasına (data/mega_havaalanlari.txt,
     data/buyuk_olcekli_havaalanlari.txt, data/orta_olcekli_havaalanlari.txt,
     data/kucuk_olcekli_havaalanlari.txt) göre yeniden çözülür
     (`refresh_airport_scales()` - hiçbir listede bulunamayan bir
     airport'un MEVCUT scale'i KORUNUR, rastgele/uydurma scale
     ATANMAZ; aynı kod 2+ listede varsa `scale=None`).
  2) `AirportOperationalConfig`'in scale-derived (`is_seeded_default=
     True`) satırları YENİ `SCALE_RESOURCES` sayılarına resync edilir
     (`ensure_airport_operational_configs()` - insan eliyle override
     edilmiş satırlara -`is_seeded_default=False`- HİÇ DOKUNULMAZ).

IDEMPOTENT: aynı komut ikinci kez çalıştırıldığında `scale.updated`/
`config.resynced`/`config.created` sıfıra düşer - hiçbir satır
DUPLICATE olmaz, hiçbir override EZİLMEZ.

Kullanım (DATABASE_URL zorunlu - bkz. app/db.py, SQLite fallback YOK):

    DATABASE_URL="mysql+pymysql://user:pass@host:3306/dbname?charset=utf8mb4" \\
        python scripts/update_airport_scale_resources.py

`--report-only` ile hiçbir yazma yapmadan sadece mevcut durumu
(scale sayıları + her tier için bir örnek havalimanının effective
config'i) raporlar.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.db import get_session, init_db  # noqa: E402
from app.queue.config import get_config  # noqa: E402
from app.queue.domain.airport_scale import SCALES  # noqa: E402
from app.queue.ingestion.airports_import import refresh_airport_scales  # noqa: E402
from app.queue.models import Airport  # noqa: E402
from app.queue.pipeline import (  # noqa: E402
    DATA_DIR,
    LARGE_SCALE_TXT,
    MEDIUM_SCALE_TXT,
    MEGA_SCALE_TXT,
    SMALL_SCALE_TXT,
    ensure_airport_operational_configs,
)


def _scale_txt_paths(data_dir: str) -> tuple[str, str, str, str]:
    join = lambda name: str(Path(data_dir) / name)  # noqa: E731
    return (
        join(MEGA_SCALE_TXT), join(LARGE_SCALE_TXT),
        join(MEDIUM_SCALE_TXT), join(SMALL_SCALE_TXT),
    )


def run_refresh(session, data_dir: str = DATA_DIR) -> dict:
    """İki adımı da uygular ve her ikisinin özetini döner."""
    mega_path, large_path, medium_path, small_path = _scale_txt_paths(data_dir)
    scale_result = refresh_airport_scales(
        session, mega_path, large_path, medium_path, small_path,
    )
    config_result = ensure_airport_operational_configs(session)
    return {"scale": scale_result, "config": config_result}


def report(session) -> None:
    """
    Bölüm 9/22 - `SELECT scale, COUNT(*) FROM airports GROUP BY scale`
    + her tier'dan bir representative airport'un GERÇEK effective
    config'i (`config.py:get_config()` - production'ın KENDİ
    resolution zinciri, kopyası DEĞİL).
    """
    counts = dict(
        session.execute(
            select(Airport.scale, func.count(Airport.iata_code)).group_by(Airport.scale)
        ).all()
    )
    print("SCALE COUNTS:")
    for scale in (*SCALES, None):
        label = scale if scale is not None else "null"
        print(f"  {label}: {counts.get(scale, 0)}")

    print()
    print("REPRESENTATIVE EFFECTIVE CONFIG:")
    for scale in SCALES:
        airport = session.execute(
            select(Airport).where(Airport.scale == scale).limit(1)
        ).scalar_one_or_none()
        if airport is None:
            print(f"  {scale}: (bu ölçekte havalimanı yok)")
            continue
        cfg = get_config(session, airport.iata_code)
        print(
            f"  {scale.upper()} - {airport.iata_code}: "
            f"domestic_security={cfg.domestic_security_lane_count} "
            f"departure_passport={cfg.passport_departure_server_count} "
            f"international_security={cfg.international_security_lane_count} "
            f"arrival_passport={cfg.passport_arrival_server_count}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-only", action="store_true",
        help="Hiçbir yazma yapma, sadece mevcut scale/config durumunu raporla.",
    )
    parser.add_argument(
        "--data-dir", default=DATA_DIR,
        help="4 scale txt dosyasının bulunduğu klasör (varsayılan: data/).",
    )
    args = parser.parse_args()

    init_db()
    session = get_session()
    try:
        if not args.report_only:
            result = run_refresh(session, args.data_dir)
            print("SCALE REFRESH:", result["scale"])
            print("CONFIG RESYNC:", result["config"])
            print()
        report(session)
    finally:
        session.close()


if __name__ == "__main__":
    main()
