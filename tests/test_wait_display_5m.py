"""
ADIM (Current-Queue Weighted Remaining Wait) - grafik metriği ARTIK "bu
5dk'da yeni gelenlerin ortalama wait'i" (`passenger_weighted_arrival_
window` - IST'te passport_dep kendi arzını 20:17'de tükettiğinde, hâlâ
~10.000 kişilik bir backlog varken değerin 213dk'dan ANİDEN 0'a düşmesine,
yanıltıcı "queue bitti" izlenimine yol açmıştı) DEĞİL.

YENİ formül: her checkpoint'te (`arrival_time <= checkpoint <
service_start_time`) GERÇEKTEN kuyrukta bekleyen yolcuların passenger-
weighted ORTALAMA KALAN bekleme süresi (`remaining_wait = service_start
- checkpoint`). Bir yolcu `service_start`'a ulaştığı anda (serviste/
tamamlanmış) hesaba hiç dahil edilmez. Farklı bir hesap İCAT EDİLMEDİ -
SADECE production'ın zaten ürettiği `arrival_time`/`service_start_time`/
`count` okunuyor (bkz. `_remaining_wait_at_checkpoint`).

`five_minute_wait_series()` (eski "yeni-gelenler" arrival-window metriği)
KALDIRILMADI - artık ana display'in kaynağı değil, ama saf/doğru bir
yardımcı olarak SQL-yan audit/karşılaştırma için KORUNDU (bkz. kendi
testleri aşağıda, DEĞİŞMEDİ).
"""
from datetime import datetime, timedelta

import pytest

from app.queue.config import default_config
from app.queue.constants import (
    PROCESS_PASSPORT_ARRIVAL,
    PROCESS_PASSPORT_DEPARTURE,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
)
from app.queue.core.event_queue import ServiceEvent, simulate_fifo_queue
from app.queue.domain.demand import DemandCalculator
from app.queue.engine import (
    DISPLAY_5M_PROCESSES,
    _remaining_wait_at_checkpoint,
    event_driven_display_series,
    five_minute_wait_series,
    predict_airport,
)

from .conftest import FakeCapacityResult, FakeResolver, make_arrival, make_departure


# ---------------------------------------------------------------------------
# `_remaining_wait_at_checkpoint` - saf fonksiyon, tek başına
# ---------------------------------------------------------------------------

def test_waiting_predicate_arrival_before_and_service_start_after_checkpoint():
    """Bölüm 1/2 - user örneği: A(pax=100,ss=23:20) B(pax=200,ss=22:20)
    C(pax=50,ss=21:20), checkpoint=20:20 -> 128.57dk."""
    checkpoint = datetime(2026, 3, 10, 20, 20)
    base_arrival = datetime(2026, 3, 10, 18, 0)  # checkpoint'ten önce, hepsi hâlâ bekliyor
    events = [
        ServiceEvent("x", 100.0, base_arrival, datetime(2026, 3, 10, 23, 20), datetime(2026, 3, 10, 23, 21)),
        ServiceEvent("x", 200.0, base_arrival, datetime(2026, 3, 10, 22, 20), datetime(2026, 3, 10, 22, 21)),
        ServiceEvent("x", 50.0, base_arrival, datetime(2026, 3, 10, 21, 20), datetime(2026, 3, 10, 21, 21)),
    ]
    avg, pax, numerator = _remaining_wait_at_checkpoint(events, checkpoint)

    assert pax == 350.0
    assert numerator == pytest.approx(100 * 180 + 200 * 120 + 50 * 60)
    assert avg == pytest.approx(45000 / 350, rel=1e-4)


def test_remaining_wait_is_service_start_minus_checkpoint():
    checkpoint = datetime(2026, 3, 10, 12, 0)
    event = ServiceEvent(
        "x", 10.0, checkpoint - timedelta(minutes=5),
        checkpoint + timedelta(minutes=17), checkpoint + timedelta(minutes=18),
    )
    avg, pax, numerator = _remaining_wait_at_checkpoint([event], checkpoint)
    assert avg == pytest.approx(17.0)
    assert pax == 10.0
    assert numerator == pytest.approx(170.0)


