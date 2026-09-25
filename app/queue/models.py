"""
AŞAMA 8 - Kuyruk tahmin motorunun veritabanı modelleri.

Madde 1'in Base'i import edilir, YENİDEN TANIMLANMAZ - böylece
tek metadata, tek veritabanı, tek create_all.

Şişme koruması (YASAK 4):
  - Flight / QueuePrediction / HistoricalFlightCount: her refresh'te
    session.merge() ile UPSERT, yeni satır açılmaz.
  - FlightEvent: SADECE gerçek bir durum değişikliği tespit
    edildiğinde satır eklenir.
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base, utcnow
from .constants import SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES


class Airport(Base):
    """flight_airports.sql'den bir kere içe aktarılır."""

    __tablename__ = "airports"

    iata_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    icao_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    airport_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    city_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    country_code: Mapped[str | None] = mapped_column(
        String(4), nullable=True, index=True
    )
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ADIM (4-Tier Airport Scale): "mega"/"large"/"medium"/"small" -
    # `app/queue/domain/airport_scale.py`'nin çözdüğü değer,
    # `ingestion/airports_import.py:import_airport_scales()` ile
    # BİR KEZ import edilir (mevcut bir DB'yi YENİ contract'a göre
    # güncellemek için `refresh_airport_scales()`/`scripts/update_
    # airport_scale_resources.py`). Bilinmiyorsa None - UYDURMA bir
    # ölçek ATANMAZ (bkz. config.py'nin unknown-scale davranışı).
    scale: Mapped[str | None] = mapped_column(String(16), nullable=True)


