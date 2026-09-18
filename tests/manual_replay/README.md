# Manual Replay - genel-proje.md Bölüm 36/47 tarzı, 2026-09-18 IST senaryosu

Bu klasör, **gerçek `data/*.json` kaynak dosyalarıyla AYNI şemada** ama
tamamen izole, üretilmiş test fixture'ları içerir. Amaç: production
`database.sqlite`'a ve gerçek `data/` dosyalarına DOKUNMADAN, frontend'in
4 grafiğini (Overall / Domestic Security / International Departure /
International Arrival) gerçek pipeline zincirinden geçirilmiş, anlamlı
bir veri setiyle localde görebilmek.

## İçerik

| Dosya | Ne | Gerçek karşılığı |
|---|---|---|
| `Delays - Type Departures.json` | Kaynak A - IST kalkışları (14 kayıt) | `data/Delays - Type Departures.json` ile AYNI şema/wrapper |
| `Delays - Type Arrivals.json` | Kaynak A - IST varışları (6 kayıt) | `data/Delays - Type Arrivals.json` ile AYNI şema/wrapper |
| `flights_live.json` | Kaynak B - AirLabs `flights` (canlı ADS-B) formatı (3 kayıt) | `data/response-delays.json` ile AYNI alan adları (`hex`, `updated`, `aircraft_icao`, ...) - **`response-delays.json`'ın KENDİSİ kullanılmadı**, sadece şema referans alındı |
| `run_manual_replay.py` | Runner - izole DB'ye karşı pipeline'ı çalıştırır ve rapor basar | - |
| `_generate_fixtures.py` | Üstteki 3 JSON'u üreten (idempotent) yardımcı script - deliverable değil, şeffaflık için bırakıldı | - |
| `manual_replay.sqlite` | Runner çalıştırılınca OLUŞUR - izole SQLite, `.gitignore`'a eklenmedi ama **production DB değildir** | - |

Toplam **20 uçuş** (14 departure + 6 arrival), IST ana havalimanı, tarih
**2026-09-18**, `Europe/Istanbul` (UTC+3) yerel saat.

## Senaryo kapsamı

- Aynı saate yığılmış uçuşlar: `TK101/TK102/PC103` (09:00-09:05 domestic), `TK201-TK205` (12:00-12:20 international dalga), `TK301/TK302` (08:00-08:05 international arrival)
- Yüksek kapasiteli uçak: `A359` (475), `B77W` (365, curated_fallback)
- Normal uçaklar: `A320` (150), `A21N` (220), `B738` (162)
- Bilinmeyen ICAO -> 180 fallback: `TK206` (`aircraft_icao="ZZZZ"`)
- `aircraft_icao=null` + Source B enrichment: `SV205` (Kaynak A'da null, `flights_live.json`'daki eşleşen kayıttan `B738` olarak çözülür)
- Gecikmeli uçuş: `TK207` (departure), `TK305` (arrival)
- Actual zamanlı uçuş: `TK209`
- Estimated zamanlı uçuş: `TK210`
- Cancelled: `TK208` (departure), `TK306` (arrival)
- Farklı airline: `TK` (Turkish Airlines), `PC` (Pegasus), `SV` (Saudia)
- Domestic/international ayrımı: domestic = `TK101/102/209/210` (dep) + `TK303` (arr, ESB->IST) - hepsi TR-TR
- Gece yarısına yakın: `TK211` (dep, yerel 23:50), `TK304` (arr, yerel 23:55 - +15dk buffer ile queue event'i YEREL 00:10'a, yani bir SONRAKİ takvim gününe taşar; flight'ın kendi operasyonel günü hâlâ 18 Eylül'dür - Bölüm 61 ayrımının kanıtı)
- Passport->security coupling: `TK201-TK205` dalgası passport havuzunu (8 server × 1.5dk = 320 pax/saat) doyurup `international_departure` grafiğinde security yükünün SONRAKİ saatlere yayıldığını gösterir (bkz. runner çıktısındaki "passport->security coupling windows")

## 1) Manual replay'i çalıştırma

```powershell
venv\Scripts\python.exe tests\manual_replay\run_manual_replay.py
```

Bu komut:
- `tests\manual_replay\manual_replay.sqlite` dosyasını (varsa) siler ve YENİDEN oluşturur - **gerçek `database.sqlite`'a hiç dokunmaz**.
- `DATABASE_URL` ortam değişkenini SADECE bu process için bu dosyaya ayarlar.
- Gerçek `app/queue/pipeline.py:run()` zincirini (`parse_source_a` -> `refresh_flights` -> `AircraftCapacityService` -> `run_predictions` -> `airport_predictions`) bu klasördeki fixture'larla çalıştırır.
- `data/flight_airports.sql` + `data/yolcu_ucaklari.json` + `data/curated_fallback.json` dosyalarını SADECE OKUR (referans/seed - production'ın zaten yaptığı salt-okunur import).
- Konsola: parsed/inserted/updated, IST bulundu mu, 4 grafiğin bucket sayıları, unknown ICAO/cancelled/enrichment/coupling doğrulama satırlarını yazdırır.

## 2) Frontend'i BU test DB'siyle localde açma

```powershell
$env:DATABASE_URL = "sqlite:///$PWD/tests/manual_replay/manual_replay.sqlite"
venv\Scripts\python.exe -m app.web.server --port 8000
```

Tarayıcıda:

```
http://localhost:8000/
```

Sol üstten **IST**'i seçip 4 grafiği (Overall, Domestic Security,
International Departure, International Arrival) inceleyebilirsiniz.
Bir saate tıklayınca alt detayda "Tahmini bekleme süresi" / "Passport
bekleme" / "Security bekleme" artık `X saat Y dk` formatında görünür
(backend hâlâ dakika cinsinden ham sayı döndürür - sadece gösterim
biçimlendirildi).

Sunucuyu durdurmak için terminalde `Ctrl+C`.

## 3) Gerçek production DB'ye geri dönme

Aynı PowerShell penceresinde `DATABASE_URL` ayarlandığı için, o pencereyi
kapatmadan devam ederseniz production komutları (`python -m
app.queue.pipeline`, `python -m app.web.server`) YANLIŞLIKLA test DB'sini
kullanmaya devam eder. Geri dönmek için:

```powershell
Remove-Item Env:DATABASE_URL
```

veya yeni bir PowerShell penceresi açın (ortam değişkeni sadece o oturuma
özeldir, gerçek `database.sqlite` hiçbir zaman değişmedi). Doğrulamak
için:

```powershell
venv\Scripts\python.exe -c "from app.db import DATABASE_URL; print(DATABASE_URL)"
```

çıktısı `sqlite:///...\database.sqlite` olmalı (test dosyası DEĞİL).

## Güvenlik notları

- Gerçek `database.sqlite` bu tur boyunca AÇILMADI/YAZILMADI (`DATABASE_URL` her zaman izole dosyaya işaret etti).
- Gerçek `data/*.json` dosyaları hiç overwrite edilmedi - sadece OKUNDU (havalimanı referansı + kapasite seed için, production'ın zaten yaptığı gibi).
- `manual_replay.sqlite` tekrar çalıştırıldığında baştan oluşturulur (idempotent) - elle silmek isterseniz: `Remove-Item tests\manual_replay\manual_replay.sqlite`.