def test_passenger_not_yet_arrived_is_excluded():
    checkpoint = datetime(2026, 3, 10, 12, 0)
    event = ServiceEvent(
        "x", 10.0, checkpoint + timedelta(minutes=1),  # henüz gelmedi
        checkpoint + timedelta(minutes=5), checkpoint + timedelta(minutes=6),
    )
    avg, pax, numerator = _remaining_wait_at_checkpoint([event], checkpoint)
    assert (avg, pax, numerator) == (0.0, 0.0, 0.0)


def test_service_started_passenger_excluded():
    """Bölüm 3/Bölüm 24-4 - service_start <= checkpoint olduğu anda
    artık 'waiting' değil, hesaba KATILMAZ."""
    checkpoint = datetime(2026, 3, 10, 21, 10)
    still_waiting = ServiceEvent(
        "x", 5.0, checkpoint - timedelta(minutes=20),
        checkpoint + timedelta(minutes=1), checkpoint + timedelta(minutes=2),
    )
    just_started = ServiceEvent(
        "x", 8.0, checkpoint - timedelta(minutes=30),
        checkpoint, checkpoint + timedelta(minutes=1),  # service_start == checkpoint tam
    )
    avg, pax, numerator = _remaining_wait_at_checkpoint([still_waiting, just_started], checkpoint)
    # SADECE still_waiting dahil - just_started'ın service_start'ı checkpoint'e EŞİT (>değil)
    assert pax == 5.0
    assert avg == pytest.approx(1.0)


def test_completed_passenger_excluded():
    """Bölüm 5/16 - service_start<=checkpoint<completion (serviste) VE
    completion<=checkpoint (tamamlanmış) ikisi de waiting average'a DAHİL EDİLMEZ."""
    checkpoint = datetime(2026, 3, 10, 12, 0)
    in_service = ServiceEvent(
        "x", 3.0, checkpoint - timedelta(minutes=10),
        checkpoint - timedelta(minutes=1), checkpoint + timedelta(minutes=5),
    )
    completed = ServiceEvent(
        "x", 7.0, checkpoint - timedelta(minutes=20),
        checkpoint - timedelta(minutes=15), checkpoint - timedelta(minutes=1),
    )
    avg, pax, numerator = _remaining_wait_at_checkpoint([in_service, completed], checkpoint)
    assert (avg, pax, numerator) == (0.0, 0.0, 0.0)


def test_no_waiting_passengers_gives_explicit_zero():
    """Bölüm 24-7 - gerçekten kimse beklemiyorsa 0."""
    checkpoint = datetime(2026, 3, 10, 12, 0)
    avg, pax, numerator = _remaining_wait_at_checkpoint([], checkpoint)
    assert (avg, pax, numerator) == (0.0, 0.0, 0.0)


def test_ist_style_backlog_persists_after_new_arrivals_stop():
    """
    Bölüm 24-6/11 - EN KRİTİK REGRESYON: IST security_intl senaryosunun
    küçük ölçekli benzeri. 20:00-20:17 arası büyük bir show-up dalgası
    geliyor (hepsi çok ileri bir service_start'a sahip - ağır backlog),
    20:17'den SONRA hiç yeni arrival YOK. 20:20 checkpoint'inde:
      - ESKİ metrik (arrival-window): pax=0 -> wait=0 (YANILTICI)
      - YENİ metrik (remaining-wait): backlog hâlâ duruyor -> wait > 0
    """
    base = datetime(2026, 3, 10, 20, 0)
    # 300 kişi 20:00'da geliyor, backlog nedeniyle service_start çok geç (23:00).
    heavy_wave = ServiceEvent("x", 300.0, base, base + timedelta(hours=3), base + timedelta(hours=3, minutes=1))
    events = [heavy_wave]

    checkpoint_20_20 = base + timedelta(minutes=20)

    # Eski (kaldırılan ana metrik) semantics: bu pencerede (20:20-20:25)
    # yeni arrival YOK -> arrival-window metriği burada sıfır dönerdi.
    arrival_window_points = dict(
        (w, avg) for w, avg, _, _ in five_minute_wait_series(events, window_minutes=5)
    )
    assert checkpoint_20_20 not in arrival_window_points  # o pencerede yeni arrival yok

    # Yeni metrik: 300 kişi HÂLÂ bekliyor (service_start 23:00'da, 20:20'den ÇOK sonra).
    avg, pax, numerator = _remaining_wait_at_checkpoint(events, checkpoint_20_20)
    assert pax == 300.0
    assert avg == pytest.approx(160.0)  # 23:00 - 20:20 = 2s40dk = 160dk
    assert avg > 0  # ANİDEN 0'a düşmedi


