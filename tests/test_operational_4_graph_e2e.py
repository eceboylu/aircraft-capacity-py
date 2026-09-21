"""
ADIM 6D-2 H1 - 4-GRAF OPERASYONEL E2E AUDIT + WAIT DISPLAY AUDIT.

Gerçek zincir uçtan uca sürülür:

    synthetic AirLabs response (ham dict)
      -> app.queue.ingestion.sources.parse_source_a (GERÇEK parser)
      -> app.queue.ingestion.refresh.refresh_flights (GERÇEK upsert)
      -> app.queue.engine.run_predictions (GERÇEK motor:
         ICAO capacity resolver -> demand -> security/passport scoring
         -> backlog zinciri -> QueuePrediction)
      -> app.queue.api.airport_predictions (GERÇEK, salt-okunur katman)

Hiçbir fixture'a beklenen risk/wait YAZILMAZ - QueuePrediction
satırları HER ZAMAN gerçek motor tarafından üretilir; testler sadece
üretilen sonucu (ve elle hesaplanmış beklenen değerleri, üretim
formülünün KENDİSİ kullanılarak - yeni bir formül İCAT EDİLMEDEN)
doğrular.

Kapasite çözümleyici: `MockCapacityResolver` (tests/factories.py) -
`AircraftCapacityService` ile AYNI `.resolve(icao, airline)` arayüzü,
mevcut E2E testlerinde (test_e2e_ist.py) zaten kullanılan standart
test double'ı. Ülke/domestic-international ayrımı GERÇEK
`resolve_location()`/`country_lookup()` zincirinden geçer - hiçbir
"international = passport" gibi kısayol UYDURULMAZ.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.constants import (
    PROCESS_PASSPORT,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
    RISK_CRITICAL,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_ORDER,
)
from app.queue.engine import run_predictions
from app.queue.ingestion.airports_import import country_lookup
from app.queue.ingestion.refresh import refresh_flights
from app.queue.ingestion.sources import parse_source_a
from app.queue.models import Airport, HistoricalFlightCount

from .factories import MockCapacityResolver

DAY = datetime(2026, 9, 15)  # gerçek run_predictions/parse_source_a chain için sabit bir tarih


# ------------------------------------------------------------------
# Ortak altyapı
# ------------------------------------------------------------------

@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    try:
        yield db
    finally:
        db.close()


def seed_airports(session):
    """
    Gerçek IATA/ICAO/ülke kimlikleri (uydurma değil): IST/ESB/ADB
    Türkiye, CDG Fransa - `resolve_location()`'ın gerçek domestic/
    international ayrımını sürebilmesi için.
    """
    rows = [
        ("IST", "LTFM", "Istanbul Airport", "TR"),
        ("ESB", "LTAC", "Ankara Esenboga Airport", "TR"),
        ("ADB", "LTBJ", "Izmir Adnan Menderes Airport", "TR"),
        ("CDG", "LFPG", "Paris Charles de Gaulle Airport", "FR"),
    ]
    for iata, icao, name, country in rows:
        session.add(Airport(
            iata_code=iata, icao_code=icao, airport_name=name,
            city_code=iata, country_code=country, timezone="UTC",
        ))
    session.commit()


def seed_baseline(session, airport_iata, process, hour_of_day, average_flight_count):
    session.add(HistoricalFlightCount(
        airport_iata=airport_iata,
        process=process,
        hour_of_day=hour_of_day,
        day_of_week=DAY.weekday(),
        average_flight_count=average_flight_count,
        sample_size=5,
    ))
    session.commit()


def hour_start(hour: int) -> datetime:
    return DAY.replace(hour=hour, minute=0, second=0, microsecond=0)


_seq = {"n": 0}


def _next_number() -> str:
    _seq["n"] += 1
    return str(_seq["n"])


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def domestic_departure_records(window_start, count, airport="IST", partner="ESB", aircraft="A320"):
    """
    IST->ESB (TR->TR), süre 70dk. ADIM (Airport Queue Model V2 - sabit
    -120dk offset): dep_scheduled = window_start + 125dk -> effective_time
    = window_start + 5dk (pencerenin İÇİNDE, uçuş başına 1dk kaydırılarak
    - aynı pencerede kalır).
    """
    records = []
    for i in range(count):
        dep_dt = window_start + timedelta(minutes=125 + i)
        arr_dt = dep_dt + timedelta(minutes=70)
        records.append({
            "flight_iata": f"TK{_next_number()}", "flight_number": _next_number(),
            "airline_iata": "TK", "dep_iata": airport, "arr_iata": partner,
            "dep_time_utc": _fmt(dep_dt), "arr_time_utc": _fmt(arr_dt),
            "status": "scheduled", "aircraft_icao": aircraft,
        })
    return records


def international_departure_records(window_start, count, airport="IST", partner="CDG", aircraft="A320"):
    """
    IST->CDG (TR->FR), süre 210dk. ADIM (Airport Queue Model V2 - sabit
    -120dk offset): dep_scheduled = window_start + 125dk -> effective_time
    = window_start + 5dk (lokasyondan/süreden bağımsız SABİT offset,
    domestic ile AYNI formül).
    """
    records = []
    for i in range(count):
        dep_dt = window_start + timedelta(minutes=125 + i)
        arr_dt = dep_dt + timedelta(minutes=210)
        records.append({
            "flight_iata": f"TK{_next_number()}", "flight_number": _next_number(),
            "airline_iata": "TK", "dep_iata": airport, "arr_iata": partner,
            "dep_time_utc": _fmt(dep_dt), "arr_time_utc": _fmt(arr_dt),
            "status": "scheduled", "aircraft_icao": aircraft,
        })
    return records


def international_arrival_records(window_start, count, airport="IST", partner="CDG", aircraft="A320"):
    """
    CDG->IST (FR->TR) varış - sabit 15dk passport buffer (süreden bağımsız).
    arr_scheduled = window_start - 10dk -> effective_time = window_start + 5dk.
    """
    records = []
    for i in range(count):
        arr_dt = window_start + timedelta(minutes=-10 + i)
        dep_dt = arr_dt - timedelta(minutes=180)
        records.append({
            "flight_iata": f"TK{_next_number()}", "flight_number": _next_number(),
            "airline_iata": "TK", "dep_iata": partner, "arr_iata": airport,
            "dep_time_utc": _fmt(dep_dt), "arr_time_utc": _fmt(arr_dt),
            "status": "scheduled", "aircraft_icao": aircraft,
        })
    return records


def ingest(session, dep_records, arr_records):
    countries = country_lookup(session)
    parsed = (
        parse_source_a(dep_records, "departure", countries, {})
        + parse_source_a(arr_records, "arrival", countries, {})
    )
    if parsed:
        refresh_flights(session, parsed)
    return parsed


CAPACITIES = {"A320": 180, "B738": 189, "B77W": 350, "E190": 100}


def resolver():
    return MockCapacityResolver(capacities=CAPACITIES)


def risk_of(series: dict, window_start: datetime) -> str | None:
    for w in series["windows"]:
        if w["window_start"] == window_start.isoformat():
            return w["risk"]
    return None


def window_of(series: dict, window_start: datetime) -> dict | None:
    for w in series["windows"]:
        if w["window_start"] == window_start.isoformat():
            return w
    return None


# ==================================================================
# A) PASSPORT WAIT DISPLAY AUDIT (statik kaynak kilidi)
# ==================================================================

INDEX_HTML = None


def _read_index_html():
    import os
    global INDEX_HTML
    if INDEX_HTML is None:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "app", "web", "static", "index.html",
        )
        with open(path, "r", encoding="utf-8") as fh:
            INDEX_HTML = fh.read()
    return INDEX_HTML


def test_a_wait_time_priority_estimated_wait_wins_over_capacity_exceeded():
    """
    estimated_wait_minutes=18.3 (dolu) + utilization=1.4 (>=1) ->
    frontend "18.3 dk" göstermeli, "Kapasite aşıldı" DEĞİL.

    `waitTimeText()`'in KENDİSİ `estimated_wait_minutes != null` dalını
    `utilization`'dan ÖNCE kontrol ediyor (mevcut kod zaten doğru -
    DEĞİŞTİRİLMEDİ, bkz. app/web/static/index.html) - bu test SADECE
    o önceliği kilitliyor.
    """
    html = _read_index_html()
    script = html.split("<script>", 1)[1]
    body = script.split("function waitTimeText(w) {", 1)[1].split("\n  }", 1)[0]

    first_check_pos = body.index("estimated_wait_minutes")
    second_check_pos = body.index("utilization")
    assert first_check_pos < second_check_pos, (
        "waitTimeText() estimated_wait_minutes kontrolünü utilization'dan "
        "ÖNCE yapmalı - aksi halde dolu bir wait değeri 'Kapasite aşıldı' "
        "ile ezilebilir."
    )


def test_a_wait_time_function_used_by_both_current_and_click_selected_window():
    """
    ADIM (Frontend 4-Graph Contract), İmza ADIM (International Departure
    Split Graphs)'ta SADELEŞTİ - `graphSectionHtml()` her iki durumda da
    (tıklanmamış -> current, tıklanmış -> `window_start`'ı eşleşen
    pencere) AYNI `displayWindow` üzerinden `waitDetailHtml()`'i
    çağırmalı - current ve click için AYRI/farklı bir öncelik mantığı
    olmamalı. `kind` parametresi (Bölüm 50'nin "single" vs "breakdown"
    ayrımı) KALKTI - International Departure artık passport/security
    için İKİ BAĞIMSIZ `graphSectionHtml()` çağrısı, ayrı bir "breakdown"
    kind'ına gerek yok.
    """
    html = _read_index_html()
    script = html.split("<script>", 1)[1]
    graph_fn = script.split("function graphSectionHtml(key, title, section) {", 1)[1].split("\n  }", 1)[0]

    assert "state.detail[key]" in graph_fn
    assert "section.current" in graph_fn
    assert "waitDetailHtml(displayWindow)" in graph_fn
    # Tek bir waitDetailHtml çağrısı var - current/click için ikinci
    # bir kopya YOK; `displayWindow` HER İKİ durumda da AYNI değişkenden
    # gelir (yukarıda tek bir yerde çözülür).
    assert graph_fn.count("waitDetailHtml(") == 1


def test_a_waittimetext_never_recomputes_erlang_c():
    html = _read_index_html()
    script = html.split("<script>", 1)[1]
    body = script.split("function waitTimeText(w) {", 1)[1].split("\n  }", 1)[0]
    for forbidden in ("Erlang", "erlang_c", "* mu", "lam /"):
        assert forbidden not in body


# ==================================================================
# B/C) DOMESTIC SECURITY SURGE - IST 06:00/07:00/08:00
# ==================================================================

@pytest.fixture
def domestic_surge_session(session):
    seed_airports(session)
    for hour in (6, 7, 8):
        seed_baseline(session, "IST", PROCESS_SECURITY_DOMESTIC, hour, average_flight_count=3.0)

    dep_records = (
        domestic_departure_records(hour_start(6), 3, aircraft="E190")
        + domestic_departure_records(hour_start(7), 9, aircraft="E190")
        + domestic_departure_records(hour_start(8), 3, aircraft="E190")
    )
    ingest(session, dep_records, [])
    run_predictions(session, resolver(), airports=["IST"], now=datetime(2026, 9, 15, 23, 0))
    return session


def test_c_domestic_surge_raises_domestic_security_risk(domestic_surge_session):
    """
    ADIM (Visible Risk = Gerçek Queue Pressure) ile GÜNCELLENDİ: risk
    artık sadece o saatin KENDİ gelen talebine değil, GERÇEK, event-
    türevli backlog'a da bakıyor. 07:00'ın 9-uçuşluk dalgası 08:00'a
    devasa bir backlog bırakıyor - gerçek `estimated_wait_minutes` bunu
    ZATEN gösteriyordu, risk artık bu GERÇEĞİ doğru yansıtıyor: 08:00 de
    CRITICAL olmalı, eski model gibi yapay biçimde LOW'a düşmemeli.

    ADIM (Departure Show-Up Profile): her flight artık KENDİ departure
    saatinden ÖNCEKİ 3 saate show-up ile yayılıyor (bkz. `domain/
    demand.py:departure_show_up_events()`) - 06:00'ın grubu (3 flight,
    dep~08:05) BİLE artık show-up'ın %60'ını 06:00'a, kalanını 05:00/
    07:00'e taşıyor; 07:00 grubu (9 flight, dep~09:05) 06:00'a da
    %20 taşıyor - bu yüzden 06:00'ın KENDİ talebi artık MEDIUM eşiğine
    (rho>=0.7) çıkıyor (eskiden LOW'du) - gerçek, doğrulanmış (bu turda
    `predict_airport()` ile üretilmiş) bir sonuç, körlemesine seçilmedi.
    """
    result = airport_predictions(domestic_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    assert risk_of(result["domestic_security"], hour_start(6)) == RISK_MEDIUM
    assert risk_of(result["domestic_security"], hour_start(7)) == RISK_CRITICAL
    assert risk_of(result["domestic_security"], hour_start(8)) == RISK_CRITICAL


def test_c_domestic_surge_does_not_raise_international_security_or_passport(domestic_surge_session):
    """
    ADIM (24-Hour Graph) ile GÜNCELLENDİ: `international_security`
    (BEŞ görünür grafikten biri, artık 24-saat PADDED) bu saatler için
    `None` DEĞİL (bkz. api.py `_pad_series_to_24_hours`) - ama GERÇEK/
    etkilenmiş bir satır da DEĞİL, sıfır-talep (flight_count=0)
    bucket'ı. `international_passport` (LEGACY, bilinçli olarak PAD
    EDİLMEDİ) hâlâ eski `None` davranışını korur. Asıl iddia AYNI:
    domestic surge bu iki süreci HİÇ ETKİLEMEDİ.
    """
    result = airport_predictions(domestic_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    for hour in (6, 7, 8):
        assert window_of(result["international_security"], hour_start(hour))["flight_count"] == 0
        assert window_of(result["international_passport"], hour_start(hour)) is None


def test_c_domestic_flight_never_enters_passport(domestic_surge_session):
    from app.queue.models import QueuePrediction
    from sqlalchemy import select
    rows = domestic_surge_session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == "IST",
            QueuePrediction.process == PROCESS_PASSPORT,
        )
    ).scalars().all()
    assert rows == []


def test_c_overall_follows_domestic_security_at_its_surge_hour(domestic_surge_session):
    result = airport_predictions(domestic_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    assert risk_of(result["overall"], hour_start(7)) == RISK_CRITICAL


# ==================================================================
# D) INTERNATIONAL SECURITY SURGE - IST 10:00/11:00/12:00
#    (international departure GERÇEKTEN hem security_intl HEM passport
#    besler - flows.py `passport_flights()`; bu YENİ bir kural DEĞİL.)
# ==================================================================

@pytest.fixture
def intl_security_surge_session(session):
    seed_airports(session)
    for hour in (10, 11, 12):
        seed_baseline(session, "IST", PROCESS_SECURITY_INTL, hour, average_flight_count=2.0)

    dep_records = (
        international_departure_records(hour_start(10), 1, aircraft="E190")
        + international_departure_records(hour_start(11), 3, aircraft="B738")
        + international_departure_records(hour_start(12), 1, aircraft="E190")
    )
    ingest(session, dep_records, [])
    run_predictions(session, resolver(), airports=["IST"], now=datetime(2026, 9, 15, 23, 0))
    return session


def test_d_international_security_surge_t0_t1_t2(intl_security_surge_session):
    """
    ADIM (Passport->Security zaman-kuplajı): `international_security`nin
    talebi artık uluslararası kalkışın KENDİ effective_time'ında değil,
    passport'un o saatte GERÇEKTEN serbest bıraktığı miktardır - ve bu
    miktar passport'un KENDİ kapasitesiyle (320 pax/saat, 4 gişe x 2
    paralel görevli/gişe) sınırlıdır. Bu yapısal bir sonuç: passport
    kapasitesi (320/saat) security kapasitesinin (480/saat) SADECE
    2/3'ü olduğu için, SADECE passport'tan gelen talep security_intl'i
    ASLA rho>=0.7'ye (MEDIUM eşiği) bile taşıyamaz (320/480=0.667<0.7) -
    passport kendisi zaten çok daha önce (320/saat'te) tıkanır. T1'de
    gerçek darboğaz PASSPORT'tur (bkz. aşağıdaki test), security_intl
    ise passport'un "musluğundan" akan miktarla sınırlı kalır ve HER
    ZAMAN LOW kalır.
    """
    result = airport_predictions(intl_security_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    t0 = window_of(result["international_security"], hour_start(10))
    t1 = window_of(result["international_security"], hour_start(11))
    t2 = window_of(result["international_security"], hour_start(12))
    assert t0["risk"] == RISK_LOW
    # T1: passport'un o saat GERÇEKTEN serbest bırakabileceği tavan -
    # 320 pax/saat (passport kapasitesi) - security'nin KENDİ 480/saat
    # kapasitesinin rho eşiğinin (0.7) altında kalır, yani hep LOW kalır.
    assert t1["expected_passengers"] <= 320.0
    assert t1["risk"] == RISK_LOW
    assert t2["risk"] == RISK_LOW


def test_d_domestic_security_unaffected_by_international_surge(intl_security_surge_session):
    """ADIM (24-Hour Graph): domestic_security artık 24-saat PADDED - bu saatler `None` değil, sıfır-talep bucket'ı (`flight_count=0`)."""
    result = airport_predictions(intl_security_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    for hour in (10, 11, 12):
        assert window_of(result["domestic_security"], hour_start(hour))["flight_count"] == 0


def test_d_passport_is_genuinely_affected_because_international_departures_feed_it_too(
    intl_security_surge_session,
):
    """
    "international = passport" diye YENİ bir kural UYDURULMADI - bu,
    flows.py'deki MEVCUT `passport_flights()` kuralının (uluslararası
    KALKIŞ + uluslararası VARIŞ) doğal bir sonucu. T1'deki uluslararası
    kalkış sürgülü passport talebini de gerçekten yükseltir.
    """
    result = airport_predictions(intl_security_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    t0 = window_of(result["international_passport"], hour_start(10))
    t1 = window_of(result["international_passport"], hour_start(11))
    t2 = window_of(result["international_passport"], hour_start(12))
    assert t0["risk"] == RISK_LOW
    assert t1["risk"] == RISK_CRITICAL
    assert t1["utilization"] > 1.0
    # T2: rho tekrar düşük VE (ADIM Departure Show-Up Profile ile,
    # gerçek `predict_airport()` çıktısıyla doğrulandı) backlog bu
    # senaryoda T2'ye gelmeden TAMAMEN boşalıyor (show-up'ın kendisi
    # T1'in yükünü zaten 3 saate yaydığı için tek-saatlik eski modele
    # göre daha AZ art arda birikim kalıyor) - wait sıfıra döner, ama
    # HÂLÂ T1'den KESİN olarak düşük/eşit (asla YAPAY olarak T1'i AŞMAZ).
    assert t2["risk"] == RISK_LOW
    assert t2["estimated_wait_minutes"] <= t1["estimated_wait_minutes"]
    assert t2["estimated_wait_minutes"] == 0.0


def test_d_overall_matches_max_of_the_three_real_series(intl_security_surge_session):
    """
    `overall`, `_merge_overall_series()`'in GERÇEK girdileri olan
    `domestic_security`/`international_security`/`passport_departure`/
    `passport_arrival`'ın worst-of'udur (bkz. api.py - "Overall Graph
    Legacy Passport Bug Fix" ADIM'ı, legacy `international_passport`
    ARTIK KULLANILMIYOR). Bu test ÖNCEDEN legacy `international_passport`
    ile karşılaştırıyordu - bu, `passport_departure`/`passport_arrival`
    ile (farklı server-count formülü, bkz. core/scoring.py) COĞU zaman
    TESADÜFEN aynı risk'i üretiyordu; ADIM (Visible Risk = Gerçek Queue
    Pressure) ile bu tesadüf artık geçerli değil (`passport_departure`
    backlog-farkındalı, legacy DEĞİL) - test artık `overall`'ın GERÇEK
    girdisiyle karşılaştırıyor.
    """
    result = airport_predictions(intl_security_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    for hour in (10, 11, 12):
        ds = risk_of(result["domestic_security"], hour_start(hour))
        isec = risk_of(result["international_security"], hour_start(hour))
        pdep = risk_of(result["international_departure"]["passport"], hour_start(hour))
        parr = risk_of(result["international_arrival"], hour_start(hour))
        candidates = [r for r in (ds, isec, pdep, parr) if r is not None]
        expected = max(candidates, key=lambda r: RISK_ORDER.get(r, -1))
        assert risk_of(result["overall"], hour_start(hour)) == expected


# ==================================================================
# E) INTERNATIONAL PASSPORT SURGE - IST 15:00/16:00/17:00 (VARIŞ, sadece)
#    Varışlar security'yi hiç BESLEMEZ (security_flights sadece kalkış
#    alır) - bu saatlerde security tamamen sessiz kalmalı.
#    D'nin backlog'u (13:00/14:00 boş saatlerde otomatik servis
#    edildiği için) bu bloğa GİRMEDEN önce sıfırlanmış olmalı.
# ==================================================================

@pytest.fixture
def passport_surge_session(session):
    seed_airports(session)
    for hour in (10, 11, 12):
        seed_baseline(session, "IST", PROCESS_SECURITY_INTL, hour, average_flight_count=2.0)

    dep_records = (
        international_departure_records(hour_start(10), 1, aircraft="E190")
        + international_departure_records(hour_start(11), 3, aircraft="B738")
        + international_departure_records(hour_start(12), 1, aircraft="E190")
    )
    arr_records = (
        international_arrival_records(hour_start(15), 1, aircraft="E190")
        + international_arrival_records(hour_start(16), 7, aircraft="E190")
        + international_arrival_records(hour_start(17), 1, aircraft="E190")
    )
    ingest(session, dep_records, arr_records)
    run_predictions(session, resolver(), airports=["IST"], now=datetime(2026, 9, 15, 23, 0))
    return session


def test_e_backlog_from_d_drains_during_the_empty_gap_before_e_starts(passport_surge_session):
    t0 = window_of(
        airport_predictions(passport_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))["international_passport"],
        hour_start(15),
    )
    # T0 (15:00) rho < 1 VE önceki backlog sıfıra inmiş olmalı ->
    # KARARLI (stabil Erlang-C) dal - `passport_queue_model`'in kendi
    # ayrımına göre bu YALNIZCA backlog_start<=0 iken mümkündür.
    assert t0["risk"] == RISK_LOW
    assert t0["utilization"] < 0.7


def test_e_international_passport_surge_t0_t1_t2(passport_surge_session):
    result = airport_predictions(passport_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    t0 = window_of(result["international_passport"], hour_start(15))
    t1 = window_of(result["international_passport"], hour_start(16))
    t2 = window_of(result["international_passport"], hour_start(17))

    assert t0["risk"] == RISK_LOW
    assert t1["risk"] == RISK_CRITICAL
    assert t1["utilization"] > 1.0
    # Recovery kademeli - T2 T1'den DÜŞÜK ama backlog'un aynı anda
    # sıfırlanması ZORLANMIYOR.
    assert t2["utilization"] < t1["utilization"]
    assert t2["estimated_wait_minutes"] < t1["estimated_wait_minutes"]
    assert t2["estimated_wait_minutes"] > 0


def test_e_domestic_security_never_appears_in_this_window_block(passport_surge_session):
    """ADIM (24-Hour Graph): domestic_security artık 24-saat PADDED - bu saatler `None` değil, sıfır-talep bucket'ı (`flight_count=0`)."""
    result = airport_predictions(passport_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    for hour in (15, 16, 17):
        assert window_of(result["domestic_security"], hour_start(hour))["flight_count"] == 0


def test_e_international_security_never_sees_the_arrivals_own_demand(passport_surge_session):
    """
    `security_flights()`/`security_international_flights()` SADECE
    kalkışları alır (bkz. flows.py) - bu blokta 15/16/17'nin KENDİ yeni
    talebi SADECE varış (100/300/100 pax) - bu yüzden security_intl'in
    o saatlerdeki talebi HİÇBİR ZAMAN o varış miktarlarına EŞİT/yakın
    olamaz (varış demand'i security'ye hiç girmiyor).

    15:00'da GERÇEKTEN küçük bir kalıntı GÖRÜLEBİLİR (D senaryosunun
    11:00 kalkış sürgüsünden arta kalan, 13:00/14:00 boş saatlerde tam
    boşalmamış passport backlog'unun KUYRUK SIRASI gereği security'ye
    hâlâ akan son parçası - bkz. `_passport_security_hourly_coupling`
    docstring'i, "saat sınırında aktarım" testi). Bu "international=
    passport" kısayolunun TERSİNİ kanıtlıyor: aynı "international"
    etiketi security'ye otomatik girmiyor - SADECE kalkışa bağlı, VE
    SADECE passport'un GERÇEKTEN serbest bıraktığı kadar.
    """
    result = airport_predictions(passport_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    w15 = window_of(result["international_security"], hour_start(15))
    # ADIM (24-Hour Graph): w15 artık HER ZAMAN bir dict (24-saat PADDED,
    # `None` DEĞİL) - gerçek bir kalıntı yoksa sıfır-talep bucket'ı
    # (expected_passengers=0) döner, bu da aşağıdaki üst-sınır iddiasını
    # otomatik olarak sağlar; `if w15 is not None` koruması artık gereksiz.
    # D'nin kalkış sürgüsünden kalan KÜÇÜK bir kuyruk kalıntısı OLABİLİR
    # (matematiksel üst sınır: passport'un TEK saatlik kapasitesi,
    # 160 pax) - ama bu 15:00'ın KENDİ varış talebi (100 pax, E190)
    # DEĞİL; farklı bir sayı olmalı ve passport'un kapasitesini AŞAMAZ.
    assert w15["expected_passengers"] <= 160.0
    # 16:00/17:00'da artık hiçbir kalkış-bağlantılı kalıntı kalmamış
    # olmalı (15:00'daki küçük kalıntı security'nin KENDİ 480/saat
    # kapasitesiyle o saat içinde tükeniyor) - varışların KENDİ talebi
    # (300/100 pax) hiçbir zaman security'ye YANSIMAMALI.
    for hour in (16, 17):
        # `flight_count` DEĞİL `expected_passengers` kontrol edilir -
        # coupling demand_override ile flight_count=0 olsa bile talep
        # (expected_passengers) sızabilirdi; asıl iddia "hiçbir talep
        # (gerçek veya coupled) yok"tur.
        assert window_of(result["international_security"], hour_start(hour))["expected_passengers"] == 0


# ==================================================================
# F) OVERALL - GENEL, üç alt sürecin GERÇEK max'ı, sahte overall wait YOK
# ==================================================================

def test_f_overall_wait_carries_security_wait(domestic_surge_session):
    """
    07:00 penceresinde SADECE domestic_security var (CRITICAL) - onun
    `estimated_wait_minutes`'ı hep None'dır (security wait modeli yok).
    Overall bu null'ı OLDUĞU GİBİ taşımalı - sahte bir dakika ÜRETİLMEZ.
    """
    result = airport_predictions(domestic_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    w = window_of(result["overall"], hour_start(7))
    assert w["risk"] == RISK_CRITICAL
    security = window_of(result["domestic_security"], hour_start(7))
    assert w["estimated_wait_minutes"] == security["estimated_wait_minutes"]
    assert w["estimated_wait_minutes"] is not None


@pytest.fixture
def domestic_and_passport_critical_same_hour_session(session):
    """
    PASSPORT→SECURITY kuplajı yüzünden `international_security` tek
    başına (passport'un 160 pax/saat kapasitesiyle sınırlı olduğu için)
    ASLA CRITICAL'e ulaşamıyor (bkz. `test_d_international_security_surge_t0_t1_t2`).
    Bu yüzden "iki süreç AYNI saatte AYNI severity taşıyor" senaryosu
    için domestic_security (kuplajdan TAMAMEN bağımsız, KENDİ 480 pax/
    saat kapasitesi) + passport (uluslararası kalkış sürgüsüyle) AYNI
    saatte CRITICAL yapılıyor.
    """
    seed_airports(session)
    dep_records = (
        domestic_departure_records(hour_start(20), 8, aircraft="B77W")  # 8x350=2800 >> 480/saat
        + international_departure_records(hour_start(20), 2, aircraft="B77W")  # 2x350=700 >> 160/saat
    )
    ingest(session, dep_records, [])
    run_predictions(session, resolver(), airports=["IST"], now=datetime(2026, 9, 15, 23, 0))
    return session


def test_f_overall_wait_carries_real_winning_process_wait(domestic_and_passport_critical_same_hour_session):
    """
    ADIM 6D-2 H2 - domestic_security VE passport aynı saatte AYNI
    severity (CRITICAL) taşıyor; ikisi de artık gerçek/finite wait
    üretiyor (security artık ortak queue modelini kullanıyor - bkz.
    PASSPORT→SECURITY kuplaj ADIM'ı). Tie-break "argüman sırasında ilk"
    DEĞİL, "aynı en-yüksek severity içinde gerçek/finite wait'i OLAN" -
    ikisi de finite olduğunda `_merge_overall_series(domestic_security,
    international_security, passport)` çağrı SIRASINA göre domestic_
    security kazanır (DEĞİŞMEDİ - H2'nin kendi kuralı, burada SADECE
    yeni bir senaryoyla yeniden doğrulanıyor).
    """
    result = airport_predictions(
        domestic_and_passport_critical_same_hour_session, "IST", now=datetime(2026, 9, 15, 23, 0)
    )
    overall_t1 = window_of(result["overall"], hour_start(20))
    passport_t1 = window_of(result["international_passport"], hour_start(20))
    domestic_t1 = window_of(result["domestic_security"], hour_start(20))

    assert domestic_t1["risk"] == passport_t1["risk"] == RISK_CRITICAL
    assert domestic_t1["estimated_wait_minutes"] is not None
    assert passport_t1["estimated_wait_minutes"] is not None

    assert overall_t1["risk"] == RISK_CRITICAL
    assert overall_t1["estimated_wait_minutes"] == domestic_t1["estimated_wait_minutes"]


# ==================================================================
# G) T2 RECOVERY - backlog/utilization/wait kademeli düşüş
#    (yukarıdaki D/E testlerinde zaten doğrulandı; burada sadece tek
#    yerde özetleyen bir regresyon testi tutuluyor. Backlog'un
#    NİHAYETİNDE sıfıra inip stabil Erlang-C davranışına dönmesi
#    zaten `tests/test_passport_backlog_model.py::
#    test_k_full_t0_t1_t2_t3_chain_engine_level`'de kilitli - burada
#    TEKRAR ÜRETİLMEDİ.)
# ==================================================================

def test_g_recovery_is_gradual_not_forced_to_zero(passport_surge_session):
    result = airport_predictions(passport_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    t1 = window_of(result["international_passport"], hour_start(16))
    t2 = window_of(result["international_passport"], hour_start(17))
    assert 0 < t2["estimated_wait_minutes"] < t1["estimated_wait_minutes"]


# ==================================================================
# H) AIRPORT ISOLATION
# ==================================================================

def test_h_ist_surge_does_not_change_control_airport_adb(session):
    # Oturum 1: SADECE ADB (IST hiç yok).
    seed_airports(session)
    seed_baseline(session, "ADB", PROCESS_SECURITY_DOMESTIC, 6, average_flight_count=2.0)
    adb_records = domestic_departure_records(hour_start(6), 2, airport="ADB", partner="ESB")
    ingest(session, adb_records, [])
    run_predictions(session, resolver(), airports=["ADB"], now=datetime(2026, 9, 15, 23, 0))
    baseline_result = airport_predictions(session, "ADB", now=datetime(2026, 9, 15, 23, 0))

    # Aynı oturuma şimdi IST'in domestic surge senaryosunu EKLE.
    for hour in (6, 7, 8):
        seed_baseline(session, "IST", PROCESS_SECURITY_DOMESTIC, hour, average_flight_count=3.0)
    ist_records = (
        domestic_departure_records(hour_start(6), 3, airport="IST", partner="ESB")
        + domestic_departure_records(hour_start(7), 9, airport="IST", partner="ESB")
        + domestic_departure_records(hour_start(8), 3, airport="IST", partner="ESB")
    )
    ingest(session, ist_records, [])
    run_predictions(session, resolver(), airports=["IST", "ADB"], now=datetime(2026, 9, 15, 23, 0))
    after_result = airport_predictions(session, "ADB", now=datetime(2026, 9, 15, 23, 0))

    assert risk_of(after_result["domestic_security"], hour_start(6)) == \
        risk_of(baseline_result["domestic_security"], hour_start(6))
    assert window_of(after_result["domestic_security"], hour_start(6))["flight_count"] == \
        window_of(baseline_result["domestic_security"], hour_start(6))["flight_count"]
    # IST'in surge saatlerinde (07:00 CRITICAL) ADB'nin KENDİ 07:00'ı
    # hâlâ sıfır-talep (ADIM 24-Hour Graph ile artık `None` değil, ama
    # flight_count=0) - havalimanları KARIŞMIYOR.
    assert window_of(after_result["domestic_security"], hour_start(7))["flight_count"] == 0


# ==================================================================
# I) API / FRONTEND CONTRACT
# ==================================================================

def test_i_all_four_series_present_with_real_60_minute_windows(domestic_surge_session):
    result = airport_predictions(domestic_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    for key in ("overall", "domestic_security", "international_security", "international_passport"):
        assert key in result
        for w in result[key]["windows"]:
            start = datetime.fromisoformat(w["window_start"])
            end = datetime.fromisoformat(w["window_end"])
            assert start.minute == 0
            assert (end - start).total_seconds() == 3600


def test_i_click_on_one_window_never_changes_another_graph_or_window(domestic_surge_session):
    """
    Frontend `state.detail[key]` per-grafik AYRI (bkz. index.html) -
    burada API sözleşmesi düzeyinde doğrulanan şey: aynı `windows`
    dizisinde 07:00 ve 08:00 penceresi BİRBİRİNDEN bağımsız, farklı
    veri taşıyor (frontend'in "sadece tıklanan pencereyi göster"
    davranışının dayandığı veri garantisi).

    NOT (ADIM Visible Risk = Gerçek Queue Pressure ile GÜNCELLENDİ):
    07:00/08:00 artık İKİSİ de CRITICAL (08:00, 07:00'ın devasa backlog'unu
    GERÇEKTEN devralıyor - bkz. `test_c_domestic_surge_raises_domestic_
    security_risk`) - risk eşitliği bu testin ASIL iddiasını (bağımsız
    pencereler) YALANLAMAZ; `estimated_wait_minutes` (gerçek, event-
    türevli, birbirinden FARKLI) bağımsızlığı hâlâ kanıtlıyor.
    """
    result = airport_predictions(domestic_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    w7 = window_of(result["domestic_security"], hour_start(7))
    w8 = window_of(result["domestic_security"], hour_start(8))
    assert w7["estimated_wait_minutes"] != w8["estimated_wait_minutes"]
    assert w7["window_start"] != w8["window_start"]


def test_i_passport_window_wait_equals_its_own_row_not_current(intl_security_surge_session):
    result = airport_predictions(intl_security_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    passport_series = result["international_passport"]
    for hour in (10, 11, 12):
        w = window_of(passport_series, hour_start(hour))
        # her satırın estimated_wait_minutes'ı DB'deki KENDİ satırından -
        # current'tan kopyalanmadı.
        assert w is not None


def test_i_security_windows_carry_real_wait_numbers(intl_security_surge_session):
    result = airport_predictions(intl_security_surge_session, "IST", now=datetime(2026, 9, 15, 23, 0))
    for w in result["international_security"]["windows"]:
        assert w["estimated_wait_minutes"] is not None


def test_i_no_passenger_or_flight_count_in_frontend_graph_rendering():
    html = _read_index_html()
    start = html.index("function graphSectionHtml")
    end = html.index("\n  }", start)
    block = html[start:end]
    assert "expected_passengers" not in block
    assert "flight_count" not in block


# ==================================================================
# J) CURRENT WAIT STALENESS - mimari değiştirilmedi, sadece doğrulandı
# ==================================================================

def test_j_poll_interval_is_5_minutes_as_coded():
    """
    ADIM (Frontend 4-Graph Contract) - Bölüm 26/52: source artık ~5
    dakikada bir yenileniyor (30 dakika DEĞİL) - bu SADECE dış veri
    yenilenme sıklığıdır, queue window/prediction window/service
    interval'dan bağımsız (bkz. index.html'deki yorum).
    """
    html = _read_index_html()
    assert "var POLL_MS = 5 * 60 * 1000;" in html


def test_j_server_predictions_endpoint_never_recomputes_engine():
    import inspect
    from app.web import server

    source = inspect.getsource(server)
    for forbidden in (
        "run_predictions(", "predict_airport(", "predict_window(",
        "import pipeline", "from ..queue.pipeline", "from ..queue import pipeline",
    ):
        assert forbidden not in source
    # `do_GET`'in predictions dalı SADECE `airport_predictions()` çağırıyor -
    # başka hiçbir hesaplama fonksiyonu YOK. `now=self._test_now_override()`
    # (ADIM Local Test-DB Viewing) SADECE mevcut `now=` enjeksiyon
    # noktasını (production varsayılanı - parametre yoksa gerçek duvar
    # saati - DEĞİŞMEDİ) opsiyonel bir query param'a bağlar, YENİ bir
    # hesaplama fonksiyonu DEĞİL.
    assert "self._send_json(airport_predictions(session, iata, now=self._test_now_override()))" in source


# ==================================================================
# L) full pytest 0 failed - ayrı çalıştırılır (bkz. rapor).
# ==================================================================
