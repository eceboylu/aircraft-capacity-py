"""
AŞAMA 2 - Hangi akış hangi kuyruğu besler.

  domestic_departure      -> security
  international_departure -> security + passport
  international_arrival   -> passport
  domestic_arrival        -> HİÇBİR kuyruğu beslemez

domestic_arrival için ayrı bir akış anahtarı üretilmez; raporlama
katmanında da yer almaz (AŞAMA 9).

direction ve location alanları ingestion sırasında bir kez yazılır;
burada YENİDEN HESAPLANMAZ, doğrudan okunur.
"""

from typing import Sequence

from ..constants import (
    DIRECTION_ARRIVAL,
    DIRECTION_DEPARTURE,
    LOCATION_DOMESTIC,
    LOCATION_INTERNATIONAL,
)

FLOW_DOMESTIC_DEPARTURE = "domestic_departure"
FLOW_INTERNATIONAL_DEPARTURE = "international_departure"
FLOW_INTERNATIONAL_ARRIVAL = "international_arrival"

# Raporlamada görünen akışlar - domestic_arrival bilinçli olarak YOK.
REPORTED_FLOWS = (
    FLOW_DOMESTIC_DEPARTURE,
    FLOW_INTERNATIONAL_DEPARTURE,
    FLOW_INTERNATIONAL_ARRIVAL,
)


def is_domestic_departure(flight) -> bool:
    return (
        flight.direction == DIRECTION_DEPARTURE
        and flight.location == LOCATION_DOMESTIC
    )


def is_international_departure(flight) -> bool:
    return (
        flight.direction == DIRECTION_DEPARTURE
        and flight.location == LOCATION_INTERNATIONAL
    )


def is_international_arrival(flight) -> bool:
    return (
        flight.direction == DIRECTION_ARRIVAL
        and flight.location == LOCATION_INTERNATIONAL
    )


def _requires_passport(flight) -> bool:
    """
    ADIM (Schengen-Aware Passport Routing) - `flight.requires_passport`
    OKUNUR, YENİDEN HESAPLANMAZ (ingestion sırasında bir kez yazılır,
    bkz. `ingestion/sources.py:parse_source_a_record`). Alan henüz
    yoksa (ör. eski/mock bir flight nesnesi - test double'ları)
    `getattr` varsayılanı `True` - GÜVENLİ TARAF, mevcut/eski
    "her international passport gerektirir" davranışını korur.
    """
    return getattr(flight, "requires_passport", True)


def is_international_departure_requiring_passport(flight) -> bool:
    """`is_international_departure()`'ın ALT KÜMESİ: Schengen->Schengen HARİÇ."""
    return is_international_departure(flight) and _requires_passport(flight)


def is_international_arrival_requiring_passport(flight) -> bool:
    """`is_international_arrival()`'ın ALT KÜMESİ: Schengen->Schengen HARİÇ."""
    return is_international_arrival(flight) and _requires_passport(flight)


def is_schengen_departure_skipping_passport(flight) -> bool:
    """
    International kalkış AMA pasaport GEREKMEZ (Schengen->Schengen).
    Bu uçuşlar `location`'da international KALIR (traffic type
    değişmez) ve HÂLÂ international security'ye girer - SADECE passport
    aşamasını atlarlar (bkz. `engine.py:_event_driven_queue_demand` -
    show-up zamanı DOĞRUDAN security'nin arrival_time'ı olur, passport
    completion_time'ından TÜRETİLMEZ).
    """
    return is_international_departure(flight) and not _requires_passport(flight)


def flow_of(flight) -> str | None:
    """Uçuşun ait olduğu akış; domestic arrival için None."""
    if is_domestic_departure(flight):
        return FLOW_DOMESTIC_DEPARTURE
    if is_international_departure(flight):
        return FLOW_INTERNATIONAL_DEPARTURE
    if is_international_arrival(flight):
        return FLOW_INTERNATIONAL_ARRIVAL
    return None


def security_flights(flights: Sequence) -> list:
    """Security kuyruğunu besleyen uçuşlar: TÜM kalkışlar (birleşik, DEĞİŞMEDİ)."""
    return [f for f in flights if f.direction == DIRECTION_DEPARTURE]


def security_domestic_flights(flights: Sequence) -> list:
    """
    ADIM (Security Domestic/International Split) - `security_flights()`'ın
    (TÜM kalkışlar) bir ALT KÜMESİ: sadece domestic kalkışlar. Yeni bir
    akış kuralı İCAT EDİLMEDİ - `is_domestic_departure()` zaten mevcut,
    değişmeyen predicate.
    """
    return [f for f in flights if is_domestic_departure(f)]


def security_international_flights(flights: Sequence) -> list:
    """`security_flights()`'ın ALT KÜMESİ: sadece international kalkışlar."""
    return [f for f in flights if is_international_departure(f)]


def passport_flights(flights: Sequence) -> list:
    """
    Passport kuyruğunu besleyen uçuşlar:
    uluslararası kalkış + uluslararası varış.

    ADIM (Schengen-Aware Passport Routing) - Schengen->Schengen uçuşlar
    ARTIK HARİÇ (bkz. `passport_departure_flights`/`passport_arrival_
    flights` docstring'i). Bu, legacy BİRLEŞİK `PROCESS_PASSPORT`'un da
    kullandığı fonksiyondur (`engine.py:_event_driven_queue_demand`) -
    formül/kapasite hesabı DEĞİŞMEDİ, sadece HANGİ uçuşların passport
    demand'i ÜRETTİĞİ düzeltildi (demand inflation'ın kök nedeni).
    """
    return [
        f for f in flights
        if is_international_departure_requiring_passport(f)
        or is_international_arrival_requiring_passport(f)
    ]


def passport_departure_flights(flights: Sequence) -> list:
    """
    ADIM (4-Graph API Contract) - `passport_flights()`'ın ALT KÜMESİ:
    SADECE kalkış-bağlantılı (International Departure grafiğinin passport
    aşaması) uçuşlar.

    ADIM (Schengen-Aware Passport Routing) - Schengen->Schengen kalkışlar
    ARTIK HARİÇ tutulur (`is_international_departure_requiring_passport`)
    - bu uçuşlar pasaport kontrolünden GEÇMEZ, dolayısıyla passport
    kuyruğu demand'ine KATKI VERMEMELİDİR (hâlâ international security'ye
    girerler - bkz. `is_schengen_departure_skipping_passport`). Fiziksel
    passport havuzu bu ayrımdan ETKİLENMEZ (bkz. Bölüm 17) - bu SADECE
    raporlama/cohort ayrımı içindir.
    """
    return [f for f in flights if is_international_departure_requiring_passport(f)]


def passport_arrival_flights(flights: Sequence) -> list:
    """
    `passport_flights()`'ın ALT KÜMESİ: SADECE varış-bağlantılı
    (International Arrival grafiği) uçuşlar.

    ADIM (Schengen-Aware Passport Routing) - Schengen->Schengen varışlar
    ARTIK HARİÇ tutulur (bkz. `passport_departure_flights` docstring'i,
    aynı gerekçe - "NO Arrival Passport modeled" for Schengen->Schengen).
    """
    return [f for f in flights if is_international_arrival_requiring_passport(f)]
