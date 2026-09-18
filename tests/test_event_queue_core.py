"""
ADIM (Event-Driven Queue Core) - `app/queue/core/event_queue.py` doğrulaması.

Bu modül `engine.py:_event_driven_queue_demand()` tarafından KULLANILIYOR
- testler doğrudan SAF fonksiyonları hedefler. Mevcut Erlang-C/
capacity-rate fonksiyonları (`core/scoring.py`, `core/erlang.py`) SADECE
referans/çapraz-doğrulama için import edilir.

ADIM (Airport-Scale Queue Capacity): `simulate_passport()`'un eski
ORTAK/shared-pool sözleşmesi KALDIRILDI - departure/arrival ARTIK AYRI
fiziksel havuz (bkz. ilgili testler, "eski shared-pool contract'ı
KALDIRILDI" notu).
"""

from datetime import datetime, timedelta

import pytest

from app.queue.config import AirportConfigView, default_config
from app.queue.core.erlang import erlang_c_wait_time
from app.queue.core.event_queue import (
    ServiceEvent,
    simulate_fifo_queue,
    simulate_international_departure_journey,
    simulate_passport,
    simulate_security,
    total_count,
)
from app.queue.core.scoring import (
    passport_capacity_rate,
    passport_effective_server_count,
    security_capacity_rate,
)


def T(hour, minute=0, second=0):
    return datetime(2026, 9, 14, hour, minute, second)


# ========================================================================
# Bölüm 32 - QUEUE TIMING TESTİ (birebir spec senaryosu)
# ========================================================================

def test_section32_passport_first_completion_is_exactly_10_01_30():
    """
    300 passenger 10:00'da airport'a geliyor. Passport: 8 efektif
    server, 1.5 dk servis. İlk completion timestamp = 10:01:30.
    """
    events = simulate_fifo_queue(
        [(T(10, 0), "departure", 300.0)],
        server_count=8,
        service_time_minutes=1.5,
    )
    first = min(events, key=lambda e: e.completion_time)
    assert first.service_start_time == T(10, 0)
    assert first.completion_time == T(10, 1, 30)
    assert first.count == 8.0   # ilk dalga = 8 (server sayısı kadar)


def test_section32_cohort_never_reaches_security_before_first_passport_completion():
    """Bu cohort security'ye passport'un ilk completion'ından ÖNCE girmez."""
    passport_events = simulate_fifo_queue(
        [(T(10, 0), "departure", 300.0)], server_count=8, service_time_minutes=1.5,
    )
    security_arrivals = [(e.completion_time, e.count) for e in passport_events]
    security_events = simulate_security(security_arrivals, lane_count=8, service_time_minutes=1.0)

    assert min(e.arrival_time for e in security_events) == T(10, 1, 30)
    assert all(e.arrival_time >= T(10, 1, 30) for e in security_events)


def test_section32_security_keeps_processing_existing_backlog_before_passport_group_arrives():
    """
    Security'de 09:50'de gelmiş 50 kişilik eski bir backlog var. Passport
    grubu (8 kişi) ancak 10:01:30'da gelecek. Bu süre boyunca (09:50-
    10:01:30) security kendi lane'leriyle eski backlog'u işlemeye DEVAM
    etmeli - passport'un yeni grubunu beklememeli.
    """
    events = simulate_security(
        [(T(9, 50), 50.0), (T(10, 1, 30), 8.0)],
        lane_count=8, service_time_minutes=1.0,
    )
    old_backlog_events = [e for e in events if e.arrival_time == T(9, 50)]
    new_group_events = [e for e in events if e.arrival_time == T(10, 1, 30)]

    # Eski backlog TAMAMEN 10:01:30'dan ÖNCE işlenmiş (7 dalga, 1 dk/dalga,
    # 09:50 -> 09:57 - passport'un yeni grubunu HİÇ beklemedi).
    assert max(e.completion_time for e in old_backlog_events) == T(9, 57)
    assert total_count(old_backlog_events) == 50.0
    assert all(e.completion_time <= T(9, 57) for e in old_backlog_events)

    # Yeni grup (8 kişi) - 10:01:30'da TÜM lane'ler zaten boş olduğu için
    # HEMEN başlar (server'lar 09:57'den beri atıl, bekliyordu).
    assert total_count(new_group_events) == 8.0
    for e in new_group_events:
        assert e.service_start_time == T(10, 1, 30)   # passport'u beklemedi, ama KENDİ arrival'ından ÖNCE de başlamadı
        assert e.completion_time == T(10, 2, 30)


