"""
Date-shift fixture üretici - GERÇEK data/*.json dosyalarını BİREBİR
okuyup SADECE tarih alanlarını +4 gün kaydırarak
`tests/date_shift_replay/` altına yazar.

Hiçbir uçuş eklenmez/silinmez/değiştirilmez (aircraft/airline/status/
delay/terminal/gate/flight_number DOKUNULMAZ) - SADECE datetime string
ve epoch alanları `timedelta(days=4)` ile kaydırılır.

Bu script deliverable DEĞİLDİR (şeffaflık için bırakıldı, tekrar
çalıştırılabilir/idempotent - kaynak gerçek dosyalar hiç yazılmaz).
"""
import copy
import json
import os
from datetime import datetime, timedelta

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
DATA_DIR = os.path.join(REPO_ROOT, "data")
OUT_DIR = os.path.dirname(__file__)

SHIFT = timedelta(days=4)
SHIFT_SECONDS = 4 * 24 * 60 * 60  # 345600

# Kaynak A (schedules/delays) - gerçek response kayıtlarında görülen
# datetime STRING alanları (bkz. rapor: production `sources.py:field()`
# çağrılarının okuduğu AYNI alan adları).
_DATETIME_STRING_FIELDS = (
    "dep_time", "dep_time_utc", "dep_estimated", "dep_estimated_utc",
    "dep_actual", "dep_actual_utc",
    "arr_time", "arr_time_utc", "arr_estimated", "arr_estimated_utc",
    "arr_actual", "arr_actual_utc",
)
_EPOCH_FIELDS = (
    "dep_time_ts", "dep_estimated_ts", "dep_actual_ts",
    "arr_time_ts", "arr_estimated_ts", "arr_actual_ts",
)

_DT_FORMAT = "%Y-%m-%d %H:%M"


def _shift_datetime_string(value: str) -> str:
    parsed = datetime.strptime(value, _DT_FORMAT)
    return (parsed + SHIFT).strftime(_DT_FORMAT)


def shift_source_a_record(record: dict) -> dict:
    shifted = copy.deepcopy(record)
    for key in _DATETIME_STRING_FIELDS:
        value = shifted.get(key)
        if value:
            shifted[key] = _shift_datetime_string(value)
    for key in _EPOCH_FIELDS:
        value = shifted.get(key)
        if value is not None:
            shifted[key] = value + SHIFT_SECONDS
    return shifted


def shift_source_b_record(record: dict) -> dict:
    shifted = copy.deepcopy(record)
    if shifted.get("updated") is not None:
        shifted["updated"] = shifted["updated"] + SHIFT_SECONDS
    return shifted


def _load(name: str) -> dict:
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def _write(name: str, payload: dict) -> None:
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("wrote", path, "records=", len(payload["response"]))


def main() -> None:
    departures = _load("Delays - Type Departures.json")
    arrivals = _load("Delays - Type Arrivals.json")
    source_b = _load("response-delays.json")

    shifted_departures = copy.deepcopy(departures)
    shifted_departures["response"] = [
        shift_source_a_record(r) for r in departures["response"]
    ]

    shifted_arrivals = copy.deepcopy(arrivals)
    shifted_arrivals["response"] = [
        shift_source_a_record(r) for r in arrivals["response"]
    ]

    shifted_source_b = copy.deepcopy(source_b)
    shifted_source_b["response"] = [
        shift_source_b_record(r) for r in source_b["response"]
    ]

    _write("Delays - Type Departures.json", shifted_departures)
    _write("Delays - Type Arrivals.json", shifted_arrivals)
    _write("flights_live.json", shifted_source_b)


if __name__ == "__main__":
    main()
