# Havalimanı Security / Passport Yoğunluk Tahmin Sistemi

Uçuş tarifesinden yola çıkarak, **her havalimanı için ayrı ayrı**,
15 dakikalık pencerelerde security ve passport kuyruk yoğunluğunu
tahmin eder; yoğunluğun **nedenini** de metin olarak üretir.

Sistem üç madde halinde kurulmuştur:

| Madde | Kapsam | Yer |
|---|---|---|
| 1 | Uçak tipi (ICAO) → yolcu kapasitesi çözümleyici | `app/models.py`, `app/db.py`, `app/seed.py`, `app/service.py` |
| 2 | Veri birleştirme, talep, kuyruk modeli, neden tespiti | `app/queue/` |
| 3 | Kalıcılık, çok-havalimanlı orkestrasyon, raporlama | `app/queue/engine.py`, `app/queue/reporting.py` |

Madde 1 kodu Madde 2/3 tarafından **import edilip kullanılır**,
değiştirilmez.

---

## Çalıştırma

```bash
python -m venv venv
venv\Scripts\pip install sqlalchemy pytest

# Madde 1 referans verisi (uçak tipi -> kapasite)
venv\Scripts\python -m app.seed

# Uçtan uca: kaynak dosyalar -> veritabanı -> tahminler
venv\Scripts\python -m app.queue.pipeline

# Testler
venv\Scripts\python -m pytest -q
```

`DATABASE_URL` ortam değişkeni tanımlıysa o kullanılır; yoksa proje
kökünde `database.sqlite` açılır.

> **Not — `app.seed`:** Script üç kaynağı yükler: 101 doğrulanmış
> tip (`yolcu_ucaklari.json`, ana kaynak), 11 yedek varyant
> (`curated_fallback.json`) ve 65 aile/genel havacılık satırı.
>
> `app.seed.run()` tabloları `drop_first=True` ile düşürür. Script
> `app.queue.models`'i import etmediği için kuyruk tabloları bu
> düşürmeye dahil olmaz; ancak aynı süreçte önce kuyruk modelleri
> import edilirse uçuş ve tahmin tabloları da silinir. Seed'i her
> zaman ayrı bir süreçte çalıştırın.

---

## Veri akışı

```
data/Delays - Type Departures.json ─┐
data/Delays - Type Arrivals.json  ──┤ Kaynak A: zaman, terminal, statü
                                    │
data/response-delays.json ──────────┤ Kaynak B: aircraft_icao
                                    │
data/flight_airports.sql ───────────┘ havalimanı + ülke + timezone
                 │
                 ▼
     ingestion/sources.py   (AŞAMA 0: normalize + enrichment)
                 │
                 ▼
     ingestion/refresh.py   (upsert + FlightEvent)
                 │
                 ▼
     engine.py              (AŞAMA 2-8: akış, pencere, skor, neden, confidence)
                 │
                 ▼
     reporting.py           (AŞAMA 9: saatlik + özet veri yapısı)
```

**Kaynak A ↔ Kaynak B eşleşmesi** uçuş numarası üzerinden yapılır
(`"AC 72"` → `"AC72"`). Eşleşme bulunamazsa `aircraft_icao = None`
kalır; Madde 1 bunu `unknown_default` (150 koltuk) ile karşılar ve
tahminin confidence'ı düşer. Sistem bu durumda **çökmez**.

> Elimizdeki örnek kaynak dosyalarında Kaynak B, dünya genelinde
> anlık bir ADS-B kesitidir; Kaynak A'daki 200 uçuşla kesişimi
> düşüktür. Bu yüzden gerçek çalıştırmada `aircraft_match_rate`
> düşük çıkar ve confidence ~0.40 seviyesine iner. Bu bir hata değil,
> verinin dürüst yansımasıdır.

---

## Modül haritası

