"""
ADIM (Departure Show-Up Profile).

Departure yolcu talebi artık TEK bir `effective_time()` (-120dk) spike'ı
DEĞİL - her flight KENDİ deterministic show-up profiline
(`DEPARTURE_SHOW_UP_PROFILE`, `domain/demand.py:departure_show_up_
events()`) göre kendi departure zamanından ÖNCEKİ 3 saate (12 adet 15dk
batch) yayılan bağımsız bir talep akışı üretir. Queue simülasyonu
CONTINUOUS/event-driven kalır (`core/event_queue.py` DEĞİŞMEDİ) - saatlik
bucket SADECE grafik/raporlama katmanının bir görünümüdür.

Arrival tarafı (International Arrival +15dk, Domestic Arrival excluded)
bu ADIM'dan HİÇ ETKİLENMEDİ - `engine.py`'nin `_arrivals()` closure'ı
(arrival_arrivals için) AYNEN kalır.
"""

from datetime import datetime, timedelta

from app.queue.config import default_config
from app.queue.constants import (
    DEPARTURE_SHOW_UP_PROFILE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    RISK_LOW,
)
from app.queue.core.event_queue import simulate_fifo_queue, total_count
from app.queue.domain.demand import DemandCalculator, departure_show_up_events
from app.queue.engine import predict_airport

from .factories import MockCapacityResolver, arrival, at, departure


def _demand():
    return DemandCalculator(MockCapacityResolver())


def intl_departure(hour, minute=0, **kwargs):
    kwargs.setdefault("location", LOCATION_INTERNATIONAL)
    return departure(hour, minute, **kwargs)


def intl_arrival(hour, minute=0, **kwargs):
    kwargs.setdefault("location", LOCATION_INTERNATIONAL)
    return arrival(hour, minute, **kwargs)


# ========================================================================
# 1) PROFILE - deterministic, toplam %100, random YOK.
# ========================================================================

def test_profile_fractions_sum_to_exactly_one():
    assert sum(frac for _, _, frac in DEPARTURE_SHOW_UP_PROFILE) == 1.0


def test_profile_is_12_batches_of_15_minutes_each():
    assert len(DEPARTURE_SHOW_UP_PROFILE) == 12
    for start, end, _ in DEPARTURE_SHOW_UP_PROFILE:
        assert start - end == 15
    assert DEPARTURE_SHOW_UP_PROFILE[0][0] == 180   # T-180 ile başlar
    assert DEPARTURE_SHOW_UP_PROFILE[-1][1] == 0    # T0'da biter


# ========================================================================
# 2) PASSENGER CONSERVATION - sum(batches) == passenger_demand(flight).
# ========================================================================

def test_departure_show_up_events_conserve_total_passenger_count():
    flight = intl_departure(12, 0, key="C1", number="1", aircraft="B77W")
    total = _demand().passenger_demand(flight)
    events = departure_show_up_events(flight, total)
    assert sum(count for _, count in events) == total


def test_conservation_holds_for_small_totals_no_negative_batches():
    flight = intl_departure(12, 0, key="C2", number="2")
    for total in (1, 2, 3, 5, 7, 13, 27):
        events = departure_show_up_events(flight, total)
        assert sum(count for _, count in events) == total
        assert all(count > 0 for _, count in events)   # sıfır/negatif batch üretilmez


def test_no_departure_reference_time_produces_no_events():
    from types import SimpleNamespace
    flight = SimpleNamespace(dep_actual_utc=None, dep_estimated_utc=None, dep_scheduled_utc=None)
    assert departure_show_up_events(flight, 200) == []


def test_zero_or_negative_demand_produces_no_events():
    flight = intl_departure(12, 0, key="C3", number="3")
    assert departure_show_up_events(flight, 0) == []
    assert departure_show_up_events(flight, -5) == []


# ========================================================================
# 3) DETERMINISM - aynı flight + aynı demand HER ZAMAN aynı batch listesi.
# ========================================================================

def test_show_up_events_are_deterministic_not_random():
    flight = intl_departure(12, 0, key="D1", number="1")
    a = departure_show_up_events(flight, 333)
    b = departure_show_up_events(flight, 333)
    assert a == b


