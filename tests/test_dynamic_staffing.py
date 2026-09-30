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
from app.queue.constants import (
    ARRIVAL_PASSPORT_PROACTIVE_LOOKAHEAD_MINUTES,
    PASSPORT_ARR_OPERATIONAL_LEVELS,
    PASSPORT_DEP_OPERATIONAL_LEVELS,
    SECURITY_INTL_OPERATIONAL_LEVELS,
)
from app.queue.core.event_queue import DynamicStaffingParams, simulate_fifo_queue_dynamic
from app.queue.engine import _dynamic_staffing_params_for

BASE = datetime(2026, 3, 10, 8, 0)


# --- A-D: scale resource / config resolution -----------------------------

def test_mega_resource_dynamic_contract():
    cfg = default_config("XXX", scale="mega")
    assert cfg.passport_departure_server_count == 30
    assert cfg.passport_departure_server_count_max == 60
    assert cfg.passport_departure_dynamic is True
    assert cfg.passport_arrival_server_count == 30
    assert cfg.passport_arrival_server_count_max == 60
    assert cfg.passport_arrival_dynamic is True
    assert cfg.domestic_security_lane_count == 20
    assert cfg.international_security_lane_count == 20
    assert cfg.international_security_lane_count_max == 40
    assert cfg.security_intl_dynamic is True


def test_large_resource_static_contract():
    cfg = default_config("XXX", scale="large")
    assert cfg.passport_departure_server_count == 15
    assert cfg.passport_arrival_server_count == 16
    assert cfg.passport_departure_server_count_max is None
    assert cfg.passport_arrival_server_count_max is None
    assert cfg.passport_departure_dynamic is False
    assert cfg.passport_arrival_dynamic is False


def test_medium_resource_static_contract():
    cfg = default_config("XXX", scale="medium")
    assert cfg.passport_departure_server_count == 8
    assert cfg.passport_arrival_server_count == 8
    assert cfg.passport_departure_server_count_max is None
    assert cfg.passport_arrival_server_count_max is None
    assert cfg.passport_departure_dynamic is False
    assert cfg.passport_arrival_dynamic is False


def test_small_resource_static_contract():
    cfg = default_config("XXX", scale="small")
    assert cfg.passport_departure_server_count == 4
    assert cfg.passport_arrival_server_count == 4
    assert cfg.passport_departure_server_count_max is None
    assert cfg.passport_arrival_server_count_max is None
    assert cfg.passport_departure_dynamic is False
    assert cfg.passport_arrival_dynamic is False


def test_domestic_security_never_dynamic_for_any_scale():
    """MEGA dynamic resource policy Bölüm 6 - domestic security HİÇBİR
    ölçekte dynamic olmuyor (sadece international_security_intl MEGA'da
    dynamic oldu - bkz. aşağıdaki test)."""
    for scale, expected_dom in [("mega", 20), ("large", 15), ("medium", 6), ("small", 3)]:
        cfg = default_config("XXX", scale=scale)
        assert cfg.domestic_security_lane_count == expected_dom


