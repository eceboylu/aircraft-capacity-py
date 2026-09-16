"""
Kuyruk tahmin motorunun sabitleri.

Buradaki her sayı ya dokümanda tanımlı bir eşik, ya da AÇIKÇA
işaretlenmiş bir varsayımdır. Havalimanına göre değişebilen
operasyonel değerler burada DEĞİL, AirportOperationalConfig
tablosundadır (bkz. models.py) - böylece mantık tek bir
havalimanına bağlanmaz.
"""

# --- Zaman pencereleri -------------------------------------------------
# ADIM 6D-2 HOURLY MIGRATION: prediction penceresi 15 dk'dan 60 dk'ya
# (saatlik: 08:00-09:00, 09:00-10:00, ...) geçirildi - TEK merkezi
# sabit. `floor_to_window`/`flights_in_window`/`window_starts`/
# `_bucket_flights_by_window`/`passport_queue_model`/
# `security_density_score`/`QueuePrediction`/`HistoricalFlightCount`
# hepsi bu sabiti (veya ondan türeyen `window_minutes` parametresini)
# kullanır - "60" ikinci bir yerde AYRI tanımlı DEĞİLDİR.
DEMAND_WINDOW_MINUTES = 60

# Arrival yolcusunun uçaktan inip passport kuyruğuna ulaşması için
# geçen süre. Sabit varsayım - gerçek veri yok. BİLİNÇLİ OLARAK
# DEMAND_WINDOW_MINUTES'TEN BAĞIMSIZDIR (rastgele aynı sayı - bir uçuş
# özelliği, pencere genişliği DEĞİL) - hourly migration'da DOKUNULMADI.
PASSPORT_RELEASE_BUFFER_MINUTES = 15

# --- Yön / lokasyon / süreç sözlüğü ------------------------------------
DIRECTION_DEPARTURE = "departure"
DIRECTION_ARRIVAL = "arrival"

LOCATION_DOMESTIC = "domestic"
LOCATION_INTERNATIONAL = "international"

PROCESS_SECURITY = "security"
PROCESS_PASSPORT = "passport"
# ADIM (Security Domestic/International Split) - AYRI, mevcut
# `security_density_score()`'u BAĞIMSIZ flight kümeleri (sadece
# domestic departure / sadece international departure) üzerinde
# çalıştıran EK süreçler. Birleşik `PROCESS_SECURITY` (TÜM kalkışlar)
# SİLİNMEDİ - geriye dönük uyumluluk için AYNEN üretilmeye devam
# ediyor; bunlar ONA EK. Değerler String(16) sütun sınırına (bkz.
# models.py QueuePrediction/HistoricalFlightCount/BaselineObservation)
# uyacak şekilde seçildi.
PROCESS_SECURITY_DOMESTIC = "security_dom"
PROCESS_SECURITY_INTL = "security_intl"

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

# MADDE 7 - Security risk iki sinyalin ağırlıklı ortalamasıdır:
#   flight_ratio    : pencere uçuş sayısı / geçmiş ortalama uçuş sayısı
#   passenger_ratio : pencere yolcu talebi / geçmiş ortalama yolcu talebi
#
# Ağırlıklar EŞİTTİR: dokümanda (TASK_QUEUE_FINAL.md) kesin bir ağırlık
# verilmemiştir, bu yüzden iki sinyalden birini diğerine üstün kılacak
# bir varsayım yapılmaz. Ağırlıklar toplamlarına bölünerek normalize
# edilir; böylece birleşik oran tek tek oranlarla AYNI ölçekte kalır ve
# yukarıdaki mevcut risk bantları (1.2 / 1.4 / 1.8) değişmeden geçerli
# olmaya devam eder.
SECURITY_FLIGHT_RATIO_WEIGHT = 0.5
SECURITY_PASSENGER_RATIO_WEIGHT = 0.5

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

# --- Risk sıralaması (raporlamada "en kötü saat" seçimi için) ----------
# UNKNOWN bir şiddet seviyesi DEĞİLDİR: veri yokluğunu anlatır, bu
# yüzden sıralamada en altta durur ve gerçek bir riskin önüne geçmez.
RISK_ORDER = {
    RISK_UNKNOWN: -1,
    RISK_LOW: 0,
    RISK_MEDIUM: 1,
    RISK_HIGH: 2,
    RISK_CRITICAL: 3,
}

# AŞAMA 5'te üretilen durum notlarının kodları. Bunlar 9 tespit
# fonksiyonuna ek DEĞİL, skorlama sonucunun kendi şeffaflık notlarıdır.
REASON_CAPACITY_EXCEEDED = "capacity_exceeded"
