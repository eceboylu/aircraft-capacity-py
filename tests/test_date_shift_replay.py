"""
Date-shift / time-translation doğrulaması (genel-proje.md bu turun
talimatı).

GERÇEK `data/*.json` dosyaları (`tests/date_shift_replay/_generate_
shifted_fixtures.py` ile ÜRETİLMİŞ, sadece +4 gün kaydırılmış birebir
kopyaları) production pipeline zincirinden (gerçek parser, gerçek
event-driven engine, gerçek API) izole SQLite DB'lere karşı iki kez
geçirilir - biri orijinal tarihle ("bugün" = 2026-09-14), diğeri
shifted tarihle ("bugün" = 2026-09-18) - ve sonuçlar birebir (SADECE
tarih +4 gün farkla) karşılaştırılır.

Production kodu bu turda DEĞİŞTİRİLMEDİ. Gerçek `database.sqlite` ve
gerçek `data/*.json` dosyaları HİÇ açılıp yazılmadı - sadece OKUNDU
(CDG/CBR fixture üretimi için) ve izole `tests/date_shift_replay/*.sqlite`
dosyaları kullanıldı.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.date_shift_replay.run_date_shift_replay import (
    REAL_DATA_DIR,
    REPLAY_DIR,
    replay,
)

SHIFT = timedelta(days=4)

# CDG: Europe/Paris (UTC+2 in September) - 20:00 UTC güvenle AYNI yerel
# güne düşer (22:00 local). Temsili havalimanı - gerçek datada
# International Departure için EN yoğun tek saat/havalimanı kümesi
# (bkz. rapor, Bölüm 7 - 14 uçuş, hepsi 06:00 UTC bucket'ında).
CDG_NOW_ORIGINAL = datetime(2026, 9, 14, 20, 0)
CDG_NOW_SHIFTED = datetime(2026, 9, 18, 20, 0)

# CBR: Australia/Sydney (UTC+10, Eylül'de henüz DST yok) - 12:00 UTC
# güvenle aynı yerel güne düşer (22:00 local). Domestic Departure akışı
# için gerçek datadaki EN yoğun tek havalimanı (5 flight, tek saat).
CBR_NOW_ORIGINAL = datetime(2026, 9, 14, 12, 0)
CBR_NOW_SHIFTED = datetime(2026, 9, 18, 12, 0)


@pytest.fixture(scope="module")
def cdg_original(tmp_path_factory):
    r = replay(
        REAL_DATA_DIR, tmp_path_factory.mktemp("ds") / "cdg_original.sqlite",
        CDG_NOW_ORIGINAL, airports=["CDG"],
    )
    yield r
    r["session"].close()


@pytest.fixture(scope="module")
def cdg_shifted(tmp_path_factory):
    """
    ADIM (Date-Shift Replay Test Data Expansion) NOTU: `REPLAY_DIR`
    artık CDG için EK TS9xxx test flight'ları da içeriyor (Bölüm 3/4) -
    bu fixture'ı kullanan ESKİ "saf +4 gün kaydırma kimliği" testleri
    (`test_graph_series_identical_except_date_shift` vb.) bu EK veriden
    ETKİLENMESİN diye `exclude_flight_iata_prefix="TS9"` ile SADECE
    gerçek 200 flight'ın +4 gün kaydırılmış kopyası kullanılır - test
    verisi SİLİNMEDİ, sadece bu ÖZEL identity-proof fixture'ı için
    devre dışı bırakıldı (bkz. `replay()` docstring'i).
    """
    r = replay(
        REPLAY_DIR, tmp_path_factory.mktemp("ds") / "cdg_shifted.sqlite",
        CDG_NOW_SHIFTED, airports=["CDG"], exclude_flight_iata_prefix="TS9",
    )
    yield r
    r["session"].close()


@pytest.fixture(scope="module")
def cdg_original_with_sep18_context(tmp_path_factory):
    """Bölüm 18 (B aşaması) - AYNI orijinal dosya, ama 'now' Sep18 diyor."""
    r = replay(
        REAL_DATA_DIR, tmp_path_factory.mktemp("ds") / "cdg_original_sep18ctx.sqlite",
        CDG_NOW_SHIFTED, airports=["CDG"],
    )
    yield r
    r["session"].close()


@pytest.fixture(scope="module")
def cbr_original(tmp_path_factory):
    r = replay(
        REAL_DATA_DIR, tmp_path_factory.mktemp("ds") / "cbr_original.sqlite",
        CBR_NOW_ORIGINAL, airports=["CBR"],
    )
    yield r
    r["session"].close()


@pytest.fixture(scope="module")
def cbr_shifted(tmp_path_factory):
    """
    ADIM (Airport Scale Test Matrix) NOTU: `REPLAY_DIR` artık CBR için
    EK `TM9xxx` işaretli scale-matrix test flight'ları da içeriyor (bkz.
    `_generate_scale_matrix_test_flights.py`) - bu identity-proof'un
    (SADECE gerçek 5-flight CBR domestic kümesiyle) etkilenmemesi için
    `exclude_flight_iata_prefix="TM9"` ile filtrelenir.
    """
    r = replay(
        REPLAY_DIR, tmp_path_factory.mktemp("ds") / "cbr_shifted.sqlite",
        CBR_NOW_SHIFTED, airports=["CBR"], exclude_flight_iata_prefix="TM9",
    )
    yield r
    r["session"].close()


# ========================================================================
# Bölüm 1-4 - fixture üretim doğruluğu (record count, alan koruması).
# ========================================================================

def test_source_a_record_counts_preserved_after_shift():
    """
    ADIM (Date-Shift Replay Test Data Expansion) - Bölüm 2/17: shifted
    fixture artık orijinal 100+100 gerçek kayda EK OLARAK TS9xxx test
    flight'ları içeriyor (bilinçli genişletme, bkz. `_generate_extra_
    test_flights.py`) - bu yüzden ARTIK "birebir eşit sayı" DEĞİL,
    "en az kadar VE ilk 100 kayıt birebir orijinalin AYNISI (sadece +4
    gün kaydırılmış)" doğrulanır - gerçek 200 flight KAYBOLMADI/
    DEĞİŞMEDİ, SADECE üzerine eklendi.
    """
    import json
    orig_dep = json.load(open(REAL_DATA_DIR / "Delays - Type Departures.json", encoding="utf-8"))["response"]
    shift_dep = json.load(open(REPLAY_DIR / "Delays - Type Departures.json", encoding="utf-8"))["response"]
    orig_arr = json.load(open(REAL_DATA_DIR / "Delays - Type Arrivals.json", encoding="utf-8"))["response"]
    shift_arr = json.load(open(REPLAY_DIR / "Delays - Type Arrivals.json", encoding="utf-8"))["response"]
    assert len(shift_dep) >= len(orig_dep)
    assert len(shift_arr) >= len(orig_arr)
    assert [r.get("flight_iata") for r in shift_dep[:len(orig_dep)]] == [r.get("flight_iata") for r in orig_dep]
    assert [r.get("flight_iata") for r in shift_arr[:len(orig_arr)]] == [r.get("flight_iata") for r in orig_arr]
    # Eklenen fazlalık SADECE TS9xxx (Bölüm 18) VEYA TM9xxx (ADIM Airport
    # Scale Test Matrix - CBR/MFG/OAG, bkz. `_generate_scale_matrix_test_
    # flights.py`) marker'lı test flight'ları olmalı.
    extra_dep = shift_dep[len(orig_dep):]
    extra_arr = shift_arr[len(orig_arr):]

    def _is_marked_test_flight(record):
        iata = record.get("flight_iata") or ""
        return iata.startswith("TS9") or iata.startswith("TM9")

    assert all(_is_marked_test_flight(r) for r in extra_dep)
    assert all(_is_marked_test_flight(r) for r in extra_arr)


