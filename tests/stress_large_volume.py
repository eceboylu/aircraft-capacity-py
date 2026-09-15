"""
ADIM 5C - Büyük veri / binlerce uçuş yükü ölçümü.

Bu bir pytest testi DEĞİLDİR (bilinçli olarak `test_` önekiyle
İSİMLENDİRİLMEDİ) - pytest'in normal `python -m pytest` koşusuna DAHİL
OLMAZ, çünkü 5.000-10.000 kayıtlık senaryolar her geliştirme
döngüsünde çalıştırılacak hızlı bir regresyon testi değil, tek seferlik
bir PERFORMANS ÖLÇÜM script'idir. Mevcut hızlı test suite'ini (431
passed) kalıcı olarak yavaşlatmamak için ayrı tutuldu.

Çalıştırma:

    venv/Scripts/python -m tests.stress_large_volume

Mimari kural: gerçek `airlabs_client` -> `parse_source_a` ->
`refresh_flights` -> `AircraftCapacityService`/`run_predictions`
zincirinin TAMAMI kullanılır. SADECE `urllib.request.urlopen` (HTTP
katmanı) sahte - synthetic kayıtlar, sayfalara bölünüp GERÇEK AirLabs
`request.has_more`/`offset` zarfıyla sunuluyor; `_request()`'in
sayfalama döngüsü hiç değişmeden çalışıyor.

Hiçbir gerçek AirLabs API çağrısı yapılmaz, AIRLABS_API_KEY sahte/dummy
bir string'tir, gerçek `database.sqlite`'a dokunulmaz (bellek içi
`sqlite://`).
"""

from __future__ import annotations

import json
import os
import sys
import time
import tracemalloc
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.queue.pipeline as pipeline_module
from app.models import Base
from app.queue.ingestion import airlabs_client
from app.queue.ingestion.airports_import import country_lookup, import_airports
from app.queue.ingestion.refresh import refresh_flights
from app.queue.ingestion.sources import aircraft_match_rate, parse_source_a
from app.queue.engine import run_predictions
from app.queue.models import Flight
from app.service import AircraftCapacityService
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset

REAL_AIRPORTS_SQL = os.path.join(
    os.path.dirname(__file__), "..", "data", "flight_airports.sql"
)

# Gerçek uçak tipleri (yolcu_ucaklari.json'da doğrulanmış) - rotasyonla
# kullanılıyor, uydurma bir tip YOK. Bazı kayıtlarda bilinçli olarak
# aircraft_icao BOŞ bırakılıyor (gerçekte de schedules'ta çoğu kayıtta
# boş - bkz. ADIM 1 analizi) - DEFAULT_CAPACITY fallback yolunu da
# yükün altında egzersiz eder.
_AIRCRAFT_TYPES = ["A320", "A321", "B738", "B77W", "A20N", None, None]
_ARRIVAL_AIRPORTS = ["CDG", "DXB", "JFK", "LHR", "FRA", "ESB", "ADB", "SAW"]
_STATUSES = ["scheduled", "active", "landed", "cancelled"]
_AIRLINES = ["TK", "PC", "AF", "BA", "LH", "EK"]

