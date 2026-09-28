
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

    __tablename__ = "airports"

    iata_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    icao_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    airport_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    city_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    country_code: Mapped[str | None] = mapped_column(
        String(4), nullable=True, index=True
    )
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scale: Mapped[str | None] = mapped_column(String(16), nullable=True)


class Flight(Base):

    __tablename__ = "flights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    flight_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    airport_iata: Mapped[str] = mapped_column(String(10), index=True)

    direction: Mapped[str] = mapped_column(String(16))
    location: Mapped[str] = mapped_column(String(16))

    requires_passport: Mapped[bool] = mapped_column(Boolean, default=True)

    airline_iata: Mapped[str | None] = mapped_column(String(8), nullable=True)
    flight_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    flight_iata: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True
    )

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

    __tablename__ = "airport_operational_configs"

    airport_iata: Mapped[str] = mapped_column(String(10), primary_key=True)

    passport_counter_count: Mapped[int] = mapped_column(Integer, default=4)
    passport_staff_count: Mapped[int] = mapped_column(Integer, default=8)

    passport_service_time_minutes: Mapped[float] = mapped_column(
        Float, default=1.0
    )

    security_lane_count: Mapped[int] = mapped_column(Integer, default=8)
    domestic_security_lane_count: Mapped[int] = mapped_column(Integer, default=8)
    international_security_lane_count: Mapped[int] = mapped_column(Integer, default=8)
    security_service_time_minutes: Mapped[float] = mapped_column(
        Float, default=SECURITY_EFFECTIVE_SERVICE_TIME_MINUTES
    )

    passport_staff_per_counter: Mapped[float] = mapped_column(Float, default=2.0)
    passport_service_rate_per_staff: Mapped[float] = mapped_column(Float, default=1.0)
    passport_efficiency_multiplier: Mapped[float] = mapped_column(Float, default=0.8125)

    passport_departure_server_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    passport_arrival_server_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )

    arrival_bank_threshold: Mapped[int] = mapped_column(Integer, default=5)

    is_seeded_default: Mapped[bool] = mapped_column(Boolean, default=False)


class AirportScaleConfig(Base):

    __tablename__ = "airport_scale_configs"

    scale: Mapped[str] = mapped_column(String(16), primary_key=True)
    departure_passport_servers: Mapped[int] = mapped_column(Integer, nullable=False)
    departure_passport_servers_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arrival_passport_servers: Mapped[int] = mapped_column(Integer, nullable=False)
    arrival_passport_servers_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    domestic_security_lanes: Mapped[int] = mapped_column(Integer, nullable=False)
    international_security_lanes: Mapped[int] = mapped_column(Integer, nullable=False)
    # ADIM (MEGA Dynamic Security) - `international_security_lanes`
    # (yukarıda) artık BASE lane sayısı olarak okunuyor; bu kolon o
    # BASE'in üstüne çıkabileceği MAX'ı taşır. NULL ise (LARGE/MEDIUM/
    # SMALL - ve MEGA'da bile bilinçli olarak boş bırakılırsa) security
    # STATIC kalır - dynamic sadece bu değer DOLU olduğunda devreye
    # girer (bkz. config.py `_build_config_view`).
    international_security_lanes_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ADIM (5-Minute Control Interval) - MEGA dynamic security VE
    # passport için checkpoint aralığı artık DB'den okunuyor (eskiden
    # engine.py'de hardcoded 10dk idi). NULL ise kod tarafında 5
    # dakikaya düşülür (bkz. constants.py DEFAULT_DYNAMIC_CONTROL_
    # INTERVAL_MINUTES).
    security_dynamic_control_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passport_dynamic_control_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)


class QueuePrediction(Base):

    __tablename__ = "queue_predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))
    window_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime)
    operational_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    flight_count: Mapped[int] = mapped_column(Integer, default=0)
    expected_passengers: Mapped[int] = mapped_column(Integer, default=0)
    baseline_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    flight_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    passenger_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    utilization: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_wait_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)

    risk: Mapped[str] = mapped_column(String(16))
    reasons: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float, default=0.1)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "airport_iata", "process", "window_start",
            name="uq_queue_prediction_window",
        ),
    )


class QueueWaitDisplay5m(Base):
    # estimated_wait_minutes = queue-state / virtual-arrival wait: "şu an
    # bir yolcu gelse mevcut FIFO durumuna göre kaç dakika beklerdi"
    # (bkz. engine.py:event_driven_display_series, core/event_queue.py:
    # virtual_arrival_wait) - "o pencerede yeni gelenlerin ortalama
    # wait'i" DEĞİL. Her operasyonel gün için process başına sabit 288
    # satır (5dk aralıklarla) - yeni arrival olmasa da satır YOK OLMAZ.

    __tablename__ = "queue_wait_display_5m"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))
    window_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    estimated_wait_minutes: Mapped[float] = mapped_column(Float)
    risk: Mapped[str] = mapped_column(String(16))
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "airport_iata", "process", "window_start",
            name="uq_queue_wait_display_5m_window",
        ),
    )