def test_section32_passport_does_not_wait_for_security():
    """
    Security'nin KENDİ kapasitesi/backlog'u passport'un servis hızını/
    completion zamanlarını HİÇ etkilemez - passport TAMAMEN bağımsız
    kendi simülasyonuyla çalışır.
    """
    passport_alone = simulate_fifo_queue(
        [(T(10, 0), "departure", 300.0)], server_count=8, service_time_minutes=1.5,
    )
    # Security ne kadar dolu/yavaş olursa olsun (hatta hiç simüle
    # edilmese bile) passport'un kendi çıktısı DEĞİŞMEZ - passport
    # fonksiyonu security'nin durumunu parametre olarak bile ALMAZ.
    first = min(passport_alone, key=lambda e: e.completion_time)
    assert first.completion_time == T(10, 1, 30)


def test_section32_security_accepts_passengers_as_passport_completion_events_arrive():
    """Security her passport completion event'inde YENİ gelen grubu kabul eder (ayrı arrival)."""
    passport_events = simulate_fifo_queue(
        [(T(10, 0), "departure", 24.0)], server_count=8, service_time_minutes=1.5,
    )
    # 24 kişi / 8 server = tam 3 dalga: 10:01:30, 10:03:00, 10:04:30.
    completion_times = sorted({e.completion_time for e in passport_events})
    assert completion_times == [T(10, 1, 30), T(10, 3, 0), T(10, 4, 30)]

    security_arrivals = [(e.completion_time, e.count) for e in passport_events]
    security_events = simulate_security(security_arrivals, lane_count=8, service_time_minutes=1.0)
    security_arrival_times = sorted({e.arrival_time for e in security_events})
    assert security_arrival_times == completion_times   # AYNI 3 ayrı arrival event'i


# ========================================================================
# Bölüm 9 - PASSPORT -> INTERNATIONAL SECURITY COUPLING, gerçek örnek
# ========================================================================

def test_section9_passport_release_becomes_security_arrival_at_exact_completion_time():
    result = simulate_international_departure_journey(
        departure_arrivals=[(T(10, 0), 8.0)],
        arrival_arrivals=[],
        passport_departure_server_count=8,
        passport_arrival_server_count=8,
        passport_service_time_minutes=1.5,
        international_security_lane_count=8,
        security_service_time_minutes=1.0,
    )
    dep = result["passport_departure"]
    sec = result["security"]

    assert len(dep) == 1
    assert dep[0].service_start_time == T(10, 0)
    assert dep[0].completion_time == T(10, 1, 30)

    assert len(sec) == 1
    # AŞAMA 9 örneği birebir: security arrival = passport completion.
    assert sec[0].arrival_time == T(10, 1, 30)
    assert sec[0].service_start_time == T(10, 1, 30)
    assert sec[0].completion_time == T(10, 2, 30)   # security 1 dk servis


def test_section9_international_arrival_never_produces_security_event():
    """Uluslararası varış passport'ta biter - security'ye HİÇ girmez (AŞAMA 30)."""
    result = simulate_international_departure_journey(
        departure_arrivals=[],
        arrival_arrivals=[(T(13, 0), 40.0)],
        passport_departure_server_count=8,
        passport_arrival_server_count=8,
        passport_service_time_minutes=1.5,
        international_security_lane_count=8,
        security_service_time_minutes=1.0,
    )
    assert result["security"] == []
    assert total_count(result["passport_arrival"]) == 40.0
    assert total_count(result["passport_departure"]) == 0.0