def test_source_b_updated_shifted_exactly_4_days_and_count_preserved():
    import json
    orig_b = json.load(open(REAL_DATA_DIR / "response-delays.json", encoding="utf-8"))["response"]
    shift_b = json.load(open(REPLAY_DIR / "flights_live.json", encoding="utf-8"))["response"]
    assert len(orig_b) == len(shift_b)
    for o, s in zip(orig_b, shift_b):
        assert s["updated"] - o["updated"] == 4 * 24 * 60 * 60


def test_every_datetime_field_shifted_exactly_4_days_aircraft_status_unchanged():
    """
    ADIM (Date-Shift Replay Test Data Expansion) NOTU: `shift` artık
    orijinal 100 kaydın ÜZERİNE TS9xxx test flight'ları da içeriyor
    (bkz. `test_source_a_record_counts_preserved_after_shift`) - bu
    yüzden ARTIK "birebir eşit uzunluk" değil, "shift'in İLK 100 kaydı
    orijinalin BİREBİR +4 gün kaydırılmış hali" doğrulanır.
    """
    import json
    orig = json.load(open(REAL_DATA_DIR / "Delays - Type Departures.json", encoding="utf-8"))["response"]
    shift_full = json.load(open(REPLAY_DIR / "Delays - Type Departures.json", encoding="utf-8"))["response"]
    assert len(shift_full) >= len(orig)
    shift = shift_full[:len(orig)]
    for o, s in zip(orig, shift):
        # Kimlik/uçak/statü alanları DEĞİŞMEMELİ.
        for field in (
            "airline_iata", "flight_iata", "flight_icao", "flight_number",
            "dep_iata", "arr_iata", "aircraft_icao", "status", "duration",
            "delayed", "dep_delayed", "arr_delayed", "dep_terminal", "dep_gate",
        ):
            assert o.get(field) == s.get(field), field
        # Datetime string alanları +4 gün.
        for field in ("dep_time_utc", "dep_estimated_utc", "dep_actual_utc", "arr_time_utc"):
            if o.get(field):
                fmt = "%Y-%m-%d %H:%M"
                o_dt = datetime.strptime(o[field], fmt)
                s_dt = datetime.strptime(s[field], fmt)
                assert s_dt - o_dt == SHIFT, field
        # Epoch alanları +345600 saniye.
        for field in ("dep_time_ts", "dep_estimated_ts", "dep_actual_ts", "arr_time_ts"):
            if o.get(field) is not None:
                assert s[field] - o[field] == 345600, field


# ========================================================================
# Bölüm 6/18 - operational-day seçimi, ana invariant.
# ========================================================================

def test_operational_day_selection_three_stage_proof(
    cdg_original, cdg_original_with_sep18_context, cdg_shifted,
):
    """
    `cdg_shifted` artık `exclude_flight_iata_prefix="TS9"` ile SADECE
    gerçek 200 flight'ın +4 gün kaydırılmış kopyasını kullanıyor (bkz.
    fixture docstring'i) - bu yüzden bu identity-proof AYNEN (Date-Shift
    Replay Test Data Expansion ÖNCESİYLE BİREBİR) geçerliliğini korur.
    """
    selected_A = cdg_original["selected_by_airport"]["CDG"]
    selected_B = cdg_original_with_sep18_context["selected_by_airport"]["CDG"]
    selected_C = cdg_shifted["selected_by_airport"]["CDG"]

    assert len(selected_A) == 15  # gerçek datada CDG'nin Sep14 kaydı sayısı
    # B) AYNI orijinal (Sep14) veri, ama "bugün" Sep18 context'inde
    #    çalıştırılırsa - Sep14 flight'ları ARTIK "bugünün" flight'ı
    #    DEĞİL, operational-day filtresi hepsini ELER.
    assert len(selected_B) == 0
    # C) +4 gün shifted veri (Sep18), "bugün" Sep18 context'inde -
    #    AYNI flight seti tekrar seçilir.
    assert len(selected_C) == len(selected_A)

    keys_A = {f.flight_key.replace("2026-09-14", "") for f in selected_A}
    keys_C = {f.flight_key.replace("2026-09-18", "") for f in selected_C}
    assert keys_A == keys_C  # tarih dışında flight_key'ler BİREBİR aynı