```
app/queue/
├── constants.py              eşikler ve açık varsayımlar
├── models.py                 AŞAMA 8 tabloları
├── config.py                 AŞAMA 1 havalimanı config çözümleme
├── baseline.py               clustering baseline'ı (birikmeden None)
├── engine.py                 orkestrasyon + kalıcılık
├── reporting.py              AŞAMA 9 veri katmanı
├── pipeline.py               uçtan uca çalıştırıcı
├── core/
│   ├── erlang.py             Erlang-C (saf matematik)
│   └── scoring.py            security yoğunluk skoru + passport Erlang-C
├── domain/
│   ├── flight_rules.py       load factor, buffer, gecikme
│   ├── demand.py             yolcu talebi + pencere toplama
│   └── flows.py              AŞAMA 2 yönlendirme kuralları
├── reasons/detector.py       9 neden tespit fonksiyonu
└── ingestion/
    ├── sources.py            Kaynak A/B ayrıştırma + enrichment
    ├── airports_import.py    flight_airports.sql içe aktarma
    └── refresh.py            upsert + değişiklik event'leri
```

`core/`, `domain/` ve `reasons/` katmanları **veritabanına dokunmaz**;
hepsi mock nesnelerle test edilebilir. Veritabanı sadece `engine`,
`reporting`, `baseline`, `config` ve `ingestion` katmanlarındadır.

---

## Akış kuralları (AŞAMA 2)

| Uçuş | Security | Passport |
|---|---|---|
| Domestic + Departure | ✔ | — |
| Domestic + Arrival | — | — |
| International + Departure | ✔ | ✔ |
| International + Arrival | — | ✔ |
| Cancelled / Diverted | — | — |

`domestic_arrival` hiçbir kuyruğu beslemediği için raporlama
yapısında **hiç yer almaz**.

---

## Security ve passport aynı formülü kullanmaz

**Security — basit yoğunluk sinyali.** Gerçek lane/gişe/personel
sayısı bilinmediği için kuyruk teorisi kurulmaz. O pencerede geçmiş
ortalamaya göre kaç kat uçuş olduğuna bakılır.
`estimated_wait_minutes` **hiçbir koşulda üretilmez**, her zaman
`null`'dır. Geçmiş veri birikmemişse risk `UNKNOWN` döner — sahte
baseline üretilmez.

**Passport — tam Erlang-C.** Gişe sayısı (c) ve servis hızı (μ)
havalimanı config'inden gelir, bekleme süresi dakika olarak üretilir.
ρ ≥ 1 ise kuyruk teorik olarak sürdürülemez: risk `CRITICAL`,
`estimated_wait_minutes = null`.

---

## Açık varsayımlar

Aşağıdakiler **canlı veri değildir**, açıkça işaretlenmiş statik
tahminlerdir. Kod içindeki yorumlarıyla birlikte korunmalıdır.

| Varsayım | Değer | Nerede |
|---|---|---|
| Load factor — domestic | 0.78 | `constants.py` |
| Load factor — kısa/orta/uzun menzil uluslararası | 0.82 / 0.84 / 0.88 | `constants.py` |
| Security'ye varış buffer'ı (boarding saati yok) | 45 / 60 / 90 dk | `constants.py` |
| Passport'a ulaşma buffer'ı (iniş + yürüme) | 15 dk | `constants.py` |
| Gişe verimlilik çarpanı (2 kişi = 2× değil) | 1.5 | `AirportOperationalConfig` |
| Talep penceresi | 15 dk | `constants.py` |

Uçuş süresi de doğrudan gelmez: `arrScheduled − depScheduled`
farkından türetilen bir **mesafe proxy'sidir**.

**Zaman önceliği:** bir uçuşun kuyruğa yansıdığı an da, gecikmesi de
`actual > estimated > scheduled` sırasıyla okunur. İkisinin aynı
kaynağı kullanması zorunludur: gecikme `estimated`'tan okunup pencere
`scheduled`'a göre seçilseydi, henüz gerçekleşmemiş uçuşun yolcuları
hiç var olmayacakları pencereye yazılır, gerçekten geldikleri pencere
boş görünürdü. Gelecek uçuşlarda `actual` zaten yoktur — tahmin
sisteminin asıl çalıştığı durum budur.

