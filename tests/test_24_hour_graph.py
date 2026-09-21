"""
ADIM (24-Hour Graph) - Section A-E/Z doğrulaması.

Beş görünür grafiğin (Overall/Domestic Security/International
Departure — Passport/— Security/International Arrival) HER ZAMAN TAM
24 saat (00..23) gösterdiğini, boş saatlerin SIFIR-TALEP (uydurma
DEĞİL) olarak korunduğunu, "hesaplanamadı" (null) ile "talep yok"
(0) ayrımını, current'ın havalimanının KENDİ yerel saatine göre
seçildiğini ve cross-midnight event'lerin YANLIŞ güne taşınmadığını
kanıtlar.
"""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.domain.operational_day import operational_day_window, resolve_airport_timezone
from app.queue.models import Airport, QueuePrediction

# IST = Europe/Istanbul (UTC+3, Eylül'de DST yok) - havalimanının YEREL
# operasyonel günü (00:00-24:00 yerel), UTC'ye çevrildiğinde takvim
# günüyle HİZALI DEĞİLDİR (Bölüm A - "UTC 00:00-23:00" DEĞİL, havalimanının
# KENDİ yerel gününün 24 saati) - bu yüzden sabit bir UTC saat kümesi
# yazmak yerine PRODUCTION'IN KENDİ `operational_day_window()`'u
# çağrılır (uydurma bir varsayım YOK, testin kendisi de production
# ile AYNI kaynağı kullanır).
_IST_TZ = resolve_airport_timezone("Europe/Istanbul")
_IST_DAY_START, _ = operational_day_window(_IST_TZ, datetime(2026, 9, 19, 14, 37))
EXPECTED_HOURS = {(_IST_DAY_START + timedelta(hours=k)).isoformat() for k in range(24)}


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def _hours(windows):
    return {w["window_start"] for w in windows}


# ------------------------------------------------------------------
# Gerçek incoming DB (IST) - tam gün gerçek 5 grafik.
# ------------------------------------------------------------------

@pytest.fixture(scope="module")
def ist_incoming_api():
    from pathlib import Path

    from tests.date_shift_replay.run_date_shift_replay import REPLAY_DIR, replay

    incoming_dir = Path(__file__).resolve().parent / "incoming_2026_09_19"
    db_path = incoming_dir / "incoming_2026_09_19.sqlite"
    # Bu ADIM'ın kendi test dosyasının (test_new_daily_incoming_ist_saw.py)
    # zaten kurduğu/bıraktığı DB varsa TEKRAR KURMAYA gerek yok (Bölüm 28 -
    # DB test sonunda silinmiyor); yoksa (bu dosya tek başına çalıştırıldıysa)
    # aynı iki-aşamalı replay'i burada da kur.
    if not db_path.exists():
        replay(REPLAY_DIR, db_path, datetime(2026, 9, 18, 20, 0), airports=None, reset_db=True, per_airport_now=True)
        replay(incoming_dir, db_path, datetime(2026, 9, 19, 20, 0), airports=None, reset_db=False, per_airport_now=False)

    engine = create_engine(f"sqlite:///{db_path}")
    session = sessionmaker(bind=engine)()
    api = airport_predictions(session, "IST", now=datetime(2026, 9, 19, 14, 37))
    session.close()
    return api


@pytest.mark.parametrize("graph_path", [
    ("overall",),
    ("domestic_security",),
    ("international_departure", "passport"),
    ("international_departure", "security"),
    ("international_arrival",),
])
def test_all_five_visible_graphs_have_exactly_24_hours(ist_incoming_api, graph_path):
    """
    19 Eylül'ün TAM 24 saati HER ZAMAN mevcut olmalı (>=24 - additive
    padding, bkz. `_pad_series_to_24_hours` docstring'i). Gerçek IST
    fixture'ı bazı süreçlerde 18 Eylül'e taşan GERÇEK cross-midnight
    event'ler de taşıyabilir (Bölüm D - bu satırlar SİLİNMEZ) - bu
    yüzden `len(windows) >= 24` ve "19 Eylül'ün 24 saati bir ALT KÜME
    olarak İÇERİLİYOR" kontrol edilir, tam eşitlik DEĞİL.
    """
    node = ist_incoming_api
    for key in graph_path:
        node = node[key]
    windows = node["windows"]
    assert len(windows) >= 24, graph_path
    assert EXPECTED_HOURS <= _hours(windows), graph_path


def test_zero_demand_hour_has_real_numeric_zero_not_missing(ist_incoming_api):
    """
    ADIM (Departure Show-Up Profile): `flight_count==0` ARTIK "bu saatte
    hiç talep yok" ANLAMINA GELMİYOR (DEĞİŞTİ) - departure show-up
    profili komşu bir saatin flight'ından bu saate GERÇEK bir talep payı
    taşıyabilir (flight_count HÂLÂ tek effective_time() noktasında
    toplanıyor, expected_passengers ARTIK show-up'a göre). Bu testin
    ASIL iddiası ("gerçek sıfır-talep saat null DEĞİL, gerçek sayısal
    sıfır taşır") DEĞİŞMEDİ - SADECE filtre `expected_passengers==0`
    (GERÇEKTEN sıfır-talep) üzerinden yapılıyor, `flight_count==0`
    (artık zayıf bir proxy) DEĞİL.
    """
    windows = ist_incoming_api["domestic_security"]["windows"]
    zero_hours = [w for w in windows if w["expected_passengers"] == 0]
    assert zero_hours   # IST günün her saatinde gerçek talep taşımıyor - en az 1 sıfır-saat var
    for w in zero_hours:
        assert w["estimated_wait_minutes"] == 0.0   # null DEĞİL - gerçek sayısal sıfır
        assert w["risk"] == "LOW"
        assert w["expected_passengers"] == 0