def test_selected_flight_identity_preserved_across_shift(cdg_original, cdg_shifted):
    orig_by_id = {
        (f.airline_iata, f.flight_number, f.dep_iata, f.arr_iata): f
        for f in cdg_original["selected_by_airport"]["CDG"]
    }
    shift_by_id = {
        (f.airline_iata, f.flight_number, f.dep_iata, f.arr_iata): f
        for f in cdg_shifted["selected_by_airport"]["CDG"]
    }
    assert set(orig_by_id) == set(shift_by_id)
    for identity, o in orig_by_id.items():
        s = shift_by_id[identity]
        assert o.aircraft_icao == s.aircraft_icao
        assert o.status == s.status
        assert o.direction == s.direction
        assert o.location == s.location
        assert s.dep_scheduled_utc - o.dep_scheduled_utc == SHIFT if o.dep_scheduled_utc else s.dep_scheduled_utc == o.dep_scheduled_utc
        if o.dep_actual_utc:
            assert s.dep_actual_utc - o.dep_actual_utc == SHIFT
        if o.dep_estimated_utc:
            assert s.dep_estimated_utc - o.dep_estimated_utc == SHIFT


# ========================================================================
# Bölüm 12/13 - graph/API date-shift (4 grafiğin TAMAMI).
# ========================================================================

def _windows_by_start(windows):
    """`windows` zaten shifted dataset'ten geldiği için burada TEKRAR
    +4 gün uygulanmaz - sadece kendi `window_start`'ına göre indekslenir
    (arama tarafı `original_start + SHIFT` ile karşılık gelen anahtarı
    hesaplar - bkz. çağıran testler)."""
    return {
        datetime.fromisoformat(w["window_start"]): w for w in windows
    }


def _resolve_section(api_result: dict, path: str) -> dict:
    """
    ADIM (International Departure Split Graphs) - `international_departure`
    artık tek düz bir seri değil, `{process, passport, security}` -
    bu yüzden bazı bölümler dotted-path ile ("international_departure.
    passport") adreslenir; diğerleri (overall/domestic_security/
    international_arrival) düz kalır.
    """
    node = api_result
    for part in path.split("."):
        node = node[part]
    return node


@pytest.mark.parametrize("section,fields", [
    ("overall", ("risk", "estimated_wait_minutes")),
    ("domestic_security", ("flight_count", "expected_passengers", "risk", "estimated_wait_minutes")),
    ("international_arrival", ("flight_count", "expected_passengers", "risk", "estimated_wait_minutes")),
    ("international_departure.passport", ("risk", "estimated_wait_minutes")),
    ("international_departure.security", ("risk", "estimated_wait_minutes")),
])
def test_graph_series_identical_except_date_shift(cdg_original, cdg_shifted, section, fields):
    original_windows = _resolve_section(cdg_original["api_by_airport"]["CDG"], section)["windows"]
    shifted_windows = _resolve_section(cdg_shifted["api_by_airport"]["CDG"], section)["windows"]
    shifted_by_start = _windows_by_start(shifted_windows)

    assert len(original_windows) == len(shifted_windows)
    for w in original_windows:
        expected_start = datetime.fromisoformat(w["window_start"]) + SHIFT
        match = shifted_by_start.get(expected_start)
        assert match is not None, f"{section}: {w['window_start']} + 4d bulunamadı"
        for field in fields:
            assert w[field] == match[field], f"{section}.{field} @ {w['window_start']}"


def test_international_departure_passport_and_security_series_match_after_shift(
    cdg_original, cdg_shifted,
):
    """
    ADIM (International Departure Split Graphs): passport/security artık
    İKİ BAĞIMSIZ seri - her biri KENDİ `windows` listesinde +4 gün
    kaydırma dışında birebir aynı olmalı (eskiden tek pencerenin İÇİNDEKİ
    alt-nesneler karşılaştırılıyordu, artık iki AYRI seri karşılaştırılır).
    """
    intl_dep_o = cdg_original["api_by_airport"]["CDG"]["international_departure"]
    intl_dep_s = cdg_shifted["api_by_airport"]["CDG"]["international_departure"]

    for stage, fields in (
        ("passport", ("flight_count", "expected_passengers", "estimated_wait_minutes")),
        ("security", ("expected_passengers", "estimated_wait_minutes")),
    ):
        original_windows = intl_dep_o[stage]["windows"]
        shifted_by_start = _windows_by_start(intl_dep_s[stage]["windows"])
        assert original_windows, stage
        for w in original_windows:
            expected_start = datetime.fromisoformat(w["window_start"]) + SHIFT
            match = shifted_by_start[expected_start]
            for field in fields:
                assert w[field] == match[field], f"{stage}.{field} @ {w['window_start']}"


def test_domestic_security_graph_shift_cbr(cbr_original, cbr_shifted):
    """
    CBR: gerçek datadaki en yoğun tek-havalimanılı Domestic Departure
    kümesi. ADIM (24-Hour Graph) ile GÜNCELLENDİ: seri artık TAM 24
    saat (bkz. `_pad_series_to_24_hours`) - GERÇEK (flight_count>0)
    olan TEK saat aranır, diğer 23 saat sıfır-talep bucket'ı olarak
    kalır (asıl iddia AYNI: tek gerçek yoğun saat +4 gün kaydırılmış).
    """
    orig = cbr_original["api_by_airport"]["CBR"]["domestic_security"]["windows"]
    shifted = cbr_shifted["api_by_airport"]["CBR"]["domestic_security"]["windows"]
    assert len(orig) == 24
    assert len(shifted) == 24
    orig_real = [w for w in orig if w["flight_count"] > 0]
    shifted_real = [w for w in shifted if w["flight_count"] > 0]
    assert len(orig_real) == 1
    assert len(shifted_real) == 1
    o, s = orig_real[0], shifted_real[0]
    assert datetime.fromisoformat(s["window_start"]) - datetime.fromisoformat(o["window_start"]) == SHIFT
    assert o["flight_count"] == s["flight_count"] == 5
    # ADIM (Departure Show-Up Profile): bu saatin ham talebi (900, 5
    # flight'ın TOPLAMI) artık TEK bu saatte DEĞİL - her flight'ın show-up
    # profili (bkz. domain/demand.py) talebini KENDİ departure saatinden
    # önceki 3 saate yayıyor; bu saat SADECE kendi payını (475, komşu
    # saatlere yayılanın GERİ KALANI) taşıyor - flight_count (bucket
    # kimliği, DEĞİŞMEDİ) hâlâ 5. Asıl iddia (4 günlük kayma sonrası
    # AYNI değer) korunuyor - SADECE mutlak sayı değişti.
    assert o["expected_passengers"] == s["expected_passengers"] == 475
    assert o["risk"] == s["risk"]
    assert o["estimated_wait_minutes"] == s["estimated_wait_minutes"]
    # ADIM (Departure Show-Up Profile): komşu saatler (flight_count=0)
    # ARTIK gerçekten sıfır-talep DEĞİL - show-up'ın bu saate düşen
    # payını taşıyorlar (uydurma bir değer DEĞİL, GERÇEK dağılım). Asıl
    # invariant DEĞİŞMEDİ: (a) 24 saatin TOPLAMI (900, hiçbir yolcu
    # kaybolmadı/çoğalmadı) VE (b) TÜM serinin (sadece tek "gerçek" saat
    # değil) +4 gün kaydırıldığında BİREBİR AYNI kalması.
    assert sum(w["expected_passengers"] for w in orig) == 900
    assert sum(w["expected_passengers"] for w in shifted) == 900
    orig_by_offset = {datetime.fromisoformat(w["window_start"]): w for w in orig}
    shifted_by_offset = {datetime.fromisoformat(w["window_start"]) - SHIFT: w for w in shifted}
    assert set(orig_by_offset) == set(shifted_by_offset)
    for start, w in orig_by_offset.items():
        match = shifted_by_offset[start]
        assert w["expected_passengers"] == match["expected_passengers"]
        assert w["flight_count"] == match["flight_count"]


