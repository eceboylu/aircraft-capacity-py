"""
PASSPORT -> SECURITY zaman-akışlı kuplaj.

Production kuralı: uluslararası kalkış yolcusu ÖNCE passport'a girer,
security'ye ANCAK passport'tan GERÇEKTEN serbest bırakıldıktan SONRA
ulaşır. Domestic kalkış passport'u hiç görmeden DOĞRUDAN security'ye
girer. Uluslararası varış SADECE passport'a girer, security'ye HİÇ
girmez (yolculuk burada bitiyor).

Bu dosya `engine.py:_passport_security_hourly_coupling()` +
`predict_airport()`'un onu PROCESS_SECURITY/PROCESS_SECURITY_INTL için
nasıl kullandığını, motor seviyesinde (gerçek `predict_airport()`
çağrısı, mock olmayan DemandCalculator+MockCapacityResolver ile)
doğrular. Passport modeli, 160 pax/saat (4 gişe x 1.5dk) ve security
480 pax/saat (8 lane x 1dk) modeli DEĞİŞTİRİLMEDİ - sadece security'nin
GİRDİ ZAMANLAMASI düzeltildi.
"""

from datetime import datetime, timedelta

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
)
from app.queue.domain.demand import DemandCalculator
from app.queue.engine import predict_airport

from .factories import MockCapacityResolver, arrival, at, departure


def _demand():
    return DemandCalculator(MockCapacityResolver())


def intl_departure(hour, minute=0, **kwargs):
    kwargs.setdefault("location", LOCATION_INTERNATIONAL)
    return departure(hour, minute, **kwargs)


def intl_arrival(hour, minute=0, **kwargs):
    kwargs.setdefault("location", LOCATION_INTERNATIONAL)
    return arrival(hour, minute, **kwargs)


def _by(predictions, process):
    return sorted(
        [p for p in predictions if p.process == process],
        key=lambda p: p.window_start,
    )


def _at_hour(predictions, process, hour):
    rows = [
        p for p in predictions
        if p.process == process and p.window_start == at(hour)
    ]
    return rows[0] if rows else None


# ========================================================================
# 1) Domestic departure - passport'u atlar, DOĞRUDAN security'ye girer.
# ========================================================================

def test_domestic_departure_reaches_security_same_hour_no_passport_delay():
    # ADIM (Airport Queue Model V2 - sabit -120dk offset): 09:00 kalkış
    # effective_time'ı 07:00'a (pencere 07:00) düşürür.
    flights = [
        departure(9, 0, location=LOCATION_DOMESTIC, key=f"DOM{i}", number=str(i), aircraft="B77W")
        for i in range(3)   # 3 x 350 = 1050 pax, security kapasitesini (480) aşan bir yük
    ]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    security = _at_hour(predictions, PROCESS_SECURITY, 7)
    assert security is not None
    assert security.expected_passengers == 1050
    # Passport'u hiç GÖRMEDİ - hiçbir gecikme/kuplaj YOK, aynı saatte tam talep.
    passport = _at_hour(predictions, PROCESS_PASSPORT, 7)
    assert passport is None   # domestic hiçbir zaman passport'a girmez


# ========================================================================
# 2) Uluslararası varış - SADECE passport'a girer, security'ye HİÇ girmez.
# ========================================================================

def test_international_arrival_never_reaches_security():
    flights = [intl_arrival(10, 0, key="ARR1", number="1", aircraft="B77W")]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    passport = _by(predictions, PROCESS_PASSPORT)
    assert len(passport) == 1
    assert passport[0].expected_passengers == 350

    security = _by(predictions, PROCESS_SECURITY)
    security_intl = _by(predictions, PROCESS_SECURITY_INTL)
    assert security == []
    assert security_intl == []


# ========================================================================
# 3) Uluslararası kalkış - security'ye ANCAK passport'tan serbest
#    bırakıldıktan SONRA ulaşır (tek pencere, backlog YOK durumda).
# ========================================================================

