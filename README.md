<div align="center">

# ✈️ Airport Queue Monitor

**Havalimanlarında security ve pasaport kuyruklarının yoğunluğunu uçuş tarifesinden tahmin eden sistem.**

Her havalimanı için 15 dakikalık pencerelerde bekleme riskini hesaplar ve yoğunluğun **nedenini** de açıklar.

![Python](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-D71F00?logo=sqlalchemy&logoColor=white)
![MySQL](https://img.shields.io/badge/MySQL-8-4479A1?logo=mysql&logoColor=white)
![AirLabs](https://img.shields.io/badge/Data-AirLabs%20API-0A66C2)
![pytest](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)
![systemd](https://img.shields.io/badge/deploy-systemd-30D475?logo=linux&logoColor=white)

</div>

---

## Ne yapar?

Bir havalimanındaki uçuş tarifesini alır ve şu soruları cevaplar:

- Önümüzdeki saatlerde **security** ve **pasaport** kontrolünde ne kadar yoğunluk olacak?
- Hangi 15 dakikalık pencere riskli, tahmini bekleme kaç dakika?
- Yoğunluğun **sebebi** ne: aynı anda inen geniş gövdeli uçaklar mı, gecikmelerin üst üste binmesi mi, uluslararası yolcu dalgası mı?
- Bu tahmine ne kadar **güvenilebilir**?

Sonuçlar bir web arayüzünde havalimanı bazında grafiklerle gösterilir ve JSON API üzerinden de okunabilir.

## Öne çıkanlar

| | |
|---|---|
| 🛫 **Uçak tipi → kapasite** | ICAO uçak tipinden koltuk sayısını çözer (101 doğrulanmış tip, yedek varyantlar ve aile eşleşmeleri) |
| 👥 **Yolcu talebi modeli** | Load factor, menzile göre havalimanına varış süresi ve gecikmelerle yolcuların kuyruğa ne zaman ulaşacağını hesaplar |
| 🧮 **Kuyruk teorisi** | Pasaport için Erlang-C, olay tabanlı kuyruk simülasyonu ve birikmiş kuyruk (backlog) takibi |
| 🛂 **4 ayrı kuyruk** | Pasaport giriş, pasaport çıkış, iç hat security ve dış hat security ayrı ayrı modellenir |
| 🔍 **Neden tespiti** | 9 tespit kuralı: yığılma, geniş gövde, gecikme sıkışması, iptal, uçak değişikliği, diversion… |
| 📊 **Confidence skoru** | Her tahminin ne kadar güvenilir olduğu, eksik veriye göre şeffafça düşürülür |
| 🌍 **Çok havalimanlı** | Her havalimanı kendi ölçeği, gişe ve personel yapılandırması ve saat dilimiyle bağımsız hesaplanır |
| 🔄 **Canlı veri** | AirLabs API entegrasyonu; worker 5 dakikada bir veriyi yenileyip tahminleri günceller |
| 🧹 **Kontrollü veri saklama** | 48 saatlik operasyonel saklama, iki ayrı güvenlik anahtarıyla (varsayılan kapalı) |
| ✅ **Kapsamlı testler** | 76 test dosyası: matematiksel doğrulama, gerçekçi uçuş senaryoları, uçtan uca ve arayüz testleri |

## Mimari

```mermaid
flowchart LR
    AL[AirLabs API<br/>schedules + flights] --> W
    F[Kaynak dosyalar<br/>data/] --> W
    W[Worker<br/>app.worker<br/>5 dk'da bir] --> P[Pipeline<br/>ingestion → refresh → tahmin]
    P --> DB[(MySQL)]
    DB --> API[Web sunucusu<br/>app.web.server<br/>salt okunur]
    API --> UI[Airport Queue Monitor<br/>web arayüzü]
```

Worker ve web sunucusu **iki ayrı süreçtir**: biri çökerse diğeri etkilenmez. Web sunucusu yalnızca veritabanından okur; hesaplama her zaman worker tarafındadır. Worker bir turda hata alırsa son başarılı tahminler gösterilmeye devam eder.

### Tahmin akışı

```mermaid
flowchart TD
    A[Uçuş tarifesi] --> B[Normalize + uçak tipi eşleştirme]
    B --> C[Uçak tipi → koltuk kapasitesi]
    C --> D[Yolcu talebi<br/>load factor, varış süresi, gecikme]
    D --> E[Akış kuralları<br/>iç hat / dış hat · kalkış / varış]
    E --> F[15 dakikalık pencereler]
    F --> G[Kuyruk modeli<br/>Erlang-C · olay tabanlı simülasyon]
    G --> H[Risk + bekleme süresi]
    G --> I[9 neden tespiti]
    G --> J[Confidence skoru]
    H & I & J --> K[(queue_predictions)]
```

## API

Sunucu yalnızca `GET` isteklerini kabul eder.

| Uç nokta | Döndürdüğü |
|---|---|
| `GET /` | Web arayüzü |
| `GET /api/airports` | Takip edilen havalimanları (`["ADB", "IST", "SAW"]`) |
| `GET /api/airports/directory` | Havalimanı adlarıyla liste |
| `GET /api/airports/{iata}/predictions` | Genel durum, security, pasaport ve kırılımlar |

## Hızlı başlangıç

### Gereksinimler

- Python 3
- MySQL 8 (ya da PostgreSQL)
- AirLabs API anahtarı (sadece canlı veri için)

### Kurulum

```bash
git clone https://github.com/eceboylu/aircraft-capacity-py.git
cd aircraft-capacity-py

python -m venv venv
venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
```

`DATABASE_URL` **zorunludur**; tanımlı değilse uygulama açık bir hatayla durur, gizlice SQLite'a geçmez.

```bash
# Windows (PowerShell)
$env:DATABASE_URL = "mysql+pymysql://kullanici:sifre@localhost:3306/airport_queue?charset=utf8mb4"
```

### Çalıştırma

```bash
# 1) Uçak tipi → kapasite referans verisi (ayrı bir süreçte çalıştırın)
venv\Scripts\python -m app.seed

# 2) Tek seferlik: kaynak veri → veritabanı → tahminler
venv\Scripts\python -m app.queue.pipeline

# 3) Web arayüzü ve API  →  http://localhost:8000
venv\Scripts\python -m app.web.server

# 4) Sürekli güncelleme (5 dakikada bir)
venv\Scripts\python -m app.worker

# Testler
venv\Scripts\python -m pytest -q
```

### Ortam değişkenleri

| Değişken | Zorunlu | Açıklama |
|---|---|---|
| `DATABASE_URL` | Evet | `mysql+pymysql://…` ya da `postgresql://…` |
| `AIRLABS_API_KEY` | Canlı veri için | AirLabs API anahtarı; koda ve loglara hiçbir zaman yazılmaz |
| `RETENTION_ENABLED` | Hayır | `true` olursa eski kayıtlar temizlenir (varsayılan `false`) |
| `RETENTION_DRY_RUN` | Hayır | `false` yapılmadan hiçbir şey silinmez, sadece raporlanır (varsayılan `true`) |
| `LOG_LEVEL` | Hayır | Varsayılan `INFO` |

Örnek dosya: [`deploy/systemd/aircraft-capacity.env.example`](deploy/systemd/aircraft-capacity.env.example). Gerçek değerler repoya **asla** eklenmez.

## Production

Linux sunucuda iki `systemd` servisi çalışır:

| Servis | Görevi |
|---|---|
| `airport-queue-web.service` | Web arayüzü ve API (`app.web.server`) |
| `airport-queue-worker.service` | 5 dakikada bir veri yenileme ve tahmin (`app.worker`), tek kopya kilidiyle |

Kurulum adımları: [`deploy/systemd/README.md`](deploy/systemd/README.md)

## Proje yapısı

```
app/
├── models.py, service.py, seed.py   Uçak tipi → kapasite çözümleyici
├── db.py                            Veritabanı bağlantısı (DATABASE_URL zorunlu)
├── worker.py                        Arka plan yenileme süreci
├── web/
│   ├── server.py                    Salt okunur HTTP sunucusu + JSON API
│   └── static/index.html            Web arayüzü
└── queue/
    ├── core/                        Erlang-C, olay tabanlı kuyruk, skorlama (saf matematik)
    ├── domain/                      Talep, akış kuralları, havalimanı ölçeği, personel
    ├── reasons/                     Neden tespit kuralları
    ├── ingestion/                   AirLabs istemcisi, kaynak ayrıştırma, upsert
    ├── engine.py                    Çok havalimanlı orkestrasyon
    ├── pipeline.py                  Uçtan uca çalıştırıcı
    ├── reporting.py, api.py         Raporlama ve API veri katmanı
    └── retention.py                 Kontrollü veri saklama
data/                                Örnek kaynak veriler, havalimanı ve uçak listeleri
deploy/systemd/                      Servis ve timer dosyaları
tests/                               Birim, senaryo, uçtan uca ve arayüz testleri
```

---

# Teknik dokümantasyon

Aşağıdaki bölümler modelin temel tasarım kararlarını ve varsayımlarını anlatır. Proje geliştikçe bazı ayrıntılar değişmiştir; güncel davranışın kaynağı koddaki açıklamalar ve testlerdir.

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
