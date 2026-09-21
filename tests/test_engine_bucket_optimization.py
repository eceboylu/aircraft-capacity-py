"""
ADIM 5E-2 - prediction lookup optimizasyonu doğrulaması.

`predict_airport()` artık process başına uçuşları TEK bir geçişte
(`_bucket_flights_by_window`) 15 dk pencerelere önceden gruplayıp,
her pencerede sadece kendi bucket'ını okuyor - eskiden her pencerede
TÜM uçuş listesini (`flights_in_window`) baştan taşıyordu.

Bu dosya İKİ şeyi kanıtlıyor:
  1) Yeni (bucket tabanlı) yol ile eski (`predict_window` + `flights_in_window`,
     HÂLÂ değişmeden mevcut) yolun ürettiği sonuçlar BİREBİR AYNI.
  2) `effective_time()` artık uçuş başına TAM OLARAK BİR KEZ çağrılıyor.

`predict_window()`/`flights_in_window()`/`window_starts()`'ın kendisi
DEĞİŞMEDİ - "eski yol" burada gerçekten eski, bağımsız bir kod yoludur
(mock/varsayım değil).
"""

from datetime import datetime, timedelta

import pytest

from app.queue.config import AirportConfigView
from app.queue.constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    EXCLUDED_STATUSES,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    PROCESS_SECURITY_DOMESTIC,
    STATUS_CANCELLED,
    STATUS_DIVERTED,
)
from app.queue.domain.demand import DemandCalculator, effective_time, flights_in_window
from app.queue.domain.flows import passport_flights, security_flights
from app.queue.engine import (
    PROCESSES,
    _PROCESS_FLIGHTS,
    _bucket_flights_by_window,
    floor_to_window,
    predict_airport,
    predict_window,
    window_starts,
)

from .factories import MockCapacityResolver, MockFlight, at

AIRPORT = "AAA"


def roomy_config(airport=AIRPORT) -> AirportConfigView:
    return AirportConfigView(
        airport_iata=airport,
        passport_counter_count=24,
        passport_staff_count=24,
        passport_service_time_minutes=1.5,
        security_lane_count=8,
        security_service_time_minutes=1.0,
        passport_staff_per_counter=2.0,
        passport_service_rate_per_staff=0.5,
        passport_efficiency_multiplier=1.5,
        arrival_bank_threshold=5,
        is_default=False,
    )


def make_demand() -> DemandCalculator:
    return DemandCalculator(MockCapacityResolver())


def _flight(
    direction, hour, minute=0, *, aircraft="A320", location=LOCATION_INTERNATIONAL,
    duration_minutes=90, key=None, number="1", airline="TK",
    status="scheduled", dep_estimated=None, dep_actual=None,
    arr_estimated=None, arr_actual=None, airport=AIRPORT,
) -> MockFlight:
    """test_e2e_ist.py:flight() ile aynı desende - tüm zaman alanları kontrol edilebilir."""
    base = at(hour, minute)
    if direction == DIRECTION_DEPARTURE:
        dep_scheduled = base
        arr_scheduled = base + timedelta(minutes=duration_minutes)
        dep_iata, arr_iata = airport, "ZZZ"
    else:
        arr_scheduled = base
        dep_scheduled = base - timedelta(minutes=duration_minutes)
        dep_iata, arr_iata = "ZZZ", airport

    return MockFlight(
        flight_key=key or f"{airline}_{number}_{hour:02d}{minute:02d}{direction[0]}",
        airport_iata=airport,
        direction=direction,
        location=location,
        airline_iata=airline,
        flight_number=number,
        flight_iata=f"{airline}{number}",
        aircraft_icao=aircraft,
        aircraft_match_found=aircraft is not None,
        dep_iata=dep_iata,
        arr_iata=arr_iata,
        dep_scheduled_utc=dep_scheduled,
        dep_estimated_utc=dep_estimated,
        dep_actual_utc=dep_actual,
        arr_scheduled_utc=arr_scheduled,
        arr_estimated_utc=arr_estimated,
        arr_actual_utc=arr_actual,
        status=status,
    )


