"""
AŞAMA 3 + AŞAMA 4 - Yolcu talebi ve zaman penceresi toplama.

Kapasite çözümlemesi Madde 1'in servisinden gelir; o kod BURADA
DEĞİŞTİRİLMEZ, sadece enjekte edilip kullanılır. Enjeksiyon sayesinde
bu katman mock bir çözümleyiciyle veritabanısız test edilebilir.
"""

from datetime import datetime, timedelta
from typing import Sequence

from .flight_rules import security_arrival_buffer_minutes
from ..constants import (
    DEMAND_WINDOW_MINUTES,
    DIRECTION_DEPARTURE,
    EXCLUDED_STATUSES,
    PASSPORT_RELEASE_BUFFER_MINUTES,
)


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

        ADIM (ICAO Demand Kalibrasyonu): artık `route_based_load_factor()`
        İLE ÇARPILMAZ - resolver'ın çözdüğü koltuk kapasitesi DOĞRUDAN
        yolcu talebi sayılır (ICAO A320 -> capacity=180 ise demand=180).
        Load factor fonksiyonu (bkz. `flight_rules.route_based_load_factor`)
        SİLİNMEDİ - kendi birim testlerinde hâlâ mevcut/geçerli bir saf
        fonksiyon olarak duruyor, sadece prediction zincirinden AYRILDI.

        counts_toward_passenger_total False ise (genel havacılık)
        talep hesabına HİÇ girmez.
        """
        result = self._resolve(flight)
        if not result.counts_toward_passenger_total:
            return 0
        return result.capacity


def effective_time(flight) -> datetime | None:
    """
    Uçuşun kuyruğa yansıdığı an.

    Departure : actual > estimated > scheduled, EKSİ dinamik security buffer
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
        return base - timedelta(minutes=security_arrival_buffer_minutes(flight))

    base = (
        flight.arr_actual_utc
        or flight.arr_estimated_utc
        or flight.arr_scheduled_utc
    )
    if base is None:
        return None
    return base + timedelta(minutes=PASSPORT_RELEASE_BUFFER_MINUTES)


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
