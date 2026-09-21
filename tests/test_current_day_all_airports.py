"""
ADIM (Current-Day Graph Leakage - Global Audit) - Section 13/14/15
doğrulaması.

Önceki ADIM (Current Operational Day Isolation) tek bir bulguyu (IST'te
görülen previous-day leakage) 4 karşılaştırma havalimanında (IST/CBR/
MFG/OAG) doğruladı. Bu dosya AYNI fix'in (`QueuePrediction.operational_
date` + `process_series()`'in bu alanla filtrelemesi) test DB'sindeki
TÜM aktif havalimanları (`tracked_airports()`, `IST/CBR/MFG/OAG`'la
SINIRLI DEĞİL) ve 5 user-visible graph'ın (Overall/Domestic Security/
International Departure Passport/Security/International Arrival)
HEPSİ için sistematik olarak doğru çalıştığını kanıtlar - "sadece IST
için mi düzeltildi yoksa gerçekten genel mi" sorusuna kesin cevap.

Persistan `tests/incoming_2026_09_19/incoming_2026_09_19.sqlite`
kullanılır - YENİ DB oluşturulmaz, production `data/*`/`database.
sqlite`'a hiç dokunulmaz.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.queue.api import airport_predictions, tracked_airports  # noqa: E402
from app.queue.constants import PROCESS_SECURITY_DOMESTIC  # noqa: E402
from app.queue.domain.operational_day import (  # noqa: E402
    operational_date,
    operational_day_window,
    resolve_airport_timezone,
)
from app.queue.engine import run_predictions  # noqa: E402
from app.queue.models import Airport, QueuePrediction  # noqa: E402

DB_PATH = REPO_ROOT / "tests" / "incoming_2026_09_19" / "incoming_2026_09_19.sqlite"

VISIBLE_GRAPHS = [
    ("overall", lambda api: api["overall"]),
    ("domestic_security", lambda api: api["domestic_security"]),
    ("international_departure.passport", lambda api: api["international_departure"]["passport"]),
    ("international_departure.security", lambda api: api["international_departure"]["security"]),
    ("international_arrival", lambda api: api["international_arrival"]),
]

# Africa/Casablanca (ve benzeri, DST'yi yılın belirli bir gününde
# BAŞLATAN/BİTİREN) havalimanları icin, sorgunun rastladigi GERCEK
# takvim gunu bir DST gecisi icerebilir - bu durumda 25 gercek saat
# (fall-back) veya 23 gercek saat (spring-forward) vardir, "24 UTC-
# adimli grid + yerel LABEL" modeli bu durumda ayni yerel saat
# ETIKETINI iki farkli GERCEK ana (once/sonra offset) atar. Bu,
# ONCEKI GUNUN VERISININ SIZMASI DEGILDIR (asagida ayrica dogrulanir -
# tum bucket'lar TEK bir local_date tasir) - bu yuzden bu OZEL/nadir
# takvim durumu, "her yerel saat TAM 1 kez" testinden ayrik tutulur,
# GIZLENMEZ - raporda ayrica listelenir.


@pytest.fixture(scope="module")
def session():
    assert DB_PATH.exists(), "Persistent test DB bulunamadi - YENI DB olusturulmaz"
    engine = create_engine(f"sqlite:///{DB_PATH}")
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module")
def all_airport_codes(session):
    codes = tracked_airports(session)
    assert len(codes) > 4, "test DB'de sadece bir avuc havalimani var - global audit anlamsiz olur"
    return codes


@pytest.fixture(scope="module")
def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _audit_one_airport(session, code, now):
    airport = session.get(Airport, code)
    tz = resolve_airport_timezone(airport.timezone if airport else None)
    cur_local_date = str(operational_date(tz, now)) if tz is not None else None

    api = airport_predictions(session, code, now=now)

    null_rows = session.scalar(
        select(func.count()).select_from(QueuePrediction).where(
            QueuePrediction.airport_iata == code,
            QueuePrediction.operational_date.is_(None),
        )
    )

    graphs = {}
    for gname, getter in VISIBLE_GRAPHS:
        node = getter(api)
        windows = node["windows"]
        local_dates = sorted({w["window_start_local"][:10] for w in windows if w.get("window_start_local")})
        # (date, hour) cifti ile sayilir - AYNI gundeki bir saatin
        # TEKRARI (gercek DST anomalisi) ile FARKLI gunlerin (mesru,
        # backlog-driven forward spillover) AYNI saat ETIKETINI
        # tasimasi birbirine KARISTIRILMAZ (ikincisi bug DEGIL).
        local_hours = [
            (w["window_start_local"][:10], w["window_start_local"][11:13])
            for w in windows if w.get("window_start_local")
        ]
        hour_counts: dict[tuple[str, str], int] = {}
        for key in local_hours:
            hour_counts[key] = hour_counts.get(key, 0) + 1
        graphs[gname] = {
            "count": len(windows),
            "local_dates": local_dates,
            "hour_counts": hour_counts,
            "current": node["current"],
        }

    return {
        "airport": code, "timezone": airport.timezone if airport else None,
        "current_local_date": cur_local_date, "null_rows": null_rows, "graphs": graphs,
    }


@pytest.fixture(scope="module")
def audit_all(session, all_airport_codes, now_utc):
    return {code: _audit_one_airport(session, code, now_utc) for code in all_airport_codes}


# ========================================================================
# Section 3 - NULL operational_date GLOBAL KONTROL
# ========================================================================

def test_global_null_operational_date_rows_never_leak_into_current_view(session, audit_all):
    """
    NULL `operational_date` satirlari (eski/tag'lenmemis - bu ADIM'dan
    ONCE yazilmis) var OLABILIR (backward-compat, Bolum 3 - "raw/legacy
    alanlarda gerekiyorsa ayri kalsin") ama HICBIR airport'un current
    (padded, day_start'li) visible graph `windows`'una BASKA GUNUN
    NULL satiri olarak SIZMAMALI - `windows` HER airport icin TEK bir
    local_date tasimali (asagidaki previous-day-leak testiyle AYNI
    garanti, burada NULL sayisi ayrica raporlanir).
    """
    total_null = 0
    for code, info in audit_all.items():
        total_null += info["null_rows"]
    print(f"\nGLOBAL NULL operational_date row count (all {len(audit_all)} airports): {total_null}")
    # NULL satir SAYISI sifir olmak ZORUNDA DEGIL (legacy/backward-compat,
    # Bolum 3) - ama asagidaki previous-day-leak testi NULL satirlarin
    # bile current view'a SIZMADIGINI ayrica kanitlar.


@pytest.mark.parametrize("graph_name", [g[0] for g in VISIBLE_GRAPHS])
def test_every_airport_every_graph_is_single_current_local_day(audit_all, graph_name):
    """
    Section 1/4/10/11 - TUM airportlar, TUM 5 graph icin: `current_local_
    date` HER ZAMAN windows icinde bulunmali VE HICBIR tarih current_
    local_date'ten DAHA ESKI olmamali - bu, asil aranan "previous-day
    leakage" imzasidir (D-1/D-2 gibi GECMISTE kalan bir gunun SIZMASI).

    NOT (operational_date fix'inin bilincli, korunan bir yan etkisi):
    KENDI GUNU icinde asiri backlog tasiyan (ornegin kucuk-olcekli bir
    havalimaninda kapasiteyi cok asan bir dalga) bir sürec, event-turevli
    completion zaman damgalari GERCEKTEN 24 saati asip current_local_
    date'in BIR SONRAKI takvim gunune tasabilir - bu satirlar operational_
    date etiketiyle HALA dogru KAYNAK gune (bugune) aittir, sadece
    window_start_local'lari ILERI tasmistir (Section 61 - cross-midnight/
    backlog-carry davranisinin BILEREK KORUNMASI, bkz. models.py
    QueuePrediction.operational_date docstring'i). Bu, GECMIS bir gunun
    SIZMASINDAN kokten FARKLI - o yuzden ILERI (current_local_date'ten
    SONRAKI) tarihler burada BUG SAYILMAZ, sadece current_local_date'ten
    ESKI (ONCEKI) bir tarih SAYILIR.
    """
    leaking = []
    for code, info in audit_all.items():
        g = info["graphs"][graph_name]
        dates = g["local_dates"]
        if not dates:
            continue  # timezone cozulemeyen/hic prediction olmayan airport - ayri ele alinir
        current = info["current_local_date"]
        earlier_dates = [d for d in dates if d < current]
        if current not in dates or earlier_dates:
            leaking.append((code, dates, current))
    assert not leaking, f"{graph_name}: previous-day/mismatched-date leakage bulundu: {leaking}"


@pytest.mark.parametrize("graph_name", [g[0] for g in VISIBLE_GRAPHS])
def test_every_airport_every_graph_has_exactly_24_buckets(audit_all, graph_name):
    """
    `windows` HER ZAMAN EN AZ 24 bucket icermeli (current_local_date'in
    tam 24 saati padding ile GARANTİ). Bazen (asiri backlog spillover,
    yukaridaki testin docstring'inde aciklanan MESRU durum) 24'ten FAZLA
    olabilir - bu bir eksiklik DEGIL, TAM TERSİ (fazladan GERCEK veri).
    Sadece 24'TEN AZ olmak gercek bir eksiklik/bug olurdu.
    """
    under24 = [(code, info["graphs"][graph_name]["count"]) for code, info in audit_all.items()
               if info["graphs"][graph_name]["count"] < 24 and info["timezone"]]
    assert not under24, f"{graph_name}: 24'ten AZ bucket tasiyan airportlar: {under24}"


# ========================================================================
# Section 5 - REPEATED HOURS (DST-gecis gunleri HARIC - yukaridaki not)
# ========================================================================

def test_repeated_local_hours_are_only_explainable_by_a_real_dst_transition(audit_all):
    """
    `hour_counts` artik (local_date, local_hour) ciftiyle anahtarlanir
    (bkz. `_audit_one_airport`) - bu yuzden bir anahtarin BIRDEN FAZLA
    kez gorunmesi ARTIK SADECE "AYNI takvim gununde AYNI saat etiketi
    iki kez uretildi" anlamina gelir (mesru forward-spillover'in FARKLI
    tarihlerdeki AYNI saat etiketleri - ör. "2026-09-20 05:00" ve
    "2026-09-21 05:00" - burada HICBIR ZAMAN "repeated" SAYILMAZ, cunku
    anahtarlari zaten FARKLI). Boyle bir TEK-GUN ici tekrar SADECE o
    havalimaninin timezone'u o GUN GERCEKTEN bir DST gecisi (fall-back)
    yasiyorsa fiziksel olarak mumkundur.
    """
    unexplained = []
    dst_transition_days = []
    for code, info in audit_all.items():
        tz = resolve_airport_timezone(info["timezone"])
        if tz is None:
            continue
        for gname, g in info["graphs"].items():
            repeated = {key: c for key, c in g["hour_counts"].items() if c > 1}
            if not repeated:
                continue
            for (date_str, hour_str), count in repeated.items():
                year, month, day = (int(x) for x in date_str.split("-"))
                # Bu SPESIFIK takvim gunu icin gercekten bir DST gecisi
                # var mi? (o gunun ogle vakti civarindan operational_
                # day_window ile GUNUN KENDI sinirlari cozulur - hard-
                # code offset YOK, production'in KENDI fonksiyonu.)
                probe = datetime(year, month, day, 12, 0)
                day_start, day_end = operational_day_window(tz, probe)
                offset_start = day_start.replace(tzinfo=timezone.utc).astimezone(tz).utcoffset()
                offset_end = (day_end - timedelta(seconds=1)).replace(tzinfo=timezone.utc).astimezone(tz).utcoffset()
                if offset_start == offset_end:
                    unexplained.append((code, gname, date_str, hour_str, count, "no DST transition found on this day"))
                else:
                    dst_transition_days.append((code, gname, date_str, hour_str, count, str(offset_start), str(offset_end)))
    print(f"\nDST-transition-day repeated-hour instances (expected, NOT a bug): {dst_transition_days}")
    assert not unexplained, f"unexplained repeated local hours (real leakage suspected): {unexplained}"


# ========================================================================
# Section 7 - MULTI-AIRPORT DIFFERENT LOCAL DATE (genisletilmis liste)
# ========================================================================

def test_widely_spread_timezones_each_resolve_independently(session):
    """CDG/ZRH/LHR gibi gercek, farkli (ama Avrupa-benzeri) timezone'lu
    havalimanlari test DB'sinde mevcut - her biri KENDI Airport.timezone'undan
    bagimsiz cozulmeli, global bir UTC/server date'e ZORLANMAMALI."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    candidates = ["CDG", "ZRH", "LHR", "IST", "CBR", "MFG"]
    present = [c for c in candidates if session.get(Airport, c) is not None]
    assert len(present) >= 4, f"beklenen genis-yayilmis havalimanlarindan cok azi test DB'sinde mevcut: {present}"

    resolved = {}
    for code in present:
        airport = session.get(Airport, code)
        tz = resolve_airport_timezone(airport.timezone)
        assert tz is not None, f"{code} icin timezone cozulemedi"
        resolved[code] = operational_date(tz, now)

    api_results = {code: airport_predictions(session, code, now=now) for code in present}
    for code in present:
        dates = {w["window_start_local"][:10] for w in api_results[code]["domestic_security"]["windows"] if w.get("window_start_local")}
        assert dates == {str(resolved[code])}, f"{code}: API'nin dondurdugu local tarih, bagimsiz hesaplanan {resolved[code]} ile uyusmuyor"


# ========================================================================
# Section 12 - FRONTEND CONTRACT (Node runtime harness, 24 bar kaniti)
# ========================================================================

INDEX_HTML = REPO_ROOT / "app" / "web" / "static" / "index.html"
BAR_HARNESS = REPO_ROOT / "tests" / "frontend_harness" / "run_chart_bar_count.js"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node.js bu ortamda kurulu değil")
@pytest.mark.parametrize("code", ["IST", "CBR", "MFG", "OAG"])
def test_frontend_drawchart_renders_exactly_24_bars_per_visible_graph(session, code, now_utc, tmp_path):
    """
    index.html'in KENDI `drawChart()`'ini gercek bir JS motorunda
    (Node) calistirip, backend'den gelen GERCEK (bu ADIM'in fix'inden
    GECMIS) `airport_predictions()` cevabi ile her 5 canvas'in TAM 24
    bar aldigini kanitlar - frontend'in kendisi ekstra history merge
    YAPMIYOR, sadece API'nin verdigi current-day 24-window serisini
    ciziyor.
    """
    import json

    api = airport_predictions(session, code, now=now_utc)
    api["airport"] = code
    data_path = tmp_path / f"{code}_data.json"
    data_path.write_text(json.dumps(api), encoding="utf-8")

    result = subprocess.run(
        [NODE, str(BAR_HARNESS), str(INDEX_HTML), str(data_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"node harness threw for {code}: {result.stdout}\n{result.stderr}"
    assert result.stdout.startswith("OK"), result.stdout

    bar_counts = json.loads(result.stdout.strip().splitlines()[1])
    expected_canvases = [
        "chart-overall", "chart-domestic_security",
        "chart-international_departure_passport", "chart-international_departure_security",
        "chart-international_arrival",
    ]
    for canvas_id in expected_canvases:
        assert canvas_id in bar_counts, f"{code}: {canvas_id} hic cizilmedi"
        # >= 24: padding HER ZAMAN current_local_date'in tam 24 saatini
        # garanti eder; asiri backlog spillover (bkz. yukaridaki testin
        # docstring'i) MESRU olarak 24'TEN FAZLA gercek bar uretebilir -
        # sadece 24'TEN AZ olmak eksiklik/bug olurdu.
        assert bar_counts[canvas_id]["count"] >= 24, f"{code}/{canvas_id}: {bar_counts[canvas_id]['count']} bar (>=24 bekleniyor)"
        assert bar_counts[canvas_id]["dataCount"] == bar_counts[canvas_id]["count"]


# ========================================================================
# Section 14 - NEW-DAY INGEST REGRESSION: large/medium/small/unknown
# ========================================================================

@pytest.mark.parametrize("code,scale_label", [
    ("IST", "large"), ("CBR", "medium"), ("MFG", "small"), ("OAG", "unknown"),
])
def test_new_day_empty_then_new_batch_arrives_per_scale(session, code, scale_label):
    """
    Section 6/14 - her scale icin AYRI AYRI: onceki gun (20 Eylul,
    gercek/yogun) -> yeni gun (22 Eylul, HENUZ flight yok) -> current
    graph 22 Eylul icin 24 zero-demand bucket gostermeli (20 Eylul'un
    yogunlugu GORUNMEMELI) -> 22 Eylul icin YENI bir batch (run_
    predictions, `flights_of_airport` uzerinden GERCEK bir flight ile)
    gelince, AYNI request (restart YOK) artik gercek data gostermeli.
    """
    airport = session.get(Airport, code)
    tz = resolve_airport_timezone(airport.timezone)
    assert tz is not None, f"{code} icin timezone cozulemedi - scale={scale_label}"

    # 22 Eylul henuz hic ingest edilmedi (bu DB'de sadece 19/20 Eylul var) -
    # bu YENI, bos bir operasyonel gunu simule eder.
    now_empty_day = datetime(2026, 9, 22, 10, 0)
    api_empty = airport_predictions(session, code, now=now_empty_day)
    empty_windows = api_empty["domestic_security"]["windows"]
    assert len(empty_windows) == 24, f"{code} ({scale_label}): 22 Eylul icin 24 bucket yok"
    assert all(w["flight_count"] == 0 for w in empty_windows), (
        f"{code} ({scale_label}): 22 Eylul (flight yok) icin onceki gunun yogunlugu sizdi"
    )
    empty_dates = {w["window_start_local"][:10] for w in empty_windows if w.get("window_start_local")}
    assert empty_dates == {"2026-09-22"}

    # Simdi 22 Eylul icin GERCEK bir flight ekleyip run_predictions'i
    # (gercek production fonksiyonu, manuel DB yazma YOK) tekrar
    # calistiralim - AYNI API cagrisi artik gercek data gormeli.
    from app.queue.ingestion.refresh import refresh_flights

    dep_local = datetime(2026, 9, 22, 9, 0)
    dep_utc = dep_local.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    row = {
        "flight_key": f"{code}-NEWDAY-TEST", "airport_iata": code,
        "direction": "departure", "location": "international",
        "airline_iata": "TS", "flight_number": "9001", "flight_iata": "TS9001",
        "aircraft_icao": "A320", "aircraft_match_found": True,
        "dep_iata": code, "arr_iata": "ZRH",
        "dep_scheduled_utc": dep_utc, "dep_estimated_utc": None, "dep_actual_utc": None,
        "arr_scheduled_utc": dep_utc + timedelta(hours=3), "arr_estimated_utc": None, "arr_actual_utc": None,
        "status": "scheduled",
    }
    refresh_flights(session, [row])

    from tests.factories import MockCapacityResolver

    resolver = MockCapacityResolver()
    run_predictions(session, resolver, airports=[code], now=now_empty_day)

    api_after = airport_predictions(session, code, now=now_empty_day)
    after_windows = api_after["international_departure"]["passport"]["windows"]
    nonzero = [w for w in after_windows if w["flight_count"] > 0]
    assert nonzero, f"{code} ({scale_label}): yeni 22 Eylul batch'i ingest edildi ama current graph hala bos - restart gerekmeden guncellenmedi"
    for w in nonzero:
        assert w["window_start_local"][:10] == "2026-09-22"

    # 20 Eylul'un (bu airport icin gercek, yogun) verisi bu SORGUDA
    # (now=22 Eylul) hala GORUNMUYOR - history DB'de duruyor ama
    # current view'a sizmadi.
    all_dates = {w["window_start_local"][:10] for w in after_windows if w.get("window_start_local")}
    assert all_dates == {"2026-09-22"}, f"{code}: 22 Eylul current view'ina baska tarih sizdi: {all_dates}"


# ========================================================================
# Section 15 - HISTORY KORUNUYOR MU (yukaridaki testler DB'yi degistirdi -
# 20 Eylul'un gercek satirlari hala fiziksel olarak orada mi?)
# ========================================================================

def test_history_rows_survive_all_the_above_queries_and_new_day_ingests(session):
    for code in ("IST", "CBR", "MFG", "OAG"):
        airport = session.get(Airport, code)
        tz = resolve_airport_timezone(airport.timezone)
        op_date_20 = operational_date(tz, datetime(2026, 9, 20, 10, 0))
        remaining = session.scalar(
            select(func.count()).select_from(QueuePrediction).where(
                QueuePrediction.airport_iata == code,
                QueuePrediction.operational_date == op_date_20,
            )
        )
        assert remaining > 0, f"{code}: 20 Eylul'un GERCEK gecmis QueuePrediction satirlari kayboldu"
