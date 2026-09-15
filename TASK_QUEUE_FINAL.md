TASK_QUEUE_FINAL.md

GÖREV: Havalimanı Security / Passport Yoğunluk Tahmin Sistemi — Finalizasyon, E2E Test ve Production Hardening

ÇALIŞMA KURALI

Bu dosyadaki maddeleri kesinlikle sırayla uygula.

Genel kurallar:

Önce mevcut kodu ve testleri incele.

Mevcut mimariyi gereksiz yere bozma.

Aynı işi yapan ikinci bir servis/model oluşturma.

Mevcut AircraftCapacityService kapasite çözümünü kullan; queue tarafında kapasite mantığını kopyalama.

Mevcut çalışan testleri koru.

Her aşamadan sonra ilgili testleri çalıştır.

Bir aşama başarısızsa sonraki aşamaya geçmeden önce düzelt.

Testlerde gerçek üretim verisini değiştirme.

Gerçek olmayan canlı uçuş verisi varmış gibi davranma.

Test verilerini açıkça TEST verisi olarak işaretle.

Magic number kullanma; mevcut config/constants yapısını kullan veya uygun constant ekle.

Production kodunda developer bilgisayarı/path'i kullanma.

Gereksiz büyük refactor yapma.

Bana bütün dosyaları baştan sona yazdırma. Sadece:

yapılan değişiklik,

değişen dosyalar,

test sonucu,

varsa hata/bloker
şeklinde kısa rapor ver.

Her aşamada PASS olmadan sonraki aşamaya geçme.

AŞAMA 0 — ÖNCE ANALİZ, KOD DEĞİŞTİRME

İlk olarak repository'yi incele.

Özellikle şunları bul:

aircraft capacity service

airline fleet seat config

airport model/importer

flight model/upsert

flight event

demand calculator

effective time hesaplama

15 dakikalık window mantığı

security prediction

passport prediction

reason/explanation

confidence

QueuePrediction

baseline/history

test yapısı

mevcut seed'ler

mevcut DB/session yapısı

mevcut logging yapısı

Sonra:

mevcut mimariyi özetle

aşağıdaki 8 ana maddenin hangileri zaten yapılmış belirt

eksik olanları dosya ve fonksiyon adıyla belirt

mevcut test sayısını ve durumunu belirt

riskli/gereksiz değişiklikleri belirt

Bu aşamada kod değiştirme.

MADDE 1 — PASSPORT STAFF PER COUNTER

Passport kapasite hesabında:

c = passport_counter_count

staff_per_counter yalnızca mu değerini etkilemeli.

Formül:

mu*per_counter =
passport_service_rate_per_staff
* passport*staff_per_counter
* passport_efficiency_multiplier

total_capacity_rate =
passport_counter_count \* mu_per_counter

rho =
lambda / total_capacity_rate

Örnek:

4 counter
2 staff/counter
1.5 passenger/min/staff
efficiency = 1.0

mu*per_counter = 1.5 * 2 \_ 1.0 = 3
total_capacity_rate = 4 \* 3 = 12 passenger/min

Kurallar:

passport_staff_count tekrar çarpılıp kapasite iki kez büyütülmemeli.

c personel sayısı değil counter sayısı olmalı.

Staff count ile counter \* staff_per_counter uyuşmuyorsa warning/validation üretilebilir.

Ama staff sayısını ikinci kez kapasiteye çarpma.

Test:

1 staff/counter

2 staff/counter

counter sayısı değişimi

staff count mismatch

rho hesabı

MADDE 2 — PASSPORT COUNTER MODEL

Passport Erlang-C modelinde:

c = passport_counter_count

olmalı.

staff_per_counter yalnızca mu değerini değiştirmeli.

Aşağıdaki hatanın olmadığını test et:

c = counter_count \* staff_per_counter

Bu yanlış.

Doğru:

c = counter*count
mu = service_rate_per_staff * staff*per_counter * efficiency

Mevcut Erlang-C mantığını bozma.

MADDE 3 — 15 DAKİKALIK BASELINE IDEMPOTENCY

Aynı airport + process + 15 dakikalık window ikinci kez baseline'a yazılmamalı.

Minimum identity:

airport_iata + process + window_start

Örnek:

18:00 - 18:15

18:07 refresh:

18:00-18:15 henüz kapanmadı

baseline'a yazma.

18:16 refresh:

18:00-18:15 kapandı

baseline'a yazılabilir.

Aynı window 100 kez refresh edilse bile:

1 observation

olmalı.

Kurallar:

sadece CLOSED windows baseline'a girmeli

açık window baseline'a girmemeli

aynı observation tekrar insert edilmemeli

mümkünse DB unique constraint / idempotent upsert kullan

mevcut mimariye en uygun çözümü seç

Test:

aynı window 5+ kez refresh

açık window

kapanmış window

farklı process aynı window

farklı airport aynı window

MADDE 4 — AIRLINE FLEET SEAT CONFIG GERÇEKTEN KULLANILMALI

AirlineFleetSeatConfig, AircraftCapacityService.resolve() üzerinden gerçekten kullanılmalı.

Öncelik:

1. exact airline + aircraft ICAO
2. airline + ICAO weighted fleet average
3. aircraft_capacity
4. family fallback
5. unknown/default

Weighted average:

SUM(seats \* fleet_count) / SUM(fleet_count)

Test:

exact match

weighted average

aircraft_capacity fallback

family fallback

unknown aircraft

passenger aircraft / GA / freighter ayrımı

GA/freighter yolcu talebine dahil edilmemeli.

Kapasite queue kodunda tekrar hesaplanmamalı.

MADDE 5 — ARRIVAL FLIGHT KEY / OPERATIONAL TIME

Departure için operasyonel scheduled time:

dep_scheduled_utc

Arrival için:

arr_scheduled_utc

Flight key estimated/actual değişimlerinden etkilenmemeli.

Örnek:

TK_123_2026-09-15

Key scheduled operational date üzerinden oluşturulmalı.

Test:

estimated değişince key aynı

actual değişince key aynı

arrival date doğru

midnight boundary

missing scheduled time

aynı gün flight-number uniqueness kontrolü

Arrival effective time için scheduled/estimated/actual source mapping'i doğru olmalı.

MADDE 6 — GERÇEK MYSQL AIRPORT SQL PARSER

queue/ingestion/airports_import.py içindeki:

parse_airports_sql()

gerçek flight_airports.sql dosyasıyla test edilmeli.

Özellikle MySQL escaping:

\'
\\
\n
\r
\"
''

kontrol edilmeli.

Parse edilen alanlar:

airport name

IATA

ICAO

country_code

city_code

timezone

Customized JSON alanları bozuksa:

import tümüyle crash olmamalı

ilgili alan None kalmalı

veri uydurulmamalı

Duplicate IATA durumunda davranış deterministik olmalı.

DB import tekrar çalıştırıldığında:

session.merge()

veya mevcut eşdeğer idempotent yöntem ile duplicate oluşmamalı.

Mümkünse gerçek SQL fixture test kullan.

MADDE 7 — SECURITY RISK: FLIGHT + PASSENGER DEMAND

Security risk yalnızca flight count'a göre hesaplanmamalı.

İki oran kullanılmalı:

flight_ratio =
current_flight_count / historical_flight_count

passenger_ratio =
current_expected_passengers / historical_expected_passengers

Historical baseline'da passenger bilgisi yoksa:

passenger_ratio = None

Fake veri üretme.

Ağırlıkları constant/config üzerinden kullan:

SECURITY_FLIGHT_RATIO_WEIGHT
SECURITY_PASSENGER_RATIO_WEIGHT

Magic number kullanma.

Beklenen davranış:

düşük flight + düşük passenger

→ düşük risk

yüksek flight + yüksek passenger

→ daha yüksek risk

yüksek flight + düşük passenger

→ clustering / flight density etkisi

düşük flight + yüksek passenger

→ widebody / passenger density etkisi

Security tarafında Erlang-C kullanma.

Security kapasitesi bilinmiyorsa:

estimated_wait_minutes = None

Wait time uydurma.

MADDE 8 — AIRCRAFT CHANGE EVENTLERİ

Bir window içinde bütün aircraft change eventleri korunmalı.

Yanlış:

window -> sadece son aircraft change

Doğru:

flight_key -> [
(A320, A321),
(A321, A330)
]

