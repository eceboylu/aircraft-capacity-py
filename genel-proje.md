# MASTER TASK

# FINAL PRODUCTION VALIDATION + REALISTIC 9-AIRPORT DATASET

# + SAFE 48-HOUR OPERATIONAL RETENTION

# + DUPLICATION / REFRESH AUDIT

# + DATA DEPENDENCY AUDIT

Bu görev aircraft-capacity-py projesinin production'a yaklaşan son kapsamlı
validation / hardening aşamasıdır.

ÇOK ÖNEMLİ ANA PRENSİP:

ÇALIŞAN PRODUCTION DAVRANIŞINI GEREKSİZ YERE DEĞİŞTİRME.

Önce repository'deki mevcut gerçek implementation'ı incele.
Bir özellik zaten doğru çalışıyorsa yeniden yazma, refactor etme veya alternatif
bir sistem oluşturma.

Sadece:

- gerçek bug varsa,
- bu görevde eklenen retention davranışı mevcut sistemi etkiliyorsa,
- veya test ile kanıtlanmış bir eksik varsa

minimum gerekli değişikliği yap.

==================================================
0 — GLOBAL SAFETY / CHANGE CONTROL
==================================================

1. Repository primary source of truth'tur.
2. Prompt'taki örnekleri production truth kabul etme.
3. Mevcut production contract'larını gereksiz değiştirme.
4. Yeni paralel:
   - resolver
   - scoring
   - queue engine
   - routing system
   - ingestion system
     yazma.
5. Existing services'i bypass etme.
6. Test için production logic hard-code etme.
7. Passing behavior'ı sırf yeni fixture için değiştirme.
8. Gereksiz refactor yapma.
9. Test sonucu görmeden PASS deme.
10. Full pytest 0 failed olmadan final PASS verme.
11. Çalıştırmadığın şeyi çalıştırmış gibi raporlama.
12. False-positive validation üretme.
13. Shared test DB ile isolated destructive retention DB'yi karıştırma.

==================================================
1 — EXISTING PRODUCTION CONTRACTS: DO NOT BREAK
==================================================

Aşağıdaki mevcut contract'ları önce doğrula ve gereksiz yere değiştirme:

AircraftCapacityService
airport scale resolver
airport resource resolver
queue routing
event-driven simulation
event-derived backlog
queue pressure
event wait
passport → security coupling
timezone
operational day
airport discovery
current-day isolation
24-hour graph contract
frontend API contract
frontend polling
5-minute source refresh
worker prediction loop
flight upsert/idempotency
HistoricalFlightCount baseline

Mevcut visible physical processes:

passport_departure
passport_arrival
security_domestic
security_intl

Legacy:

PROCESS_PASSPORT
PROCESS_SECURITY

user-visible graph/risk hesabına geri sızmamalı.

==================================================
2 — PRODUCTION FILE SAFETY
==================================================

Production:

database.sqlite

üzerinde:

- test data insert etme
- reset yapma
- recreate etme
- retention cleanup çalıştırma
- delete yapma

Production:

data/\*

üzerinde:

- delete
- truncate
- overwrite
- age-based cleanup

YAPMA.

Final:

Production database.sqlite touched:
NO

Production data/\* destructively modified:
NO

Files actually deleted from data/:
NONE

==================================================
3 — SHARED TEST DB
==================================================

Main validation DB:

tests/incoming_2026_09_19/incoming_2026_09_19.sqlite

Bu DB:

- reset_db=True ile recreate edilmeyecek
- unlink edilmeyecek
- drop_all yapılmayacak
- destructive fixture setup ile sıfırlanmayacak

Daha önce destructive shared-DB testleri tmp_path/tmp_path_factory ile
izole edildi.

Bu contract korunmalı.

==================================================
4 — EXISTING 5-MINUTE REFRESH CONTRACT
==================================================

ÖNEMLİ:

Mevcut sistem yaklaşık 5 dakikada bir source/API verisini refresh ediyor.

Bu görevde mevcut refresh cadence'i değiştirme.