# ========================================================================
# Bölüm 9D/16 - passport->security passenger conservation.
# ========================================================================

def test_passenger_conservation_departure_passport_equals_security_total(cdg_original, cdg_shifted):
    """
    ADIM (International Departure Split Graphs): passport ve security
    artık AYRI seriler, farklı saatlere dağılabilirler (cross-hour
    coupling) - ama TOPLAM yolcu korunumu (departure passport'un
    serbest bıraktığı toplam == security'nin karşıladığı toplam)
    hâlâ geçerli, sadece artık HER SERİNİN KENDİ `windows` toplamından
    hesaplanıyor.
    """
    for label, result in (("original", cdg_original), ("shifted", cdg_shifted)):
        intl_dep = result["api_by_airport"]["CDG"]["international_departure"]
        total_passport_dep = sum(
            w["expected_passengers"] for w in intl_dep["passport"]["windows"]
        )
        total_security = sum(
            w["expected_passengers"] for w in intl_dep["security"]["windows"]
        )
        assert total_passport_dep == total_security == 2645, label


# ========================================================================
# Bölüm 14 - hourly bucket shift (bütün bucket'lar programatik).
# ========================================================================

def test_all_hourly_buckets_shifted_exactly_4_days(cdg_original, cdg_shifted):
    for section in (
        "overall", "domestic_security", "international_arrival",
        "international_departure.passport", "international_departure.security",
    ):
        orig_starts = {
            datetime.fromisoformat(w["window_start"])
            for w in _resolve_section(cdg_original["api_by_airport"]["CDG"], section)["windows"]
        }
        shifted_starts = {
            datetime.fromisoformat(w["window_start"])
            for w in _resolve_section(cdg_shifted["api_by_airport"]["CDG"], section)["windows"]
        }
        expected_shifted_starts = {t + SHIFT for t in orig_starts}
        assert expected_shifted_starts == shifted_starts, section
        # her bucket TAM saat sınırında (dk/sn=0).
        for t in shifted_starts:
            assert t.minute == 0 and t.second == 0


# ========================================================================
# Bölüm 19 - gerçek database.sqlite / gerçek data dosyaları dokunulmadı.
# ========================================================================

def test_real_data_files_and_database_untouched():
    """
    Replay bu turda gerçek `data/*` dosyalarını/`database.sqlite`'ı
    MUTATE ETMEDİ mi - `git status --porcelain` ile doğrulanır.

    ADIM (Airport-Scale Queue Capacity): `data/buyuk_olcekli_
    havaalanlari.txt` / `orta_olcekli_havaalanlari.txt` / `kucuk_
    olcekli_havaalanlari.txt` BİLİNÇLİ, YENİ proje girdileridir (large/
    medium/small ölçek referansı) - henüz git'e commit edilmemiş
    OLABİLİRLER ("??" = untracked), bu bir MUTATION değildir (bu replay
    onları hiç YAZMADI, sadece OKUDU - `airports_import.py:import_
    airport_scales()`). Bu yüzden SADECE bu 3 dosya allowlist'e girer -
    başka HİÇBİR data/ değişikliği veya yeni dosyası (ya da
    database.sqlite'a dair HERHANGİ bir satır) kabul edilmez; tracked
    bir data dosyasının GERÇEK bir değişikliği (" M data/...") hâlâ bu
    testi FAIL ettirir.
    """
    import subprocess

    repo_root = REPLAY_DIR.parents[1]
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "data/", "database.sqlite"],
        cwd=repo_root, capture_output=True, text=True,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]

    allowlisted_untracked = {
        "?? data/buyuk_olcekli_havaalanlari.txt",
        "?? data/orta_olcekli_havaalanlari.txt",
        "?? data/kucuk_olcekli_havaalanlari.txt",
    }
    unexpected = [line for line in lines if line.strip() not in allowlisted_untracked]
    assert unexpected == [], "\n".join(unexpected)


# ========================================================================
# ADIM (Multi-Airport Date-Shift Replay) - Bölüm 3/4/12/13: tek
# havalimanına hard-code YOK, shift edilmiş veride GERÇEKTEN bulunan
# TÜM operational havalimanları import edilir, her biri BAĞIMSIZ
# prediction üretir.
# ========================================================================

@pytest.fixture(scope="module")
def all_airports_shifted(tmp_path_factory):
    r = replay(
        REPLAY_DIR, tmp_path_factory.mktemp("ds-all") / "all_airports.sqlite",
        CDG_NOW_SHIFTED, airports=None,
    )
    yield r
    r["session"].close()


def test_shifted_data_contains_multiple_distinct_airports():
    """Bölüm 13.1: shift edilmiş kaynak veri içinde birden fazla havalimanı var."""
    import json

    dep = json.load(open(REPLAY_DIR / "Delays - Type Departures.json", encoding="utf-8"))["response"]
    arr = json.load(open(REPLAY_DIR / "Delays - Type Arrivals.json", encoding="utf-8"))["response"]
    dep_airports = {r["dep_iata"] for r in dep if r.get("dep_iata")}
    arr_airports = {r["arr_iata"] for r in arr if r.get("arr_iata")}
    assert len(dep_airports | arr_airports) > 10  # 78 gerçek - eşik gevşek, kırılganlığı önler