def test_current_selected_by_airport_local_hour_not_utc(ist_incoming_api):
    """`now`=2026-09-19 14:37 UTC; IST=Europe/Istanbul (UTC+3) -> yerel 17:37 -> current bucket 17:00 UTC+3 = 14:00 UTC olmalı."""
    current = ist_incoming_api["domestic_security"]["current"]
    assert current is not None
    assert current["window_start"] == "2026-09-19T14:00:00"


def test_passport_and_security_have_independent_current_hours(ist_incoming_api):
    intl_dep = ist_incoming_api["international_departure"]
    p_current = intl_dep["passport"]["current"]
    s_current = intl_dep["security"]["current"]
    assert p_current is not None and s_current is not None
    # İkisi de AYNI (now'ı kapsayan) saat olmalı BURADA (her ikisi de
    # 24-saat padded, "now" aynı) - asıl bağımsızlık garantisi (biri
    # diğerini EZMEZ) `test_international_departure_split_graphs.py`'de
    # farklı gerçek pencerelerle ayrıca kanıtlanmıştır.
    assert p_current["window_start"] == "2026-09-19T14:00:00"
    assert s_current["window_start"] == "2026-09-19T14:00:00"


# ------------------------------------------------------------------
# Missing/error (null) ile gerçek sıfır-talep (0) ayrımı - timezone
# çözülemeyen bir havalimanı için padding UYGULANMAZ (Bölüm B).
# ------------------------------------------------------------------

def test_unresolvable_timezone_airport_is_not_padded_stays_raw(session):
    """Airport.timezone=None (veya zoneinfo'da tanınmıyor) ise 24-saat padding UYGULANMAZ - mevcut (ham) davranış korunur, uydurma bir gün sınırı İCAT EDİLMEZ."""
    session.add(Airport(iata_code="ZZZ", airport_name="Test Unknown TZ Airport", timezone=None))
    session.add(QueuePrediction(
        airport_iata="ZZZ", process="security_dom",
        window_start=datetime(2026, 9, 19, 8, 0), window_end=datetime(2026, 9, 19, 9, 0),
        flight_count=3, expected_passengers=300, utilization=0.3,
        estimated_wait_minutes=2.0, risk="LOW", reasons=json.dumps([]), confidence=0.7,
    ))
    session.commit()

    api = airport_predictions(session, "ZZZ", now=datetime(2026, 9, 19, 8, 30))
    windows = api["domestic_security"]["windows"]
    assert len(windows) == 1   # PAD EDİLMEDİ - sadece gerçek satır
    assert windows[0]["flight_count"] == 3


# ------------------------------------------------------------------
# Cross-midnight event - flight'ın OWN operasyonel günü ile queue
# event timestamp'i farklı takvim gününe düşebilir; padding bunu
# YANLIŞ güne TAŞIMAMALI (Bölüm D).
# ------------------------------------------------------------------

def test_cross_midnight_departure_queue_event_stays_on_its_real_hour_not_shifted(session):
    """
    19 Eylül 00:45 departure -> effective_time (-120dk) = 18 Eylül
    22:45. Bu satır GERÇEKTEN 18 Eylül'ün 22:00 bucket'ına düşer.

    ADIM (Exact 24-Bucket Visible Graph) - Bölüm 3.3: CALCULATION STATE
    != VISIBLE GRAPH RANGE. Bu satır ham veritabanında (`calculation
    state`) HİÇ KAYBOLMAZ/kaydırılmaz - hâlâ KENDİ doğru saatinde
    (18 Eylül 22:00) durur. Ama 19 Eylül'ün GÖRÜNÜR grafiği artık KESİN
    `[day_start, day_end)` ile sınırlı (önceki turdaki ±120dk marj bu
    turda kaldırıldı: "24 + legitimate overflow KABUL EDİLMEZ") - bu
    yüzden bu satır `windows` listesine (19 Eylül'ün görünür penceresi)
    ARTIK GİRMEZ, SADECE ham DB sorgusunda görünür.
    """
    session.add(Airport(iata_code="YYY", airport_name="Test Cross Midnight", timezone="UTC"))
    session.add(QueuePrediction(
        airport_iata="YYY", process="security_dom",
        window_start=datetime(2026, 9, 18, 22, 0), window_end=datetime(2026, 9, 18, 23, 0),
        flight_count=1, expected_passengers=150, utilization=0.2,
        estimated_wait_minutes=1.5, risk="LOW", reasons=json.dumps([]), confidence=0.7,
    ))
    session.commit()

    # Ham DB'de (calculation state) satır hâlâ KENDİ doğru saatinde durur.
    raw = session.query(QueuePrediction).filter_by(
        airport_iata="YYY", window_start=datetime(2026, 9, 18, 22, 0),
    ).one()
    assert raw.flight_count == 1
    assert raw.expected_passengers == 150

    # "now" 19 Eylül içinde - bu havalimanının 24 saatlik padded görünümü BUGÜN (19 Eylül).
    api = airport_predictions(session, "YYY", now=datetime(2026, 9, 19, 10, 0))
    windows = api["domestic_security"]["windows"]
    hours = _hours(windows)

    # 19 Eylül'ün TAM 24 saati (bu havalimanı UTC olduğu için takvim
    # günüyle hizalı) var - NE FAZLA NE EKSİK.
    expected_utc_day_hours = {f"2026-09-19T{h:02d}:00:00" for h in range(24)}
    assert hours == expected_utc_day_hours
    assert len(windows) == 24

    # 18 Eylül 22:00'daki spillover satırı GÖRÜNÜR grafikte YOK -
    # exact-boundary contract'a göre dışlandı.
    assert not any(w["window_start"] == "2026-09-18T22:00:00" for w in windows)