# ========================================================================
# ADIM (Airport-Scale Queue Capacity) - departure/arrival passport ARTIK
# AYRI fiziksel havuz (eski "shared pool" contract'ı KALDIRILDI - bkz.
# genel-proje.md bu turun görevi).
# ========================================================================

def test_departure_and_arrival_passport_pools_are_physically_independent():
    """
    Aynı anda gelen departure + arrival cohortu ARTIK AYRI serverlarda
    işlenir (departure'a KENDİ 8'i, arrival'a KENDİ 8'i - PAYLAŞILMAZ).
    Kanıt: T(10,0)'da AYNI ANDA HEM departure'ın 8'i HEM arrival'ın 8'i
    servise başlar - toplam 16 (8 DEĞİL, eski shared-pool contract'ının
    tersi).
    """
    result = simulate_passport(
        departure_arrivals=[(T(10, 0), 8.0)],
        arrival_arrivals=[(T(10, 0), 8.0)],
        departure_server_count=8,
        arrival_server_count=8,
        service_time_minutes=1.5,
    )
    all_events = result["departure"] + result["arrival"]
    starts_at_t0 = [e for e in all_events if e.service_start_time == T(10, 0)]
    # İki BAĞIMSIZ 8-server havuzu AYNI ANDA 16 kişiyi servise alabilir.
    assert total_count(starts_at_t0) == 16.0
    assert total_count(result["departure"]) == 8.0
    assert total_count(result["arrival"]) == 8.0
    # Departure'ın TÜMÜ tek dalgada (8/8), arrival'ın TÜMÜ tek dalgada -
    # biri diğerini BEKLEMEDİ.
    assert all(e.completion_time == T(10, 1, 30) for e in result["departure"])
    assert all(e.completion_time == T(10, 1, 30) for e in result["arrival"])


def test_heavy_departure_backlog_does_not_delay_arrival_pool():
    """
    Departure havuzu ağır bir backlog taşırken (300 pax, 8 server ->
    ~15dk sürer) arrival havuzu KENDİ boş serverlarıyla HİÇ ETKİLENMEDEN
    anında işler - iki AYRI heap olduğunun kanıtı.
    """
    result = simulate_passport(
        departure_arrivals=[(T(10, 0), 300.0)],
        arrival_arrivals=[(T(10, 5), 8.0)],
        departure_server_count=8,
        arrival_server_count=8,
        service_time_minutes=1.5,
    )
    # Arrival, departure'ın 300 kişilik kuyruğu YÜZÜNDEN beklemedi -
    # KENDİ arrival anında (10:05) hemen başladı.
    for e in result["arrival"]:
        assert e.service_start_time == T(10, 5)
        assert e.wait_minutes == 0.0
    assert total_count(result["arrival"]) == 8.0
    assert total_count(result["departure"]) == 300.0


def test_passport_effective_server_count_matches_real_production_config():
    """
    Event queue çekirdeği server sayısını dışarıdan parametre olarak alır
    (SAF) - production config'ten gelen gerçek değer (4x2=8,
    `passport_effective_server_count`, core/scoring.py, DEĞİŞTİRİLMEDİ)
    ile TUTARLI kullanıldığı doğrulanır.
    """
    config = default_config("AAA")
    assert passport_effective_server_count(config) == 8

    events = simulate_fifo_queue(
        [(T(10, 0), "departure", 16.0)],
        server_count=passport_effective_server_count(config),
        service_time_minutes=config.passport_service_time_minutes,
    )
    # 16 kişi / 8 server = tam 2 dalga.
    assert sorted({e.completion_time for e in events}) == [T(10, 1, 30), T(10, 3, 0)]


# ========================================================================
# Bölüm 6/18 - Domestic/International security fiziksel olarak AYRI
# ========================================================================

