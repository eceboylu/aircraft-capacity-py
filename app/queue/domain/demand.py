"""
AŞAMA 3 + AŞAMA 4 - Yolcu talebi ve zaman penceresi toplama.

Kapasite çözümlemesi Madde 1'in servisinden gelir; o kod BURADA
DEĞİŞTİRİLMEZ, sadece enjekte edilip kullanılır. Enjeksiyon sayesinde
bu katman mock bir çözümleyiciyle veritabanısız test edilebilir.
"""

from datetime import datetime, timedelta
from typing import Sequence

from ..constants import (
    ARRIVAL_RELEASE_PROFILE,
    DEMAND_WINDOW_MINUTES,
    DEPARTURE_PASSENGER_ARRIVAL_OFFSET_MINUTES,
    DEPARTURE_SHOW_UP_PROFILE,
    DIRECTION_DEPARTURE,
    EXCLUDED_STATUSES,
    PASSPORT_RELEASE_BUFFER_MINUTES,
)
from .occupancy_calibration import occupancy_factor_for


class DemandCalculator:
    """
    Madde 1'in AircraftCapacityService'ini sarmalar.

    resolver: .resolve(aircraft_icao, airline_iata) -> CapacityResult
              (.capacity, .counts_toward_passenger_total)
    """

    def __init__(self, resolver):
        self._resolver = resolver
        self._cache: dict[tuple[str | None, str | None], object] = {}

    def _resolve(self, flight):
        key = (flight.aircraft_icao, flight.airline_iata)
        if key not in self._cache:
            self._cache[key] = self._resolver.resolve(
                flight.aircraft_icao, flight.airline_iata
            )
        return self._cache[key]

    def seat_capacity(self, flight) -> int:
        """
        Uçağın koltuk kapasitesi (doluluk uygulanmadan).
        Genel havacılık uçuşlarında 0 döner.
        """
        result = self._resolve(flight)
        if not result.counts_toward_passenger_total:
            return 0
        return result.capacity

    def capacity_of_icao(self, aircraft_icao: str | None) -> int:
        """
        Uçak tipinin koltuk kapasitesi (uçuş nesnesi olmadan).
        Neden 8'de eski/yeni tip karşılaştırması için kullanılır.
        """
        result = self._resolver.resolve(aircraft_icao, None)
        if not result.counts_toward_passenger_total:
            return 0
        return result.capacity

    def passenger_demand(self, flight) -> int:
        """
        Bu uçuşun kuyruğa getireceği tahmini yolcu sayısı.

        ADIM (ICAO Demand Kalibrasyonu): resolver'ın çözdüğü koltuk
        kapasitesi (`AircraftCapacityService` - HİÇ DEĞİŞTİRİLMEDİ)
        hâlâ TEK girdi; `route_based_load_factor()` (silinmedi, kendi
        birim testlerinde geçerli, prediction zincirinden AYRI) hâlâ
        KULLANILMIYOR.

        ADIM (Passenger Demand Calibration) - resolve edilen koltuk
        sayısı artık DOĞRUDAN talep DEĞİL; `occupancy_calibration.
        occupancy_factor_for(flight.airport_iata)`'ın döndürdüğü,
        SADECE bu turda gerçek kaynaktan araştırılmış 9 havalimanı için
        tanımlı bir çarpanla ayarlanır (bkz. o modülün SOURCE/DATE/VALUE
        metodolojisi). Araştırılmamış HERHANGİ bir havalimanı için bu
        çarpan 1.0 (ESKİ %100-koltuk davranışı, DEĞİŞMEDEN) - production'daki
        diğer tüm havalimanları (ve onları kullanan mevcut testler) bu
        ADIM'dan HİÇ ETKİLENMEZ. `round()` ile deterministik tam sayıya
        yuvarlanır (rastgelelik YOK - aynı girdi HER ZAMAN aynı çıktı).

        counts_toward_passenger_total False ise (genel havacılık)
        talep hesabına HİÇ girmez.
        """
        result = self._resolve(flight)
        if not result.counts_toward_passenger_total:
            return 0
        factor, _level = occupancy_factor_for(getattr(flight, "airport_iata", None))
        return round(result.capacity * factor)


def effective_time(flight) -> datetime | None:
    """
    Uçuşun kuyruğa yansıdığı an.

    Departure : actual > estimated > scheduled, EKSİ sabit 120 dk
                (DEPARTURE_PASSENGER_ARRIVAL_OFFSET_MINUTES - yolcunun
                havalimanına gerçekte geldiği an, uçuş süresinden BAĞIMSIZ)
    Arrival   : actual > estimated > scheduled, ARTI sabit passport buffer

    Zaman önceliği delay_minutes() ile AYNI olmak zorundadır: gecikme
    "estimated"tan okunup pencere "scheduled"a göre seçilirse, henüz
    gerçekleşmemiş uçuşta yolcular hiç var olmayacakları pencereye
    yazılır, gerçekten geldikleri pencere ise boş görünür. Gelecek
    uçuşlarda actual zaten yoktur; tahmin sisteminin asıl çalıştığı
    durum budur.

    Gecikmiş uçuşlarda kaymış saat kullanıldığı için, birden fazla
    gecikmiş uçuşun aynı yeni pencerede toplanması (schedule
    compression) ayrı bir dedektöre gerek kalmadan otomatik yakalanır.
    """
    if flight.direction == DIRECTION_DEPARTURE:
        base = (
            flight.dep_actual_utc
            or flight.dep_estimated_utc
            or flight.dep_scheduled_utc
        )
        if base is None:
            return None
        return base - timedelta(minutes=DEPARTURE_PASSENGER_ARRIVAL_OFFSET_MINUTES)

    base = (
        flight.arr_actual_utc
        or flight.arr_estimated_utc
        or flight.arr_scheduled_utc
    )
    if base is None:
        return None
    return base + timedelta(minutes=PASSPORT_RELEASE_BUFFER_MINUTES)