def test_metric_increases_when_new_arrivals_join_the_queue():
    """Bölüm 24-8 - yeni yolcu gelirse (queue'ya eklenirse) metrik artabilir."""
    checkpoint = datetime(2026, 3, 10, 12, 0)
    small_wait = ServiceEvent(
        "x", 10.0, checkpoint - timedelta(minutes=5),
        checkpoint + timedelta(minutes=5), checkpoint + timedelta(minutes=6),
    )
    avg_before, _, _ = _remaining_wait_at_checkpoint([small_wait], checkpoint)

    big_new_arrival = ServiceEvent(
        "x", 500.0, checkpoint - timedelta(minutes=1),
        checkpoint + timedelta(minutes=90), checkpoint + timedelta(minutes=91),
    )
    avg_after, _, _ = _remaining_wait_at_checkpoint([small_wait, big_new_arrival], checkpoint)
    assert avg_after > avg_before


def test_metric_decreases_naturally_as_queue_drains_without_new_arrivals():
    """Bölüm 24-9 - hiç yeni arrival gelmese bile, zaman ilerledikçe
    (bazı yolcular service_start'a ulaştıkça) metrik azalabilir."""
    base = datetime(2026, 3, 10, 20, 0)
    events = [
        ServiceEvent("x", 50.0, base, base + timedelta(minutes=30), base + timedelta(minutes=31)),
        ServiceEvent("x", 50.0, base, base + timedelta(minutes=60), base + timedelta(minutes=61)),
        ServiceEvent("x", 50.0, base, base + timedelta(minutes=90), base + timedelta(minutes=91)),
    ]
    avg_t20, pax_t20, _ = _remaining_wait_at_checkpoint(events, base + timedelta(minutes=20))
    avg_t40, pax_t40, _ = _remaining_wait_at_checkpoint(events, base + timedelta(minutes=40))
    avg_t65, pax_t65, _ = _remaining_wait_at_checkpoint(events, base + timedelta(minutes=65))

    assert pax_t20 == 150.0  # hepsi hâlâ bekliyor
    assert pax_t40 == 100.0  # ilk grup (ss=30) artık serviste, hesaba dahil değil
    assert pax_t65 == 50.0   # ilk iki grup da serviste
    assert avg_t20 > avg_t40 > avg_t65 > 0


# ---------------------------------------------------------------------------
# `event_driven_display_series` - tam gün, no-interpolation, tüm süreçler
# ---------------------------------------------------------------------------

def test_display_5m_processes_are_only_the_four_split_processes():
    assert set(DISPLAY_5M_PROCESSES) == {
        PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL,
        PROCESS_PASSPORT_DEPARTURE, PROCESS_PASSPORT_ARRIVAL,
    }


# ---------------------------------------------------------------------------
# ADIM (Display Wait Semantics Unification) - `passport_arr`'ın DA (diğer
# 3 event-driven süreç gibi) SADECE `_remaining_wait_at_checkpoint`
# (current_queue_weighted_remaining_wait) kullandığını, process'e özel
# FARKLI bir formül OLMADIĞINI ve saat sınırında sıfırlanma OLMADIĞINI
# doğrulayan testler.
# ---------------------------------------------------------------------------