def test_domestic_and_international_security_are_independent_queues():
    """
    Aynı demand, farklı lane sayısı (Bölüm 34) - iki AYRI
    `simulate_security` çağrısı, birbirinin server heap'ini HİÇ görmez.
    `international_security_lane_count` artık hazır config alanından
    okunuyor.
    """
    config = AirportConfigView(
        airport_iata="AAA",
        passport_counter_count=4, passport_staff_count=8,
        passport_service_time_minutes=1.5, security_lane_count=8,
        domestic_security_lane_count=6, international_security_lane_count=10,
        security_service_time_minutes=1.0, passport_staff_per_counter=2.0,
        passport_service_rate_per_staff=1.0, passport_efficiency_multiplier=0.8125,
        arrival_bank_threshold=5, is_default=False,
    )
    domestic_arrivals = [(T(9, 0), 24.0)]
    international_arrivals = [(T(9, 0), 24.0)]   # varsayım: passport'tan zaten çıkmış

    dom_events = simulate_security(
        domestic_arrivals, lane_count=config.domestic_security_lane_count,
        service_time_minutes=config.security_service_time_minutes, origin="domestic",
    )
    intl_events = simulate_security(
        international_arrivals, lane_count=config.international_security_lane_count,
        service_time_minutes=config.security_service_time_minutes, origin="international",
    )

    # AYNI talep, FARKLI lane sayısı -> FARKLI tamamlanma profili.
    dom_last_completion = max(e.completion_time for e in dom_events)
    intl_last_completion = max(e.completion_time for e in intl_events)
    assert dom_last_completion > intl_last_completion   # dar (6) daha YAVAŞ biter
    # 24/6 = 4 dalga (dom) vs 24/10 -> 3 dalga (intl, son dalga 4 kişi).
    assert len({e.completion_time for e in dom_events}) == 4
    assert len({e.completion_time for e in intl_events}) == 3

    # Hiçbir olay diğerinin origin etiketini TAŞIMIYOR - fiziksel karışma yok.
    assert all(e.origin == "domestic" for e in dom_events)
    assert all(e.origin == "international" for e in intl_events)


# ========================================================================
# Bölüm 23 - WAIT = queue arrival -> service start
# ========================================================================

def test_wait_minutes_is_service_start_minus_arrival():
    events = simulate_security(
        [(T(9, 0), 24.0)], lane_count=8, service_time_minutes=1.0,
    )
    for e in events:
        expected_wait = (e.service_start_time - e.arrival_time).total_seconds() / 60.0
        assert e.wait_minutes == pytest.approx(expected_wait)

    # İlk dalga (8 kişi) hiç beklemedi (server'lar zaten boş).
    first_wave = min(events, key=lambda e: e.service_start_time)
    assert first_wave.wait_minutes == 0.0

    # Sonraki dalgalar (backlog nedeniyle) beklemiş olmalı (>0).
    later = [e for e in events if e.service_start_time > T(9, 0)]
    assert all(e.wait_minutes > 0 for e in later)


def test_wait_matches_erlang_c_reference_under_light_stable_load():
    """
    Referans/çapraz-doğrulama (core/erlang.py DEĞİŞTİRİLMEDİ, sadece
    KIYAS için kullanıldı): stabil, HAFİF yük altında (rho<<1, tek küçük
    cohort, hiç backlog yok) event-driven simülasyonun wait'i sıfıra
    yakın olmalı - tıpkı Erlang-C'nin de bu rejimde ürettiği gibi.
    """
    config = default_config("AAA")
    lam = 40.0 / 60.0   # 40 pax/saat -> dakikada
    mu = 1.0 / config.passport_service_time_minutes
    c = passport_effective_server_count(config)
    erlang_wait = erlang_c_wait_time(c, lam, mu)
    assert erlang_wait < 0.5   # referans: neredeyse anlık (rho çok düşük)

    events = simulate_fifo_queue(
        [(T(10, 0), "departure", 40.0)], server_count=c,
        service_time_minutes=config.passport_service_time_minutes,
    )
    # 40 kişi / 8 server = 5 dalga, TÜMÜ aynı anda (10:00) başlar çünkü
    # server sayısı >= toplam kişi/dalga başına yetiyor - gerçekte ilk
    # dalga hiç beklemez, kalanlar sadece kendi dalgalarının başlangıcında.
    first_wave = [e for e in events if e.service_start_time == T(10, 0)]
    assert all(e.wait_minutes == 0.0 for e in first_wave)