Aynı refresh sırasında aynı state tekrar görülürse duplicate event oluşturma.

Aircraft change:

explanation/reason olabilir

doğrudan risk score'u artırmamalı

Capacity delta doğru hesaplanmalı:

old_capacity
new_capacity
delta

Test:

A320 -> A321 -> A330

iki değişiklik de korunmalı.

AŞAMA 9 — E2E TEST ORTAMI

Üretim verisine dokunma.

E2E için ayrı test DB / mevcut test fixture mimarisini kullan.

IST test verisi başlangıçta test DB'de yoksa oluştur.

Airport:

IATA = IST
ICAO = LTFM
name = Istanbul Airport

Gerçek airport kimliği doğru kullanılmalı.

Şehir/ülke/timezone bilgileri gerçek airport kimliğiyle uyumlu olmalı; uydurma airport bilgisi üretme.

AŞAMA 10 — IST TEST FLIGHTLARI

Test için birden fazla uçuş oluştur.

Domestic departures

TK
PC

International departures

TK
QR
EK
LH
BA
AF

International arrivals

TK
QR
EK
LH
BA

Domestic arrivals

TK
PC

Uçuş numaraları TEST verisidir; canlı uçuş iddiası oluşturma.

Aircraft:

A320
A321
B738
B38M
A330
B77W
TEST_UNKNOWN

AŞAMA 11 — AIRCRAFT CAPACITY E2E

Her test flight için:

aircraft_icao
capacity
source
confidence
counts_toward_passenger_total

raporlanmalı.

Unknown aircraft:

TEST_UNKNOWN

sistemi crash ettirmemeli.

Configured fallback varsa onu kullan.

Unknown durumunda:

unknown flow

düşük confidence

log/warning

passenger total'a dahil edilip edilmeyeceği mevcut policy'ye göre açıkça belirtilmeli

GA/freighter passenger demand'a dahil edilmemeli.

AŞAMA 12 — FLIGHT STATE E2E

Aşağıdaki durumları test et:

normal

delayed

heavily delayed

cancelled

diverted

aircraft changed

missing aircraft

domestic departure

international departure

international arrival

domestic arrival

Cancelled/diverted uçuşlar demand hesabından çıkarılmalı.

Ama reason üretmeli:

cancellation
diversion

Passenger redistribution uydurma.

AŞAMA 13 — 15 DAKİKA WINDOW TESTİ

Şu zamanları kullan:

17:45
18:00
18:05
18:10
18:14
18:15
18:20

Kural:

18:00 <= effective_time < 18:15

Dolayısıyla:

18:00 -> dahil
18:05 -> dahil
18:10 -> dahil
18:14 -> dahil
18:15 -> hariç

Boundary test mutlaka olsun.

AŞAMA 14 — DELAY / EFFECTIVE TIME

Örnek:

scheduled = 18:00
estimated = 18:40
security_buffer = 60 min

Effective:

17:40

Estimated varsa effective-time hesabında scheduled yerine estimated kullanılmalı.

Arrival için source precedence'i mevcut business rule'a göre doğrula:

actual / estimated / scheduled

ve passport release buffer doğru uygulanmalı.

Test:

scheduled only

estimated

actual

estimated -> actual update

midnight crossing

AŞAMA 15 — SECURITY E2E

Security sadece departure flow'u dikkate almalı.

Arrivals security demand'a dahil edilmemeli.

Kontrol et:

expected_passengers
flight_ratio
passenger_ratio
risk
estimated_wait_minutes
reasons
confidence

Security için Erlang-C kullanılmamalı.

Capacity unknown ise wait:

None

olmalı.

AŞAMA 16 — PASSPORT E2E

Passport demand:

dahil

international departure

international arrival

hariç

domestic departure

domestic arrival

Örnek capacity:

4 counters
2 staff/counter
1.5 passenger/min/staff
efficiency = 1.0

Sonuç:

mu_per_counter = 3
total_capacity_rate = 12

Demand senaryoları oluştur:

capacity altında

capacity'ye yakın

capacity üzerinde

Beklenen:

rho < 1

→ Erlang-C hesaplanabilir ve wait üretilebilir.

rho >= 1

→ CRITICAL

→ wait:

None

Staff double-counting olmadığını doğrula.