def _departure_show_up_base(flight) -> datetime | None:
    """
    Departure show-up profilinin T0'ı - flight'ın KENDİ gerçek departure
    referansı (actual > estimated > scheduled), `effective_time()`'ın
    departure dalıyla AYNI öncelik sırası ama `-120dk` OFFSET'İ
    UYGULANMADAN (offset artık TEK bir nokta değil, profilin KENDİSİ).
    """
    return (
        flight.dep_actual_utc
        or flight.dep_estimated_utc
        or flight.dep_scheduled_utc
    )


def departure_show_up_events(flight, total_demand: float) -> list[tuple[datetime, float]]:
    """
    ADIM (Departure Show-Up Profile) - bir departure flight'ın TOPLAM
    yolcu talebini (`DemandCalculator.passenger_demand(flight)` - bu
    fonksiyon KENDİSİ hesaplamaz, dışarıdan ALIR), `DEPARTURE_SHOW_UP_
    PROFILE`'a göre flight'ın KENDİ departure zamanından ÖNCEKİ 12 adet
    15 dakikalık deterministic batch'e böler.

    SAF/DETERMINISTIC: aynı flight + aynı total_demand HER ZAMAN aynı
    batch listesini üretir - RANDOM YOK. Her flight KENDİ show-up
    profilini KENDİ departure zamanına göre üretir (Bölüm 1/3) - farklı
    flight'ların batch'leri zaman ekseninde DOĞAL olarak ÜST ÜSTE biner
    (bu fonksiyon bunu ENGELLEMEZ/KOORDİNE ETMEZ - `simulate_fifo_queue`
    zaten farklı kaynaklardan gelen tuple'ları TEK bir kronolojik listede
    birleştirmek için tasarlanmış, bkz. core/event_queue.py).

    PASSENGER CONSERVATION: kümülatif yuvarlama (her batch = kümülatif
    hedefin YUVARLANMIŞ farkı) kullanılır - `sum(count for _, count in
    events) == round(total_demand)` HER ZAMAN tam sağlanır (tek tek
    batch yuvarlamalarının toplamda kayıp/fazlalık üretmesi
    matematiksel olarak İMKANSIZ hale gelir); pratikte bu, "son batch
    yuvarlama artığını emer" (Bölüm 4) ilkesiyle AYNI sonucu verir, ama
    ARA batch'lerin de asla negatif/tutarsız olmamasını garanti eder.

    `flight`'ın departure referans zamanı (actual>estimated>scheduled)
    yoksa VEYA `total_demand <= 0` ise boş liste döner - uydurma bir
    zaman/yolcu ÜRETİLMEZ (mevcut `effective_time()`/`passenger_demand()`
    ile AYNI "veri yoksa sessizce atla" ilkesi).

    Dönen liste `simulate_fifo_queue()`'nun beklediği `(timestamp, count)`
    ikili formatındadır - `origin` etiketi ÇAĞIRAN tarafça eklenir (bkz.
    `engine.py`), bu fonksiyon onu bilmez/karışmaz.
    """
    base = _departure_show_up_base(flight)
    if base is None or total_demand <= 0:
        return []

    events: list[tuple[datetime, float]] = []
    cumulative_target = 0.0
    cumulative_rounded = 0
    for minutes_before_start, _minutes_before_end, fraction in DEPARTURE_SHOW_UP_PROFILE:
        cumulative_target += total_demand * fraction
        new_cumulative_rounded = round(cumulative_target)
        count = new_cumulative_rounded - cumulative_rounded
        cumulative_rounded = new_cumulative_rounded
        if count <= 0:
            continue
        batch_time = base - timedelta(minutes=minutes_before_start)
        events.append((batch_time, float(count)))
    return events