BASE_DEP = datetime(2026, 9, 15, 6, 0)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def generate_records(n: int) -> list[dict]:
    """
    `n` adet synthetic AirLabs `schedules` kaydı üretir - IST'ten
    kalkış, ~10 saatlik gerçekçi bir tarife penceresine (AirLabs
    schedules'ın kendi lookahead sınırı) yayılmış, gerçek alan adları
    (ADIM 2'deki mock dataset ile AYNI şema) kullanılarak.
    """
    records = []
    window_minutes = 600  # ~10 saat, schedules'ın gerçek lookahead sınırı
    for i in range(n):
        offset_minutes = int(i * window_minutes / max(n, 1))
        dep = BASE_DEP + timedelta(minutes=offset_minutes)
        duration = 60 + (i % 12) * 45  # 60..585 dk arası çeşitlilik
        arr = dep + timedelta(minutes=duration)
        airline = _AIRLINES[i % len(_AIRLINES)]
        aircraft = _AIRCRAFT_TYPES[i % len(_AIRCRAFT_TYPES)]
        status = _STATUSES[i % len(_STATUSES)]
        record = {
            "airline_iata": airline,
            "flight_iata": f"{airline}{1000 + i}",
            "flight_icao": f"{airline}X{1000 + i}",
            "flight_number": str(1000 + i),
            "dep_iata": "IST",
            "dep_icao": "LTFM",
            "dep_terminal": "1",
            "dep_gate": f"B{i % 40}",
            "dep_time": _fmt(dep),
            "dep_time_utc": _fmt(dep),
            "dep_estimated_utc": _fmt(dep + timedelta(minutes=(i % 7))),
            "dep_actual_utc": None,
            "dep_delayed": i % 7,
            "arr_iata": _ARRIVAL_AIRPORTS[i % len(_ARRIVAL_AIRPORTS)],
            "arr_icao": "XXXX",
            "arr_time": _fmt(arr),
            "arr_time_utc": _fmt(arr),
            "arr_estimated_utc": None,
            "arr_actual_utc": None,
            "duration": duration,
            "status": status,
        }
        if aircraft:
            record["aircraft_icao"] = aircraft
        records.append(record)
    return records


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def make_paginating_urlopen(all_records: list[dict], page_size: int, fail_on_page: int | None = None):
    """
    `all_records`'ı `page_size`'lık sayfalara böler, GERÇEK AirLabs
    zarfıyla (`request.has_more`, `response`) sunan bir sahte
    `urlopen` üretir - `airlabs_client._request()`'in offset ilerletme
    mantığı hiç değişmeden bu sayfaları tüketir.

    `fail_on_page` verilirse (1-index), o sayfa numarasında HTTPError
    fırlatır - hata izolasyonu davranışını (madde 10) ölçmek için.
    """
    pages = [
        all_records[i:i + page_size]
        for i in range(0, len(all_records), page_size)
    ] or [[]]

    def fake_urlopen(request, timeout=None):
        parsed = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        endpoint = parsed.path.rsplit("/", 1)[-1]

        if endpoint == "flights":
            return _FakeHTTPResponse({"request": {"has_more": False}, "response": []})

        offset = int(query.get("offset", ["0"])[0])
        page_index = offset // page_size if page_size else 0

        if fail_on_page is not None and (page_index + 1) == fail_on_page:
            raise urllib.error.URLError("kasıtlı test hatası (stress script)")

        page = pages[page_index] if page_index < len(pages) else []
        has_more = page_index + 1 < len(pages)
        return _FakeHTTPResponse({
            "request": {"has_more": has_more, "total_items": len(all_records)},
            "response": page,
        })

    return fake_urlopen, len(pages)


class _Patch:
    """Basit monkeypatch yerine - script pytest DIŞINDA çalıştığı için
    kendi restore mekanizması."""

    def __init__(self):
        self._orig = {}

    def set(self, obj, name, value):
        self._orig[(obj, name)] = getattr(obj, name)
        setattr(obj, name, value)

    def restore(self):
        for (obj, name), value in self._orig.items():
            setattr(obj, name, value)


def build_isolated_env(patch: _Patch):
    """Bellek içi izole DB + referans veri (havalimanı + uçak kapasite)."""
    test_engine = create_engine("sqlite://")
    TestSessionLocal = sessionmaker(bind=test_engine)
    Base.metadata.create_all(test_engine)

    patch.set(pipeline_module, "init_db", lambda drop_first=False: None)
    patch.set(pipeline_module, "get_session", lambda: TestSessionLocal())

    seed_session = TestSessionLocal()
    import_airports(seed_session, REAL_AIRPORTS_SQL)
    seed_verified_dataset(seed_session)
    seed_curated_fallback(seed_session)
    seed_family_and_ga(seed_session)
    seed_session.close()

    return TestSessionLocal


