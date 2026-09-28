"""
security_dom / security_intl hourly flight_count artık effective_time(-120)
tabanlı bucket'tan DEĞİL, gerçek event-driven arrival stream'e katkı yapan
DISTINCT PHYSICAL FLIGHT sayısından geliyor (bkz. engine.py:predict_airport
`attributed_flight_count_processes`, `_bucket_contributing_flight_keys`).

Flight identity, `ServiceEvent.source_flight_keys` (additive, opsiyonel alan)
üzerinden queue math'e DOKUNMADAN taşınıyor - domestic/Schengen için show-up
cohort'una flight_key etiketlenir; non-Schengen için passport completion
event'i, passport ServiceEvent'inin KENDİ source_flight_keys'ini miras alır.
"""
from datetime import datetime

from app.queue.config import default_config
from app.queue.constants import PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL
from app.queue.core.event_queue import simulate_fifo_queue
from app.queue.domain.demand import DemandCalculator
from app.queue.engine import predict_airport

from .conftest import FakeCapacityResult, FakeResolver, make_departure


def _rows_by_hour(predictions, process):
    return {p.window_start: p for p in predictions if p.process == process}


def test_domestic_multi_flight_different_percentage_segments():
    """13:00/14:00/15:00/16:00 kalkışlarının FARKLI show-up segmentleri aynı 12:00-13:00 saatine katkı yapar - hepsi flight_count'a girmeli."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("XXX", scale="large")
    day = datetime(2026, 3, 10)
    flights = [
        make_departure(when=day.replace(hour=13), location="domestic", flight_key="F13"),
        make_departure(when=day.replace(hour=14), location="domestic", flight_key="F14"),
        make_departure(when=day.replace(hour=15), location="domestic", flight_key="F15"),
        make_departure(when=day.replace(hour=16), location="domestic", flight_key="F16"),
    ]
    preds = predict_airport("XXX", flights, config, demand)
    rows = _rows_by_hour(preds, PROCESS_SECURITY_DOMESTIC)
    row = rows[day.replace(hour=12)]
    assert row.flight_count == 4
    assert row.expected_passengers > 0


def test_domestic_one_flight_many_cohorts_counts_once():
    """Bir flight aynı saate 12 ayrı 5dk cohort göndersin - flight_count HÂLÂ 1 olmalı, 12 değil."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=300)})
    demand = DemandCalculator(resolver)
    config = default_config("XXX", scale="large")
    day = datetime(2026, 3, 10)
    # T-180..T-120 segmenti (45%) tam bir saate denk düşecek şekilde round-hour departure.
    flight = make_departure(when=day.replace(hour=15), location="domestic", flight_key="ONLY")
    preds = predict_airport("XXX", [flight], config, demand)
    rows = _rows_by_hour(preds, PROCESS_SECURITY_DOMESTIC)
    row = rows[day.replace(hour=12)]
    assert row.flight_count == 1


def test_domestic_one_flight_counted_in_every_hour_it_contributes():
    """Bir flight birden fazla saate katkı yapıyorsa, flight_count HER İKİSİNDE de 1 olmalı (aynı flight, ayrı saatler)."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("XXX", scale="large")
    flight = make_departure(when=datetime(2026, 3, 10, 15, 20), location="domestic", flight_key="SPANNING")
    preds = predict_airport("XXX", [flight], config, demand)
    rows = _rows_by_hour(preds, PROCESS_SECURITY_DOMESTIC)
    contributing_hours = [h for h, r in rows.items() if r.flight_count > 0]
    assert len(contributing_hours) >= 2
    assert all(rows[h].flight_count == 1 for h in contributing_hours)


def test_no_hour_has_zero_flight_count_with_nonzero_passengers():
    """Regresyon kilidi: eski bug'ın imzası (flight_count=0 ama expected_passengers>0) artık HİÇBİR saatte olmamalı."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("XXX", scale="large")
    flights = [
        make_departure(when=datetime(2026, 3, 10, 12, 0), location="domestic", flight_key="D1"),
        make_departure(when=datetime(2026, 3, 10, 15, 20), location="international", requires_passport=True, flight_key="NS1"),
        make_departure(when=datetime(2026, 3, 10, 12, 10), location="international", requires_passport=False, flight_key="SCH1"),
    ]
    preds = predict_airport("XXX", flights, config, demand)
    for p in preds:
        if p.process in (PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL):
            if p.expected_passengers > 0:
                assert p.flight_count > 0, (p.process, p.window_start, p.expected_passengers)


def test_schengen_direct_attribution_uses_show_up_time():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("XXX", scale="large")
    flight = make_departure(
        when=datetime(2026, 3, 10, 15, 0), location="international",
        requires_passport=False, flight_key="SCHENGEN-DIRECT",
    )
    preds = predict_airport("XXX", [flight], config, demand)
    rows = _rows_by_hour(preds, PROCESS_SECURITY_INTL)
    # T-180..T-120 segmenti (%45) tam olarak 12:00-13:00'a düşüyor.
    assert rows[datetime(2026, 3, 10, 12, 0)].flight_count == 1


