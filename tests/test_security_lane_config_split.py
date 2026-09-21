"""
ADIM (Domestic/International Security Lane Ayrımı) - config katmanı
doğrulaması.

Bu ADIM `passport->security` kuplaj fonksiyonuna (engine.py:
`_passport_security_hourly_coupling`) DOKUNMUYOR - kapsam SADECE
`PROCESS_SECURITY_DOMESTIC`'in kendi fiziksel lane sayısını
(`domestic_security_lane_count`) `PROCESS_PASSPORT`/birleşik
`PROCESS_SECURITY`'den bağımsız, airport-bazlı config'ten okumasıdır.
`international_security_lane_count` şema/config katmanında taşınır ve
her havalimanı için ayrı ayarlanabilir, ama `PROCESS_SECURITY_INTL`'in
queue matematiğine henüz BAĞLANMADI (kuplaj fonksiyonunun içini
değiştirmeyi gerektirir - bu ADIM'ın kapsamı dışında, bkz. rapor).

ADIM (Departure Show-Up Profile): bu dosyadaki TÜM flight'lar AYNI dakikada
(09:00) kalkıyor - show-up profili (bkz. `domain/demand.py:departure_
show_up_events()`) bu durumda HER ZAMAN aynı sabit oranla 3 saate
(06:00/%20, 07:00/%60, 08:00/%20) bölünür. Bu dosyanın testleri hep 07:00
saatine bakıyor - bu yüzden beklenen `expected_passengers` artık eski
"tüm talep" DEĞİL, `0.6 * tüm talep` (60%). `utilization`/`estimated_wait_
minutes` bu YENİ (0.6 çarpanlı) değerden TÜRETİLDİĞİ için onlar da
orantılı şekilde güncellendi - queue formülünün KENDİSİ (Erlang-C/backlog)
DEĞİŞMEDİ, SADECE girdi (demand) değişti.
"""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.config import (
    AirportConfigView,
    default_config,
    get_config,
    get_configs,
)
from app.queue.constants import (
    LOCATION_DOMESTIC,
    PROCESS_SECURITY_DOMESTIC,
)
from app.queue.domain.demand import DemandCalculator
from app.queue.engine import predict_airport
from app.queue.models import AirportOperationalConfig

from .factories import MockCapacityResolver, departure


def _demand():
    return DemandCalculator(MockCapacityResolver())


def _at_hour(predictions, process, hour):
    rows = [
        p for p in predictions
        if p.process == process and p.window_start.hour == hour
    ]
    return rows[0] if rows else None


# ========================================================================
# 1) Default/fallback davranışı - satır yoksa güvenli, mevcutla AYNI
#    varsayım (8/8) - hiçbir mevcut airport'un davranışı bozulmaz.
# ========================================================================

def test_default_config_domestic_and_international_lane_counts_are_safe_fallback():
    cfg = default_config("ZZZ")
    assert cfg.is_default is True
    assert cfg.domestic_security_lane_count == 8
    assert cfg.international_security_lane_count == 8
    # Mevcut, geriye dönük uyumlu birleşik alan da AYNI kalır.
    assert cfg.security_lane_count == 8


def test_airport_config_view_accepts_call_sites_without_new_fields():
    """
    Mevcut çağıranlar (ör. testler) yeni alanları HİÇ vermezse bile
    (kw_only + default=8) TypeError almamalı - additive/geriye dönük
    uyumlu.
    """
    cfg = AirportConfigView(
        airport_iata="AAA",
        passport_counter_count=4,
        passport_staff_count=8,
        passport_service_time_minutes=1.5,
        security_lane_count=8,
        security_service_time_minutes=1.0,
        passport_staff_per_counter=2.0,
        passport_service_rate_per_staff=1.0,
        passport_efficiency_multiplier=0.8125,
        arrival_bank_threshold=5,
        is_default=True,
    )
    assert cfg.domestic_security_lane_count == 8
    assert cfg.international_security_lane_count == 8


