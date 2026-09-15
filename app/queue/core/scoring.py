"""
AŞAMA 5 + AŞAMA 7 - Risk skorlama ve güven skoru.

SAF KATMAN: Bu modül veritabanına, dosyaya veya ağa dokunmaz.
Yolcu talebi bir fonksiyon olarak (demand_fn) dışarıdan enjekte
edilir; böylece mock verilerle bağımsız test edilebilir.

KRİTİK: Security ve passport AYNI formülü kullanmaz.
  - Security -> basit yoğunluk sinyali, Erlang-C DEĞİL, dakika YOK.
  - Passport  -> tam Erlang-C, dakika ÜRETİLİR.
"""

from typing import Callable, Sequence

from .erlang import erlang_c_wait_time
from ..constants import (
    AIRCRAFT_MATCH_RATE_THRESHOLD,
    CONFIDENCE_FLOOR,
    CONFIDENCE_PENALTY_BOARDING_BUFFER,
    CONFIDENCE_PENALTY_DEFAULT_CONFIG,
    CONFIDENCE_PENALTY_LOAD_FACTOR,
    CONFIDENCE_PENALTY_LOW_MATCH_RATE,
    CONFIDENCE_PENALTY_NO_BASELINE,
    CONFIDENCE_PENALTY_NULL_AIRCRAFT,
    DEMAND_WINDOW_MINUTES,
    NO_BASELINE_MESSAGE,
    PASSPORT_OVERLOAD_MESSAGE,
    PASSPORT_RHO_LOW,
    PASSPORT_RHO_MEDIUM,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
    SECURITY_FLIGHT_RATIO_WEIGHT,
    SECURITY_PASSENGER_RATIO_WEIGHT,
    SECURITY_RATIO_HIGH,
    SECURITY_RATIO_LOW,
    SECURITY_RATIO_MEDIUM,
)


def combined_security_ratio(
    flight_ratio: float, passenger_ratio: float | None
) -> float:
    """
    MADDE 7 - iki sinyalin ağırlıklı ortalaması.

        combined = (Wf * flight_ratio + Wp * passenger_ratio) / (Wf + Wp)

    Ağırlıklar toplamlarına bölünerek normalize edilir; böylece
    birleşik oran tek tek oranlarla AYNI ölçekte kalır ve mevcut risk
    bantları (1.2 / 1.4 / 1.8) değişmeden geçerli olur.

    passenger_ratio None ise (geçmiş yolcu verisi yok) yalnızca
    flight_ratio kullanılır - eksik sinyal 0 sayılıp riski YAPAY olarak
    aşağı çekmez, uydurma bir değerle de doldurulmaz. Bu, yolcu
    baseline'ı birikene kadar eski davranışın birebir korunması
    demektir.
    """
    if passenger_ratio is None:
        return flight_ratio

    total_weight = (
        SECURITY_FLIGHT_RATIO_WEIGHT + SECURITY_PASSENGER_RATIO_WEIGHT
    )
    if total_weight <= 0:
        return flight_ratio

    return (
        SECURITY_FLIGHT_RATIO_WEIGHT * flight_ratio
        + SECURITY_PASSENGER_RATIO_WEIGHT * passenger_ratio
    ) / total_weight