def _arrival_release_base(flight) -> datetime | None:
    """
    Arrival release profilinin T0'ı - flight'ın KENDİ HAM arrival
    referansı (actual > estimated > scheduled). `effective_time()`'ın
    arrival dalıyla KARIŞTIRILMAZ: `effective_time()` bu tabana ZATEN
    `PASSPORT_RELEASE_BUFFER_MINUTES` (+15dk) EKLER - bu fonksiyon o
    +15dk'yı UYGULAMADAN, HAM zamanı döner. Release profilinin kendisi
    (`ARRIVAL_RELEASE_PROFILE`, +10/+15/+20/+25/+30dk noktaları) bu HAM
    tabanın ÜZERİNE uygulanır - `effective_time(flight)`'IN SONUCUNUN
    üzerine DEĞİL (bkz. `arrival_passenger_release_events()` docstring'i -
    ÇİFT OFFSET riski BİLİNÇLİ OLARAK burada önlenir).
    """
    return (
        flight.arr_actual_utc
        or flight.arr_estimated_utc
        or flight.arr_scheduled_utc
    )


def arrival_passenger_release_events(flight, total_demand: float) -> list[tuple[datetime, float]]:
    """
    ADIM (Arrival Release Profile) - bir international arrival flight'ın
    TOPLAM yolcu talebini (`DemandCalculator.passenger_demand(flight)` -
    bu fonksiyon KENDİSİ hesaplamaz, dışarıdan ALIR), `ARRIVAL_RELEASE_
    PROFILE`'a göre flight'ın KENDİ HAM arrival zamanından (bkz.
    `_arrival_release_base()` - `effective_time()` DEĞİL, ÇİFT OFFSET
    YOK) SONRAKİ 5 adet deterministic timestamp'e böler.

    ÖRNEK (çift-offset OLMADIĞININ kanıtı): raw arrival 14:00 ise İLK
    release event'i 14:10'dur (14:00 + ARRIVAL_RELEASE_PROFILE'ın ilk
    noktası "+10dk") - 14:25 (14:00 + effective_time'ın +15dk'sı + profil
    ilk noktasının +10dk'sı) DEĞİL. `effective_time()`'ın KENDİSİ bu
    fonksiyonun hesabına HİÇ KARIŞMAZ.

    SAF/DETERMINISTIC: aynı flight + aynı total_demand HER ZAMAN aynı
    batch listesini üretir - RANDOM YOK. Her flight KENDİ release
    profilini KENDİ arrival zamanına göre üretir - farklı flight'ların
    batch'leri zaman ekseninde DOĞAL olarak ÜST ÜSTE biner (bu fonksiyon
    bunu ENGELLEMEZ/KOORDİNE ETMEZ - `simulate_fifo_queue` zaten farklı
    kaynaklardan gelen tuple'ları TEK bir kronolojik listede birleştirmek
    için tasarlanmış, bkz. core/event_queue.py).

    PASSENGER CONSERVATION: `departure_show_up_events()` ile AYNI
    kümülatif yuvarlama tekniği (her batch = kümülatif hedefin
    YUVARLANMIŞ farkı) - `sum(count for _, count in events) ==
    round(total_demand)` HER ZAMAN tam sağlanır, ara batch'ler asla
    negatif/tutarsız olmaz. Base-time hesaplama (`_arrival_release_
    base()`) departure'ınkiyle (`_departure_show_up_base()`) BİLİNÇLİ
    OLARAK ortaklaştırılmadı - farklı alanları (arr_* vs dep_*) okurlar,
    tek bir yanlış paylaşım her ikisini de bozabilirdi.

    `flight`'ın arrival referans zamanı yoksa VEYA `total_demand <= 0`
    ise boş liste döner - uydurma bir zaman/yolcu ÜRETİLMEZ.

    Dönen liste `simulate_fifo_queue()`'nun beklediği `(timestamp, count)`
    ikili formatındadır - `origin` etiketi ÇAĞIRAN tarafça eklenir (bkz.
    `engine.py`).
    """
    base = _arrival_release_base(flight)
    if base is None or total_demand <= 0:
        return []

    events: list[tuple[datetime, float]] = []
    cumulative_target = 0.0
    cumulative_rounded = 0
    for minutes_after, fraction in ARRIVAL_RELEASE_PROFILE:
        cumulative_target += total_demand * fraction
        new_cumulative_rounded = round(cumulative_target)
        count = new_cumulative_rounded - cumulative_rounded
        cumulative_rounded = new_cumulative_rounded
        if count <= 0:
            continue
        batch_time = base + timedelta(minutes=minutes_after)
        events.append((batch_time, float(count)))
    return events


def flights_in_window(
    all_flights: Sequence,
    window_start: datetime,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    include_excluded: bool = False,
) -> list:
    """
    Pencereye düşen uçuşlar.

    Varsayılan davranışta iptal ve yönlendirilmiş uçuşlar talep
    hesabına GİRMEZ.

    include_excluded=True ise bu uçuşlar da döner; AŞAMA 6'daki
    iptal (Neden 7) ve yönlendirme (Neden 9) notlarının hangi
    pencereye ait olduğunu belirlemek için kullanılır - talep
    toplamına yine de eklenmezler.
    """
    window_end = window_start + timedelta(minutes=window_minutes)
    selected = []
    for flight in all_flights:
        if not include_excluded and flight.status in EXCLUDED_STATUSES:
            continue
        moment = effective_time(flight)
        if moment is None:
            continue
        if window_start <= moment < window_end:
            selected.append(flight)
    return selected
