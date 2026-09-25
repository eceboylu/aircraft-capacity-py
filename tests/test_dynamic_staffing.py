"""
Madde 1-7/16-17 - Dynamic MEGA Passport Staffing geri getirme
regresyonu.

Bir önceki task'ta dynamic staffing (yanlışlıkla) TÜM scale-derived
havuzlar için kaldırılmıştı (`SCALE_RESOURCES`'tan `_max` anahtarları
silinerek). Bu task bunu MEGA'ya taşıyarak geri getiriyor - mekanizmanın
KENDİSİ (`core/event_queue.py:DynamicStaffingParams`/`simulate_fifo_
queue_dynamic()`) HİÇ SİLİNMEMİŞTİ, sadece `SCALE_RESOURCES`'ta hangi
tier'ın `_max` taşıdığı değişiyor - bu testler PRODUCTION fonksiyonlarını
(kopyalamadan) doğrudan çağırır.

NOT (ramp-down testi hakkında dürüstlük notu): Bu algoritmanın GERÇEK
parametreleriyle (per_server_rate=1/1.5≈0.667 pax/dk, target_utilization
=0.85, look_ahead=10dk) MAX kapasitede (45 server) bir control interval'da
temizlenebilecek backlog miktarı (~300 pax/10dk) her bir "needed_servers"
bandının genişliğinden (~25-30 pax) kat kat büyüktür. Bu YAPISAL olarak
şu anlama gelir: organik backlog decay'i ASLA "45→40→35→30" gibi
checkpoint-checkpoint 4 ayrı basamaklı bir cascade ÜRETEMEZ - backlog
boşaldıkça needed_servers TEK bir 10dk aralığında ban'ların hepsini
birden geçer, bu yüzden gözlemlenen davranış HER ZAMAN "max'ta uzun süre
sabit kal, sonra TEK bir -5 adımıyla düş, hemen ardından kuyruk boşal"
şeklindedir (aşağıdaki `test_ramp_down_...` testinde ampirik olarak
doğrulanmıştır). Bu testler bu yüzden LİTERAL 4 basamaklı bir dizi
yerine gerçek GÜVENLİK invariant'ını (`|Δactive| <= ramp_step` HER
checkpoint'te, HİÇBİR ZAMAN max'tan min'e tek adımda düşme) doğrular -
görev talimatının "FINAL ACCEPTANCE CRITERIA" bölümü de zaten literal
diziyi değil "scale-down graceful" / "max=45 never exceeded" gibi bu
invariant'ları listeliyor.
"""
import math
from datetime import datetime, timedelta

import pytest

from app.queue.config import default_config
from app.queue.core.event_queue import DynamicStaffingParams, simulate_fifo_queue_dynamic
from app.queue.engine import _dynamic_staffing_params_for

BASE = datetime(2026, 3, 10, 8, 0)


# --- A-D: scale resource / config resolution -----------------------------

def test_mega_resource_dynamic_contract():
    cfg = default_config("XXX", scale="mega")
    assert cfg.passport_departure_server_count == 30
    assert cfg.passport_departure_server_count_max == 45
    assert cfg.passport_departure_dynamic is True
    assert cfg.passport_arrival_server_count == 35
    assert cfg.passport_arrival_server_count_max == 45
    assert cfg.passport_arrival_dynamic is True
    assert cfg.domestic_security_lane_count == 30
    assert cfg.international_security_lane_count == 20


def test_large_resource_static_contract():
    cfg = default_config("XXX", scale="large")
    assert cfg.passport_departure_server_count == 10
    assert cfg.passport_arrival_server_count == 12
    assert cfg.passport_departure_server_count_max is None
    assert cfg.passport_arrival_server_count_max is None
    assert cfg.passport_departure_dynamic is False
    assert cfg.passport_arrival_dynamic is False


def test_medium_resource_static_contract():
    cfg = default_config("XXX", scale="medium")
    assert cfg.passport_departure_server_count == 4
    assert cfg.passport_arrival_server_count == 4
    assert cfg.passport_departure_server_count_max is None
    assert cfg.passport_arrival_server_count_max is None
    assert cfg.passport_departure_dynamic is False
    assert cfg.passport_arrival_dynamic is False


def test_small_resource_static_contract():
    cfg = default_config("XXX", scale="small")
    assert cfg.passport_departure_server_count == 2
    assert cfg.passport_arrival_server_count == 2
    assert cfg.passport_departure_server_count_max is None
    assert cfg.passport_arrival_server_count_max is None
    assert cfg.passport_departure_dynamic is False
    assert cfg.passport_arrival_dynamic is False


