"""
ADIM (Visible Risk = Gerçek Queue Pressure) - user-visible queue risk
modelini gerçek event-driven backlog ile düzeltme dogrulama testleri.

ESKİ model: risk = f(rho) = f(incoming_demand / capacity) - SADECE bu
saatin KENDİ yeni gelen talebine bakıyordu, önceki saatlerden taşınan
GERÇEK backlog'u görmezden geliyordu (bkz. onceki ADIM'in analiz
raporu - CBR ornegi: MEDIUM/261dk yaninda CRITICAL/36dk).

YENİ model: risk = f(queue_pressure), queue_pressure =
    (risk_backlog_start + arrivals_in_window) / hour_capacity
- risk_backlog_start GERÇEK, ServiceEvent-türevli (`_event_derived_
backlog_by_hour()`) bekleyen-passenger sayısı; ne fluid `_hourly_
backlog_chain()` yaklaşıklığı, ne YENİ bir simülasyon.

DEĞİŞMEYENLER (bu dosyada ayrıca doğrulanır):
  - `estimated_wait_minutes` hesaplama zinciri (ServiceEvent.wait_minutes
    -> passenger-ağırlıklı saatlik ortalama) HİÇ DOKUNULMADI.
  - `event_queue.py`'nin simülasyon çekirdeği (`simulate_fifo_queue`)
    HİÇ DEĞİŞMEDİ.
  - Legacy `PROCESS_PASSPORT`/`PROCESS_SECURITY` (birleşik) risk'i
    ESKİ, sadece-rho davranışını korur (visible 5 grafiğe hiç
    girmiyorlar).
  - `utilization` API alanı (incoming-only rho, diagnostic) DEĞİŞMEDİ.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.queue.core.event_queue import ServiceEvent
from app.queue.core.scoring import queue_capacity_model
from app.queue.engine import _event_derived_backlog_by_hour, run_predictions
from app.queue.api import airport_predictions
from app.queue.constants import RISK_CRITICAL, RISK_HIGH, RISK_LOW, RISK_MEDIUM, RISK_ORDER

from .factories import MockCapacityResolver, at


# ========================================================================
# Section 10 - MATEMATİKSEL INVARIANT TESTLERİ (A-H)
# ========================================================================
# server_count=1, service_time_minutes=1.0, window_minutes=60 ->
# hour_capacity = 1 * (1/1.0) * 60 = 60 kişi/saat - "capacity" = 60.

CAPACITY = 60.0


def _pressure(risk_backlog_start: float, arrivals: float) -> dict:
    return queue_capacity_model(
        [], lambda f: 0, server_count=1, service_time_minutes=1.0, window_minutes=60,
        demand_override=arrivals, risk_backlog_start=risk_backlog_start,
    )


def test_a_zero_backlog_zero_arrivals_pressure_zero():
    r = _pressure(0.0, 0.0)
    assert r["queue_pressure"] == 0.0
    assert r["risk"] == RISK_LOW
    assert r["estimated_wait_minutes"] == 0.0


def test_b_zero_backlog_arrivals_equal_capacity_pressure_one():
    r = _pressure(0.0, CAPACITY)
    assert r["queue_pressure"] == pytest.approx(1.0)
    assert r["risk"] == RISK_CRITICAL


def test_c_backlog_equal_capacity_zero_arrivals_pressure_one():
    r = _pressure(CAPACITY, 0.0)
    assert r["queue_pressure"] == pytest.approx(1.0)
    assert r["risk"] == RISK_CRITICAL


def test_d_zero_backlog_half_capacity_arrivals_pressure_half():
    r = _pressure(0.0, 0.5 * CAPACITY)
    assert r["queue_pressure"] == pytest.approx(0.5)
    # 0.5 < PASSPORT_RHO_LOW(0.7) -> LOW olmali.
    assert r["risk"] == RISK_LOW


def test_e_pressure_never_decreases_as_backlog_increases():
    base = _pressure(10.0, 20.0)["queue_pressure"]
    more_backlog = _pressure(30.0, 20.0)["queue_pressure"]
    assert more_backlog >= base


def test_f_pressure_never_decreases_as_arrivals_increase():
    base = _pressure(10.0, 20.0)["queue_pressure"]
    more_arrivals = _pressure(10.0, 40.0)["queue_pressure"]
    assert more_arrivals >= base


def test_g_pressure_never_increases_as_capacity_increases():
    low_capacity = queue_capacity_model(
        [], lambda f: 0, server_count=1, service_time_minutes=1.0, window_minutes=60,
        demand_override=20.0, risk_backlog_start=10.0,
    )["queue_pressure"]
    high_capacity = queue_capacity_model(
        [], lambda f: 0, server_count=2, service_time_minutes=1.0, window_minutes=60,
        demand_override=20.0, risk_backlog_start=10.0,
    )["queue_pressure"]
    assert high_capacity <= low_capacity


def test_h_double_count_guard_at_boundary():
    """
    Bir birim (arrival_time == T) hem `arrivals_in_window` hem
    `risk_backlog_start`'a AYNI ANDA giremez - `_event_derived_backlog_
    by_hour`'un `arrival_time < T` (KESIN kucuk) siniri bunu garanti
    eder. Burada, T'nin TAM UZERINE denk gelen bir event'in backlog'a
    GIRMEDIGINI dogrudan test ediyoruz.
    """
    T = at(10, 0)
    events = [
        ServiceEvent(
            origin="x", count=5.0,
            arrival_time=T, service_start_time=T + timedelta(minutes=30),
            completion_time=T + timedelta(minutes=31),
        ),
    ]
    backlog = _event_derived_backlog_by_hour(events, [T])
    assert backlog[T] == 0.0, "arrival_time == T olan birim yanlislikla backlog'a girdi (double-count riski)"


# ========================================================================
# Section 2/3 - BACKLOG TANIMI DOĞRULAMASI (event-türevli, in-service dışlama)
# ========================================================================

def test_backlog_excludes_units_still_waiting_but_counts_correctly():
    """arrival_time < T ve service_start_time > T olan birim GERCEKTEN backlog'a girmeli."""
    T = at(10, 0)
    events = [
        ServiceEvent(
            origin="x", count=3.0,
            arrival_time=T - timedelta(minutes=20),
            service_start_time=T + timedelta(minutes=5),   # T'de hala bekliyor
            completion_time=T + timedelta(minutes=6),
        ),
    ]
    backlog = _event_derived_backlog_by_hour(events, [T])
    assert backlog[T] == 3.0


def test_backlog_excludes_in_service_passengers_at_boundary():
    """
    service_start_time <= T < completion_time olan (T aninda ZATEN
    servis alan) birim backlog'a DAHIL EDILMEMELI (Bolum 3 - residual
    workload modellemesi bilincli olarak yapilmadi, rapor gerekcesi).
    """
    T = at(10, 0)
    events = [
        ServiceEvent(
            origin="x", count=7.0,
            arrival_time=T - timedelta(minutes=10),
            service_start_time=T - timedelta(minutes=1),   # servis T'den ONCE basladi
            completion_time=T + timedelta(minutes=1),        # T'yi biraz asiyor
        ),
    ]
    backlog = _event_derived_backlog_by_hour(events, [T])
    assert backlog[T] == 0.0


def test_backlog_excludes_units_already_completed_before_boundary():
    T = at(10, 0)
    events = [
        ServiceEvent(
            origin="x", count=4.0,
            arrival_time=T - timedelta(minutes=30),
            service_start_time=T - timedelta(minutes=20),
            completion_time=T - timedelta(minutes=19),   # T'den once tamamen bitti
        ),
    ]
    backlog = _event_derived_backlog_by_hour(events, [T])
    assert backlog[T] == 0.0


def test_backlog_excludes_future_arrivals():
    """arrival_time >= T olan (henuz gelmemis VEYA bu pencerenin KENDI
    arrival'i olan) birim backlog'a asla girmemeli."""
    T = at(10, 0)
    events = [
        ServiceEvent(
            origin="x", count=9.0,
            arrival_time=T,  # aynen T - bu pencerenin KENDI arrival'i
            service_start_time=T + timedelta(minutes=2),
            completion_time=T + timedelta(minutes=3),
        ),
    ]
    backlog = _event_derived_backlog_by_hour(events, [T])
    assert backlog[T] == 0.0


def test_no_double_count_across_multiple_hours():
    """
    Bir birim, arrival'indan completion'ina kadar GECTIGI HER saat
    sinirinda EN FAZLA backlog VEYA arrival olarak sayilir, ikisi
    ASLA AYNI ANDA degil - farkli saat sinirlarinda FARKLI rollerde
    (arrival sonra backlog) gorunmesi normal/beklenen, ama HERHANGI
    BIR TEK sinirda cift sayim YOK.
    """
    hour0 = at(10, 0)
    hour1 = at(11, 0)
    events = [
        ServiceEvent(
            origin="x", count=6.0,
            arrival_time=hour0 + timedelta(minutes=50),   # hour0'in KENDI arrival'i
            service_start_time=hour1 + timedelta(minutes=5),  # hour1'e tasan bekleme
            completion_time=hour1 + timedelta(minutes=6),
        ),
    ]
    backlog = _event_derived_backlog_by_hour(events, [hour0, hour1])
    assert backlog[hour0] == 0.0   # hour0 sinirinda bu birim henuz gelmedi (arrival hour0+50dk, sinirin KENDISI degil)
    assert backlog[hour1] == 6.0   # hour1 sinirinda: arrival(hour0+50) < hour1 VE service_start(hour1+5) > hour1 -> backlog


# ========================================================================
# Section 8/9 - QUEUE BUILD-UP / RECOVERY DAVRANIŞI (senaryo, direkt fonksiyon)
# ========================================================================

def test_queue_recovery_pressure_decreases_as_backlog_drains_not_sticky():
    """
    Onceki saatlerde yogunluk var, backlog birikti. Sonraki saatlerde
    arrivals azaliyor VE backlog gercekten eriyor (kod DEGISTIRILMEDEN,
    SADECE risk_backlog_start senaryosal olarak azaltiliyor - gercek
    _event_derived_backlog_by_hour zaten byle bir azalmayi ServiceEvent
    listesinden dogal olarak uretir, bkz. asagidaki end-to-end regresyon
    testi). Risk SONSUZA kadar CRITICAL kalmamali - "bir kere kirmizi,
    hep kirmizi" gibi sticky bir davranis OLMAMALI.
    """
    server_count, service_time = 2, 1.0  # capacity = 2*60=120/saat
    # Saat 1: buyuk backlog + yuksek arrivals -> CRITICAL
    r1 = queue_capacity_model(
        [], lambda f: 0, server_count, service_time, window_minutes=60,
        demand_override=100.0, risk_backlog_start=100.0,
    )
    # Saat 2: backlog kismen eridi (60'a dustu), arrivals da dustu (20)
    r2 = queue_capacity_model(
        [], lambda f: 0, server_count, service_time, window_minutes=60,
        demand_override=20.0, risk_backlog_start=60.0,
    )
    # Saat 3: backlog tamamen eridi (0), arrivals dusuk (10)
    r3 = queue_capacity_model(
        [], lambda f: 0, server_count, service_time, window_minutes=60,
        demand_override=10.0, risk_backlog_start=0.0,
    )
    assert r1["risk"] == RISK_CRITICAL
    assert RISK_ORDER[r2["risk"]] < RISK_ORDER[r1["risk"]], "backlog eridikce risk azalmadi (sticky davranis suphesi)"
    assert r3["risk"] == RISK_LOW
    assert r1["queue_pressure"] > r2["queue_pressure"] > r3["queue_pressure"]


def test_queue_buildup_high_arrivals_still_raise_pressure_even_with_low_backlog():
    """
    Yeni yogunluk basladiginda backlog_start DUSUK ama arrivals YUKSEK
    ise pressure yine yukselebilmeli - backlog eklenmesi mevcut rho'nun
    ERKEN congestion sinyalini ORTADAN KALDIRMAMALI (Bolum 9).
    """
    r = queue_capacity_model(
        [], lambda f: 0, server_count=1, service_time_minutes=1.0, window_minutes=60,
        demand_override=60.0, risk_backlog_start=0.0,
    )
    assert r["risk"] == RISK_CRITICAL
    assert r["queue_pressure"] == pytest.approx(1.0)


# ========================================================================
# Section 11 - GERÇEK PROBLEMİ REGRESSION TEST (CBR, gerçek fixture)
# ========================================================================

@pytest.fixture()
def cbr_style_session():
    from app.models import Base
    from app.queue.ingestion.refresh import refresh_flights
    from app.queue.models import Airport

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Airport(iata_code="AAA", airport_name="Test", timezone="UTC"))
    session.commit()
    return session


def _intl_departure_row(flight_key: str, dep_scheduled: datetime, capacity_hint: str = "A320") -> dict:
    return {
        "flight_key": flight_key, "airport_iata": "AAA",
        "direction": "departure", "location": "international",
        "airline_iata": "XX", "flight_number": flight_key, "flight_iata": f"XX{flight_key}",
        "aircraft_icao": capacity_hint, "aircraft_match_found": True,
        "dep_iata": "AAA", "arr_iata": "ZZZ",
        "dep_scheduled_utc": dep_scheduled, "dep_estimated_utc": None, "dep_actual_utc": None,
        "arr_scheduled_utc": dep_scheduled + timedelta(hours=3), "arr_estimated_utc": None, "arr_actual_utc": None,
        "status": "scheduled",
    }


def test_large_real_backlog_hour_is_not_artificially_downgraded_when_its_own_arrivals_drop(cbr_style_session):
    """
    CBR ornegindeki (onceki ADIM'in analiz raporu) gercek desen: bir
    saatin KENDI arrivals'i dusuk olsa bile, ONCEKI saatlerin devasa
    backlog'u hala oradaysa, o saat YAPAY OLARAK dusuk risk ALMAMALI.
    Simule edilen senaryo: saat 04:00'te 20 ucus (buyuk dalga, backlog
    yaratir), saat 07:00'te SADECE 1 ucus (dusuk KENDI arrival) ama
    backlog henuz erimemis olmali -> 07:00 ESKI modelde MEDIUM/dusuk-
    risk cikardi (sadece kendi arrival'ina bakti), YENI modelde backlog
    hala orada oldugu icin risk asla dusuk cikmamali.
    """
    session = cbr_style_session
    from app.queue.ingestion.refresh import refresh_flights

    rows = []
    # 04:00 - buyuk dalga (20 ucus, A333=295 koltuk -> ~5900 yolcu potansiyeli,
    # kucuk kapasiteli bir security/passport havuzunu agir bicimde asar).
    for i in range(20):
        rows.append(_intl_departure_row(f"D04-{i}", at(4, i % 60 // 3), capacity_hint="A333"))
    # 07:00 - SADECE 1 ucus (kucuk, kendi arrival'i dusuk).
    rows.append(_intl_departure_row("D07-0", at(7, 0), capacity_hint="A320"))

    refresh_flights(session, rows)
    resolver = MockCapacityResolver(capacities={"A333": 295, "A320": 180})

    from app.queue.config import get_configs
    configs = get_configs(session, ["AAA"])
    # Kucuk kapasiteli, GERCEKCI bir kucuk havalimani senaryosu (SMALL scale
    # benzeri) - kucuk sunucu sayisi backlog'un birikmesini SAGLAR.
    configs["AAA"].passport_departure_server_count = 2
    configs["AAA"].international_security_lane_count = 2

    now = at(8, 0)
    run_predictions(session, resolver, airports=["AAA"], now=now)

    api = airport_predictions(session, "AAA", now=now)
    windows = {w["window_start"]: w for w in api["international_departure"]["passport"]["windows"]}

    w04 = windows.get(at(4, 0).isoformat())
    w07 = windows.get(at(7, 0).isoformat())
    assert w04 is not None and w07 is not None

    # 07:00'in KENDI arrival'i (1 ucus) 04:00'in (20 ucus) COK altinda,
    # ama backlog devam ettigi icin risk asla 04:00'den DAHA IYI (daha
    # dusuk severity) OLMAMALI - eski modelin tam ustune dustugu hata.
    assert RISK_ORDER[w07["risk"]] >= RISK_ORDER[w04["risk"]] or w07["risk"] in (RISK_HIGH, RISK_CRITICAL), (
        f"07:00 (dusuk kendi-arrival) yapay olarak dusuk risk aldi: {w07['risk']} "
        f"(04:00 buyuk dalga sonrasi hala CRITICAL/HIGH olmali)"
    )


def test_no_lower_risk_higher_wait_anomalies_across_real_cbr_fixture():
    """
    Gercek, persistan test DB'sindeki (tests/incoming_2026_09_19/
    incoming_2026_09_19.sqlite) CBR/IST/MFG/OAG icin, onceki ADIM'in
    bulgusu olan "62 lower-risk/higher-wait anomaly pair" YENI modelde
    SIFIRA dusmus olmali.
    """
    from pathlib import Path

    db_path = Path(__file__).resolve().parent / "incoming_2026_09_19" / "incoming_2026_09_19.sqlite"
    if not db_path.exists():
        pytest.skip("persistent test DB bu ortamda henuz kurulmadi")

    engine = create_engine(f"sqlite:///{db_path}")
    session = sessionmaker(bind=engine)()
    resolver = _real_resolver(session)
    now = datetime(2026, 9, 20, 10, 0)
    run_predictions(session, resolver, airports=["IST", "CBR", "MFG", "OAG"], now=now)

    processes = [
        ("domestic_security", lambda api: api["domestic_security"]),
        ("international_departure.passport", lambda api: api["international_departure"]["passport"]),
        ("international_departure.security", lambda api: api["international_departure"]["security"]),
        ("international_arrival", lambda api: api["international_arrival"]),
    ]

    anomalies = []
    for code in ("IST", "CBR", "MFG", "OAG"):
        api = airport_predictions(session, code, now=now)
        for pname, getter in processes:
            windows = [w for w in getter(api)["windows"] if w["flight_count"] > 0 or w["estimated_wait_minutes"]]
            for a in windows:
                for b in windows:
                    if a is b:
                        continue
                    ra, rb = RISK_ORDER.get(a["risk"], -1), RISK_ORDER.get(b["risk"], -1)
                    wa, wb = a["estimated_wait_minutes"], b["estimated_wait_minutes"]
                    if wa is None or wb is None:
                        continue
                    if ra < rb and wa > wb:
                        anomalies.append((code, pname, a["window_start_local"], a["risk"], wa, b["window_start_local"], b["risk"], wb))

    session.close()
    assert not anomalies, f"{len(anomalies)} lower-risk/higher-wait anomaly kaldi: {anomalies[:5]}"


def _real_resolver(session):
    from app.service import AircraftCapacityService
    return AircraftCapacityService(session)


# ========================================================================
# Section 5 - WAIT HESABI DEĞİŞMEDİ (event-driven wait override aynen)
# ========================================================================

def test_wait_calculation_untouched_matches_event_driven_override():
    """`estimated_wait_minutes` hala ServiceEvent.wait_minutes tabanli
    override'dan geliyor - risk_backlog_start bu degeri ETKİLEMEMELİ."""
    common = dict(
        window_flights=[], demand_fn=lambda f: 0, server_count=1,
        service_time_minutes=1.0, window_minutes=60, demand_override=10.0,
        backlog_start=0.0,
    )
    r_no_risk_backlog = queue_capacity_model(**common, risk_backlog_start=0.0)
    r_with_risk_backlog = queue_capacity_model(**common, risk_backlog_start=500.0)
    assert r_no_risk_backlog["estimated_wait_minutes"] == r_with_risk_backlog["estimated_wait_minutes"]
    assert r_no_risk_backlog["risk"] != r_with_risk_backlog["risk"]  # risk FARKLI, wait AYNI


# ========================================================================
# Section 12 - LEGACY CONTAMINATION: PROCESS_PASSPORT/PROCESS_SECURITY
# risk'i ESKİ (sadece-rho) davranışı korumalı.
# ========================================================================

def test_legacy_combined_processes_keep_old_rho_only_risk_behavior():
    """
    Legacy `PROCESS_PASSPORT`/`PROCESS_SECURITY` (birlesik) risk_backlog_
    start'i ASLA ALMAZ (predict_airport() sadece _EVENT_DRIVEN_WAIT_
    PROCESSES icin hesapliyor) - yani legacy'nin queue_pressure'i HER
    ZAMAN rho'ya esit kalmali (eski davranis, degismedi).
    """
    from app.queue.constants import PROCESS_PASSPORT, PROCESS_SECURITY
    from app.queue.engine import _EVENT_DRIVEN_WAIT_PROCESSES

    assert PROCESS_PASSPORT not in _EVENT_DRIVEN_WAIT_PROCESSES
    assert PROCESS_SECURITY not in _EVENT_DRIVEN_WAIT_PROCESSES


def test_visible_five_graphs_never_reference_legacy_processes(cbr_style_session):
    """Overall/split graph'lar SADECE 4 event-driven surecin sonuclarini kullanir."""
    import inspect

    from app.queue import api as api_module

    source = inspect.getsource(api_module)
    # `_merge_overall_series` cagrisi passport/security (legacy) DEGIL,
    # domestic_security/international_security/passport_departure/
    # passport_arrival ile yapiliyor olmali (bkz. onceki ADIM raporu).
    assert "_merge_overall_series(\n        domestic_security, international_security,\n        passport_departure, passport_arrival" in source