class Flight(Base):
    """
    Kaynak A (tarife) + Kaynak B (uçak tipi enrichment) birleşimi.

    flight_key benzersizdir; aynı uçuş tekrar geldiğinde INSERT değil
    UPDATE yapılır.
    """

    __tablename__ = "flights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # "{airline_iata}_{flight_number}_{dep_scheduled_date_utc}"
    flight_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # Hangi havalimanının kapsamında olduğu. Çok-havalimanlı
    # filtreleme için ZORUNLU (YASAK 5).
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)

    direction: Mapped[str] = mapped_column(String(16))   # arrival | departure
    location: Mapped[str] = mapped_column(String(16))    # domestic | international

    # ADIM (Schengen-Aware Passport Routing) - `location` (traffic type)
    # İLE KARIŞTIRILMAMALI: Schengen->Schengen bir uçuş `location`'da
    # international KALIR ama sınırda pasaport kontrolü YOKTUR - bkz.
    # `domain/schengen.py:requires_passport_control()`. Varsayılan True
    # (mevcut/eski davranışı korur - Schengen bilgisi HENÜZ hesaplanmamış
    # satırlar için "passport gerekir" güvenli tarafı).
    requires_passport: Mapped[bool] = mapped_column(Boolean, default=True)

    airline_iata: Mapped[str | None] = mapped_column(String(8), nullable=True)
    flight_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    flight_iata: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True
    )

    # Kaynak B'den enrichment ile gelir; eşleşme yoksa None kalır.
    aircraft_icao: Mapped[str | None] = mapped_column(String(8), nullable=True)
    aircraft_match_found: Mapped[bool] = mapped_column(Boolean, default=False)

    dep_iata: Mapped[str | None] = mapped_column(String(10), nullable=True)
    arr_iata: Mapped[str | None] = mapped_column(String(10), nullable=True)

    dep_scheduled_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dep_estimated_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dep_actual_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arr_scheduled_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arr_estimated_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arr_actual_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    dep_terminal: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dep_gate: Mapped[str | None] = mapped_column(String(16), nullable=True)
    arr_terminal: Mapped[str | None] = mapped_column(String(16), nullable=True)
    arr_gate: Mapped[str | None] = mapped_column(String(16), nullable=True)

    status: Mapped[str] = mapped_column(String(32))
    last_refreshed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FlightEvent(Base):
    """
    SADECE değişiklik olduğunda satır açılır (YASAK 4).
    30 dakikalık refresh'te otomatik kayıt YAZILMAZ.

    Her gerçek değişiklik AYRI bir satırdır (MADDE 8) - aynı uçuşun
    aynı pencerede birden fazla aircraft change'i varsa (A320->A321,
    sonra A321->A330) her ikisi de burada AYRI satır olarak durur,
    hiçbiri üzerine yazılmaz.

    flight_effective_time (additive/nullable kolon): SADECE
    AIRCRAFT_CHANGED event'lerinde doldurulur - değişikliğin ait
    olduğu uçuşun o anki effective_time()'ıdır (dep/arr scheduled
    değil, mevcut sistemin pencere-atama kuralıyla AYNI fonksiyon).
    Bu, event'in hangi 15 dk prediction window'una düştüğünü ve
    duplicate event tespitini (aynı flight+aynı eski/yeni tip+aynı
    effective_time) belirler. Diğer event tiplerinde (CANCELLED,
    DIVERTED, DELAYED) None kalır - MADDE 8 kapsamı sadece aircraft
    change'dir, bu alan onları etkilemez.

    Şema notu: bu kolon sonradan eklendi, nullable'dır - var olan bir
    production veritabanına ALTER TABLE gerekir (`create_all()` var olan
    tabloyu değiştirmez, sadece eksik tabloları yaratır) - bkz. alembic/
    versions/ altındaki migration dosyaları (şema değişiklikleri artık
    buradan yönetiliyor, bkz. alembic/README).
    """

    __tablename__ = "flight_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flight_key: Mapped[str] = mapped_column(String(64), index=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    old_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    flight_effective_time: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AirportOperationalConfig(Base):
    """
    Havalimanı bazlı operasyon modeli.

    Varsayılan değerler ÖRNEK/VARSAYIMDIR, ölçülmüş gerçek veri
    DEĞİLDİR. Her havalimanı için bu tablodan override edilebilir -
    mantık tek bir havalimanına bağlanmaz (YASAK 5).
    """

    __tablename__ = "airport_operational_configs"

    airport_iata: Mapped[str] = mapped_column(String(10), primary_key=True)

    # Fiziksel gişe (masa/kabin) sayısı. Erlang-C'nin `c` parametresi
    # BUNUN KENDİSİ DEĞİL - bkz. `passport_staff_per_counter` (her
    # gişede AYNI ANDA paralel çalışan görevli sayısı); gerçek `c` =
    # `passport_effective_server_count()` = counter_count x
    # staff_per_counter (core/scoring.py). `passport_staff_count`
    # (toplam personel/vardiya) kapasiteye HİÇ girmez - salt
    # bilgilendirici (bkz. `passport_staff_count_mismatch`).
    passport_counter_count: Mapped[int] = mapped_column(Integer, default=4)
    passport_staff_count: Mapped[int] = mapped_column(Integer, default=8)

    # Production queue modelinin açık servis varsayımı: TEK BİR
    # GÖREVLİNİN bir yolcuyu işleme süresi.
    #
    # ADIM (Resource/Service Throughput Calibration) - kullanıcının
    # AÇIKÇA belirttiği yeni contract değeri: 1.0 dk = 60 pax/saat/
    # görevli (eski değer 1.5 dk'dan DEĞİŞTİ - bu bilinçli bir
    # kullanıcı kararıdır, genel araştırma oranı DEĞİL). mu = 1/1.0 =
    # 1.0 pax/dk/görevli; c=4 gişe x 2 paralel görevli/gişe = 8 efektif
    # server ile toplam kapasite 480 pax/saat (core/scoring.py:
    # `passport_capacity_rate`).
    passport_service_time_minutes: Mapped[float] = mapped_column(
        Float, default=1.0
    )

    # Security için fiziksel lane sayısı ve lane başına işlem süresi.
    #
    # ADIM (Security Capacity Contract v4 - lane_count x pax/hour/
    # lane) - bu alan artık "TEK bir yolcunun lane'de geçirdiği
    # fiziksel muayene süresi" olarak OKUNMAZ/YORUMLANMAZ. Kaynak
    # gerçek sayı `constants.py:SECURITY_PASSENGERS_PER_HOUR_PER_LANE`
    # (=150, kullanıcının AÇIKÇA verdiği contract) - bu alanın
    # varsayılanı SADECE Erlang-C/event-driven FIFO'nun (core/
    # scoring.py, core/event_queue.py) matematiksel olarak "dakika/
    # yolcu" birimi BEKLEMESİ yüzünden var: `SECURITY_EFFECTIVE_
    # SERVICE_TIME_MINUTES` = 60/150 = 0.4 dk, "EFFECTIVE AGGREGATE
    # LANE THROUGHPUT"'un o birime çevrilmiş HALİDİR, literal passenger
    # inspection time DEĞİLDİR. İki yerde ayrı yazılan bir "150" ve bir
    # "0.4" YOK - ikincisi birincinin türevi (bkz. constants.py).
    # capacity_per_hour = lane_count x 150 (queue simulation/scoring/
    # reporting/utilization/queue_pressure HEPSİ bu tek formülü, bu tek
    # sabit üzerinden kullanır).
    #
    # `security_lane_count`: BİRLEŞİK/legacy `PROCESS_SECURITY` (tüm
    # kalkışlar, geriye dönük uyumluluk) ve `PROCESS_SECURITY_INTL`
    # (passport→security kuplajının İÇİNDE, bkz. engine.py
    # `_passport_security_hourly_coupling`) için kullanılmaya devam
    # ediyor - bu ADIM kuplaj fonksiyonuna DOKUNMUYOR.
    #
    # `domestic_security_lane_count`: SADECE `PROCESS_SECURITY_DOMESTIC`
    # için - bu süreç kuplajdan tamamen bağımsız (domestic kalkış
    # passport'u hiç görmeden doğrudan security'ye girer), bu yüzden
    # kendi fiziksel lane sayısını GÜVENLE ayrı taşıyabilir. Varsayılan
    # mevcut `security_lane_count` ile AYNI (8) - additive migration
    # sonrası hiçbir mevcut airport'un davranışı DEĞİŞMEZ.
    #
    # `international_security_lane_count`: şema/config katmanında
    # airport-bazlı olarak taşınır ve toplu düzenlenebilir (bkz.
    # config.py), ancak `PROCESS_SECURITY_INTL`'in queue matematiğine
    # BAĞLANMADI - bu, coupling fonksiyonunun içini değiştirmeyi
    # gerektirir (bu ADIM'ın kapsamı dışında, bkz. rapor).
    security_lane_count: Mapped[int] = mapped_column(Integer, default=8)
    domestic_security_lane_count: Mapped[int] = mapped_column(Integer, default=8)
    international_security_lane_count: Mapped[int] = mapped_column(Integer, default=8)
    security_service_time_minutes: Mapped[float] = mapped_column(
        Float, default=SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES
    )

    # `passport_staff_per_counter` (4x2=8 efektif server modeli) AKTİF
    # olarak `passport_effective_server_count()`/`passport_capacity_rate()`
    # tarafından KULLANILIYOR - "legacy" DEĞİL. Diğer ikisi
    # (`passport_service_rate_per_staff`, `passport_efficiency_multiplier`)
    # hâlâ legacy/kullanılmıyor - mevcut veritabanı/config satırlarını
    # kırmamak için şemada korunuyorlar.
    passport_staff_per_counter: Mapped[float] = mapped_column(Float, default=2.0)
    passport_service_rate_per_staff: Mapped[float] = mapped_column(Float, default=1.0)
    passport_efficiency_multiplier: Mapped[float] = mapped_column(Float, default=0.8125)

    # ADIM (Airport-Scale Queue Capacity) - departure/arrival passport
    # havuzları artık AYRI (bkz. core/event_queue.py). NULLABLE, default
    # YOK (None) - bu, "airport-specific EXPLICIT override" ile "hiç
    # dokunulmadı" arasındaki farkı taşır: None ise config.py önce
    # `Airport.scale`'den türetilmiş değeri, o da yoksa eski
    # `passport_counter_count x passport_staff_per_counter` (unknown-
    # scale fallback) kullanır. Eski `passport_counter_count`/
    # `passport_staff_per_counter` SİLİNMEDİ (geriye dönük uyumluluk -
    # unknown-scale fallback'i hâlâ onlardan türer).
    passport_departure_server_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    passport_arrival_server_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )

    arrival_bank_threshold: Mapped[int] = mapped_column(Integer, default=5)

    # ADIM (Airport Operational Config Materialization) - önceden bir
    # satırın VARLIĞI = "bu havalimanı açıkça override edildi" anlamına
    # geliyordu (bkz. config.py:_build_config_view - `is_default = row
    # is None`). Artık HER ölçeği çözülen havalimanı için (`ensure_
    # airport_operational_configs()` - bkz. pipeline.py) bir satır
    # OTOMATİK oluşturuluyor - satırın VARLIĞI artık "override" ile
    # AYNI ŞEY DEĞİL. Bu kolon o farkı taşır:
    #
    #   True  = satır SADECE scale'in (`airport_scale.py:SCALE_
    #           RESOURCES`) kopyası olarak seed edildi - insan ELİYLE
    #           HİÇBİR alanı değiştirilmedi. `confidence_score()`'un
    #           `CONFIDENCE_PENALTY_DEFAULT_CONFIG` cezası bu satırlar
    #           için HÂLÂ uygulanır (bkz. config.py `is_default`) -
    #           satırın var OLMASI, gerçek/ölçülmüş bir veri olduğu
    #           anlamına GELMEZ.
    #   False = bu satırdaki bir/birden fazla alan AÇIKÇA bu havalimanı
    #           için özelleştirildi (override) - confidence cezası
    #           KALKAR, `AirportConfigView.is_default=False` olur.
    #
    # Varsayılan `False`: elle INSERT edilen (seed fonksiyonundan
    # GEÇMEYEN) eski/manuel satırlar hep "override" sayılır - geriye
    # dönük uyumlu, YANLIŞLIKLA "default" sayılıp confidence'ı YÜKSELEN
    # bir satır OLMAZ.
    is_seeded_default: Mapped[bool] = mapped_column(Boolean, default=False)