class HistoricalFlightCount(Base):

    __tablename__ = "historical_flight_counts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))
    hour_of_day: Mapped[int] = mapped_column(Integer)
    day_of_week: Mapped[int] = mapped_column(Integer)
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


class QueueCalculationHourlyAudit(Base):
    # READ-ONLY audit satırı - queue hesaplama motoru bu tabloyu ASLA
    # okumaz (bkz. app/queue/audit.py modül docstring'i). Sadece
    # ZATEN hesaplanmış `WindowPrediction`/coupling sonuçlarının SQL'den
    # izlenebilir bir kopyası.

    __tablename__ = "queue_calculation_hourly_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)

    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    airport_scale: Mapped[str | None] = mapped_column(String(16), nullable=True)
    airport_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    calculation_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    process: Mapped[str] = mapped_column(String(16))

    window_start_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    window_start_local: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    window_end_utc: Mapped[datetime] = mapped_column(DateTime)
    window_end_local: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    direction: Mapped[str | None] = mapped_column(String(16), nullable=True)

    flight_count: Mapped[int] = mapped_column("ucus_sayisi", Integer, default=0)
    expected_passengers: Mapped[float] = mapped_column("beklenen_yolcu_sayisi", Float, default=0)

    lane_or_desk_count: Mapped[int | None] = mapped_column("lane_veya_gise_sayisi", Integer, nullable=True)
    resource_type: Mapped[str | None] = mapped_column("kaynak_tipi", String(16), nullable=True)
    service_time_minutes: Mapped[float | None] = mapped_column("islem_suresi_dakika", Float, nullable=True)
    capacity_per_resource_per_hour: Mapped[float | None] = mapped_column("kaynak_basina_saatlik_kapasite", Float, nullable=True)
    total_hourly_capacity: Mapped[float | None] = mapped_column("toplam_saatlik_kapasite", Float, nullable=True)

    backlog_start: Mapped[float | None] = mapped_column("saat_basi_bekleyen_yolcu", Float, nullable=True)
    backlog_end: Mapped[float | None] = mapped_column("saat_sonu_bekleyen_yolcu", Float, nullable=True)

    estimated_wait_minutes: Mapped[float | None] = mapped_column("tahmini_bekleme_dakika", Float, nullable=True)
    risk: Mapped[str | None] = mapped_column(String(16), nullable=True)
    display_status: Mapped[str | None] = mapped_column("gorunum_durumu", String(16), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "run_id", "airport_iata", "process", "window_start_utc",
            name="uq_queue_calc_hourly_audit",
        ),
    )