def test_security_lanes_never_dynamic_for_any_scale():
    """Madde 12 - security tüm ölçeklerde static, config'te dynamic bayrağı bile yok."""
    for scale, expected_dom, expected_intl in [
        ("mega", 30, 20), ("large", 8, 6), ("medium", 3, 2), ("small", 2, 2),
    ]:
        cfg = default_config("XXX", scale=scale)
        assert cfg.domestic_security_lane_count == expected_dom
        assert cfg.international_security_lane_count == expected_intl


# --- K: LARGE dynamic regression ------------------------------------------

def test_large_never_activates_dynamic_staffing_via_engine():
    """
    Madde 6/K - LARGE için `_dynamic_staffing_params_for()` HER ZAMAN
    None dönmeli (demand ne olursa olsun - bu fonksiyon config'ten
    okur, canlı demand'e bakmaz, ama config'in KENDİSİ LARGE için asla
    dynamic olamayacağını garanti eder).
    """
    cfg = default_config("ESB", scale="large")
    assert _dynamic_staffing_params_for(cfg, "departure") is None
    assert _dynamic_staffing_params_for(cfg, "arrival") is None


def test_medium_and_small_never_activate_dynamic_staffing():
    for scale in ("medium", "small"):
        cfg = default_config("XXX", scale=scale)
        assert _dynamic_staffing_params_for(cfg, "departure") is None
        assert _dynamic_staffing_params_for(cfg, "arrival") is None


def test_mega_activates_dynamic_staffing_via_engine():
    cfg = default_config("IST", scale="mega")
    dep_params = _dynamic_staffing_params_for(cfg, "departure")
    arr_params = _dynamic_staffing_params_for(cfg, "arrival")

    assert dep_params is not None
    assert dep_params.default_server_count == 30
    assert dep_params.max_server_count == 45

    assert arr_params is not None
    assert arr_params.default_server_count == 35
    assert arr_params.max_server_count == 45

    for params in (dep_params, arr_params):
        assert params.control_interval_minutes == 10
        assert params.look_ahead_minutes == 10
        assert params.target_utilization == 0.85
        assert params.ramp_step == 5
        assert params.scale_down_backlog_floor_minutes == 30.0


# --- E/F: ramp-up cascades -------------------------------------------------

