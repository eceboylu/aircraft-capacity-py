"""
AŞAMA 6 - Operasyonel Neden Tespiti.

Burada tanımlı 9 tespit fonksiyonunun DIŞINDA hiçbir neden
üretilmez. Hiçbiri eşiği geçmezse tek bir "Normal operasyonel
yoğunluk" maddesi yazılır; boş liste ASLA dönmez.

Her mesaj, dokümanda tanımlı şablondan doldurulur - serbest/jenerik
metin yazılmaz.

Kombinasyonlar için ayrı kod yoktur: birden fazla neden aynı
pencerede tetiklenirse bu zaten bir kombinasyondur.

SAF KATMAN: veritabanına gitmez. Baseline, utilization ve uçak
değişikliği bilgisi çağıran taraftan hazır olarak gelir.
"""

from dataclasses import dataclass
from typing import Callable, Sequence

from ..constants import (
    ARRIVAL_BANK_DEFAULT_THRESHOLD,
    CLUSTERING_RATIO_THRESHOLD,
    DELAY_CLUSTER_THRESHOLD,
    DELAY_SIGNIFICANT_MINUTES,
    DEMAND_WINDOW_MINUTES,
    DIRECTION_ARRIVAL,
    INTL_SHARE_THRESHOLD,
    LOCATION_INTERNATIONAL,
    NORMAL_REASON_MESSAGE,
    PROCESS_PASSPORT,
    REASON_AIRCRAFT_CHANGE,
    REASON_ARRIVAL_BANK,
    REASON_CANCELLATION,
    REASON_CLUSTERING,
    REASON_DELAY_COMPRESSION,
    REASON_DIVERSION,
    REASON_INTL_SHARE,
    REASON_NORMAL,
    REASON_SINGLE_DELAY,
    REASON_UTILIZATION,
    REASON_WIDEBODY,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    STATUS_CANCELLED,
    STATUS_DIVERTED,
    UTILIZATION_REASON_THRESHOLD,
    WIDEBODY_COUNT_THRESHOLD,
    WIDEBODY_SEAT_THRESHOLD,
    WIDEBODY_SHARE_THRESHOLD,
)
from ..domain.flight_rules import delay_minutes


@dataclass
class DetectedReason:
    code: str            # "clustering", "widebody", "delay_compression", ...
    severity: str        # "info" | "warning" | "critical"
    message: str         # şablondan doldurulmuş, kullanıcıya gösterilecek
    metric_value: float  # tetikleyen ham değer (log/debug için)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "metric_value": self.metric_value,
        }


# --------------------------------------------------------------------
# Neden 1 - Departure/Arrival Clustering
# --------------------------------------------------------------------

def detect_clustering(
    window_flights: Sequence,
    historical_baseline: float | None,
    window_label: str,
) -> DetectedReason | None:
    """
    Baseline yoksa bu neden ATLANIR - sahte baseline üretilmez.
    """
    if not historical_baseline:
        return None

    current = len(window_flights)
    ratio = current / historical_baseline
    if ratio <= CLUSTERING_RATIO_THRESHOLD:
        return None

    return DetectedReason(
        code=REASON_CLUSTERING,
        severity=SEVERITY_WARNING,
        message=(
            f"{window_label}: {current} uçuş, "
            f"geçmiş ortalamanın {ratio:.1f} katı"
        ),
        metric_value=round(ratio, 3),
    )


# --------------------------------------------------------------------
# Neden 2 - Wide-body / Büyük Uçak Yoğunluğu
# --------------------------------------------------------------------

def detect_widebody(
    window_flights: Sequence,
    seat_capacity_fn: Callable[[object], int],
    demand_fn: Callable[[object], int],
) -> DetectedReason | None:
    if not window_flights:
        return None

    widebody = [
        f for f in window_flights
        if seat_capacity_fn(f) >= WIDEBODY_SEAT_THRESHOLD
    ]
    count = len(widebody)
    if count == 0:
        return None

    share = count / len(window_flights)
    if not (count >= WIDEBODY_COUNT_THRESHOLD or share > WIDEBODY_SHARE_THRESHOLD):
        return None

    total_pax = sum(demand_fn(f) for f in widebody)
    return DetectedReason(
        code=REASON_WIDEBODY,
        severity=SEVERITY_WARNING,
        message=(
            f"{count} geniş gövde uçak aynı pencerede "
            f"(toplam ~{total_pax} yolcu)"
        ),
        metric_value=float(count),
    )


# --------------------------------------------------------------------
# Neden 3 - Delay-Based Compression
# --------------------------------------------------------------------

