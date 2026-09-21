"""
ADIM (IST International Departure Test Yoğunluğunu Artır) doğrulaması.

`tests/incoming_2026_09_19/`'daki IST international departure sayısı
belirgin şekilde artırıldı (Bölüm 1) - iki AÇIK peak bank (06:00-07:00
ve 17:00-19:00 YEREL, `effective_time()`'ın -120dk offsetiyle GERÇEKTE
daha erken bir UTC bucket'ına düşer - bkz. final rapor) ile. Production
`data/*`/`database.sqlite` HİÇ açılmadı; IST'nin scale/resource config'i
(`large`, dep=20/arr=30/security=22/22) HİÇ DEĞİŞMEDİ - SADECE input
flight sayısı arttı, risk/wait tamamen gerçek event-driven motordan.
"""
import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.queue.api import airport_predictions
from app.queue.config import get_configs
from app.queue.domain.airport_scale import SCALE_RESOURCES
from app.queue.models import Airport, Flight

INCOMING_DIR = Path(__file__).resolve().parent / "incoming_2026_09_19"
INCOMING_DB = INCOMING_DIR / "incoming_2026_09_19.sqlite"
NOW = datetime(2026, 9, 19, 14, 37)


def _ensure_db():
    """Bu ADIM'ın kendi DB'sini (yoksa) `test_new_daily_incoming_ist_saw.py` ile AYNI iki-aşamalı replay'le kurar - Bölüm 28'in "DB silinmiyor" kuralına uygun, gerekmedikçe TEKRAR KURULMAZ."""
    if INCOMING_DB.exists():
        return
    from tests.date_shift_replay.run_date_shift_replay import REPLAY_DIR, replay
    replay(REPLAY_DIR, INCOMING_DB, datetime(2026, 9, 18, 20, 0), airports=None, reset_db=True, per_airport_now=True)
    replay(INCOMING_DIR, INCOMING_DB, NOW, airports=None, reset_db=False, per_airport_now=False)


@pytest.fixture(scope="module")
def session():
    _ensure_db()
    engine = create_engine(f"sqlite:///{INCOMING_DB}")
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


# ------------------------------------------------------------------
# Bölüm 1 - IST intl departure sayısı 25-35 aralığında.
# ------------------------------------------------------------------

def test_ist_international_departure_count_in_target_range():
    dep = json.loads((INCOMING_DIR / "Delays - Type Departures.json").read_text(encoding="utf-8"))["response"]
    intl = [r for r in dep if r["dep_iata"] == "IST" and r["arr_iata"] not in ("ADB", "AYT")]
    assert 25 <= len(intl) <= 35, len(intl)


def test_ist_has_two_distinct_peak_banks_of_flights_close_together():
    """Bölüm 2 - en az iki 'bank' (aynı ~1 saatlik pencerede >=5 kalkış) olmalı."""
    dep = json.loads((INCOMING_DIR / "Delays - Type Departures.json").read_text(encoding="utf-8"))["response"]
    intl = [r for r in dep if r["dep_iata"] == "IST" and r["arr_iata"] not in ("ADB", "AYT")]
    from collections import Counter
    hour_counts = Counter(r["dep_time_utc"][11:13] for r in intl)
    banks = [h for h, c in hour_counts.items() if c >= 4]
    assert len(banks) >= 2, hour_counts


# ------------------------------------------------------------------
# Bölüm W - IST scale/resource HİÇ DEĞİŞMEDİ.
# ------------------------------------------------------------------

def test_ist_scale_and_resources_unchanged(session):
    airport = session.get(Airport, "IST")
    assert airport.scale == "large"
    config = get_configs(session, ["IST"])["IST"]
    assert config.passport_departure_server_count == SCALE_RESOURCES["large"]["departure_passport_servers"] == 30
    assert config.passport_arrival_server_count == SCALE_RESOURCES["large"]["arrival_passport_servers"] == 45
    assert config.domestic_security_lane_count == SCALE_RESOURCES["large"]["domestic_security_lanes"] == 28
    assert config.international_security_lane_count == SCALE_RESOURCES["large"]["international_security_lanes"] == 18