def _overloaded_scenario(process: str):
    """Her event-driven süreç için, o sürece backlog biriktirecek TEK bir
    ağır flight + o pool'u 1 kaynağa düşüren config override döndürür."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=400)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")
    base = datetime(2026, 3, 10, 12, 0)

    if process == PROCESS_SECURITY_DOMESTIC:
        config.domestic_security_lane_count = 1
        flight = make_departure(when=base, location="domestic", aircraft_icao="A321")
    elif process == PROCESS_PASSPORT_DEPARTURE:
        config.passport_departure_server_count = 1
        flight = make_departure(when=base, location="international", requires_passport=True, aircraft_icao="A321")
    elif process == PROCESS_SECURITY_INTL:
        config.international_security_lane_count = 1
        flight = make_departure(when=base, location="international", requires_passport=True, aircraft_icao="A321")
    elif process == PROCESS_PASSPORT_ARRIVAL:
        config.passport_arrival_server_count = 1
        flight = make_arrival(when=base, location="international", requires_passport=True, aircraft_icao="A321")
    else:
        raise AssertionError(process)

    return [flight], config, demand


@pytest.mark.parametrize("process", [
    PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL,
    PROCESS_PASSPORT_DEPARTURE, PROCESS_PASSPORT_ARRIVAL,
])
def test_all_four_processes_use_identical_checkpoint_formula(process):
    """Bölüm 4/8 - process-specific FARKLI bir wait formula YOK: her
    süreç için `event_driven_display_series()`'in ürettiği nokta,
    AYNI `_remaining_wait_at_checkpoint()` ile o sürecin KENDİ
    event listesinden manuel yeniden hesaplanan değerle birebir eşleşmeli."""
    flights, config, demand = _overloaded_scenario(process)
    now = datetime(2026, 3, 10, 14, 0)

    coupling_events = None
    from app.queue.engine import _event_driven_queue_demand
    coupling = _event_driven_queue_demand(flights, config, demand, now=now)
    coupling_events = coupling["process_events"][process]

    series = event_driven_display_series(flights, config, demand, now=now)
    checkpoint = datetime(2026, 3, 10, 13, 5)
    point = next(p for p in series[process] if p.window_start == checkpoint)

    expected_avg, expected_pax, expected_numerator = _remaining_wait_at_checkpoint(
        coupling_events, checkpoint
    )
    assert point.wait_minutes == pytest.approx(expected_avg)
    assert point.passenger_count == pytest.approx(expected_pax)
    assert point.wait_numerator == pytest.approx(expected_numerator)
    assert point.passenger_count > 0  # senaryo GERÇEKTEN backlog üretmiş olmalı


@pytest.mark.parametrize("process", [
    PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL,
    PROCESS_PASSPORT_DEPARTURE, PROCESS_PASSPORT_ARRIVAL,
])
def test_no_hour_boundary_reset_for_any_event_driven_process(process):
    """Bölüm 11/15/16 - backlog saat sınırını (ör. 16:55 -> 17:00)
    geçiyorsa, display wait ANİDEN 0'a düşmemeli - `_remaining_wait_at_
    checkpoint`'in kendisi saat/bucket kavramından tamamen bağımsız
    olduğu için bu HER sürecte aynı şekilde garanti olmalı."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=2000)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")
    base = datetime(2026, 3, 10, 6, 0)

    if process == PROCESS_SECURITY_DOMESTIC:
        config.domestic_security_lane_count = 1
        flight = make_departure(when=base, location="domestic", aircraft_icao="A321")
    elif process == PROCESS_PASSPORT_DEPARTURE:
        config.passport_departure_server_count = 1
        flight = make_departure(when=base, location="international", requires_passport=True, aircraft_icao="A321")
    elif process == PROCESS_SECURITY_INTL:
        config.international_security_lane_count = 1
        flight = make_departure(when=base, location="international", requires_passport=True, aircraft_icao="A321")
    else:
        config.passport_arrival_server_count = 1
        flight = make_arrival(when=base, location="international", requires_passport=True, aircraft_icao="A321")

    now = datetime(2026, 3, 10, 23, 0)
    series = event_driven_display_series([flight], config, demand, now=now)
    points = {p.window_start: p for p in series[process]}

    before_boundary = points[datetime(2026, 3, 10, 16, 55)]
    after_boundary = points[datetime(2026, 3, 10, 17, 0)]

    assert before_boundary.passenger_count > 0  # senaryo bu noktada hâlâ backlog üretmiş olmalı
    assert before_boundary.wait_minutes > 0
    # Saat sınırını geçtikten HEMEN sonra da backlog hâlâ sürüyorsa (yeni
    # arrival gelmese bile) wait ANİDEN 0'a düşmemeli.
    assert after_boundary.passenger_count > 0
    assert after_boundary.wait_minutes > 0
    # Reset değil, doğal (küçük) bir azalma/artış bekleniyor - büyüklük
    # mertebesi aynı kalmalı (10 kata sıçrama/çökme YOK).
    assert 0.1 < (after_boundary.wait_minutes / before_boundary.wait_minutes) < 2.0


def test_passport_arr_display_does_not_equal_hourly_arrival_experienced_wait():
    """Bölüm 6/7 - regresyon kilidi: `passport_arr` display metriği
    (current_queue_weighted_remaining_wait) ile saatlik `_bucket_weighted_
    wait` (hourly_arrival_experienced_wait, `queue_predictions.estimated_
    wait_minutes`'in kaynağı) KASITLI OLARAK FARKLI iki metrik - backlog
    büyürken biri diğerinden yapısal olarak farklı çıkmalı (aynı koda
    yanlışlıkla birleştirilmediğinin kanıtı)."""
    from app.queue.engine import _event_driven_queue_demand

    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=2000)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")
    config.passport_arrival_server_count = 1
    flight = make_arrival(when=datetime(2026, 3, 10, 6, 0), location="international", requires_passport=True, aircraft_icao="A321")

    now = datetime(2026, 3, 10, 20, 0)
    coupling = _event_driven_queue_demand([flight], config, demand, now=now)
    events = coupling["process_events"][PROCESS_PASSPORT_ARRIVAL]

    # Bu senaryoda TÜM release cohort'ları (yeni arrival'lar) 06:10-06:34
    # arası tetikleniyor (flight'ın KENDİSİ 06:00'da) - `_bucket_weighted_
    # wait` bu yüzden SADECE 06:00 saatlik bucket'ında veri taşır; 14:00
    # checkpoint'inde ise (backlog hâlâ sürerken) "hâlâ bekleyenlerin
    # kalan süresi" ölçülüyor - iki metrik FARKLI zaman eksenlerinde.
    checkpoint = datetime(2026, 3, 10, 14, 0)
    remaining_avg, _, _ = _remaining_wait_at_checkpoint(events, checkpoint)

    # `_bucket_weighted_wait` `_event_driven_queue_demand()` içinde iç
    # (nested) bir fonksiyon - dışarıdan import edilemez, ama ürettiği
    # sonuç zaten `coupling["event_wait_by_hour"]` üzerinden dışa açık.
    hourly = coupling["event_wait_by_hour"][PROCESS_PASSPORT_ARRIVAL]
    hourly_06 = hourly.get(datetime(2026, 3, 10, 6, 0))

    assert remaining_avg > 0
    assert hourly_06 is not None
    # İki metrik FARKLI şeyler ölçtüğü (checkpoint-remaining-wait vs
    # hourly-arrival-experienced-wait) ve FARKLI zaman eksenlerinde
    # tanımlı olduğu için sayısal olarak eşit OLMAMALI.
    assert remaining_avg != pytest.approx(hourly_06, rel=1e-6)


