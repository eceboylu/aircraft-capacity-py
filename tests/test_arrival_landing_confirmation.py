"""
ADIM (Arrival Landing Confirmation) - kullanıcı talebi: "Henüz gerçekten
inmemiş arrival flight'ların passenger'ları current passport_arr queue'ya
girmesin, ama future forecast kaybolmasın."

Tasarım (bkz. `engine.py:_event_driven_queue_demand`/`event_driven_
display_series`, `domain/demand.py:is_arrival_landing_confirmed`):
- Tek trusted sinyal: `arr_actual_utc IS NOT NULL` (status hiç okunmuyor -
  status='landed'+actual=NULL de dahil, actual olmadan HİÇBİR flight
  "realized" sayılmaz - forecast'te kalır).
- `event_driven_display_series()` (queue_wait_display_5m'in kaynağı)
  `passport_arr_realized_only=True` ile çağrılır - passport_arr'ın
  current/display queue'su SADECE confirmed arrival'lardan oluşur.
- `run_predictions()`'ın kendi çağrısı (saatlik `queue_predictions`/
  forecast tablosu) DEĞİŞMEDİ (`passport_arr_realized_only=False`,
  varsayılan) - forecast demand KAYBOLMAZ.
- security_dom/security_intl/passport_dep VE departure tarafı bu
  parametreden HİÇ ETKİLENMEZ.
"""
from datetime import datetime, timedelta

from app.queue.config import default_config
from app.queue.constants import PROCESS_PASSPORT_ARRIVAL, PROCESS_SECURITY_DOMESTIC
from app.queue.domain.demand import DemandCalculator, is_arrival_landing_confirmed
from app.queue.engine import _event_driven_queue_demand, event_driven_display_series

from .conftest import FakeCapacityResult, FakeResolver, make_arrival, make_departure


def _resolver_and_demand(capacity=400):
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=capacity)})
    return resolver, DemandCalculator(resolver)


def test_is_arrival_landing_confirmed_only_checks_actual_timestamp():
    confirmed = make_arrival(when=datetime(2026, 3, 10, 9, 0))
    confirmed.arr_actual_utc = datetime(2026, 3, 10, 9, 0)
    assert is_arrival_landing_confirmed(confirmed) is True

    unconfirmed_active = make_arrival(when=datetime(2026, 3, 10, 9, 0))
    unconfirmed_active.status = "active"
    unconfirmed_active.arr_actual_utc = None
    assert is_arrival_landing_confirmed(unconfirmed_active) is False

    # status='landed' ama arr_actual_utc=NULL - SEÇİLEN policy: yine de
    # confirmed DEĞİL (status trusted bir sinyal değil).
    landed_without_actual = make_arrival(when=datetime(2026, 3, 10, 9, 0))
    landed_without_actual.status = "landed"
    landed_without_actual.arr_actual_utc = None
    assert is_arrival_landing_confirmed(landed_without_actual) is False


def test_confirmed_actual_arrival_produces_realized_cohorts_in_display():
    """Test 1 - actual exists -> realized cohorts current queue'ya girer."""
    _, demand = _resolver_and_demand(400)
    config = default_config("IST")
    config.passport_arrival_server_count = 1
    flight = make_arrival(when=datetime(2026, 3, 10, 9, 0), aircraft_icao="A321")
    flight.arr_actual_utc = datetime(2026, 3, 10, 9, 0)

    now = datetime(2026, 3, 10, 9, 20)
    series = event_driven_display_series([flight], config, demand, now=now)
    points = {p.window_start: p for p in series[PROCESS_PASSPORT_ARRIVAL]}
    # 9:00+10=9:10 ilk cohort - 9:20 checkpoint'inde queue'da olmalı.
    assert points[datetime(2026, 3, 10, 9, 15)].passenger_count > 0


def test_active_actual_null_future_estimated_is_forecast_only():
    """Test 2 - estimated gelecekte ise zaten doğal olarak current=0
    (checkpoint < arrival_time) - regresyon güvencesi."""
    _, demand = _resolver_and_demand(400)
    config = default_config("IST")
    config.passport_arrival_server_count = 1
    flight = make_arrival(when=datetime(2026, 3, 10, 14, 54), aircraft_icao="A321")
    flight.status = "active"
    flight.arr_actual_utc = None
    flight.arr_estimated_utc = datetime(2026, 3, 10, 14, 54)
    flight.arr_scheduled_utc = datetime(2026, 3, 10, 14, 40)

    now = datetime(2026, 3, 10, 14, 30)  # checkpoint estimated'dan ÖNCE
    series = event_driven_display_series([flight], config, demand, now=now)
    points = {p.window_start: p for p in series[PROCESS_PASSPORT_ARRIVAL]}
    assert points[datetime(2026, 3, 10, 14, 30)].passenger_count == 0.0