# ========================================================================
# 2) DB satırından okuma - airport-bazlı, ayrı ayrı override edilebilir.
# ========================================================================

def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_get_config_reads_domestic_and_international_lane_counts_from_row():
    session = _session()
    session.add(AirportOperationalConfig(
        airport_iata="IST",
        domestic_security_lane_count=6,
        international_security_lane_count=10,
    ))
    session.commit()

    cfg = get_config(session, "IST")
    assert cfg.is_default is False
    assert cfg.domestic_security_lane_count == 6
    assert cfg.international_security_lane_count == 10
    session.close()


def test_get_config_row_without_explicit_lane_columns_uses_column_default():
    """Satır VAR ama yeni kolonlar elle set edilmemiş -> ORM/DB varsayımı (8/8)."""
    session = _session()
    session.add(AirportOperationalConfig(airport_iata="ESB"))
    session.commit()

    cfg = get_config(session, "ESB")
    assert cfg.domestic_security_lane_count == 8
    assert cfg.international_security_lane_count == 8
    session.close()


def test_get_configs_multi_airport_lane_counts_do_not_mix():
    session = _session()
    session.add(AirportOperationalConfig(
        airport_iata="IST", domestic_security_lane_count=6,
        international_security_lane_count=10,
    ))
    session.add(AirportOperationalConfig(
        airport_iata="SAW", domestic_security_lane_count=3,
        international_security_lane_count=5,
    ))
    session.commit()

    configs = get_configs(session, ["IST", "SAW", "ADB"])

    assert configs["IST"].domestic_security_lane_count == 6
    assert configs["IST"].international_security_lane_count == 10
    assert configs["SAW"].domestic_security_lane_count == 3
    assert configs["SAW"].international_security_lane_count == 5
    # ADB'nin satırı yok -> güvenli varsayım, IST/SAW'dan HİÇBİR ŞEY sızmadı.
    assert configs["ADB"].is_default is True
    assert configs["ADB"].domestic_security_lane_count == 8
    assert configs["ADB"].international_security_lane_count == 8
    session.close()


# ========================================================================
# 3) Bölüm 34 - aynı demand, farklı domestic lane sayısı -> farklı
#    utilization/wait (PROCESS_SECURITY_DOMESTIC, kuplajdan bağımsız).
# ========================================================================

def _config_with_domestic_lanes(airport: str, domestic_lanes: int) -> AirportConfigView:
    base = default_config(airport)
    return AirportConfigView(
        airport_iata=airport,
        passport_counter_count=base.passport_counter_count,
        passport_staff_count=base.passport_staff_count,
        passport_service_time_minutes=base.passport_service_time_minutes,
        security_lane_count=base.security_lane_count,
        domestic_security_lane_count=domestic_lanes,
        international_security_lane_count=base.international_security_lane_count,
        security_service_time_minutes=base.security_service_time_minutes,
        passport_staff_per_counter=base.passport_staff_per_counter,
        passport_service_rate_per_staff=base.passport_service_rate_per_staff,
        passport_efficiency_multiplier=base.passport_efficiency_multiplier,
        arrival_bank_threshold=base.arrival_bank_threshold,
        is_default=False,
    )