def test_display_series_does_not_mutate_hourly_prediction():
    """Grafik-only çağrı `predict_airport()`'un (resmi/saatlik) sonucunu HİÇ DEĞİŞTİRMEZ."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")
    flight = make_departure(when=datetime(2026, 3, 10, 9, 30), aircraft_icao="A321")

    before = predict_airport("IST", [flight], config, demand)
    event_driven_display_series([flight], config, demand, now=datetime(2026, 3, 10, 10, 0))
    after = predict_airport("IST", [flight], config, demand)

    key = lambda p: (p.process, p.window_start)
    assert {key(p): p.expected_passengers for p in before} == {key(p): p.expected_passengers for p in after}
    assert {key(p): p.estimated_wait_minutes for p in before} == {key(p): p.estimated_wait_minutes for p in after}
    assert {key(p): p.risk for p in before} == {key(p): p.risk for p in after}


def test_full_day_has_288_points_per_process_no_gaps_no_interpolation():
    """Bölüm 24-10 - 24h/5dk=288 nokta, sahte smoothing/interpolasyon YOK
    (her nokta KENDİ checkpoint'inin gerçek hesabı)."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")
    flight = make_departure(when=datetime(2026, 3, 10, 9, 30), aircraft_icao="A321")

    now = datetime(2026, 3, 10, 23, 0)
    series = event_driven_display_series([flight], config, demand, now=now)

    for process in DISPLAY_5M_PROCESSES:
        points = series[process]
        assert len(points) == 288
        expected_start = datetime(2026, 3, 10, 0, 0)
        assert points[0].window_start == expected_start
        assert points[1].window_start == expected_start + timedelta(minutes=5)
        assert points[-1].window_start == expected_start + timedelta(hours=23, minutes=55)


def test_truly_empty_queue_gives_zero():
    """Bölüm 24-7 - gerçekten kimse beklemiyorsa (uzak, sakin bir saat) 0."""
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=180)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")
    flight = make_departure(when=datetime(2026, 3, 10, 12, 0), location="domestic", aircraft_icao="A321")

    now = datetime(2026, 3, 10, 12, 0)
    series = event_driven_display_series([flight], config, demand, now=now)
    points = {p.window_start: p for p in series[PROCESS_SECURITY_DOMESTIC]}

    far_before = datetime(2026, 3, 10, 0, 0)
    assert far_before in points
    assert points[far_before].wait_minutes == 0.0
    assert points[far_before].passenger_count == 0.0