def security_density_score(
    window_flights: Sequence,
    historical_baseline: float | None,
    demand_fn: Callable[[object], int],
    historical_passenger_baseline: float | None = None,
) -> dict:
    """
    Security - yoğunluk sinyali. Erlang-C KULLANILMAZ (bkz. modül
    başlığı): gerçek gişe/kanal sayısı bilinmediği için kuyruk teorisi
    kurulmaz, dakika üretilmez.

    window_flights : domestic departure + international departure
    historical_baseline
                   : bu havalimanı + saat için geçmiş ortalama UÇUŞ
                     sayısı. Yoksa None - SAHTE DEĞER ÜRETİLMEZ.
    historical_passenger_baseline
                   : aynı pencere için geçmiş ortalama YOLCU talebi.
                     Yoksa None -> passenger_ratio None kalır ve
                     birleşik orana hiç katılmaz (MADDE 7).

    MADDE 7: risk yalnızca uçuş sayısına göre değil, uçuş oranı ile
    yolcu oranının ağırlıklı ortalamasına göre belirlenir. Böylece
      - az uçuş + geniş gövde  -> passenger_ratio yükselir, risk artar
      - çok uçuş + küçük uçak  -> flight_ratio yükselir, risk artar
    ikisi de ayrı ayrı görünür olur.

    estimated_wait_minutes HER ZAMAN None döner: gerçek kanal/kapasite
    verisi olmadığı için dakika tahmini uydurma olurdu.
    """
    flight_count = len(window_flights)
    demand = sum(demand_fn(f) for f in window_flights)

    if not historical_baseline:
        return {
            "flight_count": flight_count,
            "expected_passengers": demand,
            "baseline_ratio": None,
            "flight_ratio": None,
            "passenger_ratio": None,
            "risk": RISK_UNKNOWN,
            "estimated_wait_minutes": None,
            "reasons": [NO_BASELINE_MESSAGE],
        }

    flight_ratio = flight_count / historical_baseline

    # Sıfır/None yolcu baseline'ı ile bölme YOK - böyle bir durumda
    # oran hesaplanmaz, None kalır.
    passenger_ratio = (
        demand / historical_passenger_baseline
        if historical_passenger_baseline else None
    )

    ratio = combined_security_ratio(flight_ratio, passenger_ratio)

    if ratio < SECURITY_RATIO_LOW:
        risk = RISK_LOW
    elif ratio < SECURITY_RATIO_MEDIUM:
        risk = RISK_MEDIUM
    elif ratio < SECURITY_RATIO_HIGH:
        risk = RISK_HIGH
    else:
        risk = RISK_CRITICAL

    return {
        "flight_count": flight_count,
        "expected_passengers": demand,
        "baseline_ratio": round(ratio, 2),
        "flight_ratio": round(flight_ratio, 2),
        "passenger_ratio": (
            round(passenger_ratio, 2) if passenger_ratio is not None else None
        ),
        "risk": risk,
        "estimated_wait_minutes": None,
        "reasons": [],   # AŞAMA 6 dolduracak
    }


def passport_effective_service_rate(config) -> float:
    """
    Gişe başına efektif servis hızı (mu_per_counter).

    mu_per_counter = service_rate_per_staff * staff_per_counter * efficiency_multiplier

    Kanal sayısı (c = passport_counter_count) bu hesaba GİRMEZ - Erlang-C'de
    c ayrı bir parametredir (bkz. passport_queue_model). staff_per_counter
    SADECE burada, mu'yu büyütmek için kullanılır.

    passport_staff_count kasıtlı olarak KULLANILMAZ: personel yoğunluğu
    zaten staff_per_counter üzerinden mu'ya yansıyor. staff_count'u ayrıca
    çarpmak kapasiteyi iki kez büyütür (double-count).
    efficiency_multiplier bir VARSAYIMDIR (bkz. models.py).
    """
    return (
        config.passport_service_rate_per_staff
        * config.passport_staff_per_counter
        * config.passport_efficiency_multiplier
    )


def passport_staff_count_mismatch(config) -> bool:
    """
    passport_staff_count, counter_count * staff_per_counter ile
    uyuşmuyor mu?

    Sadece bilgilendirme amaçlıdır - kapasite hesabına (rho, mu)
    hiçbir şekilde girmez; staff_count orada zaten kullanılmıyor.
    """
    expected = config.passport_counter_count * config.passport_staff_per_counter
    return config.passport_staff_count != expected