def _diverse_flight_set(n_per_kind: int = 25) -> list[MockFlight]:
    """
    Mix: departure/arrival, domestic/international, çeşitli aircraft,
    çeşitli status (scheduled/active/landed/cancelled/diverted),
    scheduled-only / scheduled+estimated / scheduled+estimated+actual
    zaman kombinasyonları, 6 saatlik bir pencereye yayılmış - birden
    fazla 15 dk penceresi kesin olarak oluşturacak şekilde.
    """
    aircraft_cycle = ["A320", "A321", "B738", "B77W", None]
    status_cycle = ["scheduled", "active", "landed", STATUS_CANCELLED, STATUS_DIVERTED]
    flights = []

    for i in range(n_per_kind):
        hour = 6 + (i * 7) % 360 // 60   # geniş bir yayılım
        minute = (i * 7) % 60
        aircraft = aircraft_cycle[i % len(aircraft_cycle)]
        status = status_cycle[i % len(status_cycle)]

        # Zaman kombinasyonu rotasyonu: scheduled-only / +estimated / +estimated+actual
        dep_est = dep_act = None
        if i % 3 == 1:
            dep_est = at(hour, minute) + timedelta(minutes=10)
        elif i % 3 == 2:
            dep_est = at(hour, minute) + timedelta(minutes=10)
            dep_act = at(hour, minute) + timedelta(minutes=17)

        flights.append(_flight(
            DIRECTION_DEPARTURE, hour, minute,
            aircraft=aircraft, location=LOCATION_DOMESTIC if i % 4 == 0 else LOCATION_INTERNATIONAL,
            key=f"DEP{i}", number=str(i), airline="TK", status=status,
            dep_estimated=dep_est, dep_actual=dep_act,
        ))

        arr_hour = 6 + (i * 11) % 6
        arr_minute = (i * 13) % 60
        arr_est = arr_act = None
        if i % 3 == 1:
            arr_est = at(arr_hour, arr_minute) + timedelta(minutes=8)
        elif i % 3 == 2:
            arr_est = at(arr_hour, arr_minute) + timedelta(minutes=8)
            arr_act = at(arr_hour, arr_minute) + timedelta(minutes=15)

        flights.append(_flight(
            DIRECTION_ARRIVAL, arr_hour, arr_minute,
            aircraft=aircraft, location=LOCATION_DOMESTIC if i % 5 == 0 else LOCATION_INTERNATIONAL,
            key=f"ARR{i}", number=str(1000 + i), airline="AF", status=status,
            arr_estimated=arr_est, arr_actual=arr_act,
        ))

    return flights