Eğer repository inspection doğrularsa mevcut zincir aynen kalsın:

source/API refresh
→ refresh_flights
→ predictions update
→ API
→ frontend polling

Yaklaşık:

5 MINUTES

Bu mekanizma:

RETENTION CLEANUP İLE AYNI ŞEY DEĞİLDİR.

İkisini birbirine bağlama.

==================================================
5 — 5-MINUTE REFRESH DUPLICATION AUDIT
==================================================

Mevcut kodu ÖNCE incele.

Soru:

Aynı flight upstream source tarafından art arda refresh'lerde tekrar gelirse
mevcut code duplicate Flight oluşturuyor mu?

Özellikle incele:

flight_key
unique constraint
normalize_flight_number
refresh_flights
upsert/update logic
FlightEvent change-only insertion
idempotency tests

Senaryo:

T0:
flight source'ta geliyor.

T+5 dakika:
aynı flight tekrar geliyor.

T+10 dakika:
aynı flight tekrar geliyor.

Beklenen:

Flight row count artmamalı.

Aynı flight:

- yeni duplicate Flight olmamalı
- aynı process passenger demand'e iki kere eklenmemeli
- QueuePrediction'da aynı passenger iki kez sayılmamalı

FlightEvent:
yalnız gerçek alan değişikliği varsa yeni event oluşturması mevcut contract ise
o davranış korunmalı.

EĞER mevcut kod bunu zaten doğru önlüyorsa:

KODU DEĞİŞTİRME.

Final report:

5-MIN REFRESH DUPLICATION PROTECTION:
ALREADY SAFE / FIXED / FAIL

CURRENT MECHANISM:
...

CODE CHANGE REQUIRED:
YES / NO

==================================================
6 — RETENTION VE REFRESH TAMAMEN BAĞIMSIZ
==================================================

Source refresh:

approximately every 5 minutes

Retention cleanup:

every 48 hours

Bunlar ayrı lifecycle'dır.

5-minute refresh:
yeni/recent flight data almak içindir.

48-hour cleanup:
expired raw operational records'ı temizlemek içindir.

Birinin schedule/failure durumu diğerini etkilememeli.

Refresh loop retention yüzünden beklememeli veya durmamalı.

==================================================
7 — EXACT OPERATIONAL RETENTION CONTRACT
==================================================

Raw operational tables:

Flight
FlightEvent
QueuePrediction

için retention horizon:

48 HOURS

Conceptual cutoff:

cutoff_utc = now_utc - timedelta(hours=48)

Boundary:

timestamp < cutoff_utc
→ expired

timestamp == cutoff_utc
→ retained

timestamp > cutoff_utc
→ retained

Calendar-day shortcut kullanma.

UTC instant kullan.

==================================================
8 — IMPORTANT: "MAX 2 DAYS USED" VS CLEANUP EVERY 2 DAYS
==================================================

Physical cleanup sadece 48 saatte bir çalışabilir.

Bu yüzden DB'de bir sonraki cleanup'ı bekleyen ve 48 saati aşmış bir raw row
geçici olarak bulunabilir.

AMA:

Operational prediction/API logic 48 saatten eski expired raw row'u
CURRENT calculation input'u olarak KULLANMAMALI.

Yani iki farklı kavram:

A) USAGE HORIZON
raw operational data older than 48h
→ production calculation için kullanılmaz.

B) PHYSICAL CLEANUP
expired raw rows
→ her 48 saatte bir DB'den temizlenir.

Bu sayede:

"operational kullanım max 2 gün"

korunurken cleanup'ı 5 dakikada bir çalıştırmak gerekmez.

==================================================
9 — RETENTION CLEANUP SCHEDULE
==================================================

Cleanup cadence:

ONCE EVERY 48 HOURS

NOT:

- every 5 minutes
- every refresh
- every prediction
- hourly
- daily

Existing worker architecture uygun ise minimum integration yap.

Ama mevcut worker refresh loop'unu yeniden tasarlama.

Cleanup exception:

- refresh loop'u öldürmez
- prediction loop'u öldürmez
- transaction rollback olur
- error loglanır
- sonraki cleanup cycle'da tekrar denenebilir

Retention cleanup kendi başına idempotent olmalı.

==================================================
10 — RETENTION CONFIG
==================================================

Mevcut config style'ına uygun olarak destekle:

FLIGHT_RETENTION_DAYS=2
FLIGHT_EVENT_RETENTION_DAYS=2
QUEUE_PREDICTION_RETENTION_DAYS=2

RETENTION_ENABLED
RETENTION_DRY_RUN

Ama destructive cleanup'ı production'da sessizce enable etme.

Mevcut deployment/config behavior'ını önce incele.

Yeni destructive default eklemek gerekiyorsa güvenli rollout yaklaşımını kullan
ve final raporda production activation'ın nasıl yapılacağını açıkla.

==================================================
11 — PERSISTENT TABLES
==================================================

Retention şu tablolara uygulanmaz:

HistoricalFlightCount
Airport
AirportOperationalConfig
Aircraft/reference tables

Bunlar persistent kalır.

==================================================
12 — HISTORICALFLIGHTCOUNT EXACTLY-ONCE SAFETY
==================================================

ÇOK ÖNEMLİ:

Mevcut architecture'ta historical observation zaten:

record_observation()

veya repository'deki equivalent mechanism ile
HistoricalFlightCount'a commit ediliyorsa:

RETENTION CLEANUP AYNI RAW DATA'YI TEKRAR AGGREGATE ETMEMELİ.

Cleanup'ın görevi yeni baseline üretmek değildir.

Önce mevcut flow'u incele.

Required historical information zaten committed ise:

raw expired row delete için eligible olabilir.

Eğer historical requirement henüz committed değilse:

row'u DELETE ETME.
SKIP et.

Ama sırf cleanup yapmak için aynı Flight'ı HistoricalFlightCount'a ikinci kez
yazma.

Exactly-once historical contribution korunmalı.

Assert:

retention cleanup öncesi
HistoricalFlightCount snapshot

vs

retention cleanup sonrası

already-observed rows için değişmemeli.

Final:

HISTORICAL DOUBLE AGGREGATION:
PASS / FAIL

==================================================
13 — RETENTION DOMAIN TIMESTAMPS
==================================================

Flight için sadece created_at kullanmak doğru olmayabilir.

Repository'deki domain'i incele.

Future scheduled flight'ın eski created_at yüzünden yanlış silinmesini önle.

Flight retention için doğru canonical operational timestamp'i belirle:

scheduled/effective operational time
veya mevcut architecture'taki doğru alan.

FlightEvent için gerçek event timestamp.

QueuePrediction için gerçek prediction window timestamp / operational semantics.

Seçimi final raporda açıkla.

==================================================
14 — CURRENT / ACTIVE / FUTURE SAFETY
==================================================

Retention hiçbir zaman yanlışlıkla silmemeli:

current active records
future scheduled flights
active prediction dependencies
required-but-not-committed historical information

Future flight:

old created_at

- future scheduled time

→ PRESERVED

olmalı.

==================================================
15 — RE-INGEST LOOP AUDIT
==================================================

Scenario:

expired old Flight
→ retention cleanup deletes raw row
→ upstream API aynı old flight'ı hâlâ döndürüyor
→ 5-minute refresh_flights çalışıyor

Expected:

expired flight operational DB'ye sonsuza kadar tekrar tekrar girip
silinen/reinsert edilen loop oluşturmamalı.

ÖNCE source horizon'u ve mevcut ingestion behavior'ını araştır.

Tercih:

existing timestamp/source horizon filter veya minimal ingestion cutoff.

Mevcut code zaten old/out-of-horizon rows'u ignore ediyorsa:
KODU DEĞİŞTİRME.

Yeni tombstone table sırf testi geçirmek için EKLEME.

Tombstone yalnız:

- source horizon yeterli değilse
- existing upsert bunu çözemiyorsa
- necessity açıkça kanıtlandıysa

