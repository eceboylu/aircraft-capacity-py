
DEMAND_WINDOW_MINUTES = 60

# ADIM (Security Service Time - 50 saniye/yolcu) - kullanıcı talebi: "1
# passenger / 1 security lane = 50 seconds". TEK kaynak burası -
# `SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES` (dakika, `AirportOperational
# Config.security_service_time_minutes`'in Python-taraflı varsayılanı,
# bkz. models.py) bu sabitten TÜRETİLİYOR; hiçbir yerde "50" veya "0.8333"
# ayrıca hardcode EDİLMEDİ. 3600/50=72 pax/saat/lane -> 60/72=0.8333...
# dakika/yolcu. Passport service time (`passport_service_time_minutes`,
# ayrı bir alan) bu değişiklikten TAMAMEN BAĞIMSIZ, DOKUNULMADI.
SECURITY_PASSENGERS_PER_HOUR_PER_LANE = 72

SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES = 60.0 / SECURITY_PASSENGERS_PER_HOUR_PER_LANE

PASSPORT_RELEASE_BUFFER_MINUTES = 15

DEPARTURE_PASSENGER_ARRIVAL_OFFSET_MINUTES = 120

DEPARTURE_SHOW_UP_PROFILE: tuple[tuple[int, int, float], ...] = (
    (240, 180, 0.10),
    (180, 120, 0.45),
    (120, 60, 0.40),
    (60, 0, 0.05),
)

DEPARTURE_SHOWUP_BUCKET_MINUTES = 5

WAIT_DISPLAY_BUCKET_MINUTES = 5

ARRIVAL_RELEASE_PROFILE: tuple[tuple[int, int, float], ...] = (
    (10, 15, 0.15),
    (15, 20, 0.35),
    (20, 25, 0.30),
    (25, 30, 0.15),
    (30, 35, 0.05),
)

ARRIVAL_RELEASE_BUCKET_MINUTES = 1

# MEGA dynamic resource (security_intl/passport_dep/passport_arr) checkpoint
# aralığı - DB'de `security_dynamic_control_interval_minutes`/
# `passport_dynamic_control_interval_minutes` NULL ise bu varsayılana
# düşülür. Eski değer 10dk idi (passport'ta); yeni source of truth 5dk.
DEFAULT_DYNAMIC_CONTROL_INTERVAL_MINUTES = 5

# passport_arr için proaktif, "backlog oluşmadan önce gör" penceresi
# (bkz. event_queue.py DynamicStaffingParams.extended_look_ahead_minutes).
ARRIVAL_PASSPORT_PROACTIVE_LOOKAHEAD_MINUTES = 20

DYNAMIC_TARGET_UTILIZATION = 0.85
# ADIM (Kademeleri 10'a Çıkarma) - kullanıcı talebi: "5'er 5'er değil
# artık 30-40-50 diye artsın" - MEGA'nın 3 dynamic süreci (passport_dep/
# passport_arr/security_intl) artık HER checkpoint'te 10'luk kademelerle
# hareket ediyor (`_operational_levels_for()`'ın `range(base, max+1,
# step)` formülü - bkz. engine.py). Eski step=5 (30,35,40,45,50,55,60)
# ARTIK KULLANILMIYOR.
DYNAMIC_RAMP_STEP = 10

# ADIM (Discrete Operational Levels) - MEGA dynamic resource'lar artık
# rastgele bir tamsayıya (ör. 19, 24, 31, 38) DEĞİL, sadece bu sabit
# seviyelerden birine oturabiliyor. DB'den okunan max, bu listeyi
# runtime'da FİLTRELİYOR (ör. DB max=35 ise 40 hiç kullanılamaz) - bkz.
# event_queue.py `_apply_checkpoint`. Liste kodda sabit ama TAVAN DB'den
# geliyor; "40" burada sadece "olası en yüksek seviye", zorunlu hedef
# değil.
SECURITY_INTL_OPERATIONAL_LEVELS = (20, 30, 40)
# ADIM (phpMyAdmin'den canlı güncelleme) - passport_dep/passport_arr
# base/max SQL'den 30/60'a güncellendi; bu SADECE bir FALLBACK'tir
# (DB'deki base/max geçerliyse hiç kullanılmaz, bkz. engine.py
# `_operational_levels_for`) ama tutarlılık için AYNI 30/40/50/60
# değerine senkronize edildi.
PASSPORT_DEP_OPERATIONAL_LEVELS = (30, 40, 50, 60)
PASSPORT_ARR_OPERATIONAL_LEVELS = (30, 40, 50, 60)

