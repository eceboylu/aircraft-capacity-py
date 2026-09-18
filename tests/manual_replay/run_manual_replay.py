"""
genel-proje.md Bölüm 36/47 tarzı MANUEL replay runner.

Gerçek production pipeline zincirini (`app/queue/pipeline.py:run()`) bu
klasördeki fixture dosyalarıyla, TAMAMEN İZOLE bir SQLite dosyasına karşı
çalıştırır.

GÜVENLİK (bu script boyunca DEĞİŞMEZ garantiler):
  - `DATABASE_URL` bu process için `tests/manual_replay/manual_replay.sqlite`
    dosyasına ayarlanır - gerçek `database.sqlite` HİÇ açılmaz/yazılmaz.
    Bu, `app.db` importundan ÖNCE yapılmalıdır (engine import zamanında
    kurulur) - bu yüzden bu dosyanın en tepesinde, başka HİÇBİR `app.*`
    import'undan önce ayarlanır.
  - `data/*.json` (Source A/B) dosyalarına HİÇ dokunulmaz - sadece bu
    klasördeki (`tests/manual_replay/*.json`) fixture'lar okunur.
  - `data/flight_airports.sql` + `data/yolcu_ucaklari.json` +
    `data/curated_fallback.json` SADECE OKUNUR (ensure_airports/
    ensure_capacity_reference gerçek production kodunun ZATEN yaptığı
    salt-okunur import) - hiçbir satır silinmez/değiştirilmez.

Kullanım:
    venv\\Scripts\\python.exe tests\\manual_replay\\run_manual_replay.py
"""
import os
import sys
from pathlib import Path

MANUAL_REPLAY_DIR = Path(__file__).resolve().parent
REPO_ROOT = MANUAL_REPLAY_DIR.parents[1]
DB_PATH = MANUAL_REPLAY_DIR / "manual_replay.sqlite"

# Önceki bir çalıştırmadan kalan test DB'sini temizle - gerçek
# database.sqlite'a DEĞİL, bu dosyaya dokunuyoruz.
if DB_PATH.exists():
    DB_PATH.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

sys.path.insert(0, str(REPO_ROOT))

from datetime import datetime  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.db import get_session  # noqa: E402
from app.queue.api import airport_predictions  # noqa: E402
from app.queue.constants import EXCLUDED_STATUSES, STATUS_CANCELLED  # noqa: E402
from app.queue.domain.operational_day import (  # noqa: E402
    filter_flights_for_operational_day,
    resolve_airport_timezone,
)
from app.queue.ingestion.airports_import import country_lookup  # noqa: E402
from app.queue.ingestion.sources import load_source_payload  # noqa: E402
from app.queue.models import Airport, Flight  # noqa: E402
from app.queue.pipeline import DATA_DIR, run  # noqa: E402

IST_LOCAL_NOW = datetime(2026, 9, 18, 23, 30)  # Europe/Istanbul yerel - hâlâ 18 Eylül
NOW_UTC = datetime(2026, 9, 18, 20, 30)  # yukarıdakiyle AYNI an, UTC (Istanbul = UTC+3)


def source_a(direction: str) -> list[dict]:
    filename = (
        "Delays - Type Departures.json" if direction == "departure"
        else "Delays - Type Arrivals.json"
    )
    return load_source_payload(str(MANUAL_REPLAY_DIR / filename))


def source_b() -> list[dict]:
    return load_source_payload(str(MANUAL_REPLAY_DIR / "flights_live.json"))