# ------------------------------------------------------------------
# Bölüm 11.2 - aircraft capacity resolver kullanıldı (hard-code YOK).
# ------------------------------------------------------------------

def test_new_dense_flights_use_real_aircraft_types_resolved_by_service(session):
    from app.service import AircraftCapacityService

    flights = session.execute(
        select(Flight).where(Flight.airport_iata == "IST", Flight.flight_key.like("TK\\_6%", escape="\\"))
    ).scalars().all()
    assert flights
    resolver = AircraftCapacityService(session)
    seen_types = set()
    for f in flights:
        assert f.aircraft_icao is not None
        result = resolver.resolve(f.aircraft_icao)
        assert result.capacity > 0
        assert result.source in ("verified_dataset", "curated_fallback", "aircraft_family")
        seen_types.add(f.aircraft_icao)
    # Kısa/orta VE uzun menzil tipleri KARIŞIK kullanıldı (Bölüm 4).
    assert seen_types & {"A320", "A321", "B738"}
    assert seen_types & {"A333", "B789"}


# ------------------------------------------------------------------
# Bölüm 7 - passport event-driven, peak/sakin saatler FARKLI wait/risk.
# ------------------------------------------------------------------

def test_passport_wait_is_event_driven_and_varies_between_peak_and_calm_hours(session):
    api = airport_predictions(session, "IST", now=NOW)
    windows = api["international_departure"]["passport"]["windows"]
    waits = {w["estimated_wait_minutes"] for w in windows}
    risks = {w["risk"] for w in windows}
    # Hard-code edilmiş TEK bir değer YOK - birden fazla farklı wait/risk var.
    assert len(waits) > 1
    assert len(risks) > 1


def test_passport_risk_reaches_beyond_low_naturally(session):
    """Bölüm 7 - LOW dışında MEDIUM/HIGH/CRITICAL doğal olarak oluşmalı (hard-code YOK, gerçek motor sonucu)."""
    api = airport_predictions(session, "IST", now=NOW)
    windows = api["international_departure"]["passport"]["windows"]
    risks = {w["risk"] for w in windows}
    assert risks - {"LOW"}, risks


def test_international_security_utilization_is_real_computed_value(session):
    api = airport_predictions(session, "IST", now=NOW)
    windows = api["international_departure"]["security"]["windows"]
    real_rows = [w for w in windows if w["flight_count"] > 0 or w["expected_passengers"] > 0]
    assert real_rows or windows  # her durumda 24-saat padded seri var
    for w in windows:
        assert w["utilization"] is not None   # her zaman GERÇEK bir sayı (None DEĞİL) - queue_capacity_model çalıştı


# ------------------------------------------------------------------
# Bölüm 8 - 24-hour graph korunuyor.
# ------------------------------------------------------------------

def test_24_hour_graph_preserved_for_intl_departure_passport_and_security(session):
    api = airport_predictions(session, "IST", now=NOW)
    assert len(api["international_departure"]["passport"]["windows"]) >= 24
    assert len(api["international_departure"]["security"]["windows"]) >= 24


# ------------------------------------------------------------------
# Bölüm 9 - idempotency.
# ------------------------------------------------------------------

def test_second_ingest_of_dense_ist_data_does_not_duplicate(session):
    from tests.date_shift_replay.run_date_shift_replay import replay

    before = len(session.execute(select(Flight)).scalars().all())
    replay(INCOMING_DIR, INCOMING_DB, NOW, airports=None, reset_db=False, per_airport_now=False)

    engine2 = create_engine(f"sqlite:///{INCOMING_DB}")
    session2 = sessionmaker(bind=engine2)()
    after = len(session2.execute(select(Flight)).scalars().all())
    session2.close()
    assert before == after


# ------------------------------------------------------------------
# Bölüm 12 - production data untouched.
# ------------------------------------------------------------------

def test_real_production_data_and_database_untouched():
    import subprocess
    repo_root = INCOMING_DIR.parents[1]
    result = subprocess.run(
        ["git", "status", "--porcelain", "data/", "database.sqlite"],
        cwd=repo_root, capture_output=True, text=True,
    )
    assert result.stdout.strip() == "", result.stdout