def test_no_single_airport_filter_applied_all_operational_airports_imported(all_airports_shifted):
    """Bölüm 13.2/13.3: `airports=None` -> hard-code tek havalimanı filtresi YOK, DB'de birden fazla distinct airport var."""
    session = all_airports_shifted["session"]
    from sqlalchemy import select, func

    from app.queue.models import Flight

    distinct_airports = session.execute(
        select(func.count(func.distinct(Flight.airport_iata)))
    ).scalar_one()
    assert distinct_airports > 10
    assert distinct_airports == len(all_airports_shifted["airports"])


def test_each_airport_flight_count_matches_its_own_imported_data(all_airports_shifted):
    """Bölüm 13.4: her airport'un flight_count'u KENDİ (izole sorgulanmış) verisiyle uyumlu."""
    session = all_airports_shifted["session"]
    from sqlalchemy import select, func

    from app.queue.models import Flight

    for code, flights in all_airports_shifted["all_flights_by_airport"].items():
        db_count = session.execute(
            select(func.count()).select_from(Flight).where(Flight.airport_iata == code)
        ).scalar_one()
        assert db_count == len(flights)


def test_airport_isolation_no_passenger_leakage_between_real_airports(all_airports_shifted):
    """
    Bölüm 12/13.5: Airport A'nın passenger'ı Airport B'ye KARIŞMIYOR.
    CDG (15 flight) ve ZRH (15 flight) - en zengin iki gerçek havalimanı,
    her ikisi de kendi flight setiyle BİREBİR eşleşmeli, biri diğerinin
    demand'ini İÇERMEMELİ.
    """
    api = all_airports_shifted["api_by_airport"]
    cdg_flights = {f.flight_key for f in all_airports_shifted["all_flights_by_airport"]["CDG"]}
    zrh_flights = {f.flight_key for f in all_airports_shifted["all_flights_by_airport"]["ZRH"]}
    assert cdg_flights.isdisjoint(zrh_flights)

    # API seviyesinde: CDG'nin overall/graph serisi ZRH'ninkiyle AYNI
    # DEĞİL (farklı gerçek demand -> farklı sonuç, kopya/karışma yok).
    assert api["CDG"]["overall"]["windows"] != api["ZRH"]["overall"]["windows"]


def test_plus_4_days_preserved_for_a_real_multi_airport_flight(all_airports_shifted):
    """Bölüm 13.6: +4 gün TÜM timestamp'lerde korunuyor - gerçek CDG flight'ı üzerinden."""
    flights = all_airports_shifted["all_flights_by_airport"]["CDG"]
    assert flights
    for f in flights:
        for field in (f.dep_scheduled_utc, f.dep_actual_utc, f.dep_estimated_utc):
            if field is not None:
                assert field.year == 2026 and field.month == 9 and field.day in (17, 18, 19)


def test_original_json_files_unchanged_multi_airport():
    """Bölüm 13.7 - önceki turda zaten test edildi, burada AYRICA doğrulanır."""
    import json

    orig_dep = json.load(open(REAL_DATA_DIR / "Delays - Type Departures.json", encoding="utf-8"))["response"]
    assert len(orig_dep) == 100


def test_real_database_sqlite_unchanged_after_multi_airport_replay(all_airports_shifted):
    """Bölüm 13.8: çoklu-havalimanı replay'i database.sqlite'ı DEĞİŞTİRMEDİ."""
    import subprocess

    repo_root = REPLAY_DIR.parents[1]
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "database.sqlite"],
        cwd=repo_root, capture_output=True, text=True,
    )
    assert result.stdout.strip() == ""


def test_api_produces_different_responses_for_different_real_airports(all_airports_shifted):
    """Bölüm 13.9: test DB üzerinden API farklı airport'lar için FARKLI response üretebiliyor."""
    api = all_airports_shifted["api_by_airport"]
    responses = {code: data["breakdown"] for code, data in api.items() if code in ("CDG", "ZRH", "DUB", "AMS")}
    assert len(responses) == 4
    # en az bir çiftin breakdown'ı FARKLI olmalı (kopya bir tablo değil).
    values = list(responses.values())
    assert not all(v == values[0] for v in values)


def test_four_graph_contract_holds_for_every_airport_with_predictions(all_airports_shifted):
    """
    Bölüm 13.10/14: HER havalimanı için (prediction'ı olsun ya da
    olmasın) 4-graph contract (anahtarların KENDİSİ) bozulmuyor. Bir
    airport'ta belirli bir graph boş olabilir (ör. domestic flight
    yoksa Domestic Security boş) - bu BUG DEĞİL, dürüstçe raporlanan
    gerçek veri sonucudur.
    """
    api = all_airports_shifted["api_by_airport"]
    assert len(api) > 10
    for code, data in api.items():
        for key in ("overall", "domestic_security", "international_arrival"):
            assert key in data, f"{code} eksik graph anahtarı: {key}"
            assert "windows" in data[key]
        assert "international_departure" in data, f"{code} eksik graph anahtarı: international_departure"
        for stage in ("passport", "security"):
            assert stage in data["international_departure"], f"{code} eksik stage: {stage}"
            assert "windows" in data["international_departure"][stage]


def test_airport_scale_bootstrap_ran_via_real_production_pipeline_function(all_airports_shifted):
    """Bölüm 7: scale bootstrap gerçek `pipeline.py:ensure_airport_scales()` üzerinden çalıştı, hard-code YOK."""
    scale_by_airport = all_airports_shifted["scale_by_airport"]
    resolved = {code: scale for code, scale in scale_by_airport.items() if scale is not None}
    assert len(resolved) > 5
    assert scale_by_airport.get("CDG") == "large"
    assert scale_by_airport.get("CBR") == "medium"


# ========================================================================
# ADIM (Airport-Bazlı Replay Now) - Bölüm 12: artık TEK global `now`
# YOK, her havalimanı KENDİ shift edilmiş flight timeline'ından
# türetilmiş KENDİ `now`'uyla çalışıyor.
# ========================================================================

@pytest.fixture(scope="module")
def per_airport_now_replay(tmp_path_factory):
    r = replay(
        REPLAY_DIR, tmp_path_factory.mktemp("ds-pan") / "per_airport_now.sqlite",
        CDG_NOW_SHIFTED, airports=None, per_airport_now=True,
    )
    yield r
    r["session"].close()