class QueueFlightHourContributionAudit(Base):
    # Bir flight'ın bir saatlik process window'a katkısı - hangi profil
    # segmenti/yüzdesi kullanıldığı, kaç yolcu ürettiği. Bkz. audit.py.

    __tablename__ = "queue_flight_hour_contribution_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)

    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))

    window_start_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    window_start_local: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    window_end_local: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    flight_db_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flight_key: Mapped[str] = mapped_column(String(64), index=True)

    flight_iata: Mapped[str | None] = mapped_column(String(16), nullable=True)
    flight_icao: Mapped[str | None] = mapped_column(String(16), nullable=True)
    airline_iata: Mapped[str | None] = mapped_column(String(8), nullable=True)
    airline_icao: Mapped[str | None] = mapped_column(String(8), nullable=True)

    direction: Mapped[str] = mapped_column(String(16))
    dep_iata: Mapped[str | None] = mapped_column(String(10), nullable=True)
    arr_iata: Mapped[str | None] = mapped_column(String(10), nullable=True)

    dep_time_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dep_time_local: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arr_time_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    arr_time_local: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    is_domestic: Mapped[bool] = mapped_column(Boolean, default=False)
    is_international: Mapped[bool] = mapped_column(Boolean, default=False)
    is_schengen: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    requires_passport: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    aircraft_icao: Mapped[str | None] = mapped_column(String(8), nullable=True)
    resolved_aircraft_capacity: Mapped[int | None] = mapped_column("cozumlenen_ucak_kapasitesi", Integer, nullable=True)

    profile_name: Mapped[str] = mapped_column("profil_adi", String(32))
    profile_segment: Mapped[str | None] = mapped_column("profil_segmenti", String(32), nullable=True)
    profile_percentage: Mapped[float | None] = mapped_column("profil_yuzdesi", Float, nullable=True)

    segment_start_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    segment_end_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    passengers_from_segment: Mapped[float | None] = mapped_column("segmentten_gelen_yolcu", Float, nullable=True)
    passengers_contributed_to_this_hour: Mapped[float] = mapped_column("bu_saate_katilan_yolcu", Float, default=0)

    routing_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    routing_destination: Mapped[str | None] = mapped_column(String(32), nullable=True)
    security_arrival_source: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QueueCohortAudit(Base):
    # Her üretilen 5dk (departure) / 1dk (arrival) cohort için bir satır.

    __tablename__ = "queue_cohort_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)

    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))

    flight_key: Mapped[str] = mapped_column(String(64), index=True)
    flight_iata: Mapped[str | None] = mapped_column(String(16), nullable=True)
    direction: Mapped[str] = mapped_column(String(16))

    profile_segment: Mapped[str | None] = mapped_column("profil_segmenti", String(32), nullable=True)
    profile_percentage: Mapped[float | None] = mapped_column("profil_yuzdesi", Float, nullable=True)
    aircraft_capacity: Mapped[int | None] = mapped_column("ucak_kapasitesi", Integer, nullable=True)

    cohort_start_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    cohort_start_local: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cohort_resolution_minutes: Mapped[int] = mapped_column("cohort_cozunurluk_dakika", Integer)

    passenger_count: Mapped[float] = mapped_column("yolcu_sayisi", Float, default=0)

    routing_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_process: Mapped[str | None] = mapped_column(String(32), nullable=True)
    destination_process: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QueueServiceEventAudit(Base):
    # ServiceEvent seviyesinde audit - gerçek FIFO çıktısının kopyası.

    __tablename__ = "queue_service_event_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)

    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))

    source_flight_keys: Mapped[str | None] = mapped_column(Text, nullable=True)

    arrival_time_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    arrival_time_local: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    service_start_time_utc: Mapped[datetime] = mapped_column(DateTime)
    service_start_time_local: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completion_time_utc: Mapped[datetime] = mapped_column(DateTime)
    completion_time_local: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    wait_minutes: Mapped[float] = mapped_column("bekleme_dakika", Float)
    passenger_count: Mapped[float] = mapped_column("yolcu_sayisi", Float)

    resource_count: Mapped[int | None] = mapped_column("kaynak_sayisi", Integer, nullable=True)
    service_time_minutes: Mapped[float | None] = mapped_column("islem_suresi_dakika", Float, nullable=True)
    backlog_before: Mapped[float | None] = mapped_column("islem_oncesi_backlog", Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QueueRoutingSummaryAudit(Base):
    # Airport/gün bazında classification özeti (kaç domestic/intl/
    # schengen/non-schengen flight, kaç tanesi passport'u bypass etti).

    __tablename__ = "queue_routing_summary_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    calculation_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    total_physical_flights: Mapped[int] = mapped_column(Integer, default=0)
    domestic_departure_flights: Mapped[int] = mapped_column(Integer, default=0)
    international_departure_flights: Mapped[int] = mapped_column(Integer, default=0)
    domestic_arrival_flights: Mapped[int] = mapped_column(Integer, default=0)
    international_arrival_flights: Mapped[int] = mapped_column(Integer, default=0)

    schengen_departure_flights: Mapped[int] = mapped_column(Integer, default=0)
    non_schengen_departure_flights: Mapped[int] = mapped_column(Integer, default=0)
    schengen_arrival_flights: Mapped[int] = mapped_column(Integer, default=0)
    non_schengen_arrival_flights: Mapped[int] = mapped_column(Integer, default=0)

    schengen_departures_bypassed_passport: Mapped[int] = mapped_column(Integer, default=0)
    non_schengen_departures_entered_passport: Mapped[int] = mapped_column(Integer, default=0)
    schengen_arrivals_bypassed_passport: Mapped[int] = mapped_column(Integer, default=0)
    non_schengen_arrivals_entered_passport: Mapped[int] = mapped_column(Integer, default=0)

    codeshare_records_removed: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "run_id", "airport_iata", name="uq_queue_routing_summary_audit",
        ),
    )