def test_active_actual_null_past_estimated_is_excluded_from_current_queue():
    """Test 3 - ANA BUG FIX: TK_51 tarzı senaryo - status=active,
    actual=NULL, estimated GEÇMİŞTE (checkpoint'ten önce). Eski davranışta
    bu current queue'ya girerdi (audit'te kanıtlandı) - YENİ davranışta
    current contribution = 0 olmalı."""
    _, demand = _resolver_and_demand(400)
    config = default_config("IST")
    config.passport_arrival_server_count = 1
    flight = make_arrival(when=datetime(2026, 3, 10, 8, 0), aircraft_icao="A321")
    flight.status = "active"
    flight.arr_actual_utc = None
    flight.arr_estimated_utc = datetime(2026, 3, 10, 9, 0)  # geçmişte kalacak
    flight.arr_scheduled_utc = datetime(2026, 3, 10, 8, 55)

    checkpoint = datetime(2026, 3, 10, 10, 0)  # estimated(9:00)+10..35'ten SONRA
    series = event_driven_display_series([flight], config, demand, now=checkpoint)
    points = {p.window_start: p for p in series[PROCESS_PASSPORT_ARRIVAL]}

    assert points[checkpoint].passenger_count == 0.0
    assert points[checkpoint].wait_minutes == 0.0

    # Forecast (run_predictions'ın saatlik queue_predictions tablosu) için
    # demand HÂLÂ üretilmeli - `passport_arr_realized_only=False`
    # (varsayılan) ile forecast kaybolmadığı ayrı testte doğrulanıyor
    # (bkz. `test_forecast_demand_preserved_for_hourly_predictions`).


def test_landed_status_with_null_actual_treated_same_as_unconfirmed():
    """Test 4 - status='landed' + arr_actual_utc=NULL: SEÇİLEN policy
    (actual olmadan asla realized denemez) - active ile AYNI şekilde
    current queue'dan hariç tutulmalı."""
    _, demand = _resolver_and_demand(400)
    config = default_config("IST")
    config.passport_arrival_server_count = 1
    flight = make_arrival(when=datetime(2026, 3, 10, 8, 0), aircraft_icao="A321")
    flight.status = "landed"
    flight.arr_actual_utc = None
    flight.arr_estimated_utc = datetime(2026, 3, 10, 9, 0)
    flight.arr_scheduled_utc = datetime(2026, 3, 10, 8, 55)

    checkpoint = datetime(2026, 3, 10, 10, 0)
    series = event_driven_display_series([flight], config, demand, now=checkpoint)
    points = {p.window_start: p for p in series[PROCESS_PASSPORT_ARRIVAL]}
    assert points[checkpoint].passenger_count == 0.0


def test_cancelled_and_diverted_never_enter_realized_queue():
    """Test 5/6 - cancelled/diverted actual olsa bile current queue'ya
    hiç girmemeli (EXCLUDED_STATUSES kontrolü predicate'ten ÖNCE)."""
    _, demand = _resolver_and_demand(400)
    config = default_config("IST")
    config.passport_arrival_server_count = 1
    cancelled = make_arrival(when=datetime(2026, 3, 10, 9, 0), status="cancelled", aircraft_icao="A321")
    cancelled.arr_actual_utc = datetime(2026, 3, 10, 9, 0)
    diverted = make_arrival(when=datetime(2026, 3, 10, 9, 0), status="diverted", aircraft_icao="A321")
    diverted.arr_actual_utc = datetime(2026, 3, 10, 9, 0)

    now = datetime(2026, 3, 10, 9, 20)
    series = event_driven_display_series([cancelled, diverted], config, demand, now=now)
    assert all(p.passenger_count == 0.0 for p in series[PROCESS_PASSPORT_ARRIVAL])


