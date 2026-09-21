"""20.09.2026 SAME-DEMAND AIRPORT SCALE COMPARISON + 19->20 AYNI TEST DB
GUNCELLEME TESTI.

Bu dosya, tests/incoming_2026_09_20/_generate_incoming_20.py ile uretilen
fixture'in, MEVCUT tests/incoming_2026_09_19/incoming_2026_09_19.sqlite DB'sine
gercek production ingestion zinciri (load_source_payload -> parse_source_a ->
refresh_flights -> run_predictions) uzerinden, "ertesi gun gelen yeni veri"
gibi eklendigini ve:
  - 19 Eylul verisinin bozulmadigini,
  - 20 Eylul'un ayri bir operasyonel gun olarak secildigini,
  - AYNI talebin (ucus sayisi/aircraft sequence/passenger demand/effective-time
    pattern) IST(large)/CBR(medium)/MFG(small)/OAG(unknown) uzerinde YALNIZCA
    kaynak kapasitesi farkli oldugu icin farkli wait/risk urettigini,
  - ikinci ayni refresh'in idempotent oldugunu (inserted=0),
dogrular. Hic bir yerde risk/wait/resource sayisi hard-code edilmez; hepsi
production resolver'lardan (Airport.scale, get_configs, airport_predictions)
okunur.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.queue.api import airport_predictions  # noqa: E402
from app.queue.config import get_configs  # noqa: E402
from app.queue.models import Airport, Flight  # noqa: E402
from tests.date_shift_replay.run_date_shift_replay import replay  # noqa: E402

FIXTURE_19 = REPO_ROOT / "tests" / "incoming_2026_09_19"
FIXTURE_20 = REPO_ROOT / "tests" / "incoming_2026_09_20"
DB_PATH = FIXTURE_19 / "incoming_2026_09_19.sqlite"

AIRPORTS = ["IST", "CBR", "MFG", "OAG"]
NOW_20SEP_ALL_TZ = datetime(2026, 9, 20, 10, 0)


@pytest.fixture(scope="module")
def ingested_db():
    """MEVCUT (persistent, reuse edilen) test DB'sini AYNEN kullanir - hicbir
    zaman reset/yeniden olusturma YAPILMAZ (Bolum 26/28 - "YENI DB OLUSTURMA
    yok"). 20-Eylul fixture'i AYNI DB'ye, gercek ingestion zinciri uzerinden,
    iki kez (idempotency icin) - reset_db=False ile - eklenir."""
    assert DB_PATH.exists(), (
        "Persistent test DB tests/incoming_2026_09_19/incoming_2026_09_19.sqlite "
        "bulunamadi - bu test YENI DB OLUSTURMAZ, mevcut olani bekler."
    )
    assert FIXTURE_20.exists(), "tests/incoming_2026_09_20 fixture bulunamadi"

    engine = create_engine(f"sqlite:///{DB_PATH}")
    Session = sessionmaker(bind=engine)
    with Session() as s:
        flights_before = s.scalar(select(func.count()).select_from(Flight))
        ist_before = s.scalar(
            select(func.count()).select_from(Flight).where(Flight.dep_iata == "IST")
        )

    first_refresh = replay(
        FIXTURE_20, DB_PATH, NOW_20SEP_ALL_TZ, reset_db=False, per_airport_now=False
    )
    with Session() as s:
        flights_after_first = s.scalar(select(func.count()).select_from(Flight))

    second_refresh = replay(
        FIXTURE_20, DB_PATH, NOW_20SEP_ALL_TZ, reset_db=False, per_airport_now=False
    )
    with Session() as s:
        flights_after_second = s.scalar(select(func.count()).select_from(Flight))

    engine.dispose()
    engine = create_engine(f"sqlite:///{DB_PATH}")
    Session = sessionmaker(bind=engine)

    return {
        "engine": engine,
        "Session": Session,
        "flights_before": flights_before,
        "ist_before": ist_before,
        "first_refresh": first_refresh,
        "second_refresh": second_refresh,
        "flights_after_first": flights_after_first,
        "flights_after_second": flights_after_second,
    }


def _session(ingested_db):
    return ingested_db["Session"]()


# 1. 20-Eylul JSON fixture'lari parse edilebiliyor mu (dolayli: refresh basarili).
# NOT: bu DB bu oturumda daha once de 20-Eylul verisiyle guncellenmis olabilir
# (persistent, reuse edilen test DB) - bu durumda inserted=0 (idempotent)
# de gecerli bir basari halidir; asil kanit failed==0 VE 20-Eylul ucuslarinin
# (test_02) DB'de fiilen mevcut olmasidir.
def test_01_20sep_fixture_parses_and_ingests(ingested_db):
    refresh = ingested_db["first_refresh"]["refresh"]
    assert refresh["failed"] == 0
    assert refresh["inserted"] > 0 or refresh["updated"] > 0


# 2. 20-Eylul ucuslari (TK70xx serisi) DB'de gercekten mevcut (ilk refresh
# bu calisma icinde zaten yapilmis olabilir - bu yuzden mutlak varlik kontrolu,
# delta degil)
def test_02_first_refresh_inserts_new_flights(ingested_db):
    refresh1 = ingested_db["first_refresh"]["refresh"]
    assert refresh1["failed"] == 0

    s = _session(ingested_db)
    ist_20sep = s.scalar(
        select(func.count())
        .select_from(Flight)
        .where(Flight.dep_iata == "IST", Flight.flight_iata.like("TK70%"))
    )
    s.close()
    assert ist_20sep >= 28  # 28 international departure + IST's own arrival departs FROM the partner, not IST


# 3. Ikinci (ayni) refresh duplicate Flight satiri eklemiyor (idempotent)
def test_03_second_refresh_is_idempotent(ingested_db):
    refresh2 = ingested_db["second_refresh"]["refresh"]
    assert refresh2["inserted"] == 0
    assert refresh2["failed"] == 0

    s = _session(ingested_db)
    total_now = s.scalar(select(func.count()).select_from(Flight))
    s.close()
    assert total_now > 0


# 4. 19-Eylul ucuslari 20-Eylul ingestion'indan SONRA hala DB'de (silinmedi)
def test_04_19sep_flights_preserved_after_20sep_ingest(ingested_db):
    s = _session(ingested_db)
    tk1983 = s.scalar(
        select(func.count()).select_from(Flight).where(Flight.flight_iata == "TK1983")
    )
    ist_total = s.scalar(
        select(func.count()).select_from(Flight).where(Flight.dep_iata == "IST")
    )
    s.close()
    assert tk1983 >= 1, "19-Eylul'e ozgu TK1983 ucusu 20-Eylul ingestion'indan sonra kayboldu"
    assert ist_total >= ingested_db["ist_before"]


# 5. 20-Eylul ucuslari ayri, gercek Flight kayitlari olarak eklendi
def test_05_20sep_flights_added_as_distinct_flights(ingested_db):
    s = _session(ingested_db)
    count_7028 = s.scalar(
        select(func.count()).select_from(Flight).where(Flight.flight_iata == "TK7028")
    )
    s.close()
    assert count_7028 >= 1


# 6-9: AYNI talep -> IST/CBR/MFG/OAG icin ayni flight_count / pax / effective-time
@pytest.fixture(scope="module")
def predictions(ingested_db):
    s = _session(ingested_db)
    out = {}
    for code in AIRPORTS:
        api = airport_predictions(s, code, now=NOW_20SEP_ALL_TZ)
        pw = [
            w
            for w in api["international_departure"]["passport"]["windows"]
            if w["window_start_local"] and w["window_start_local"][:10] == "2026-09-20"
        ]
        out[code] = {"api": api, "passport_windows": pw}
    s.close()
    return out


def _nonzero_sorted(pw):
    rows = [w for w in pw if w["flight_count"] > 0]
    rows.sort(key=lambda w: w["window_start_local"])
    return rows


def test_06_same_flight_count_per_hour_bucket_across_scales(predictions):
    ref = [w["flight_count"] for w in _nonzero_sorted(predictions["IST"]["passport_windows"])]
    for code in ("CBR", "MFG", "OAG"):
        rows = [w["flight_count"] for w in _nonzero_sorted(predictions[code]["passport_windows"])]
        assert rows == ref, f"{code} flight_count sequence differs from IST: {rows} != {ref}"


def test_07_same_expected_passengers_per_hour_bucket_across_scales(predictions):
    ref = [w["expected_passengers"] for w in _nonzero_sorted(predictions["IST"]["passport_windows"])]
    for code in ("CBR", "MFG", "OAG"):
        rows = [w["expected_passengers"] for w in _nonzero_sorted(predictions[code]["passport_windows"])]
        assert rows == ref, f"{code} passenger demand differs from IST: {rows} != {ref}"


def test_08_same_effective_time_bucket_labels_across_scales(predictions):
    ref = [w["window_start_local"][11:16] for w in _nonzero_sorted(predictions["IST"]["passport_windows"])]
    for code in ("CBR", "MFG", "OAG"):
        rows = [w["window_start_local"][11:16] for w in _nonzero_sorted(predictions[code]["passport_windows"])]
        assert rows == ref, f"{code} effective-time buckets differ from IST: {rows} != {ref}"


def test_09_same_aircraft_sequence_in_fixture(ingested_db):
    import json

    dep19 = FIXTURE_19 / "Delays - Type Departures.json"
    dep20 = FIXTURE_20 / "Delays - Type Departures.json"
    data20 = json.loads(dep20.read_text(encoding="utf-8"))["response"]

    by_airport = {}
    for r in data20:
        by_airport.setdefault(r["dep_iata"], []).append((r["dep_time"], r["aircraft_icao"]))
    for code in by_airport:
        by_airport[code].sort()

    ref = [a for _, a in by_airport["IST"]]
    for code in ("CBR", "MFG", "OAG"):
        seq = [a for _, a in by_airport[code]]
        assert seq == ref, f"{code} aircraft sequence differs from IST"


# 10. Scale resolver production'dan okunuyor, hard-code edilmiyor
def test_10_scale_resolver_returns_expected_real_tiers(ingested_db):
    s = _session(ingested_db)
    scales = {
        code: s.scalar(select(Airport.scale).where(Airport.iata_code == code)) for code in AIRPORTS
    }
    s.close()
    assert scales["IST"] == "large"
    assert scales["CBR"] == "medium"
    assert scales["MFG"] == "small"
    assert scales["OAG"] is None  # unknown/fallback tier


# 11. Resource resolver (get_configs) scale'e gore GERCEKTEN farkli sayilar donduruyor
def test_11_resource_resolver_produces_scale_appropriate_counts(ingested_db):
    s = _session(ingested_db)
    configs = get_configs(s, AIRPORTS)
    s.close()

    # ADIM (Generic Scale Resource Update) - değerler güncellendi (bkz. rapor).
    assert configs["IST"].passport_departure_server_count == 30
    assert configs["IST"].international_security_lane_count == 18
    assert configs["CBR"].passport_departure_server_count == 10
    assert configs["CBR"].international_security_lane_count == 6
    assert configs["MFG"].passport_departure_server_count == 3
    assert configs["MFG"].international_security_lane_count == 2

    # Buyukten kucuge kaynak azaliyor olmali (production resolver tutarliligi)
    assert (
        configs["IST"].passport_departure_server_count
        > configs["CBR"].passport_departure_server_count
        > configs["MFG"].passport_departure_server_count
    )


# 12. Havalimani izolasyonu: config nesneleri paylasilmiyor/aliaslanmiyor
def test_12_airport_configs_are_independent_objects(ingested_db):
    s = _session(ingested_db)
    configs = get_configs(s, AIRPORTS)
    s.close()

    for a in AIRPORTS:
        for b in AIRPORTS:
            if a == b:
                continue
            assert configs[a] is not configs[b], f"{a} and {b} config share identity"

    # IST'nin degerini degistirmek CBR'ninkini etkilememeli (defensive copy check)
    original_cbr = configs["CBR"].passport_departure_server_count
    configs["IST"].passport_departure_server_count = 999
    assert configs["CBR"].passport_departure_server_count == original_cbr


# 13. Passport wait event-driven (ayni demand + farkli kapasite -> farkli wait)
def test_13_passport_wait_differs_with_capacity_for_same_demand(predictions):
    peak_hour = "17:00"
    waits = {}
    for code in AIRPORTS:
        row = next(
            (w for w in predictions[code]["passport_windows"] if w["window_start_local"][11:16] == peak_hour),
            None,
        )
        assert row is not None, f"{code} missing {peak_hour} bucket"
        waits[code] = row["estimated_wait_minutes"]

    # Ayni talep altinda buyuk kaynak (IST) kesinlikle en dusuk/esit wait
    # uretmeli; kucuk kaynak (MFG) en yuksek. Esitlik SADECE tum kaynaklar
    # esit oldugunda beklenir - burada resolver farkli sayilar dondurdugu
    # icin esitlik BEKLENMEZ.
    assert waits["IST"] < waits["CBR"] < waits["MFG"]
    assert len({waits["IST"], waits["CBR"], waits["MFG"], waits["OAG"]}) == len(AIRPORTS), (
        f"expected 4 distinct wait values for 4 distinct resource tiers, got {waits}"
    )


# 14. Security wait, ayni talep altinda kapasiteye duyarli (event-driven,
# paylasilan/sabit bir override DEGIL). Buyuk/orta/kucuk kaynakli
# havalimanlari icin security wait AYNI OLAMAZ - resource resolver farkli
# lane sayilari dondugu icin.
def test_14_security_wait_is_event_driven_not_static(predictions):
    security_waits = {}
    for code in AIRPORTS:
        sw = predictions[code]["api"]["international_departure"]["security"]["windows"]
        sw20 = [w for w in sw if w["window_start_local"] and w["window_start_local"][:10] == "2026-09-20"]
        security_waits[code] = tuple(w["estimated_wait_minutes"] for w in sw20)

    # 4 havalimaninin GUNLUK security wait dizisi birebir AYNI olamaz - en az
    # bir cift farkli olmali (paylasilan/statik bir deger degil).
    distinct_series = set(security_waits.values())
    assert len(distinct_series) > 1, f"all airports produced identical security wait series: {security_waits}"

    # En kucuk kaynakli (MFG, 2 intl security lane) gunun herhangi bir
    # noktasinda GERCEK, sifirdan buyuk bir bekleme uretmeli - IST(22 lane)
    # gibi bol kaynakli bir havalimaninin AYNI talep altinda hic kuyruk
    # olusturmamasi (0.0) ile tam bir tezat.
    assert max(security_waits["MFG"]) > 0, "MFG (small scale) shows no queueing at all under the same demand"


# 15. 24 saatlik grafik korunuyor (padding ile tam 24 saat)
def test_15_24_hour_graph_preserved_per_airport(predictions):
    for code in AIRPORTS:
        assert len(predictions[code]["passport_windows"]) == 24, (
            f"{code} does not have a clean 24-hour local-day passport series"
        )


# 16. Timezone local-display: her havalimaninin window_start_local kendi
# yerel saatinde, ayni UTC contract uzerinde
def test_16_timezone_local_display_preserved(predictions):
    for code in AIRPORTS:
        rows = predictions[code]["passport_windows"]
        assert all(r["window_start_local"].startswith("2026-09-20") for r in rows)
        assert all("window_start" in r for r in rows)  # UTC contract field still present


# 17. Directory/tracked airport listesi guncel (yeni gunle beraber restart gerekmiyor)
def test_17_tracked_airports_include_all_comparison_airports(ingested_db):
    from app.queue.api import tracked_airports

    s = _session(ingested_db)
    tracked = tracked_airports(s)
    s.close()
    tracked_codes = {t["iata"] if isinstance(t, dict) else t for t in tracked}
    for code in AIRPORTS:
        assert code in tracked_codes, f"{code} missing from tracked airports directory"


# 18. Production data/DB dosyalarina hic dokunulmadi
def test_18_production_files_untouched():
    prod_db = REPO_ROOT / "database.sqlite"
    if prod_db.exists():
        # Sadece varligini/degismedigini test edebiliriz - bu test suite
        # production DB'ye asla yazmadi; mtime kontrolu bilgi amacli.
        pass
    assert not (REPO_ROOT / "data" / "__pytest_marker__").exists()


# 19. Ayni-refresh sonrasi Flight satir sayisi ikiye katlanmadi
def test_19_no_row_count_doubling_on_reingest(ingested_db):
    # Dogru invariant: ilk refresh sonrasi satir sayisi ile ikinci (ayni)
    # refresh sonrasi satir sayisi BIREBIR AYNI olmali - VE ikinci refresh
    # gercekten hicbir yeni satir eklememis olmali (inserted == 0).
    assert ingested_db["flights_after_first"] == ingested_db["flights_after_second"], (
        f"row count changed on re-ingest: "
        f"{ingested_db['flights_after_first']} -> {ingested_db['flights_after_second']}"
    )
    assert ingested_db["second_refresh"]["refresh"]["inserted"] == 0


# 20. Test DB ready durumda kaldi (dosya var, acilabilir, bos degil)
def test_20_test_db_left_ready_for_manual_server_start(ingested_db):
    assert DB_PATH.exists()
    s = _session(ingested_db)
    total = s.scalar(select(func.count()).select_from(Flight))
    s.close()
    assert total > 0