def test_non_schengen_attributed_to_passport_completion_hour_not_show_up_hour():
    """
    Show-up 11:xx civarında ama passport backlog'u yüzünden completion
    12:xx'e taşarsa, security_intl flight_count 12:00 saatine girmeli,
    11:00'e DEĞİL (bkz. audit'in "Flight X" örneği).
    """
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=900)})
    demand = DemandCalculator(resolver)
    config = default_config("XXX", scale="large")
    config.passport_departure_server_count = 1  # kasıtlı olarak aşırı yavaş - backlog garanti

    # Show-up'ın büyük kısmı 11:xx'te oluşsun (dep=15:00, T-180..T-120=%45 -> 12:00-13:00... )
    # Daha net kontrol için dep=14:00 kullanıyoruz: T-180..T-120 (%45) -> 11:00-12:00.
    flight = make_departure(
        when=datetime(2026, 3, 10, 14, 0), location="international",
        requires_passport=True, flight_key="SLOW-PASSPORT", aircraft_icao="A321",
    )
    preds = predict_airport("XXX", [flight], config, demand)
    dom_rows = {p.window_start: p for p in preds if p.process == PROCESS_SECURITY_INTL}

    show_up_hour = datetime(2026, 3, 10, 11, 0)
    # 1 passport server ile 900 pax'lık bir show-up dalgası saatlerce sürer -
    # completion'lar show-up saatinden SONRAKİ saatlere taşar.
    later_hours_have_contribution = any(
        h > show_up_hour and r.flight_count > 0 for h, r in dom_rows.items()
    )
    assert later_hours_have_contribution


def test_mixed_international_hour_counts_all_distinct_flights():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("XXX", scale="mega")  # bol kapasite, backlog taşması olmasın
    day = datetime(2026, 3, 10)
    flights = [
        make_departure(when=day.replace(hour=15), location="international", requires_passport=False, flight_key="SCH-A"),
        make_departure(when=day.replace(hour=15), location="international", requires_passport=False, flight_key="SCH-B"),
        make_departure(when=day.replace(hour=15), location="international", requires_passport=True, flight_key="NS-A"),
        make_departure(when=day.replace(hour=15), location="international", requires_passport=True, flight_key="NS-B"),
        make_departure(when=day.replace(hour=15), location="international", requires_passport=True, flight_key="NS-C"),
    ]
    preds = predict_airport("XXX", flights, config, demand)
    rows = _rows_by_hour(preds, PROCESS_SECURITY_INTL)
    row = rows[day.replace(hour=12)]  # T-180..T-120 (%45) -> 12:00-13:00 saati
    assert row.flight_count == 5


def test_domestic_and_international_never_share_flight_count():
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("XXX", scale="large")
    day = datetime(2026, 3, 10)
    flights = [
        make_departure(when=day.replace(hour=15), location="domestic", flight_key="DOM-ONLY"),
    ]
    preds = predict_airport("XXX", flights, config, demand)
    intl_rows = _rows_by_hour(preds, PROCESS_SECURITY_INTL)
    assert all(r.flight_count == 0 for r in intl_rows.values())


def test_metadata_fix_does_not_change_wait_or_passenger_totals():
    """Flight_count/window_flights değişikliği estimated_wait_minutes'ı ve expected_passengers toplamını ETKİLEMEMELİ - sadece metadata."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("XXX", scale="large")
    flights = [
        make_departure(when=datetime(2026, 3, 10, 15, 20), location="domestic", flight_key="A"),
        make_departure(when=datetime(2026, 3, 10, 16, 0), location="domestic", flight_key="B"),
    ]
    preds = predict_airport("XXX", flights, config, demand)
    dom_rows = [p for p in preds if p.process == PROCESS_SECURITY_DOMESTIC]
    total_pax = sum(p.expected_passengers for p in dom_rows)
    assert total_pax == 360  # 2 x 180, conservation korunuyor

    # Wait hâlâ gerçek FIFO event'lerinden (queue_capacity_model/event-driven override) - flight_count'a bağlı DEĞİL.
    for p in dom_rows:
        assert p.estimated_wait_minutes is not None


def test_passport_completion_event_carries_source_flight_keys():
    """Queue math seviyesinde: passport completion ServiceEvent'i, security'ye taşınan flight kimliğini KORUR."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("XXX", scale="mega")
    flight = make_departure(
        when=datetime(2026, 3, 10, 15, 0), location="international",
        requires_passport=True, flight_key="TRACKED",
    )
    from app.queue.domain.demand import departure_show_up_events
    show_up = [
        (t, c, flight.flight_key)
        for t, c in departure_show_up_events(flight, 180)
    ]
    events = simulate_fifo_queue(
        [(t, "departure", c, k) for t, c, k in show_up],
        server_count=8, service_time_minutes=1.0,
    )
    assert events
    assert all(e.source_flight_keys == {"TRACKED": e.count} for e in events)


def test_merge_sums_per_flight_passenger_shares():
    """
    İki farklı flight'tan gelen ama AYNI (arrival,service_start,completion)
    sahip event'ler merge olduğunda `source_flight_keys` her flight'ın
    GERÇEK yolcu payını (count DEĞİL, sum(shares)=count olacak şekilde)
    korur - "kim katkı yaptı" (set) değil "kim kaç yolcu katkı yaptı" (dict).
    """
    base = datetime(2026, 3, 10, 12, 20)
    events = simulate_fifo_queue(
        [(base, "x", 5.0, "A"), (base, "x", 3.0, "B")],
        server_count=100, service_time_minutes=1.5,
    )
    # 100 server, 8 kişi -> hepsi aynı anda (SS=arrival) servise girer, tek merged event.
    assert len(events) == 1
    assert events[0].count == 8.0
    assert events[0].source_flight_keys == {"A": 5.0, "B": 3.0}
    assert sum(events[0].source_flight_keys.values()) == events[0].count


def test_backward_compatible_plain_tuples_still_work_without_identity():
    """Eski 3-tuple çağrı biçimi (identity YOK) hâlâ çalışmalı - source_flight_keys=None."""
    base = datetime(2026, 3, 10, 12, 0)
    events = simulate_fifo_queue([(base, "x", 4.0)], server_count=8, service_time_minutes=1.0)
    assert events[0].source_flight_keys is None