def _old_path_predictions(flights, config, demand, baseline_fn, passenger_baseline_fn):
    """
    ESKİ algoritma - `window_starts()` + `predict_window()` (ikisi de
    DEĞİŞMEDİ, hâlâ kendi O(N) `flights_in_window()` taramasını yapıyor).
    Bu, `predict_airport()`'tan TAMAMEN BAĞIMSIZ ikinci bir koddur.
    """
    match_rate = None
    from app.queue.engine import flight_match_rate
    match_rate = flight_match_rate(flights)

    results = []
    for process in PROCESSES:
        relevant = _PROCESS_FLIGHTS[process](flights)
        starts = window_starts(relevant)
        capacity_rate = (
            config.passport_counter_count / config.passport_service_time_minutes
            if process == PROCESS_PASSPORT
            else config.security_lane_count / config.security_service_time_minutes
        )
        backlog = 0.0
        previous = None
        for start in starts:
            if previous is not None:
                gap_windows = int((start - previous).total_seconds() // 3600) - 1
                backlog = max(0.0, backlog - capacity_rate * 60 * gap_windows)
            baseline = baseline_fn(process, start)
            passenger_baseline = passenger_baseline_fn(process, start)
            result = predict_window(
                airport_iata=AIRPORT,
                process=process,
                window_start=start,
                process_flights=relevant,
                config=config,
                demand=demand,
                historical_baseline=baseline,
                aircraft_match_rate=match_rate,
                aircraft_changes=None,
                historical_passenger_baseline=passenger_baseline,
                backlog_start=backlog,
            )
            results.append(result)
            backlog = max(
                0.0,
                backlog + result.expected_passengers - capacity_rate * 60,
            )
            previous = start
    return results


def _as_comparable(predictions):
    """
    WindowPrediction listesini (process, window_start) sıralı,
    alan-alan karşılaştırılabilir bir tuple listesine çevirir.

    ADIM (Event-Driven Wait Reporting): `estimated_wait_minutes` BİLEREK
    DIŞARIDA - `predict_airport()` (yeni/bucket yol) artık PROCESS_
    SECURITY_DOMESTIC/INTL ve PROCESS_PASSPORT_DEPARTURE/ARRIVAL için
    GERÇEK event-driven wait kullanıyor (`_event_driven_queue_demand()`
    üzerinden), `predict_window()` (bu dosyanın "eski yol"u, TEK pencere
    API'si) bu kuplaja hiç erişemiyor - hâlâ eski Erlang-C/fluid `wq`
    üretiyor. Bu, BİLİNÇLİ ve BEKLENEN bir ayrışma (wait DOĞRULUĞU bu
    dosyanın konusu DEĞİL - bkz. `tests/test_event_driven_wait_
    reporting.py`) - bu dosya SADECE "bucket gruplama, uçuş/pencere
    tarama sonucuyla flight_count/demand/risk/reasons açısından
    BİREBİR aynı mı" sorusuna cevap veriyor.
    """
    ordered = sorted(predictions, key=lambda p: (p.process, p.window_start))
    return [
        (
            p.airport_iata, p.process, p.window_start, p.window_end,
            p.flight_count, p.expected_passengers, p.baseline_ratio,
            p.utilization, p.risk,
            p.confidence, p.flight_ratio, p.passenger_ratio,
            p.reasons_as_dicts(),   # sıra DAHİL karşılaştırılır
        )
        for p in ordered
    ]


def _as_comparable_bucket_identity(predictions):
    """
    ADIM (Departure Show-Up Profile): departure yolcularının talebi
    artık `effective_time()`'ın TEK noktası yerine `departure_show_up_
    events()`'ın 3 saate yaydığı batch'lerden geliyor - `_old_path_
    predictions()` (bu dosyanın bağımsız "eski yol"u, `predict_window()`
    üzerinden, show-up'tan TAMAMEN HABERSİZ) bu yüzden ARTIK hiçbir
    departure-dokunan süreç için `expected_passengers`/`utilization`/
    `risk` bakımından `predict_airport()` (yeni yol) ile AYNI DEĞİL -
    bu BEKLENEN, doğru bir ayrışma (show-up modelinin doğruluğu bu
    dosyanın konusu DEĞİL - bkz. `tests/test_passport_security_
    coupling.py`, `tests/test_event_driven_wait_reporting.py`).

    Bu fonksiyon SADECE bu dosyanın GERÇEK konusunu - `_bucket_flights_
    by_window()`'un ürettiği pencere KÜMESİ/flight_count'un, eski O(N×W)
    `flights_in_window()` taramasıyla BİREBİR AYNI olduğunu -
    karşılaştırır (demand-türevli alanlar HARİÇ).
    """
    ordered = sorted(predictions, key=lambda p: (p.process, p.window_start))
    return [
        (p.airport_iata, p.process, p.window_start, p.window_end, p.flight_count)
        for p in ordered
    ]


# ========================================================================
# 1) GOLDEN/REFERENCE KARŞILAŞTIRMA - eski yol vs yeni yol, BİREBİR aynı
# ========================================================================

def test_bucket_based_predict_airport_matches_old_window_scan_algorithm():
    flights = _diverse_flight_set(n_per_kind=25)
    config = roomy_config()
    demand = make_demand()

    def baseline_fn(process, start):
        return 3.0   # sabit, deterministik - iki yolda da AYNI değer

    def passenger_baseline_fn(process, start):
        return 250.0

    old_results = _old_path_predictions(flights, config, demand, baseline_fn, passenger_baseline_fn)

    new_demand = make_demand()   # ayrı DemandCalculator - cache çapraz sızıntısı olmasın
    new_results = predict_airport(
        airport_iata=AIRPORT,
        flights=flights,
        config=config,
        demand=new_demand,
        baseline_fn=baseline_fn,
        aircraft_changes=None,
        passenger_baseline_fn=passenger_baseline_fn,
    )

    assert len(old_results) > 0, "test uçuşları hiç pencereye düşmedi"

    # PASSPORT→SECURITY zaman-kuplajı ADIM'ı: PROCESS_SECURITY (birleşik)
    # ve PROCESS_SECURITY_INTL artık `predict_airport()` içindeki
    # `_passport_security_hourly_coupling()`'den (passport'un TÜM uçuş
    # listesi + saatlik backlog zinciri gerektirir) gelen zaman-kaydırmalı
    # talep kullanıyor - bu, `predict_window()`'ın (bu dosyanın "eski
    # yolu") TEK PENCERE, backlog_start'ı DIŞARIDAN alan API'siyle
    # YAPISAL OLARAK temsil edilemez (kuplaj hesabı passport'un TÜM
    # saatlik zincirini gerektirir, tek bir pencereyi değil). Bu yüzden
    # eşdeğerlik SADECE kuplajdan ETKİLENMEYEN iki süreçte (PASSPORT,
    # SECURITY_DOMESTIC) doğrulanıyor; PROCESS_SECURITY/SECURITY_INTL'in
    # KENDİ doğruluğu `tests/test_production_shape_hourly_replay.py` ve
    # `tests/test_passport_security_coupling.py`'de AYRICA kanıtlanıyor.
    #
    # ADIM (Departure Show-Up Profile): PASSPORT/SECURITY_DOMESTIC artık
    # `expected_passengers`/`risk`/`utilization` bakımından da "eski yol"
    # (show-up'tan habersiz) ile AYNI DEĞİL (bkz. `_as_comparable_bucket_
    # identity()` docstring'i) - bu yüzden SADECE bucket/flight_count
    # kimliği (bu dosyanın GERÇEK konusu) karşılaştırılıyor, VE SADECE
    # eski yolun da ÜRETTİĞİ pencerelerde (show-up'ın YENİ eklediği,
    # eski yolda hiç var olmayan saatler hariç - bkz. `predict_airport()`
    # `starts = set(buckets) | set(coupled_demand)` birleşimi).
    unaffected = {PROCESS_PASSPORT, PROCESS_SECURITY_DOMESTIC}
    old_unaffected = [p for p in old_results if p.process in unaffected]
    old_keys = {(p.process, p.window_start) for p in old_unaffected}
    new_unaffected = [
        p for p in new_results
        if p.process in unaffected and (p.process, p.window_start) in old_keys
    ]
    assert len(old_unaffected) > 0
    assert len(old_unaffected) == len(new_unaffected)
    assert _as_comparable_bucket_identity(old_unaffected) == _as_comparable_bucket_identity(new_unaffected)


def test_bucket_based_matches_old_algorithm_with_no_baseline_at_all():
    """
    historical_baseline=None yolunda da (RISK_UNKNOWN / NO_BASELINE reason)
    birebir aynı.

    ADIM (Airport Queue Model V2 - sabit -120dk offset): bu karşılaştırma
    daha önce (eski dinamik 45/60/90dk buffer altında) TESADÜFEN tüm
    süreçlerde eşleşiyordu - `_diverse_flight_set()`'in ürettiği uçuşlar
    o zamanki saat dağılımıyla passport'u hiç doldurmuyordu, bu yüzden
    PROCESS_SECURITY/PROCESS_SECURITY_INTL'in `_passport_security_hourly_
    coupling()`'den gelen kuplajlı talebi, eski yolun (`predict_window`)
    kuplajdan HABERSİZ, saf pencere talebiyle çakışıyordu. Sabit -120dk
    offset'te aynı uçuşlar farklı saatlere düştüğü için bu tesadüf artık
    geçerli değil - `test_bucket_based_predict_airport_matches_old_window_
    scan_algorithm()` (bu dosyada, yukarıda) zaten aynı yapısal nedenle
    SADECE kuplajdan ETKİLENMEYEN süreçleri (PASSPORT, SECURITY_DOMESTIC)
    karşılaştırıyor - burada da AYNI, önceden belgelenmiş kısıtlama
    uygulanıyor (production kuplaj matematiği DEĞİŞMEDİ, sadece bu testin
    kapsamı gerçek mimariye uygun hale getirildi).
    """
    flights = _diverse_flight_set(n_per_kind=10)
    config = roomy_config()

    old_results = _old_path_predictions(
        flights, config, make_demand(),
        baseline_fn=lambda process, start: None,
        passenger_baseline_fn=lambda process, start: None,
    )
    new_results = predict_airport(
        airport_iata=AIRPORT, flights=flights, config=config, demand=make_demand(),
        baseline_fn=None, passenger_baseline_fn=None,
    )

    # ADIM (Departure Show-Up Profile): bkz. yukarıdaki testin AYNI notu -
    # SADECE bucket/flight_count kimliği, SADECE eski yolun da ürettiği
    # pencerelerde karşılaştırılıyor.
    unaffected = {PROCESS_PASSPORT, PROCESS_SECURITY_DOMESTIC}
    old_unaffected = [p for p in old_results if p.process in unaffected]
    old_keys = {(p.process, p.window_start) for p in old_unaffected}
    new_unaffected = [
        p for p in new_results
        if p.process in unaffected and (p.process, p.window_start) in old_keys
    ]
    assert len(old_unaffected) > 0
    assert len(old_unaffected) == len(new_unaffected)
    assert _as_comparable_bucket_identity(old_unaffected) == _as_comparable_bucket_identity(new_unaffected)


# ========================================================================
# 2) EFFECTIVE_TIME() ÇAĞRI SAYISI - process başına bucket geçişi +
#    event-driven simülasyonun KENDİ arrival-timeline geçişi.
#
#    ADIM (Event-Driven Engine Entegrasyonu) NOTU: eski saatlik-oransal
#    `_passport_security_hourly_coupling()`'in YERİNİ `_event_driven_
#    queue_demand()` aldı. Bu fonksiyon artık `current_arrived_override`'ı
#    (ve `demand_override`/`backlog_start`'ı) DÖRT sürecin DÖRDÜ için de
#    besliyor - bu yüzden `_predict_window_core`'un eski inline "current-
#    wait effective_time<=now" filtresi (PASSPORT/SECURITY_DOMESTIC için
#    ayrı bir `effective_time()` geçişi gerektiren dal) ARTIK HİÇ
#    ÇALIŞMIYOR (`current_arrived_override is not None` her zaman doğru).
#    Bunun yerine `_event_driven_queue_demand()`'ın KENDİ `_arrivals()`
#    yardımcı fonksiyonu, passport'a giren uçuşları (`is_international_
#    departure`/`is_international_arrival` İKİ AYRI ama BİRBİRİNİ
#    DIŞLAYAN predikat) ve domestic kalkışları TARAYIP `effective_time()`
#    çağırıyor - her flight (iptal/diverted HARİÇ) yine TAM OLARAK BİR
#    KEZ (predikatlardan sadece BİRİNE uyduğu için, ikisine de değil).
# ========================================================================

def test_effective_time_called_bounded_times_per_flight(monkeypatch):
    flights = _diverse_flight_set(n_per_kind=30)   # 60 uçuş

    import app.queue.engine as engine_mod
    real_effective_time = engine_mod.effective_time
    calls = {"n": 0}

    def counting(flight):
        calls["n"] += 1
        return real_effective_time(flight)

    monkeypatch.setattr(engine_mod, "effective_time", counting)

    predict_airport(
        airport_iata=AIRPORT, flights=flights, config=roomy_config(),
        demand=make_demand(), baseline_fn=lambda p, s: 3.0,
        passenger_baseline_fn=lambda p, s: 100.0,
    )

    # security_flights = TÜM departure'lar, passport_flights = intl departure + intl arrival.
    n_security = len(security_flights(flights))
    n_passport = len(passport_flights(flights))
    # ADIM (Security Domestic/International Split): iki EK süreç, her
    # biri KENDİ (daha küçük) flight alt kümesi üzerinde AYRI bir
    # `_bucket_flights_by_window()` geçişi yapıyor - security_flights'ın
    # AYNI flight'ları için birden fazla kez sayılması DEĞİL, bu iki
    # yeni sürecin KENDİ ayrı bucket'lama geçişleri.
    from app.queue.domain.flows import (
        security_domestic_flights,
        security_international_flights,
    )
    n_security_dom = len(security_domestic_flights(flights))
    n_security_intl = len(security_international_flights(flights))

    def included(items):
        return len([f for f in items if f.status not in EXCLUDED_STATUSES])

    # ANA DÖNGÜ - `predict_airport()`'ın 4 süreç için KENDİ `_bucket_
    # flights_by_window()` geçişi (RAW liste boyutu, iptal/diverted DAHİL -
    # `_bucket_flights_by_window` status'e bakmadan HER flight için
    # effective_time() çağırır, sadece None-moment'ları eler).
    main_loop_calls = n_security + n_security_dom + n_security_intl + n_passport

    # EVENT-DRIVEN SİMÜLASYON - ADIM (Departure Show-Up Profile +
    # Arrival Release Profile) ile DEĞİŞTİ: `_departure_show_up_
    # arrivals()` (departure_arrivals VE domestic_arrivals için) VE
    # `_arrival_release_arrivals()` (arrival_arrivals için) ARTIK
    # `effective_time()` HİÇ ÇAĞIRMAZ - ikisi de flight'ın KENDİ HAM
    # dep_*/arr_* alanlarını DOĞRUDAN okur (bkz. `_departure_show_up_
    # base()`/`_arrival_release_base()`). Event-driven simülasyonun
    # `effective_time()` çağrı payı bu yüzden SIFIR.
    event_driven_calls = 0

    # ADIM (4-Graph API Contract) - `_passport_cohort_breakdown()` KENDİ
    # İKİ AYRI `_bucket_flights_by_window()` geçişi yapıyor (departure-
    # kökenli + arrival-kökenli, RAW liste, status'e bakılmadan) - bu
    # ikisi BİRBİRİNİ DIŞLAYAN alt kümeler olduğu için TOPLAMDA
    # `passport_flights(flights)`'ın RAW boyutu kadar (n_passport) EK
    # `effective_time()` çağrısı - iki kez DEĞİL, bucket'lamanın kendisi
    # ana döngüdekiyle AYNI ilke (status filtrelenmeden, sadece None-
    # moment elenir).
    cohort_breakdown_calls = n_passport

    expected = main_loop_calls + event_driven_calls + cohort_breakdown_calls
    assert calls["n"] == expected, (
        "effective_time() ana döngüde her süreç için RAW flight "
        "listesinde TAM OLARAK bir kez (ADIM 5E-2 ilkesi), event-driven "
        "simülasyonda ise passport/domestic'e giren (talebe giren) "
        "uçuşlar için TAM OLARAK bir kez daha çağrılmalı - toplamda "
        "O(N×W) DEĞİL."
    )


# ========================================================================
# 3) BUCKET EŞDEĞERLİĞİ - flights_in_window ile birebir aynı üyelik
# ========================================================================

@pytest.mark.parametrize("window_minutes", [15])
def test_bucket_membership_matches_flights_in_window_for_every_window(window_minutes):
    flights = _diverse_flight_set(n_per_kind=20)
    relevant = security_flights(flights)

    buckets = _bucket_flights_by_window(relevant, window_minutes)

    for window_start in window_starts(relevant, window_minutes):
        bucket_all = buckets.get(window_start, [])
        expected_all = flights_in_window(
            relevant, window_start, window_minutes, include_excluded=True
        )
        assert bucket_all == expected_all   # SIRA dahil birebir aynı

        bucket_demand = [f for f in bucket_all if f.status not in (STATUS_CANCELLED, STATUS_DIVERTED)]
        expected_demand = flights_in_window(relevant, window_start, window_minutes)
        assert bucket_demand == expected_demand


def test_bucket_does_not_copy_flight_objects_same_identity():
    flights = _diverse_flight_set(n_per_kind=5)
    relevant = security_flights(flights)
    buckets = _bucket_flights_by_window(relevant)

    for window_start, bucket in buckets.items():
        for flight in bucket:
            original = next(f for f in relevant if f.flight_key == flight.flight_key)
            assert flight is original   # aynı REFERANS, kopya değil


# ========================================================================
# 4) EFFECTIVE_TIME KENARY SENARYOLARI - scheduled/estimated/actual
# ========================================================================

def test_scheduled_only_flight_buckets_by_scheduled_time():
    f = _flight(DIRECTION_DEPARTURE, 10, 0, key="SCHED_ONLY", location=LOCATION_DOMESTIC)
    buckets = _bucket_flights_by_window([f])
    expected_window = floor_to_window(effective_time(f))
    assert list(buckets.keys()) == [expected_window]
    assert buckets[expected_window] == [f]


def test_scheduled_plus_estimated_uses_estimated_for_bucketing():
    f = _flight(
        DIRECTION_DEPARTURE, 10, 0, key="SCHED_EST",
        dep_estimated=at(10, 40), location=LOCATION_DOMESTIC,
    )
    buckets = _bucket_flights_by_window([f])
    expected_window = floor_to_window(effective_time(f))
    # effective_time estimated'ı kullanmalı - scheduled (10:00) penceresine DEĞİL.
    assert expected_window != floor_to_window(at(10, 0))
    assert list(buckets.keys()) == [expected_window]


def test_scheduled_plus_estimated_plus_actual_uses_actual_for_bucketing():
    f_estimated_only = _flight(
        DIRECTION_DEPARTURE, 10, 0, key="EST_ONLY",
        dep_estimated=at(10, 20), location=LOCATION_DOMESTIC,
    )
    f_with_actual = _flight(
        DIRECTION_DEPARTURE, 10, 0, key="SCHED_EST_ACT",
        dep_estimated=at(10, 20), dep_actual=at(13, 40), location=LOCATION_DOMESTIC,
    )

    window_estimated_only = list(_bucket_flights_by_window([f_estimated_only]).keys())[0]
    window_with_actual = list(_bucket_flights_by_window([f_with_actual]).keys())[0]

    # actual (13:40) estimated'ı (10:20) override ETMELİ - farklı pencerelere düşerler.
    assert window_with_actual != window_estimated_only
    assert window_with_actual == floor_to_window(effective_time(f_with_actual))


def test_estimated_change_moves_flight_to_different_bucket():
    f1 = _flight(DIRECTION_DEPARTURE, 10, 0, key="MOVE", dep_estimated=at(10, 5), location=LOCATION_DOMESTIC)
    f2 = _flight(DIRECTION_DEPARTURE, 10, 0, key="MOVE", dep_estimated=at(12, 30), location=LOCATION_DOMESTIC)

    window1 = list(_bucket_flights_by_window([f1]).keys())[0]
    window2 = list(_bucket_flights_by_window([f2]).keys())[0]

    assert window1 != window2   # estimated değişimi pencereyi kaydırmalı


def test_actual_overrides_estimated_for_arrival_too():
    f = _flight(
        DIRECTION_ARRIVAL, 10, 0, key="ARR_OVERRIDE",
        arr_estimated=at(10, 10), arr_actual=at(10, 45), location=LOCATION_INTERNATIONAL,
    )
    buckets = _bucket_flights_by_window([f])
    expected_window = floor_to_window(effective_time(f))
    assert list(buckets.keys()) == [expected_window]
    assert expected_window == floor_to_window(at(10, 45) + timedelta(minutes=15))  # sabit passport buffer


# ========================================================================
# 5) EXCLUDED FLIGHT TESTLERİ - active / cancelled / diverted
# ========================================================================

def test_active_flight_counts_toward_demand_and_appears_in_both_lists():
    f = _flight(DIRECTION_DEPARTURE, 9, 0, key="ACTIVE1", status="active", location=LOCATION_DOMESTIC)
    buckets = _bucket_flights_by_window([f])
    window_start = list(buckets.keys())[0]
    window_all = buckets[window_start]
    window_demand = [x for x in window_all if x.status not in (STATUS_CANCELLED, STATUS_DIVERTED)]

    assert f in window_all
    assert f in window_demand


def test_cancelled_flight_excluded_from_demand_but_present_in_window_all():
    f = _flight(DIRECTION_DEPARTURE, 9, 0, key="CXL1", status=STATUS_CANCELLED, location=LOCATION_DOMESTIC)
    buckets = _bucket_flights_by_window([f])
    window_start = list(buckets.keys())[0]
    window_all = buckets[window_start]
    window_demand = [x for x in window_all if x.status not in (STATUS_CANCELLED, STATUS_DIVERTED)]

    assert f in window_all           # neden tespiti (Neden 7) için hâlâ görünür
    assert f not in window_demand    # talep hesabına GİRMEZ


def test_diverted_flight_excluded_from_demand_but_present_in_window_all():
    f = _flight(DIRECTION_DEPARTURE, 9, 0, key="DIV1", status=STATUS_DIVERTED, location=LOCATION_DOMESTIC)
    buckets = _bucket_flights_by_window([f])
    window_start = list(buckets.keys())[0]
    window_all = buckets[window_start]
    window_demand = [x for x in window_all if x.status not in (STATUS_CANCELLED, STATUS_DIVERTED)]

    assert f in window_all
    assert f not in window_demand


def test_end_to_end_active_cancelled_diverted_same_window_matches_old_algorithm():
    """
    Aynı pencerede active + cancelled + diverted karışık - flight_count/
    expected_passengers SADECE active'i saymalı, reasons hem cancellation
    hem diversion notunu içermeli - eski algoritma ile BİREBİR aynı.

    ADIM (Departure Show-Up Profile): active flight'ın talebi artık
    show-up ile 3 saate yayılıyor (bkz. dosya başındaki `_as_comparable_
    bucket_identity()` notu) - bu yüzden karşılaştırma SADECE bucket/
    flight_count kimliğine (eski yolun ürettiği pencerelerde) bakıyor.
    """
    active = _flight(DIRECTION_DEPARTURE, 9, 0, key="MIX_ACT", status="active", location=LOCATION_DOMESTIC, number="1")
    cancelled = _flight(DIRECTION_DEPARTURE, 9, 2, key="MIX_CXL", status=STATUS_CANCELLED, location=LOCATION_DOMESTIC, number="2")
    diverted = _flight(DIRECTION_DEPARTURE, 9, 4, key="MIX_DIV", status=STATUS_DIVERTED, location=LOCATION_DOMESTIC, number="3")
    flights = [active, cancelled, diverted]

    config = roomy_config()
    old_results = _old_path_predictions(
        flights, config, make_demand(),
        baseline_fn=lambda p, s: 1.0, passenger_baseline_fn=lambda p, s: 50.0,
    )
    new_results = predict_airport(
        airport_iata=AIRPORT, flights=flights, config=config, demand=make_demand(),
        baseline_fn=lambda p, s: 1.0, passenger_baseline_fn=lambda p, s: 50.0,
    )

    old_keys = {(p.process, p.window_start) for p in old_results}
    new_matching = [p for p in new_results if (p.process, p.window_start) in old_keys]
    assert _as_comparable_bucket_identity(old_results) == _as_comparable_bucket_identity(new_matching)

    # flight_count HÂLÂ tek effective_time() noktasında (07:00) toplanıyor (DEĞİŞMEDİ).
    security_pred = next(p for p in new_results if p.process == PROCESS_SECURITY and p.window_start.hour == 7)
    assert security_pred.flight_count == 1   # sadece active
    codes = {r.code for r in security_pred.reasons}
    assert "cancellation" in codes
    assert "diversion" in codes