def test_international_departure_reaches_security_only_after_passport_when_not_backlogged():
    # Tek bir küçük uçuş (E190=100 pax) - passport'un 160 pax/saat
    # kapasitesinin altında, backlog YOK, AYNI saatte tam serbest bırakılır.
    flights = [intl_departure(9, 0, key="D1", number="1", aircraft="E190")]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    passport = _at_hour(predictions, PROCESS_PASSPORT, 7)
    security_intl = _at_hour(predictions, PROCESS_SECURITY_INTL, 7)
    assert passport.expected_passengers == 100
    assert security_intl.expected_passengers == 100   # backlog yok -> AYNI saatte tamamı serbest


# ========================================================================
# 4) Passport backlog'lu iken: security'ye AKTARILAN <= passport'un
#    GERÇEKTEN işlediği (asla passport tamamlanmadan giriş yok, asla
#    passport'un işlediğinden FAZLASI security'ye sızmaz).
# ========================================================================

def test_security_never_receives_more_than_passport_actually_released():
    # 6 x E190 (100 her biri = 600 toplam) - passport kapasitesi 320/saat,
    # ağır backlog.
    flights = [
        intl_departure(9, 0, key=f"D{i}", number=str(i), aircraft="E190")
        for i in range(6)
    ]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    passport = _at_hour(predictions, PROCESS_PASSPORT, 7)
    security_intl = _at_hour(predictions, PROCESS_SECURITY_INTL, 7)

    assert passport.expected_passengers == 600
    assert passport.risk == RISK_CRITICAL   # passport gerçek darboğaz

    # security_intl SADECE passport'un o saat GERÇEKTEN işleyebildiği
    # kadarını görür - ham talebin (600) TAMAMI DEĞİL. Gerçek event-driven
    # simülasyonda (8 server, 1.5dk/servis) saat içine TAM olarak 39 dalga
    # sığar (39*1.5=58.5dk); 40. dalga tam saat sınırında (60dk) tamamlanır
    # ve yarı-açık pencere kuralıyla [start,end) BİR SONRAKİ saate düşer -
    # bu yüzden referans kapasite (320=8*40*1.5/60) ile DEĞİL, 39*8=312 ile
    # sınırlı (kesirli 40. dalga sınırda kesiliyor).
    assert security_intl.expected_passengers == 312.0
    assert security_intl.expected_passengers < passport.expected_passengers


# ========================================================================
# 5) Conservation: giren = işlenen + kalan kuyruk (yolcu
#    yaratma/kaybetme/double-count YOK). Backlog tamamen boşalana kadar
#    izleniyor; toplam security_intl'e aktarılan tam olarak passport'un
#    işlediğine (backlog düşüşüne) eşit.
# ========================================================================

def test_conservation_across_hours_until_passport_backlog_fully_drains():
    # Tek büyük uçuş (B77W=350 pax) - passport 320/saat ile birkaç saat
    # sürer boşalması.
    flight_demand = 350
    flights = [intl_departure(9, 0, key="BIG", number="1", aircraft="B77W")]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    passport_rows = _by(predictions, PROCESS_PASSPORT)
    security_intl_rows = _by(predictions, PROCESS_SECURITY_INTL)

    assert len(passport_rows) == 1   # passport sadece kendi (tek) saatinde bir satır üretir
    assert passport_rows[0].expected_passengers == flight_demand

    # security_intl BİRDEN FAZLA saate yayılmış olabilir (backlog boşalırken).
    total_transferred_to_security = sum(r.expected_passengers for r in security_intl_rows)

    # Gerçek event-driven simülasyon: ilk saate TAM 39 dalga (39*8=312)
    # sığar (40. dalga tam saat sınırında tamamlanır, yarı-açık pencere
    # kuralıyla İKİNCİ saate düşer); kalan 350-312=38 ikinci saatte.
    assert len(security_intl_rows) == 2
    assert security_intl_rows[0].expected_passengers == 312.0
    assert security_intl_rows[1].expected_passengers == 38.0

    # CONSERVATION: security'ye aktarılan TOPLAM == passport'un işlediği
    # TOPLAM == flight'ın TÜM talebi (hiçbir yolcu yaratılmadı/kaybolmadı).
    assert total_transferred_to_security == flight_demand