**Varsayılan havalimanı config'i** (4 gişe, 8 personel, kişi başı
0.5 yolcu/dk) örnek değerdir, ölçüm değildir. Bu değerlerle 15
dakikalık bir pencerede tek bir geniş gövde uçağın yolcusu bile
kapasiteyi aşar; gerçek bir havalimanı için
`airport_operational_configs` tablosuna kendi satırı yazılmalıdır.
Varsayılanla hesaplanan her tahmin confidence'tan 0.05 kaybeder.

---

## Çok-havalimanlı mimari

Hesap her zaman **tek bir havalimanının** uçuş listesi üzerinde
çalışır; `run_predictions` bunu sistemdeki her havalimanı için
tekrarlar. Havalimanları birbirinin uçuşunu, config'ini veya
baseline'ını görmez.

Yeni havalimanı eklemek = `airport_operational_configs` tablosuna
**bir satır** eklemek. Kod değişikliği gerekmez; satır yoksa
varsayılan config `is_default=True` ile kullanılır.

---

## Neden tespiti (AŞAMA 6)

Her pencere için dokuz tespit fonksiyonunun **hepsi** çalıştırılır:

1. **clustering** — pencere uçuş sayısı / geçmiş ortalama > 1.4
2. **widebody** — ≥250 koltuklu uçak sayısı ≥ 2 veya payı > %30
3. **delay_compression** — ≥2 gecikmiş uçuş aynı pencerede
   (tek gecikme `single_delay`, sadece bilgi notu — riski artırmaz)
4. **utilization** — ρ > 0.9 (yalnız passport)
5. **intl_share** — uçuşların > %60'ı uluslararası (yalnız passport)
6. **arrival_bank** — pencerede inen uçuş ≥ config eşiği (yalnız passport)
7. **cancellation** — iptal edilen uçuş var
8. **aircraft_change** — `AIRCRAFT_CHANGED` event'i, kapasite deltası ≠ 0
9. **diversion** — `status == "diverted"`

Hiçbiri eşiği geçmezse tek bir `normal` maddesi yazılır; neden
listesi **asla boş dönmez**. Serbest/jenerik metin üretilmez — her
mesaj sabit bir şablondan doldurulur.