def test_all_batches_land_before_or_at_departure_never_after():
    flight = intl_departure(12, 0, key="D2", number="2")
    dep = flight.dep_scheduled_utc
    events = departure_show_up_events(flight, 300)
    assert all(t <= dep for t, _ in events)
    assert all(t >= dep - timedelta(hours=3) for t, _ in events)


# ========================================================================
# 4) HER FLIGHT KENDİ PROFİLİNİ KENDİ DEPARTURE SAATİNE GÖRE ÜRETİR -
#    farklı flight'ların batch'leri DOĞAL olarak ÜST ÜSTE biner.
# ========================================================================

def test_each_flight_produces_independent_profile_around_its_own_departure():
    flight_a = intl_departure(10, 0, key="OVL_A", number="1")
    flight_b = intl_departure(10, 30, key="OVL_B", number="2")
    events_a = departure_show_up_events(flight_a, 200)
    events_b = departure_show_up_events(flight_b, 200)

    times_a = {t for t, _ in events_a}
    times_b = {t for t, _ in events_b}
    # Farklı departure saatleri -> farklı ama KISMEN ÇAKIŞAN batch zamanları.
    assert times_a != times_b
    assert times_a & times_b   # overlap VAR - bu İSTENEN davranış


def test_overlapping_flight_batches_merge_into_shared_queue_state():
    """
    İki flight'ın show-up batch'leri AYNI saniyeye denk gelirse
    `simulate_fifo_queue` (core/event_queue.py, DEĞİŞMEDİ) bunları TEK
    fiziksel kuyruk durumunda birleştirir - flight'lar birbirinden
    HABERSİZ olsa bile queue matematiği DOĞRU çalışır.
    """
    flight_a = intl_departure(10, 0, key="MRG_A", number="1")
    flight_b = intl_departure(10, 0, key="MRG_B", number="2")   # AYNI departure saati
    events_a = departure_show_up_events(flight_a, 100)
    events_b = departure_show_up_events(flight_b, 100)

    combined = [(t, "a", c) for t, c in events_a] + [(t, "b", c) for t, c in events_b]
    service_events = simulate_fifo_queue(combined, server_count=2, service_time_minutes=1.5)
    assert total_count(service_events) == 200   # iki flight'ın TOPLAMI, kayıp/çoğalma yok


# ========================================================================
# 5) QUEUE ASLA SAAT SINIRINDA SIFIRLANMAZ - service devam ederken yeni
#    batch gelir, backlog sürekli taşınır.
# ========================================================================

def test_service_continues_while_new_batches_arrive_no_hourly_reset():
    """
    Senaryo (Bölüm 15): passport server'ları 08:00'dan itibaren batch A'yı
    işliyor; 08:15 batch B, 08:30 batch C geliyor. A'nın servisi
    kesilmez/resetlenmez, B/C mevcut kuyruğa EKLENİR, server availability
    zaman damgaları KORUNUR, FIFO doğru, backlog sürekli, passenger kaybı
    yok.
    """
    T0 = datetime(2026, 9, 18, 8, 0)
    arrivals = [
        (T0, "A", 30.0),
        (T0 + timedelta(minutes=15), "B", 30.0),
        (T0 + timedelta(minutes=30), "C", 30.0),
    ]
    # 1 server, 1 dk/pax -> 90 pax'i tek server işlemek 90dk sürer;
    # 08:00'da başlayan A(30 pax) 08:00-08:30 sürer, B(08:15 gelen)
    # A bitene kadar bekler (server meşgul), C de B'den sonra.
    events = simulate_fifo_queue(arrivals, server_count=1, service_time_minutes=1.0)

    a_events = [e for e in events if e.origin == "A"]
    b_events = [e for e in events if e.origin == "B"]
    c_events = [e for e in events if e.origin == "C"]

    # A'nın SON tamamlanması (server_count=1, FIFO) tam olarak T0+30dk.
    a_last_completion = max(e.completion_time for e in a_events)
    assert a_last_completion == T0 + timedelta(minutes=30)

    # B, A tamamen bitmeden SERVİS ALAMAZ (server meşgul, "resetlenmedi") -
    # B'nin İLK service_start'ı A'nın son completion'ından ÖNCE OLAMAZ.
    b_first_service_start = min(e.service_start_time for e in b_events)
    assert b_first_service_start >= a_last_completion

    # C, B tamamen bitmeden servis alamaz - AYNI ilke, zincirleme.
    b_last_completion = max(e.completion_time for e in b_events)
    c_first_service_start = min(e.service_start_time for e in c_events)
    assert c_first_service_start >= b_last_completion

    # Passenger kaybı yok - toplam giriş == toplam tamamlanma.
    assert total_count(events) == 90.0
    # Server availability/FIFO sırası korunuyor: HİÇBİR event kendi
    # arrival'ından ÖNCE başlamadı.
    assert all(e.service_start_time >= e.arrival_time for e in events)