class AirportScaleConfig(Base):
    """
    ADIM (DB-Editable Scale Resource Contract) - `domain/airport_scale.py:
    SCALE_RESOURCES`'ın (Python sabiti) CANLI/düzenlenebilir üst katmanı.

    Bu tablo ÖNCEDEN (bu ADIM'dan önce) kodda hiç okunmayan, yetim/orphan
    bir tabloydu (3-tier döneminden kalma eski değerler taşıyordu, mega
    hiç yoktu) - `config.py:_scale_resources_from_db()` artık bu tabloyu
    GERÇEKTEN okuyor: `scale` başına bir satır VARSA, o satırın değerleri
    `SCALE_RESOURCES`'ın hardcoded değerlerinin YERİNE geçer (öncelik
    zinciri: DB satırı > Python sabiti). Bu, phpMyAdmin'den bir sayı
    değiştirip deploy/kod değişikliği yapmadan tüm o ölçekteki
    havalimanlarının bir SONRAKİ hesaplamada yeni değeri kullanmasını
    sağlar.

    `scale` DIŞINDA bir satırda YOKSA (ör. tablo boşsa, ya da sadece
    bazı tier'lar için satır varsa) o tier için `SCALE_RESOURCES`
    (Python sabiti) AYNEN kullanılmaya devam eder - bu tablo KISMİ
    olabilir, "hepsi ya da hiçbiri" değildir.

    `departure_passport_servers_max`/`arrival_passport_servers_max`
    NULL ise (LARGE/MEDIUM/SMALL gibi) o havuz İÇİN dynamic staffing
    PASİF kalır (bkz. `config.py:_build_config_view()` - `_max is not
    None` kontrolü DEĞİŞMEDİ, sadece `_max`'ın KAYNAĞI artık bu tablo
    olabilir).
    """

    __tablename__ = "airport_scale_configs"

    scale: Mapped[str] = mapped_column(String(16), primary_key=True)
    departure_passport_servers: Mapped[int] = mapped_column(Integer, nullable=False)
    departure_passport_servers_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arrival_passport_servers: Mapped[int] = mapped_column(Integer, nullable=False)
    arrival_passport_servers_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    domestic_security_lanes: Mapped[int] = mapped_column(Integer, nullable=False)
    international_security_lanes: Mapped[int] = mapped_column(Integer, nullable=False)