def detect_delay_compression(window_flights: Sequence) -> DetectedReason | None:
    """
    Birden fazla gecikmiş uçuş aynı pencereye toplandıysa tetiklenir.
    Tek gecikmiş uçuş riski ARTIRMAZ, sadece bilgi notu üretir.
    """
    delayed = [
        f for f in window_flights
        if delay_minutes(f) > DELAY_SIGNIFICANT_MINUTES
    ]
    count = len(delayed)

    if count == 0:
        return None

    if count == 1:
        single = delayed[0]
        return DetectedReason(
            code=REASON_SINGLE_DELAY,
            severity=SEVERITY_INFO,
            message=f"1 uçuş {delay_minutes(single)} dakika gecikmeli",
            metric_value=float(delay_minutes(single)),
        )

    if count < DELAY_CLUSTER_THRESHOLD:
        return None

    avg_delay = sum(delay_minutes(f) for f in delayed) / count
    return DetectedReason(
        code=REASON_DELAY_COMPRESSION,
        severity=SEVERITY_WARNING,
        message=(
            f"{count} gecikmiş uçuş aynı pencereye toplandı "
            f"(ort. {avg_delay:.0f} dk gecikme)"
        ),
        metric_value=float(count),
    )


# --------------------------------------------------------------------
# Neden 4 - Utilization (SADECE passport)
# --------------------------------------------------------------------

def detect_utilization(process: str, rho: float | None) -> DetectedReason | None:
    """
    Security'de kapasite verisi olmadığı için bu metrik YOKTUR;
    neden sadece passport çıktısında görünür.
    """
    if process != PROCESS_PASSPORT or rho is None:
        return None
    if rho <= UTILIZATION_REASON_THRESHOLD:
        return None

    return DetectedReason(
        code=REASON_UTILIZATION,
        severity=SEVERITY_WARNING,
        message=(
            f"Talep/kapasite oranı {rho:.2f} — "
            f"kapasitenin {rho * 100:.0f}%'i kullanılıyor"
        ),
        metric_value=round(rho, 3),
    )


# --------------------------------------------------------------------
# Neden 5 - International Yoğunluğu (SADECE passport)
# --------------------------------------------------------------------

def detect_intl_share(
    process: str,
    window_flights: Sequence,
) -> DetectedReason | None:
    if process != PROCESS_PASSPORT or not window_flights:
        return None

    intl_count = len([
        f for f in window_flights if f.location == LOCATION_INTERNATIONAL
    ])
    share = intl_count / len(window_flights)
    if share <= INTL_SHARE_THRESHOLD:
        return None

    return DetectedReason(
        code=REASON_INTL_SHARE,
        severity=SEVERITY_WARNING,
        message=(
            f"Uçuşların {share * 100:.0f}%'i uluslararası — "
            f"passport yükü artıyor"
        ),
        metric_value=round(share, 3),
    )


# --------------------------------------------------------------------
# Neden 6 - Arrival Bank (SADECE passport)
# --------------------------------------------------------------------

def detect_arrival_bank(
    process: str,
    window_flights: Sequence,
    threshold: int = ARRIVAL_BANK_DEFAULT_THRESHOLD,
    window_minutes: int = DEMAND_WINDOW_MINUTES,
) -> DetectedReason | None:
    """
    Etkisi sadece passport'a yansır, security'ye değil.
    Eşik havalimanı bazlı override edilebilir.
    """
    if process != PROCESS_PASSPORT:
        return None

    arrival_count = len([
        f for f in window_flights if f.direction == DIRECTION_ARRIVAL
    ])
    if arrival_count < threshold:
        return None

    return DetectedReason(
        code=REASON_ARRIVAL_BANK,
        severity=SEVERITY_WARNING,
        message=(
            f"{arrival_count} uçuş aynı {window_minutes} dk "
            f"pencerede iniyor"
        ),
        metric_value=float(arrival_count),
    )


# --------------------------------------------------------------------
# Neden 7 - Cancellation Etkisi
# --------------------------------------------------------------------

def detect_cancellation(all_period_flights: Sequence) -> DetectedReason | None:
    """
    Talebi DÜŞÜREN faktör; riski artırmaz, şeffaflık için gösterilir.
    """
    cancelled = [f for f in all_period_flights if f.status == STATUS_CANCELLED]
    if not cancelled:
        return None

    return DetectedReason(
        code=REASON_CANCELLATION,
        severity=SEVERITY_INFO,
        message=(
            f"{len(cancelled)} uçuş iptal edildi, talepten çıkarıldı. "
            f"Yolcuların nereye aktarıldığı bilinmiyor"
        ),
        metric_value=float(len(cancelled)),
    )