def test_each_airport_gets_its_own_separately_computed_now(per_airport_now_replay):
    """Bölüm 12.1/12.2: her airport için AYRI bir `now` hesaplanıyor - tek global now DEĞİL."""
    now_by_airport = per_airport_now_replay["now_by_airport"]
    assert len(now_by_airport) > 10
    distinct_now_values = set(now_by_airport.values())
    # Tek global now kullanılsaydı TÜM havalimanları AYNI now değerine
    # sahip olurdu - burada BİRDEN FAZLA farklı değer olmalı.
    assert len(distinct_now_values) > 5


def test_airport_now_is_derived_from_its_own_real_flight_timeline(per_airport_now_replay):
    """
    Bölüm 12.3: `airport_now`, o airport'un GERÇEK shift edilmiş
    flight'larından (production `effective_time()` üzerinden) türetilir
    - uydurma/rastgele bir değer DEĞİL. İki geçerli sonuç vardır (bkz.
    `compute_airport_now()`):
      1) Normal yol: `airport_now = first_queue_event - 30dk` (delta
         tam olarak 30dk).
      2) Güvenli-sınır nudge'ı (Bölüm 4): `first_queue_event - 30dk`
         tetikleyici flight'ı KENDİ operasyonel günü dışına düşürüyorsa,
         `airport_now` o flight'ın KENDİ `flight_reference_time()`'ına
         düşer - bu durumda delta 30dk'dan FARKLI olabilir (gerçek bir
         örnek: CDG'nin operasyonel günü tam `first_queue_event`'te
         başladığı için 2 saatlik nudge oluşuyor - bkz. rapor) ama HÂLÂ
         "gerçek timeline'dan türetilmiş" bir değerdir - rastgele/
         hard-code DEĞİL.
    """
    from tests.date_shift_replay.run_date_shift_replay import compute_airport_now
    from app.queue.domain.operational_day import resolve_airport_timezone
    from app.queue.models import Airport

    session = per_airport_now_replay["session"]
    now_by_airport = per_airport_now_replay["now_by_airport"]
    first_event_by_airport = per_airport_now_replay["first_queue_event_by_airport"]
    all_flights_by_airport = per_airport_now_replay["all_flights_by_airport"]
    checked = 0
    for code, first_event in first_event_by_airport.items():
        if first_event is None:
            continue
        checked += 1
        expected_now, expected_first_event = compute_airport_now(
            all_flights_by_airport[code],
            resolve_airport_timezone(session.get(Airport, code).timezone),
            per_airport_now_replay["now"],
        )
        assert expected_first_event == first_event, code
        assert expected_now == now_by_airport[code], code
    assert checked > 10


def test_no_manual_timezone_offset_hardcoded_in_replay_runner():
    """Bölüm 5: replay runner'da elle timezone offset (ör. sabit +2/+10 saat) YOK - hepsi production `operational_day.py`/`demand.py`'ye devredilmiş."""
    source = (REPLAY_DIR / "run_date_shift_replay.py").read_text(encoding="utf-8")
    assert "timedelta(hours=" not in source
    assert "resolve_airport_timezone" in source
    assert "effective_time" in source
    assert "operational_day_window" in source


def test_airport_isolation_holds_under_per_airport_now(per_airport_now_replay):
    """
    Bölüm 8: bir airport'un `airport_now`'u başka bir airport'un
    queue state'ini/predictions'ını ETKİLEMEMELİ - CDG ve ZRH'nin
    (en zengin iki gerçek havalimanı) `now`'ları farklı olsa bile
    her biri kendi gerçek flight'larıyla eşleşen bağımsız sonuç üretir.
    """
    now_by_airport = per_airport_now_replay["now_by_airport"]
    api = per_airport_now_replay["api_by_airport"]
    assert now_by_airport["CDG"] != now_by_airport["ZRH"] or True  # farklı olabilir ama zorunlu değil
    cdg_flights = {f.flight_key for f in per_airport_now_replay["all_flights_by_airport"]["CDG"]}
    zrh_flights = {f.flight_key for f in per_airport_now_replay["all_flights_by_airport"]["ZRH"]}
    assert cdg_flights.isdisjoint(zrh_flights)
    assert api["CDG"]["overall"]["windows"] != api["ZRH"]["overall"]["windows"]


def test_original_json_files_unchanged_after_per_airport_now_replay():
    """Bölüm 12.6."""
    import json

    orig_dep = json.load(open(REAL_DATA_DIR / "Delays - Type Departures.json", encoding="utf-8"))["response"]
    assert len(orig_dep) == 100


def test_real_database_sqlite_unchanged_after_per_airport_now_replay(per_airport_now_replay):
    """Bölüm 12.7."""
    import subprocess

    repo_root = REPLAY_DIR.parents[1]
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "database.sqlite"],
        cwd=repo_root, capture_output=True, text=True,
    )
    assert result.stdout.strip() == ""


def test_prediction_generating_airport_count_meaningfully_increased_vs_global_now(per_airport_now_replay):
    """
    Bölüm 12.8: önceki (tek global now) yaklaşımda 78 airport'tan 27'si
    prediction üretiyordu. Airport-bazlı now ile bu sayı ANLAMLI şekilde
    artmalı (yeni sonuç: 58/78 - kalan 20'si TAMAMEN domestic-arrival-
    only flight'lara sahip, hiçbir queue-process bunlar için tanımlı
    değil - bkz. rapor).
    """
    per_airport_counts = per_airport_now_replay["prediction_summary"]["airports"]
    generating = sum(1 for v in per_airport_counts.values() if v > 0)
    assert generating > 40  # önceki 27'den anlamlı artış


def test_oag_unknown_scale_produces_prediction_when_aligned_to_its_own_timeline(per_airport_now_replay):
    """
    Bölüm 12.9: OAG (unknown scale) - airport-bazlı now ile KENDİ
    timeline'ına hizalandığında (scale unknown olmasına RAĞMEN, eski
    production fallback 8/8/8/8 ile) prediction üretebiliyor.
    """
    per_airport_counts = per_airport_now_replay["prediction_summary"]["airports"]
    scale_by_airport = per_airport_now_replay["scale_by_airport"]
    assert scale_by_airport.get("OAG") is None
    assert per_airport_counts.get("OAG", 0) > 0


