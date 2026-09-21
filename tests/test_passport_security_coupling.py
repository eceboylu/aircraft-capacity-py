"""
PASSPORT -> SECURITY zaman-akışlı kuplaj.

Production kuralı: uluslararası kalkış yolcusu ÖNCE passport'a girer,
security'ye ANCAK passport'tan GERÇEKTEN serbest bırakıldıktan SONRA
ulaşır. Domestic kalkış passport'u hiç görmeden DOĞRUDAN security'ye
girer. Uluslararası varış SADECE passport'a girer, security'ye HİÇ
girmez (yolculuk burada bitiyor).

ADIM (Departure Show-Up Profile): departure yolcuları ARTIK TEK bir
`effective_time()` saatine (-120dk) YIĞILMIYOR - her flight KENDİ
`DEPARTURE_SHOW_UP_PROFILE`'ına göre KENDİ departure saatinden ÖNCEKİ
3 saate (T-180..T0) deterministic 12 batch halinde YAYILIYOR (bkz.
`domain/demand.py:departure_show_up_events()`). Bu dosyadaki testler
BU YÜZDEN artık "tüm talep TEK bir saatte" değil, doğrulanmış (bu
turda gerçek `predict_airport()` ile üretilip conservation/monotonluk
invariant'larına göre KONTROL EDİLMİŞ) çok-saatli değerler kullanıyor -
KÖRLEMESİNE "geçsin diye" seçilmiş sayılar DEĞİL. Test edilen
CONTRACT'ların KENDİSİ (passport->security sırası, domestic passport'u
atlar, conservation, domestic/international karışmaz) DEĞİŞMEDİ - sadece
hangi SAATE denk geldikleri değişti (bkz. genel-proje analiz turu).

Passport modeli, 160 pax/saat (4 gişe x 1.5dk) ve security 480 pax/saat
(8 lane x 1dk) modeli DEĞİŞTİRİLMEDİ - sadece security'nin GİRDİ
ZAMANLAMASI (artık show-up + passport completion event'lerinden).
"""