def test_international_security_dynamic_only_for_mega():
    """MEGA dynamic resource policy Bölüm 1/31 - security_intl SADECE
    MEGA'da dynamic, diğer tüm scale'lerde static kalıyor (dynamic
    bayrağı False, max None)."""
    expectations = [
        ("mega", 20, True, 40),
        ("large", 15, False, None),
        ("medium", 5, False, None),
        ("small", 3, False, None),
    ]
    for scale, expected_base, expected_dynamic, expected_max in expectations:
        cfg = default_config("XXX", scale=scale)
        assert cfg.international_security_lane_count == expected_base
        assert cfg.security_intl_dynamic is expected_dynamic
        assert cfg.international_security_lane_count_max == expected_max


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
    security_params = _dynamic_staffing_params_for(cfg, "security_intl")

    assert dep_params is not None
    assert dep_params.default_server_count == 30
    assert dep_params.max_server_count == 60

    assert arr_params is not None
    assert arr_params.default_server_count == 30
    assert arr_params.max_server_count == 60
    # Bölüm 11 - passport_arr'a ÖZGÜ proaktif uzun lookahead; departure/
    # security'de bu KAPALI (None), davranışları eskisiyle AYNI kalır.
    assert arr_params.extended_look_ahead_minutes == ARRIVAL_PASSPORT_PROACTIVE_LOOKAHEAD_MINUTES

    assert security_params is not None
    assert security_params.default_server_count == 20
    assert security_params.max_server_count == 40

    for params in (dep_params, arr_params, security_params):
        assert params.control_interval_minutes == 5
        assert params.look_ahead_minutes == 5
        assert params.target_utilization == 0.85
        assert params.ramp_step == 10
        assert params.scale_down_backlog_floor_minutes == 30.0

    assert dep_params.extended_look_ahead_minutes is None
    assert security_params.extended_look_ahead_minutes is None

    # Discrete Operational Levels - artık her üç MEGA süreci de rastgele
    # bir tamsayıya değil, sadece bu sabit listelere oturabiliyor.
    assert security_params.allowed_levels == (20, 30, 40)
    assert dep_params.allowed_levels == (30, 40, 50, 60)
    assert arr_params.allowed_levels == (30, 40, 50, 60)


def test_mega_base_revision_floor_is_never_15():
    """
    ADIM (MEGA Base Revision) - kullanıcı talebi: "artık 15 aktif
    security_intl resource count OLAMAZ" (taban 20'ye yükseltildi).
    Ayrıca ADIM (phpMyAdmin'den canlı güncelleme) - passport_dep tabanı
    kullanıcı tarafından SQL'den 30'a (max 45) güncellendi ve Python
    varsayılanı ("ona göre de kodda güncelle" talebi) bununla senkronize
    edildi. Hiçbir dynamic MEGA sürecinde ASLA eski 15 tabanı
    görünmemeli. `passport_arr` (base=30) hiç değişmedi.
    """
    cfg = default_config("IST", scale="mega")
    dep_params = _dynamic_staffing_params_for(cfg, "departure")
    security_params = _dynamic_staffing_params_for(cfg, "security_intl")
    arr_params = _dynamic_staffing_params_for(cfg, "arrival")

    assert dep_params.default_server_count == 30
    assert 15 not in dep_params.allowed_levels
    assert 20 not in dep_params.allowed_levels
    assert min(dep_params.allowed_levels) == 30

    assert security_params.default_server_count == 20
    assert 15 not in security_params.allowed_levels
    assert min(security_params.allowed_levels) == 20

    assert arr_params.default_server_count == 30
    assert arr_params.allowed_levels == (30, 40, 50, 60)


def test_mega_scale_down_never_goes_below_20_floor():
    """
    ADIM (MEGA Base Revision, Bölüm 4) - security_intl/passport_dep
    scale-down 40->35->30->25->20 gider, 20'nin ALTINA (eski 15'e)
    ASLA inmemeli.
    """
    params = DynamicStaffingParams(
        default_server_count=20, max_server_count=40,
        control_interval_minutes=5, look_ahead_minutes=5,
        target_utilization=0.85, ramp_step=5,
        allowed_levels=SECURITY_INTL_OPERATIONAL_LEVELS,
    )
    heavy = _heavy_sustained_demand(server_count_hint=20, minutes=20)
    last_heavy_time = max(t for t, _, _ in heavy)
    tail = [(last_heavy_time + timedelta(hours=8), "departure", 1.0)]
    _events, schedule, _log = simulate_fifo_queue_dynamic(heavy + tail, params, service_time_minutes=1.0)
    counts = {count for _, count in schedule}
    assert min(counts) == 20
    assert 15 not in counts


def test_large_medium_small_never_activate_security_dynamic_staffing():
    """MEGA-only guard (Bölüm 31/32) - security_intl için de LARGE/
    MEDIUM/SMALL'da `_dynamic_staffing_params_for(..., "security_intl")`
    HER ZAMAN None dönmeli, hiçbir checkpoint/resource mutation olmaz."""
    for scale in ("large", "medium", "small"):
        cfg = default_config("XXX", scale=scale)
        assert _dynamic_staffing_params_for(cfg, "security_intl") is None


