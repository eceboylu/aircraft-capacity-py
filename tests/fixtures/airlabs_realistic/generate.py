"""
Gerçekçi, ÇOK-havalimanlı sentetik AirLabs `schedules` fixture'ı.

Bu script bir PYTEST DOSYASI DEĞİLDİR (`tests/stress_large_volume.py` /
`tests/fixtures/airlabs_operational/generate.py` ile AYNI konvansiyon).

AMAÇ: Gerçek örnek verimizin (data/Delays - Type *.json) çok havalimanı
için AŞIRI seyrek olması (çoğu havalimanı sadece 1 uçuş) yüzünden
passport ortalama bekleme hesabının anlamlı olamadığı havalimanlarına,
GERÇEK AirLabs `schedules` şemasıyla (aynı zarf/alan adları,
tests/fixtures/airlabs_mock/README.md'deki kontratla) tam günlük,
gerçekçi bir tarife üretmek.

BU VERİ SENTETİKTİR - her dosyada `_mock_disclaimer` alanı bunu açıkça
taşır. Gerçek bir AirLabs API çağrısından gelmemiştir.

Gerçek örnek veriden (Delays JSON'ları) FARKLI olarak, buradaki
kayıtların ÇOĞUNDA `aircraft_icao` Kaynak A'nın kendi alanında
DOLU tutuldu - bu, gerçek üretim AirLabs `schedules` akışının
BEKLENEN davranışıdır (bizim örnek dosyamızın bu alanı doldurmaması
kendi sınırlılığıdır, bkz. ADIM 6C raporu §F). Amaç: capacity
resolver'ın `verified_dataset` katmanını GERÇEKÇİ oranda kullandığı
bir senaryo test etmek.

Hiçbir risk/utilization/wait/demand DEĞERİ burada YAZILMAZ - hepsi
gerçek pipeline (parser -> refresh -> engine -> Erlang-C) tarafından
hesaplanır.
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "airlabs_operational"))
import generate as op  # noqa: E402 - mevcut üreticinin record()/_route() vb. yardımcılarını YENİDEN KULLANIR

OUT_DIR = os.path.dirname(__file__)
REF_DAY = op.REF_DAY

# Gerçekten flight_airports.sql'de bulunan, GERÇEK IATA kodları -
# uydurma kod YOK. `data/Delays - Type *.json`'daki (gerçek örnek
# veri) TÜM havalimanları - IST/SAW/ADB hariç (onlar ayrı, operasyonel
# senaryo fixture'ında zaten zengin). Ülke/timezone bilgisi mevcut
# resolve_location() tarafından, gerçek referans tablosundan okunacak.
TARGET_AIRPORTS = [
    "AEP", "AER", "ALC", "AMS", "ASR", "AYT", "CAI", "CAN", "CBR", "CDG",
    "CGK", "CGO", "CHQ", "CKG", "CUN", "DAD", "DAT", "DEL", "DEN", "DIG",
    "DIN", "DLC", "DME", "DPS", "DUB", "DXB", "FBM", "FOC", "FUK", "HAN",
    "HGH", "HND", "HNL", "HRB", "ICN", "IXB", "IXJ", "JED", "KBL", "KMJ",
    "KOJ", "KRL", "KZN", "LAX", "LTN", "MEL", "MUC", "OAG", "OKA", "ORN",
    "ORY", "PHE", "PKX", "PUS", "RAK", "RTM", "RUH", "SEA", "SJU", "SKG",
    "SQD", "SUB", "SVO", "SVQ", "SVX", "SZX", "TAE", "TAG", "TAO", "TFN",
    "TFU", "TPE", "VIE", "WEH", "WUH", "YTY", "ZRH",
]

random.seed(20260916)  # deterministik - her çalıştırmada AYNI fixture üretilir


def _fill_own_aircraft(records: list[dict]) -> None:
    """
    Kaynak A'nın kendi `aircraft_icao` alanını GERÇEKÇİ bir oranda
    (yaklaşık %85 - gerçek AirLabs `schedules` akışında bu alan
    genelde dolu gelir) DOLU tutar; kalan ~%15 boş bırakılır ki
    "hiç eşleşme yoksa flight DROP edilmez, unknown_default'a düşer"
    davranışı da test edilebilsin.
    """
    for i, r in enumerate(records):
        if i % 7 == 0:   # ~%14'ü bilinçli olarak boş
            r["aircraft_icao"] = None


def main() -> None:
    total_written = 0
    for index, airport in enumerate(TARGET_AIRPORTS):
        # `hash(airport)` KULLANILMADI - Python'da string hash'i
        # PYTHONHASHSEED'e bağlı, çalıştırmalar arası DEĞİŞİR. Liste
        # sırasına dayalı bu ofset her zaman AYNI (deterministik) ve
        # 77 havalimanı için de çakışmasız (her biri 200'lük bir
        # flight-number bloğu alıyor).
        offset = 6000 + index * 200
        dep = op.baseline_schedule(airport, "departure", 30, start_number=offset)
        arr = op.baseline_schedule(airport, "arrival", 30, start_number=offset + 100)

        # Gerçekçi gecikme çeşitliliği (departure kayıtları -> dep_estimated_utc).
        for r in dep:
            if random.random() < 0.25:
                delay = random.choice([5, 10, 15, 20, 30, 35])
                scheduled = datetime.strptime(r["dep_time_utc"], "%Y-%m-%d %H:%M")
                estimated = scheduled + timedelta(minutes=delay)
                r["dep_estimated"] = estimated.strftime("%Y-%m-%d %H:%M")
                r["dep_estimated_utc"] = r["dep_estimated"]
                r["status"] = "active"
        for r in arr:
            if random.random() < 0.25:
                delay = random.choice([5, 10, 15, 20, 30, 35])
                scheduled = datetime.strptime(r["arr_time_utc"], "%Y-%m-%d %H:%M")
                estimated = scheduled + timedelta(minutes=delay)
                r["arr_estimated"] = estimated.strftime("%Y-%m-%d %H:%M")
                r["arr_estimated_utc"] = r["arr_estimated"]
                r["status"] = "active"

        _fill_own_aircraft(dep)
        _fill_own_aircraft(arr)

        op._write_pages("t0", airport, "departure", dep)
        op._write_pages("t0", airport, "arrival", arr)
        total_written += len(dep) + len(arr)
        print(f"{airport}: departure={len(dep)} arrival={len(arr)}")

    print("toplam kayıt:", total_written)


if __name__ == "__main__":
    # `op._write_pages` OUT_DIR/round/... yoluna yazıyor - burada
    # KENDİ OUT_DIR'imize yazması için geçici olarak yönlendiriyoruz.
    op.OUT_DIR = OUT_DIR
    os.makedirs(os.path.join(OUT_DIR, "t0"), exist_ok=True)
    main()
