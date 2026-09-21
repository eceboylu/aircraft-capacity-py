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

# ADIM (Airport Queue Model V2 - passenger arrival offset): departure
# yolcusunun havalimanına GERÇEKTE ne zaman geldiği artık uçuş süresine
# göre değişen dinamik bir buffer (45/60/90 dk, bkz.
# `flight_rules.security_arrival_buffer_minutes`) DEĞİL, sabit bir
# varsayımdır - effective departure zamanından SABİT 120 dakika önce.
# `security_arrival_buffer_minutes()` fonksiyonu SİLİNMEDİ (kendi saf
# birim testlerinde hâlâ geçerli bir hesaplama olarak duruyor), sadece
# `effective_time()`'ın departure dalından artık ÇAĞRILMIYOR.
DEPARTURE_PASSENGER_ARRIVAL_OFFSET_MINUTES = 120

# ADIM (Departure Show-Up Profile) - departure yolcularının kuyruğa TEK
# bir -120dk spike'ı yerine flight öncesi zamana YAYILMIŞ, deterministic
# (RANDOM YOK) "show-up" batch'leri halinde gelmesini tanımlar - bkz.
# `domain/demand.py:departure_show_up_events()`. `DEPARTURE_PASSENGER_
# ARRIVAL_OFFSET_MINUTES` (120) SİLİNMEDİ/DEĞİŞMEDİ - `effective_time()`
# hâlâ bu sabiti kullanır (flight-seviyesinde legacy/reference bir zaman
# damgası üretmeye devam eder, bkz. o fonksiyonun docstring'i); SADECE
# gerçek departure queue event ÜRETİMİ artık bu profili kullanır.
#
# Her satır: (departure'dan ÖNCE dakika-aralığı-başlangıcı, dakika-
# aralığı-bitişi, o 15 dakikalık dilime düşen TOPLAM yolcu oranı).
# Kullanıcının verdiği 30 dakikalık 6 segment (%5/%15/%30/%30/%15/%5),
# HER biri deterministic olarak (RANDOM YOK) İKİ EŞİT 15 dakikalık
# alt-batch'e bölünmüştür (her alt-batch kendi 30dk ebeveyninin YARISINI
# taşır) - bkz. Bölüm 4: "iki adet 15 dakikalık batch'e deterministic
# şekilde dağıt". Toplam = tam 1.0 (%100) - `test_departure_show_up_
# profile.py` bunu doğrular.
DEPARTURE_SHOW_UP_PROFILE: tuple[tuple[int, int, float], ...] = (
    (180, 165, 0.025),
    (165, 150, 0.025),
    (150, 135, 0.075),
    (135, 120, 0.075),
    (120, 105, 0.15),
    (105, 90, 0.15),
    (90, 75, 0.15),
    (75, 60, 0.15),
    (60, 45, 0.075),
    (45, 30, 0.075),
    (30, 15, 0.025),
    (15, 0, 0.025),
)

# ADIM (Arrival Release Profile) - international arrival yolcularının
# Arrival Passport kuyruğuna TEK bir +15dk spike'ı yerine iniş sonrası
# (deboarding/walking) zamana YAYILMIŞ, deterministic (RANDOM YOK) kısa
# bir "release" profiliyle gelmesini tanımlar - bkz. `domain/demand.py:
# arrival_passenger_release_events()`. `PASSPORT_RELEASE_BUFFER_MINUTES`
# (15) SİLİNMEDİ/DEĞİŞMEDİ - `effective_time()` hâlâ bu sabiti kullanır
# (flight-seviyesinde legacy/reference bir zaman damgası üretmeye devam
# eder - flight_count/reasons bucket'lama, `FlightEvent` dedup, vb.);
# SADECE gerçek Arrival Passport queue event ÜRETİMİ artık bu profili
# kullanır - ve bunu `effective_time()`'IN ÜZERİNE değil, flight'ın HAM
# arrival referansı (actual>estimated>scheduled) ÜZERİNE uygular (bkz.
# `_arrival_release_base()` - `effective_time()`'ın zaten içerdiği +15dk
# ile ÇİFT OFFSET yapılmaz, bkz. `test_arrival_release_no_double_offset`).
#
# BU PROFİL GERÇEK, HAVALİMANINA-ÖZGÜ BİR ÖLÇÜM DEĞİLDİR - türetilmiş/
# varsayılan bir operasyonel yaklaşıklıktır (kullanıcı tarafından
# başlangıç değeri olarak verildi, gerçek deboarding/yürüme telemetri
# verisi YOK). `DEPARTURE_SHOW_UP_PROFILE`'dan (30dk'lık aralıklar,
# 3 saatlik pencere) YAPISAL OLARAK FARKLI: burada her satır TEK bir
# dakika NOKTASI (aralık DEĞİL) - uçaktan inen yolcuların passport
# kuyruğuna varış anını doğrudan modeller, 5 dakika aralıklarla.
# Her satır: (varıştan SONRA dakika, o ana düşen TOPLAM yolcu oranı).
# Toplam = tam 1.0 (%100) - `test_arrival_release_profile.py` doğrular.
ARRIVAL_RELEASE_PROFILE: tuple[tuple[int, float], ...] = (
    (10, 0.15),
    (15, 0.35),
    (20, 0.30),
    (25, 0.15),
    (30, 0.05),
)

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

# ADIM (4-Graph API Contract) - AYRI, mevcut birleşik `PROCESS_PASSPORT`'un
# (departure+arrival, DEĞİŞMEDİ) cohort/kaynak bazında raporlama BÖLÜNMESİ.
# Bölüm 17: paylaşılan FİZİKSEL passport havuzu (aynı 8 efektif server)
# İKİYE AYRILMAZ/duplicate edilmez - bu iki süreç KENDİ Erlang-C/backlog
# hesabını YAPMAZ, `engine.py:predict_airport()` bunları birleşik
# `PROCESS_PASSPORT` penceresinin utilization/wait/risk/confidence
# değerlerini AYNEN kopyalayıp SADECE `expected_passengers`/`flight_count`'u
# kendi cohort'una göre ayırarak üretir - double-count YOK.
PROCESS_PASSPORT_DEPARTURE = "passport_dep"
PROCESS_PASSPORT_ARRIVAL = "passport_arr"

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