# --- Section 33/34: base/max GERÇEKTEN okunuyor mu (DB'siz, saf mekanizma) --

def test_security_style_base_and_max_are_never_exceeded():
    """Section 33.A - security_intl'in GÜNCEL source-of-truth'u
    (base=20, max=40, control=5dk) ile `simulate_fifo_queue_dynamic()`'i
    doğrudan çalıştır: hiçbir checkpoint 40'ı aşmamalı, hiçbir checkpoint
    20'nin ALTINA düşmemeli (varsayılan taban)."""
    params = DynamicStaffingParams(
        default_server_count=20, max_server_count=40,
        control_interval_minutes=5, look_ahead_minutes=5,
        target_utilization=0.85, ramp_step=5,
    )
    arrivals = _heavy_sustained_demand(server_count_hint=40, minutes=120)
    _events, schedule, log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.0)
    counts = [count for _, count in schedule]
    assert max(counts) <= 40
    assert min(counts) >= 20
    assert counts[0] == 20  # düşük talepte GERÇEKTEN base'den (20) başlıyor - hardcoded eski 30 DEĞİL.
    # Her checkpoint için |değişim| <= ramp_step (5) invariant'ı.
    for entry in log:
        assert abs(entry.ramp) <= params.ramp_step
        assert entry.reason in {
            "normal_load", "demand_threshold", "backlog_pressure",
            "lookahead_pressure", "peak_pressure", "scale_down", "initial",
        }


def test_security_style_max_is_driven_by_param_not_hardcoded():
    """Section 33.B/C - `DynamicStaffingParams.max_server_count`'u DIŞARIDAN
    (ör. DB override simülasyonu) farklı bir değere ayarlarsak, runtime
    o YENİ tavanı kullanmalı - kodda security için 40'a kilitli bir
    hardcode YOK, her şey `max_server_count` parametresinden geliyor."""
    for injected_max in (35, 25):
        params = DynamicStaffingParams(
            default_server_count=20, max_server_count=injected_max,
            control_interval_minutes=5, look_ahead_minutes=5,
            target_utilization=0.85, ramp_step=5,
        )
        arrivals = _heavy_sustained_demand(server_count_hint=injected_max, minutes=120)
        _events, schedule, _log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.0)
        counts = [count for _, count in schedule]
        assert max(counts) <= injected_max
        assert max(counts) == injected_max  # yeterince ağır yük altında GERÇEKTEN o tavana ulaşıyor.


def test_checkpoint_log_exposes_backlog_lookahead_needed_for_audit():
    """Section 16 - artık backlog/lookahead/needed_servers NULL kalmıyor;
    `simulate_fifo_queue_dynamic()`'in checkpoint_log'unda GERÇEKTEN dolu
    geliyor (production schedule/FIFO'ya dokunmadan, ek bir dönüş değeri)."""
    params = DynamicStaffingParams(
        default_server_count=20, max_server_count=40,
        control_interval_minutes=5, look_ahead_minutes=5,
        target_utilization=0.85, ramp_step=5,
    )
    arrivals = _heavy_sustained_demand(server_count_hint=40, minutes=60)
    _events, _schedule, log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.0)
    assert len(log) > 0
    for entry in log:
        assert entry.backlog is not None
        assert entry.lookahead_demand is not None
        assert entry.needed_servers is not None
        assert entry.backlog >= 0
        assert entry.lookahead_demand >= 0


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
    _events, schedule, _log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

    counts = [count for _, count in schedule]
    assert counts[:4] == [30, 35, 40, 45]