ayrı gerekçe ile düşünülebilir.

Final:

NO RE-INGEST LOOP:
PASS / FAIL

MECHANISM:
...

==================================================
16 — RETENTION DELETE SAFETY
==================================================

Actual destructive retention tests:

SADECE isolated temporary SQLite DB üzerinde.

Shared DB üzerinde bu master task'ta:

INVENTORY

- DRY RUN

yap.

Shared validation DB üzerinde DELETE çalıştırma.

DELETE = 0.

Shared DB retention behavior'ı:

candidate calculation
current/future protection
persistent-table protection
cutoff correctness

ile doğrulanmalı.

==================================================
17 — FK SAFETY
==================================================

Gerçek relationships'i incele.

FlightEvent → Flight gibi FK varsa:

children first
then parent

veya mevcut safe cascade contract'ı.

Assert:

no orphan
no FK violation

==================================================
18 — BATCH DELETE
==================================================

Retention implementation milyonlarca row için tek massive DELETE yapmamalı.

Batching kullan.

Batch size repository/deployment expectation'a göre gerekçelendir.

Random sayı seçme.

==================================================
19 — SQLITE MAINTENANCE
==================================================

Inspect:

PRAGMA auto_vacuum
journal mode
WAL
checkpoint strategy
freelist
VACUUM behavior

Her cleanup sonrası full VACUUM çalıştırma.

Safe periodic maintenance recommendation üret.

==================================================
20 — RETENTION ISOLATED TEST MATRIX
==================================================

Temporary DB'de:

10 days old
5 days old
3 days old
49 hours old
exactly 48 hours old
47 hours old
yesterday
today
future

records üret.

Flight
FlightEvent
QueuePrediction

için test et.

Expected:

10d → removed
5d → removed
3d → removed
49h → removed

48h exactly → preserved
47h → preserved
yesterday → preserved
today → preserved
future → preserved

HistoricalFlightCount → preserved
Airport → preserved
AirportOperationalConfig → preserved
Aircraft reference → preserved

==================================================
21 — CURRENT-DAY REGRESSION AFTER RETENTION
==================================================

Retention code eklendikten sonra mevcut:

current predictions
airport directory
operational day
timezone
24 buckets
queue pressure
wait
5 visible graphs

bozulmamalı.

==================================================
22 — PART A: REALISTIC 9-AIRPORT DATASET
==================================================

2026-09-21 için:

3 LARGE
3 MEDIUM
3 SMALL

toplam 9 gerçek airport seç.

UNKNOWN kullanma.

Scale source:

production scale resolver.

Airport'ları kendi yorumuna göre scale'e atama.

Farklı ülke/timezone tercih et.

Public annual passenger throughput bulunabilen airport'lar seç.

==================================================
23 — CURRENT-DAY FIXTURE COLLISION SAFETY
==================================================

9 airport seçmeden ÖNCE shared DB'de:

2026-09-21

için halihazırda synthetic/test flight bulunan airport'ları çıkar.

Mevcut known test data örnekleri olabilir:

IST
CBR
MFG
OAG
EAP

Ama repository/DB gerçek durumu source of truth'tur.

Yeni realistic fixture mevcut current-day synthetic fixture ile yanlışlıkla
üst üste bindirilmemeli.

Tercih:

2026-09-21 için current synthetic data olmayan airport seç.

Eğer airport'ta zaten 21 Sep test data varsa:

- mevcut row'ları silme
- airport'u temizleme
- başka uygun airport seç

New realistic dataset + unrelated old fixture totals birleşip
"realistic daily total" olarak raporlanmamalı.

==================================================
24 — REAL PASSENGER THROUGHPUT
==================================================

Her 9 airport için mümkünse en güncel tamamlanmış annual passenger throughput
bul.

Source priority:

1. airport official
2. airport authority
3. government / aviation authority
4. official statistics

Mümkünse 2025.
Yoksa en güncel tamamlanmış yıl.

Kaynak bulunamıyorsa sayı UYDURMA.

Report:

Annual passengers:
Source:
Source year:

Average daily:

annual_passengers / actual_year_day_count

Leap year = 366.

==================================================
25 — REAL VS SYNTHETIC
==================================================

REAL/REFERENCE:

airport identity
IATA
ICAO
timezone
scale
annual throughput
average daily passengers
aircraft reference capacity

SYNTHETIC:

flight number
schedule
banks
route
estimated/actual timing
fixture traffic pattern

Generated timetable'ı real published timetable olarak sunma.

==================================================
26 — IMPORTANT MODEL REALISM LIMITATION
==================================================

Mevcut production demand modeli eğer:

AircraftCapacityService.resolve(...)
→ resolved seat capacity
→ passenger demand

kullanıyorsa:

fixture passenger demand gerçek boarded passenger count değildir.

Load factor geri getirme.

0.78 / 0.82 / 0.84 / 0.88

gibi eski load factor'ları yeniden ekleme.

Dataset'i:

THROUGHPUT-CALIBRATED SYNTHETIC VALIDATION DATASET

olarak tanımla.

Annual passenger throughput:
REAL REFERENCE

Generated production demand:
SYNTHETIC MODEL DEMAND

olarak ayrı raporla.

==================================================
27 — DAILY TARGET
==================================================

Her airport:

target_daily_passengers =
annual / days_in_year

Fixture'ın production-resolved demand'i buna makul yakın olsun.

Bunu sağlamak için:

flight count
aircraft mix
traffic distribution

ayarla.

Production demand formula'yı değiştirme.

==================================================
28 — AIRCRAFT MIX
==================================================

Airport-specific realistic aircraft mix oluştur.

Large:
narrowbody + appropriate widebody

Medium:
mostly narrowbody + regional + limited realistic widebody

Small:
regional + turboprop + suitable narrowbody

Her ICAO production capacity resolver tarafından çözülmeli.

Fallback varsa raporla.

==================================================
29 — PRODUCTION JSON SCHEMA
==================================================

Existing:

Delays - Type Departures.json
Delays - Type Arrivals.json
flights_live.json

schema'sını kullan.

Yeni schema icat etme.

Parser'ı fixture'a uydurma.

Fixture'ı mevcut production parser'a uydur.

==================================================
30 — 24-HOUR TRAFFIC DISTRIBUTION
==================================================

Her airport local timezone'a göre:

morning bank
midday
afternoon
evening bank
quiet periods

oluştur.

Tüm flights tek saate yığılmasın.

Large/Medium/Small flight count aynı olmasın.

==================================================
31 — FIXTURE OUTPUT
==================================================

Create:

tests/final_realistic_2026_09_21/

flights_live.json
Delays - Type Departures.json
Delays - Type Arrivals.json
validation_report.md
reports/

Her 9 airport için ayrı report.

Yeni persistent operational DB oluşturma.

==================================================
32 — NORMAL INGESTION PATH ONLY
==================================================

Shared DB target:

tests/incoming_2026_09_19/incoming_2026_09_19.sqlite

Use:

load_source_payload
→ existing parser
→ refresh_flights
→ airport discovery
→ AircraftCapacityService.resolve
→ routing
→ event-driven simulation
→ run_predictions

Forbidden:

direct SQL Flight insert
direct SQL QueuePrediction insert
fake resolver
hard-coded wait
hard-coded risk
hard-coded capacity
fixture-specific parallel engine

==================================================
33 — PRE-INGEST INVENTORY
==================================================

Record:

Flight count
FlightEvent count
QueuePrediction count
Airport count
Config count

min/max operational timestamp
min/max prediction window

date distribution
airport distribution

==================================================
34 — INGEST + IDEMPOTENCY
==================================================

First ingest:
record inserted/updated/failed.

Second identical ingest:

inserted = 0

Assert:

Flight count does not increase
flight_key duplicate = 0
same-process passenger duplication = 0

Bu test ayrıca 5-minute refresh idempotency audit'i ile karşılaştırılmalı.

==================================================
35 — PASSENGER / EVENT DUPLICATION
==================================================