Bunlara ek olarak skorlamanın kendi iki şeffaflık notu vardır:
`no_baseline` (security'de geçmiş veri yok) ve `capacity_exceeded`
(passport'ta ρ ≥ 1). Bunlar tespit fonksiyonu değildir, skorun neden
`UNKNOWN`/`CRITICAL` olduğunu açıklar.

Kombinasyonlar için ayrı kod yoktur: aynı pencerede birden fazla
nedenin tetiklenmesi zaten bir kombinasyondur.

---

## Confidence skoru (AŞAMA 7)

```
1.00
 -0.10  load factor her zaman statik varsayım
 -0.10  boarding buffer her zaman türetilmiş
 -0.15  uçak tipi eşleşme oranı < 0.80
 -0.10  pencerede aircraft_icao'su None kalan uçuş var
 -0.10  clustering baseline'ı henüz birikmedi
 -0.05  havalimanı config'i varsayılan
        (alt sınır 0.10)
```

---

## Veritabanı ve şişme koruması

- `flights`, `queue_predictions`, `historical_flight_counts`:
  her refresh'te **upsert**, yeni satır açılmaz.
  `queue_predictions` benzersizliği `(airport_iata, process,
  window_start)`.
- `flight_events`: **sadece gerçek değişiklikte** satır açılır —
  uçak tipi değişimi, gecikme eşiğinin geçilmesi, iptal, diversion.
  Aynı veri tekrar gelirse hiçbir satır yazılmaz.
- `airports`: `flight_airports.sql`'den bir kere içe aktarılır.
- Uçuşu olmayan pencereye tahmin satırı **yazılmaz**.
- Bir uçuş gecikip başka pencereye taşındığında, eski penceresinin
  tahmini **silinir**. `queue_predictions` bir current-state
  tablosudur: uçuş tablosundan deterministik türetilir, yeni hesapta
  yer almayan satır tanım gereği bayattır. Uzun vadeli hafıza bu
  tabloda değil `historical_flight_counts`'tadır.

Baseline birimi: "o havalimanı + süreç + saat + haftanın günü için
**bir 15 dakikalık penceredeki** ortalama uçuş sayısı". Gözlem,
tahminler hesaplandıktan *sonra* kaydedilir; pencere kendi
baseline'ını beslemez.

---

## Raporlama (AŞAMA 9)

İki veri yapısı üretilir; ikisi de **yeni hesap tetiklemez**, yazılmış
`queue_predictions` satırlarını okur.

- `hourly_report(session, iata, date, resolver)` → `/airports/{iata}/hourly`
- `summary_report(session, date)` → `/airports/summary`

Saatlik satır, o saatin 15 dakikalık pencerelerinin özetidir:
risk = en kötü pencere, oran/ρ/bekleme = en yüksek değer,
confidence = en düşük değer, nedenler = koda göre tekilleştirilmiş.

Bu katman grafik çizmez; HTTP çatısı (FastAPI vb.) eklendiğinde bu
iki fonksiyon doğrudan endpoint gövdesi olarak kullanılabilir.

---

## Sistemde OLMAYAN veri kaynakları

Aşağıdaki yedi kalem için kod, config, placeholder veya değişken
**yoktur**; sistemin bunlardan haberi yoktur:

1. Personel eksikliği (staff shortage) feed'i
2. Ekipman arızası / lane kapanması sinyali
3. E-gate durumu
4. Passport sistem arızası durumu
5. Hava durumu kesintisi API'si
6. Canlı/gerçek load factor
7. Boarding time

---

## Tespit edilemeyen senaryolar

Bunlar **bilinçli olarak modellenmemiştir**; kodda placeholder veya
yorum satırı da bırakılmamıştır.

- **Rebooking dağılımı.** İptal sonrası yolcuların hangi uçuşlara
  aktarıldığı bilinmiyor. İptalin kendisi tespit edilir (Neden 7) ve
  talepten düşülür, ama yolcuların yeniden dağılımı **sayısal olarak
  modellenmez**.
- **Connecting flight bank.** Transfer yolcu sayısı verisi yok.
  Arrival bank (Neden 6) yalnızca kısmi bir proxy sağlar.
- **Staff shortage + peak hour kombinasyonu.** Personel verisi yok.
- **Equipment failure + high demand.** Ekipman verisi yok.
- **Security incident + lane closure.** Lane durumu verisi yok.
- **Passport system failure + international wave.** Sistem durumu
  verisi yok.
- **E-gate failure + high volume.** E-gate verisi yok.
- **Diversion'ın downstream etkisi.** Yönlendirilen uçuş talepten
  çıkarılır ve not edilir, ama yolcuların vardığı havalimanındaki
  etkisi modellenmez.

---

## Production scheduler (systemd)

Sistem sürekli çalışan bir servis/worker DEĞİLDİR: AirLabs verisi zaten
~30 dakikada bir yenilendiği için `python -m app.queue.pipeline`
(bkz. `app/queue/pipeline.py:main()`) periyodik bir **batch** olarak
çalıştırılır — her çalıştırma ingestion → refresh → prediction → DB
adımlarını yapıp çıkar. Bunun için ek bir Python scheduler/worker
(APScheduler, Celery, Redis kuyruğu vb.) **yazılmadı/eklenmedi**;
Linux production sunucularında bunu `systemd timer` + `systemd
service` üstleniyor. Unit dosyaları: `deploy/systemd/`.

| Dosya | Amaç |
|---|---|
| `deploy/systemd/aircraft-capacity.service` | `python -m app.queue.pipeline`'ı tek seferlik (`Type=oneshot`) çalıştırır |
| `deploy/systemd/aircraft-capacity.timer` | Servisi saatin :00/:30'unda tetikler (`Persistent=true`) |
| `deploy/systemd/aircraft-capacity.env.example` | `EnvironmentFile=` için ÖRNEK biçim - gerçek secret İÇERMEZ |

### Kurulum

```bash
# 1) Ayrıcalıksız sistem kullanıcısı + uygulama dizini
sudo useradd --system --home /opt/aircraft-capacity --shell /usr/sbin/nologin aircraft-capacity
sudo mkdir -p /opt/aircraft-capacity
# (repo'yu /opt/aircraft-capacity içine kopyalayın/klonlayın, sonra:)
cd /opt/aircraft-capacity
sudo -u aircraft-capacity python -m venv venv
sudo -u aircraft-capacity venv/bin/pip install -r requirements.txt

# 2) Secret'lar - repo'nun DIŞINDA, kısıtlı izinli bir dosyada
sudo mkdir -p /etc/aircraft-capacity
sudo cp deploy/systemd/aircraft-capacity.env.example /etc/aircraft-capacity/aircraft-capacity.env
sudo chmod 600 /etc/aircraft-capacity/aircraft-capacity.env
sudo chown aircraft-capacity:aircraft-capacity /etc/aircraft-capacity/aircraft-capacity.env
# ^ bu dosyayı GERÇEK AIRLABS_API_KEY / DATABASE_URL değerleriyle düzenleyin.

# 3) Unit dosyalarını kur
sudo cp deploy/systemd/aircraft-capacity.service deploy/systemd/aircraft-capacity.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

### Timer'ı etkinleştirme / başlatma

```bash
sudo systemctl enable --now aircraft-capacity.timer
```

Bu, `aircraft-capacity.service`'i DOĞRUDAN başlatmaz - sadece timer'ı
kurar; ilk gerçek çalıştırma bir sonraki :00/:30'da olur (`Persistent=
true` sayesinde sunucu o an kapalıysa da bir sonraki açılışta telafi
edilir).

### Durum kontrolü

```bash
systemctl status aircraft-capacity.timer     # timer aktif mi, bir sonraki tetikleme ne zaman
systemctl list-timers aircraft-capacity.timer
systemctl status aircraft-capacity.service   # son çalıştırmanın sonucu (exit code dahil)
```

Kritik bir hata (bkz. `main()`'in exit-code kararı) `systemctl
--failed` altında görünür; kısmi havalimanı hatası (`failed_airports`)
servisi "failed" YAPMAZ, sadece WARNING olarak loglanır.

### Logları görüntüleme

```bash
journalctl -u aircraft-capacity.service -f              # canlı takip
journalctl -u aircraft-capacity.service --since "2h ago" # geçmiş
```

`app/logging_config.py`'nin ürettiği rutin INFO logları (run started/
completed, ingestion/refresh/prediction özetleri, süre) buradan okunur
- ayrı bir log dosyası/rotasyon yönetimi gerekmez, journald üstlenir.

### Manuel (tek seferlik) çalıştırma

```bash
sudo systemctl start aircraft-capacity.service
# veya, geliştirme/hata ayıklama için doğrudan:
sudo -u aircraft-capacity /opt/aircraft-capacity/venv/bin/python -m app.queue.pipeline
```

### Overlap koruması

Ayrı bir kilit dosyası/PID kontrolü **yok** - systemd, aynı unit adı
(`aircraft-capacity.service`) zaten çalışırken timer'dan gelen ikinci
bir tetiklemeyi ikinci bir instance olarak BAŞLATMAZ. Bir run 30
dakikadan uzun sürerse bir sonraki tetikleme bu garanti sayesinde
çakışmaz.

---

## Testler

```
tests/test_core_math.py         Erlang-C, risk bantları, confidence
tests/test_domain_rules.py      load factor, buffer, gecikme, pencere
tests/test_ingestion.py         normalizasyon, enrichment, upsert, event
tests/test_reason_detection.py  9 tespit fonksiyonu ve eşikleri
tests/test_engine.py            14 senaryonun tamamı (motor seviyesi)
tests/test_reporting.py         kalıcılık, şişme koruması, AŞAMA 9 çıktıları
```

Senaryo testleri yalnızca gerçek veriden türetilebilen durumları
kapsar. Personel, ekipman, e-gate, hava durumu ve passport sistem
arızası için test **yazılmamıştır** — bu özellikler sistemde yoktur.