def test_backlog_keeps_nonzero_wait_after_new_arrivals_stop_end_to_end():
    """
    Bölüm 24-6/11 - `event_driven_display_series()` seviyesinde uçtan uca:
    ağır yüklü, tek lane'li bir domestic security senaryosunda, show-up
    penceresi bitip yeni arrival kalmadıktan SONRAKİ checkpoint'lerde bile
    (backlog hâlâ sürerken) wait ANİDEN 0'a düşmemeli.
    """
    resolver = FakeResolver({"A321": FakeCapacityResult(capacity=300)})
    demand = DemandCalculator(resolver)
    config = default_config("IST")
    config.domestic_security_lane_count = 1  # aşırı yüklü, backlog uzun sürsün
    flight = make_departure(when=datetime(2026, 3, 10, 12, 0), location="domestic", aircraft_icao="A321")

    now = datetime(2026, 3, 10, 13, 0)
    series = event_driven_display_series([flight], config, demand, now=now)
    points = {p.window_start: p for p in series[PROCESS_SECURITY_DOMESTIC]}

    # Show-up penceresi (T-240..T-0, yani 08:00-12:00) bittikten hemen
    # sonraki checkpoint'lerde hâlâ backlog var mı diye bak - varsa wait>0
    # olmalı (0'a ani düşüş YOK).
    after_showup = [t for t in sorted(points) if datetime(2026, 3, 10, 12, 0) < t <= datetime(2026, 3, 10, 12, 30)]
    nonzero_after = [points[t].wait_minutes for t in after_showup if points[t].passenger_count > 0]
    assert nonzero_after  # backlog GERÇEKTEN devam ediyor olmalı (1 lane ile 300 kapasiteli uçak)
    assert all(w > 0 for w in nonzero_after)


# ---------------------------------------------------------------------------
# `five_minute_wait_series` - eski arrival-window yardımcı, KORUNDU
# (ana display'in kaynağı DEĞİL artık, SQL-audit için hâlâ doğru/geçerli)
# ---------------------------------------------------------------------------

def test_five_minute_series_matches_hand_built_service_events():
    base = datetime(2026, 3, 10, 9, 0)
    events = [
        ServiceEvent("x", 2.0, base, base, base),
        ServiceEvent("x", 3.0, base, base + timedelta(minutes=4), base + timedelta(minutes=4)),
        ServiceEvent("x", 1.0, base + timedelta(minutes=10), base + timedelta(minutes=20), base + timedelta(minutes=20)),
    ]
    series = five_minute_wait_series(events, window_minutes=5)
    assert series == [
        (base, 2.4, 5.0, 12.0),
        (base + timedelta(minutes=10), 10.0, 1.0, 10.0),
    ]


def test_arrival_time_bucket_policy_not_service_start_or_completion():
    arrival = datetime(2026, 3, 10, 9, 3)
    service_start = datetime(2026, 3, 10, 9, 23)
    completion = datetime(2026, 3, 10, 9, 24)
    events = [ServiceEvent("x", 10.0, arrival, service_start, completion)]
    series = five_minute_wait_series(events, window_minutes=5)
    assert len(series) == 1
    window_start, avg_wait, pax, numerator = series[0]
    assert window_start == datetime(2026, 3, 10, 9, 0)
    assert avg_wait == 20.0
    assert pax == 10.0
    assert numerator == 200.0