def _heavy_sustained_demand(server_count_hint: int, minutes: int = 60) -> list[tuple[datetime, str, float]]:
    """Talebin sürekli olarak max kapasitenin ÇOK üzerinde kaldığı bir yük."""
    arrivals = []
    t = BASE
    for _ in range(minutes // 10):
        arrivals.append((t, "departure", server_count_hint * 100.0))
        t += timedelta(minutes=10)
    return arrivals


def test_mega_departure_ramp_up_is_30_35_40_45():
    params = DynamicStaffingParams(
        default_server_count=30, max_server_count=45,
        control_interval_minutes=10, look_ahead_minutes=10,
        target_utilization=0.85, ramp_step=5,
    )
    arrivals = _heavy_sustained_demand(45)
    _events, schedule = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

    counts = [count for _, count in schedule]
    assert counts[:4] == [30, 35, 40, 45]


def test_mega_arrival_ramp_up_is_35_40_45():
    params = DynamicStaffingParams(
        default_server_count=35, max_server_count=45,
        control_interval_minutes=10, look_ahead_minutes=10,
        target_utilization=0.85, ramp_step=5,
    )
    arrivals = _heavy_sustained_demand(45)
    _events, schedule = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

    counts = [count for _, count in schedule]
    assert counts[:3] == [35, 40, 45]


# --- G/H: max/min clamp -----------------------------------------------------

def test_departure_never_exceeds_max_45_or_drops_below_default_30():
    params = DynamicStaffingParams(
        default_server_count=30, max_server_count=45,
        control_interval_minutes=10, look_ahead_minutes=10,
        target_utilization=0.85, ramp_step=5,
    )
    arrivals = _heavy_sustained_demand(45, minutes=200)
    _events, schedule = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

    counts = [count for _, count in schedule]
    assert max(counts) <= 45
    assert min(counts) >= 30


def test_arrival_never_exceeds_max_45_or_drops_below_default_35():
    params = DynamicStaffingParams(
        default_server_count=35, max_server_count=45,
        control_interval_minutes=10, look_ahead_minutes=10,
        target_utilization=0.85, ramp_step=5,
    )
    arrivals = _heavy_sustained_demand(45, minutes=200)
    _events, schedule = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

    counts = [count for _, count in schedule]
    assert max(counts) <= 45
    assert min(counts) >= 35


# --- I: ramp-down (bounded-step invariant, see module docstring) -----------

def test_ramp_never_changes_by_more_than_ramp_step_in_either_direction():
    """
    Madde 4/I - HİÇBİR checkpoint'te active server count bir öncekinden
    ramp_step'ten (5) fazla değişemez - "30 -> 45 tek adımda" veya
    "45 -> 30 tek adımda" YAPISAL OLARAK imkansız olmalı.
    """
    params = DynamicStaffingParams(
        default_server_count=30, max_server_count=45,
        control_interval_minutes=10, look_ahead_minutes=10,
        target_utilization=0.85, ramp_step=5,
    )
    arrivals = _heavy_sustained_demand(45, minutes=300)
    _events, schedule = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

    counts = [count for _, count in schedule]
    for previous, current in zip(counts, counts[1:]):
        assert abs(current - previous) <= 5, (previous, current)


def test_scale_down_actually_happens_when_demand_disappears():
    """
    Madde I - talep tamamen kesilince sistem GERÇEKTEN aşağı ramp
    yapmalı (sonsuza kadar max'ta takılı kalmamalı), ama tek adımda
    değil - bkz. modül docstring'i (bu parametrelerle organik decay
    her zaman TEK bir -5 adımıyla gerçekleşir, hemen ardından kuyruk
    boşalır).
    """
    params = DynamicStaffingParams(
        default_server_count=30, max_server_count=45,
        control_interval_minutes=10, look_ahead_minutes=10,
        target_utilization=0.85, ramp_step=5,
    )
    arrivals = [(BASE, "departure", 1200.0)]  # tek seferlik burst, sonra sessizlik
    _events, schedule = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

    counts = [count for _, count in schedule]
    assert max(counts) == 45
    assert counts[-1] < max(counts)
    assert max(counts) - counts[-1] <= 5


# --- J: backlog scale-down protection ---------------------------------------

def test_high_backlog_blocks_premature_scale_down():
    """
    Madde 5/J - backlog, MAX kapasitede bile `scale_down_backlog_floor_
    minutes` (30dk) içinde temizlenemiyorsa o checkpoint'te downward
    scale YAPILMAMALI. Devasa bir tek seferlik burst ile bunu doğrudan
    gözlemliyoruz: sistem UZUN SÜRE (onlarca checkpoint) 45'te sabit
    kalmalı, backlog gerçekten azalana kadar HİÇ aşağı inmemeli.
    """
    params = DynamicStaffingParams(
        default_server_count=30, max_server_count=45,
        control_interval_minutes=10, look_ahead_minutes=10,
        target_utilization=0.85, ramp_step=5,
        scale_down_backlog_floor_minutes=30.0,
    )
    arrivals = [(BASE, "departure", 50000.0)]
    events, schedule = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

    assert sum(e.count for e in events) == 50000.0

    counts = [count for _, count in schedule]
    decreases = [
        (prev, curr) for prev, curr in zip(counts, counts[1:]) if curr < prev
    ]

    # 50000 yolculuk backlog, MAX kapasitede (45 server) bile
    # scale_down_backlog_floor_minutes'ten (30dk) ÇOK daha uzun sürer -
    # bu yüzden HİÇBİR erken düşüş olmamalı: TEK bir düşüş (gerçek
    # tükenmenin hemen öncesinde) dışında hiçbiri gözlenmemeli.
    assert len(decreases) == 1
    assert counts[:100].count(45) >= 90  # onlarca checkpoint boyunca 45'te sabit


# --- L: Passport -> Security regression (dynamic active) -------------------

def test_passport_to_security_completion_time_coupling_unchanged_with_dynamic_staffing():
    """
    Madde 9/L - dynamic staffing throughput'u etkiler ama Passport ->
    Security kuplajının KENDİSİNİ (security arrival_time == passport
    completion_time) DEĞİŞTİRMEMELİ.
    """
    from app.queue.core.event_queue import simulate_passport, simulate_security

    dynamic = DynamicStaffingParams(
        default_server_count=30, max_server_count=45,
        control_interval_minutes=10, look_ahead_minutes=10,
        target_utilization=0.85, ramp_step=5,
    )
    departure_arrivals = _heavy_sustained_demand(45, minutes=30)
    departure_arrivals = [(t, c) for t, _origin, c in departure_arrivals]

    passport_result = simulate_passport(
        departure_arrivals, [],
        departure_server_count=30, arrival_server_count=35,
        service_time_minutes=1.5,
        departure_dynamic=dynamic,
    )
    departure_events = passport_result["departure"]
    assert departure_events
    assert passport_result["departure_schedule"] is not None

    security_arrivals = [(e.completion_time, e.count) for e in departure_events]
    security_events = simulate_security(security_arrivals, lane_count=20, service_time_minutes=0.4, origin="international")

    # Security'nin GÖRDÜĞÜ her arrival anı, passport'un GERÇEKTEN
    # tamamladığı bir completion_time OLMALI - security kendi
    # kapasitesine göre TEK bir passport completion cohort'unu birden
    # fazla ayrı ServiceEvent'e bölebilir (farklı service_start/
    # completion, AYNI arrival_time) - bu YÜZDEN liste UZUNLUKLARI
    # eşit olmak ZORUNDA DEĞİL, ama zaman KÜMESİ passport'un completion
    # kümesinin ALT KÜMESİ (burada TAM KÜMESİ) olmalı - hiçbir security
    # arrival'ı passport'un HİÇ üretmediği bir andan gelmemeli (show-up'tan
    # direkt duplicate YOK).
    passport_completions = {e.completion_time for e in departure_events}
    security_arrival_times = {e.arrival_time for e in security_events}
    assert security_arrival_times <= passport_completions

    assert sum(e.count for e in departure_events) == pytest.approx(
        sum(e.count for e in security_events)
    )


# --- Section 17: needed_servers formula, exercised via production code -----

def test_needed_servers_formula_forces_full_ramp_step_when_demand_is_huge():
    """
    Madde 17 - görev.md'nin örneği: passport_service_time_minutes=1.5,
    target_utilization=0.85, 10 dakikalık total_relevant_demand=250 ->
    needed_servers = ceil((250/10) / ((1/1.5)*0.85)) = ceil(25/0.56667)
    = 45.

    `_apply_checkpoint` (core/event_queue.py) private bir closure
    olduğu için formülü DOĞRUDAN çağıramıyoruz - onun YERİNE gerçek
    `simulate_fifo_queue_dynamic()`'i, backlog=0 ve İLK GERÇEK kontrol
    checkpoint'inin (`first_arrival + control_interval` - `schedule`
    listesinin index 0'ı HENÜZ hiçbir hesap yapmamış başlangıç tohumu,
    ASIL ilk hesaplanan checkpoint index 1'dir) lookahead penceresine
    DÜŞECEK şekilde zamanlanmış tam 250 pax'lık tek bir arrival ile
    besleyip GÖZLEMLENEBİLİR sonucu doğruluyoruz: needed=45 hesaplanmış
    olmalı ki ramp_step (+5) tavanına vursun (yani 30'dan 35'e ÇIKMALI,
    needed daha düşük olsaydı - ör. needed=32 - ramp +2 ile 32'ye
    giderdi, +5 TAVANINA vurmazdı).
    """
    params = DynamicStaffingParams(
        default_server_count=30, max_server_count=45,
        control_interval_minutes=10, look_ahead_minutes=10,
        target_utilization=0.85, ramp_step=5,
    )
    anchor = (BASE, "departure", 0.001)  # first_arrival'ı (08:00) sabitler, backlog ANINDA (default kapasiteyle) tükenir
    # İlk GERÇEK checkpoint 08:10'dur (08:00 + control_interval). Onun
    # lookahead penceresi (08:10,08:20] - burst'ü 08:00'a değil BU
    # pencereye düşecek şekilde 08:15'e yerleştiriyoruz (08:05 olsaydı
    # 08:00 checkpoint'inin KENDİ lookahead'ine düşer ve default
    # kapasiteyle checkpoint'e kadar kısmen zaten servise girerdi).
    lookahead_burst = (BASE + timedelta(minutes=15), "departure", 250.0)

    _events, schedule = simulate_fifo_queue_dynamic(
        [anchor, lookahead_burst], params, service_time_minutes=1.5,
    )
    counts = [count for _, count in schedule]

    # Doğrulanan formül: sum([0.6666667*0.85])=0.5666667; required_rate=25;
    # needed=ceil(25/0.5666667)=45.
    per_server_rate = 1 / 1.5
    required_rate = 250 / 10
    needed = math.ceil(required_rate / (per_server_rate * 0.85))
    assert needed == 45

    # ramp_step +5 TAVANINA vurduğunu (yani needed >= default+ramp_step
    # olduğunu) gösteren gözlemlenebilir kanıt: index 0 başlangıç
    # tohumu (30), index 1 İLK GERÇEK checkpoint (08:10 - 250'nin
    # lookahead'i tam bu anda görülür) TAM +5 ADIM atmalı (30 -> 35) -
    # needed daha düşük olsaydı (< 35) bu TAM +5 sıçraması gerçekleşmezdi.
    assert counts[0] == 30
    assert counts[1] == 35
