"""
ADIM 6D - Passport anlık bekleme süresi + overload/backlog.

Erlang-C rho>=1 durumunda `estimated_wait_minutes=None` döndürüyordu
(matematiksel olarak doğru ama kullanıcıya hiçbir bilgi vermiyor).
Bu dosya, pencereler-arası backlog zincirinin (bkz. `engine.py`
`_passport_backlog_starts`, `core/scoring.py` `passport_queue_model`):

  A. c=4 (gişe) ile staff_count=8 (personel) ASLA karıştırılmadığını
  B. rho<1 VE backlog_start<=0 durumunda ESKİ Erlang-C sonucunun
     BİREBİR AYNI kaldığını
  C. rho>=1 durumunda artık SONLU bir bekleme süresi ürettiğini
  D. backlog'un pencereler arasında (boş pencereler dahil) doğru
     biçimde taşındığını
  E. kuyruğun bir önceki pencerenin backlog'u sıfırlanana kadar
     ANINDA sıfıra dönmediğini (kademeli boşalma)
  F. havalimanı bazında izole olduğunu
  G. security sürecinin bundan HİÇ etkilenmediğini
  H. iptal/yönlendirilmiş uçuşların talebe/backlog'a hiç girmediğini
  I. c=4 ile (yanlış) c=8 modelinin sayısal olarak FARKLI sonuç
     verdiğini (personel sayısını kanal sayısı gibi kullanmanın somut
     kanıtı)
  J. API katmanının bu sonlu değeri OLDUĞU GİBİ yüzeye çıkardığını
  K. gerçekçi bir T0 (normal) -> T1 (patlama) -> [boş pencereler] ->
     T2 (toparlanma, HÂLÂ backlog var) -> T3 (tam toparlanma) zincirini

doğrular. Fixture/mocked değer YOK - her sayı bu dosyada elle
hesaplanıp koddan doğrulanıyor.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.config import AirportConfigView, default_config
from app.queue.constants import (
    DIRECTION_ARRIVAL,
    LOCATION_INTERNATIONAL,
    PASSPORT_RHO_LOW,
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    RISK_CRITICAL,
    RISK_LOW,
    RISK_MEDIUM,
)
from app.queue.core.erlang import erlang_c_wait_time
from app.queue.core.scoring import passport_effective_service_rate, passport_queue_model
from app.queue.domain.demand import DemandCalculator
from app.queue.engine import persist_predictions, predict_airport
from app.queue.models import Flight

from .factories import MockCapacityResolver, arrival, departure, at


# --------------------------------------------------------------------
# TEST A - staff_count=8 KANAL SAYISI GİBİ KULLANILMIYOR (c=4)
# --------------------------------------------------------------------

def test_a_default_config_is_4_gişe_8_polis_and_staff_count_is_never_c():
    cfg = default_config("AAA")
    assert cfg.passport_counter_count == 4
    assert cfg.passport_staff_count == 8

    mu = passport_effective_service_rate(cfg)
    # mu = service_rate_per_staff(0.5) * staff_per_counter(2.0) * efficiency(1.5)
    assert mu == pytest.approx(1.5)

    # staff_count'u 8 -> 999 değiştirmek mu'yu HİÇ etkilemez (double-count yok).
    mutated = AirportConfigView(
        airport_iata="AAA",
        passport_counter_count=cfg.passport_counter_count,
        passport_staff_count=999,
        passport_staff_per_counter=cfg.passport_staff_per_counter,
        passport_service_rate_per_staff=cfg.passport_service_rate_per_staff,
        passport_efficiency_multiplier=cfg.passport_efficiency_multiplier,
        arrival_bank_threshold=cfg.arrival_bank_threshold,
        is_default=True,
    )
    assert passport_effective_service_rate(mutated) == mu


# --------------------------------------------------------------------
# TEST B - NORMAL YÜK: rho<1 VE backlog_start<=0 -> ESKİ Erlang-C
#          sonucu BİREBİR AYNI.
# --------------------------------------------------------------------

def test_b_normal_load_preserves_exact_old_erlang_c_result():
    cfg = default_config("AAA")   # c=4, mu=1.5 -> capacity_rate=6 kişi/dk
    window_flights = [10, 10]     # demand_fn=identity -> demand=20
    score = passport_queue_model(
        window_flights, cfg, demand_fn=lambda x: x, window_minutes=15,
        backlog_start=0.0,
    )
    lam = 20 / 15
    mu = 1.5
    c = 4
    expected_wq = erlang_c_wait_time(c, lam, mu)

    assert score["utilization"] == pytest.approx(round(lam / 6, 3))
    assert score["utilization"] < 1.0
    assert score["estimated_wait_minutes"] == round(expected_wq, 1)
    assert score["backlog_end"] == 0.0   # 20 kişi < 90 kişilik kapasite, hepsi bitti
    assert score["risk"] in (RISK_LOW, RISK_MEDIUM)


# --------------------------------------------------------------------
# TEST C - AŞIRI YÜK: rho>=1 artık SONLU bir dakika üretir (None değil).
# --------------------------------------------------------------------

def test_c_overload_produces_finite_wait_not_none():
    cfg = default_config("AAA")
    window_flights = [100]        # demand=100, lam=100/15=6.667, rho=6.667/6=1.111
    score = passport_queue_model(
        window_flights, cfg, demand_fn=lambda x: x, window_minutes=15,
        backlog_start=0.0,
    )
    assert score["utilization"] == pytest.approx(1.111, abs=0.001)
    assert score["utilization"] >= 1.0
    assert score["risk"] == RISK_CRITICAL

    # ESKİ davranış (ADIM 6D öncesi) None döndürürdü - artık SONLU.
    assert score["estimated_wait_minutes"] is not None
    # queue_ahead = backlog_start(0) + demand(100) = 100; wait = 100/6
    assert score["estimated_wait_minutes"] == round(100 / 6, 1)
    # backlog_end = max(0, 0 + 100 - 90) = 10
    assert score["backlog_end"] == pytest.approx(10.0)


# --------------------------------------------------------------------
# TEST D/E - BACKLOG ZİNCİRİ: T1 (patlama) -> T2 (backlog_start>0,
# kendi rho'su <1 olsa da bekleme ANINDA sıfıra düşmez) -> T3 (tam
# toparlanma, backlog=0 -> yeniden BİREBİR Erlang-C).
# --------------------------------------------------------------------

def test_d_backlog_carries_and_decays_without_snapping_to_zero():
    cfg = default_config("AAA")

    # T1: demand=200 -> backlog_end = max(0, 0+200-90) = 110
    t1 = passport_queue_model(
        [200], cfg, demand_fn=lambda x: x, window_minutes=15, backlog_start=0.0,
    )
    assert t1["utilization"] >= 1.0
    assert t1["backlog_end"] == pytest.approx(110.0)

    # T2: kendi talebi YOK (demand=0), ama backlog_start=110 miras alınır.
    t2 = passport_queue_model(
        [], cfg, demand_fn=lambda x: x, window_minutes=15,
        backlog_start=t1["backlog_end"],
    )
    assert t2["utilization"] < 1.0                 # kendi rho'su düşük
    assert t2["estimated_wait_minutes"] > 0         # AMA anında sıfıra düşmedi
    # queue_ahead = 110 + 0 = 110 -> wait = 110/6 = 18.3
    assert t2["estimated_wait_minutes"] == round(110 / 6, 1)
    assert t2["backlog_end"] == pytest.approx(20.0)  # max(0, 110-90)

    # T3: backlog_start=20, kendi talebi yok -> backlog_end = max(0,20-90)=0
    t3 = passport_queue_model(
        [], cfg, demand_fn=lambda x: x, window_minutes=15,
        backlog_start=t2["backlog_end"],
    )
    assert t3["backlog_end"] == 0.0
    assert t3["estimated_wait_minutes"] == round(20 / 6, 1)

    # T4: backlog_start=0 VE rho<1 -> tam olarak eski Erlang-C'ye DÖNER.
    t4 = passport_queue_model(
        [], cfg, demand_fn=lambda x: x, window_minutes=15,
        backlog_start=t3["backlog_end"],
    )
    assert t4["backlog_end"] == 0.0
    expected_wq0 = erlang_c_wait_time(4, 0.0, 1.5)
    assert t4["estimated_wait_minutes"] == round(expected_wq0, 1)
    assert t4["estimated_wait_minutes"] == 0.0


# --------------------------------------------------------------------
# Motor seviyesi (predict_airport) - gerçek Flight nesneleriyle.
# --------------------------------------------------------------------

RESOLVER = MockCapacityResolver(capacities={"BIG": 500, "SML": 80})


def _demand():
    return DemandCalculator(RESOLVER)


def _passport_series(predictions):
    rows = [p for p in predictions if p.process == PROCESS_PASSPORT]
    rows.sort(key=lambda p: p.window_start)
    return rows


def test_k_full_t0_t1_t2_t3_chain_engine_level():
    """
    T0 (08:00 penceresi, normal) -> T1 (08:15, patlama) -> [08:30/08:45/
    09:00 BOŞ pencereler - hiç uçuş yok, ama gişeler servis vermeye
    DEVAM eder] -> T2 (09:15, toparlanma - kendi talebi düşük ama
    ÖNCEKİ backlog hâlâ sürüyor) -> T3 (09:45, TAM toparlanma).

    Uçuşlar arrival() ile - `effective_time()` varışa +15 dk sabit
    passport buffer'ı ekler (bkz. domain/demand.py), bu yüzden
    scheduled saatler istenen pencere başlangıcından 15 dk ÖNCE
    seçildi (7:45 -> pencere 8:00, 8:00 -> pencere 8:15, vb.)
    """
    cfg = default_config("T6D")

    flights = [
        arrival(7, 45, airport="T6D", aircraft="SML", duration_minutes=90,
                key="T0_001"),
        arrival(8, 0, airport="T6D", aircraft="BIG", duration_minutes=90,
                key="T1_001"),
        arrival(9, 0, airport="T6D", aircraft="SML", duration_minutes=90,
                key="T2_001"),
        arrival(9, 30, airport="T6D", aircraft="SML", duration_minutes=90,
                key="T3_001"),
    ]

    predictions = predict_airport(
        airport_iata="T6D",
        flights=flights,
        config=cfg,
        demand=_demand(),
    )
    passport = _passport_series(predictions)
    assert [p.window_start for p in passport] == [
        at(8, 0), at(8, 15), at(9, 15), at(9, 45),
    ]

    t0, t1, t2, t3 = passport

    # T0: normal - backlog_start=0, rho<1 -> BİREBİR Erlang-C.
    demand0 = round(80 * 0.82)   # SML, kısa menzil intl load factor 0.82
    lam0 = demand0 / 15
    expected_wq0 = erlang_c_wait_time(4, lam0, 1.5)
    assert t0.utilization < 1.0
    assert t0.estimated_wait_minutes == round(expected_wq0, 1)

    # T1: patlama - BIG uçak, rho>=1 -> artık SONLU bir dakika (None değil).
    demand1 = round(500 * 0.82)
    assert t1.utilization >= 1.0
    assert t1.risk == RISK_CRITICAL
    assert t1.estimated_wait_minutes is not None
    assert t1.estimated_wait_minutes == round(demand1 / 6, 1)

    # T2: 3 BOŞ pencere sonra (08:30/08:45/09:00) - backlog HÂLÂ pozitif,
    # kendi talebi (SML, düşük) rho<1 olsa da bekleme SIFIRA SNAP OLMAZ.
    backlog_after_t1 = max(0.0, 0.0 + demand1 - 90)         # 08:15 sonu
    backlog_before_t2 = backlog_after_t1
    for _ in range(3):                                       # 3 boş pencere
        backlog_before_t2 = max(0.0, backlog_before_t2 - 90)
    demand2 = round(80 * 0.82)
    assert t2.utilization < 1.0
    assert t2.estimated_wait_minutes > 0
    assert t2.estimated_wait_minutes == round(
        (backlog_before_t2 + demand2) / 6, 1
    )

    # T3: backlog tamamen boşaldı (09:30 boş pencere üstüne) -> T0 ile
    # AYNI talep/AYNI Erlang-C sonucu - tam toparlanma kanıtı.
    backlog_before_t3 = max(0.0, (backlog_before_t2 + demand2 - 90) - 90)
    assert backlog_before_t3 == 0.0
    assert t3.estimated_wait_minutes == t0.estimated_wait_minutes


def test_f_airport_isolation_backlog_never_leaks_across_airports():
    cfg = default_config("AAA")

    surge_a = [arrival(8, 0, airport="AAA", aircraft="BIG", duration_minutes=90)]
    surge_b = [arrival(8, 0, airport="BBB", aircraft="SML", duration_minutes=90)]

    preds_a = _passport_series(predict_airport("AAA", surge_a, cfg, _demand()))
    preds_b = _passport_series(predict_airport("BBB", surge_b, cfg, _demand()))

    assert preds_a[0].utilization >= 1.0          # AAA patlamada
    assert preds_b[0].utilization < 1.0           # BBB normal - AAA'dan ETKİLENMEDİ
    assert preds_a[0].estimated_wait_minutes != preds_b[0].estimated_wait_minutes


def test_g_security_completely_unaffected_by_passport_backlog():
    cfg = default_config("AAA")
    flights = [
        arrival(8, 0, airport="AAA", aircraft="BIG", duration_minutes=90),   # passport patlama
        departure(8, 0, airport="AAA", aircraft="BIG",
                  location="domestic", duration_minutes=60),                 # security
    ]
    predictions = predict_airport("AAA", flights, cfg, _demand())
    security = [p for p in predictions if p.process == PROCESS_SECURITY]
    assert security
    for p in security:
        # Security'nin estimated_wait_minutes'ı HER ZAMAN None kalır -
        # passport'taki backlog/overload'dan bağımsız (AŞAMA 6D
        # security'ye HİÇ dokunmadı).
        assert p.estimated_wait_minutes is None


def test_h_cancelled_flight_excluded_from_demand_and_backlog():
    cfg = default_config("AAA")
    flights = [
        arrival(8, 0, airport="AAA", aircraft="SML", duration_minutes=90,
                status="cancelled"),
        arrival(8, 0, airport="AAA", aircraft="BIG", duration_minutes=90,
                key="kept"),
    ]
    predictions = _passport_series(predict_airport("AAA", flights, cfg, _demand()))
    only_big = _passport_series(predict_airport(
        "AAA",
        [arrival(8, 0, airport="AAA", aircraft="BIG", duration_minutes=90, key="kept")],
        cfg, _demand(),
    ))
    # İptal edilen SML uçuşu talebe hiç girmedi -> iki senaryo AYNI sonucu üretir.
    assert predictions[0].expected_passengers == only_big[0].expected_passengers
    assert predictions[0].estimated_wait_minutes == only_big[0].estimated_wait_minutes


def test_i_c4_vs_wrong_c8_model_numerically_different():
    """
    Personel sayısını (8) kanal sayısı (c) gibi kullanan YANLIŞ bir
    model ile, gerçek c=4 modelinin AYNI talep için FARKLI (ve daha
    kötümser - daha uzun bekleme) sonuç ürettiğinin sayısal kanıtı.
    """
    correct_cfg = default_config("AAA")   # c=4
    wrong_cfg = AirportConfigView(
        airport_iata="AAA",
        passport_counter_count=8,          # YANLIŞ: staff_count'u c yaptık
        passport_staff_count=8,
        passport_staff_per_counter=correct_cfg.passport_staff_per_counter,
        passport_service_rate_per_staff=correct_cfg.passport_service_rate_per_staff,
        passport_efficiency_multiplier=correct_cfg.passport_efficiency_multiplier,
        arrival_bank_threshold=correct_cfg.arrival_bank_threshold,
        is_default=False,
    )

    demand_fn = lambda x: x
    window_flights = [200]

    correct = passport_queue_model(window_flights, correct_cfg, demand_fn, 15)
    wrong = passport_queue_model(window_flights, wrong_cfg, demand_fn, 15)

    assert correct["utilization"] != wrong["utilization"]
    assert correct["estimated_wait_minutes"] != wrong["estimated_wait_minutes"]
    # c=4 (gerçek) modelin kapasitesi YARISI -> bekleme süresi belirgin
    # şekilde DAHA YÜKSEK olmalı (kötümser değil, gerçekçi).
    assert correct["estimated_wait_minutes"] > wrong["estimated_wait_minutes"]


# --------------------------------------------------------------------
# TEST J - API katmanı sonlu değeri OLDUĞU GİBİ yüzeye çıkarır.
# --------------------------------------------------------------------

@pytest.fixture()
def api_session():
    engine = create_engine("sqlite://")
    Session = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    session = Session()
    yield session
    session.close()


def test_j_api_surfaces_finite_wait_instead_of_none(api_session):
    cfg = default_config("T6D")
    flights = [
        arrival(8, 0, airport="T6D", aircraft="BIG", duration_minutes=90, key="surge"),
    ]
    for f in flights:
        api_session.add(Flight(
            flight_key=f.flight_key, airport_iata=f.airport_iata,
            direction=f.direction, location=f.location,
            airline_iata=f.airline_iata, flight_number=f.flight_number,
            flight_iata=f.flight_iata, aircraft_icao=f.aircraft_icao,
            aircraft_match_found=f.aircraft_match_found,
            dep_iata=f.dep_iata, arr_iata=f.arr_iata,
            dep_scheduled_utc=f.dep_scheduled_utc,
            arr_scheduled_utc=f.arr_scheduled_utc,
            arr_actual_utc=f.arr_actual_utc,
            status=f.status,
        ))
    api_session.commit()

    predictions = predict_airport("T6D", flights, cfg, _demand())
    persist_predictions(api_session, predictions)

    window_start = at(8, 15)   # +15 dk passport buffer -> 08:15 penceresi
    data = airport_predictions(
        api_session, "T6D", now=window_start + timedelta(minutes=1),
    )
    current = data["passport"]["current"]
    assert current is not None
    assert current["window_start"] == window_start.isoformat()
    assert current["utilization"] >= 1.0
    # ESKİ davranış: None ("Kapasite aşıldı" metnine düşerdi).
    # YENİ davranış: sonlu sayı - frontend artık gerçek dakika gösterir.
    assert current["estimated_wait_minutes"] is not None
    assert current["estimated_wait_minutes"] > 0