DIRECTION_DEPARTURE = "departure"
DIRECTION_ARRIVAL = "arrival"

LOCATION_DOMESTIC = "domestic"
LOCATION_INTERNATIONAL = "international"

PROCESS_SECURITY = "security"
PROCESS_PASSPORT = "passport"
PROCESS_SECURITY_DOMESTIC = "security_dom"
PROCESS_SECURITY_INTL = "security_intl"

PROCESS_PASSPORT_DEPARTURE = "passport_dep"
PROCESS_PASSPORT_ARRIVAL = "passport_arr"

EXCLUDED_STATUSES = ("cancelled", "diverted")

STATUS_CANCELLED = "cancelled"
STATUS_DIVERTED = "diverted"

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"
RISK_CRITICAL = "CRITICAL"
RISK_UNKNOWN = "UNKNOWN"

SECURITY_RATIO_LOW = 1.2
SECURITY_RATIO_MEDIUM = 1.4
SECURITY_RATIO_HIGH = 1.8

SECURITY_FLIGHT_RATIO_WEIGHT = 0.5
SECURITY_PASSENGER_RATIO_WEIGHT = 0.5

PASSPORT_RHO_LOW = 0.7
PASSPORT_RHO_MEDIUM = 0.9

WAIT_RISK_LOW_MINUTES = 10
WAIT_RISK_MEDIUM_MINUTES = 15
WAIT_RISK_HIGH_MINUTES = 30

LOAD_FACTOR_DOMESTIC = 0.78
LOAD_FACTOR_INTL_UNKNOWN = 0.83
LOAD_FACTOR_INTL_LONG = 0.88
LOAD_FACTOR_INTL_MEDIUM = 0.84
LOAD_FACTOR_INTL_SHORT = 0.82

SECURITY_BUFFER_UNKNOWN_MINUTES = 45
SECURITY_BUFFER_LONG_MINUTES = 90
SECURITY_BUFFER_MEDIUM_MINUTES = 60
SECURITY_BUFFER_SHORT_MINUTES = 45

RANGE_LONG_HAUL_MINUTES = 360
RANGE_MEDIUM_HAUL_MINUTES = 120

CLUSTERING_RATIO_THRESHOLD = 1.4
WIDEBODY_SEAT_THRESHOLD = 250
WIDEBODY_COUNT_THRESHOLD = 2
WIDEBODY_SHARE_THRESHOLD = 0.3
DELAY_SIGNIFICANT_MINUTES = 10
DELAY_CLUSTER_THRESHOLD = 2
UTILIZATION_REASON_THRESHOLD = 0.9
INTL_SHARE_THRESHOLD = 0.6
ARRIVAL_BANK_DEFAULT_THRESHOLD = 5

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

CONFIDENCE_PENALTY_LOAD_FACTOR = 0.10
CONFIDENCE_PENALTY_BOARDING_BUFFER = 0.10
CONFIDENCE_PENALTY_LOW_MATCH_RATE = 0.15
CONFIDENCE_PENALTY_NULL_AIRCRAFT = 0.10
CONFIDENCE_PENALTY_NO_BASELINE = 0.10
CONFIDENCE_PENALTY_DEFAULT_CONFIG = 0.05
AIRCRAFT_MATCH_RATE_THRESHOLD = 0.8
CONFIDENCE_FLOOR = 0.10

EVENT_AIRCRAFT_CHANGED = "AIRCRAFT_CHANGED"
EVENT_DELAYED = "DELAYED"
EVENT_CANCELLED = "CANCELLED"
EVENT_DIVERTED = "DIVERTED"

RISK_ORDER = {
    RISK_UNKNOWN: -1,
    RISK_LOW: 0,
    RISK_MEDIUM: 1,
    RISK_HIGH: 2,
    RISK_CRITICAL: 3,
}

REASON_CAPACITY_EXCEEDED = "capacity_exceeded"