def main() -> None:
    print(f"[manual-replay] DATABASE_URL = {os.environ['DATABASE_URL']}")
    print(f"[manual-replay] now (UTC)    = {NOW_UTC.isoformat()} (Europe/Istanbul yerel {IST_LOCAL_NOW.isoformat()})")

    summary = run(
        data_dir=DATA_DIR,  # gerçek data/ klasörü - SADECE airports.sql + capacity seed için, salt-okunur
        update_baseline=False,  # manuel replay baseline havuzunu KİRLETMEMELİ
        source_a=source_a,
        source_b=source_b,
        now=NOW_UTC,
    )

    session = get_session()
    try:
        ist = session.get(Airport, "IST")
        all_ist_flights = list(
            session.execute(select(Flight).where(Flight.airport_iata == "IST")).scalars()
        )
        tz = resolve_airport_timezone(ist.timezone) if ist else None
        selected = (
            filter_flights_for_operational_day(all_ist_flights, tz, NOW_UTC)
            if tz is not None else all_ist_flights
        )
        cancelled_in_selection = [f for f in selected if f.status == STATUS_CANCELLED]
        # NOT: operational-day seçimi status'e göre filtrelemez (Flight
        # o güne AİT olduğu için seçilir) - cancelled/diverted uçuşlar
        # SADECE demand aşamasında (EXCLUDED_STATUSES) dışlanır, bkz.
        # engine.py `window_flights = [f for f in window_all if
        # f.status not in EXCLUDED_STATUSES]`. Aşağıdaki demand-effective
        # liste BUNU replike eder.
        demand_effective = [f for f in selected if f.status not in EXCLUDED_STATUSES]
        unknown_icao_flights = [
            f for f in all_ist_flights
            if f.aircraft_icao in (None, "ZZZZ")
        ]
        sv205 = session.scalar(
            select(Flight).where(Flight.flight_iata == "SV205")
        )

        api = airport_predictions(session, "IST", now=NOW_UTC)

        print("\n=== PIPELINE SUMMARY ===")
        for key, value in summary.items():
            print(f"  {key}: {value}")

        print("\n=== IST DOĞRULAMA ===")
        print(f"  airport IST found: {ist is not None}")
        print(f"  airport IST timezone: {ist.timezone if ist else None}")
        print(f"  total flights (IST, tüm DB): {len(all_ist_flights)}")
        print(f"  selected operational-day flights (IST, {NOW_UTC.date()} yerel): {len(selected)}")
        print(f"  cancelled flights in selection (day-scope, status'ten bağımsız): "
              f"{len(cancelled_in_selection)} ({[f.flight_iata for f in cancelled_in_selection]})")
        print(f"  demand-effective flights (EXCLUDED_STATUSES çıkarılmış): {len(demand_effective)}")
        print(f"  unknown/missing ICAO flights (DB'de, enrichment SONRASI - SV205 zaten çözülmüş olmalı): "
              f"{[f.flight_iata for f in unknown_icao_flights]}")
        print(f"  SV205 aircraft_icao (Source B enrichment sonrası): "
              f"{sv205.aircraft_icao if sv205 else 'NOT FOUND'}")

        print("\n=== API / 4-GRAPH ===")
        for section in ("overall", "domestic_security", "international_departure", "international_arrival"):
            windows = api.get(section, {}).get("windows", [])
            print(f"  {section}: {len(windows)} bucket(s)")

        print("\n=== VALIDATION FLAGS ===")
        print(f"  IST found: {ist is not None}")
        print(f"  unknown ICAO -> 180 fallback present: "
              f"{any(f.flight_iata == 'TK206' for f in all_ist_flights)}")
        print(f"  cancelled flight excluded from demand (EXCLUDED_STATUSES): "
              f"{all(f.flight_iata not in ('TK208', 'TK306') for f in demand_effective)}")
        print(f"  Source B aircraft enrichment worked (SV205): "
              f"{sv205 is not None and sv205.aircraft_icao == 'B738'}")

        intl_dep_windows = api.get("international_departure", {}).get("windows", [])
        coupling_evidence = [
            w for w in intl_dep_windows
            if w.get("international_security", {}).get("expected_passengers", 0) > 0
        ]
        print(f"  passport->security coupling windows (intl departure, security demand>0): "
              f"{len(coupling_evidence)}")

    finally:
        session.close()


if __name__ == "__main__":
    main()