class QueuePrediction(Base):
    """
    Current-state tablosu: her (havalimanı, süreç, pencere) için
    TEK satır. Refresh'te upsert ile güncellenir (YASAK 4).
    """

    __tablename__ = "queue_predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))   # security | passport
    window_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    # ADIM (Current Operational Day Isolation) - bu satırı ÜRETEN
    # `run_predictions()` çağrısının, bu havalimanı için çözdüğü YEREL
    # takvim günü (`operational_day.operational_date(tz, now)` - BÖLÜM
    # 58/61'in "flight selection"ı için kullandığı AYNI kaynak).
    # `window_start` (event'in KENDİSİ, `effective_time()`'ın -120dk/+15dk
    # kaydırdığı UTC an) İLE KARIŞTIRILMAZ: bir günün flight'ı, backlog/
    # offset nedeniyle `window_start` olarak ÖNCEKİ/SONRAKİ takvim gününe
    # düşebilir (Bölüm 61 - cross-midnight/queue-carry event'ler HÂLÂ
    # KORUNUR) - ama bu satır GERÇEKTE hangi operasyonel GÜNÜN talebinden
    # üretildiğini burada TAŞIR. `api.py:process_series()` "sadece
    # BUGÜNÜN grafiği" filtresini `window_start` ARALIĞI ile DEĞİL, bu
    # alanla yapar - böylece kalıcı/çok-günlü bir DB'de ESKİ bir günün
    # TAMAMEN AYRI, GERÇEK verisi "bugünün" current grafiğine SIZMAZ,
    # ama AYNI günün kendi sınır-geçişli event'leri asla YANLIŞLIKLA
    # dışlanmaz. NULLABLE: eski (bu ADIM'dan ÖNCE yazılmış) satırlar VEYA
    # timezone'u çözülemeyen havalimanları için `None` - çağıran taraf
    # (`process_series`/`prune_stale_predictions`) bu durumda ESKİ,
    # `window_start` tabanlı (geniş/superset) davranışa GERİ DÖNER.
    operational_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    flight_count: Mapped[int] = mapped_column(Integer, default=0)
    expected_passengers: Mapped[int] = mapped_column(Integer, default=0)
    # MADDE 7: security'de baseline_ratio, flight_ratio ile
    # passenger_ratio'nun ağırlıklı ortalamasıdır. Bileşenler ayrıca
    # saklanır - raporlamada hangi sinyalin tetiklediği görülebilsin.
    # passenger_ratio, geçmiş yolcu verisi yoksa None kalır.
    baseline_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    flight_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    passenger_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Passport ve security aynı ortak queue-capacity çekirdeğinden gerçek
    # utilization/wait üretir; process'e özel c/mu config'ten gelir.
    utilization: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_wait_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)

    risk: Mapped[str] = mapped_column(String(16))
    # DetectedReason listesi, JSON string
    reasons: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float, default=0.1)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "airport_iata", "process", "window_start",
            name="uq_queue_prediction_window",
        ),
    )