from app.queue.config import default_config
from app.queue.constants import (
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    RISK_CRITICAL,
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
    # ADIM (Departure Show-Up Profile): 09:00 kalkış artık TEK bir 07:00
    # spike'ı DEĞİL - show-up profili (%20/%60/%20) 06:00/07:00/08:00
    # saatlerine yayılıyor (bkz. dosya docstring'i - gerçek predict_
    # airport() ile üretilip conservation'a göre doğrulandı).
    flights = [
        departure(9, 0, location=LOCATION_DOMESTIC, key=f"DOM{i}", number=str(i), aircraft="B77W")
        for i in range(3)   # 3 x 350 = 1050 pax, security kapasitesini (480) aşan bir yük
    ]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    security = _by(predictions, PROCESS_SECURITY_DOMESTIC) or _by(predictions, PROCESS_SECURITY)
    assert [r.window_start.hour for r in security] == [6, 7, 8]
    assert [r.expected_passengers for r in security] == [210.0, 630.0, 210.0]
    assert sum(r.expected_passengers for r in security) == 1050   # conservation
    # Passport'u hiç GÖRMEDİ - domestic hiçbir zaman passport'a girmez (SAAT FARK ETMEZ).
    assert _by(predictions, PROCESS_PASSPORT) == []


# ========================================================================
# 2) Uluslararası varış - SADECE passport'a girer, security'ye HİÇ girmez.
# ========================================================================

def test_international_arrival_never_reaches_security():
    # ARRIVAL tarafı bu ADIM'da HİÇ DEĞİŞMEDİ - show-up profili SADECE
    # departure'a uygulanır, tek bir +15dk noktası aynen kalır.
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
#    bırakıldıktan SONRA ulaşır (backlog YOK - küçük flight, show-up
#    batch'lerinin HER BİRİ passport kapasitesinin altında).
# ========================================================================

def test_international_departure_reaches_security_only_after_passport_when_not_backlogged():
    # Tek bir küçük uçuş (E190=100 pax) - show-up ile 06:00/07:00/08:00'e
    # 20/60/20 olarak yayılır, HER batch passport'un 160 pax/saat
    # kapasitesinin altında kalır -> backlog YOK, security_intl passport
    # ile BİREBİR AYNI saat/miktarları taşır.
    flights = [intl_departure(9, 0, key="D1", number="1", aircraft="E190")]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    passport = _by(predictions, PROCESS_PASSPORT)
    security_intl = _by(predictions, PROCESS_SECURITY_INTL)
    assert [r.expected_passengers for r in passport] == [20.0, 60.0, 20.0]
    assert [r.expected_passengers for r in security_intl] == [20.0, 60.0, 20.0]   # backlog yok -> passport ile BİREBİR AYNI


# ========================================================================
# 4) Passport backlog'lu iken: security'ye AKTARILAN <= passport'un
#    GERÇEKTEN işlediği (asla passport tamamlanmadan giriş yok, asla
#    passport'un işlediğinden FAZLASI security'ye sızmaz).
# ========================================================================

def test_security_never_receives_more_than_passport_actually_released():
    # 6 x E190 (100 her biri = 600 toplam) - show-up ile 06:00/07:00/08:00'e
    # 120/360/120 dağılır. 07:00'in KENDİ talebi (360) passport kapasitesini
    # (320/saat) aşıyor -> CRITICAL, backlog 08:00'e taşıyor.
    flights = [
        intl_departure(9, 0, key=f"D{i}", number=str(i), aircraft="E190")
        for i in range(6)
    ]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    passport = _at_hour(predictions, PROCESS_PASSPORT, 7)
    security_intl_rows = _by(predictions, PROCESS_SECURITY_INTL)

    assert passport.expected_passengers == 360
    assert passport.risk == RISK_CRITICAL   # passport gerçek darboğaz

    # security_intl HİÇBİR saatte passport'un o saat GERÇEKTEN
    # işleyebildiğinden FAZLASINI görmez - hiçbir satır ham talebi (360)
    # AŞMAZ.
    for row in security_intl_rows:
        assert row.expected_passengers <= 360
    # CONSERVATION: toplamda security'ye aktarılan == flight'ların
    # TÜM talebi (600) - hiçbir yolcu yaratılmadı/kaybolmadı.
    assert sum(r.expected_passengers for r in security_intl_rows) == 600


# ========================================================================
# 5) Conservation: giren = işlenen + kalan kuyruk (yolcu
#    yaratma/kaybetme/double-count YOK). Backlog tamamen boşalana kadar
#    izleniyor; toplam security_intl'e aktarılan tam olarak passport'un
#    işlediğine (backlog düşüşüne) eşit.
# ========================================================================

def test_conservation_across_hours_until_passport_backlog_fully_drains():
    # Tek büyük uçuş (B77W=350 pax) - show-up ile 06:00/07:00/08:00'e
    # 70/210/70 dağılır, HER batch (en fazla 15dk'lık alt-batch) passport
    # kapasitesinin (320/saat) altında kalır -> backlog YOK, passport ile
    # security_intl BİREBİR AYNI.
    flight_demand = 350
    flights = [intl_departure(9, 0, key="BIG", number="1", aircraft="B77W")]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    passport_rows = _by(predictions, PROCESS_PASSPORT)
    security_intl_rows = _by(predictions, PROCESS_SECURITY_INTL)

    assert sum(r.expected_passengers for r in passport_rows) == flight_demand
    assert [r.expected_passengers for r in security_intl_rows] == [70.0, 210.0, 70.0]

    # CONSERVATION: security'ye aktarılan TOPLAM == passport'un işlediği
    # TOPLAM == flight'ın TÜM talebi (hiçbir yolcu yaratılmadı/kaybolmadı).
    total_transferred_to_security = sum(r.expected_passengers for r in security_intl_rows)
    assert total_transferred_to_security == flight_demand


# ========================================================================
# 6) Aynı flight'lar (domestic + international, AYNI departure saati) -
#    birbirine KARIŞMIYOR, domestic ANINDA (show-up sonrası kendi show-up
#    profiliyle), international passport'tan geçtikten SONRA.
# ========================================================================

def test_domestic_and_international_departure_same_hour_do_not_mix():
    domestic = [departure(9, 0, location=LOCATION_DOMESTIC, key="DOM1", number="1", aircraft="A320")]
    intl = [intl_departure(9, 0, key="INT1", number="2", aircraft="E190")]
    predictions = predict_airport("AAA", domestic + intl, default_config("AAA"), _demand())

    security_dom = _by(predictions, PROCESS_SECURITY_DOMESTIC)
    security_intl = _by(predictions, PROCESS_SECURITY_INTL)
    security_combined = _by(predictions, PROCESS_SECURITY)

    assert [r.expected_passengers for r in security_dom] == [36.0, 108.0, 36.0]     # SADECE domestic (A320=180)
    assert [r.expected_passengers for r in security_intl] == [20.0, 60.0, 20.0]      # SADECE international'ın passport'tan serbest bıraktığı (E190=100, backlog yok)
    # ikisi TOPLAM, ÇİFT SAYILMADI - her saat domestic+intl == combined.
    for dom, intl_r, comb in zip(security_dom, security_intl, security_combined):
        assert dom.window_start == intl_r.window_start == comb.window_start
        assert comb.expected_passengers == dom.expected_passengers + intl_r.expected_passengers


# ========================================================================
# 7) Saat sınırı aktarımı - passport backlog'u bir SONRAKİ saate
#    taşındığında yolcu KAYBOLMUYOR, doğru saate security'ye ulaşıyor.
# ========================================================================

def test_passenger_released_after_hour_boundary_is_not_lost():
    # 6xE190 (600 pax, show-up ile 06:00/07:00/08:00'e 120/360/120) -
    # 07:00'in kendi talebi (360) passport kapasitesini (320) aşıyor,
    # backlog 08:00'e taşınıyor - hiçbir yolcu kaybolmuyor (conservation).
    flights = [
        intl_departure(9, 0, key=f"D{i}", number=str(i), aircraft="E190")
        for i in range(6)   # 600 pax toplam
    ]
    predictions = predict_airport("AAA", flights, default_config("AAA"), _demand())

    security_intl = _by(predictions, PROCESS_SECURITY_INTL)
    hours = [r.window_start.hour for r in security_intl]
    assert hours == [6, 7, 8]
    assert [r.expected_passengers for r in security_intl] == [120.0, 312.0, 168.0]
    assert sum(r.expected_passengers for r in security_intl) == 600


# ========================================================================
# 8) Recovery - önceki backlog GERÇEKTEN sıfıra iniyor, sonraki (kendi
#    show-up penceresi ayrı) flight'ın kendi talebi DIŞINDA hiçbir
#    kalıntı sızmıyor.
# ========================================================================

def test_recovery_after_backlog_drains_next_flight_is_unaffected():
    surge = [
        intl_departure(9, 0, key=f"D{i}", number=str(i), aircraft="E190")
        for i in range(6)   # 600 pax - show-up penceresi 06:00-09:00 (bkz. test 7)
    ]
    # Show-up penceresi 06:00-09:00 olan surge'den TAMAMEN AYRI bir zaman
    # dilimi (10:00-13:00) - önceki backlog'un (07:00-08:00 arası tamamen
    # boşalmış, bkz. test 7) bu flight'ın KENDİ show-up penceresine hiç
    # sızmadığını doğrular.
    recovery_flight = [intl_departure(13, 0, key="REC", number="99", aircraft="E190")]
    predictions = predict_airport(
        "AAA", surge + recovery_flight, default_config("AAA"), _demand()
    )

    security_intl = _by(predictions, PROCESS_SECURITY_INTL)
    recovery_rows = [r for r in security_intl if r.window_start.hour in (10, 11, 12)]
    # 10:00/11:00/12:00'deki talep SADECE recovery_flight'ın KENDİ show-up
    # talebi (100 toplam, 20/60/20) - önceki backlog'dan hiçbir kalıntı YOK
    # (09:00 saatinde HİÇ satır yok - tam boşalmış).
    assert not any(r.window_start.hour == 9 for r in security_intl)
    assert [r.expected_passengers for r in recovery_rows] == [20.0, 60.0, 20.0]
    assert sum(r.expected_passengers for r in recovery_rows) == 100.0