class QueueCountryRoutingAudit(Base):

    __tablename__ = "queue_country_routing_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    calculation_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    country_code: Mapped[str | None] = mapped_column(String(4), nullable=True, index=True)
    country_name: Mapped[str | None] = mapped_column("ulke_adi", String(100), nullable=True)

    direction: Mapped[str] = mapped_column(String(16))
    flight_type: Mapped[str] = mapped_column("ucus_tipi", String(16))
    schengen_status: Mapped[str] = mapped_column("schengen_durumu", String(16))

    flight_count: Mapped[int] = mapped_column("ucus_sayisi", Integer, default=0)
    passenger_count: Mapped[float] = mapped_column("yolcu_sayisi", Float, default=0)

    passport_required_count: Mapped[int] = mapped_column("pasaport_gereken_yolcu_sayisi", Integer, default=0)
    passport_bypass_count: Mapped[int] = mapped_column("pasaport_atlayan_yolcu_sayisi", Integer, default=0)

    security_dom_passengers: Mapped[float] = mapped_column("domestic_security_yolcu_sayisi", Float, default=0)
    passport_dep_passengers: Mapped[float] = mapped_column("departure_passport_yolcu_sayisi", Float, default=0)
    security_intl_passengers: Mapped[float] = mapped_column("international_security_yolcu_sayisi", Float, default=0)
    passport_arr_passengers: Mapped[float] = mapped_column("arrival_passport_yolcu_sayisi", Float, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QueueResourceConfigAudit(Base):

    __tablename__ = "queue_resource_config_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    airport_scale: Mapped[str | None] = mapped_column(String(16), nullable=True)

    domestic_security_lanes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # `international_security_lanes` = BASE (MEGA'da dynamic'in başladığı
    # taban, diğer scale'lerde sabit statik değer - bkz. models.py
    # AirportScaleConfig yorumu, section 30'daki "duplicate kolon
    # oluşturma" uyarısı gereği ayrı bir "_base" kolonu AÇILMADI).
    international_security_lanes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    international_security_lanes_max: Mapped[int | None] = mapped_column(Integer, nullable=True)

    departure_passport_desks_base: Mapped[int | None] = mapped_column(Integer, nullable=True)
    departure_passport_desks_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arrival_passport_desks_base: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arrival_passport_desks_max: Mapped[int | None] = mapped_column(Integer, nullable=True)

    security_control_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passport_control_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    security_service_time_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    passport_service_time_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)

    domestic_security_capacity_per_hour: Mapped[float | None] = mapped_column(Float, nullable=True)
    international_security_capacity_per_hour: Mapped[float | None] = mapped_column(Float, nullable=True)

    config_source: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QueueDynamicStaffingAudit(Base):

    __tablename__ = "queue_dynamic_staffing_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))

    checkpoint_time_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    checkpoint_time_local: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    previous_server_count: Mapped[int | None] = mapped_column("onceki_gise_sayisi", Integer, nullable=True)
    new_server_count: Mapped[int] = mapped_column("yeni_gise_sayisi", Integer)

    backlog: Mapped[float | None] = mapped_column(Float, nullable=True)
    lookahead_demand: Mapped[float | None] = mapped_column("ileri_bakis_talep", Float, nullable=True)
    needed_servers: Mapped[int | None] = mapped_column("gereken_gise_sayisi", Integer, nullable=True)
    # ADIM (Discrete Operational Levels) - `needed_servers` HAM matematik
    # sonucu; bu alan bunun sabit operational level listesine (15/20/25/
    # 30/35/40 gibi) yuvarlanmış İDEAL hedefi - `new_server_count` (tek
    # checkpoint'te sadece BİR seviye ilerleyebildiği için) bundan farklı
    # olabilir (ör. needed=38, target_operational_level=40, ama
    # new_server_count sadece bir önceki seviyeden bir sonrakine çıkar).
    target_operational_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ramp_delta: Mapped[int | None] = mapped_column("gise_degisimi", Integer, nullable=True)
    pending_retirements: Mapped[int | None] = mapped_column("kapanmayi_bekleyen_gise_sayisi", Integer, nullable=True)
    # normal_load / demand_threshold / backlog_pressure / lookahead_pressure
    # / peak_pressure / scale_down - bkz. event_queue.py _apply_checkpoint.
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QueueGraphDisplayAudit(Base):

    __tablename__ = "queue_graph_display_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    airport_iata: Mapped[str] = mapped_column(String(10), index=True)
    process: Mapped[str] = mapped_column(String(16))

    bucket_start_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    bucket_start_local: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    source_resolution_minutes: Mapped[int] = mapped_column(Integer, default=5)
    visual_bucket_minutes: Mapped[int] = mapped_column(Integer, default=30)
    source_point_count: Mapped[int] = mapped_column("kaynak_nokta_sayisi", Integer, default=0)

    average_wait_minutes: Mapped[float | None] = mapped_column("ortalama_bekleme_dakika", Float, nullable=True)
    peak_wait_minutes: Mapped[float | None] = mapped_column("maksimum_bekleme_dakika", Float, nullable=True)
    display_wait_minutes: Mapped[float | None] = mapped_column("grafikte_gosterilen_bekleme_dakika", Float, nullable=True)

    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BaselineObservation(Base):

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