def test_domestic_security_lane_count_changes_utilization_and_wait():
    """
    Aynı domestic talep (6 x A320 = 6 x 180 = 1080 pax), tek fark lane
    sayısı - dar (3 lane) konfigürasyon GENİŞ (10 lane) konfigürasyondan
    kesinlikle daha yüksek utilization/daha uzun (ya da en az o kadar
    uzun) bekleme üretmeli. Gerçek `predict_airport()` üzerinden, mock
    olmayan production queue matematiğiyle doğrulanır.
    """
    flights = [
        departure(9, 0, location=LOCATION_DOMESTIC, key=f"D{i}", number=str(i), aircraft="A320")
        for i in range(6)
    ]

    narrow = predict_airport(
        "NARROW", flights, _config_with_domestic_lanes("NARROW", 3), _demand(),
    )
    wide = predict_airport(
        "WIDE", flights, _config_with_domestic_lanes("WIDE", 10), _demand(),
    )

    narrow_dom = _at_hour(narrow, PROCESS_SECURITY_DOMESTIC, 7)
    wide_dom = _at_hour(wide, PROCESS_SECURITY_DOMESTIC, 7)

    assert narrow_dom is not None and wide_dom is not None
    # Talep AYNI - show-up profili 09:00 kalkışın %60'ını 07:00'e koyar
    # (bkz. dosya docstring'i): 1080 * 0.6 = 648, iki config için de AYNI
    # (sadece KAPASİTE farklı).
    assert narrow_dom.expected_passengers == wide_dom.expected_passengers == 648.0

    # server_count = server_count parametresi (queue_capacity_model'in
    # döndürdüğü ham alan) - gerçekten FARKLI lane sayısı KULLANILDIĞININ
    # doğrudan kanıtı (utilization/wait bunun TÜREVİDİR).
    assert narrow_dom.utilization > wide_dom.utilization
    assert narrow_dom.estimated_wait_minutes > wide_dom.estimated_wait_minutes

    # Sayısal doğrulama - gerçek Erlang-C/backlog formülünden:
    # 3 lane x 1 dk = 3 pax/dk = 180 pax/saat kapasite; 648 talep ->
    # rho = 648/60 / 3 = 3.6 (>=1 -> CRITICAL, backlog kaçınılmaz).
    # 10 lane x 1 dk = 10 pax/dk = 600 pax/saat; rho = 10.8/10 = 1.08
    # (>=1 -> hâlâ CRITICAL ama daha DÜŞÜK rho / daha KISA bekleme).
    assert narrow_dom.utilization == 3.6
    assert wide_dom.utilization == 1.08


def test_domestic_security_lane_count_matches_wide_config_reference_capacity():
    """
    Aşırı talep OLMADIĞI (stabil) senaryoda, 10 lane x 1 dk = 600
    pax/saat referans kapasitesiyle rho = talep/kapasite formülü
    birebir doğrulanır (Erlang-C stabil dal).
    """
    flights = [
        departure(9, 0, location=LOCATION_DOMESTIC, key="D1", number="1", aircraft="A320")
    ]
    predictions = predict_airport(
        "WIDE2", flights, _config_with_domestic_lanes("WIDE2", 10), _demand(),
    )
    dom = _at_hour(predictions, PROCESS_SECURITY_DOMESTIC, 7)
    # Show-up: 180 * 0.6 = 108 (bkz. dosya docstring'i).
    assert dom.expected_passengers == 108.0
    # rho = (108/60) / (10*1) = 1.8/10 = 0.18
    assert dom.utilization == 0.18
    assert dom.risk == "LOW"


# ========================================================================
# 4) Airport isolation - iki havalimanının domestic/international lane
#    config'i BİRBİRİNİ ASLA etkilemez (Bölüm 1/34).
# ========================================================================