def test_estimated_to_actual_refresh_replaces_forecast_timing_no_double_count():
    """Test 7/8/9/11 - Refresh senaryosu: Refresh 1'de flight
    active/actual=NULL/estimated=10:00 -> current contribution=0.
    Refresh 2'de AYNI flight actual=10:08 alır -> artık realized, release
    actual(10:08) bazlı (10:18->10:42), estimated(10:00) bazlı ESKİ
    forecast timing (10:10->10:34) realized olarak SAYILMAZ (iki farklı
    obje, iki ayrı pipeline run - forecast+realized double-count riski
    yok çünkü HER run fresh flights listesinden yeniden hesaplanıyor)."""
    _, demand = _resolver_and_demand(200)
    config = default_config("IST")
    config.passport_arrival_server_count = 1

    # --- Refresh 1 ---
    flight_r1 = make_arrival(when=datetime(2026, 3, 10, 9, 50), aircraft_icao="A321")
    flight_r1.status = "active"
    flight_r1.arr_actual_utc = None
    flight_r1.arr_estimated_utc = datetime(2026, 3, 10, 10, 0)
    flight_r1.arr_scheduled_utc = datetime(2026, 3, 10, 9, 55)

    checkpoint1 = datetime(2026, 3, 10, 10, 20)
    series1 = event_driven_display_series([flight_r1], config, demand, now=checkpoint1)
    points1 = {p.window_start: p for p in series1[PROCESS_PASSPORT_ARRIVAL]}
    assert points1[checkpoint1].passenger_count == 0.0  # henüz confirmed değil

    coupling1 = _event_driven_queue_demand(
        [flight_r1], config, demand, now=checkpoint1, passport_arr_realized_only=True,
    )
    assert coupling1["process_events"][PROCESS_PASSPORT_ARRIVAL] == []

    # --- Refresh 2: actual geldi ---
    flight_r2 = make_arrival(when=datetime(2026, 3, 10, 9, 50), aircraft_icao="A321")
    flight_r2.status = "landed"
    flight_r2.arr_actual_utc = datetime(2026, 3, 10, 10, 8)
    flight_r2.arr_estimated_utc = datetime(2026, 3, 10, 10, 0)
    flight_r2.arr_scheduled_utc = datetime(2026, 3, 10, 9, 55)

    coupling2 = _event_driven_queue_demand(
        [flight_r2], config, demand, now=checkpoint1, passport_arr_realized_only=True,
    )
    events2 = coupling2["process_events"][PROCESS_PASSPORT_ARRIVAL]
    assert events2  # artık realized cohort var
    # Release base actual(10:08) olmalı: ilk cohort 10:08+10=10:18
    assert min(e.arrival_time for e in events2) == datetime(2026, 3, 10, 10, 18)
    assert max(e.arrival_time for e in events2) == datetime(2026, 3, 10, 10, 42)
    # exact passenger conservation - tam kapasite (200) korunmalı
    assert sum(e.count for e in events2) == 200.0


def test_no_double_count_forecast_plus_realized_same_flight():
    """Test 9/15 - AYNI flight (actual mevcut) her çağrıda TEK SET cohort
    üretmeli - forecast ve realized set'i birleştirilip iki kez
    sayılmıyor (zaten TEK bir `arrival_passenger_release_events()`
    çağrısı var, forecast/realized AYRI bir ikinci fonksiyon DEĞİL)."""
    _, demand = _resolver_and_demand(300)
    config = default_config("IST")
    config.passport_arrival_server_count = 1
    flight = make_arrival(when=datetime(2026, 3, 10, 9, 0), aircraft_icao="A321")
    flight.arr_actual_utc = datetime(2026, 3, 10, 9, 0)

    coupling = _event_driven_queue_demand(
        [flight], config, demand, now=datetime(2026, 3, 10, 12, 0),
        passport_arr_realized_only=True,
    )
    events = coupling["process_events"][PROCESS_PASSPORT_ARRIVAL]
    assert sum(e.count for e in events) == 300.0  # 2x300 DEĞİL


def test_old_queue_continues_when_new_realized_arrival_joins():
    """Test 10/25 - Flight A actual=09:00 (backlog uzun sürsün diye 1
    server), backlog hâlâ sürerken Flight B actual=10:08 confirmed olursa
    B'nin passenger'ları FIFO'da A'dan kalan kuyruğun ARKASINA eklenmeli
    (queue reset YOK)."""
    _, demand = _resolver_and_demand(300)
    config = default_config("IST")
    config.passport_arrival_server_count = 1  # backlog uzun sürsün

    flight_a = make_arrival(when=datetime(2026, 3, 10, 9, 0), aircraft_icao="A321", flight_key="A")
    flight_a.arr_actual_utc = datetime(2026, 3, 10, 9, 0)

    flight_b = make_arrival(when=datetime(2026, 3, 10, 10, 0), aircraft_icao="A321", flight_key="B")
    flight_b.arr_actual_utc = datetime(2026, 3, 10, 10, 8)

    coupling = _event_driven_queue_demand(
        [flight_a, flight_b], config, demand, now=datetime(2026, 3, 10, 12, 0),
        passport_arr_realized_only=True,
    )
    events = coupling["process_events"][PROCESS_PASSPORT_ARRIVAL]
    a_events = [e for e in events if "A" in e.source_flight_keys]
    b_events = [e for e in events if "B" in e.source_flight_keys]
    assert a_events and b_events
    # B'nin İLK service_start'ı, A'nın SON service_start'ından ÖNCE
    # OLAMAZ (A'nın backlog'u bitmeden B başlamış olamaz - FIFO garanti).
    assert min(e.service_start_time for e in b_events) >= max(e.service_start_time for e in a_events)