def passport_queue_model(
    window_flights: Sequence,
    config,
    demand_fn: Callable[[object], int],
    window_minutes: int = DEMAND_WINDOW_MINUTES,
    backlog_start: float = 0.0,
) -> dict:
    """
    Passport - tam Erlang-C + AŞAMA 6D pencereler-arası backlog.

    window_flights : international departure + international arrival
    backlog_start  : bu pencereye ÖNCEKİ pencere(ler)den kalan, henüz
                      işlenmemiş yolcu sayısı (kişi). Çağıran taraf
                      (bkz. engine.py `_passport_backlog_chain`)
                      havalimanı+süreç bazında KRONOLOJİK olarak
                      hesaplayıp buraya besler; bu fonksiyon kendi
                      başına önceki pencereleri BİLMEZ (saf kalır).
                      Varsayılan 0.0 - eski çağıranlar (`predict_window`
                      doğrudan çağrıldığında) ESKİ davranışla birebir
                      aynı sonucu üretir.

    RISK: SADECE rho'ya bağlıdır, backlog'a DEĞİL (AŞAMA 6D §redline:
    risk threshold'ları değişmedi). estimated_wait_minutes ise backlog'u
    da hesaba katar - risk ve bekleme süresi BİRBİRİNDEN BAĞIMSIZ iki
    çıktıdır.

    KARARLI DURUM (backlog_start <= 0 VE rho < 1): eski Erlang-C
    sonucu BİREBİR AYNI - bu dal hiç değişmedi.

    AŞIRI YÜK / TAŞAN BACKLOG (rho >= 1 VEYA backlog_start > 0):
    Erlang-C'nin rho>=1'de matematiksel olarak tanımsız (Wq -> sonsuz)
    sonucu yerine, ÖNÜNDE gerçekten bekleyen kişi sayısına dayanan
    sonlu bir "an itibariyle bekleme" tahmini:

        queue_ahead = backlog_start + demand   (kişi)
        wait        = queue_ahead / capacity_rate   (kişi / (kişi/dk) = dk)

    Burada `demand` bu pencerenin KENDİ talebidir (arrival_rate * window_minutes
    ile aynı büyüklük) - yani "şu an bu pencerenin başında kuyruğa
    girecek olan biri, önündeki backlog + bu pencerede kendisiyle
    birlikte gelenlerin hepsi bitene kadar" bekler. Sahte/keyfi bir üst
    sınır YOKTUR; c=4 (gişe) matematiği c=8 (personel) ile ASLA
    karıştırılmaz (bkz. passport_effective_service_rate, mu bu
    fonksiyona ZATEN staff_count'suz gelir).

    backlog_end (round(.,3)) çağıran tarafın (`engine.py`) bir SONRAKİ
    pencereye taşıyacağı değerdir - bu fonksiyon kendi çıktısını
    ASLA geriye okuyup üstüne eklemez (idempotent: aynı backlog_start +
    aynı window_flights -> aynı backlog_end, her çalıştırmada).
    """
    demand = sum(demand_fn(f) for f in window_flights)
    lam = demand / window_minutes            # dakikada gelen yolcu
    c = config.passport_counter_count
    mu = passport_effective_service_rate(config)

    capacity_rate = c * mu
    rho = lam / capacity_rate if capacity_rate > 0 else float("inf")

    if capacity_rate <= 0:
        # Kanal/servis hızı yok - hesaplanamaz durum (gerçek config
        # hatası). Backlog hiç boşalmaz, sadece büyür - servis eden
        # kimse yok.
        return {
            "flight_count": len(window_flights),
            "expected_passengers": demand,
            "arrival_rate": round(lam, 3),
            "utilization": None,
            "estimated_wait_minutes": None,
            "backlog_end": round(backlog_start + demand, 3),
            "risk": RISK_CRITICAL,
            "reasons": [PASSPORT_OVERLOAD_MESSAGE],
        }

    service_capacity = capacity_rate * window_minutes   # kişi, bu pencerede sunulabilecek
    backlog_end = max(0.0, backlog_start + demand - service_capacity)

    if rho >= 1.0:
        risk = RISK_CRITICAL
        reasons = [PASSPORT_OVERLOAD_MESSAGE]
    elif rho < PASSPORT_RHO_LOW:
        risk = RISK_LOW
        reasons = []
    elif rho < PASSPORT_RHO_MEDIUM:
        risk = RISK_MEDIUM
        reasons = []
    else:
        risk = RISK_HIGH
        reasons = []

    if backlog_start <= 0.0 and rho < 1.0:
        wq = erlang_c_wait_time(c, lam, mu)
    else:
        queue_ahead = backlog_start + demand
        wq = queue_ahead / capacity_rate

    return {
        "flight_count": len(window_flights),
        "expected_passengers": demand,
        "arrival_rate": round(lam, 3),
        "utilization": round(rho, 3),
        "estimated_wait_minutes": round(wq, 1),
        "backlog_end": round(backlog_end, 3),
        "risk": risk,
        "reasons": reasons,
    }


def confidence_score(
    window_flights: Sequence,
    historical_baseline_available: bool,
    aircraft_match_rate: float,
    config_is_default: bool,
) -> float:
    """
    AŞAMA 7 - Tahminin ne kadar sağlam veriye dayandığı.

    Load factor ve boarding buffer HER ZAMAN statik varsayım olduğu
    için bu iki ceza koşulsuz uygulanır.
    """
    score = 1.0

    score -= CONFIDENCE_PENALTY_LOAD_FACTOR
    score -= CONFIDENCE_PENALTY_BOARDING_BUFFER

    if aircraft_match_rate < AIRCRAFT_MATCH_RATE_THRESHOLD:
        score -= CONFIDENCE_PENALTY_LOW_MATCH_RATE

    if any(getattr(f, "aircraft_icao", None) is None for f in window_flights):
        score -= CONFIDENCE_PENALTY_NULL_AIRCRAFT

    if not historical_baseline_available:
        score -= CONFIDENCE_PENALTY_NO_BASELINE

    if config_is_default:
        score -= CONFIDENCE_PENALTY_DEFAULT_CONFIG

    return max(round(score, 2), CONFIDENCE_FLOOR)
