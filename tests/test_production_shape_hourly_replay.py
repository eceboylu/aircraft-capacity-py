"""Ten-hour production-shape replay in an isolated SQLite database."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.queue.api import airport_predictions
from app.queue.constants import PROCESS_PASSPORT, PROCESS_SECURITY
from app.queue.engine import run_predictions
from app.queue.ingestion.airports_import import country_lookup
from app.queue.ingestion.refresh import refresh_flights
from app.queue.ingestion.sources import build_aircraft_index, parse_source_a
from app.queue.models import Airport, QueuePrediction
from app.seed import seed_curated_fallback, seed_family_and_ga, seed_verified_dataset
from app.service import AircraftCapacityService


DAY = datetime(2026, 9, 15)


def _fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _source_records():
    """Source A + Source B records with the real feed's flat field names."""
    # Each tuple: queue hour, international ICAOs, domestic ICAOs.
    plan = [
        (6, ["CRJ9"], []),
        (7, ["CRJ9", "CRJ9"], []),
        (8, ["CRJ9"], []),
        (9, ["A320", "A320", "A320"], []),
        (10, ["CRJ9"], []),
        (11, ["CRJ9"], ["A320", "A320", "A320", "A320"]),
        (12, ["CRJ9"], []),
        (13, ["CRJ9"], []),
        (14, ["A320", "A320"], []),
        (15, ["CRJ9"], []),
    ]
    source_a = []
    source_b = []
    sequence = 0
    for hour, international, domestic in plan:
        for location, aircraft_codes in (
            ("international", international), ("domestic", domestic)
        ):
            for aircraft in aircraft_codes:
                sequence += 1
                queue_start = DAY.replace(hour=hour)
                duration = 210 if location == "international" else 70
                # ADIM (Airport Queue Model V2 - sabit -120dk offset):
                # departure passenger arrival artık lokasyon/süreden
                # bağımsız SABİT 120 dakikadır (eskiden intl=60dk,
                # domestic=45dk dinamikti) - departure zamanı buna göre
                # geriye doğru kurulur (+5dk pay ile queue_start'ın hemen
                # içine düşecek şekilde), her iki lokasyon için de AYNI.
                departure = queue_start + timedelta(minutes=120 + 5)
                arrival = departure + timedelta(minutes=duration)
                flight_iata = f"TK{sequence}"
                destination = "CDG" if location == "international" else "ESB"
                source_a.append({
                    "airline_iata": "TK",
                    "airline_icao": "THY",
                    "flight_iata": flight_iata,
                    "flight_icao": f"THY{sequence}",
                    "flight_number": str(sequence),
                    "dep_iata": "IST",
                    "dep_icao": "LTFM",
                    "dep_time_utc": _fmt(departure),
                    "dep_estimated_utc": None,
                    "dep_actual_utc": None,
                    "arr_iata": destination,
                    "arr_icao": "LFPG" if destination == "CDG" else "LTAC",
                    "arr_time_utc": _fmt(arrival),
                    "arr_estimated_utc": None,
                    "arr_actual_utc": None,
                    "status": "scheduled",
                    "duration": duration,
                    "aircraft_icao": None,
                })
                source_b.append({
                    "hex": f"4B{sequence:04X}",
                    "reg_number": f"TC{sequence:03d}",
                    "flag": "TR",
                    "lat": 41.0,
                    "lng": 29.0,
                    "flight_number": str(sequence),
                    "flight_iata": flight_iata,
                    "flight_icao": f"THY{sequence}",
                    "dep_iata": "IST",
                    "dep_icao": "LTFM",
                    "arr_iata": destination,
                    "arr_icao": "LFPG" if destination == "CDG" else "LTAC",
                    "airline_icao": "THY",
                    "aircraft_icao": aircraft,
                    "updated": int(departure.replace(
                        tzinfo=timezone.utc
                    ).timestamp()),
                    "status": "en-route",
                    "type": "aircraft",
                })
    return plan, source_a, source_b


def run_production_shape_replay(database_path: Path) -> dict:
    isolated_engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(isolated_engine)
    session = sessionmaker(bind=isolated_engine)()
    try:
        for iata, icao, name, country in (
            ("IST", "LTFM", "Istanbul Airport", "TR"),
            ("ESB", "LTAC", "Ankara Esenboga Airport", "TR"),
            ("CDG", "LFPG", "Paris Charles de Gaulle Airport", "FR"),
        ):
            session.add(Airport(
                iata_code=iata, icao_code=icao, airport_name=name,
                city_code=iata, country_code=country, timezone="UTC",
            ))
        session.commit()

        seed_verified_dataset(session)
        seed_curated_fallback(session)
        seed_family_and_ga(session)
        resolver = AircraftCapacityService(session)

        plan, source_a, source_b = _source_records()
        parsed = parse_source_a(
            source_a,
            "departure",
            country_lookup(session),
            build_aircraft_index(source_b),
        )
        refresh = refresh_flights(session, parsed)
        prediction_summary = run_predictions(
            session,
            resolver,
            airports=["IST"],
            update_baseline=False,
            now=DAY.replace(hour=23),
        )

        predictions = session.execute(
            select(QueuePrediction).where(
                QueuePrediction.airport_iata == "IST",
                QueuePrediction.process.in_((PROCESS_PASSPORT, PROCESS_SECURITY)),
            ).order_by(QueuePrediction.process, QueuePrediction.window_start)
        ).scalars().all()
        by_key = {(row.process, row.window_start.hour): row for row in predictions}

        passport_backlog = 0.0
        security_backlog = 0.0
        table = []
        for hour, _, _ in plan:
            passport = by_key[(PROCESS_PASSPORT, hour)]
            security = by_key[(PROCESS_SECURITY, hour)]
            passport_start = passport_backlog
            security_start = security_backlog
            passport_backlog = max(
                0.0, passport_start + passport.expected_passengers - 320.0
            )
            security_backlog = max(
                0.0, security_start + security.expected_passengers - 480.0
            )
            table.append({
                "hour": f"{hour:02d}:00",
                "passport_demand": passport.expected_passengers,
                "passport_rho": passport.utilization,
                "passport_backlog_start": passport_start,
                "passport_backlog_end": passport_backlog,
                "passport_wait": passport.estimated_wait_minutes,
                "passport_risk": passport.risk,
                "security_demand": security.expected_passengers,
                "security_rho": security.utilization,
                "security_backlog_start": security_start,
                "security_backlog_end": security_backlog,
                "security_wait": security.estimated_wait_minutes,
                "security_risk": security.risk,
            })

        api = airport_predictions(session, "IST", now=DAY.replace(hour=23))
        return {
            "source_a_count": len(source_a),
            "source_b_count": len(source_b),
            "parsed_count": len(parsed),
            "refresh": refresh,
            "prediction_summary": prediction_summary,
            "table": table,
            "api": api,
            # Neither actual feed shape carries a transfer/cohort marker.
            "tandem_status": "NOT DETERMINABLE",
            "minute_trace": None,
            "source_keys": {
                "a": sorted(source_a[0]),
                "b": sorted(source_b[0]),
            },
        }
    finally:
        session.close()
        isolated_engine.dispose()