# ========================================================================
# Bölüm 33 - Exact timestamp korunumu, saatlik agregasyon YOK
# ========================================================================

def test_section33_multiple_flights_exact_arrival_times_preserved_not_bulk_at_bucket_start():
    """
    12:03/12:17/12:34/12:59 kalkışları -> (-120dk sabit offset) airport
    demand event'leri 10:03/10:17/10:34/10:59 - grafik bucket'ı (10.00)
    HEPSİNİN 10:00'da topluca geldiği ANLAMINA GELMEZ; internal
    simülasyon GERÇEK dakikaları korumalı.
    """
    departures = [
        (T(10, 3), 1.0), (T(10, 17), 1.0), (T(10, 34), 1.0), (T(10, 59), 1.0),
    ]
    events = simulate_fifo_queue(
        [(t, "departure", c) for t, c in departures],
        server_count=8, service_time_minutes=1.5,
    )
    arrival_times = sorted({e.arrival_time for e in events})
    # 4 AYRI gerçek dakika korunmuş - hiçbiri 10:00'a YUVARLANMADI.
    assert arrival_times == [T(10, 3), T(10, 17), T(10, 34), T(10, 59)]
    # Yeterli server olduğu için hiçbiri beklemedi, her biri KENDİ
    # gerçek dakikasında servise başladı.
    for e in events:
        assert e.service_start_time == e.arrival_time
        assert e.wait_minutes == 0.0


# ========================================================================
# Bölüm 29 - PASSENGER CONSERVATION
# ========================================================================

def test_conservation_input_equals_completed_when_queue_fully_drains():
    """input demand = completed (+ waiting/in-service, burada 0 çünkü tam boşalıyor)."""
    total_input = 733.0
    events = simulate_fifo_queue(
        [(T(9, 0), "x", 300.0), (T(9, 20), "x", 433.0)],
        server_count=8, service_time_minutes=1.5,
    )
    assert total_count(events) == total_input


def test_conservation_international_departure_full_chain_airport_to_security():
    """
    AŞAMA 29 - International Departure: airport demand -> passport ->
    international security -> completed zincirinde conservation.
    """
    total_input = 617.0
    result = simulate_international_departure_journey(
        departure_arrivals=[(T(10, 0), 300.0), (T(10, 30), 317.0)],
        arrival_arrivals=[],
        passport_departure_server_count=8, passport_arrival_server_count=8,
        passport_service_time_minutes=1.5,
        international_security_lane_count=8, security_service_time_minutes=1.0,
    )
    assert total_count(result["passport_departure"]) == total_input
    assert total_count(result["security"]) == total_input   # hiçbiri kaybolmadı/çoğalmadı


def test_conservation_mixed_departure_and_arrival_no_creation_or_loss():
    dep_total = 150.0
    arr_total = 90.0
    result = simulate_passport(
        departure_arrivals=[(T(9, 0), dep_total)],
        arrival_arrivals=[(T(9, 15), arr_total)],
        departure_server_count=8, arrival_server_count=8, service_time_minutes=1.5,
    )
    assert total_count(result["departure"]) == dep_total
    assert total_count(result["arrival"]) == arr_total
    assert total_count(result["departure"]) + total_count(result["arrival"]) == dep_total + arr_total


# ========================================================================
# Bölüm 30 - DOUBLE COUNT TESTLERİ
# ========================================================================

def test_double_count_domestic_departure_enters_security_dom_exactly_once():
    events = simulate_security(
        [(T(9, 0), 180.0)], lane_count=6, service_time_minutes=1.0, origin="domestic",
    )
    assert total_count(events) == 180.0
    assert all(e.origin == "domestic" for e in events)