AŞAMA 17 — HIGH DENSITY / CLUSTERING

Historical:

5 flights

Current:

10 flights

Flight ratio:

2.0

Clustering reason oluşmalı.

Ayrıca:

2 x A330
2 x B77W

gibi widebody yoğunluğu oluştur.

Widebody reason oluşmalı.

Aircraft change tek başına risk artırmamalı.

AŞAMA 18 — DELAY COMPRESSION

Örnek:

A: 17:00 -> 18:05
B: 17:20 -> 18:08
C: 17:30 -> 18:12

Effective time'ları hesapla.

Aynı kısa pencereye sıkışan uçuşları tespit et.

Reason:

delay_compression

oluşmalı.

Karşılaştır:

tek delayed flight

birden fazla delayed flight

Birden fazla gecikmenin clustering/compression etkisi görünmeli.

AŞAMA 19 — AIRCRAFT CHANGE E2E

Aynı flight için:

A320 -> A321 -> A330

test et.

Beklenen:

iki event korunuyor

duplicate event yok

old/new capacity doğru

delta doğru

reasons korunuyor

aircraft change tek başına risk artırmıyor

AŞAMA 20 — BASELINE E2E

IST için aynı window'u en az 5 kez refresh et.

Beklenen:

1 baseline observation

olmalı.

Açık window baseline'a girmemeli.

Window kapandıktan sonra baseline'a tam bir kez girmeli.

Farklı process aynı timestamp'te ayrı observation olabilir.

AŞAMA 21 — UNKNOWN AIRPORT

Test için ikinci bir bilinmeyen IATA kullan.

Sistem bunu sessizce gerçek airport'a dönüştürmemeli.

Beklenen:

controlled error
veya

açıkça tanımlanmış manual test fixture

Kullanıcıya/veriye gerçek airport bilgisi uydurma.

AŞAMA 22 — FULL PIPELINE E2E

Aşağıdaki zincirin gerçekten çalıştığını doğrula:

Airport
↓
Ingestion
↓
Normalization
↓
Aircraft enrichment
↓
Flight upsert
↓
FlightEvent
↓
DemandCalculator
↓
AircraftCapacityService
↓
Load factor
↓
Effective time
↓
15-minute windows
↓
Security
↓
Passport
↓
Reason
↓
Confidence
↓
QueuePrediction
↓
Baseline
↓
Report

Bir adımın diğerini bypass edip etmediğini kontrol et.

AŞAMA 23 — GERÇEK DEĞERLERİ GÖSTEREN FINAL TEST RAPORU

Sadece:

141 tests passed

deme.

Gerçek hesaplanan değerleri raporla.

IST

flight count

domestic/international

arrival/departure

SECURITY

flight count

expected passengers

historical flight count

historical expected passengers

flight ratio

passenger ratio

risk

wait

reasons

confidence

PASSPORT

flight count

expected passengers

lambda

mu per counter

total mu

c

rho

wait

risk

SAMPLE FLIGHT

En az birkaç flight için:

flight key

airline

aircraft ICAO

capacity

capacity source

confidence

load factor

passenger demand

effective time

flow

domestic/international

status

Matematiksel tutarlılığı kontrol et.

Örneğin:

mu_per_counter = 3
counter_count = 4

total_mu = 12

ise raporda gerçekten bunlar görünmeli.

AŞAMA 24 — PRODUCTION HARDENING

Final production kodunu kontrol et.

Seed

Normal çalışmada:

drop_all()

olmamalı.

Şunlar normal runtime'da olmamalı:

DROP DATABASE
DROP TABLE

Reset gerekiyorsa explicit:

--reset

gibi açık bir kullanıcı aksiyonu gerektirsin.

Logging

Production'da print() yerine logging kullan.

Seviyeler:

INFO
WARNING
ERROR

Loglanması gerekenler:

refresh

insert/update

aircraft changes

unknown aircraft

baseline update

prediction failure

invalid source data

Sensitive data loglama.

Error isolation

Tek bir hatalı flight mümkünse tüm refresh'i öldürmemeli.

Örneğin:

flight A -> valid
flight B -> malformed
flight C -> valid

ise:

A işlenir
B loglanır/skipped
C işlenir

Ama critical DB/system hatalarını sessizce swallow etme.