def test_ten_hour_production_shape_replay(tmp_path):
    report = run_production_shape_replay(tmp_path / "hourly-replay.sqlite")
    assert report["parsed_count"] == report["source_a_count"]
    assert report["refresh"]["inserted"] == report["parsed_count"]
    assert len(report["table"]) == 10
    assert [row["hour"] for row in report["table"]] == [
        f"{hour:02d}:00" for hour in range(6, 16)
    ]

    # ADIM (Departure Show-Up Profile): passport talebi artık her
    # flight'ın KENDİ departure saatinden ÖNCEKİ 3 saate (show-up ile)
    # yayılıyor - bu yüzden 06:00-15:00 penceresindeki her saat ARTIK
    # SADECE o saatin KENDİ flight'ının değil, komşu saatlerin flight'
    # larının da (show-up ile taşan) PAYINI taşıyor - eski "tek saat =
    # tek flight'ın TAM talebi" model DEĞİŞTİ. Aşağıdaki değerler gerçek
    # `predict_airport()` çıktısından alınmıştır (körlemesine seçilmedi) -
    # bkz. rapor.
    assert [row["passport_demand"] for row in report["table"]] == [
        90, 144, 180, 306, 162, 90, 90, 132, 216, 114,
    ]
    # ADIM (Event-Driven Engine Entegrasyonu): security artık uluslararası
    # kalkış talebini KENDİ effective_time'ında değil, passport'un GERÇEK
    # discrete-event simülasyonundan (core/event_queue.py) çıkan completion
    # timestamp'leriyle görüyor (bkz. engine.py:_event_driven_queue_demand).
    # ADIM (Departure Show-Up Profile) ile passport'un GİRDİSİ de artık
    # show-up ile yayılmış olduğu için security'nin bu penceredeki
    # toplamı da değişti - saat sınırı aktarımının kanıtı (Bölüm 9D)
    # aynen geçerli, SADECE mutlak sayılar değişti.
    assert [row["security_demand"] for row in report["table"]] == [
        90, 144, 177, 282, 309, 450, 210, 132, 211, 119,
    ]
    # ADIM (Departure Show-Up Profile): bu tablo SADECE 06:00-15:00
    # penceresini kapsıyor - flight'ların show-up'ı bu pencerenin
    # SINIRLARININ dışına (05:xx'ten önce/16:00'dan sonra) da taşabilir;
    # bu yüzden bu PARTIAL pencerenin toplamı artık ORİJİNAL 2160'IN
    # TAMAMI DEĞİL (hiçbir yolcu kaybolmadı - sadece bu 10 saatlik
    # tabloya YAKALANMAYAN bir kısmı var, tablo dışı saatlerde).
    assert sum(row["security_demand"] for row in report["table"]) == 2124
    assert all(row["security_wait"] is not None for row in report["table"])
    assert all(row["passport_wait"] is not None for row in report["table"])
    # ADIM (Departure Show-Up Profile): show-up'ın demand'i daha yumuşak
    # dağıtması nedeniyle bu SPESİFİK senaryoda artık hiçbir saat
    # kapasiteyi aşıp bir SONRAKİ saate backlog BIRAKMIYOR (eski
    # senaryonun "130/210 backlog" bulgusu bu smoothing'in DOĞRUDAN
    # sonucu) - backlog formülünün KENDİSİ değişmedi (bkz. test_passport_
    # security_coupling.py'nin AYRI, KASITLI-overload senaryoları hâlâ
    # backlog üretiyor), SADECE bu senaryonun girdi profili artık
    # kapasiteyi aşmıyor.
    assert report["table"][3]["passport_backlog_end"] == 0.0
    assert report["table"][5]["security_backlog_end"] == 0.0

    for section in ("domestic_security", "international_security"):
        assert all(
            window["estimated_wait_minutes"] is not None
            for window in report["api"][section]["windows"]
        )

    frontend = (
        Path(__file__).parents[1] / "app" / "web" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert "Tahmini bekleme süresi:" in frontend
    assert 'return "Hesaplanamıyor";' in frontend

    assert not ({"transfer", "cohort", "passenger_type"} & set(
        report["source_keys"]["a"] + report["source_keys"]["b"]
    ))
    assert report["tandem_status"] == "NOT DETERMINABLE"
    assert report["minute_trace"] is None