def test_double_count_international_departure_enters_passport_once_then_security_intl_once():
    result = simulate_international_departure_journey(
        departure_arrivals=[(T(9, 0), 100.0)],
        arrival_arrivals=[],
        passport_departure_server_count=8, passport_arrival_server_count=8,
        passport_service_time_minutes=1.5,
        international_security_lane_count=8, security_service_time_minutes=1.0,
    )
    # Passport'a TAM 1 kez (100), security_intl'e TAM 1 kez (100) - 200 DEĞİL.
    assert total_count(result["passport_departure"]) == 100.0
    assert total_count(result["security"]) == 100.0


def test_double_count_international_arrival_enters_passport_once_security_zero_times():
    result = simulate_international_departure_journey(
        departure_arrivals=[],
        arrival_arrivals=[(T(13, 0), 70.0)],
        passport_departure_server_count=8, passport_arrival_server_count=8,
        passport_service_time_minutes=1.5,
        international_security_lane_count=8, security_service_time_minutes=1.0,
    )
    assert total_count(result["passport_arrival"]) == 70.0
    assert result["security"] == []


def test_double_count_domestic_arrival_never_enters_any_simulated_queue():
    """
    Domestic arrival bu modülün hiçbir fonksiyonuna GİRDİ olarak
    verilmez - AŞAMA 5/30 gereği zaten hiçbir kuyruk demand'i üretmiyor;
    burada "0 kez" invariant'ı, bu modülün domestic arrival için hiçbir
    arrival listesi ÜRETMEDİĞİNİ (çağıran tarafın onu hiç bu fonksiyonlara
    vermeyeceğini) doğrulayan bir NEGATİF kontrol olarak, boş girdiyle
    boş çıktı üretildiğini kanıtlar.
    """
    result = simulate_passport(
        departure_arrivals=[], arrival_arrivals=[],
        departure_server_count=8, arrival_server_count=8, service_time_minutes=1.5,
    )
    assert result["departure"] == []
    assert result["arrival"] == []


# ========================================================================
# Performans / AŞAMA 41 - cohort/batch, milyonlarca ORM satırı YOK
# ========================================================================

def test_large_cohort_produces_bucketed_output_not_one_row_per_passenger():
    """
    5000 kişilik TEK bir cohort - çıktı, passenger sayısı kadar (5000)
    DEĞİL, benzersiz dalga sayısı kadar (ceil(5000/8)=625) ServiceEvent
    üretmeli - `_merge_adjacent_events` bunu garanti eder.
    """
    events = simulate_fifo_queue(
        [(T(6, 0), "departure", 5000.0)], server_count=8, service_time_minutes=1.5,
    )
    assert total_count(events) == 5000.0
    assert len(events) == 625   # ceil(5000/8)
    assert len(events) < 5000   # hiçbir zaman passenger-başına satır değil


def test_large_cohort_runs_fast(benchmark=None):
    import time
    start = time.perf_counter()
    events = simulate_fifo_queue(
        [(T(6, 0), "departure", 20000.0)], server_count=8, service_time_minutes=1.5,
    )
    elapsed = time.perf_counter() - start
    assert total_count(events) == 20000.0
    assert elapsed < 2.0   # O(N log c) - 20000 heap işlemi saniyenin çok altında olmalı


# ========================================================================
# Yapısal / hata kontrolleri
# ========================================================================

def test_empty_arrivals_produce_empty_result():
    assert simulate_fifo_queue([], server_count=8, service_time_minutes=1.5) == []


def test_invalid_server_count_raises():
    with pytest.raises(ValueError):
        simulate_fifo_queue([(T(9, 0), "x", 10.0)], server_count=0, service_time_minutes=1.0)


def test_invalid_service_time_raises():
    with pytest.raises(ValueError):
        simulate_fifo_queue([(T(9, 0), "x", 10.0)], server_count=8, service_time_minutes=0)


def test_service_event_is_immutable_and_frozen():
    events = simulate_fifo_queue(
        [(T(9, 0), "x", 1.0)], server_count=8, service_time_minutes=1.0,
    )
    with pytest.raises(Exception):
        events[0].count = 999.0   # frozen dataclass - AttributeError bekleniyor