Developer path

Production kodunda şunları ara:

C:\Users\Ece
C:\Users\assistant
Desktop
developer-specific absolute path

Bunların tamamını kaldır.

Debug

Production:

debug = false

SQLAlchemy

Production:

echo = false

Schema

Production schema değişikliği:

migration

ile yapılmalı.

Normal runtime'da:

drop_all()
create_all()

kullanma.

Test data

Test fixture / TEST_UNKNOWN / IST E2E verileri production runtime'a sızmamalı.

AŞAMA 25 — FINAL TODO / HACK TARAMASI

Repository'de tara:

TODO
FIXME
temporary
hack
debug
print(
drop_all
create_all
DROP DATABASE
DROP TABLE
C:\Users\
Desktop\
fake data
mock
test-only

Production'a kalmış geçici kodları temizle.

Test amaçlı mocklar yalnızca test tarafında kalmalı.

FINAL CHECKLIST

Aşağıdakilerin tamamı PASS olmalı:

Passport c = counter_count

staff_per_counter sadece mu'yu etkiliyor

staff double-count yok

Erlang-C rho doğru

closed window baseline'a giriyor

open window baseline'a girmiyor

baseline idempotent

AirlineFleetSeatConfig gerçekten kullanılıyor

exact airline+ICAO çalışıyor

weighted average doğru

fallback sırası doğru

GA/freighter passenger demand'a girmiyor

arrival/departure scheduled time doğru

flight key stable

midnight boundary doğru

gerçek flight_airports.sql parser test edildi

MySQL escaping test edildi

invalid JSON crash ettirmiyor

airport import idempotent

security flight ratio var

security passenger ratio var

historical passenger yoksa ratio None

security Erlang-C kullanmıyor

unknown capacity durumunda wait None

aircraft changes overwrite edilmiyor

duplicate aircraft event oluşmuyor

capacity delta doğru

cancellation/diversion demand'dan çıkarılıyor

cancellation/diversion reason üretiliyor

delay compression çalışıyor

widebody reason çalışıyor

clustering reason çalışıyor

IST E2E çalışıyor

unknown airport kontrollü

full pipeline çalışıyor

final report gerçek değerleri gösteriyor

production seed güvenli

production logging düzgün

developer path yok

debug kapalı

SQLAlchemy echo kapalı

schema migration mantığı korunuyor

test verisi production'a sızmıyor

TODO/FIXME/hack taraması temiz

FINAL RAPOR FORMATI

En sonunda sadece şu formatta kısa ama yeterli rapor ver:

A — Changed Files

Değişen dosyalar ve her birinin neden değiştiği.

B — 8 Main Requirements

Madde

Sonuç

1 Passport staff

PASS/FAIL

2 Passport counter

PASS/FAIL

3 Baseline idempotency

PASS/FAIL

4 Fleet seat config

PASS/FAIL

5 Arrival key/time

PASS/FAIL

6 Airport SQL parser

PASS/FAIL

7 Security ratios

PASS/FAIL

8 Aircraft changes

PASS/FAIL

C — Tests

existing tests:

new tests:

E2E tests:

total:

failures:

D — IST E2E

Gerçek hesaplanan değerleri göster.

E — Mathematical Consistency

Özellikle:

lambda
mu
c
rho
wait

kontrolünü göster.

F — Baseline

open window:

closed window:

refresh count:

stored observations:

G — Production Status

logging

paths

seed safety

debug

SQL echo

migrations

test data isolation

H — Operational Assumptions

Kodda kullanılan operasyonel varsayımları açıkça listele.

Canlı veri varmış gibi gösterme.

ÖNEMLİ ÇALIŞMA SIRASI

Claude Code bu dosyayı okuduğunda doğrudan her şeyi tek seferde yapmaya çalışma.

Şu sırayı kullan:

AŞAMA 0
↓
MADDE 1-2
↓
MADDE 3-4
↓
MADDE 5-6
↓
MADDE 7-8
↓
AŞAMA 9-14
↓
AŞAMA 15-20
↓
AŞAMA 21-23
↓
AŞAMA 24-25
↓
FINAL REPORT

Her bloktan sonra test çalıştır ve PASS olmadan sonraki bloğa geçme.
