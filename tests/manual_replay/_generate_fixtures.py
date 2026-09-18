"""
Bir kereye mahsus üretici script - `tests/manual_replay/*.json` fixture'larını
üretir. Gerçek `data/*.json` dosyalarındaki şemayı/wrapper yapısını AYNEN
taklit eder (bkz. rapor). Bu script deliverable DEĞİLDİR, sadece elle JSON
yazarken hesap hatası yapmamak için kullanıldı; tekrar çalıştırılabilir
(idempotent - aynı sabit veriden aynı dosyaları üretir).
"""
import json
import os
from datetime import datetime, timedelta, timezone

FIXTURE_DIR = os.path.dirname(__file__)
TZ_OFFSET = timedelta(hours=3)  # Europe/Istanbul, 18 Eylül 2026 DST kapsam dışı (kış saati farkı yok, sabit +3)


def local(h, m):
    """2026-09-18 IST yerel saatini UTC naive datetime'a çevirir."""
    return datetime(2026, 9, 18, h, m) - TZ_OFFSET


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None


def local_fmt(dt_utc):
    if dt_utc is None:
        return None
    local_dt = dt_utc + TZ_OFFSET
    return local_dt.strftime("%Y-%m-%d %H:%M")


def ts(dt):
    if dt is None:
        return None
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


AIRPORTS = {
    "IST": "LTFM", "ESB": "LTAC", "ADB": "LTBJ", "CDG": "LFPG",
    "FRA": "EDDF", "LHR": "EGLL", "DXB": "OMDB", "JFK": "KJFK",
}