Audit:

Flight
→ demand
→ ServiceEvent
→ demand_by_hour
→ backlog
→ wait
→ risk

Aynı passenger aynı process'te iki kere bulunmamalı.

International Departure:

Passport once
Security once after passport completion

DOĞRUDUR.

Bu cross-process duplication değildir.

Assert:

SAME-PROCESS PASSENGER DUPLICATION:
PASS / FAIL

COUPLED PROCESS FLOW:
PASS / FAIL

==================================================
36 — ROUTING
==================================================

Existing production routing source of truth.

Expected if unchanged:

Domestic Departure
→ Domestic Security

Domestic Arrival
→ no queue

International Departure
→ Departure Passport
→ International Security

International Arrival
→ Arrival Passport

==================================================
37 — EVENT-DERIVED BACKLOG
==================================================

Use actual ServiceEvent.

At T:

arrival_time < T
AND
service_start_time > T

→ waiting backlog.

In-service passenger'ı backlog'a tekrar ekleme.

==================================================
38 — QUEUE PRESSURE
==================================================

Use current production formula if still:

queue_pressure =
(backlog_start + arrivals_in_window)
/
hour_capacity

Thresholds current code'dan oku.

Do not alter unless repository shows otherwise.

Also retain incoming/utilization metric if current code does.

==================================================
39 — WAIT
==================================================

Do not change.

ServiceEvent:

wait =
service_start_time - arrival_time

Hourly:

sum(wait \* passenger_count)
/
sum(passenger_count)

==================================================
40 — AIRPORT SCALE / CAPACITY
==================================================

Use production resolver.

Read:

Scale
Departure Passport servers
Arrival Passport servers
Domestic Security lanes
International Security lanes

Do not hard-code production output from prompt examples.

==================================================
41 — SCALE MATHEMATICAL VALIDATION
==================================================

For same:

backlog
arrivals
service time
window

only resolved production capacity differs.

Assert:

capacity_A > capacity_B
→ pressure_A <= pressure_B

for each physical process where comparable.

==================================================
42 — SAMPLE FLIGHT TRACE
==================================================

For each 9 airport:

choose meaningful busy local hour.

Report at least 3 sample flights.

For each:

raw JSON
flight
airline
aircraft
direction
domestic/international
scheduled local
scheduled UTC
effective queue arrival
routing
resolved capacity
capacity source
confidence
passenger demand

==================================================
43 — PASSPORT → SECURITY TRACE
==================================================

At least one generated international departure:

flight
→ passport arrival
→ passport service start
→ passport completion
→ security arrival
→ security service start
→ security wait

show numerically.

==================================================
44 — DAILY PEAKS
==================================================

Per airport:

Departure Passport
Domestic Security
International Security
Arrival Passport

For each:

local hour
arrivals
backlog
pressure
wait
risk

==================================================
45 — FRONTEND / AUTO-DISCOVERY
==================================================

IMPORTANT:

Existing frontend polling already works if repository confirms it.

DO NOT rewrite it.

Inspect only.

If current code already:

setInterval(loadAll, POLL_MS)

and each poll re-fetches airport directory and predictions:

KODU DEĞİŞTİRME.

Validate:

new airport discovered
directory sees it
no duplicate selector entry
current selection preserved
airport selectable
5 graph payload
24 current-day buckets

Browser açma.

Automated code/test/harness level verification only.

Final:

FRONTEND AUTO UPDATE WITHOUT F5:
PASS / FAIL / NOT BROWSER-EXECUTED

==================================================
46 — DATA FILE DEPENDENCY AUDIT
==================================================

Audit:

data/buyuk_olcekli_havaalanlari.txt
data/orta_olcekli_havaalanlari.txt
data/kucuk_olcekli_havaalanlari.txt
data/yolcu_ucaklari.json
data/response-delays.json
data/Delays - Type Arrivals.json
data/Delays - Type Departures.json
data/created_fallback.json
data/flight_airports.sql

Search repository-wide:

exact refs
loaders
runtime
bootstrap
seed
tests
debug
worker
imports

Classify each:

CORE_RUNTIME_REFERENCE
BOOTSTRAP_SEED
LIVE_SOURCE_SAMPLE
TEST_FIXTURE
DEBUG_SAMPLE
LEGACY_UNUSED
UNKNOWN_NEEDS_REVIEW

Do not delete anything.

==================================================
47 — TARGETED TESTS
==================================================

Run targeted tests for:

5-minute refresh idempotency
flight upsert
same-process duplication
capacity resolution
routing
event queue
event backlog
queue pressure
wait
passport-security coupling
airport scale
current-day isolation
timezone
24 buckets
new airport discovery
frontend polling contract
retention cutoff
48h boundary
future flight preservation
persistent tables
HistoricalFlightCount no-double-aggregation
re-ingest loop prevention
FK safety
retention dry-run

==================================================
48 — FULL PYTEST
==================================================

Then:

.\venv\Scripts\python.exe -m pytest -ra

0 failed required.

If failure:

find root cause
make minimum required fix
rerun targeted
rerun full pytest

Do not weaken unrelated assertions just to get green.

==================================================
49 — POST-PYTEST SHARED DB INTEGRITY
==================================================

After full pytest verify:

shared DB not reset
new 9-airport fixture remains
existing recent/current data remains
no unexpected duplicate flights
current-day predictions remain
persistent tables remain

Shared DB retention was DRY RUN only:

no destructive retention deletion should have happened.

==================================================
50 — FINAL REPORT
==================================================

Report:

A) EXISTING REFRESH

Current refresh cadence:
...

5-MIN REFRESH CODE CHANGED:
YES / NO

If NO:
reason = existing mechanism already correct

5-MIN REFRESH DUPLICATION PROTECTION:
PASS / FAIL

Flight duplicate count:
...

Same-process passenger duplication:
PASS / FAIL

B) RETENTION

Operational usage horizon:
48 HOURS

Physical cleanup cadence:
48 HOURS

Refresh and retention independent:
YES / NO

Flight retention:
2 DAYS

FlightEvent retention:
2 DAYS

QueuePrediction retention:
2 DAYS

HistoricalFlightCount:
PERSISTENT

Airport:
PERSISTENT

AirportOperationalConfig:
PERSISTENT

Aircraft/reference:
PERSISTENT

Historical double aggregation:
PASS / FAIL

Exactly-48h boundary:
PASS / FAIL

Future protection:
PASS / FAIL

No re-ingest loop:
PASS / FAIL

Shared DB retention:
DRY RUN ONLY

Production DB cleanup executed:
NO

C) REALISTIC DATASET

List 3 LARGE
List 3 MEDIUM
List 3 SMALL

For each:
annual passengers
source
year
avg/day
fixture demand
difference %
flight count
peak backlog
peak pressure
peak wait
peak risk

Fixture collision with existing 21-Sep test data:
NONE / explain

D) QUEUE INTEGRITY

Passenger duplication:
PASS / FAIL

Coupled flow:
PASS / FAIL

Scale capacity:
PASS / FAIL

Scale pressure monotonicity:
PASS / FAIL

Queue pressure dimension:
PASS / FAIL

Wait unchanged:
YES / NO

Event simulation unchanged:
YES / NO

E) FRONTEND

Existing 5-minute polling preserved:
YES / NO

New airport auto-discovery:
PASS / FAIL

Auto list update without F5:
PASS / FAIL / NOT BROWSER-EXECUTED

5 graph payload:
PASS / FAIL

24 current-day buckets:
PASS / FAIL

F) DATA FILE AUDIT

Classification table for all requested data/\* files.

FILES ACTUALLY DELETED:
NONE

G) FINAL SAFETY

Production database.sqlite touched:
NO

Production data/\* destructively modified:
NO

Shared DB reset:
NO

Web server started:
NO

Worker started:
NO

Browser opened:
NO

FULL PYTEST:
X passed / Y skipped / 0 failed

Then STOP.