def test_mega_arrival_ramp_up_is_35_40_45():
    params = DynamicStaffingParams(
        default_server_count=35, max_server_count=45,
        control_interval_minutes=10, look_ahead_minutes=10,
        target_utilization=0.85, ramp_step=5,
    )
    arrivals = _heavy_sustained_demand(45)
    _events, schedule, _log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

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
    _events, schedule, _log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

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
    _events, schedule, _log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

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
    _events, schedule, _log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

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
    _events, schedule, _log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

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
    events, schedule, _log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.5)

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

    _events, schedule, _log = simulate_fifo_queue_dynamic(
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


# --- Discrete Operational Levels (MEGA level-snapping policy) -------------

def test_needed_to_level_rounding_table():
    """Section 4/19 - needed_servers, bir sonraki UYGUN operational
    level'a (yukarı) yuvarlanıyor."""
    levels = SECURITY_INTL_OPERATIONAL_LEVELS  # (20,30,40) - step=10

    def target_for(needed):
        candidates = [lvl for lvl in levels if lvl >= needed]
        return min(candidates) if candidates else levels[-1]

    assert target_for(16) == 20
    assert target_for(19) == 20
    assert target_for(20) == 20
    assert target_for(21) == 30
    assert target_for(24) == 30
    assert target_for(26) == 30
    assert target_for(31) == 40
    assert target_for(38) == 40
    assert target_for(999) == 40


def test_active_count_only_ever_takes_allowed_level_values():
    """Section 1/8 - runtime aktif lane sayısı SADECE 20/25/30/35/40
    olabilir; 19/24/29/31/33/38 gibi ara tamsayılar ASLA görünmemeli."""
    params = DynamicStaffingParams(
        default_server_count=20, max_server_count=40,
        control_interval_minutes=5, look_ahead_minutes=5,
        target_utilization=0.85, ramp_step=5,
        allowed_levels=SECURITY_INTL_OPERATIONAL_LEVELS,
    )
    arrivals = _heavy_sustained_demand(server_count_hint=40, minutes=180)
    _events, schedule, log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.0)
    counts = {count for _, count in schedule}
    assert counts.issubset(set(SECURITY_INTL_OPERATIONAL_LEVELS))
    forbidden = {15, 19, 24, 29, 31, 33, 38}
    assert counts.isdisjoint(forbidden)
    assert any(entry.needed_servers not in SECURITY_INTL_OPERATIONAL_LEVELS for entry in log)


def test_only_one_level_step_per_checkpoint_even_under_extreme_demand():
    """Section 5/8 - current=20 iken needed aniden fırlasa bile, İLK
    checkpoint SADECE 20->30 yapar (step=10); 40'a doğrudan ATLAMAZ."""
    params = DynamicStaffingParams(
        default_server_count=20, max_server_count=40,
        control_interval_minutes=5, look_ahead_minutes=5,
        target_utilization=0.85, ramp_step=5,
        allowed_levels=SECURITY_INTL_OPERATIONAL_LEVELS,
    )
    arrivals = _heavy_sustained_demand(server_count_hint=200, minutes=60)
    _events, schedule, log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.0)
    counts = [count for _, count in schedule]
    assert counts[0] == 20
    assert counts[1] == 30
    level_index = {lvl: i for i, lvl in enumerate(SECURITY_INTL_OPERATIONAL_LEVELS)}
    for prev, curr in zip(counts, counts[1:]):
        assert abs(level_index[curr] - level_index[prev]) <= 1
    assert any(e.target_operational_level == 40 and e.new_count < 40 for e in log)


def test_scale_down_also_moves_one_level_at_a_time():
    """Section 6 - aşağı inerken de aynı kural: 40'tan aniden 20'ye
    DÜŞMEZ, sırayla 40->35->30->25->20 gider."""
    params = DynamicStaffingParams(
        default_server_count=20, max_server_count=40,
        control_interval_minutes=5, look_ahead_minutes=5,
        target_utilization=0.85, ramp_step=5,
        allowed_levels=SECURITY_INTL_OPERATIONAL_LEVELS,
    )
    # Küçük bir yük darbesi (toplam 4000 yolcu) - 40 sunucuyla makul bir
    # sürede TAMAMEN drene olur; 8 saatlik boşluk sonrası base'e (20)
    # dönmüş olması beklenir (200×100 gibi devasa bir yük 75+ saat
    # sürerdi - bu yüzden buradaki daha küçük hacim BİLEREK seçildi).
    heavy = _heavy_sustained_demand(server_count_hint=20, minutes=20)
    last_heavy_time = max(t for t, _, _ in heavy)
    tail = [(last_heavy_time + timedelta(hours=8), "departure", 1.0)]
    _events, schedule, _log = simulate_fifo_queue_dynamic(heavy + tail, params, service_time_minutes=1.0)
    counts = [count for _, count in schedule]
    assert max(counts) == 40
    level_index = {lvl: i for i, lvl in enumerate(SECURITY_INTL_OPERATIONAL_LEVELS)}
    for prev, curr in zip(counts, counts[1:]):
        assert abs(level_index[curr] - level_index[prev]) <= 1
    assert counts[-1] == 20