class HistoricalFlightCount(Base):
    """
    Neden 1 (clustering) ve MADDE 7 (security flight/passenger ratio)
    baseline'ı. Geçmiş veri birikmediyse satır yoktur ve sahte baseline
    ÜRETİLMEZ.

    Yolcu ortalaması AYRI bir örneklem sayacıyla (passenger_sample_size)
    tutulur: bu sütunlar sonradan eklendiği için eski satırlarda yolcu
    verisi YOKTUR. Uçuş örneklemi ile yolcu örneklemini aynı sayaca
    bağlamak, olmayan yolcu gözlemlerini varmış gibi göstererek
    ortalamayı bozardı. passenger_sample_size == 0 iken yolcu baseline'ı
    None'dır ve passenger_ratio hesaplanmaz (uydurulmaz).
    """

    __tablename__ = "historical_flight_counts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))
    hour_of_day: Mapped[int] = mapped_column(Integer)   # 0-23
    day_of_week: Mapped[int] = mapped_column(Integer)   # 0-6
    average_flight_count: Mapped[float] = mapped_column(Float)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    average_expected_passengers: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    passenger_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    last_updated: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "airport_iata", "process", "hour_of_day", "day_of_week",
            name="uq_historical_flight_count",
        ),
    )


class BaselineObservation(Base):
    """
    AŞAMA 3 (MADDE 3) - idempotency defteri.

    HistoricalFlightCount (hour_of_day, day_of_week) bazlı bir HAVUZ
    tutar - aynı saat dilimine düşen birçok farklı tarihin ortalamasını
    biriktirir. Bu tablo ise tek bir somut pencere örneğinin (belirli
    bir airport + process + window_start) o havuza DAHA ÖNCE eklenip
    eklenmediğini tutar.

    UNIQUE constraint bu üçlü üzerindedir - aynı pencere ikinci kez
    kaydedilmeye çalışıldığında DB seviyesinde reddedilir (race/duplicate
    refresh'lere karşı da güvenlidir, sadece application-level kontrol
    değildir).
    """

    __tablename__ = "baseline_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))
    window_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "airport_iata", "process", "window_start",
            name="uq_baseline_observation_window",
        ),
    )
