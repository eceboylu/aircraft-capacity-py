"""
ADIM (MySQL Performance Regression Audit) - gerçek MySQL üzerinde,
gerçek production kod yolu (`app.worker._build_live_run_fn()` ->
`app.queue.ingestion.airlabs_client` -> `app.queue.pipeline.run()` ->
ingestion -> prediction -> persistence) üzerinden ölçekli, tekrarlanabilir
bir performans/SQL-instrumentation benchmark'ı.

Bu script PRODUCTION KODUNU DEĞİŞTİRMEZ - sadece ölçüm amacıyla, SADECE
bu process'in ömrü boyunca geçerli monkeypatch tabanlı zamanlayıcı
sarmalayıcılar + SQLAlchemy engine event hook'ları kullanır (queue
matematiği/business logic HİÇ değiştirilmez, sadece etrafına zamanlayıcı
sarılır).

GÜVENLİK:
  - `DATABASE_URL` GERÇEK/production bir veritabanına işaret ETMEMELİDİR.
    Bu script, veritabanı adında "bench" GEÇMEYEN bir DATABASE_URL ile
    ÇALIŞMAYI REDDEDER (aşağıdaki `_ensure_bench_database` - `app/db.py`
    içindeki `ALLOW_DESTRUCTIVE_DB_RESET` guard'ından TAMAMEN AYRI, EK
    bir koruma katmanı).
  - Gerçek AirLabs API'ye HİÇ bağlanılmaz - `AIRLABS_API_KEY` sahte bir
    değerdir, `urllib.request.urlopen` tamamen bu script'in kendi
    deterministic fake transport'una yönlendirilir.

Kullanım:
    DATABASE_URL="mysql+pymysql://bench_app:bench_app_pw@127.0.0.1:3309/aircraft_capacity_bench_1k?charset=utf8mb4" \\
        venv/Scripts/python.exe scripts/benchmark_mysql_scale.py --flights 1300 --airports 25
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ensure_bench_database(database_url: str) -> None:
    """
    EK güvenlik katmanı (`app/db.py`'nin `ALLOW_DESTRUCTIVE_DB_RESET`
    guard'ından AYRI) - bu script SADECE veritabanı adında "bench"
    GEÇEN bir DATABASE_URL ile çalışabilir. Production/local-dev DB'sine
    YANLIŞLIKLA bağlanmayı ENGELLER.
    """
    try:
        from sqlalchemy.engine import make_url
        db_name = (make_url(database_url).database or "").lower()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"DATABASE_URL ayrıştırılamadı: {exc}")
    if "bench" not in db_name:
        raise SystemExit(
            f"REDDEDİLDİ: veritabanı adı ({db_name!r}) 'bench' İÇERMİYOR - "
            "bu script SADECE açıkça isimlendirilmiş bir benchmark DB'sinde "
            "çalışabilir (ör. aircraft_capacity_bench_1k). Production/local-dev "
            "DB'sine yanlışlıkla bağlanmayı önlemek için REFUSE edildi."
        )


DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL zorunlu - bir benchmark MySQL DB'sine işaret etmeli.")
_ensure_bench_database(DATABASE_URL)

os.environ.setdefault("AIRLABS_API_KEY", "fake-benchmark-key-not-real")

import app.worker as worker_module  # noqa: E402
import app.queue.pipeline as pipeline_module  # noqa: E402
from app.db import init_db  # noqa: E402
from app.health import record_successful_refresh  # noqa: E402
from app.queue import baseline as baseline_module  # noqa: E402
from app.queue import engine as engine_module  # noqa: E402
from app.queue.ingestion import refresh as refresh_module  # noqa: E402
from app.queue.models import Flight, FlightEvent, QueuePrediction  # noqa: E402


# ========================================================================
# Deterministic AirLabs-shaped dataset generator
# ========================================================================

# 25 gerçek IATA kodu - flight_airports.sql'de var olduğu ADIM
# (AirLabs Refresh + Timezone Validation) sırasında zaten doğrulanan
# geniş küme + yaygın bilinen büyük havalimanları.
REAL_AIRPORT_CODES = [
    "IST", "SAW", "ESB", "AYT", "ADB", "DXB", "JFK", "CDG", "LHR", "AMS",
    "FRA", "MAD", "FCO", "VKO", "SVO", "DME", "CAN", "PEK", "HND", "ICN",
    "SIN", "DEL", "GRU", "MEX", "ORD",
]
HOME_AIRPORT = "IST"
AIRLINES = ["TK", "PC", "VF", "XY"]
AIRCRAFT_TYPES = ["A320", "A321", "A359", "B738", "B77W", "A20N", "A21N"]


def _fmt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None


def generate_dataset(target_flights: int, airport_count: int, seed: int = 1300) -> list[dict]:
    """
    HOME_AIRPORT (IST) merkezli, `airport_count` farklı yabancı
    havalimanına giden/gelen, deterministic (seeded RNG) bir uçuş
    listesi üretir. AirLabs `schedules` response şeklinde (snake_case
    alanlar) - gerçek `parse_source_a_record()`'ın beklediği ile AYNI.
    """
    rng = random.Random(seed)
    foreign = REAL_AIRPORT_CODES[:airport_count]
    if HOME_AIRPORT in foreign:
        foreign = [a for a in foreign if a != HOME_AIRPORT]
    base_day = datetime(2026, 9, 15, 5, 0)

    records: list[dict] = []
    n = 0
    while n < target_flights:
        for foreign_iata in foreign:
            if n >= target_flights:
                break
            direction = "departure" if n % 2 == 0 else "arrival"
            airline = AIRLINES[n % len(AIRLINES)]
            flight_no = f"{100 + n}"
            aircraft = AIRCRAFT_TYPES[n % len(AIRCRAFT_TYPES)] if n % 9 != 0 else None  # ~11% aircraft ICAO eksik
            sched = base_day + timedelta(minutes=(n * 7) % (18 * 60))
            status = "scheduled"
            if n % 97 == 0:
                status = "cancelled"
            elif n % 131 == 0:
                status = "diverted"

            if direction == "departure":
                dep_iata, arr_iata = HOME_AIRPORT, foreign_iata
                dep_sched, arr_sched = sched, sched + timedelta(hours=2 + (n % 8))
            else:
                dep_iata, arr_iata = foreign_iata, HOME_AIRPORT
                dep_sched, arr_sched = sched - timedelta(hours=2 + (n % 8)), sched

            records.append({
                "airline_iata": airline, "flight_iata": f"{airline}{flight_no}",
                "flight_icao": None, "flight_number": flight_no,
                "dep_iata": dep_iata, "arr_iata": arr_iata,
                "dep_time_utc": _fmt(dep_sched), "arr_time_utc": _fmt(arr_sched),
                "dep_estimated_utc": None, "arr_estimated_utc": None,
                "dep_actual_utc": None, "arr_actual_utc": None,
                "dep_terminal": "1", "arr_terminal": "1",
                "dep_gate": f"B{n % 40}", "arr_gate": f"E{n % 40}",
                "status": status, "aircraft_icao": aircraft,
                "_direction": direction,
            })
            n += 1
    return records


def mutate_10_percent(records: list[dict], seed: int = 999) -> list[dict]:
    """~%10'unda GERÇEK mutable alan değiştirir (estimated/aircraft/status/gate) - flight identity (flight_iata/dep_iata/arr_iata/dep_time_utc) DEĞİŞMEZ."""
    rng = random.Random(seed)
    out = [dict(r) for r in records]
    indices = rng.sample(range(len(out)), max(1, len(out) // 10))
    for i in indices:
        r = out[i]
        choice = i % 4
        if choice == 0:
            base = datetime.strptime(r["dep_time_utc"], "%Y-%m-%d %H:%M") if r["dep_time_utc"] else datetime(2026, 9, 15, 12, 0)
            r["dep_estimated_utc"] = _fmt(base + timedelta(minutes=25))
        elif choice == 1:
            base = datetime.strptime(r["arr_time_utc"], "%Y-%m-%d %H:%M") if r["arr_time_utc"] else datetime(2026, 9, 15, 12, 0)
            r["arr_estimated_utc"] = _fmt(base + timedelta(minutes=25))
        elif choice == 2:
            r["aircraft_icao"] = "A20N" if r["aircraft_icao"] != "A20N" else "A21N"
        else:
            r["dep_gate"] = f"C{i % 20}"
    return out


# ========================================================================
# Fake AirLabs HTTP transport - GERÇEK ağa HİÇ çıkılmaz
# ========================================================================

class _FakeResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class BenchmarkTransport:
    def __init__(self, records_by_airport_direction: dict[tuple[str, str], list[dict]]):
        self._data = records_by_airport_direction
        self.call_count = 0
        self.fetch_seconds = 0.0

    def urlopen(self, request, timeout=None):
        start = time.perf_counter()
        self.call_count += 1
        parsed = urllib.parse.urlparse(request.full_url)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        query = urllib.parse.parse_qs(parsed.query)

        if endpoint == "flights":
            result = _FakeResponse({"request": {"has_more": False}, "response": []})
            self.fetch_seconds += time.perf_counter() - start
            return result

        if "dep_iata" in query:
            airport, direction = query["dep_iata"][0], "departure"
        else:
            airport, direction = query["arr_iata"][0], "arrival"

        records = [
            {k: v for k, v in r.items() if not k.startswith("_")}
            for r in self._data.get((airport, direction), [])
        ]
        result = _FakeResponse({
            "request": {"host": "airlabs.co", "method": "schedules", "has_more": False},
            "response": records,
        })
        self.fetch_seconds += time.perf_counter() - start
        return result


# ========================================================================
# SQL instrumentation (benchmark-only, SQLAlchemy engine event hooks)
# ========================================================================

@dataclass
class SqlCounters:
    select: int = 0
    insert: int = 0
    update: int = 0
    delete: int = 0
    other: int = 0
    commit: int = 0
    rollback: int = 0
    executemany: int = 0

    def as_dict(self) -> dict:
        return {
            "select": self.select, "insert": self.insert, "update": self.update,
            "delete": self.delete, "other": self.other, "commit": self.commit,
            "rollback": self.rollback, "executemany": self.executemany,
            "total_statements": self.select + self.insert + self.update + self.delete + self.other,
        }


def install_sql_instrumentation(engine) -> SqlCounters:
    from sqlalchemy import event

    counters = SqlCounters()

    @event.listens_for(engine, "before_cursor_execute")
    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        head = statement.strip().split(None, 1)[0].upper() if statement.strip() else ""
        if head == "SELECT":
            counters.select += 1
        elif head == "INSERT":
            counters.insert += 1
        elif head == "UPDATE":
            counters.update += 1
        elif head == "DELETE":
            counters.delete += 1
        else:
            counters.other += 1
        if executemany:
            counters.executemany += 1

    @event.listens_for(engine, "commit")
    def _commit(conn):
        counters.commit += 1

    @event.listens_for(engine, "rollback")
    def _rollback(conn):
        counters.rollback += 1

    return counters


# ========================================================================
# Stage timing (benchmark-only monkeypatch wrappers - queue math ASLA değiştirilmez)
# ========================================================================

@dataclass
class StageTimers:
    totals: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)

    def record(self, name: str, elapsed: float) -> None:
        self.totals[name] = self.totals.get(name, 0.0) + elapsed
        self.counts[name] = self.counts.get(name, 0) + 1


@contextmanager
def _timed_wrap(module, attr_name: str, timers: StageTimers, stage_name: str):
    """GERÇEK fonksiyonu SARAR (davranışı/return değeri/argümanları HİÇ değiştirilmez), SADECE süresini ölçer."""
    original = getattr(module, attr_name)

    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            timers.record(stage_name, time.perf_counter() - start)

    setattr(module, attr_name, wrapper)
    try:
        yield
    finally:
        setattr(module, attr_name, original)


@contextmanager
def instrument_stages(timers: StageTimers):
    """
    Bölüm 9 - stage breakdown. Her sarmalayıcı GERÇEK, DEĞİŞTİRİLMEMİŞ
    fonksiyonu çağırır - SADECE süresini ölçer.

    ÖNEMLİ (Python monkeypatch inceliği): `refresh_flights` `pipeline.py`
    içinde `from .ingestion.refresh import refresh_flights` ile İSİM-
    BAĞLAMASIYLA import edilmiş - `refresh_module.refresh_flights`'ı
    patch'lemek `pipeline_module`'ün KENDİ, ÇAĞRI ANINDA KULLANDIĞI
    lokal referansını ETKİLEMEZ; doğru hedef ÇAĞIRANIN modülüdür
    (`pipeline_module`).

    ADIM (MySQL Performance Regression Fix - Phase 1) SONRASI GÜNCELLENDİ:
    `engine.get_baseline`/`get_passenger_baseline` artık import EDİLMİYOR
    (Faz 1'de `_load_historical_flight_count_cache`'e taşındı) - "baseline_
    load" stage'i artık O fonksiyonu sarar: ÖNCEDEN pencere-başına
    çağrılıyordu (~1000 kez/cycle), ARTIK havalimanı-başına TEK KEZ
    çağrılıyor - bu stage'in `call_count`'undaki düşüş TEK BAŞINA Faz
    1'in baseline-cache düzeltmesinin doğrudan kanıtıdır.
    """
    with _timed_wrap(pipeline_module, "refresh_flights", timers, "refresh_flights"), \
         _timed_wrap(engine_module, "persist_predictions", timers, "prediction_persist"), \
         _timed_wrap(engine_module, "record_baseline_observations", timers, "baseline_observation_persist"), \
         _timed_wrap(engine_module, "_load_historical_flight_count_cache", timers, "baseline_load"), \
         _timed_wrap(engine_module, "predict_airport", timers, "prediction_compute"), \
         _timed_wrap(pipeline_module, "load_flight_rows", timers, "source_fetch_and_normalization"):
        yield


# ========================================================================
# Benchmark cycle runner
# ========================================================================

def run_cycle(label: str, records: list[dict], tracked_airports: list[str], now: datetime):
    by_key: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        airport = r["dep_iata"] if r["_direction"] == "departure" else r["arr_iata"]
        by_key.setdefault((airport, r["_direction"]), []).append(r)

    transport = BenchmarkTransport(by_key)
    old_urlopen = urllib.request.urlopen
    urllib.request.urlopen = transport.urlopen

    from app.queue.ingestion import airlabs_client
    source_a = airlabs_client.build_source_a(tracked_airports)
    source_b = airlabs_client.build_source_b()

    try:
        import psutil
        _proc = psutil.Process()
        rss_before_mb = round(_proc.memory_info().rss / (1024 * 1024), 1)
    except ImportError:
        _proc = None
        rss_before_mb = None

    timers = StageTimers()
    stage_start = time.perf_counter()
    try:
        with instrument_stages(timers):
            summary = pipeline_module.run(
                now=now, apply_usage_horizon=True, source_a=source_a, source_b=source_b,
            )
    finally:
        urllib.request.urlopen = old_urlopen

    rss_after_mb = round(_proc.memory_info().rss / (1024 * 1024), 1) if _proc else None

    heartbeat_start = time.perf_counter()
    from app.db import get_session
    hb_session = get_session()
    try:
        record_successful_refresh(hb_session)
    finally:
        hb_session.close()
    heartbeat_seconds = time.perf_counter() - heartbeat_start

    total_seconds = time.perf_counter() - stage_start

    return {
        "label": label,
        "summary": summary,
        "total_seconds": round(total_seconds, 3),
        "airlabs_fetch_seconds": round(transport.fetch_seconds, 3),
        "http_calls": transport.call_count,
        "heartbeat_seconds": round(heartbeat_seconds, 3),
        "rss_before_mb": rss_before_mb,
        "rss_after_mb": rss_after_mb,
        "stage_totals": {k: round(v, 3) for k, v in timers.totals.items()},
        "stage_call_counts": timers.counts,
    }


def correctness_snapshot(session_factory):
    session = session_factory()
    try:
        total_flights = session.query(Flight).count()
        distinct_keys = session.query(Flight.flight_key).distinct().count()
        pred_rows = session.query(
            QueuePrediction.airport_iata, QueuePrediction.process, QueuePrediction.window_start
        ).all()
        event_count = session.query(FlightEvent).count()
    finally:
        session.close()
    return {
        "flights_total": total_flights,
        "flights_distinct_key": distinct_keys,
        "flights_duplicate": total_flights - distinct_keys,
        "predictions_total": len(pred_rows),
        "predictions_distinct": len(set(pred_rows)),
        "predictions_duplicate": len(pred_rows) - len(set(pred_rows)),
        "flight_events_total": event_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flights", type=int, default=1300)
    parser.add_argument("--airports", type=int, default=25)
    args = parser.parse_args()

    print(f"# Benchmark: {args.flights} flights, {args.airports} airports, DB={DATABASE_URL.split('@')[-1]}")

    init_db()
    from app.db import get_session, engine as db_engine, SessionLocal
    from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset
    from app.queue.ingestion.airports_import import import_airports

    seed_session = get_session()
    try:
        if seed_session.query(Flight).first() is None:
            pass
        from app.models import AircraftCapacity
        from app.queue.models import Airport
        if seed_session.query(AircraftCapacity).first() is None:
            seed_verified_dataset(seed_session)
            seed_curated_fallback(seed_session)
            seed_family_and_ga(seed_session)
        if seed_session.query(Airport).first() is None:
            import_airports(seed_session, os.path.join("data", "flight_airports.sql"))
    finally:
        seed_session.close()

    sql_counters = install_sql_instrumentation(db_engine)

    now = datetime(2026, 9, 15, 20, 0)
    fresh_records = generate_dataset(args.flights, args.airports, seed=1300)
    tracked = sorted({HOME_AIRPORT})

    results = []

    def _run_and_report(label, records):
        sql_counters.select = sql_counters.insert = sql_counters.update = 0
        sql_counters.delete = sql_counters.other = sql_counters.commit = 0
        sql_counters.rollback = sql_counters.executemany = 0
        result = run_cycle(label, records, tracked, now)
        result["sql"] = sql_counters.as_dict()
        result["correctness"] = correctness_snapshot(SessionLocal)
        results.append(result)
        print(json.dumps(result, indent=2, default=str))
        return result

    _run_and_report("A_FRESH", fresh_records)
    _run_and_report("B_IDENTICAL", fresh_records)
    changed_records = mutate_10_percent(fresh_records)
    _run_and_report("C_10PCT_CHANGED", changed_records)

    print("\n# ==== SUMMARY ====")
    for r in results:
        print(f"{r['label']}: total={r['total_seconds']}s sql={r['sql']}")


if __name__ == "__main__":
    main()