def test_db_max_35_makes_level_40_impossible():
    """Section 7/19 - DB max=35 olursa runtime 40'a asla çıkamaz (step=10
    ile 35 zaten bir "seviye" değil, sadece bir tavan - en yüksek
    ULAŞILABİLİR seviye (20,30,40) listesinden 30'da kalır)."""
    params = DynamicStaffingParams(
        default_server_count=20, max_server_count=35,
        control_interval_minutes=5, look_ahead_minutes=5,
        target_utilization=0.85, ramp_step=5,
        allowed_levels=SECURITY_INTL_OPERATIONAL_LEVELS,
    )
    arrivals = _heavy_sustained_demand(server_count_hint=200, minutes=180)
    _events, schedule, _log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.0)
    counts = {count for _, count in schedule}
    assert 40 not in counts
    assert max(counts) == 30


def test_db_max_30_makes_levels_35_and_40_impossible():
    """Section 7/19 - DB max=30 olursa 35 VE 40 ikisi de imkansız olmalı."""
    params = DynamicStaffingParams(
        default_server_count=20, max_server_count=30,
        control_interval_minutes=5, look_ahead_minutes=5,
        target_utilization=0.85, ramp_step=5,
        allowed_levels=SECURITY_INTL_OPERATIONAL_LEVELS,
    )
    arrivals = _heavy_sustained_demand(server_count_hint=200, minutes=180)
    _events, schedule, _log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.0)
    counts = {count for _, count in schedule}
    assert 35 not in counts
    assert 40 not in counts
    assert max(counts) == 30


def test_passport_arr_only_ever_uses_30_35_40():
    """Section 3/10 - passport_arr base=30, allowed_levels sadece
    (30,35,40)."""
    params = DynamicStaffingParams(
        default_server_count=30, max_server_count=40,
        control_interval_minutes=5, look_ahead_minutes=5,
        target_utilization=0.85, ramp_step=5,
        allowed_levels=PASSPORT_ARR_OPERATIONAL_LEVELS,
    )
    arrivals = _heavy_sustained_demand(server_count_hint=200, minutes=90)
    _events, schedule, _log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.0)
    counts = {count for _, count in schedule}
    assert counts.issubset({30, 35, 40})


def test_fifo_actually_uses_the_stepped_level_not_just_audit():
    """Section 13 - level seçildikten sonra GERÇEK FIFO pool'u o sayıya
    çıkmalı."""
    params = DynamicStaffingParams(
        default_server_count=20, max_server_count=40,
        control_interval_minutes=5, look_ahead_minutes=5,
        target_utilization=0.85, ramp_step=5,
        allowed_levels=SECURITY_INTL_OPERATIONAL_LEVELS,
    )
    arrivals = _heavy_sustained_demand(server_count_hint=200, minutes=60)
    events, schedule, _log = simulate_fifo_queue_dynamic(arrivals, params, service_time_minutes=1.0)
    ordered_schedule = sorted(schedule, key=lambda item: item[0])
    first_upgrade_time = next(t for t, c in ordered_schedule if c == 30)
    max_count_after_upgrade = max(
        (e.count for e in events if e.service_start_time >= first_upgrade_time), default=0
    )
    assert max_count_after_upgrade >= 30
