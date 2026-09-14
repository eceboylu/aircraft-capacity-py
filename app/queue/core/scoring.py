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
    SECURITY_RATIO_HIGH,
    SECURITY_RATIO_LOW,
    SECURITY_RATIO_MEDIUM,
)


def security_density_score(
    window_flights: Sequence,
    historical_baseline: float | None,
    demand_fn: Callable[[object], int],
) -> dict:
    """
    Security - basit yoğunluk sinyali.

    window_flights      : domestic departure + international departure
    historical_baseline : bu havalimanı + saat için geçmiş ortalama uçuş
                          sayısı. Yoksa None - SAHTE DEĞER ÜRETİLMEZ.

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
            "risk": RISK_UNKNOWN,
            "estimated_wait_minutes": None,
            "reasons": [NO_BASELINE_MESSAGE],
        }

    ratio = flight_count / historical_baseline

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
        "risk": risk,
        "estimated_wait_minutes": None,
        "reasons": [],   # AŞAMA 6 dolduracak
    }


def passport_effective_service_rate(config) -> float:
    """
    Gişe başına efektif servis hızı (mu).

    Kanal sayısı (c) sabit kalır; personel yoğunluğu servis oranını
    etkiler. efficiency_multiplier bir VARSAYIMDIR (bkz. models.py).
    """
    return config.passport_service_rate_per_staff * config.passport_efficiency_multiplier


def passport_queue_model(
    window_flights: Sequence,
    config,
    demand_fn: Callable[[object], int],
    window_minutes: int = DEMAND_WINDOW_MINUTES,
) -> dict:
    """
    Passport - tam Erlang-C.

    window_flights : international departure + international arrival
    """
    demand = sum(demand_fn(f) for f in window_flights)
    lam = demand / window_minutes            # dakikada gelen yolcu
    c = config.passport_counter_count
    mu = passport_effective_service_rate(config)

    capacity_rate = c * mu
    rho = lam / capacity_rate if capacity_rate > 0 else float("inf")

    if rho >= 1.0:
        return {
            "flight_count": len(window_flights),
            "expected_passengers": demand,
            "arrival_rate": round(lam, 3),
            "utilization": round(rho, 3) if rho != float("inf") else None,
            "estimated_wait_minutes": None,
            "risk": RISK_CRITICAL,
            "reasons": [PASSPORT_OVERLOAD_MESSAGE],
        }

    wq = erlang_c_wait_time(c, lam, mu)

    if rho < PASSPORT_RHO_LOW:
        risk = RISK_LOW
    elif rho < PASSPORT_RHO_MEDIUM:
        risk = RISK_MEDIUM
    else:
        risk = RISK_HIGH

    return {
        "flight_count": len(window_flights),
        "expected_passengers": demand,
        "arrival_rate": round(lam, 3),
        "utilization": round(rho, 3),
        "estimated_wait_minutes": round(wq, 1),
        "risk": risk,
        "reasons": [],   # AŞAMA 6 dolduracak
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