def test_domestic_security_continuous_across_batches_from_multiple_flights():
    """Aynı senaryo, Domestic Security için (Bölüm 7/15 - AYNI ilke)."""
    flights = [
        departure(9, 0, location=LOCATION_DOMESTIC, key=f"DS{i}", number=str(i), aircraft="A320")
        for i in range(3)
    ]
    predictions = predict_airport("AAA", flights, default_config("AAA", scale="large"), _demand())
    security_dom = sorted(
        [p for p in predictions if p.process == PROCESS_SECURITY_DOMESTIC],
        key=lambda p: p.window_start,
    )
    # BİRDEN FAZLA saate yayıldı (hour boundary'de resetlenmedi, sürekli).
    assert len(security_dom) >= 2
    # Conservation: TÜM saatlerin toplamı flight'ların ham talebine eşit.
    total_demand = sum(_demand().passenger_demand(f) for f in flights)
    assert sum(p.expected_passengers for p in security_dom) == total_demand


# ========================================================================
# 6) INTERNATIONAL DEPARTURE: show-up -> passport -> completion -> security.
# ========================================================================

def test_show_up_batches_feed_passport_and_passport_completion_feeds_security():
    flights = [
        intl_departure(11, 0, key="IP1", number="1", aircraft="B77W"),
        intl_departure(11, 15, key="IP2", number="2", aircraft="A320"),
        intl_departure(11, 30, key="IP3", number="3", aircraft="A320"),
    ]
    predictions = predict_airport("AAA", flights, default_config("AAA", scale="large"), _demand())

    passport = sorted(
        [p for p in predictions if p.process == PROCESS_PASSPORT_DEPARTURE],
        key=lambda p: p.window_start,
    )
    security = sorted(
        [p for p in predictions if p.process == PROCESS_SECURITY_INTL],
        key=lambda p: p.window_start,
    )
    assert passport   # show-up -> passport
    assert security   # passport completion -> security (Bölüm 5)

    total_demand = sum(_demand().passenger_demand(f) for f in flights)
    assert sum(p.expected_passengers for p in passport) == total_demand
    # Security'ye aktarılan TOPLAM asla passport'un işlediğinden FAZLA
    # olamaz (conservation, passport->security zincirinin temel garantisi).
    assert sum(s.expected_passengers for s in security) <= total_demand


def test_passport_and_security_progress_simultaneously_and_independently():
    """
    Bölüm 16: passport yeni batch alırken (Flight B) security ÖNCEKİ
    flight'ın (A) passenger'larını AYNI ANDA işliyor olabilir - iki
    BAĞIMSIZ kapasite state'i (Bölüm 17: paylaşılan server pool YOK).
    """
    T0 = datetime(2026, 9, 18, 12, 0)
    # A: passport'tan hemen serbest (küçük, tek batch gibi davranan) -
    # completion'ı security'ye erken ulaşır.
    passport_a_events = simulate_fifo_queue(
        [(T0, "A", 10.0)], server_count=4, service_time_minutes=1.5,
    )
    security_a_arrivals = [(e.completion_time, "sec", e.count) for e in passport_a_events]

    # B: passport'a YENİ geliyor (A'nın completion'ından SONRA bir an).
    passport_b_arrival_time = T0 + timedelta(minutes=5)
    passport_b_events = simulate_fifo_queue(
        [(passport_b_arrival_time, "B", 10.0)], server_count=4, service_time_minutes=1.5,
    )

    # Security, A'nın completion event'lerini passport_b henüz kuyrukta
    # ilerlerken de işleyebilir - iki simülasyon TAMAMEN BAĞIMSIZ
    # (ayrı `simulate_fifo_queue` çağrısı, ayrı heap) - biri diğerini
    # BEKLEMEZ/ENGELLEMEZ.
    security_events = simulate_fifo_queue(
        security_a_arrivals, server_count=2, service_time_minutes=1.0,
    )
    assert total_count(security_events) == 10.0
    assert total_count(passport_b_events) == 10.0
    # B'nin passport'a girişi (arrival_time), A'nın security'ye
    # ulaşmasından (security_a_arrivals'ın en erken zamanı) bağımsız
    # olarak KENDİ gerçek zamanında kalır - hiçbiri diğerini kaydırmadı.
    assert passport_b_events[0].arrival_time == passport_b_arrival_time


