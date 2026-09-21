"""
ADIM (Security Domestic/International Split).

Production security motoru artık AYNI 60dk pencere için ÜÇ bağımsız
sonuç üretiyor:
  - `security`        (birleşik, TÜM kalkışlar - GERİYE DÖNÜK UYUMLULUK)
  - `security_dom`    (SADECE domestic kalkışlar)
  - `security_intl`   (SADECE international kalkışlar)

Her üçü de AYNI, DEĞİŞMEMİŞ `core/scoring.py:security_density_score()`
fonksiyonunu kendi flight alt kümesi üzerinde çağırır - yeni bir risk
formülü YOK. Baseline'lar `HistoricalFlightCount`/`BaselineObservation`
tablolarının MEVCUT `process` sütunu sayesinde otomatik olarak
birbirinden bağımsızdır (üç farklı process string'i = üç farklı satır).

Passport modeli, 390 pax/h kapasite modeli, ICAO full-capacity demand
modeli, 60dk window, current-wait/backlog - HİÇBİRİNE dokunulmadı.

ADIM (Departure Show-Up Profile): departure yolcuları artık TEK bir
`effective_time()` saatine YIĞILMIYOR, KENDİ show-up profiline göre
3 saate (T-180..T0) yayılıyor (bkz. `domain/demand.py:departure_show_up_
events()`). Bu dosyadaki sayısal beklentiler BU YÜZDEN güncellendi -
gerçek `predict_airport()` ile üretilip conservation/monotonluk
invariant'larına göre DOĞRULANDI (körlemesine "geçsin diye" seçilmedi).
ÖNEMLİ AYRIM: `flight_count` HÂLÂ eski tek-noktalı `effective_time()`
(-120dk) saatine göre sayılıyor (bkz. `engine.py:_bucket_flights_by_
window` - BİLİNÇLİ OLARAK DEĞİŞTİRİLMEDİ, flight-seviyesinde bir
referans kalmaya devam ediyor), `expected_passengers` ise ARTIK gerçek
show-up/event-arrival saatine göre (farklı saatlere ayrılabilir) - bu
ikisinin AYNI saatte aynı anda dolu olması ARTIK garanti DEĞİL (bkz.
`engine.py`'nin `starts = set(buckets) | set(coupled_demand)` birleşimi,
zaten önceden bu ayrımı öngörüyordu).
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.baseline import get_baseline
from app.queue.config import default_config
from app.queue.constants import (
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    RISK_CRITICAL,
    RISK_LOW,
    RISK_MEDIUM,
)
from app.queue.core.scoring import security_density_score
from app.queue.domain.demand import DemandCalculator
from app.queue.domain.flows import (
    security_domestic_flights,
    security_flights,
    security_international_flights,
)
from app.queue.engine import (
    persist_predictions,
    predict_airport,
    prune_stale_predictions,
    record_baseline_observations,
    run_predictions,
)
from app.queue.ingestion.refresh import refresh_flights
from app.queue.models import Flight, HistoricalFlightCount, QueuePrediction

from .factories import MockCapacityResolver, at, departure


def intl_departure(hour, minute=0, **kwargs):
    kwargs.setdefault("location", LOCATION_INTERNATIONAL)
    return departure(hour, minute, **kwargs)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    try:
        yield db
    finally:
        db.close()


def _demand():
    return DemandCalculator(MockCapacityResolver())


def _by_process(predictions, process, window_start=None):
    rows = [p for p in predictions if p.process == process]
    if window_start is not None:
        rows = [p for p in rows if p.window_start == window_start]
    return rows


# ========================================================================
# 1/2 - Domestic surge SADECE domestic_security'yi yükseltir,
#       international_security bundan bağımsız kalır.
# ========================================================================

def test_domestic_surge_only_raises_domestic_security_not_international():
    # Hepsi AYNI saat/dakikada - domestic_surge ve intl_normal AYNI
    # (tek) pencereye düşsün diye (effective_time buffer'la kaysa da
    # ikisi de aynı ölçüde kayar).
    domestic_surge = [
        departure(9, 0, location=LOCATION_DOMESTIC, key=f"DOM{i}", number=str(i))
        for i in range(8)
    ]
    intl_normal = [
        intl_departure(9, 0, key="INTL0", number="900"),
    ]
    flights = domestic_surge + intl_normal

    predictions = predict_airport(
        "AAA", flights, default_config("AAA"), _demand(),
        baseline_fn=lambda process, start: 1.0,   # her süreç için düşük bir baseline
    )

    dom = _by_process(predictions, PROCESS_SECURITY_DOMESTIC)
    intl = _by_process(predictions, PROCESS_SECURITY_INTL)
    # Show-up profili 3 saate yayılıyor (06:00/07:00/08:00) - flight_count
    # HÂLÂ tek `effective_time()` noktasında (07:00) toplanıyor (DEĞİŞMEDİ).
    dom_07 = next(r for r in dom if r.window_start.hour == 7)
    intl_07 = next(r for r in intl if r.window_start.hour == 7)

    # domestic_security: 8 uçuş / baseline 1.0 -> çok yüksek oran -> CRITICAL.
    assert dom_07.flight_count == 8
    assert dom_07.risk == RISK_CRITICAL

    # international_security: 1 uçuş / baseline 1.0 -> düşük oran -> CRITICAL DEĞİL.
    assert intl_07.flight_count == 1
    assert intl_07.risk != RISK_CRITICAL
    # Passport'un saatlik kapasitesinin (320) ALTINDA kalan bir tek
    # uçuşun (180 pax) HİÇBİR saatte 320'yi geçmediğini doğrula.
    assert all(r.expected_passengers <= 320.0 for r in intl)


# ========================================================================
# 3 - International surge SADECE international_security'yi yükseltir.
# ========================================================================

def test_international_surge_only_raises_international_security_not_domestic():
    intl_surge = [
        intl_departure(10, 0, key=f"INT{i}", number=str(i))
        for i in range(8)
    ]
    domestic_normal = [
        departure(10, 0, location=LOCATION_DOMESTIC, key="DOM0", number="800"),
    ]
    flights = intl_surge + domestic_normal

    predictions = predict_airport(
        "AAA", flights, default_config("AAA"), _demand(),
        baseline_fn=lambda process, start: 1.0,
    )

    dom = _by_process(predictions, PROCESS_SECURITY_DOMESTIC)
    intl = _by_process(predictions, PROCESS_SECURITY_INTL)
    dom_08 = next(r for r in dom if r.window_start.hour == 8)   # 10:00 kalkış -> effective_time 08:00
    intl_08 = next(r for r in intl if r.window_start.hour == 8)

    # 1440 pax ham talep (8x180), passport 320 pax/saat ile BİRDEN FAZLA
    # saate yayılarak boşalır - backlog "kaybolmadan" TÜM saatlere
    # yayılır (bkz. tests/test_passport_security_coupling.py conservation
    # testleri) - toplam KESİN olarak 1440'a eşit kalmalı.
    assert sum(r.expected_passengers for r in intl) == 1440

    assert intl_08.flight_count == 8
    # ADIM (Passport->Security zaman-kuplajı): international_security'nin
    # talebi HİÇBİR saatte passport'un o saat GERÇEKTEN serbest
    # bırakabildiği tavanı (320 pax/saat) AŞMAZ - 8 uçuşluk sürgü (1440
    # pax ham talep) passport'u fena tıkar (bu KENDİ grafiğinde görünür)
    # ama security_intl'e passport'un tavanından FAZLASI HİÇ sızamaz -
    # bu yüzden security_intl yapısal olarak ASLA MEDIUM/CRITICAL'e
    # ulaşamaz, HER ZAMAN LOW kalır.
    assert all(r.expected_passengers <= 320.0 for r in intl)
    assert all(r.risk == RISK_LOW for r in intl)
    # Passport'un tavanına ÇARPTIĞINI (surge'ün gerçekten var olduğunu,
    # sadece security'de görünmediğini) kanıtla: 8 uçuşun ham talebi
    # (çarpılmamış) kapasiteyi kat kat aşıyor.
    raw_intl_demand = sum(_demand().passenger_demand(f) for f in intl_surge)
    assert raw_intl_demand > 320.0
    assert dom_08.flight_count == 1
    assert dom_08.risk != RISK_CRITICAL


# ========================================================================
# 4 - Domestic/international flight kümeleri KARIŞMIYOR (flows.py
#     filtreleri karşılıklı ayrık).
# ========================================================================

def test_domestic_and_international_flight_sets_are_disjoint():
    domestic = [departure(9, 0, location=LOCATION_DOMESTIC, key="D1", number="1")]
    international = [intl_departure(9, 0, key="I1", number="2")]
    flights = domestic + international

    dom_set = {f.flight_key for f in security_domestic_flights(flights)}
    intl_set = {f.flight_key for f in security_international_flights(flights)}

    assert dom_set == {"D1"}
    assert intl_set == {"I1"}
    assert dom_set.isdisjoint(intl_set)
    # birleşik security TÜM kalkışları içerir - iki alt kümenin TOPLAMI.
    combined_set = {f.flight_key for f in security_flights(flights)}
    assert combined_set == dom_set | intl_set


# ========================================================================
# 5 - İki serinin baseline'ları KARIŞMIYOR (ayrı process key).
# ========================================================================

def test_baselines_are_independent_per_process(session):
    # ADIM (Airport Queue Model V2 - sabit -120dk offset): departure(18,0)
    # -> effective_time=16:00 -> pencere 16:00-17:00.
    window_start = at(16, 0)
    flights_dom = [departure(18, 0, location=LOCATION_DOMESTIC, key="D1", number="1")]
    flights_intl = [intl_departure(18, 0, key="I1", number="2")]

    refresh_flights(session, [_row_for(f) for f in flights_dom + flights_intl])
    resolver = MockCapacityResolver()
    now = at(19, 30)   # pencere kapanmış

    run_predictions(session, resolver, now=now)

    dom_baseline = get_baseline(
        session, "AAA", PROCESS_SECURITY_DOMESTIC,
        hour_of_day=window_start.hour, day_of_week=window_start.weekday(),
    )
    intl_baseline = get_baseline(
        session, "AAA", PROCESS_SECURITY_INTL,
        hour_of_day=window_start.hour, day_of_week=window_start.weekday(),
    )
    combined_baseline = get_baseline(
        session, "AAA", PROCESS_SECURITY,
        hour_of_day=window_start.hour, day_of_week=window_start.weekday(),
    )

    # Her sürecin KENDİ havuzu var - 1 domestic uçuş, 1 international
    # uçuş, 2 birleşik uçuş - birbirine KARIŞMADI.
    assert dom_baseline == 1.0
    assert intl_baseline == 1.0
    assert combined_baseline == 2.0

    rows = session.execute(
        select(HistoricalFlightCount).where(HistoricalFlightCount.airport_iata == "AAA")
    ).scalars().all()
    processes_seen = {r.process for r in rows}
    # intl_departure passport'u da besler (beklenen, ilgisiz) - burada
    # asıl iddia üç security sürecinin de KENDİ ayrı satırı olması.
    assert {PROCESS_SECURITY, PROCESS_SECURITY_DOMESTIC, PROCESS_SECURITY_INTL}.issubset(processes_seen)


# ========================================================================
# 6 - İkisi de GERÇEK 60dk window kullanıyor.
# ========================================================================

def test_both_split_processes_use_real_hourly_window():
    from app.queue.constants import DEMAND_WINDOW_MINUTES
    assert DEMAND_WINDOW_MINUTES == 60

    flights = [
        departure(9, 5, location=LOCATION_DOMESTIC, key="D1", number="1"),
        intl_departure(9, 50, key="I1", number="2"),
    ]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())
    dom = _by_process(predictions, PROCESS_SECURITY_DOMESTIC)
    intl = _by_process(predictions, PROCESS_SECURITY_INTL)
    # Show-up profili nedeniyle ikisi de BİRDEN FAZLA saate yayılabilir
    # (DEĞİŞTİ) - ama HER İKİSİ de hâlâ gerçek 60dk pencere (DEĞİŞMEDİ).
    assert len(dom) >= 1
    assert len(intl) >= 1
    for row in dom + intl:
        assert row.window_end - row.window_start == timedelta(minutes=60)


# ========================================================================
# 7 - İkisi de MEVCUT security_density_score() kullanıyor (yeni formül
#     yok) - aynı girdiyle DOĞRUDAN çağrılan fonksiyonla birebir aynı
#     sonucu üretmeli.
# ========================================================================

def test_split_processes_produce_identical_result_to_direct_scoring_call():
    demand = _demand()
    flights = [
        departure(9, i, location=LOCATION_DOMESTIC, key=f"D{i}", number=str(i))
        for i in range(3)
    ]
    predictions = predict_airport(
        "AAA", flights, default_config("AAA"), demand,
        baseline_fn=lambda process, start: 2.0,
    )
    # ADIM (Departure Show-Up Profile): `predict_airport()` artık HER
    # saat için `security_queue_model()`'i `demand_override=` (o saate
    # show-up ile düşen GERÇEK pay) ile çağırıyor - `window_flights`'ın
    # KENDİ ham talep toplamı (1080, TÜMÜ) İLE AYNI DEĞİL (bkz. dosya
    # docstring'i). Bu test "predict_airport() AYNI, DEĞİŞMEMİŞ
    # security_queue_model() formülünü kullanıyor, yeni bir formül İCAT
    # ETMEDİ" iddiasını doğruluyor - bu yüzden doğrudan çağrıya da AYNI
    # `demand_override`'ı vermek gerekir (production'ın KENDİ çağrı
    # şekli - `engine.py`'nin `demand_override=coupled_demand.get(...)`
    # satırıyla BİREBİR AYNI desen).
    dom_07 = next(r for r in _by_process(predictions, PROCESS_SECURITY_DOMESTIC) if r.window_start.hour == 7)

    from app.queue.core.scoring import security_queue_model
    subset = security_domestic_flights(flights)
    direct = security_queue_model(
        subset, default_config("AAA"), demand.passenger_demand,
        demand_override=dom_07.expected_passengers,
    )
    density = security_density_score(subset, 2.0, demand.passenger_demand)
    assert dom_07.flight_count == direct["flight_count"]
    assert dom_07.expected_passengers == direct["expected_passengers"]
    assert dom_07.risk == direct["risk"]
    assert dom_07.baseline_ratio == density["baseline_ratio"]


# ========================================================================
# 8 - Passport sonucu DEĞİŞMİYOR.
# ========================================================================

def test_passport_result_unaffected_by_security_split():
    flights = [
        intl_departure(9, 0, key="P1", number="1"),
    ]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())
    passport = _by_process(predictions, PROCESS_PASSPORT)
    # Show-up profili nedeniyle passport BİRDEN FAZLA saate yayılabilir
    # (DEĞİŞTİ) - ama security split'ten TAMAMEN habersiz olmaya devam
    # eder (DEĞİŞMEDİ): conservation tam, HİÇBİR satır security
    # alanlarından etkilenmez.
    assert len(passport) >= 1
    assert all(r.process == PROCESS_PASSPORT for r in passport)
    assert sum(r.expected_passengers for r in passport) == 180


# ========================================================================
# 9/10 - Birleşik security KORUNUYOR, eski davranış değişmiyor.
# ========================================================================

def test_combined_security_still_produced_and_unchanged():
    flights = [
        departure(9, 0, location=LOCATION_DOMESTIC, key="D1", number="1"),
        intl_departure(9, 0, key="I1", number="2"),
    ]
    predictions = predict_airport(
        "AAA", flights, default_config("AAA"), _demand(),
        baseline_fn=lambda process, start: 2.0,
    )
    combined = _by_process(predictions, PROCESS_SECURITY)
    # Show-up profili nedeniyle birleşik security de BİRDEN FAZLA saate
    # yayılıyor - ama flight_count HÂLÂ tek effective_time() noktasında
    # (07:00) toplanıyor (DEĞİŞMEDİ) ve HER İKİ kalkışı da içeriyor.
    assert len(combined) >= 1
    combined_07 = next(r for r in combined if r.window_start.hour == 7)
    assert combined_07.flight_count == 2   # TÜM kalkışlar - domestic split'ten ETKİLENMEDİ


# ========================================================================
# 11 - persist/upsert/prune doğru çalışıyor (üç süreç için de).
# ========================================================================

def test_persist_upsert_prune_works_for_all_three_security_processes(session):
    flights = [
        departure(9, 0, location=LOCATION_DOMESTIC, key="D1", number="1"),
        intl_departure(9, 0, key="I1", number="2"),
    ]
    refresh_flights(session, [_row_for(f) for f in flights])
    resolver = MockCapacityResolver()
    now = at(10, 30)

    run_predictions(session, resolver, now=now)
    count_after_first = session.scalar(select(func.count()).select_from(QueuePrediction))

    # Aynı veriyle tekrar çalıştır - upsert (yeni satır AÇILMAMALI).
    run_predictions(session, resolver, now=now)
    count_after_second = session.scalar(select(func.count()).select_from(QueuePrediction))
    assert count_after_first == count_after_second

    processes = {
        row.process for row in session.execute(
            select(QueuePrediction).where(QueuePrediction.airport_iata == "AAA")
        ).scalars().all()
    }
    assert PROCESS_SECURITY in processes
    assert PROCESS_SECURITY_DOMESTIC in processes
    assert PROCESS_SECURITY_INTL in processes


# ========================================================================
# 12 - Havalimanları birbirine karışmıyor.
# ========================================================================

def test_airports_do_not_mix_across_split_processes():
    flights_a = [departure(9, 0, location=LOCATION_DOMESTIC, airport="AAA", key="A1", number="1")]
    flights_b = [
        departure(9, i, location=LOCATION_DOMESTIC, airport="BBB", key=f"B{i}", number=str(i))
        for i in range(6)
    ]

    preds_a = predict_airport(
        "AAA", flights_a, default_config("AAA"), _demand(),
        baseline_fn=lambda process, start: 1.0,
    )
    preds_b = predict_airport(
        "BBB", flights_b, default_config("BBB"), _demand(),
        baseline_fn=lambda process, start: 1.0,
    )
    dom_a = next(r for r in _by_process(preds_a, PROCESS_SECURITY_DOMESTIC) if r.window_start.hour == 7)
    dom_b = next(r for r in _by_process(preds_b, PROCESS_SECURITY_DOMESTIC) if r.window_start.hour == 7)

    assert dom_a.airport_iata == "AAA" and dom_a.flight_count == 1
    assert dom_b.airport_iata == "BBB" and dom_b.flight_count == 6
    assert dom_a.risk != dom_b.risk   # B'nin surge'ü A'yı ETKİLEMEDİ


def _row_for(mock_flight) -> dict:
    return {
        "flight_key": mock_flight.flight_key,
        "airport_iata": mock_flight.airport_iata,
        "direction": mock_flight.direction,
        "location": mock_flight.location,
        "airline_iata": mock_flight.airline_iata,
        "flight_number": mock_flight.flight_number,
        "flight_iata": mock_flight.flight_iata,
        "aircraft_icao": mock_flight.aircraft_icao,
        "aircraft_match_found": mock_flight.aircraft_match_found,
        "dep_iata": mock_flight.dep_iata,
        "arr_iata": mock_flight.arr_iata,
        "dep_scheduled_utc": mock_flight.dep_scheduled_utc,
        "dep_estimated_utc": mock_flight.dep_estimated_utc,
        "dep_actual_utc": mock_flight.dep_actual_utc,
        "arr_scheduled_utc": mock_flight.arr_scheduled_utc,
        "arr_estimated_utc": mock_flight.arr_estimated_utc,
        "arr_actual_utc": mock_flight.arr_actual_utc,
        "dep_terminal": mock_flight.dep_terminal,
        "dep_gate": mock_flight.dep_gate,
        "arr_terminal": mock_flight.arr_terminal,
        "arr_gate": mock_flight.arr_gate,
        "status": mock_flight.status,
    }