def departure_record(
    airline_iata, flight_number, dep_iata, arr_iata,
    dep_sched, dep_est=None, dep_act=None,
    aircraft_icao=None, status="scheduled", duration=120,
):
    flight_iata = f"{airline_iata}{flight_number}"
    flight_icao = f"{AIRLINE_ICAO[airline_iata]}{flight_number}"
    delayed = None
    dep_delayed = None
    if dep_est is not None:
        dep_delayed = int((dep_est - dep_sched).total_seconds() // 60)
        delayed = dep_delayed if dep_delayed > 0 else None
    return {
        "airline_iata": airline_iata,
        "airline_icao": AIRLINE_ICAO[airline_iata],
        "flight_iata": flight_iata,
        "flight_icao": flight_icao,
        "flight_number": flight_number,
        "dep_iata": dep_iata,
        "dep_icao": AIRPORTS[dep_iata],
        "dep_terminal": "1" if dep_iata == "IST" else None,
        "dep_gate": None,
        "dep_time": local_fmt(dep_sched),
        "dep_time_utc": fmt(dep_sched),
        "dep_estimated": local_fmt(dep_est),
        "dep_estimated_utc": fmt(dep_est),
        "dep_actual": local_fmt(dep_act),
        "dep_actual_utc": fmt(dep_act),
        "arr_iata": arr_iata,
        "arr_icao": AIRPORTS[arr_iata],
        "arr_terminal": None,
        "arr_gate": None,
        "arr_baggage": None,
        "arr_time": local_fmt(dep_sched + timedelta(minutes=duration)),
        "arr_time_utc": fmt(dep_sched + timedelta(minutes=duration)),
        "arr_estimated": None,
        "arr_estimated_utc": None,
        "arr_actual": None,
        "arr_actual_utc": None,
        "cs_airline_iata": None,
        "cs_flight_number": None,
        "cs_flight_iata": None,
        "status": status,
        "duration": duration,
        "delayed": delayed,
        "dep_delayed": dep_delayed,
        "arr_delayed": None,
        "aircraft_icao": aircraft_icao,
        "dep_time_ts": ts(dep_sched),
        "dep_estimated_ts": ts(dep_est),
        "dep_actual_ts": ts(dep_act),
        "arr_time_ts": ts(dep_sched + timedelta(minutes=duration)),
    }


def arrival_record(
    airline_iata, flight_number, dep_iata, arr_iata,
    arr_sched, arr_est=None, arr_act=None,
    aircraft_icao=None, status="scheduled", duration=180,
):
    flight_iata = f"{airline_iata}{flight_number}"
    flight_icao = f"{AIRLINE_ICAO[airline_iata]}{flight_number}"
    delayed = None
    arr_delayed = None
    if arr_est is not None:
        arr_delayed = int((arr_est - arr_sched).total_seconds() // 60)
        delayed = arr_delayed if arr_delayed > 0 else None
    dep_sched = arr_sched - timedelta(minutes=duration)
    return {
        "airline_iata": airline_iata,
        "airline_icao": AIRLINE_ICAO[airline_iata],
        "flight_iata": flight_iata,
        "flight_icao": flight_icao,
        "flight_number": flight_number,
        "dep_iata": dep_iata,
        "dep_icao": AIRPORTS[dep_iata],
        "dep_terminal": None,
        "dep_gate": None,
        "dep_time": local_fmt(dep_sched),
        "dep_time_utc": fmt(dep_sched),
        "dep_estimated": None,
        "dep_estimated_utc": None,
        "dep_actual": None,
        "dep_actual_utc": None,
        "arr_iata": arr_iata,
        "arr_icao": AIRPORTS[arr_iata],
        "arr_terminal": "1" if arr_iata == "IST" else None,
        "arr_gate": None,
        "arr_baggage": "12" if arr_iata == "IST" else None,
        "arr_time": local_fmt(arr_sched),
        "arr_time_utc": fmt(arr_sched),
        "arr_estimated": local_fmt(arr_est),
        "arr_estimated_utc": fmt(arr_est),
        "arr_actual": local_fmt(arr_act),
        "arr_actual_utc": fmt(arr_act),
        "cs_airline_iata": None,
        "cs_flight_number": None,
        "cs_flight_iata": None,
        "status": status,
        "duration": duration,
        "delayed": delayed,
        "dep_delayed": None,
        "arr_delayed": arr_delayed,
        "aircraft_icao": aircraft_icao,
        "dep_time_ts": ts(dep_sched),
        "arr_time_ts": ts(arr_sched),
        "arr_estimated_ts": ts(arr_est),
        "arr_actual_ts": ts(arr_act),
    }


AIRLINE_ICAO = {
    "TK": "THY", "PC": "PGT", "SV": "SVA",
}


# ---------------------------------------------------------------------
# DEPARTURES (IST = dep_iata) - 14 kayıt
# ---------------------------------------------------------------------
departures = [
    # Aynı saate yığılmış domestic (09:00-09:05 yerel) - normal uçaklar.
    departure_record("TK", "101", "IST", "ESB", local(9, 0), aircraft_icao="A320", duration=70),
    departure_record("TK", "102", "IST", "ADB", local(9, 0), aircraft_icao="A21N", duration=75),
    departure_record("PC", "103", "IST", "ESB", local(9, 5), aircraft_icao="B738", duration=70),

    # actual zamanlı domestic (gerçekleşmiş uçuş).
    departure_record(
        "TK", "209", "IST", "ESB", local(10, 0),
        dep_act=local(10, 12), aircraft_icao="A320", status="landed", duration=70,
    ),
    # estimated zamanlı domestic (henüz gerçekleşmemiş, tahmini kaymış).
    departure_record(
        "TK", "210", "IST", "ADB", local(10, 30),
        dep_est=local(10, 45), aircraft_icao="B738", status="active", duration=75,
    ),

    # Aynı saatli (12:00-12:20 yerel) international departure DALGASI -
    # passport havuzunu doyurup security_intl yükünü sonraki saatlere
    # taşıyacak yoğunluk (475+365+150+220+162 = 1372 yolcu ~20dk içinde).
    departure_record("TK", "201", "IST", "CDG", local(12, 0), aircraft_icao="A359", duration=240),
    departure_record("TK", "202", "IST", "FRA", local(12, 5), aircraft_icao="B77W", duration=200),
    departure_record("TK", "203", "IST", "LHR", local(12, 10), aircraft_icao="A320", duration=250),
    departure_record("TK", "204", "IST", "DXB", local(12, 15), aircraft_icao="A21N", duration=280),
    # aircraft_icao NULL (Source A) - Source B enrichment ile çözülecek.
    departure_record("SV", "205", "IST", "JFK", local(12, 20), aircraft_icao=None, duration=600),

    # Bilinmeyen ICAO -> 180 fallback.
    departure_record("TK", "206", "IST", "DXB", local(13, 0), aircraft_icao="ZZZZ", duration=280),

    # Gecikmeli international departure.
    departure_record(
        "TK", "207", "IST", "CDG", local(14, 0),
        dep_est=local(15, 10), aircraft_icao="A320", status="active", duration=240,
    ),
    # Cancelled international departure.
    departure_record(
        "TK", "208", "IST", "FRA", local(14, 30),
        aircraft_icao="A320", status="cancelled", duration=200,
    ),

    # Gece yarısına yakın departure (yerel 23:50).
    departure_record("TK", "211", "IST", "LHR", local(23, 50), aircraft_icao="A321", duration=250),
]

# ---------------------------------------------------------------------
# ARRIVALS (IST = arr_iata) - 6 kayıt
# ---------------------------------------------------------------------
arrivals = [
    # Aynı saatli (08:00-08:05 yerel) international arrival - yüksek kapasiteli.
    arrival_record("TK", "301", "CDG", "IST", local(8, 0), aircraft_icao="A359", duration=240),
    arrival_record("TK", "302", "FRA", "IST", local(8, 5), aircraft_icao="B77W", duration=200),

    # Domestic arrival - HİÇBİR queue/graph beslemez (flows.py).
    arrival_record("TK", "303", "ESB", "IST", local(9, 0), aircraft_icao="A320", duration=70),

    # Gecikmeli international arrival.
    arrival_record(
        "TK", "305", "DXB", "IST", local(16, 0),
        arr_est=local(17, 20), aircraft_icao="B738", status="active", duration=280,
    ),
    # Cancelled international arrival.
    arrival_record(
        "TK", "306", "JFK", "IST", local(18, 0),
        aircraft_icao="A320", status="cancelled", duration=600,
    ),

    # Gece yarısına yakın international arrival (yerel 23:55) - +15dk
    # buffer ile effective_time YEREL 00:10 (19 Eylül) - flight'ın kendi
    # operasyonel günü (18 Eylül, flight_reference_time ile) ile queue
    # event timestamp'i (offset sonrası) arasındaki AYRIMIN kanıtı.
    arrival_record("TK", "304", "LHR", "IST", local(23, 55), aircraft_icao="A21N", duration=250),
]

departures_payload = {
    "request": {
        "lang": "en", "currency": "USD", "time": 12, "id": "manualreplay0918",
        "server": "a", "host": "airlabs.co", "pid": 0,
        "key": {
            "id": 0, "api_key": "REDACTED_TEST_FIXTURE_KEY", "type": "free",
            "expired": "2026-12-31T00:00:00.000Z", "registered": "2026-01-01T00:00:00.000Z",
            "upgraded": None, "limits_by_hour": 2500, "limits_by_minute": 250,
            "limits_by_month": 1000, "limits_total": 994,
        },
        "params": {"type": "departures", "lang": "en"},
        "version": 9, "method": "delays",
        "client": {
            "ip": "0.0.0.0",
            "geo": {
                "country_code": "TR", "country": "Turkey", "continent": "Asia",
                "city": "Istanbul", "lat": 41.0652, "lng": 28.9898,
                "timezone": "Europe/Istanbul",
            },
            "connection": {"type": "cable/dsl", "isp_code": 0, "isp_name": "manual-replay-fixture"},
            "device": {}, "agent": {},
            "karma": {"is_blocked": False, "is_crawler": False, "is_bot": False, "is_friend": False, "is_regular": True},
        },
        "has_more": False, "total_items": len(departures),
    },
    "response": departures,
    "terms": (
        "MANUAL REPLAY TEST FIXTURE - gercek AirLabs verisi DEGILDIR. "
        "genel-proje.md Bolum 36/47 replay senaryosu icin uretildi."
    ),
}

arrivals_payload = {
    "request": dict(departures_payload["request"], params={"type": "arrivals", "lang": "en"}, total_items=len(arrivals)),
    "response": arrivals,
    "terms": departures_payload["terms"],
}


# ---------------------------------------------------------------------
# SOURCE B (`flights` endpoint - canlı ADS-B, aircraft_icao enrichment)
# ---------------------------------------------------------------------
def adsb_record(flight_number, airline_iata, dep_iata, arr_iata, aircraft_icao, updated_utc, flight_iata=True):
    flight_icao = f"{AIRLINE_ICAO[airline_iata]}{flight_number}"
    row = {
        "hex": f"4B{int(flight_number):04X}",
        "reg_number": f"TC-{flight_number}",
        "flag": "TR",
        "lat": 41.0 if dep_iata == "IST" else 48.0,
        "lng": 29.0 if dep_iata == "IST" else 2.0,
        "alt": 3200,
        "dir": 270.0,
        "speed": 320,
        "v_speed": 5,
        "squawk": "1000",
        "flight_number": flight_number,
        "flight_icao": flight_icao,
        "dep_icao": AIRPORTS[dep_iata],
        "dep_iata": dep_iata,
        "arr_icao": AIRPORTS[arr_iata],
        "arr_iata": arr_iata,
        "airline_icao": AIRLINE_ICAO[airline_iata],
        "airline_iata": airline_iata,
        "aircraft_icao": aircraft_icao,
        "updated": ts(updated_utc),
        "status": "en-route",
        "type": "adsb",
    }
    if flight_iata:
        row["flight_iata"] = f"{airline_iata}{flight_number}"
    return row


source_b_records = [
    # SV205'in (Source A'da aircraft_icao=None) enrichment eşleşmesi -
    # AYNI flight_iata/flight_icao, `updated` 2026-09-18 (UTC) içinde.
    adsb_record("205", "SV", "IST", "JFK", "B738", local(12, 10)),
    # İlgisiz "canlı feed gürültüsü" - eşleşme ARANMAZ, sadece gerçekçi
    # şema/hacim için (gerçek response-delays.json'da da IST'le
    # ilgisiz binlerce kayıt var).
    adsb_record("637", "PC", "ADB", "ESB", "A320", local(9, 3), flight_iata=False),
    adsb_record("1294", "TK", "CDG", "IST", "A359", local(7, 40)),
]

source_b_payload = {
    "request": {
        "lang": "en", "currency": "USD", "time": 8, "id": "manualreplay0918b",
        "server": "a", "host": "airlabs.co", "pid": 0,
        "key": {
            "id": 0, "api_key": "REDACTED_TEST_FIXTURE_KEY", "type": "free",
            "expired": "2026-12-31T00:00:00.000Z", "registered": "2026-01-01T00:00:00.000Z",
            "upgraded": None, "limits_by_hour": 1000, "limits_by_minute": 100,
            "limits_by_month": 5000, "limits_total": 994,
        },
        "params": {"lang": "en"},
        "version": 9, "method": "flights",
        "has_more": False, "total_items": len(source_b_records),
    },
    "response": source_b_records,
    "terms": departures_payload["terms"],
}


def write(name, payload):
    path = os.path.join(FIXTURE_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("wrote", path, "records=", len(payload["response"]))


if __name__ == "__main__":
    write("Delays - Type Departures.json", departures_payload)
    write("Delays - Type Arrivals.json", arrivals_payload)
    write("flights_live.json", source_b_payload)
