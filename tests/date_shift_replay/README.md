# Date-Shift / Time-Translation Replay (+4 gün)

Bu klasör, **gerçek `data/*.json` dosyalarının BİREBİR kopyasıdır** —
tek fark, tüm tarih/zaman alanlarının `timedelta(days=4)` ile ileri
kaydırılmış olmasıdır. Hiçbir uçuş eklenmedi/silinmedi/değiştirilmedi
(aircraft/airline/status/delay/terminal/gate/flight_number birebir
korunuyor); SADECE takvim tarihi değişti.

Amaç: Airport Queue Model V2'nin (operational-day seçimi + event-driven
queue + hourly graph bucket) **tarih kaymasına karşı matematiksel
olarak değişmez (invariant)** olduğunu, gerçek veriyle kanıtlamak.

## Mapping

```
2026-09-13 -> 2026-09-17
2026-09-14 -> 2026-09-18
2026-09-15 -> 2026-09-19
new_datetime = original_datetime + timedelta(days=4)
```

## İçerik

| Dosya | Ne | Gerçek karşılığı |
|---|---|---|
| `Delays - Type Departures.json` | Kaynak A departures, +4 gün shifted, 100 kayıt | `data/Delays - Type Departures.json` |
| `Delays - Type Arrivals.json` | Kaynak A arrivals, +4 gün shifted, 100 kayıt | `data/Delays - Type Arrivals.json` |
| `flights_live.json` | Kaynak B (`flights`), +4 gün shifted (`updated` epoch), 9625 kayıt | `data/response-delays.json` (sadece şema referansı, kendisi KULLANILMADI) |
| `run_date_shift_replay.py` | Runner - izole DB'ye karşı gerçek pipeline'ı çalıştırır | - |
| `_generate_shifted_fixtures.py` | Üstteki 3 JSON'u üreten (idempotent) script - deliverable değil | - |
| `date_shift_replay.sqlite` | Frontend'de görüntülemek için KALICI izole DB (CDG+CBR, now=2026-09-18) | - |

## Gerçek veri özeti (bkz. final rapor için tam tablo)

- `data/Delays - Type Departures.json` + `data/Delays - Type Arrivals.json`: toplam 200 kayıt (100+100), **hepsi tek bir sayfa** (`has_more:true`, `total_items:4052` — bu sadece ilk sayfa).
- 2026-09-13: 1 departure + 1 arrival = 2 kayıt
- **2026-09-14: 99 departure + 99 arrival = 198 kayıt**
- 2026-09-15: 0 kayıt

## Temsili havalimanı: CDG

Gerçek veride tek-saat/tek-havalimanı için **en yoğun International
Departure kümesi** CDG'de: 2026-09-14 06:00 UTC queue-event bucket'ına
**14 uçuş** düşüyor (bkz. final rapor, Bölüm 7-9). Bu yüzden CDG bu
replay'in ana temsili havalimanı olarak seçildi. Domestic akış için
ayrıca **CBR** (Canberra) kullanıldı — gerçek veride en yoğun tek-saatli
Domestic Departure kümesi (5 uçuş, aynı saat).

## 1) Date-shift fixture'larını yeniden üretme (opsiyonel, idempotent)

```powershell
venv\Scripts\python.exe tests\date_shift_replay\_generate_shifted_fixtures.py
```

## 2) Date-shift replay'i çalıştırma (CLI, konsol raporu)

```powershell
venv\Scripts\python.exe tests\date_shift_replay\run_date_shift_replay.py
```

Bu hem ORİJİNAL (`data/`, now=2026-09-14) hem SHIFTED (bu klasör,
now=2026-09-18) taraflarını izole SQLite dosyalarına karşı çalıştırıp
özet basar. **Gerçek `database.sqlite`'a hiç dokunmaz.**

## 3) Frontend'i bu date-shift DB'siyle localde açma

```powershell
$env:DATABASE_URL = "sqlite:///$PWD/tests/date_shift_replay/date_shift_replay.sqlite"
venv\Scripts\python.exe -m app.web.server --port 8000
```

Tarayıcıda: `http://localhost:8000/` — **CDG**'yi seçin.

Bu DB **sabittir** — sunucu açıkken hiçbir gerçek AirLabs çağrısı
yapılmaz, hiçbir source refresh tetiklenmez; frontend sadece bu DB'deki
(zaten hesaplanmış) `QueuePrediction` satırlarını okur.

Sunucuyu durdurmak için `Ctrl+C`.

## 4) Gerçek production DB'ye geri dönme

```powershell
Remove-Item Env:DATABASE_URL
```

## Frontend Expected Values (representative saat: 2026-09-18 06:00)

Bu tabloyla `localhost:8000`'de CDG seçip 06:00 barına tıkladığınızda
gördüğünüz wait/risk'i karşılaştırabilirsiniz. (`expected_passengers`
frontend'de DOĞRUDAN gösterilmez — Bölüm 27/50 gereği frontend passenger
sayısı render etmez; API'deki gerçek değer burada referans için yazıldı.)

| Graph | Shifted Hour | API expected_passengers | Expected Wait (frontend format) | Expected Risk |
|---|---|---|---|---|
| Overall | 2026-09-18 06:00 | (breakdown, bkz. altı) | **7 saat 16 dk** (435.9 dk) | CRITICAL |
| Domestic Security | — | 0 (CDG'de bu saatte domestic kalkış YOK) | Hesaplanamıyor (pencere yok) | — |
| International Departure — Passport | 2026-09-18 06:00 | 2645 | **7 saat 16 dk** (435.9 dk) | CRITICAL |
| International Departure — International Security | 2026-09-18 06:00 | 168 | **0 dk** (0.0 dk) | LOW |
| International Arrival — Passport | 2026-09-18 08:00 | 180 | **5 saat 50 dk** (349.7 dk) | LOW |

Orijinal (2026-09-14) karşılığı, final raporun "GRAPH karşılaştırması"
tablosunda birebir (sadece tarih −4 gün) aynı sayılarla verilmiştir.

## Güvenlik notları

- Gerçek `database.sqlite` bu tur boyunca hiç açılmadı/yazılmadı.
- Gerçek `data/*.json` dosyaları hiç overwrite edilmedi, sadece OKUNDU.
- `run_date_shift_replay.py` **KENDİ izole `create_engine()`'ini** kurar
  (production `app.db`'nin global/tek-seferlik engine'ini KULLANMAZ) -
  bu, aynı Python process'inde birden fazla replay çağrısının
  birbirine karışmasını önlemek için bilinçli bir tasarım kararıdır
  (bkz. `run_date_shift_replay.py`'nin docstring'i).
