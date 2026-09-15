# AirLabs `schedules` — SYNTHETIC / MOCK Test Verisi

**Bu klasördeki hiçbir dosya gerçek bir AirLabs API çağrısından
gelmemiştir.** `AIRLABS_API_KEY` kullanılmadan, gerçek `schedules`
endpoint'i hiç çağrılmadan, sadece şunlara dayanarak elle/script ile
üretilmiştir:

- AirLabs'ın resmi `schedules` dokümantasyonu (alan adları, `request`
  zarfı yapısı, `has_more`/`offset` sayfalama sözleşmesi)
- Bu repodaki **gerçek** `delays`/`flights` response'larından (bkz.
  `data/*.json`) doğrulanmış ortak alan şeması (`schedules`, `delays`
  ile aynı field ailesini paylaşır)
- `data/flight_airports.sql`'deki **gerçek** IATA/ICAO/ülke kodları
  (IST/LTFM, SAW/LTFJ, ADB/LTBJ, CDG/LFPG, JFK/KJFK, DXB/OMDB,
  LHR/EGLL, FRA/EDDF — hepsi repodaki gerçek havalimanı referansından)

**Varsayımsal (gerçek `schedules` response'u ile HENÜZ doğrulanmamış)
olanlar:**
- `status` değer kümesi ve dağılımı
- `aircraft_icao` doluluk oranı (gerçekte `delays`'te dokümantasyonun
  iddia ettiğinden farklı çıkmıştı — `schedules` için de aynı risk
  geçerli)
- `request.has_more`/`total_items`/pagination sayıları

**Kasıtlı olarak eklenmemiş:**
- `diverted` durumu — gerçek `schedules` response'unda bu alanın nasıl
  geldiği bilinmediği için UYDURULMADI.
- `location`/`direction` alanları — hiçbir gerçek AirLabs response'unda
  (39.825 gerçek kayıt üzerinde doğrulandı) bulunmadığı için eklenmedi;
  bunlar zaten uygulama tarafından (`resolve_location()`) türetiliyor.

## Dosyalar

| Dosya | Havalimanı | Yön | Kayıt | `has_more` |
|---|---|---|---|---|
| `schedules_IST_departures_page_1.json` | IST | departure | 25 | `true` |
| `schedules_IST_departures_page_2.json` | IST | departure | 26 (+flight-identity kaydı) | `false` |
| `schedules_IST_arrivals_page_1.json` | IST | arrival | 25 | `true` |
| `schedules_IST_arrivals_page_2.json` | IST | arrival | 25 | `false` |
| `schedules_SAW_departures.json` | SAW | departure | 6 | `false` |
| `schedules_SAW_arrivals.json` | SAW | arrival | 6 | `false` |
| `schedules_ADB_departures.json` | ADB | departure | 6 | `false` |
| `schedules_ADB_arrivals.json` | ADB | arrival | 6 (+flight-identity kaydı) | `false` |

Her dosyanın kendi JSON içeriğinde de `_mock_disclaimer` alanı bu
uyarıyı taşır.

## Flight Identity Test Senaryosu

`TK123`, aynı operasyonel günde (2026-09-15) hem **IST'ten kalkış**
(`schedules_IST_departures_page_2.json`) hem **ADB'ye varış**
(`schedules_ADB_arrivals.json`) kaydı olarak yer alır. Mevcut
`build_flight_key()` mantığı bunları ayrı satırlara ayırmalıdır:

```
TK_123_2026-09-15_IST_departure
TK_123_2026-09-15_ADB_arrival
```

Bu, gerçek `parse_source_a_record()`/`airlabs_client.py` koduna
karşı doğrulanmıştır (salt okunur simülasyon, production kod
değiştirilmeden).