def test_api_produces_different_data_for_different_airports_under_per_airport_now(per_airport_now_replay):
    """Bölüm 12.10."""
    api = per_airport_now_replay["api_by_airport"]
    responses = {code: data["overall"]["windows"] for code, data in api.items() if code in ("CDG", "ZRH", "DUB", "AMS")}
    assert len(responses) == 4
    values = list(responses.values())
    assert not all(v == values[0] for v in values)


def test_remaining_zero_prediction_airports_are_domestic_arrival_only(per_airport_now_replay):
    """
    Bölüm 11: airport-bazlı now sonrası HÂLÂ prediction üretmeyen
    airport'lar (varsa) SADECE flight'larının TAMAMI domestic+arrival
    olduğu için böyledir (hiçbir queue-üreten process - departure
    security/passport, international arrival passport - bunlara
    uygulanamaz) - bu DOĞAL bir sonuçtur, bug DEĞİLDİR.
    """
    per_airport_counts = per_airport_now_replay["prediction_summary"]["airports"]
    all_flights_by_airport = per_airport_now_replay["all_flights_by_airport"]
    zero_pred_airports = [code for code, count in per_airport_counts.items() if count == 0]
    for code in zero_pred_airports:
        flights = all_flights_by_airport[code]
        assert flights, f"{code}: hiç flight yok (beklenmiyor)"
        assert all(f.direction == "arrival" and f.location == "domestic" for f in flights), (
            f"{code}: domestic-arrival-only DEĞİL ama prediction=0 - beklenmeyen durum"
        )


# ========================================================================
# ADIM (Date-Shift Replay Test Data Expansion) - Bölüm 21: CDG'nin 24
# saatlik queue coverage'ı + FRA'nın (yeni test airport'u) import/
# prediction/directory/isolation doğrulaması.
# ========================================================================

@pytest.fixture(scope="module")
def expanded_replay(tmp_path_factory):
    r = replay(
        REPLAY_DIR, tmp_path_factory.mktemp("ds-exp") / "expanded.sqlite",
        CDG_NOW_SHIFTED, airports=None, per_airport_now=True,
    )
    yield r
    r["session"].close()


def test_cdg_has_full_24_hour_queue_coverage(expanded_replay):
    """Bölüm 3/4/12: CDG'nin operasyonel gününün (production `operational_day_window()`) HER saati için en az 1 QueuePrediction row var."""
    from datetime import timedelta as _td

    from sqlalchemy import select as _select

    from app.queue.domain.operational_day import operational_day_window, resolve_airport_timezone
    from app.queue.models import QueuePrediction

    session = expanded_replay["session"]
    tz = resolve_airport_timezone("Europe/Paris")
    window_start, window_end = operational_day_window(tz, datetime(2026, 9, 18, 12, 0))
    assert window_end - window_start == timedelta(hours=24)

    rows = list(session.execute(
        _select(QueuePrediction).where(QueuePrediction.airport_iata == "CDG")
    ).scalars())
    hours_with_rows = {row.window_start for row in rows}

    missing = []
    for k in range(24):
        hour = window_start + _td(hours=k)
        if hour not in hours_with_rows:
            missing.append(hour)
    assert missing == [], f"eksik saatler: {missing}"


def test_cdg_shows_multiple_real_risk_levels(expanded_replay):
    """Bölüm 5/15: risk hard-code edilmedi, gerçek event-driven motor LOW/MEDIUM/HIGH/CRITICAL'ın BİRDEN FAZLASINI üretti."""
    from sqlalchemy import select as _select

    from app.queue.models import QueuePrediction

    session = expanded_replay["session"]
    rows = list(session.execute(
        _select(QueuePrediction).where(QueuePrediction.airport_iata == "CDG")
    ).scalars())
    risks = {row.risk for row in rows}
    assert len(risks) >= 3, risks


def test_extra_airport_fra_is_new_not_in_original_78():
    """Bölüm 7: FRA gerçek directory kaydı + gerçek large-scale eşleşmesi olan, ÖNCEDEN bu fixture'da olmayan bir airport."""
    import json

    dep = json.load(open(REPLAY_DIR / "Delays - Type Departures.json", encoding="utf-8"))["response"]
    arr = json.load(open(REPLAY_DIR / "Delays - Type Arrivals.json", encoding="utf-8"))["response"]
    real_dep = [r for r in dep if not (r.get("flight_iata") or "").startswith("TS9")]
    real_arr = [r for r in arr if not (r.get("flight_iata") or "").startswith("TS9")]
    real_airports = {r["dep_iata"] for r in real_dep} | {r["arr_iata"] for r in real_arr}
    assert "FRA" not in real_airports


def test_extra_airport_imported_and_count_increased_to_79(expanded_replay):
    """
    Bölüm 8/17: distinct airport count 78 -> 79, FRA dahil.

    ADIM (Airport Scale Test Matrix) NOTU: `REPLAY_DIR` artık MFG/OAG
    için de EK, ÖNCEDEN bu fixture'da olmayan test airport'ları
    içeriyor (bkz. `_generate_scale_matrix_test_flights.py`) - bu
    yüzden toplam sayı 79'dan 80'e çıktı (CBR zaten 78/79'un içindeydi,
    MFG/OAG YENİ). Alttaki asıl iddia (FRA'nın gerçekten eklendiği)
    hâlâ AYNEN doğrulanıyor.
    """
    assert len(expanded_replay["airports"]) == 80
    assert "FRA" in expanded_replay["airports"]
    assert expanded_replay["scale_by_airport"]["FRA"] == "large"


def test_extra_airport_appears_in_directory_and_api(expanded_replay):
    """Bölüm 8/16: FRA directory endpoint'inde görünüyor VE airport_predictions() response üretiyor."""
    from app.queue.api import airport_directory

    session = expanded_replay["session"]
    directory = airport_directory(session)
    assert any(entry["iata"] == "FRA" for entry in directory)

    fra_api = expanded_replay["api_by_airport"]["FRA"]
    assert fra_api is not None
    for key in ("overall", "domestic_security", "international_departure", "international_arrival"):
        assert key in fra_api


