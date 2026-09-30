import json
from app.queue import pipeline
from app.queue.constants import DIRECTION_ARRIVAL, DIRECTION_DEPARTURE
from app.queue.ingestion.sources import load_source_payload

# ADIM (Izole ZRH+IST test ortamı) - `aircraft_capacity_zrh_only` DB'sine
# SADECE ZRH ve IST'in kendi özel dosyalarından besleniyor - ana ortamın
# ("Delays - Type Arrivals/Departures.json", tüm havalimanları) hiçbir
# şekilde karışmaması için source_a TAMAMEN override ediliyor.
EXTRA_FILES = {
    DIRECTION_ARRIVAL: ["ZRH - Arrival.json", "IST - Arrival.json"],
    DIRECTION_DEPARTURE: ["ZRH - Departures.json", "IST - Departures.json"],
}

def merged_source_a(direction):
    records = []
    for filename in EXTRA_FILES[direction]:
        path = pipeline._path(pipeline.DATA_DIR, filename)
        records.extend(load_source_payload(path))
    return records

summary = pipeline.run(source_a=merged_source_a)
print(json.dumps(summary, indent=2, default=str))