def test_airport_isolation_domestic_lane_config_never_leaks_between_airports():
    """
    Bölüm 34 senaryosu: Airport A (domestic=6, international=10),
    Airport B (domestic=3, international=5) - AYNI passenger demand,
    SONUÇLARIN farklı olduğu VE her havalimanının SADECE kendi
    config'ini kullandığı doğrulanır.
    """
    flights = [
        departure(9, 0, location=LOCATION_DOMESTIC, key=f"D{i}", number=str(i), aircraft="A320")
        for i in range(4)
    ]

    config_a = _config_with_domestic_lanes("A", 6)
    config_b = _config_with_domestic_lanes("B", 3)

    predictions_a = predict_airport("A", flights, config_a, _demand())
    predictions_b = predict_airport("B", flights, config_b, _demand())

    dom_a = _at_hour(predictions_a, PROCESS_SECURITY_DOMESTIC, 7)
    dom_b = _at_hour(predictions_b, PROCESS_SECURITY_DOMESTIC, 7)

    # airport_iata her sonuçta doğru ve ayrı - hiçbir çapraz sızma yok.
    assert {p.airport_iata for p in predictions_a} == {"A"}
    assert {p.airport_iata for p in predictions_b} == {"B"}

    # AYNI talep - show-up: 4*180*0.6 = 432 (bkz. dosya docstring'i),
    # FARKLI lane sayısı -> FARKLI sonuç.
    assert dom_a.expected_passengers == dom_b.expected_passengers == 432.0
    assert dom_a.utilization != dom_b.utilization
    # rho_A = (432/60)/6 = 1.2 ; rho_B = (432/60)/3 = 2.4
    assert dom_a.utilization == 1.2
    assert dom_b.utilization == 2.4

    # B'nin (dar) config'i A'yı (geniş) hiç etkilemedi - A hâlâ kendi
    # (daha düşük) utilization değerini koruyor.
    assert dom_a.utilization < dom_b.utilization


def test_airport_isolation_via_db_configs_does_not_mix_rows():
    """
    Aynı senaryo ama config.py'nin GERÇEK DB okuma yolundan
    (`get_configs`) - iki havalimanının satırları AYNI sorguda
    döndürülse bile birbirine KARIŞMIYOR.
    """
    session = _session()
    session.add(AirportOperationalConfig(
        airport_iata="IST", domestic_security_lane_count=6,
    ))
    session.add(AirportOperationalConfig(
        airport_iata="SAW", domestic_security_lane_count=3,
    ))
    session.commit()

    configs = get_configs(session, ["IST", "SAW"])
    flights = [
        departure(9, 0, location=LOCATION_DOMESTIC, key=f"D{i}", number=str(i), aircraft="A320")
        for i in range(4)
    ]

    predictions_ist = predict_airport("IST", flights, configs["IST"], _demand())
    predictions_saw = predict_airport("SAW", flights, configs["SAW"], _demand())

    dom_ist = _at_hour(predictions_ist, PROCESS_SECURITY_DOMESTIC, 7)
    dom_saw = _at_hour(predictions_saw, PROCESS_SECURITY_DOMESTIC, 7)

    assert dom_ist.utilization == 1.2   # (432/60)/6 - show-up: 4*180*0.6=432
    assert dom_saw.utilization == 2.4   # (432/60)/3
    session.close()


# ========================================================================
# 5) Birleşik (legacy) PROCESS_SECURITY VE PROCESS_PASSPORT bu ADIM'dan
#    ETKİLENMEDİ - hâlâ mevcut `security_lane_count`/passport config'ini
#    kullanıyor (kuplaj fonksiyonu değişmedi).
# ========================================================================

def test_combined_security_process_still_uses_legacy_security_lane_count():
    """
    PROCESS_SECURITY (birleşik) domestic_security_lane_count'tan
    ETKİLENMEMELİ - hâlâ `security_lane_count` (8, kuplaj içinde
    `security_capacity_rate`) kullanıyor. Bu, kuplaj fonksiyonuna
    dokunulmadığının doğrudan kanıtıdır.
    """
    from app.queue.constants import PROCESS_SECURITY

    flights = [
        departure(9, 0, location=LOCATION_DOMESTIC, key=f"D{i}", number=str(i), aircraft="A320")
        for i in range(4)
    ]
    narrow_domestic_config = _config_with_domestic_lanes("X", 3)
    assert narrow_domestic_config.security_lane_count == 8   # legacy alan DEĞİŞMEDİ

    predictions = predict_airport("X", flights, narrow_domestic_config, _demand())
    combined = _at_hour(predictions, PROCESS_SECURITY, 7)
    # rho_combined = (432/60) / 8 = 0.9 - show-up: 4*180*0.6=432 -
    # domestic_security_lane_count=3 DEĞİL, hâlâ security_lane_count=8
    # kullanılıyor.
    assert combined.utilization == 0.9
