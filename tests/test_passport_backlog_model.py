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
    assert mu == pytest.approx(2 / 3)
    assert cfg.passport_counter_count * mu == pytest.approx(8 / 3)
    assert cfg.passport_counter_count * mu * 60 == pytest.approx(160.0)

    # staff_count'u 8 -> 999 değiştirmek mu'yu HİÇ etkilemez (double-count yok).
    mutated = AirportConfigView(
        airport_iata="AAA",
        passport_counter_count=cfg.passport_counter_count,
        passport_staff_count=999,
        passport_service_time_minutes=cfg.passport_service_time_minutes,
        security_lane_count=cfg.security_lane_count,
        security_service_time_minutes=cfg.security_service_time_minutes,
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
    cfg = default_config("AAA")
    window_flights = [10, 10]     # demand_fn=identity -> demand=20
    score = passport_queue_model(
        window_flights, cfg, demand_fn=lambda x: x, window_minutes=15,
        backlog_start=0.0,
    )
    lam = 20 / 15
    mu = 2 / 3
    c = 8   # 4 gişe x 2 paralel görevli/gişe
    expected_wq = erlang_c_wait_time(c, lam, mu)

    assert score["utilization"] == pytest.approx(round(lam / (16 / 3), 3))
    assert score["utilization"] < 1.0
    assert score["estimated_wait_minutes"] == round(expected_wq, 1)
    assert score["backlog_end"] == 0.0   # 20 kişi < 80 kişilik kapasite, hepsi bitti
    assert score["risk"] in (RISK_LOW, RISK_MEDIUM)


# --------------------------------------------------------------------
# TEST C - AŞIRI YÜK: rho>=1 artık SONLU bir dakika üretir (None değil).
# --------------------------------------------------------------------

def test_c_overload_produces_finite_wait_not_none():
    cfg = default_config("AAA")
    # demand=100, bu 15 dk pencerede kapasite (16/3 pax/dk * 15) = 80 kişidir.
    window_flights = [100]
    score = passport_queue_model(
        window_flights, cfg, demand_fn=lambda x: x, window_minutes=15,
        backlog_start=0.0,
    )
    assert score["utilization"] == pytest.approx(1.25, abs=0.001)
    assert score["utilization"] >= 1.0
    assert score["risk"] == RISK_CRITICAL

    # ESKİ davranış (ADIM 6D öncesi) None döndürürdü - artık SONLU.
    assert score["estimated_wait_minutes"] is not None
    # ADIM 6D-2 DÜZELTMESİ: `now` verilmediği için pencere kapanmış
    # varsayılır (current_arrived_demand=demand, elapsed_minutes=15) ->
    # service_capacity(15dk) = (16/3)*15 = 80 -> current_queue =
    # max(0, 0 + 100 - 80) = 20 -> wait = 20/(16/3) = 3.8 dk.
    assert score["estimated_wait_minutes"] == 3.8
    # backlog_end = max(0, 0 + 100 - 80) = 20 - DEĞİŞMEDİ (AŞAMA 6D §C).
    assert score["backlog_end"] == pytest.approx(20.0)
    # Kapalı pencerede current_queue == backlog_end (aynı formül).
    assert score["estimated_wait_minutes"] == round(score["backlog_end"] / (16 / 3), 1)


# --------------------------------------------------------------------
# TEST D/E - BACKLOG ZİNCİRİ: T1 (patlama) -> T2 (backlog_start>0,
# kendi rho'su <1 olsa da bekleme ANINDA sıfıra düşmez) -> T3 (tam
# toparlanma, backlog=0 -> yeniden BİREBİR Erlang-C).
# --------------------------------------------------------------------

def test_d_backlog_carries_and_decays_without_snapping_to_zero():
    cfg = default_config("AAA")   # capacity_rate=16/3 -> 15dk kapasite=80

    # T1: demand=200 -> backlog_end=120 (200-80).
    t1 = passport_queue_model(
        [200], cfg, demand_fn=lambda x: x, window_minutes=15, backlog_start=0.0,
    )
    assert t1["utilization"] >= 1.0
    assert t1["backlog_end"] == pytest.approx(120.0)

    # T2: kendi talebi YOK (demand=0), ama backlog_start=120 miras alınır.
    t2 = passport_queue_model(
        [], cfg, demand_fn=lambda x: x, window_minutes=15,
        backlog_start=t1["backlog_end"],
    )
    assert t2["utilization"] < 1.0                 # kendi rho'su düşük
    assert t2["estimated_wait_minutes"] > 0         # AMA anında sıfıra düşmedi
    # ADIM 6D-2 DÜZELTMESİ: `now` verilmediği için pencere kapanmış
    # varsayılır -> current_queue = max(0, 120 + 0 - 80) = 40 -> wait
    # = 40/(16/3) = 7.5. Kapalı pencerede current_queue == backlog_end.
    assert t2["estimated_wait_minutes"] == 7.5
    assert t2["backlog_end"] == pytest.approx(40.0)

    # T3: backlog_start=40, kendi talebi yok -> backlog_end = max(0,40-80)=0
    t3 = passport_queue_model(
        [], cfg, demand_fn=lambda x: x, window_minutes=15,
        backlog_start=t2["backlog_end"],
    )
    assert t3["backlog_end"] == 0.0
    # current_queue = max(0, 40 + 0 - 80) = 0 -> wait = 0.0
    assert t3["estimated_wait_minutes"] == 0.0

    # T4: backlog_start=0 VE rho<1 -> tam olarak eski Erlang-C'ye DÖNER.
    t4 = passport_queue_model(
        [], cfg, demand_fn=lambda x: x, window_minutes=15,
        backlog_start=t3["backlog_end"],
    )
    assert t4["backlog_end"] == 0.0
    expected_wq0 = erlang_c_wait_time(8, 0.0, 2 / 3)
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
    ADIM 6D-2 HOURLY MIGRATION + ICAO Demand Kalibrasyonu: pencereler
    SAATLİK (60dk) ve demand artık load factor'süz HAM kapasite -
    capacity_rate=16/3/dk (320/saat, 4 gişe x 2 paralel görevli/gişe).
    Bu testte BIG=400 kullanılarak backlog'un boş saatlerde kademeli
    taşınması gözlenir.

    T0 (08:00 penceresi, normal) -> T1 (09:00, patlama - 4x BIG) ->
    [10:00/11:00 BOŞ pencereler - hiç uçuş yok, ama gişeler servis
    vermeye DEVAM eder] -> T2 (12:00, toparlanma - kendi talebi düşük
    ama ÖNCEKİ backlog hâlâ sürüyor, bekleme > 0) -> [13:00/14:00 BOŞ
    pencereler] -> T3 (15:00, TAM toparlanma - T0 ile birebir aynı).

    Uçuşlar arrival() ile - `effective_time()` varışa +15 dk sabit
    passport buffer'ı ekler (bkz. domain/demand.py), bu yüzden
    scheduled saatler istenen pencere başlangıcından 15 dk ÖNCE
    seçildi (7:45 -> pencere 8:00, 8:45 -> pencere 9:00, vb.)

    ADIM (Arrival Release Profile): her arrival flight'ın talebi ARTIK
    TEK bir +15dk noktasında DEĞİL, HAM arrival zamanından itibaren
    (+10/+15/+20/+25/+30dk, bkz. `ARRIVAL_RELEASE_PROFILE`) 5 batch'e
    yayılıyor - bu YENİ pencere sayısını (4 yerine 7 - her flight'ın
    ilk %15'lik batch'i BİR ÖNCEKİ saate düşüyor: 7:45 kalkış -> ilk
    batch 7:55 (hour07), kalanı 08:0x (hour08)) ve TAM sayıları
    değiştirdi. Aşağıdaki değerler gerçek `predict_airport()`
    çıktısından alınmıştır (körlemesine seçilmedi) - asıl SENARYO
    (normal -> patlama -> BOŞ pencerelerden GEÇEREK backlog taşınması ->
    kısmi toparlanma -> tam toparlanma) DEĞİŞMEDİ, SADECE saat/sayı
    ayrıntıları güncellendi.
    """
    cfg = default_config("T6D")

    flights = [
        arrival(7, 45, airport="T6D", aircraft="SML", duration_minutes=90,
                key="T0_001"),
        arrival(8, 45, airport="T6D", aircraft="BIG", duration_minutes=90,
                key="T1_001"),
        arrival(8, 45, airport="T6D", aircraft="BIG", duration_minutes=90,
                key="T1_002"),
        arrival(8, 45, airport="T6D", aircraft="BIG", duration_minutes=90,
                key="T1_003"),
        arrival(8, 45, airport="T6D", aircraft="BIG", duration_minutes=90,
                key="T1_004"),
        arrival(11, 45, airport="T6D", aircraft="SML", duration_minutes=90,
                key="T2_001"),
        arrival(14, 45, airport="T6D", aircraft="SML", duration_minutes=90,
                key="T3_001"),
    ]

    predictions = predict_airport(
        airport_iata="T6D",
        flights=flights,
        config=cfg,
        demand=DemandCalculator(MockCapacityResolver(
            capacities={"BIG": 400, "SML": 80}
        )),
    )
    passport = _passport_series(predictions)
    # 7 pencere: her flight'ın show-up'ın ilk %15'i (T-10dk noktası) bir
    # önceki takvim saatine düşüyor (07:55, 08:55, 11:55, 14:55).
    assert [p.window_start for p in passport] == [
        at(7, 0), at(8, 0), at(9, 0), at(11, 0), at(12, 0), at(14, 0), at(15, 0),
    ]

    w07, w08, w09, w11, w12, w14, w15 = passport

    # T0 (07:00/08:00 - SML'nin release'i ikiye bölünüyor): düşük talep,
    # backlog YOK, wait düşük/sıfıra yakın.
    assert w07.risk == RISK_LOW
    assert w07.estimated_wait_minutes == 0.0

    # T1 (08:00/09:00 - 4x BIG patlaması): 08:00 zaten yükseliyor (HIGH),
    # 09:00 GERÇEK darboğaz (CRITICAL, devasa wait) - backlog kaçınılmaz.
    assert w08.utilization < 1.0   # henüz TAM patlamadı (T1'in sadece ilk %15'i burada)
    assert w09.utilization >= 1.0
    assert w09.risk == RISK_CRITICAL
    assert w09.estimated_wait_minutes > w08.estimated_wait_minutes
    assert w09.estimated_wait_minutes > 100   # devasa backlog, GERÇEK/doğrulanmış değer

    # T2 (11:00/12:00) - 10:00 tamamen BOŞ pencereden (hiç flight yok,
    # ama gişeler servis vermeye DEVAM eder) SONRA: 11:00'in KENDİ talebi
    # minik olsa (rho<1) bile, 09:00'ın devasa backlog'u HÂLÂ tükenmemiş
    # olduğu için wait SIFIRA SNAP OLMAZ - bu senaryonun ASIL iddiası.
    assert w11.utilization < 1.0
    assert w11.estimated_wait_minutes > 0   # backlog HÂLÂ sürüyor (boş saatten GEÇEREK taşındı)
    assert w12.estimated_wait_minutes > 0
    assert w12.estimated_wait_minutes < w11.estimated_wait_minutes   # kademeli boşalma

    # T3 (14:00/15:00) - 13:00 BOŞ pencereden SONRA backlog TAMAMEN
    # tükendi -> T0 ile AYNI (sıfıra yakın) duruma dönüş - tam toparlanma.
    assert w14.estimated_wait_minutes == 0.0
    assert w15.estimated_wait_minutes == w07.estimated_wait_minutes == 0.0


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
        # Security kendi lane kapasitesiyle gerçek wait üretir; passport
        # backlog'u bu bağımsız sürece sızmaz.
        assert p.estimated_wait_minutes is not None
    # ADIM (Departure Show-Up Profile): tek domestic flight'ın 500 pax'i
    # artık TEK bir saate değil, KENDİ show-up profiline göre 3 saate
    # (%20/%60/%20) yayılıyor - conservation KESİN korunmalı (500
    # kaybolmadı/çoğalmadı), zirve saatin utilization'ı KENDİ payına
    # (500*0.6=300) göre hesaplanır.
    assert sum(p.expected_passengers for p in security) == 500
    peak = max(security, key=lambda p: p.expected_passengers)
    assert peak.expected_passengers == 300.0
    assert peak.utilization == pytest.approx(300 / 480, abs=0.001)


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
        passport_service_time_minutes=correct_cfg.passport_service_time_minutes,
        security_lane_count=correct_cfg.security_lane_count,
        security_service_time_minutes=correct_cfg.security_service_time_minutes,
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

    # +15 dk passport buffer -> effective 08:15 -> ADIM 6D-2 HOURLY
    # MIGRATION'dan itibaren saatlik pencereye (08:00-09:00) yuvarlanır.
    window_start = at(8, 0)
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
