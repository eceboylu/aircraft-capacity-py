"""
Kuyruk tahmin motorunun sabitleri.

Buradaki her sayı ya dokümanda tanımlı bir eşik, ya da AÇIKÇA
işaretlenmiş bir varsayımdır. Havalimanına göre değişebilen
operasyonel değerler burada DEĞİL, AirportOperationalConfig
tablosundadır (bkz. models.py) - böylece mantık tek bir
havalimanına bağlanmaz.
"""

# --- Zaman pencereleri -------------------------------------------------
DEMAND_WINDOW_MINUTES = 15

# Arrival yolcusunun uçaktan inip passport kuyruğuna ulaşması için
# geçen süre. Sabit varsayım - gerçek veri yok.
PASSPORT_RELEASE_BUFFER_MINUTES = 15

# --- Yön / lokasyon / süreç sözlüğü ------------------------------------
DIRECTION_DEPARTURE = "departure"
DIRECTION_ARRIVAL = "arrival"

LOCATION_DOMESTIC = "domestic"
LOCATION_INTERNATIONAL = "international"

PROCESS_SECURITY = "security"
PROCESS_PASSPORT = "passport"

# Talep hesabına girmeyen statüler (AŞAMA 4)
EXCLUDED_STATUSES = ("cancelled", "diverted")

STATUS_CANCELLED = "cancelled"
STATUS_DIVERTED = "diverted"

# --- Risk kategorileri -------------------------------------------------
RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"
RISK_CRITICAL = "CRITICAL"
RISK_UNKNOWN = "UNKNOWN"

# Security yoğunluk skoru bantları (baseline_ratio üzerinden)
SECURITY_RATIO_LOW = 1.2
SECURITY_RATIO_MEDIUM = 1.4
SECURITY_RATIO_HIGH = 1.8

# Passport Erlang-C bantları (utilization / rho üzerinden)
PASSPORT_RHO_LOW = 0.7
PASSPORT_RHO_MEDIUM = 0.9

# --- Load factor varsayımları (AŞAMA 3) --------------------------------
# AÇIK VARSAYIM - canlı doluluk verisi YOK, endustri gozlemlerine
# dayali statik tahmin.
LOAD_FACTOR_DOMESTIC = 0.78
LOAD_FACTOR_INTL_UNKNOWN = 0.83
LOAD_FACTOR_INTL_LONG = 0.88
LOAD_FACTOR_INTL_MEDIUM = 0.84
LOAD_FACTOR_INTL_SHORT = 0.82

# --- Security check-in buffer varsayımları (AŞAMA 3) -------------------
# AÇIK VARSAYIM - boarding time kaynakta YOK, ucus suresinden turetiliyor.
SECURITY_BUFFER_UNKNOWN_MINUTES = 45
SECURITY_BUFFER_LONG_MINUTES = 90
SECURITY_BUFFER_MEDIUM_MINUTES = 60
SECURITY_BUFFER_SHORT_MINUTES = 45

# Menzil sınırları (dakika) - load factor ve buffer için ortak
RANGE_LONG_HAUL_MINUTES = 360
RANGE_MEDIUM_HAUL_MINUTES = 120

# --- Reason detection eşikleri (AŞAMA 6) -------------------------------
CLUSTERING_RATIO_THRESHOLD = 1.4
WIDEBODY_SEAT_THRESHOLD = 250
WIDEBODY_COUNT_THRESHOLD = 2
WIDEBODY_SHARE_THRESHOLD = 0.3
DELAY_SIGNIFICANT_MINUTES = 10
DELAY_CLUSTER_THRESHOLD = 2
UTILIZATION_REASON_THRESHOLD = 0.9
INTL_SHARE_THRESHOLD = 0.6
# Havalimanı bazlı override edilebilir (AirportOperationalConfig)
ARRIVAL_BANK_DEFAULT_THRESHOLD = 5

# --- Reason kodları ----------------------------------------------------
REASON_CLUSTERING = "clustering"
REASON_WIDEBODY = "widebody"
REASON_DELAY_COMPRESSION = "delay_compression"
REASON_SINGLE_DELAY = "single_delay"
REASON_UTILIZATION = "utilization"
REASON_INTL_SHARE = "intl_share"
REASON_ARRIVAL_BANK = "arrival_bank"
REASON_CANCELLATION = "cancellation"
REASON_AIRCRAFT_CHANGE = "aircraft_change"
REASON_DIVERSION = "diversion"
REASON_NORMAL = "normal"
REASON_NO_BASELINE = "no_baseline"

NORMAL_REASON_MESSAGE = "Normal operasyonel yoğunluk"
NO_BASELINE_MESSAGE = "Henüz karşılaştırma için geçmiş veri birikmedi"
PASSPORT_OVERLOAD_MESSAGE = (
    "Talep kapasiteyi aşıyor — kuyruk teorik olarak sürdürülemez"
)

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

# --- Confidence cezaları (AŞAMA 7) -------------------------------------
CONFIDENCE_PENALTY_LOAD_FACTOR = 0.10
CONFIDENCE_PENALTY_BOARDING_BUFFER = 0.10
CONFIDENCE_PENALTY_LOW_MATCH_RATE = 0.15
CONFIDENCE_PENALTY_NULL_AIRCRAFT = 0.10
CONFIDENCE_PENALTY_NO_BASELINE = 0.10
CONFIDENCE_PENALTY_DEFAULT_CONFIG = 0.05
AIRCRAFT_MATCH_RATE_THRESHOLD = 0.8
CONFIDENCE_FLOOR = 0.10

# --- FlightEvent tipleri (AŞAMA 8) -------------------------------------
EVENT_AIRCRAFT_CHANGED = "AIRCRAFT_CHANGED"
EVENT_DELAYED = "DELAYED"
EVENT_CANCELLED = "CANCELLED"
EVENT_DIVERTED = "DIVERTED"