# ========================================================================
# 7) DOMESTIC / INTERNATIONAL PARALLEL FLOW - process isolation korunuyor.
# ========================================================================

def test_domestic_and_international_departures_never_share_server_pool():
    domestic = [departure(10, 0, location=LOCATION_DOMESTIC, key="PAR_DOM", number="1", aircraft="A320")]
    intl = [intl_departure(10, 0, key="PAR_INT", number="2", aircraft="A320")]
    predictions = predict_airport("AAA", domestic + intl, default_config("AAA", scale="large"), _demand())

    dom_security = [p for p in predictions if p.process == PROCESS_SECURITY_DOMESTIC]
    intl_security = [p for p in predictions if p.process == PROCESS_SECURITY_INTL]
    assert dom_security and intl_security

    dom_total = sum(p.expected_passengers for p in dom_security)
    intl_capped_by_passport = sum(p.expected_passengers for p in intl_security)
    # Domestic talebi TAMAMEN korunur (kendi havuzu, international'dan
    # ETKİLENMEZ) - international ise passport tavanıyla sınırlıdır
    # (AYRI havuz/kısıt, karışmaz).
    assert dom_total == _demand().passenger_demand(domestic[0])
    assert intl_capped_by_passport <= _demand().passenger_demand(intl[0])


# ========================================================================
# 8) ARRIVAL TARAFI HİÇ ETKİLENMEDİ - regresyon.
# ========================================================================

def test_arrival_still_uses_single_effective_time_point_not_show_up():
    """
    International Arrival: effective_time()'ın +15dk noktası AYNEN kalır
    - show-up profili arrival'a HİÇ uygulanmaz (Bölüm 8).
    """
    flight = intl_arrival(14, 0, key="ARR1", number="1", aircraft="B77W")
    predictions = predict_airport("AAA", flight and [flight], default_config("AAA", scale="large"), _demand())
    passport_arrival = [p for p in predictions if p.process == PROCESS_PASSPORT_ARRIVAL]

    # TEK pencere - show-up ile 3 saate YAYILMADI.
    assert len(passport_arrival) == 1
    row = passport_arrival[0]
    assert row.expected_passengers == _demand().passenger_demand(flight)
    expected_window = flight.arr_scheduled_utc + timedelta(minutes=15)
    assert row.window_start.hour == expected_window.hour


def test_domestic_arrival_still_excluded_from_all_queues():
    flight = arrival(9, 0, location=LOCATION_DOMESTIC, key="DOMARR", number="1")
    predictions = predict_airport("AAA", [flight], default_config("AAA", scale="large"), _demand())
    assert predictions == []   # domestic arrival hiçbir kuyruğa hiç girmez


def test_arrival_before_and_after_show_up_change_produces_identical_result():
    """
    Regresyon: AYNI arrival flight'ın passport sonucu, show-up ile
    ilgisiz olmalı - engine.py'deki `_arrivals()` (arrival_arrivals için)
    hiç değişmedi, bu yüzden sonuç DETERMINISTIK ve `effective_time()`
    tabanlı hesaplamayla BİREBİR AYNI kalmalı.
    """
    from app.queue.domain.demand import effective_time

    flight = intl_arrival(16, 0, key="ARR2", number="2", aircraft="A320")
    moment = effective_time(flight)
    assert moment == flight.arr_scheduled_utc + timedelta(minutes=15)

    predictions = predict_airport("AAA", [flight], default_config("AAA", scale="large"), _demand())
    row = next(p for p in predictions if p.process == PROCESS_PASSPORT_ARRIVAL)
    assert row.window_start.hour == moment.hour
    assert row.expected_passengers == _demand().passenger_demand(flight)


