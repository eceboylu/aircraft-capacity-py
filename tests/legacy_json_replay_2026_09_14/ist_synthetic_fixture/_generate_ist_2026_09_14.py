"""
ADIM (IST 2026-09-14 Test Fixture) - bu betik SADECE
`tests/legacy_json_replay_2026_09_14/legacy_replay.sqlite` icindeki IST
verisini uretmek icin kullanilir - `data/*.json` production dosyalarina
HIC dokunmaz, sadece bu klasore yazar.

BU FIXTURE TAMAMEN SYNTHETIC/DERIVED'DIR - gercek IST 21.09.2026 tarifesi
DEGILDIR, kullaniciin verdigi test-profili hedeflerine (saatlik departure/
arrival araligi, 10:00-11:00 icin KESIN 40 departure/35 arrival, intl/dom
karisim orani) uyacak sekilde URETILMISTIR. Hicbir yolcu sayisi hard-code
EDILMEDI - AircraftCapacityService gercek uçak tipi -> kapasite
cozumlemesini KENDISI yapar (bu betik sadece gercek, production resolver'in
TANIDIGI ICAO kodlarini atar: A320/A20N/A21N/B738 agirlikli + B77W/B789/
A333/A359 widebody - hepsi `data/yolcu_ucaklari.json` + `curated_fallback.
json`'da DOGRULANDI).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

OUT_DIR = Path(__file__).resolve().parent
DATE = datetime(2026, 9, 14)
TZ = ZoneInfo("Europe/Istanbul")
UTC = ZoneInfo("UTC")

# Narrowbody agirlikli, bir miktar widebody - hepsi production resolver'in
# TANIDIGI gercek ICAO kodlari (verified_dataset/curated_fallback'te
# dogrulandi).
MIX = [
    ("A320", 0.27), ("A20N", 0.27), ("A21N", 0.12), ("B738", 0.19),
    ("B77W", 0.05), ("B789", 0.04), ("A333", 0.03), ("A359", 0.03),
]

DOMESTIC_PARTNERS = ["ESB", "ADB", "AYT", "TZX", "GZT", "ADA"]
INTL_PARTNERS = ["LHR", "CDG", "FRA", "DXB", "JFK", "AMS"]
DOM_AIRLINES = ["TK", "PC", "XQ"]
INTL_AIRLINES = ["TK", "BA", "AF", "LH", "EK", "DL"]

# Kullanicinin verdigi test-profili (saatlik departure hedef araligi) -
# her saat icin secilen SABIT deger bu aralik icinde. Saat 10 KESIN 40
# (kullanici talebi).
DEP_HOURLY_TARGET = {
    0: 18, 1: 22, 2: 16, 3: 24, 4: 20,
    5: 32, 6: 38, 7: 34, 8: 36,
    9: 42, 10: 40, 11: 45, 12: 38,
    13: 32, 14: 38, 15: 34, 16: 30,
    17: 40, 18: 44, 19: 38, 20: 42, 21: 36,
    22: 28, 23: 32,
}
# Saat 10 icin KESIN intl/dom kirilimi (kullanici: 30-32 intl, 8-10 dom).
DEP_HOUR10_INTL = 31
DEP_HOUR10_DOM = 9
assert DEP_HOUR10_INTL + DEP_HOUR10_DOM == DEP_HOURLY_TARGET[10] == 40

# Arrival - "benzer yogunlukta", saat 10 KESIN 35 (kullanici talebi).
ARR_HOURLY_TARGET = {
    0: 16, 1: 20, 2: 14, 3: 18, 4: 22,
    5: 28, 6: 34, 7: 30, 8: 32,
    9: 36, 10: 35, 11: 38, 12: 34,
    13: 30, 14: 32, 15: 28, 16: 26,
    17: 34, 18: 38, 19: 32, 20: 36, 21: 30,
    22: 24, 23: 20,
}

DEP_TOTAL = sum(DEP_HOURLY_TARGET.values())
ARR_TOTAL = sum(ARR_HOURLY_TARGET.values())

# Genel intl/dom orani (~%77 intl / %23 dom) - kullanicinin "departures'ta
# yaklasik %75-80 international, %20-25 domestic" istegine uygun; saat 10
# haricindeki her saat icin bu oran uygulanir (saat 10 yukarida SABIT).
DEP_INTL_RATIO = 0.775
ARR_INTL_RATIO = 0.75

# Kac ornegin CANCELED isaretlenecegi - saat 10 HARICINDE (o saatin KESIN
# 40/35 sayisini bozmamak icin), boylece rapor edilen KESIN sayilar
# muglaklasmaz.
CANCELLED_DEP_HOURS = [3, 15, 20]
CANCELLED_ARR_HOURS = [4, 18]


def _aircraft_cycle(n: int) -> list[str]:
    """Agirliga orantili + round-robin interleave (ayni tip ardisik yigilmaz)."""
    counts = {}
    remainders = []
    total_w = sum(w for _, w in MIX)
    for icao, w in MIX:
        exact = n * w / total_w
        counts[icao] = int(exact)
        remainders.append((icao, exact - int(exact)))
    remaining = n - sum(counts.values())
    remainders.sort(key=lambda t: -t[1])
    for icao, _ in remainders[:remaining]:
        counts[icao] += 1
    buckets = {icao: [icao] * c for icao, c in counts.items() if c > 0}
    keys = list(buckets.keys())
    out = []
    i = 0
    while any(buckets[k] for k in keys):
        k = keys[i % len(keys)]
        if buckets[k]:
            out.append(buckets[k].pop())
        i += 1
    return out


def _minutes_for(count: int) -> list[int]:
    """
    Saat icinde 60 dakikaya esit-araliklarla dagitir (hicbir flight ayni
    dakikaya YIGILMAZ) - kullanicinin "5-15 dakikalik araliklarla dagit"
    istegi, dusuk-yogunluklu saatlerde (<=12 flight/saat) BIREBIR 5-15dk
    araliga denk gelecek sekilde uygulanir; yuksek-yogunluklu saatlerde
    (40+ flight/saat, kullanicinin KENDI SABIT saat-10 hedefi) 60 dakikaya
    esit dagilim matematiksel olarak ZORUNLU minimum araliktir (40 flight
    5-15dk araliklarla en az 195-585 dakika gerektirir, 60dk'ya sigmaz) -
    bu durumda esit-arlikli dagitim EN YAKIN gercekci yaklasimdir (gercek
    havalimanlarinda payar pist/gate'lerden es zamanli kalkis/inis
    mumkun).
    """
    if count <= 1:
        return [0] * count
    step = 60 / count
    return sorted(int(i * step) % 60 for i in range(count))


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _to_utc(local_dt: datetime) -> datetime:
    return local_dt.replace(tzinfo=TZ).astimezone(UTC).replace(tzinfo=None)


def build_departures() -> list[dict]:
    aircraft_pool = _aircraft_cycle(DEP_TOTAL)
    idx = 0
    flight_num = 4000
    records = []
    for hour in range(24):
        count = DEP_HOURLY_TARGET[hour]
        minutes = _minutes_for(count)
        if hour == 10:
            n_intl = DEP_HOUR10_INTL
        else:
            n_intl = round(count * DEP_INTL_RATIO)
        n_dom = count - n_intl

        cancel_at = set()
        if hour in CANCELLED_DEP_HOURS:
            cancel_at = {0, count // 2}

        for i, minute in enumerate(minutes):
            is_intl = i < n_intl
            local_dt = datetime(DATE.year, DATE.month, DATE.day, hour, minute)
            dep_utc = _to_utc(local_dt)
            if is_intl:
                partner = INTL_PARTNERS[flight_num % len(INTL_PARTNERS)]
                airline = INTL_AIRLINES[flight_num % len(INTL_AIRLINES)]
                duration = timedelta(hours=4)
            else:
                partner = DOMESTIC_PARTNERS[flight_num % len(DOMESTIC_PARTNERS)]
                airline = DOM_AIRLINES[flight_num % len(DOM_AIRLINES)]
                duration = timedelta(hours=1, minutes=15)

            aircraft = aircraft_pool[idx]
            idx += 1
            arr_utc = dep_utc + duration
            flight_num += 1
            status = "cancelled" if i in cancel_at else "scheduled"

            records.append({
                "airline_iata": airline, "flight_iata": f"{airline}{flight_num}",
                "flight_number": str(flight_num),
                "dep_iata": "IST", "arr_iata": partner,
                "dep_terminal": "1", "dep_gate": None,
                "dep_time": _fmt(local_dt), "dep_time_utc": _fmt(dep_utc),
                "dep_estimated": _fmt(local_dt), "dep_estimated_utc": _fmt(dep_utc),
                "dep_actual": _fmt(local_dt), "dep_actual_utc": _fmt(dep_utc),
                "arr_time": _fmt(arr_utc), "arr_time_utc": _fmt(arr_utc),
                "arr_estimated": _fmt(arr_utc), "arr_estimated_utc": _fmt(arr_utc),
                "status": status,
                "location": "international" if is_intl else "domestic",
                "aircraft_icao": aircraft,
            })
    return records


def build_arrivals() -> list[dict]:
    aircraft_pool = _aircraft_cycle(ARR_TOTAL)
    idx = 0
    flight_num = 8000
    records = []
    for hour in range(24):
        count = ARR_HOURLY_TARGET[hour]
        minutes = _minutes_for(count)
        n_intl = round(count * ARR_INTL_RATIO)

        cancel_at = set()
        if hour in CANCELLED_ARR_HOURS:
            cancel_at = {0}

        for i, minute in enumerate(minutes):
            is_intl = i < n_intl
            local_dt = datetime(DATE.year, DATE.month, DATE.day, hour, minute)
            arr_utc = _to_utc(local_dt)
            if is_intl:
                partner = INTL_PARTNERS[flight_num % len(INTL_PARTNERS)]
                airline = INTL_AIRLINES[flight_num % len(INTL_AIRLINES)]
                duration = timedelta(hours=4)
            else:
                partner = DOMESTIC_PARTNERS[flight_num % len(DOMESTIC_PARTNERS)]
                airline = DOM_AIRLINES[flight_num % len(DOM_AIRLINES)]
                duration = timedelta(hours=1, minutes=15)

            aircraft = aircraft_pool[idx]
            idx += 1
            dep_utc = arr_utc - duration
            flight_num += 1
            status = "cancelled" if i in cancel_at else "scheduled"

            records.append({
                "airline_iata": airline, "flight_iata": f"{airline}{flight_num}",
                "flight_number": str(flight_num),
                "dep_iata": partner, "arr_iata": "IST",
                "arr_terminal": "1", "arr_gate": None, "arr_baggage": "1",
                "dep_time": _fmt(dep_utc), "dep_time_utc": _fmt(dep_utc),
                "dep_estimated": _fmt(dep_utc), "dep_estimated_utc": _fmt(dep_utc),
                "arr_time": _fmt(local_dt), "arr_time_utc": _fmt(arr_utc),
                "arr_estimated": _fmt(local_dt), "arr_estimated_utc": _fmt(arr_utc),
                "arr_actual": _fmt(local_dt), "arr_actual_utc": _fmt(arr_utc),
                "status": status,
                "location": "international" if is_intl else "domestic",
                "aircraft_icao": aircraft,
            })
    return records


def main() -> None:
    deps = build_departures()
    arrs = build_arrivals()

    hour10_dep = [r for r in deps if r["dep_time"][11:13] == "10" and r["status"] != "cancelled"]
    hour10_arr = [r for r in arrs if r["arr_time"][11:13] == "10" and r["status"] != "cancelled"]
    print(f"Hour 10 departures: {len(hour10_dep)} "
          f"(intl={sum(1 for r in hour10_dep if r['location']=='international')} "
          f"dom={sum(1 for r in hour10_dep if r['location']=='domestic')})")
    print(f"Hour 10 arrivals: {len(hour10_arr)} "
          f"(intl={sum(1 for r in hour10_arr if r['location']=='international')} "
          f"dom={sum(1 for r in hour10_arr if r['location']=='domestic')})")
    print(f"Total departures: {len(deps)} (cancelled={sum(1 for r in deps if r['status']=='cancelled')})")
    print(f"Total arrivals: {len(arrs)} (cancelled={sum(1 for r in arrs if r['status']=='cancelled')})")

    dep_payload = {"request": {"method": "delays", "params": {"type": "departures"}}, "response": deps, "terms": "synthetic-test-fixture-derived"}
    arr_payload = {"request": {"method": "delays", "params": {"type": "arrivals"}}, "response": arrs, "terms": "synthetic-test-fixture-derived"}

    with open(OUT_DIR / "IST_departures.json", "w", encoding="utf-8") as fh:
        json.dump(dep_payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    with open(OUT_DIR / "IST_arrivals.json", "w", encoding="utf-8") as fh:
        json.dump(arr_payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


if __name__ == "__main__":
    main()