def run_phase_timed(label, fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    elapsed = time.perf_counter() - start
    print(f"    {label}: {elapsed:.3f}s")
    return result, elapsed


def measure_scale(n: int, page_size: int) -> dict:
    print(f"\n=== N={n} kayıt (page_size={page_size}) ===")
    patch = _Patch()
    try:
        SessionLocal = build_isolated_env(patch)

        os.environ["AIRLABS_API_KEY"] = "test-dummy-key-not-real-never-sent"
        synthetic = generate_records(n)
        fake_urlopen, page_count = make_paginating_urlopen(synthetic, page_size)
        patch.set(urllib.request, "urlopen", fake_urlopen)

        source_a = airlabs_client.build_source_a(["IST"])
        source_b = airlabs_client.build_source_b()

        tracemalloc.start()

        # --- T0: FETCH + PAGINATION (gerçek airlabs_client._request) ---
        raw_records, t_fetch = run_phase_timed(
            "fetch+pagination (Kaynak A, IST departures)", source_a, "departure"
        )
        _, mem_after_fetch_peak = tracemalloc.get_traced_memory()

        session = SessionLocal()
        countries = country_lookup(session)

        # --- PARSE (gerçek parse_source_a) ---
        rows, t_parse = run_phase_timed(
            "parse_source_a", parse_source_a, raw_records, "departure", countries, {}
        )

        # --- REFRESH (gerçek refresh_flights, upsert + event tespiti) ---
        refreshed_t0, t_refresh_t0 = run_phase_timed(
            "refresh_flights (T0, tüm insert)", refresh_flights, session, rows
        )

        # --- PREDICTION (gerçek run_predictions, gerçek AircraftCapacityService) ---
        predicted_t0, t_predict_t0 = run_phase_timed(
            "run_predictions (T0)", run_predictions, session,
            AircraftCapacityService(session), airports=["IST"],
            now=datetime(2026, 9, 16, 12, 0),
        )

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        total_flights = session.scalar(select(func.count()).select_from(Flight))
        match_rate = aircraft_match_rate(rows)

        # --- T1: AYNI kayıtlar tekrar (tamamı UPDATE olmalı) ---
        raw_records_t1, t_fetch_t1 = run_phase_timed(
            "fetch+pagination (T1, tekrar)", source_a, "departure"
        )
        rows_t1, t_parse_t1 = run_phase_timed(
            "parse_source_a (T1)", parse_source_a, raw_records_t1, "departure", countries, {}
        )
        refreshed_t1, t_refresh_t1 = run_phase_timed(
            "refresh_flights (T1, tüm update)", refresh_flights, session, rows_t1
        )
        predicted_t1, t_predict_t1 = run_phase_timed(
            "run_predictions (T1)", run_predictions, session,
            AircraftCapacityService(session), airports=["IST"],
            now=datetime(2026, 9, 16, 12, 0),
        )

        total_flights_after_t1 = session.scalar(select(func.count()).select_from(Flight))
        session.close()

        total_t0 = t_fetch + t_parse + t_refresh_t0 + t_predict_t0
        total_t1 = t_fetch_t1 + t_parse_t1 + t_refresh_t1 + t_predict_t1

        result = {
            "n": n,
            "page_count": page_count,
            "t0_flights_parsed": len(rows),
            "t0_inserted": refreshed_t0["inserted"],
            "t0_updated": refreshed_t0["updated"],
            "t0_failed": refreshed_t0["failed"],
            "t0_predictions": predicted_t0["predictions"],
            "t0_failed_airports": predicted_t0["failed_airports"],
            "t1_inserted": refreshed_t1["inserted"],
            "t1_updated": refreshed_t1["updated"],
            "t1_failed": refreshed_t1["failed"],
            "duplicate_check_ok": total_flights == n and total_flights_after_t1 == n,
            "aircraft_match_rate": round(match_rate, 3),
            "mem_peak_after_fetch_bytes": mem_after_fetch_peak,
            "mem_peak_total_bytes": peak,
            "t_fetch_s": t_fetch,
            "t_parse_s": t_parse,
            "t_refresh_t0_s": t_refresh_t0,
            "t_predict_t0_s": t_predict_t0,
            "t_refresh_t1_s": t_refresh_t1,
            "t_predict_t1_s": t_predict_t1,
            "total_t0_s": total_t0,
            "total_t1_s": total_t1,
        }

        print(f"    -> T0: inserted={refreshed_t0['inserted']} updated={refreshed_t0['updated']} "
              f"failed={refreshed_t0['failed']} predictions={predicted_t0['predictions']} "
              f"failed_airports={predicted_t0['failed_airports']}")
        print(f"    -> T1: inserted={refreshed_t1['inserted']} updated={refreshed_t1['updated']} "
              f"failed={refreshed_t1['failed']} predictions={predicted_t1['predictions']}")
        print(f"    -> DB toplam satır (duplicate kontrolü): T0={total_flights} T1={total_flights_after_t1} "
              f"(beklenen ikisi de {n}) -> {'OK' if result['duplicate_check_ok'] else 'FAIL - DUPLICATE!'}")
        print(f"    -> RAM: fetch sonrası peak={mem_after_fetch_peak/1024:.1f} KB, "
              f"tüm T0 akışı peak={peak/1024:.1f} KB")

        return result
    finally:
        patch.restore()


def measure_page_failure(n: int, page_size: int, fail_on_page: int):
    """Madde 10 - bir sayfa başarısız olursa davranış güvenli mi?"""
    print(f"\n=== Sayfa hatası senaryosu: N={n}, page_size={page_size}, sayfa {fail_on_page} başarısız ===")
    patch = _Patch()
    try:
        SessionLocal = build_isolated_env(patch)
        os.environ["AIRLABS_API_KEY"] = "test-dummy-key-not-real-never-sent"
        synthetic = generate_records(n)
        fake_urlopen, page_count = make_paginating_urlopen(synthetic, page_size, fail_on_page=fail_on_page)
        patch.set(urllib.request, "urlopen", fake_urlopen)

        # Gerçek retry/backoff mantığını (bkz. airlabs_client._request_page)
        # DEĞİŞTİRMEDEN, sadece script'in gerçek zamanda beklememesi için
        # time.sleep'i no-op'a alıyoruz - davranış (kaç deneme, ne zaman
        # pes edildiği) AYNI, sadece gerçek bekleme süresi geçilmiyor.
        patch.set(time, "sleep", lambda seconds: None)

        source_a = airlabs_client.build_source_a(["IST"])
        source_b = airlabs_client.build_source_b()

        summary = pipeline_module.run(source_a=source_a, source_b=source_b)
        print(f"    -> pipeline.run() ÇÖKMEDİ. summary={summary}")
        print(f"    -> Kaynak A o yön için 0 kayıt işledi mi (beklenen davranış): "
              f"flights_parsed={summary['flights_parsed']}")
        return summary
    finally:
        patch.restore()


def measure_pagination_data_loss_regression(n: int, page_size: int):
    """
    ADIM 5D DOĞRULAMASI: bu senaryo ADIM 5C'de MAX_PAGES=50 yüzünden
    500 kayıt kaybediyordu (3.000 kayıt / page_size=50 -> 60 sayfa).
    Artık `ABSOLUTE_SAFETY_PAGE_LIMIT=5000` ve her sayfa GERÇEKTEN
    farklı içerik taşıdığı için (fingerprint tekrar tespiti
    tetiklenmez) HİÇ kayıp OLMAMALI.
    """
    print(f"\n=== Pagination veri kaybı regresyon kontrolü: N={n}, page_size={page_size} "
          f"(gereken sayfa={-(-n // page_size)}, ABSOLUTE_SAFETY_PAGE_LIMIT="
          f"{airlabs_client.ABSOLUTE_SAFETY_PAGE_LIMIT}) ===")
    patch = _Patch()
    try:
        os.environ["AIRLABS_API_KEY"] = "test-dummy-key-not-real-never-sent"
        synthetic = generate_records(n)
        fake_urlopen, page_count = make_paginating_urlopen(synthetic, page_size)
        patch.set(urllib.request, "urlopen", fake_urlopen)

        received = airlabs_client.fetch_schedules("departure", "IST")
        expected_pages = -(-n // page_size)
        truncated = len(received) < n
        print(f"    -> Gönderilen: {n} kayıt, {expected_pages} sayfa halinde. "
              f"Client'ın aldığı: {len(received)} kayıt.")
        if truncated:
            print(f"    -> HÂLÂ KAYIP VAR: {n - len(received)} kayıt eksik - REGRESYON!")
        else:
            print(f"    -> TÜM {n} kayıt eksiksiz alındı - {expected_pages} sayfa, veri kaybı YOK.")
        return {"n": n, "page_size": page_size, "expected_pages": expected_pages,
                "received": len(received), "truncated": truncated}
    finally:
        patch.restore()


def main():
    print("ADIM 5C/5D - Büyük veri / binlerce uçuş yükü + pagination veri kaybı ölçümü")
    print("=" * 70)

    results = []
    for n, page_size in [(1000, 250), (5000, 250), (10000, 250)]:
        results.append(measure_scale(n, page_size))

    measure_page_failure(n=1000, page_size=250, fail_on_page=2)
    measure_pagination_data_loss_regression(n=3000, page_size=50)

    print("\n" + "=" * 70)
    print("ÖZET TABLO")
    print("=" * 70)
    header = f"{'N':>7} {'sayfa':>6} {'T0 toplam':>10} {'T1 toplam':>10} {'RAM peak(KB)':>13} {'dup?':>6}"
    print(header)
    for r in results:
        print(
            f"{r['n']:>7} {r['page_count']:>6} {r['total_t0_s']:>9.2f}s "
            f"{r['total_t1_s']:>9.2f}s {r['mem_peak_total_bytes']/1024:>12.1f} "
            f"{'OK' if r['duplicate_check_ok'] else 'FAIL':>6}"
        )


if __name__ == "__main__":
    main()