# --------------------------------------------------------------------
# Neden 8 - Aircraft Change (Kapasite Değişikliği)
# --------------------------------------------------------------------

def detect_aircraft_changes(
    aircraft_changes: dict[str, list[tuple[str | None, str | None]]] | None,
    capacity_of_icao: Callable[[str | None], int] | None,
) -> list[DetectedReason]:
    """
    aircraft_changes : {flight_key: [(old_icao, new_icao), ...]} -
                       FlightEvent tablosundaki AIRCRAFT_CHANGED
                       kayıtlarından gelir.

    MADDE 8: aynı uçuşun aynı pencerede birden fazla değişimi varsa
    (ör. A320→A321, sonra A321→A330) HER İKİSİ de ayrı bir
    DetectedReason olarak döner - sadece son değişiklik değil. Liste
    içindeki sıra KORUNUR (çağıran taraf kronolojik sırayla verir),
    böylece mesajlar da kronolojik çıkar.

    Her değişiklik AYRI değerlendirilir: capacity_delta > 0 risk
    artırıcı, < 0 risk azaltıcı nottur (severity ile taşınır), delta
    == 0 olan tek tek değişiklikler atlanır - flight'ın DİĞER gerçek
    değişiklikleri bundan etkilenmez. Bu notlar risk skorunu (rho,
    baseline_ratio) DOĞRUDAN değiştirmez; yalnızca açıklama/context'tir.
    """
    if not aircraft_changes or capacity_of_icao is None:
        return []

    found = []
    for flight_key in sorted(aircraft_changes):
        for old_icao, new_icao in aircraft_changes[flight_key]:
            if not old_icao or not new_icao or old_icao == new_icao:
                continue

            delta = capacity_of_icao(new_icao) - capacity_of_icao(old_icao)
            if delta == 0:
                continue

            found.append(DetectedReason(
                code=REASON_AIRCRAFT_CHANGE,
                severity=SEVERITY_WARNING if delta > 0 else SEVERITY_INFO,
                message=(
                    f"{flight_key}: {old_icao}→{new_icao}, "
                    f"kapasite {delta:+d} yolcu"
                ),
                metric_value=float(delta),
            ))
    return found


# --------------------------------------------------------------------
# Neden 9 - Diversion
# --------------------------------------------------------------------

def detect_diversions(all_period_flights: Sequence) -> list[DetectedReason]:
    """
    Yönlendirilen uçuş talepten çıkarılır. Downstream etkisi
    (transit yolcu, yeniden yönlendirme operasyonu) MODELLENMEZ.
    """
    return [
        DetectedReason(
            code=REASON_DIVERSION,
            severity=SEVERITY_INFO,
            message=f"{f.flight_key} yönlendirildi, talepten çıkarıldı",
            metric_value=1.0,
        )
        for f in all_period_flights
        if f.status == STATUS_DIVERTED
    ]


# --------------------------------------------------------------------
# Orkestrasyon
# --------------------------------------------------------------------

def detect_reasons(
    airport_iata: str,
    process: str,
    window_flights: Sequence,
    all_period_flights: Sequence,
    historical_baseline: float | None,
    rho: float | None,
    config,
    seat_capacity_fn: Callable[[object], int],
    demand_fn: Callable[[object], int],
    window_label: str = "",
    aircraft_changes: dict[str, tuple[str | None, str | None]] | None = None,
    capacity_of_icao: Callable[[str | None], int] | None = None,
) -> list[DetectedReason]:
    """
    9 tespit fonksiyonunun HEPSİ çalıştırılır, eşiği geçenler
    listeye eklenir. Hiçbiri geçmezse "Normal operasyonel yoğunluk".
    """
    reasons: list[DetectedReason] = []

    threshold = getattr(
        config, "arrival_bank_threshold", ARRIVAL_BANK_DEFAULT_THRESHOLD
    )

    candidates = [
        detect_clustering(window_flights, historical_baseline, window_label),
        detect_widebody(window_flights, seat_capacity_fn, demand_fn),
        detect_delay_compression(window_flights),
        detect_utilization(process, rho),
        detect_intl_share(process, window_flights),
        detect_arrival_bank(process, window_flights, threshold),
        detect_cancellation(all_period_flights),
    ]
    reasons.extend(r for r in candidates if r is not None)
    reasons.extend(detect_aircraft_changes(aircraft_changes, capacity_of_icao))
    reasons.extend(detect_diversions(all_period_flights))

    if not reasons:
        reasons.append(DetectedReason(
            code=REASON_NORMAL,
            severity=SEVERITY_INFO,
            message=NORMAL_REASON_MESSAGE,
            metric_value=0.0,
        ))

    return reasons