# ========================================================================
# 6) Aynı saatte domestic + international kalkış - birbirine
#    KARIŞMIYOR, domestic ANINDA, international passport'tan geçtikten
#    SONRA.
# ========================================================================

def test_domestic_and_international_departure_same_hour_do_not_mix():
    domestic = [departure(9, 0, location=LOCATION_DOMESTIC, key="DOM1", number="1", aircraft="A320")]
    intl = [intl_departure(9, 0, key="INT1", number="2", aircraft="E190")]
    predictions = predict_airport("AAA", domestic + intl, default_config("AAA"), _demand())

    security_dom = _at_hour(predictions, PROCESS_SECURITY_DOMESTIC, 7)
    security_intl = _at_hour(predictions, PROCESS_SECURITY_INTL, 7)
    security_combined = _at_hour(predictions, PROCESS_SECURITY, 7)

    assert security_dom.expected_passengers == 180        # SADECE domestic (A320)
    assert security_intl.expected_passengers == 100        # SADECE international'ın passport'tan serbest bıraktığı (E190, backlog yok)
    assert security_combined.expected_passengers == 280     # ikisi TOPLAM, ÇİFT SAYILMADI (180+100)


# ========================================================================
# 7) Saat sınırı aktarımı - passport backlog'u bir SONRAKİ saate
#    taşındığında yolcu KAYBOLMUYOR, doğru saate security'ye ulaşıyor.
# ========================================================================

def test_passenger_released_after_hour_boundary_is_not_lost():
    # 9:00'da büyük bir talep (backlog oluşturur) - hiç yeni uçuş
    # gelmese bile backlog BİR SONRAKİ saatte de boşalmaya devam etmeli
    # ve security_intl'e o saatte de ulaşmalı (saat sınırı aktarımı).
    flights = [
        intl_departure(9, 0, key=f"D{i}", number=str(i), aircraft="E190")
        for i in range(6)   # 600 pax toplam
    ]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    security_intl = _by(predictions, PROCESS_SECURITY_INTL)
    hours = [r.window_start.hour for r in security_intl]
    # ADIM (Event-Driven Engine Entegrasyonu): gerçek simülasyonda saat
    # içine TAM 39 dalga (8'er kişi, 1.5dk/dalga) sığar = 312; 40. dalga
    # tam saat sınırında tamamlanır, yarı-açık [start,end) kuralıyla
    # SONRAKİ saate düşer - kalan 600-312=288 ikinci saatte.
    # ADIM (Airport Queue Model V2 - sabit -120dk offset): kalkış 09:00
    # -> effective_time penceresi 07:00.
    assert hours == [7, 8]
    assert [r.expected_passengers for r in security_intl] == [312.0, 288.0]
    assert sum(r.expected_passengers for r in security_intl) == 600


# ========================================================================
# 8) Recovery - backlog GERÇEKTEN sıfıra iniyor, sonraki saatler
#    etkilenmiyor.
# ========================================================================

def test_recovery_after_backlog_drains_next_flight_is_unaffected():
    surge = [
        intl_departure(9, 0, key=f"D{i}", number=str(i), aircraft="E190")
        for i in range(6)   # 600 pax - birkaç saat sürer boşalması (bkz. test 7)
    ]
    # ADIM (Airport Queue Model V2 - sabit -120dk offset): 13:00 kalkış
    # -> effective_time penceresi 11:00. YENİ, küçük ve TEK BAŞINA bir
    # uçuş - önceki backlog (bkz. test 7 - 07:00-08:00 arası TAMAMEN
    # boşalıyor) TAMAMEN boşaldıktan SONRA.
    recovery_flight = [intl_departure(13, 0, key="REC", number="99", aircraft="E190")]
    predictions = predict_airport(
        "AAA", surge + recovery_flight, default_config("AAA"), _demand()
    )

    security_intl = _by(predictions, PROCESS_SECURITY_INTL)
    row_11 = next(r for r in security_intl if r.window_start.hour == 11)
    # 11:00'daki talep SADECE recovery_flight'ın kendi talebi (100) -
    # önceki backlog'dan hiçbir kalıntı YOK (tam boşalmış).
    assert row_11.expected_passengers == 100.0