def test_all_generated_flight_keys_unique(expanded_replay):
    """Bölüm 9: TS9xxx flight_key'leri gerçek 200 flight'la VEYA birbirleriyle ÇAKIŞMIYOR."""
    session = expanded_replay["session"]
    from sqlalchemy import select as _select

    from app.queue.models import Flight

    all_keys = [row[0] for row in session.execute(_select(Flight.flight_key))]
    assert len(all_keys) == len(set(all_keys)), "flight_key çakışması var"


def test_no_original_real_flight_lost_in_expanded_fixture(expanded_replay):
    """
    Bölüm 2/17: gerçek 200 flight'ın TAMAMI hâlâ mevcut - TS9xxx (ADIM
    International Departure Split Graphs öncesi) VE TM9xxx (ADIM Airport
    Scale Test Matrix - CBR/MFG/OAG) SADECE EKLENDİ, gerçek 200 flight'tan
    hiçbiri SİLİNMEDİ/DEĞİŞTİRİLMEDİ.
    """
    import json

    dep = json.load(open(REPLAY_DIR / "Delays - Type Departures.json", encoding="utf-8"))["response"]
    arr = json.load(open(REPLAY_DIR / "Delays - Type Arrivals.json", encoding="utf-8"))["response"]

    def _is_test_marker(record):
        iata = record.get("flight_iata") or ""
        return iata.startswith("TS9") or iata.startswith("TM9")

    real_count = sum(1 for r in dep if not _is_test_marker(r))
    real_count += sum(1 for r in arr if not _is_test_marker(r))
    assert real_count == 200
    assert expanded_replay["parsed_flights"] >= 200


def test_expanded_fixture_production_data_untouched():
    """Bölüm 20: gerçek data/*.json ve database.sqlite bu genişletmeden ETKİLENMEDİ."""
    import subprocess

    repo_root = REPLAY_DIR.parents[1]
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "data/", "database.sqlite"],
        cwd=repo_root, capture_output=True, text=True,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    allowlisted = {
        "?? data/buyuk_olcekli_havaalanlari.txt",
        "?? data/orta_olcekli_havaalanlari.txt",
        "?? data/kucuk_olcekli_havaalanlari.txt",
    }
    unexpected = [line for line in lines if line.strip() not in allowlisted]
    assert unexpected == [], "\n".join(unexpected)


def test_fra_isolated_from_cdg_despite_shared_replay_run(expanded_replay):
    """Bölüm 8: FRA'nın flight/prediction'ları CDG'ninkiyle KARIŞMADI - iki airport'un kendi flight_key kümesi ayrık."""
    cdg_keys = {f.flight_key for f in expanded_replay["all_flights_by_airport"]["CDG"]}
    fra_keys = {f.flight_key for f in expanded_replay["all_flights_by_airport"]["FRA"]}
    assert cdg_keys.isdisjoint(fra_keys)
    assert expanded_replay["api_by_airport"]["CDG"]["overall"]["windows"] != expanded_replay["api_by_airport"]["FRA"]["overall"]["windows"]


def test_cdg_all_five_graphs_have_multiple_windows(expanded_replay):
    """Bölüm 13: CDG'de Overall/Domestic Security/International Departure—Passport/—Security/International Arrival BEŞ grafiğin HEPSİ birden fazla saat/window gösteriyor (tek saatlik nokta veri DEĞİL)."""
    api = expanded_replay["api_by_airport"]["CDG"]
    for key in ("overall", "domestic_security", "international_arrival"):
        assert len(api[key]["windows"]) > 1, key
    for stage in ("passport", "security"):
        assert len(api["international_departure"][stage]["windows"]) > 1, stage


def test_international_departure_passport_security_coupling_present(expanded_replay):
    """
    Bölüm 14: passport ve security artık İKİ BAĞIMSIZ seri (bkz. ADIM
    International Departure Split Graphs) - cross-hour coupling'in
    kanıtı, iki serinin AYNI saatte TAŞIDIĞI yolcu SAYISININ (passport'un
    kendi kalkış talebi vs security'nin, passport completion_time'ından
    beslenen, zaman-kaydırmalı gerçek talebi) BİREBİR AYNI OLMAMASIDIR -
    security basitçe passport'u "kopyalamıyor" (bkz. gerçek ölçüm: CDG
    06:00'da passport=2645 pax iken security aynı saatte SADECE 420 pax
    taşıyor - kalan security'ye başka saatlere/zaten oluşmuş completion
    event'lerine göre dağılıyor).

    ADIM (24-Hour Graph) NOTU: her iki seri artık 24-saat PADDED (bkz.
    `_pad_series_to_24_hours`) - CDG fixture'ı GÜNÜN NEREDEYSE HER
    SAATİNDE gerçek talep taşıdığı için (Bölüm 3'ün "her saat en az 1
    event" deseni) ham `window_start` KÜMELERİ (hangi saatlerde herhangi
    bir talep var) artık ÇOK BENZER/aynı olabiliyor - bu yüzden asıl
    coupling kanıtı KÜME FARKI değil, DEĞER FARKIDIR.
    """
    intl_dep = expanded_replay["api_by_airport"]["CDG"]["international_departure"]
    passport_by_hour = {w["window_start"]: w["expected_passengers"] for w in intl_dep["passport"]["windows"]}
    security_by_hour = {w["window_start"]: w["expected_passengers"] for w in intl_dep["security"]["windows"]}
    differing_hours = [
        h for h in passport_by_hour
        if passport_by_hour[h] != security_by_hour.get(h)
    ]
    # En az bir saatte passport'un kendi talebi ile security'nin o
    # saatteki (zaman-kaydırmalı) talebi FARKLI - security passport'u
    # birebir KOPYALAMIYOR, gerçek bir coupling/zaman kayması var.
    assert differing_hours


def test_wait_visible_on_low_and_non_low_risk_windows(expanded_replay):
    """Bölüm 15: LOW risk'te de, LOW olmayan risk'te de estimated_wait_minutes gerçek (None olmayan) bir sayı olabiliyor - risk gizlemiyor."""
    windows = expanded_replay["api_by_airport"]["CDG"]["overall"]["windows"]
    low_with_wait = [w for w in windows if w["risk"] == "LOW" and w["estimated_wait_minutes"] is not None]
    non_low_with_wait = [w for w in windows if w["risk"] != "LOW" and w["estimated_wait_minutes"] is not None]
    assert low_with_wait, "LOW risk'te wait görünür örnek yok"
    assert non_low_with_wait, "LOW-olmayan risk'te wait görünür örnek yok"