def test_old_queue_already_drained_new_arrival_starts_from_empty_backlog():
    """Test 11/26 - Flight A'nın backlog'u B gelene kadar TAMAMEN
    boşalmışsa (bol server, az yolcu), B kendi arrival_time'ından hemen
    sonra servise başlamalı - eski bitmiş queue'nun izi taşınmamalı."""
    _, demand = _resolver_and_demand(10)
    config = default_config("IST")
    config.passport_arrival_server_count = 30  # bol kapasite, backlog hemen biter

    flight_a = make_arrival(when=datetime(2026, 3, 10, 9, 0), aircraft_icao="A321", flight_key="A")
    flight_a.arr_actual_utc = datetime(2026, 3, 10, 9, 0)

    flight_b = make_arrival(when=datetime(2026, 3, 10, 12, 0), aircraft_icao="A321", flight_key="B")
    flight_b.arr_actual_utc = datetime(2026, 3, 10, 12, 30)

    coupling = _event_driven_queue_demand(
        [flight_a, flight_b], config, demand, now=datetime(2026, 3, 10, 13, 0),
        passport_arr_realized_only=True,
    )
    events = coupling["process_events"][PROCESS_PASSPORT_ARRIVAL]
    b_events = [e for e in events if "B" in e.source_flight_keys]
    # B'nin ilk service_start'ı KENDİ arrival_time'ına çok yakın olmalı
    # (A'dan kalan bir backlog'un arkasına eklenmiş DEĞİL).
    first_b = min(b_events, key=lambda e: e.arrival_time)
    assert (first_b.service_start_time - first_b.arrival_time).total_seconds() < 120


def test_departure_side_completely_unaffected_by_realized_only_flag():
    """Test 21/29 - `passport_arr_realized_only=True` iken security_dom/
    security_intl/passport_dep (departure kaynaklı) HİÇ etkilenmemeli -
    unconfirmed arrival flight'ın varlığı departure hesaplarını
    değiştirmemeli."""
    _, demand = _resolver_and_demand(300)
    config = default_config("IST")

    departure = make_departure(when=datetime(2026, 3, 10, 9, 0), location="domestic", aircraft_icao="A321")
    unconfirmed_arrival = make_arrival(when=datetime(2026, 3, 10, 9, 0), aircraft_icao="A321")
    unconfirmed_arrival.status = "active"
    unconfirmed_arrival.arr_actual_utc = None

    now = datetime(2026, 3, 10, 9, 30)
    coupling_without_flag = _event_driven_queue_demand(
        [departure, unconfirmed_arrival], config, demand, now=now, passport_arr_realized_only=False,
    )
    coupling_with_flag = _event_driven_queue_demand(
        [departure, unconfirmed_arrival], config, demand, now=now, passport_arr_realized_only=True,
    )
    dom_without = coupling_without_flag["process_events"][PROCESS_SECURITY_DOMESTIC]
    dom_with = coupling_with_flag["process_events"][PROCESS_SECURITY_DOMESTIC]
    assert len(dom_without) == len(dom_with)
    assert sum(e.count for e in dom_without) == sum(e.count for e in dom_with)


def test_forecast_demand_preserved_for_hourly_predictions_default_flag():
    """Test 2/8 - `passport_arr_realized_only=False` (varsayılan,
    `run_predictions()`'ın kendi çağrısı) unconfirmed flight'lar için
    HÂLÂ demand üretmeli - forecast KAYBOLMAMALI, sadece current/display
    queue'dan hariç tutuluyor."""
    _, demand = _resolver_and_demand(180)
    config = default_config("IST")
    config.passport_arrival_server_count = 1

    flight = make_arrival(when=datetime(2026, 3, 10, 8, 0), aircraft_icao="A321")
    flight.status = "active"
    flight.arr_actual_utc = None
    flight.arr_estimated_utc = datetime(2026, 3, 10, 9, 0)

    coupling = _event_driven_queue_demand(
        [flight], config, demand, now=datetime(2026, 3, 10, 10, 0),
        passport_arr_realized_only=False,
    )
    events = coupling["process_events"][PROCESS_PASSPORT_ARRIVAL]
    assert sum(e.count for e in events) == 180.0  # forecast demand HÂLÂ var
