"""
Date-shift replay runner - GERÇEK production pipeline zincirini, izole
bir SQLite DB'ye karşı, ya gerçek `data/` dosyalarıyla ya da bu
klasördeki +4 gün kaydırılmış kopyalarıyla çalıştırır.

Gerçek `database.sqlite`'a HİÇ dokunulmaz. Her `replay()` çağrısı
KENDİ, tamamen izole `sqlalchemy.create_engine(f"sqlite:///{db_path}")`
motorunu kurar ve `app.db`'nin global (bir kez, ilk import'ta bağlanan)
`engine`/`SessionLocal`'ını HİÇ KULLANMAZ - bu, `tests/test_production_
shape_hourly_replay.py`/`tests/test_final_realistic_replay.py` gibi bu
oturumdaki DİĞER tüm izole-DB testleriyle AYNI, kanıtlanmış desendir.

(Not: `os.environ["DATABASE_URL"]` ile `app.db`'yi yönlendirmeye
çalışan bir ilk versiyon, aynı pytest process'inde birden fazla
`replay()` çağrısı yapıldığında `app.db.engine`'in SADECE ilk import'ta
kurulup module-cache'lendiğini, sonraki env var değişikliklerinin hiç
etkili olmadığını ortaya çıkardı - bu YANLIŞ yaklaşım terk edildi,
production kodu bundan ETKİLENMEDİ, sadece bu test runner'ı düzeltildi.)

Bu modül hem `python tests/date_shift_replay/run_date_shift_replay.py`
ile CLI'dan hem de `tests/test_date_shift_replay.py`'den import edilerek
kullanılabilir.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

REPLAY_DIR = Path(__file__).resolve().parent
REPO_ROOT = REPLAY_DIR.parents[1]
REAL_DATA_DIR = REPO_ROOT / "data"

sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models import AircraftCapacity, Base  # noqa: E402
from app.queue.api import airport_predictions  # noqa: E402
from app.queue.constants import (  # noqa: E402
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    EXCLUDED_STATUSES,
)
from app.queue.domain.demand import effective_time  # noqa: E402
from app.queue.domain.operational_day import (  # noqa: E402
    filter_flights_for_operational_day,
    flight_reference_time,
    operational_day_window,
    resolve_airport_timezone,
)
from app.queue.engine import run_predictions  # noqa: E402
from app.queue.ingestion.airports_import import country_lookup, import_airports  # noqa: E402
from app.queue.ingestion.refresh import refresh_flights  # noqa: E402
from app.queue.ingestion.sources import build_aircraft_index, load_source_payload, parse_source_a  # noqa: E402
from app.queue.models import Airport, Flight  # noqa: E402
from app.queue.pipeline import ensure_airport_scales  # noqa: E402
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset  # noqa: E402
from app.service import AircraftCapacityService  # noqa: E402


def compute_airport_now(flights: list, tz, fallback_now: datetime) -> tuple[datetime, datetime | None]:
    """
    ADIM (Airport-Bazlı Replay Now) - bu havalimanının KENDİ shift
    edilmiş flight'larından, GERÇEK production queue-event mantığını
    (`domain/demand.py:effective_time()` - departure: -120dk, arrival:
    +15dk, -TEKRAR ELLE YAZILMADI, doğrudan production fonksiyonu
    çağrılıyor) kullanarak bir replay `now` türetir.

    Kural: airport_now = first_queue_event - 30 dakika (Bölüm 4).
    Güvenlik: eğer bu değer, tetikleyici flight'ı KENDİ operasyonel
    günü (`operational_day_window()` - production fonksiyonu) dışına
    düşürüyorsa, en yakın güvenli değere (tetikleyici flight'ın KENDİ,
    offsetsiz `flight_reference_time()`'ı - bu her zaman kendi
    operasyonel günü içinde, tanım gereği) düşülür - manuel timezone
    offset hard-code YOK, hepsi production `domain/operational_day.py`
    fonksiyonlarına devredilmiş.

    flights'ta hiç kullanılabilir queue-event yoksa (iptal/yönlendirme
    dışında hiç referans zamanı olan flight yoksa) `fallback_now`
    (çağıranın verdiği global replay now) AYNEN döner - hard-code
    YOK, sadece "hesaplanamadı" durumunun güvenli düşüşü.

    Döner: (airport_now, first_queue_event veya None)
    """
    candidates: list[tuple[datetime, object]] = []
    for flight in flights:
        if flight.status in EXCLUDED_STATUSES:
            continue
        moment = effective_time(flight)
        if moment is None:
            continue
        candidates.append((moment, flight))

    if not candidates:
        return fallback_now, None

    candidates.sort(key=lambda pair: pair[0])
    first_queue_event, trigger_flight = candidates[0]
    airport_now = first_queue_event - timedelta(minutes=30)

    if tz is not None:
        reference = flight_reference_time(trigger_flight)
        if reference is not None:
            start, end = operational_day_window(tz, airport_now)
            if not (start <= reference < end):
                airport_now = reference

    return airport_now, first_queue_event


def replay(
    fixture_dir: Path,
    db_path: Path,
    now: datetime,
    airports: list[str] | None = None,
    reset_db: bool = True,
    per_airport_now: bool = False,
    exclude_flight_iata_prefix: str | None = None,
) -> dict:
    """
    fixture_dir : Kaynak A/B dosyalarının okunacağı klasör - gerçek
                   `data/` VEYA `tests/date_shift_replay/` (shifted).
    db_path     : İZOLE SQLite dosya yolu (asla gerçek database.sqlite) -
                   BU ÇAĞRIYA ÖZEL, TAMAMEN YENİ bir engine kurulur.
    now         : `per_airport_now=False` (varsayılan, ESKİ davranış -
                   geriye dönük uyumlu) iken TÜM havalimanları için
                   `run_predictions()`/operational-day filtresine
                   verilen TEK "şu an". `per_airport_now=True` iken
                   sadece hesaplanamayan (queue-event'i olmayan)
                   havalimanlar için GÜVENLİ DÜŞÜŞ (fallback) değeri
                   olarak kullanılır - artık TEK global now DEĞİL.
    airports    : `run_predictions(airports=...)` - açıkça bir liste
                   verilirse (ör. ["CDG"]) SADECE o havalimanları
                   işlenir (mevcut, geriye dönük uyumlu davranış).
                   ADIM (Multi-Airport Date-Shift Replay): None
                   verilirse (varsayılan) `flight_airports.sql`'in
                   TÜMÜ (9766 satır) DEĞİL, bu fixture'da GERÇEKTEN
                   parse edilmiş satırlardaki `airport_iata` kümesi
                   (hard-code YOK, tek havalimanına filtre YOK)
                   otomatik türetilip kullanılır - shift edilmiş
                   veride kaç GERÇEK operational havalimanı varsa
                   hepsi işlenir.
    per_airport_now : ADIM (Airport-Bazlı Replay Now). False (varsayılan)
                   iken ESKİ, TEK-global-now davranışı AYNEN korunur
                   (mevcut `cdg_original`/`cdg_shifted` fixture'ları
                   ETKİLENMEZ). True verilirse HER havalimanı KENDİ
                   `compute_airport_now()` ile türetilmiş, KENDİ
                   flight timeline'ına göre hesaplanmış ayrı bir
                   `now` ile - AYRI, İZOLE `run_predictions(airports=
                   [code], now=airport_now)` çağrısıyla - işlenir.
                   Production `run_predictions`/`operational_day.py`
                   HİÇ DEĞİŞMEDEN, SADECE bu dosyadan farklı `now`
                   değerleriyle, havalimanı başına ayrı ayrı çağrılır.
    exclude_flight_iata_prefix : ADIM (Date-Shift Replay Test Data
                   Expansion) - Bölüm 18: `REPLAY_DIR` artık gerçek
                   200 flight'ın ÜZERİNE TS9xxx işaretli test flight'ları
                   da içeriyor (bkz. `_generate_extra_test_flights.py`).
                   Bu parametre verilirse (ör. "TS9"), `flight_iata`'sı
                   bu prefix'le başlayan kayıtlar `parse_source_a()`'dan
                   SONRA, ingestion'dan ÖNCE elenir - SADECE bu test
                   dosyasındaki ESKİ, "saf +4 gün kaydırma" kimlik
                   testlerinin (CDG/CBR tek-havalimanı identity-proof)
                   eklenen test verisinden ETKİLENMEDEN çalışmaya devam
                   etmesi içindir. Verilmezse (None, varsayılan) HİÇBİR
                   filtre uygulanmaz - TÜM (gerçek + test) flight'lar
                   ingest edilir.
    """
    if reset_db and db_path.exists():
        db_path.unlink()

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    dep_path = fixture_dir / "Delays - Type Departures.json"
    arr_path = fixture_dir / "Delays - Type Arrivals.json"
    source_b_path = (
        fixture_dir / "flights_live.json" if (fixture_dir / "flights_live.json").exists()
        else fixture_dir / "response-delays.json"
    )

    if session.query(Airport).first() is None:
        import_airports(session, str(REAL_DATA_DIR / "flight_airports.sql"))
    if session.query(AircraftCapacity).first() is None:
        seed_verified_dataset(session)
        seed_curated_fallback(session)
        seed_family_and_ga(session)
    session.commit()

    countries = country_lookup(session)
    dep_records = load_source_payload(str(dep_path))
    arr_records = load_source_payload(str(arr_path))
    source_b_records = load_source_payload(str(source_b_path))
    aircraft_index = build_aircraft_index(source_b_records)

    dep_rows = parse_source_a(dep_records, DIRECTION_DEPARTURE, countries, aircraft_index)
    arr_rows = parse_source_a(arr_records, DIRECTION_ARRIVAL, countries, aircraft_index)
    all_rows = dep_rows + arr_rows

    if exclude_flight_iata_prefix is not None:
        all_rows = [
            row for row in all_rows
            if not (row.get("flight_iata") or "").startswith(exclude_flight_iata_prefix)
        ]

    # ADIM (Multi-Airport Date-Shift Replay) - Bölüm 3/4: tek havalimanı
    # filtresi YOK. `airports` açıkça verilmediyse, shift edilmiş veride
    # GERÇEKTEN bulunan TÜM operational havalimanı kodları (parsed satır
    # başına `airport_iata`) kullanılır - `flight_airports.sql`'in TÜMÜ
    # (9766 satır, çoğu bu fixture'da hiç uçuşu olmayan) DEĞİL.
    if airports is None:
        airports = sorted({
            row["airport_iata"] for row in all_rows if row.get("airport_iata")
        })

    refresh_result = refresh_flights(session, all_rows)

    # ADIM (Airport-Scale Bootstrap) - Bölüm 7: hard-code YOK, gerçek
    # production idempotent bootstrap fonksiyonu (`pipeline.py:ensure_
    # airport_scales()`) çağrılır - large/medium/small ölçek gerçek
    # `data/*_olcekli_havaalanlari.txt` dosyalarından, mevcut parser/
    # import mantığıyla çözülür.
    scale_result = ensure_airport_scales(session, data_dir=str(REAL_DATA_DIR))

    resolver = AircraftCapacityService(session)

    all_flights_by_airport = {}
    for code in (airports or []):
        all_flights_by_airport[code] = list(session.execute(
            select(Flight).where(Flight.airport_iata == code)
        ).scalars())

    if not per_airport_now:
        # ESKİ davranış - TEK global now, TÜM havalimanları AYNI anda.
        prediction_summary = run_predictions(
            session, resolver, airports=airports, update_baseline=False, now=now,
        )
        now_by_airport = {code: now for code in (airports or [])}
        first_queue_event_by_airport = {code: None for code in (airports or [])}
    else:
        # ADIM (Airport-Bazlı Replay Now) - Bölüm 7/8: her havalimanı
        # KENDİ flight'larından türetilmiş KENDİ `now`'uyla, AYRI/İZOLE
        # bir `run_predictions(airports=[code], now=airport_now)`
        # çağrısıyla işlenir - bir havalimanının `now`'u başka bir
        # havalimanının queue state'ini/predictions'ını HİÇ ETKİLEMEZ
        # (her çağrı kendi try/except+flights+config'iyle tamamen
        # bağımsız - bkz. `run_predictions()` içindeki per-airport loop).
        now_by_airport = {}
        first_queue_event_by_airport = {}
        merged = {
            "airports": {}, "predictions": 0, "pruned": 0,
            "failed_airports": [], "timezone_missing_airports": [],
            "inserted": 0, "updated": 0,
        }
        for code in (airports or []):
            airport_row = session.get(Airport, code)
            tz = resolve_airport_timezone(airport_row.timezone) if airport_row else None
            airport_now, first_event = compute_airport_now(
                all_flights_by_airport[code], tz, now,
            )
            now_by_airport[code] = airport_now
            first_queue_event_by_airport[code] = first_event

            single = run_predictions(
                session, resolver, airports=[code], update_baseline=False, now=airport_now,
            )
            merged["airports"].update(single["airports"])
            merged["predictions"] += single["predictions"]
            merged["pruned"] += single["pruned"]
            merged["failed_airports"] += single["failed_airports"]
            merged["timezone_missing_airports"] += single["timezone_missing_airports"]
            merged["inserted"] += single["inserted"]
            merged["updated"] += single["updated"]
        prediction_summary = merged

    selected_by_airport = {}
    for code in (airports or []):
        flights = all_flights_by_airport[code]
        airport_row = session.get(Airport, code)
        tz = resolve_airport_timezone(airport_row.timezone) if airport_row else None
        airport_now = now_by_airport[code]
        selected = (
            filter_flights_for_operational_day(flights, tz, airport_now)
            if tz is not None else flights
        )
        selected_by_airport[code] = selected

    api_by_airport = {
        code: airport_predictions(session, code, now=now_by_airport[code])
        for code in (airports or [])
    }

    scale_by_airport = {
        code: (session.get(Airport, code).scale if session.get(Airport, code) else None)
        for code in airports
    }

    return {
        "session": session,
        "engine": engine,
        "fixture_dir": str(fixture_dir),
        "now": now,
        "per_airport_now": per_airport_now,
        "now_by_airport": now_by_airport,
        "first_queue_event_by_airport": first_queue_event_by_airport,
        "airports": airports,
        "scale_by_airport": scale_by_airport,
        "scale_bootstrap_result": scale_result,
        "source_a_dep_count": len(dep_records),
        "source_a_arr_count": len(arr_records),
        "source_b_count": len(source_b_records),
        "parsed_flights": len(all_rows),
        "refresh": refresh_result,
        "prediction_summary": prediction_summary,
        "all_flights_by_airport": all_flights_by_airport,
        "selected_by_airport": selected_by_airport,
        "api_by_airport": api_by_airport,
    }


if __name__ == "__main__":
    NOW_SHIFTED_FALLBACK = datetime(2026, 9, 18, 20, 0)

    # ADIM (Airport-Bazlı Replay Now) - `airports=None`: shift edilmiş
    # veride GERÇEKTEN bulunan TÜM havalimanları (hard-code YOK).
    # `per_airport_now=True`: artık TEK global now YOK - her havalimanı
    # KENDİ flight timeline'ına göre hesaplanan KENDİ `now`'uyla
    # çalışır (bkz. `compute_airport_now()`/`replay()` docstring'i).
    # `NOW_SHIFTED_FALLBACK` SADECE hiç queue-event'i hesaplanamayan
    # (ör. tüm flight'ları iptal/yönlendirilmiş) nadir durum için
    # güvenli düşüş değeridir. Bu, browser'da (README.md'deki komutla)
    # açılacak KALICI DB'dir.
    print("=== SHIFTED (tests/date_shift_replay/, airport-bazlı ayrı now, TÜM havalimanları) ===")
    r2 = replay(
        REPLAY_DIR, REPLAY_DIR / "date_shift_replay.sqlite",
        NOW_SHIFTED_FALLBACK, airports=None, per_airport_now=True,
    )
    for k in ("parsed_flights", "refresh", "prediction_summary", "airports", "scale_bootstrap_result"):
        print(k, "=", r2[k])
    print("distinct airport count =", len(r2["airports"]))

    pred_generated = sum(1 for v in r2["prediction_summary"]["airports"].values() if v > 0)
    print(f"prediction generated airport count = {pred_generated}/{len(r2['airports'])}")
    r2["session"].close()