# ========================================================================
# 9) CROSS-MIDNIGHT SHOW-UP - 01:00 departure, show-up önceki güne taşar.
# ========================================================================

def test_cross_midnight_show_up_batches_land_on_previous_calendar_day():
    """
    Bölüm 14: 01:00 kalkış -> show-up 22:00 (önceki gün) - 01:00 (flight
    günü) arasında. Batch'ler KENDİ gerçek timestamp'inde kalır -
    operational-day/flight-selection mantığına (bu ADIM'da DOKUNULMADI)
    hiç karışmaz, sadece calculation state'te (event queue) doğru
    şekilde var olur.
    """
    flight = intl_departure(1, 0, key="XMID", number="1", aircraft="A320")   # 2026-09-14 01:00
    total = _demand().passenger_demand(flight)
    events = departure_show_up_events(flight, total)

    previous_day_events = [t for t, _ in events if t.date() < flight.dep_scheduled_utc.date()]
    same_day_events = [t for t, _ in events if t.date() == flight.dep_scheduled_utc.date()]
    assert previous_day_events   # gerçekten önceki güne taşan batch VAR
    assert same_day_events       # flight'ın kendi gününde de batch VAR
    assert sum(c for _, c in events) == total   # conservation, gün sınırından ETKİLENMEZ


# ========================================================================
# 10) EXACT 24 VISIBLE GRAPH - show-up yoğun senaryoda bile tam 24.
# ========================================================================

def test_dense_show_up_schedule_still_produces_exactly_24_visible_buckets():
    from app.queue.api import airport_predictions
    from app.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.queue.models import Airport, Flight

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Airport(iata_code="AAA", airport_name="Test", timezone="UTC"))

    day = datetime(2026, 9, 14)
    for hour in range(24):
        for i in range(3):
            session.add(Flight(
                flight_key=f"DENSE_{hour}_{i}", airport_iata="AAA",
                direction="departure", location="domestic",
                airline_iata="XX", flight_number=str(i), flight_iata=f"XX{hour}{i}",
                aircraft_icao="A320", aircraft_match_found=True,
                dep_iata="AAA", arr_iata="ZZZ",
                dep_scheduled_utc=day.replace(hour=hour, minute=i * 10),
                arr_scheduled_utc=day.replace(hour=hour, minute=i * 10) + timedelta(hours=1, minutes=15),
                status="scheduled",
            ))
    session.commit()

    from app.queue.engine import run_predictions
    run_predictions(session, MockCapacityResolver(), airports=["AAA"], now=day.replace(hour=18))

    api = airport_predictions(session, "AAA", now=day.replace(hour=18))
    for graph in ("overall", "domestic_security", "international_arrival"):
        windows = api[graph]["windows"]
        assert len(windows) == 24, f"{graph}: {len(windows)} != 24"
    assert len(api["international_departure"]["passport"]["windows"]) == 24
    assert len(api["international_departure"]["security"]["windows"]) == 24


# ========================================================================
# 11) REFRESH / IDEMPOTENCY - aynı flight, aynı show-up, duplicate yok.
# ========================================================================

def test_repeated_refresh_of_same_flight_produces_identical_show_up_batches():
    """
    Show-up ephemeral/pure bir hesaplama (hiçbir batch DB'ye ayrı satır
    olarak yazılmıyor) - aynı flight girdisiyle HER ÇAĞRIDA aynı batch
    listesi üretilir, bu yüzden 5dk refresh döngüsünde duplicate
    demand/event riski YOK.
    """
    flight = intl_departure(12, 0, key="IDEMP1", number="1", aircraft="B77W")
    total = _demand().passenger_demand(flight)

    first = departure_show_up_events(flight, total)
    second = departure_show_up_events(flight, total)
    third = departure_show_up_events(flight, total)

    assert first == second == third
    assert sum(c for _, c in first) == total   # her seferinde AYNI toplam, hiç kaymadı/çoğalmadı
