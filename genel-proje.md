# CLAUDE İÇİN YÜRÜTME TALİMATI — BU KISMI ÖNCE OKU

## ROL VE ANA HEDEF

Sen, mevcut bir production Python projesi üzerinde çalışan kıdemli bir
software architect ve implementation agent'sın. Aşağıdaki gereksinimlerin
tamamını, mevcut çalışan davranışları ve veri güvenliğini koruyarak hayata
geçirmen gerekiyor.

Bu görev yalnızca analiz veya öneri görevi değildir. Repository'yi incele,
gerekli production ve test kodlarını uygula, migration gerekiyorsa güvenli
şekilde ekle, testleri çalıştır ve ancak doğrulanmış sonuçlara dayanarak final
raporunu ver.

## NORMATİF KAYNAK VE BİLGİ KAYBI YASAĞI

Aşağıdaki özgün görev metnindeki 1–69 numaralı bölümlerin TAMAMI normatif ve
zorunludur. Hiçbir maddeyi özet olduğu gerekçesiyle atlama, zayıflatma, iptal
etme veya “nice to have” kabul etme. Tekrarlanan maddeler gereksiz değildir;
aynı kritik invariant'ı farklı bağlamlarda güçlendirir.

Bu üst yürütme talimatı, aşağıdaki gereksinimleri değiştirmez. Amacı Claude'un:

- doğru sırada çalışmasını,
- çelişki gibi görünen ifadeleri doğru yorumlamasını,
- varsayım uydurmamasını,
- uygulama ve doğrulama işini yarıda bırakmamasını,
- final raporunu yalnızca kanıta dayalı vermesini

sağlamaktır.

## TALİMAT ÖNCELİĞİ

Uygulama sırasında aşağıdaki öncelik sırasını kullan:

1. Veri güvenliği, passenger conservation, airport isolation ve fiziksel
   resource doğruluğu gibi açık invariant'lar.
2. Aşağıdaki 1–69 numaralı açık business ve acceptance kuralları.
3. Mevcut production contract'ları ve doğru çalışan mevcut davranışlar.
4. Mevcut testlerin temsil ettiği davranışlar.
5. İsimlendirme, dosya yerleşimi ve implementation tercihleri.

Mevcut kod veya eski test, aşağıda açıkça değiştirilen yeni business rule ile
çelişirse yeni business rule geçerlidir. Ancak testi yalnızca yeşile çevirmek
için gevşetme; eski davranış olduğunu önce kanıtla ve yeni contract'ı net bir
regresyon testiyle kilitle.

“Minimum gerekli değişiklik” şu anlama gelir: bütün zorunlu gereksinimleri
karşılayan en küçük, mevcut mimariye en uyumlu değişiklik seti. Bu ifade hiçbir
zorunlu gereksinimi atlama izni vermez.

## BİRBİRİYLE KARIŞTIRILMAYACAK TEMEL KAVRAMLAR

Aşağıdaki ayrımlar görev boyunca korunmalıdır:

- Source refresh cadence: yaklaşık 5 dakika; yalnız dış verinin yenilenmesi.
- Queue progression: continuous/event-driven; 5 veya 60 dakikalık timestep
  değildir.
- Graph aggregation: exact event timestamp'lerinin saat başlangıcına floor
  edilmesiyle oluşan 1 saatlik bucket.
- Flight operational date: airport local timezone'a göre flight'ın ait olduğu
  operasyonel gün.
- Queue event timestamp: offset nedeniyle bir önceki veya sonraki takvim gününe
  taşabilen gerçek event zamanı.
- Physical queue/resource: gerçek kapasite, backlog ve service progression.
- Graph/reporting cohort: aynı fiziksel resource'u yeniden yaratmadan ayrı
  raporlanabilen passenger kaynağı.
- Overall airport graph: yeni bir fiziksel queue veya passenger toplamı değil,
  mevcut process sonuçlarından üretilen airport-level operational summary.

Bu kavramlardan biri için kullanılan constant, state veya hesap diğerinin
yerine kullanılamaz.

## DEĞİŞMEZ ÇEKİRDEK CONTRACT — HIZLI KONTROL

Aşağıdaki maddeler ayrıntılı 1–69 bölümlerinin yerine geçmez; uygulama boyunca
korunacak kısa kontrol listesidir:

- Her airport state'i en az `airport_iata` ile tamamen izole edilir.
- AircraftCapacityService ve mevcut resolver zinciri korunur; yalnız son global
  fallback 180 olur.
- Departure passenger airport arrival = effective departure − 120 dakika.
- International arrival passport arrival = effective arrival + 15 dakika.
- Domestic Departure → yalnız Domestic Security.
- Domestic Arrival → passport/security demand ve ayrı graph yok.
- International Departure → ortak fiziksel Passport → ayrı International
  Security; security demand yalnız gerçek passport completion anında oluşur.
- International Arrival → ortak fiziksel Passport; security yok.
- Domestic ve International Security fiziksel olarak ayrıdır ve lane sayıları
  airport-specific config'den gelir.
- Passport toplam 4 counter × 2 officer = 8 effective server, service time 1.5
  dakika ve referans capacity 320 pax/hour'dır; arrival ve departure için sahte
  iki ayrı passport havuzu yaratılmaz.
- Security service time 1 passenger / 1 dakika / lane'dir.
- Passenger kaybolamaz, yeniden yaratılamaz veya double-count edilemez.
- 24 saat dolduğu için passenger/backlog drop edilemez.
- Same-data refresh duplicate demand üretmez fakat geçen gerçek zaman boyunca
  queue çalışmaya devam eder.
- New/updated/cancelled flight delta mantığı idempotent ve atomic olmalıdır.
- Ana prediction source scope yalnız airport'un ilgili local operational day
  flight'larıdır; midnight crossing queue event'leri ve carry backlog korunur.
- API ve frontend'de her airport için tam dört ana graph vardır: Overall,
  Domestic Security, International Departure, International Arrival.
- Frontend wait hesaplamaz; backend snapshot'ını güvenli şekilde render eder.
- Hour click her graph'ta bağımsız state kullanır ve doğru bucket detayını açar.

## UYGULAMA PROTOKOLÜ

### Aşama 1 — Repository ve veri audit'i

Kod yazmadan önce:

1. Repository talimatlarını (`CLAUDE.md`, `AGENTS.md`, README ve benzeri) bul ve
   uygulanabilir olanları oku.
2. Gerçek proje ağacını, dependency/config yapısını, DB katmanını, migration
   mekanizmasını, source parser'larını, API contract'ını, frontend'i ve test
   düzenini incele.
3. `data/` altındaki gerçek dosyaları keşfet; dosya adı veya JSON alanı tahmin
   etme.
4. Mevcut working tree değişikliklerini tespit et ve kullanıcı değişikliklerini
   ezme.
5. Aşağıdaki Bölüm 40'ta istenen kısa dependency map'i kendi çalışma planında
   çıkar: CURRENT → REQUIRED CHANGE → AFFECTED MODULES → AFFECTED TESTS.
6. Mevcut effective-time, terminal status, identity/idempotency, timezone,
   database session/transaction ve queue/scoring davranışlarını kod üzerinden
   doğrula.

Audit sonucunu uygulamadan kaçmak için kullanma. Güvenli ve makul varsayımlarla
ilerleyebildiğin sürece kullanıcıdan tekrar onay bekleme.

### Aşama 2 — Tasarım ve izlenebilirlik

Implementation'dan önce 1–69 maddelerini ilgili code path ve testlerle zihinsel
olarak eşleştir. Özellikle şu kesişimleri tek bir tutarlı tasarımda çöz:

- ortak fiziksel passport pool + departure/arrival cohort reporting,
- passport completion → international security event coupling,
- exact queue time + hourly graph snapshot,
- persisted graph horizon + horizon dışındaki korunmuş backlog,
- local operational-day flight selection + midnight-crossing event timestamps,
- source refresh delta + gerçek zamanla bağımsız queue progression,
- four-graph API contract + independent frontend hour selection.

Gereksiz distributed architecture, repository çapında rewrite veya production
kodunun paralel/duplicate bir kopyasını oluşturma.

### Aşama 3 — Güvenli implementation

- Mevcut abstractions ve fonksiyonları mümkün olduğunca reuse et.
- Schema değişikliği gerekiyorsa additive migration kullan.
- `database.sqlite` dosyasını drop/reset/clean etme; gerçek flight veya mevcut
  production verisini silme.
- SQLite'a özel business logic kurma; mevcut MySQL uyumluluğunu koru.
- Event-driven eşdeğerliği koruyarak cohort/batch/heap optimizasyonu kullanmak
  serbesttir; milyonlarca ORM row üretmek zorunlu değildir.
- Missing/partial source field'larında mevcut güvenli fallback/skip/UNKNOWN
  davranışını koru; crash veya uydurma veri üretme.
- Transfer passenger bağlantısı güvenilir source verisinde yoksa tahmin üretme.
- Delay/cancellation/update işlemlerinde geçmişte gerçekten tamamlanmış service
  history'yi keyfi olarak geri alma; stale future demand'i doğru migrate/remove
  et.
- Frontend'e business math taşıma.

### Aşama 4 — Test sırası

Aşağıdaki Bölüm 37'nin test stratejisini uygula:

1. Önce static/code audit.
2. Değişen modüller için targeted tests.
3. Queue timing, coupling, conservation, double-count ve airport-isolation
   tests.
4. Refresh delta/idempotency/atomicity ve daily-scope boundary tests.
5. API ve frontend contract/hour-click tests.
6. Production-shape realistic replay.
7. Targeted testler yeşil olduktan sonra final full pytest.

Bir production bug'ı görürsen önce targeted regression test ile reproduce et,
sonra düzelt. Test sonuçlarını tahmin etme; çalıştırmadığın testi “passed” diye
raporlama.

### Aşama 5 — Tamamlama denetimi

Final yanıtından önce:

- Bölüm 48 ile Bölüm 68'deki acceptance maddelerini TEK birleşik checklist
  olarak değerlendir.
- Bölüm 49'daki final rapor formatını, Bölüm 69'daki ek alanlarla genişlet.
  Bölüm 49 metnin ortasında yer alsa da final rapor işin sonunda, Bölüm 50–69
  dahil tüm gereksinimler tamamlandıktan sonra verilir.
- PASS ancak ilgili davranış test veya sayısal kanıtla doğrulandıysa yazılır.
- Source verisi nedeniyle belirlenemeyen bir özellik için tam olarak
  `NOT DETERMINABLE FROM CURRENT SOURCE DATA` yazılır.
- Başarısız veya uygulanamayan bir maddeyi gizleme; FAIL/limitation olarak ve
  somut nedeni ile bildir.

## BELİRSİZLİK VE ÇATIŞMA YÖNETİMİ

Bir implementation ayrıntısı açıkça belirtilmemişse şu sırayı izle:

1. Mevcut production mimarisindeki kanıtı ara.
2. En dar, geri uyumlu ve reversible seçeneği tercih et.
3. Passenger conservation, physical capacity, airport isolation ve data safety
   invariant'larını ihlal etmeyen çözümü seç.
4. Testle davranışı açık hale getir.
5. Gerçekten source/contract eksikliği varsa uydurma; limitation olarak raporla.

Birbirini tekrar eden hükümleri “duplicate requirement” diye silme. Görünürde
çatışan iki ifade varsa ayrıntılı ve daha özel olan kuralı uygula; yine de her
iki amacın korunmasını sağla.

## ÇALIŞMA SIRASINDA İLETİŞİM

Uzun açıklamalarla uygulamayı erteleme. Kısa ilerleme güncellemeleri verilebilir,
ancak kullanıcıdan yeni onay beklemeden audit → implementation → targeted tests
→ full regression → final report akışını tamamla.

Final yanıt SADECE Bölüm 49 + Bölüm 69'un birleşik, kısa ve sayısal raporu
olmalıdır. Ara analiz, uzun tasarım anlatısı veya doğrulanmamış başarı iddiası
final rapora eklenmemelidir.

## AŞAĞIDAKİ ÖZGÜN GÖREV METNİ EKSİKSİZ VE BAĞLAYICIDIR

# GÖREV — AIRPORT QUEUE SYSTEM FINAL ARCHITECTURE

# Continuous Passenger Flow + Hourly Graph Buckets + Airport-Specific Security Capacity

# TÜM MEVCUT PROJEYİ ÖNCE ANALİZ ET, SONRA MİNİMUM GEREKLİ DEĞİŞİKLİKLERİ YAP

Bu görev mevcut aircraft-capacity-py projesinin mevcut çalışan sisteminin
devamıdır.

AMAÇ:

Uçuş verilerinden yolcuların havalimanına hangi gerçek zamanda geldiğini
hesaplamak, yolcuları doğru operasyonel akışlara ayırmak, passport/security
kuyruklarını gerçek işlem sürelerine göre sürekli ilerletmek ve sonucu
kullanıcıya saatlik grafik bucket'ları halinde göstermek.

ÇOK ÖNEMLİ:

5 dakika = KUYRUK WINDOW'U DEĞİLDİR.
5 dakika = SADECE dış API/source verisinin yenilenme sıklığıdır.

Kuyruk:

- 5 dakikalık bloklarla ilerlemeyecek.
- 60 dakikalık bloklarla da fiziksel olarak ilerlemeyecek.
- yolcuların gerçek işlem tamamlanma zamanlarına göre sürekli ilerleyecek.

Grafik ise:

10.00
11.00
12.00
13.00
...

şeklinde SAATLİK bucket gösterecek.

Önce mevcut kodu incele.
Çalışan davranışları gereksiz yere yeniden yazma.
Production kodunu duplicate etme.
Mevcut testleri gereksiz yere değiştirme.

==================================================

1. # HER HAVALİMANI TAMAMEN BAĞIMSIZDIR

Bu sistem yalnız IST için değildir.

Database/source içerisinde bulunan HER havalimanı için aynı hesap ayrı
yapılmalıdır.

Airport A'nın:

flight'ları
capacity config'i
passport kuyruğu
security kuyruğu
backlog'u
grafikleri

Airport B'yi ASLA etkilememelidir.

Bütün state en az:

airport_iata

ile ayrılmalıdır.

Airport isolation test edilmelidir.

================================================== 2. UÇUŞ KAPASİTESİ
==================================================

Mevcut AircraftCapacityService kullanılmaya devam etsin.

Ancak global/default aircraft capacity:

ESKİ:
150

YENİ:
180

olsun.

Resolver'ın mevcut katmanlarını bozma.

ICAO biliniyorsa gerçek resolved capacity kullanılmalı.

Sadece mevcut resmi fallback zinciri sonunda hiçbir kapasite bulunamazsa:

180

kullanılmalı.

================================================== 3. PASSENGER ARRIVAL TIME — DEPARTURE
==================================================

Departure uçuşlarının yolcuları uçuş saatinde security/passport'a
gelmiş kabul EDİLMEYECEK.

Yeni temel varsayım:

PASSENGER_AIRPORT_ARRIVAL_OFFSET = 120 dakika

Yani:

flight departure = 17:26
passenger airport arrival = 15:26

flight departure = 12:03
arrival = 10:03

flight departure = 12:34
arrival = 10:34

flight departure = 12:59
arrival = 10:59

Bunların tamamı grafik aggregation'da:

10.00

bucket'ına girer.

Ama internal queue hesabında gerçek zamanları:

10:03
10:34
10:59

korunmalıdır.

ÇOK ÖNEMLİ:

Grafikte 10.00 görünmesi yolcuların hepsinin 10:00'da fiziksel olarak
geldiği anlamına GELMEZ.

Grafik aggregation bucket'ıdır.

Internal simulation/event flow gerçek dakika/saniye zamanını korumalıdır.

================================================== 4. INTERNATIONAL ARRIVAL PASSPORT OFFSET
==================================================

International arrival yolcuları uçak yere indiği saniye passport
kuyruğuna girmiş kabul edilmesin.

Configurable:

INTERNATIONAL_ARRIVAL_PASSPORT_OFFSET_MINUTES = 15

kullan.

Örnek:

arrival = 13:00
passport arrival = 13:15
graph bucket = 13.00

arrival = 13:15
passport arrival = 13:30
graph bucket = 13.00

arrival = 13:44
passport arrival = 13:59
graph bucket = 13.00

arrival = 13:45
passport arrival = 14:00
graph bucket = 14.00

arrival = 13:50
passport arrival = 14:05
graph bucket = 14.00

Dolayısıyla bucket:

floor(passport_arrival_time to hour)

mantığıyla belirlenmeli.

================================================== 5. OPERASYONEL AKIŞLAR
==================================================

Aşağıdaki akışlar KESİNLİKLE birbirine karıştırılmamalıdır.

---

## A) DOMESTIC DEPARTURE

Domestic Departure:

Airport arrival
→ DOMESTIC SECURITY
→ çıkış

PASSPORT YOK.

Domestic security kendi fiziksel queue'sudur.

International security ile kapasite/backlog paylaşmaz.

---

## B) DOMESTIC ARRIVAL

Bu sistem kapsamında:

Domestic Arrival için:

passport YOK
security YOK
ayrı grafik YOK.

Overall airport activity gerekiyorsa uçuş aktivitesi olarak kullanılabilir,
ancak sahte passport/security demand üretme.

---

## C) INTERNATIONAL DEPARTURE

International Departure:

Airport arrival
→ PASSPORT
→ INTERNATIONAL SECURITY
→ çıkış

Passport ve security aynı anda başlamış iki bağımsız demand değildir.

Passenger önce passport'tan GERÇEKTEN çıkmalıdır.

Sonra international security'ye girmelidir.

---

## D) INTERNATIONAL ARRIVAL

International Arrival:

Aircraft arrival
→ +15 dakika
→ PASSPORT
→ çıkış

SECURITY YOK.

International arrival passenger'ını departure security'ye sokma.

================================================== 6. SECURITY QUEUE'LARI FİZİKSEL OLARAK AYRI
==================================================

Domestic Security ile International Security aynı queue değildir.

En az:

DOMESTIC SECURITY
INTERNATIONAL SECURITY

ayrı tutulmalıdır.

Her birinin:

lane count
service time
capacity
queue
backlog
utilization
estimated wait

değerleri bağımsız olmalıdır.

Özellikle international security lane sayısı HER HAVALİMANI İÇİN
kolayca değiştirilebilir olmalıdır.

Örneğin:

IST:
international_security_lanes = X

SAW:
international_security_lanes = Y

ADB:
international_security_lanes = Z

gibi.

Bunu production kodunun içine:

if airport == "IST"

gibi hard-code ETME.

AirportOperationalConfig veya mevcut config mimarisinin doğal
genişletmesini kullan.

Kolay toplu düzenlenebilir bir configuration kaynağı oluştur/kullan.

Mevcut DB config yapısını incele.

Mümkünse additive migration kullan.
Mevcut database.sqlite DROP/RESET etme.

Domestic security lane count da airport-specific olabilecek şekilde
tasarlanmalıdır.

================================================== 7. SECURITY KAPASİTE MODELİ
==================================================

Security işlem süresi:

1 passenger = 1 dakika / lane

Dolayısıyla bir security queue için:

servers = lane_count
service_time = 1 minute

Örneğin:

8 lane
→ aynı anda 8 yolcu
→ her lane 1 pax/min
→ toplam 8 pax/min
→ 480 pax/hour

Ancak bu 480 pax/hour değeri yalnız kapasite referansıdır.

Gerçek queue progression:

yolcuların arrival event'leri

- lane'lerin service completion event'leri

üzerinden sürekli ilerlemelidir.

================================================== 8. PASSPORT MODELİ
==================================================

Mevcut yeni karar korunmalıdır:

4 passport counter
2 officer per counter

effective parallel servers:

4 × 2 = 8

Her officer:

1 passenger / 1.5 dakika

Dolayısıyla:

service_time = 1.5 min
effective_servers = 8

capacity:

8 × (60 / 1.5)
= 320 pax/hour

Bu değer kapasite referansıdır.

Fakat gerçek passenger progression:

1.5 dakikalık service completion event'leri

üzerinden ilerlemelidir.

================================================== 9. PASSPORT → INTERNATIONAL SECURITY COUPLING
==================================================

BU EN ÖNEMLİ KURALDIR.

International departure passenger aynı anda passport ve security demand
OLAMAZ.

Önce passport kuyruğuna girer.

Passport'tan çıktığı gerçek zamanda:

international security queue'ya arrival event olarak eklenir.

Örnek:

Passport'ta 8 paralel officer varsa:

10:00:00
ilk 8 passenger işlem almaya başlar.

10:01:30
bu 8 passenger passport'tan çıkar.

International departure passenger ise:
→ international security'ye 10:01:30'da girer.

Passport:
→ sıradaki passenger'ları işlemeye devam eder.

Security:
→ kendi lane'leri boşsa gelen passenger'ları anında almaya başlar.

Örneğin security service time 1 dk ise:

10:02:30
ilk security grubu çıkabilir.

Bu sırada passport kendi kuyruğunu bağımsız şekilde işlemeye devam eder.

Yani:

PASSPORT SECURITY'Yİ BEKLEMEZ.
SECURITY PASSPORT'U BEKLEMEZ.

İki sistem event-driven pipeline gibi çalışır.

Passport completion:
→ Security arrival event üretir.

================================================== 10. SECURITY BOŞKEN KENDİ İŞİNİ YAPMAYA DEVAM ETMELİ
==================================================

Önceki önemli gereksinim korunmalıdır.

Security queue'da halihazırda yolcu varsa:

passport'taki yeni yolcuların gelmesini BEKLEMEDEN

kendi mevcut kuyruğunu işlemeye devam eder.

Örneğin:

security'de 50 kişi var.

passport'tan yeni grup 1.5 dakika sonra gelecek.

Bu 1.5 dakika boyunca security lane'leri boş durmamalıdır.

Mevcut security passengers işlenmeye devam eder.

Sonra passport'tan çıkan passengers security queue'ya eklenir.

================================================== 11. 5 DAKİKA KURALI — SADECE SOURCE REFRESH
==================================================

ÇOK ÖNEMLİ:

API/source artık yaklaşık:

5 dakikada bir

yeni uçuş verisi gönderecek.

BU:

queue window değildir.
prediction window değildir.
service interval değildir.
graph bucket değildir.

SADECE source refresh cadence'dir.

Örneğin:

10:00 source update
10:05 source update
10:10 source update

geldiğinde sistem yeni flight state'i kullanarak hesaplamayı günceller.

Bu arada queue:

10:01:30
10:02:30
10:03:00
10:04:30

gibi gerçek event zamanlarında ilerleyebilir.

"5 dakikada 26.6 passenger erit"

şeklinde bir model KURMA.

================================================== 12. REFRESH SIRASINDA QUEUE STATE
==================================================

5 dakikalık yeni API verisi geldiğinde:

- yeni flight
- cancelled flight
- delay
- aircraft change
- estimated time change
- actual time
- status değişimi

işlenmeli.

Ancak refresh:

mevcut fiziksel kuyruğu keyfi şekilde sıfırlamamalıdır.

Yeni source state'e göre event timeline yeniden hesaplanıyorsa:

aynı passenger demand'in duplicate edilmediği
ve geçmişte tamamlanmış service'in yeniden yaratılmadığı

garanti edilmelidir.

Mevcut pipeline/idempotency mimarisini incele ve mümkün olan en az
değişiklikle çöz.

================================================== 13. 24 SAATLİK CUTOFF KALDIRILMALI
==================================================

Mevcut:

24h backlog cutoff

yanlış sonuç üretiyor.

Önceki audit'te gerçek IST dataset'inde passenger'ların önemli kısmının
cutoff sonrası muhasebeden düştüğü kanıtlandı.

Passenger hiçbir zaman sessizce kaybolamaz.

Şu invariant korunmalıdır:

# TOTAL INPUT

TOTAL SERVED

- CURRENT REMAINING QUEUE

24 saat doldu diye passenger DROP ETME.

Ancak sonsuz DB prediction satırı da üretme.

Event simulation ile persisted graph horizon'u birbirinden ayır.

Internal queue state daha uzun sürebilir.

Frontend için yalnız gerekli/konfigüre edilmiş görünüm horizon'u
persist edilebilir.

Ama horizon dışında kalan backlog:

remaining_backlog

olarak state'te korunmalı veya sonraki hesaplamaya taşınmalıdır.

ASLA yok edilmemeli.

================================================== 14. GRAFİK ZAMANI İLE QUEUE ZAMANI AYRI KAVRAMDIR
==================================================

Internal engine:

exact datetime kullanır.

Örneğin:

10:03
10:17
10:34
10:59
11:01:30

gibi.

Frontend aggregation:

floor(timestamp → hour)

yapar.

Örneğin:

10:03 → 10.00
10:34 → 10.00
10:59 → 10.00
11:00 → 11.00

Grafikte:

10.00
11.00
12.00
13.00

görünür.

10.00 etiketi:

10:00:00–10:59:59...

bucket'ını temsil eder.

================================================== 15. İSTENEN 4 GRAFİK
==================================================

Her airport için TAM 4 ana grafik istiyorum.

---

## GRAPH 1 — TÜM HAVALİMANI YOĞUNLUĞU

Örneğin IST için:

TÜM IST HAVALİMANI YOĞUNLUĞU

Burada domestic/international diye ayrı grafik istemiyorum.

Bu airport'un genel operasyonel yoğunluk görünümüdür.

Domestic + international uçuş aktivitesini airport bazında aggregate et.

Ancak:

passport ve security yolcularını tekrar tekrar toplayıp passenger
double-count üretme.

Bu grafik fiziksel tek bir queue değildir.

Overall operational intensity/risk summary'dir.

Mevcut overall severity aggregation mantığını mümkün olduğunca koru,
ancak yeni process setiyle uyumlu hale getir.

---

## GRAPH 2 — DOMESTIC SECURITY

SADECE:

Domestic Departure

yolcuları.

Domestic Arrival YOK.

International passenger YOK.

Kendi lane config'i.
Kendi queue'su.
Kendi wait'i.
Kendi backlog'u.

---

## GRAPH 3 — INTERNATIONAL DEPARTURE

Bu grafik international departure passenger journey'sini göstermelidir:

Airport arrival
→ Passport
→ International Security

Backend'de passport ve security ayrı queue state'leri olarak tutulabilir
ve tutulmalıdır.

Frontend/API contract'ta bu grafik için en az:

passport risk/wait
international security risk/wait
journey/overall severity

erişilebilir olmalıdır.

Grafiğin yoğunluk/risk değeri bu iki aşamanın darboğazını dürüstçe
yansıtmalıdır.

Düşük severity'deki bir process'ten yüksek severity'ye sahte wait
ödünç alma.

---

## GRAPH 4 — INTERNATIONAL ARRIVAL

SADECE:

International Arrival
→ +15 dakika
→ Passport

Security YOK.

Örneğin:

13:15 arrival
→ 13:30 passport demand
→ graph bucket 13.00

13:45 arrival
→ 14:00 passport demand
→ graph bucket 14.00

================================================== 16. DOMESTIC ARRIVAL GRAPH YOK
==================================================

Domestic Arrival için beşinci grafik üretme.

4 grafik contract'ını koru.

================================================== 17. INTERNATIONAL PASSPORT AYRIMI
==================================================

International Departure Passport ile
International Arrival Passport

aynı fiziksel passport pool'unu paylaşıyorsa:

FİZİKSEL QUEUE HESABI ortak kapasite üzerinde yapılmalıdır.

Yani aynı officer'ları iki kere yaratma.

Örneğin:

4 counter × 2 officer = toplam 8 effective server

ise:

departure'a ayrı 8
arrival'a ayrı 8

vererek toplam sahte 16 server yaratma.

Fiziksel passport queue ortak olabilir.

Ancak raporlama/graph tarafında passenger cohort/source:

departure
arrival

olarak ayrılabilmelidir.

Böylece:

International Departure graph
ve
International Arrival graph

ayrı gösterilirken gerçek fiziksel passport kapasitesi double-count
edilmez.

================================================== 18. SECURITY CAPACITY AYRIMI
==================================================

Domestic Security ve International Security fiziksel olarak ayrı
tanımlanacaktır.

Dolayısıyla:

domestic_security_lane_count
international_security_lane_count

airport-specific olmalıdır.

Özellikle international lane sayılarını bütün airport'lar için kolayca
düzenleyebileceğim tek bir config mekanizması istiyorum.

Config örneği:

airport_iata
passport_counter_count
passport_staff_per_counter
passport_service_time_minutes
domestic_security_lane_count
international_security_lane_count
security_service_time_minutes
departure_arrival_offset_minutes
international_arrival_passport_offset_minutes

Mevcut schema/config yapısına göre en temiz karşılığını tasarla.

================================================== 19. DATA SOURCE
==================================================

data/ klasöründeki GERÇEK kaynakları önce keşfet.

Dosya adlarını tahmin etme.

Mevcut JSON/source parser'larını kullan.

Gerçek source'dan en az:

airport
direction
domestic/international/location
scheduled
estimated
actual
status
aircraft ICAO
terminal/gate varsa

alanlarını değerlendir.

Critical field eksikse sistem crash etmemeli.

Mevcut fallback/skip/UNKNOWN politikasını koru.

================================================== 20. SQL / DATABASE
==================================================

Mevcut DB mimarisini önce incele.

data tarafındaki SQL bağlantısı / mevcut SQLite veya MySQL uyumluluğunu
bozma.

database.sqlite mevcutsa:

DROP ETME.
RESET ETME.
MEVCUT FLIGHT VERİSİNİ SİLME.

Schema değişikliği gerekiyorsa additive migration kullan.

MySQL deployment ihtimalini göz önünde bulundur.

SQLite'a özel business logic yazma.

================================================== 21. EFFECTIVE FLIGHT TIME
==================================================

Departure için en güvenilir mevcut zaman kaynağını kullan:

actual/estimated/scheduled

mevcut production priority neyse önce incele ve onu bozma.

Passenger arrival:

effective_departure_time - 120 dakika

olmalı.

International Arrival için:

effective_arrival_time + 15 dakika

passport demand time'dır.

Cancelled/diverted terminal-status davranışlarını mevcut bug fix'e uygun
koru.

================================================== 22. EVENT-DRIVEN QUEUE MODEL
==================================================

Queue modelini "saatte X kişi" toplama hesabıyla sınırlama.

Capacity rate doğrulama/reference için kullanılabilir.

Fakat gerçek queue state:

ARRIVAL EVENT
SERVICE START
SERVICE COMPLETE
NEXT SERVICE START

mantığıyla ilerlemelidir.

Her queue için en az:

waiting passengers
busy servers
next available server times
arrival timestamp
service start timestamp
service completion timestamp
wait duration

mantığı temsil edilebilmelidir.

Tek tek milyonlarca Passenger ORM satırı oluşturmak zorunda değilsin.

Performans için cohort/batch event simulation kullanılabilir.

Örneğin aynı timestamp'te 180 passenger geldiyse:

180 ayrı DB passenger row

zorunlu değildir.

Ancak matematik tek tek passenger service mantığıyla EŞDEĞER olmalıdır.

================================================== 23. WAIT TIME
==================================================

Wait:

passenger'ın queue arrival zamanı ile
service start zamanı

arasındaki süreye dayanmalıdır.

Passport:

queue arrival
→ officer available
→ service start
→ +1.5 min
→ passport completion

International Departure için passport completion:

security arrival timestamp

olur.

Security:

security arrival
→ lane available
→ service start
→ +1 min
→ completion

Frontend sahte wait üretmemeli.

Backend hesaplayamıyorsa:

null / UNKNOWN

taşı.

================================================== 24. UTILIZATION
==================================================

Mevcut utilization/risk contract'ını mümkün olduğunca koru.

Ancak utilization fiziksel queue capacity ile tutarlı olmalıdır.

Passport:

effective servers = counters × staff_per_counter

Security:

servers = ilgili airport/process lane count

Aynı fiziksel resource'u iki graph için duplicate etme.

================================================== 25. DEFAULT CONFIG
==================================================

Global fallback config bulunabilir.

Ancak bütün airport'ların fiziksel olarak aynı lane sayısına sahip
olduğunu varsayma.

Unknown airport için güvenli default kullanılabilir ve bunun fallback
olduğu anlaşılmalıdır.

Config override sistemi:

kolay toplu düzenlenebilir
airport bazlı
DB/schema uyumlu

olmalıdır.

================================================== 26. FRONTEND
==================================================

Frontend hesap yapmayacak.

Sadece backend/API sonucunu gösterecek.

4 grafik:

1. TÜM HAVALİMANI YOĞUNLUĞU
2. DOMESTIC SECURITY
3. INTERNATIONAL DEPARTURE
4. INTERNATIONAL ARRIVAL

Her grafik:

10.00
11.00
12.00
...

saat bucket'larını göstermeli.

Gerekirse selected hour detayında gerçek zaman bilgileri ayrıca
gösterilebilir.

Örneğin:

Grafik bucket: 10.00
Passenger demand events:
10:03
10:26
10:34
10:59

Ama ana x-axis saatlik kalmalı.

================================================== 27. EMPTY / PARTIAL DATA
==================================================

Şunların hiçbirinde frontend çökmemeli:

current=null
windows=[]
wait=null
UNKNOWN risk
airport name null
bir process boş
bir airport'ta yalnız domestic
bir airport'ta yalnız international arrival
ICAO missing
estimated missing
actual missing

Geçerli diğer grafikler gösterilmeye devam etmeli.

================================================== 28. 5 DAKİKALIK REFRESH
==================================================

Mevcut 30 dakikalık varsayımlar varsa bul.

Source artık yaklaşık 5 dakikada bir güncellenecek.

Ancak mimariyi dikkatle ayır:

SOURCE REFRESH = 5 dakika

FRONTEND GRAPH BUCKET = 1 saat

QUEUE PROGRESSION = continuous/event-driven

Bunları aynı constant'a bağlama.

Frontend polling gerçekten gerekli mi ayrıca incele.

Eğer source/pipeline zaten 5 dakikada bir DB'yi güncelliyorsa frontend
sadece DB/API sonucunu okuyabilir.

Request-time external source çağrısı YAPMA.

================================================== 29. PASSENGER CONSERVATION
==================================================

Her airport ve her passenger cohort için mümkün olduğu ölçüde şu
invariant test edilmeli:

# input demand

completed

- waiting
- currently in service
- downstream transferred

Passenger:

yaratılamaz
kaybolamaz
iki kere sayılamaz.

Özellikle:

International Departure:

airport demand
→ passport
→ international security
→ completed

zincirinde conservation test et.

================================================== 30. DOUBLE COUNT TESTLERİ
==================================================

Aşağıdakileri özellikle test et:

Domestic departure:
security_dom'a 1 kez girer.

International departure:
passport'a 1 kez girer.
passport completion sonrası security_intl'e 1 kez girer.

International arrival:
passport'a 1 kez girer.
security'ye 0 kez girer.

Domestic arrival:
queue demand'e 0 kez girer.

================================================== 31. EXACT TIME → HOURLY BUCKET TESTLERİ
==================================================

Zorunlu testler:

Departure:

12:00 → airport arrival 10:00 → graph 10.00
12:03 → 10:03 → graph 10.00
12:34 → 10:34 → graph 10.00
12:59 → 10:59 → graph 10.00
13:00 → 11:00 → graph 11.00

International Arrival:

13:00 → passport 13:15 → graph 13.00
13:15 → passport 13:30 → graph 13.00
13:44 → passport 13:59 → graph 13.00
13:45 → passport 14:00 → graph 14.00
13:59 → passport 14:14 → graph 14.00

Boundary'leri özellikle test et.

================================================== 32. QUEUE TIMING TESTİ
==================================================

Deterministik küçük senaryo oluştur.

Örneğin international departure cohort:

300 passenger airport'a 10:00'da geliyor.

Passport:
8 effective server
1.5 min service.

Security:
airport-specific international lane count
1 min service.

Test:

passport ilk completion timestamp
= 10:01:30

Bu cohort'un security'ye bundan ÖNCE girmediğini doğrula.

Security'nin mevcut eski passengers varsa
10:00→10:01:30 arasında onları işlemeye devam ettiğini doğrula.

Passport'un security'yi beklemediğini doğrula.

Security'nin passport completion event geldikçe yolcu kabul ettiğini
doğrula.

================================================== 33. MULTIPLE FLIGHT TEST
==================================================

Aynı airport için gerçekçi uçuşlar oluştur:

12:03
12:17
12:34
12:59

Hepsinin kapasitesini gerçek AircraftCapacityService ile çöz.

Airport demand events:

10:03
10:17
10:34
10:59

olsun.

Grafikte hepsi:

10.00

bucket'ında aggregate edilsin.

Ancak internal queue'da 10:00'da topluca gelmiş gibi davranmasın.

Bu ayrım TESTLE kilitlensin.

================================================== 34. AIRPORT-SPECIFIC CAPACITY TEST
==================================================

Airport A:

domestic lanes = 6
international lanes = 10

Airport B:

domestic lanes = 3
international lanes = 5

Aynı passenger demand ver.

Sonuçların farklı utilization/wait üretmesini doğrula.

A'nın config'i B'yi etkilememeli.

================================================== 35. OVERALL AIRPORT GRAPH
==================================================

Overall airport graph fiziksel yeni bir queue yaratmamalı.

Existing process sonuçlarının airport-level operational summary'si
olmalıdır.

Domestic/international diye split görünüm istemiyorum.

Ancak overall oluştururken:

aynı international departure passenger'ı passport ve security'de iki kez
"airport passenger count" olarak toplama.

Overall passenger activity ile process workload kavramlarını ayır.

================================================== 36. GERÇEK DATA E2E
==================================================

Synthetic/unit testlerden sonra gerçek production-shape JSON ile E2E yap.

Zincir:

real/project source JSON
→ production parser
→ refresh
→ Flight
→ AircraftCapacityService
→ passenger demand timing
→ event queue engine
→ QueuePrediction/state
→ API
→ frontend contract

Gerçek database.sqlite'ı destructive şekilde değiştirme.

Gerekirse gerçek JSON'un kopyası + ayrı test DB kullan.

================================================== 37. TEST STRATEJİSİ — TOKEN VE ZAMAN TASARRUFU
==================================================

HER KÜÇÜK DEĞİŞİKLİKTEN SONRA FULL PYTEST ÇALIŞTIRMA.

Önce:

1. static/code audit
2. değişen modülün targeted testleri
3. ilgili queue tests
4. coupling tests
5. API tests
6. frontend contract tests

çalıştır.

Bir hata varsa önce targeted test ile çöz.

Ancak targeted testlerin tamamı yeşil olduktan sonra:

python -m pytest

SADECE BİR KEZ final regression olarak çalıştır.

Full pytest'te hata çıkarsa:

önce sadece failing testleri çalıştır.
Her düzeltmeden sonra tekrar full suite çalıştırma.

Tüm failing targeted tests yeşil olduktan sonra full suite'i tekrar
SADECE BİR KEZ çalıştır.

================================================== 38. ESKİ TESTLER
==================================================

Eski test yeni doğru business rule nedeniyle fail oluyorsa:

önce testin gerçekten eski davranışı mı kilitlediğini kanıtla.

Testi sadece "yeşil olsun" diye gevşetme.

Yeni contract'a göre güncelle.

Mevcut doğru regresyon testlerini koru.

================================================== 39. TRANSFER PASSENGERS
==================================================

Mevcut source'ta itinerary/passenger-level transfer bağlantısı yoksa:

International→International
Domestic→International
International→Domestic

transfer passenger sayısını UYDURMA.

Bu özellik için:

NOT DETERMINABLE FROM CURRENT SOURCE DATA

olarak raporla.

Source bunu desteklemeden sahte transfer demand üretme.

================================================== 40. IMPLEMENTATION ÖNCESİ AUDIT
==================================================

KOD YAZMADAN ÖNCE kısa dependency map çıkar:

CURRENT
→ REQUIRED CHANGE
→ AFFECTED MODULES
→ AFFECTED TESTS

Özellikle incele:

models.py
config.py
constants.py
domain/demand.py
domain/flows.py
core/scoring.py
core/erlang.py
engine.py
baseline.py
api.py
ingestion/\*
db.py
web/static/index.html

Mevcut fonksiyonları reuse et.

Sonra implementation'a geç.

Benden tekrar onay bekleme.

================================================== 41. PERFORMANCE
==================================================

Event-driven demek her passenger için DB row yazmak demek değildir.

Büyük airport'larda binlerce passenger olabilir.

Queue simulator:

batch/cohort
server availability heap
event aggregation

gibi verimli yöntem kullanabilir.

Örneğin Python heapq ile next available server zamanları tutulabilir.

Hedef:

tek tek yolcu mantığıyla matematiksel olarak eşdeğer
ama production'da hızlı

bir çözüm.

O(n log c) benzeri bir yaklaşım tercih edilebilir.

================================================== 42. SOURCE REFRESH IDEMPOTENCY
==================================================

5 dakikada bir aynı flight tekrar gelebilir.

Aynı flight:

yeniden passenger yaratmamalı.

Flight identity ve mevcut refresh idempotency korunmalı.

Aircraft change/delay/cancellation durumlarında mevcut event/update
mekanizmasını kullan.

================================================== 43. DELAY
==================================================

Departure time değişirse:

passenger airport-arrival time da değişmelidir.

Örneğin:

scheduled departure = 17:00
estimated departure = 18:00

production effective-time kuralına göre uçuş 18:00 kabul ediliyorsa:

passenger arrival:
16:00

olmalıdır.

Eski 15:00 demand bucket'ı stale prediction olarak kalmamalıdır.

================================================== 44. INTERNATIONAL ARRIVAL DELAY
==================================================

Arrival effective time değişirse:

passport arrival time:

effective_arrival + 15 min

yeniden hesaplanmalıdır.

Örneğin:

13:30 → 13:45 passport event → graph 13.00

delay sonrası:

13:50 → 14:05 passport event → graph 14.00

Eski 13.00 demand stale kalmamalıdır.

================================================== 45. CANCELLATION
==================================================

Cancelled flight passenger demand üretmemelidir.

Önceden demand üretmiş flight daha sonra cancelled gelirse
sonraki refresh'te stale demand/prediction kaldırılmalıdır.

Mevcut terminal-status resurrection bug fix'ini BOZMA.

================================================== 46. API CONTRACT
==================================================

API'nin frontend için gerekli tüm bilgiyi vermesini sağla.

Frontend business math yapmasın.

API'de process/cohort ayrımı açık olmalı.

Her graph için en az:

bucket_start
risk
risk_label
estimated_wait_minutes
utilization
confidence

ve gerekiyorsa process breakdown bulunmalı.

Passenger count'ın frontend'de gösterilmesi mevcut product contract'ta
yasaksa bunu bozma; backend test/debug tarafında doğrulanabilir.

================================================== 47. FINAL REALISTIC REPLAY
==================================================

En son gerçek JSON şemasının AYNISI olan realistic production-shape
test dataset'i oluştur.

Bir airport için saatler boyunca:

domestic departure
international departure
international arrival
delays
aircraft changes
cancellation
missing ICAO
unknown ICAO

karışık şekilde olsun.

Özellikle exact minute flight times kullan:

12:03
12:17
12:34
12:59
13:00
13:15
13:45
...

Saat saat rapor üret.

Her event için göster:

flight effective time
aircraft ICAO
resolved capacity
passenger queue arrival time
hangi process'e girdi
passport completion/release
security arrival
queue wait
graph bucket

Sonra API sonucuyla karşılaştır.

================================================== 48. FINAL ACCEPTANCE
==================================================

Aşağıdakilerin tamamı sağlanmadan PASS verme:

[ ] Default aircraft fallback = 180
[ ] Every airport independently calculated
[ ] Departure passenger arrival = effective departure - 120 min
[ ] International arrival passport = effective arrival + 15 min
[ ] Exact minute timestamps preserved internally
[ ] Hourly graph buckets
[ ] 5-minute source refresh queue timestep olarak kullanılmıyor
[ ] Domestic Departure → domestic security only
[ ] Domestic Arrival → no queue graph
[ ] International Departure → passport → intl security
[ ] International Arrival → passport only
[ ] Domestic and international security independent
[ ] Security lane counts airport-specific
[ ] International security lanes easy to bulk-configure
[ ] Passport = 4×2 effective workers
[ ] Passport service = 1.5 min
[ ] Passport capacity reference = 320 pax/hour
[ ] Security service = 1 min/lane
[ ] Passport completion feeds intl security at actual completion time
[ ] Security continues processing while waiting for passport output
[ ] Shared physical passport capacity is NOT duplicated between arrival/departure
[ ] Passenger conservation holds
[ ] No double counting
[ ] No silent 24h passenger loss
[ ] Delay migrates demand time
[ ] Cancellation removes demand
[ ] Refresh idempotent
[ ] API contains correct four-graph data
[ ] Frontend contains exactly four requested graph views
[ ] Empty/null data cannot crash page
[ ] Airport isolation passes
[ ] Realistic production-shape replay passes
[ ] Final full pytest passes

================================================== 49. FINAL REPORT
==================================================

İş bittiğinde SADECE kısa ama sayısal rapor ver:

CHANGED PRODUCTION FILES:
...

DATABASE MIGRATION:
...

DEFAULT AIRCRAFT CAPACITY:
180

SOURCE REFRESH:
5 min (external data refresh only)

QUEUE TIME MODEL:
continuous/event-driven

GRAPH BUCKET:
hourly

PASSPORT:
4 counters
2 officers/counter
8 effective servers
1.5 min/passenger
320 pax/hour reference capacity

DOMESTIC SECURITY:
airport-specific lane count
1 min/passenger/lane

INTERNATIONAL SECURITY:
airport-specific lane count
1 min/passenger/lane

FLOWS:
Domestic Departure = Security
Domestic Arrival = no queue graph
International Departure = Passport → International Security
International Arrival = +15 min → Passport

PASSPORT→SECURITY COUPLING:
PASS/FAIL

PASSENGER CONSERVATION:
PASS/FAIL

24H PASSENGER LOSS:
PASS/FAIL
Expected: ZERO silent loss

AIRPORT ISOLATION:
PASS/FAIL

HOURLY BUCKET BOUNDARIES:
PASS/FAIL

5-MIN REFRESH IDEMPOTENCY:
PASS/FAIL

REALISTIC REPLAY:
PASS/FAIL

TARGETED TESTS:
X passed / X failed

FULL PYTEST:
X passed / X failed / X skipped

REMAINING LIMITATIONS:
...

FINAL:
PASS / FAIL

Eğer source verisi yüzünden gerçekten yapılamayan bir özellik varsa
PASS uydurma.

Açıkça:

NOT DETERMINABLE FROM CURRENT SOURCE DATA

yaz.

Bu görev sırasında production kodunda bug bulursan:
önce targeted regression test ile reproduce et,
sonra düzelt,
sonra ilgili testleri çalıştır.

En son full pytest'i çalıştır.================================================== 50. FRONTEND — SAATLİK NOKTAYA TIKLAYINCA WAIT GÖSTER
==================================================

4 grafiğin TAMAMINDA kullanıcı saatlik bir grafik noktasına/bucket'ına
tıkladığında O SAATE ait detay açılmalıdır.

Örneğin grafik:

09.00
10.00
11.00
12.00
13.00

şeklindeyse kullanıcı:

10.00

noktasına tıkladığında 10.00 bucket'ına ait gerçek backend sonucu
gösterilmelidir.

Her grafik kendi seçili saat state'ine sahip olmalıdır.

Bir grafikte 10.00'a tıklamak diğer grafiklerin seçili saatini
DEĞİŞTİRMEMELİDİR.

Gösterilecek temel bilgi:

Tahmini bekleme süresi: X dk

ve mümkünse mevcut backend contract'ında bulunan:

risk
risk_label
utilization
confidence

bilgileri de detayda gösterilebilir.

ANCAK frontend ASLA wait hesaplamamalıdır.

Wait:

backend queue simulation sonucundan gelmelidir.

Örnek:

Domestic Security / 10.00
Tahmini bekleme süresi: 14.2 dk

International Departure / 10.00
Passport: 21.4 dk
International Security: 8.1 dk

International Arrival / 13.00
Passport: 17.8 dk

Overall / 10.00
ilgili overall operational result.

International Departure iki aşamalı olduğu için seçilen saatin
detayında passport ve international security wait değerlerini ayrı
göstermek tercih edilmelidir.

Bir journey-level wait gerekiyorsa SAHTE bir toplama/ortalama yapma.

Backend'de gerçekten tanımlanmış journey wait varsa kullan.
Yoksa iki aşamayı ayrı göster:

Passport bekleme: X dk
Security bekleme: Y dk

Wait hesaplanamıyorsa:

Tahmini bekleme süresi: Hesaplanamıyor

göster.

current=null veya ilgili bucket yoksa:

Bu saat için veri yok

gibi güvenli durum göster.

JS exception oluşmamalıdır.

================================================== 51. GRAPH CLICK — BACKEND SNAPSHOT CONTRACT
==================================================

Saatlik grafik bucket'ı sadece görsel aggregation değildir.

Backend/API, her hour bucket için frontend'in detay gösterebilmesi için
o saate ait hesaplanmış queue sonucunu taşımalıdır.

Örneğin:

{
"bucket_start": "2026-09-19T10:00:00",
"risk": "...",
"estimated_wait_minutes": 14.2,
"utilization": ...,
"confidence": ...
}

International Departure gibi birden fazla fiziksel queue içeren
journey için gerekli breakdown ayrıca taşınabilir:

{
"bucket_start": "2026-09-19T10:00:00",
"passport": {
"estimated_wait_minutes": ...
},
"international_security": {
"estimated_wait_minutes": ...
}
}

Frontend backend sonucunu yalnız render etsin.

================================================== 52. 5 DAKİKALIK REFRESH — EN ÖNEMLİ STATE KURALI
==================================================

Dış source/API yaklaşık 5 dakikada bir yeni veri sağlayacaktır.

Ancak refresh ile queue progression birbirinden TAMAMEN ayrılmalıdır.

5 dakika geçtiğinde:

queue kendi service mekanizmasıyla ilerlemiş olmalıdır.

Örneğin:

10:00 source geldi.
10:05 yeni source geldi.

Bu 5 dakika boyunca:

passport officer'ları passenger işlemeye,
security lane'leri passenger işlemeye,
passport completion event'leri security'ye passenger aktarmaya

DEVAM ETMİŞ olmalıdır.

10:05 refresh işlemi:

queue'yu sıfırlamamalıdır.
backlog'u sıfırlamamalıdır.
busy server state'i anlamsız şekilde sıfırlamamalıdır.
daha önce tamamlanmış passenger'ı yeniden queue'ya koymamalıdır.
aynı flight demand'ini tekrar eklememelidir.

================================================== 53. REFRESH'TE YENİ VERİ YOKSA
==================================================

Özellikle bu senaryoyu test et:

R0 = 10:00
source payload geldi.

Queue çalışmaya başladı.

R1 = 10:05
source tekrar geldi fakat hiçbir flight değişmedi.

Beklenen:

Flight insert = 0
yeni passenger demand = 0
duplicate passenger = 0

ANCAK:

10:00 → 10:05 arasında queue normal şekilde ilerlemiş olmalıdır.

Yani:

"source değişmedi"

ile:

"queue değişmedi"

AYNI ŞEY DEĞİLDİR.

Queue service zamanın geçmesi nedeniyle ilerler.

Örneğin:

10:00'da passport queue = 300 passenger.

10:05'te hiçbir yeni flight gelmemiş olabilir.

Ama 5 dakika boyunca 8 officer çalışmıştır.

Dolayısıyla queue state zamanın geçmesine göre ilerlemiş olmalıdır.

Refresh aynı flight'ları tekrar getirerek bu passenger'ları yeniden
eklememelidir.

================================================== 54. REFRESH'TE YENİ FLIGHT GELİRSE
==================================================

Örnek:

R0 10:00:

mevcut queue/state hesaplandı.

R1 10:05:

API'de yeni bir flight ortaya çıktı.

Yeni flight:

effective departure
aircraft ICAO
resolved capacity
airport-arrival timestamp

üzerinden hesaplanmalıdır.

Örneğin:

new flight departure = 13:26
airport arrival = 11:26

ise:

11:26

timestamp'inde yeni passenger arrival event oluşturulmalıdır.

Bu demand:

mevcut queue timeline/state'e dahil edilmelidir.

Mevcut passenger'ları silip bütün sistemi kör şekilde sıfırdan
duplicate etme.

Yeni bilgi mevcut operasyonel state'e güvenli/idempotent şekilde
uygulanmalıdır.

================================================== 55. REFRESH DELTA / IDEMPOTENCY
==================================================

Her refresh'te gelen flight kayıtlarını şu şekilde sınıflandır:

UNCHANGED
NEW
UPDATED
CANCELLED / TERMINAL CHANGE

UNCHANGED:

yeni passenger demand üretmez.

NEW:

yalnız bir kez passenger demand oluşturur.

UPDATED:

mevcut flight'ın yeni effective state'ine göre gerekli değişikliği
uygular.

Örneğin delay:

13:00 departure
→ passenger arrival 11:00

daha sonra:

14:00 departure
→ passenger arrival 12:00

olursa stale 11:00 demand'in yanında ikinci bir demand yaratma.

Aynı flight cohort'unun zamanını güncelle.

CANCELLED:

artık passenger demand üretmemelidir.

Mevcut terminal-status resurrection fix korunmalıdır.

================================================== 56. REFRESH ATOMICITY / DATABASE SAFETY
==================================================

5 dakikalık refresh database'i bozabilecek şekilde uygulanmamalıdır.

Özellikle:

aynı flight duplicate row üretmemeli.
aynı passenger cohort duplicate edilmemeli.
aynı QueuePrediction anlamsız şekilde çoğalmamalı.
yarım refresh DB'yi inconsistent durumda bırakmamalı.

Mümkün olduğunca mevcut transaction/session mimarisini kullan.

Refresh başarısız olursa önceki başarılı state gereksiz yere
silinmemelidir.

Bir source/API problemi yüzünden:

Flight tablosunu temizleme.
QueuePrediction tablosunu topluca temizleme.
Airport'ları silme.
capacity tablolarını silme.

DB destructive reset YAPMA.

================================================== 57. CONCURRENT / OVERLAPPING REFRESH KORUMASI
==================================================

5 dakikalık cadence nedeniyle önceki pipeline henüz bitmeden yeni
refresh başlama ihtimalini incele.

Aynı airport/date için iki pipeline aynı anda çalışırsa:

duplicate Flight
duplicate demand
duplicate queue state
duplicate prediction

oluşmamalıdır.

Mevcut deployment tek-process olduğu için risk yoksa bunu raporla.

Risk varsa mevcut mimariye uygun:

transaction
unique constraint
run lock
job lock

gibi minimum güvenli çözümü kullan.

Gereksiz distributed architecture kurma.

================================================== 58. SADECE İLGİLİ OPERASYONEL GÜN
==================================================

Prediction sistemi geçmiş/gelecek bütün Flight tablosunu aynı anda
hesaplamamalıdır.

Her pipeline/prediction run:

SADECE ilgili havalimanının BUGÜNKÜ operasyonel gününe ait uçuşları
hesaplamalıdır.

Örneğin ilgili airport local date:

2026-09-19

ise ana prediction dataset:

2026-09-19 tarihine ait flight'lar

olmalıdır.

18 Eylül uçuşlarını yeniden bugünün yeni demand'i gibi hesaplama.

20 Eylül uçuşlarını bugünün yoğunluğuna ekleme.

================================================== 59. "BUGÜN" SERVER DATE DEĞİLDİR
==================================================

Çok önemli:

"bugün" kör şekilde:

datetime.now().date()

ile server timezone'una göre belirlenmemelidir.

Havalimanının yerel operasyonel tarihi esas alınmalıdır.

Örneğin:

IST → Europe/Istanbul

başka ülkedeki airport → kendi timezone'u.

Projede airport timezone bilgisi zaten varsa onu kullan.

Yoksa mevcut data/schema'yı incele.

Timezone source'tan güvenilir biçimde belirlenemiyorsa sessizce yanlış
gün seçme; limitation olarak raporla.

Testlerde "now" inject edilebilir/deterministik olmalıdır.

================================================== 60. GÜN SINIRI VE KUYRUK STATE'İ
==================================================

"Yalnız bugünkü flight'ları hesapla" demek gece 00:00 olduğunda fiziksel
kuyruğu zorla sıfırla demek DEĞİLDİR.

Örneğin:

23:50'de bugünkü bir flight cohort'undan kalan gerçek passenger queue
varsa ve service:

00:05
00:15

saatlerine taşıyorsa bu backlog sessizce kaybolmamalıdır.

Yani iki kavramı ayır:

NEW FLIGHT DEMAND SOURCE:
yalnız ilgili operational day's flights

QUEUE CARRY:
önceki geçerli demand'den fiziksel olarak hâlâ bekleyen passenger varsa
conservation gereği devam edebilir.

Yeni gün başladığında eski flight tekrar NEW demand olarak eklenmez.

Ama kalan gerçek queue yok edilmez.

================================================== 61. DAY FILTER — DEPARTURE OFFSET İLE KARIŞTIRMA
==================================================

Flight hangi operational day'e ait olduğu ile passenger'ın airport'a
hangi timestamp'te geldiği farklı şeylerdir.

Örneğin:

flight departure:
19 Sep 00:45

Bu flight 19 Sep operasyonel flight'ıdır.

Passenger airport arrival:
18 Sep 22:45

olabilir.

Bu passenger event'i yalnız "-120 dakika yaptık, önceki güne düştü"
diye kaybetme.

Flight operational date:

19 Sep

olabilirken queue event:

18 Sep 22:45

olabilir.

Aynı şekilde international arrival +15 dakika nedeniyle:

19 Sep 23:55 arrival
→ 20 Sep 00:10 passport arrival

olabilir.

Bu passenger da kaybolmamalıdır.

Dolayısıyla:

FLIGHT SELECTION DATE

ile

QUEUE EVENT TIMESTAMP

ayrı tutulmalıdır.

================================================== 62. GRAPH DAY DISPLAY
==================================================

Frontend seçilen/aktif operational day için saatlik grafik göstermelidir.

Temel x-axis:

00.00
01.00
02.00
...
23.00

Ancak sadece veri bulunan saatlerin gösterilmesi mevcut UI için daha
uygunsa mevcut davranışı koruyabilirsin.

Önemli olan:

bucket timestamp doğru güne/saat dilimine ait olmalıdır.

Grafik:

10.00

etiketiyle:

10:00:00 <= timestamp < 11:00:00

aralığını temsil etmelidir.

================================================== 63. CURRENT TIME İLERLEDİKÇE WAIT DEĞİŞMELİ
==================================================

Bu yeni mimaride önemli bir davranış:

Hiç yeni source verisi gelmese bile zaman ilerledikçe mevcut queue
işlenir.

Dolayısıyla aynı graph bucket'ının:

current estimated wait

değeri zamanla değişebilir.

Örneğin:

10:00:
queue yüksek
wait = 30 dk

10:05:
yeni flight YOK

ancak queue 5 dakika service gördü.

Yeni current state:

wait daha düşük olabilir.

Bu değişiklik:

yeni passenger geldiği için değil,
zaman geçtiği ve queue işlendiği için

oluşmuştur.

Backend bunu doğru hesaplamalıdır.

================================================== 64. FRONTEND CLICK + REFRESH BİRLİKTE
==================================================

Kullanıcı örneğin:

Domestic Security → 10.00

noktasını seçmiş olsun.

10:05 refresh geldiğinde:

yeni data yoksa bile dashboard güvenli şekilde refresh olabilir.

Mümkünse kullanıcının seçili:

airport
graph
hour bucket

state'i korunmalıdır.

Refresh sonrası 10.00 bucket hâlâ varsa selection kaybolmamalıdır.

Bucket artık yoksa güvenli şekilde current/default state'e dön.

Sayfa reload olmak zorunda değildir.

JS exception oluşmamalıdır.

================================================== 65. ZORUNLU 5-MIN REFRESH TESTİ
==================================================

Aşağıdaki deterministic E2E senaryoyu kur:

R0 = 10:00

Flight A mevcut.
Flight B mevcut.

Pipeline çalışır.
Queue state/predictions oluşur.

R1 = 10:05

AYNI payload tekrar gelir.
Yeni flight YOK.

Beklenen:

duplicate Flight = 0
duplicate passenger demand = 0
Flight row count değişmez

AMA:

queue 5 dakika ilerlemiştir.

R2 = 10:10

Payload'a Flight C eklenir.

Beklenen:

yalnız Flight C NEW kabul edilir.
Flight A/B yeniden demand yaratmaz.

Flight C'nin capacity'si AircraftCapacityService ile çözülür.
Passenger arrival timestamp hesaplanır.
Doğru airport/process queue'suna dahil edilir.

R3 = 10:15

Flight B delay olur.

Beklenen:

Flight B duplicate edilmez.
Effective time güncellenir.
Passenger event gerekiyorsa doğru timestamp'e migrate edilir.
Eski + yeni demand birlikte kalmaz.

R4 = 10:20

Flight C cancelled olur.

Beklenen:

terminal status doğru uygulanır.
Stale future demand kaldırılır.
Daha önce gerçekten tamamlanmış service history keyfi şekilde
geri alınmaz.

Her aşamada:

PASSENGER CONSERVATION

kontrol et.

================================================== 66. ZORUNLU DAILY-SCOPE TESTİ
==================================================

Deterministik "now":

2026-09-19

kullan.

DB'de:

18 Sep flights
19 Sep flights
20 Sep flights

bulunsun.

Prediction'ın NEW flight demand kaynağında:

yalnız 19 Sep operational flights

kullanıldığını kanıtla.

Ancak boundary testleri ayrıca yap:

19 Sep 00:45 departure
→ passenger airport arrival 18 Sep 22:45

ve:

19 Sep 23:55 international arrival
→ passport event 20 Sep 00:10

Bu event'lerin sırf tarih değiştirdi diye kaybolmadığını doğrula.

================================================== 67. FRONTEND GRAPH CLICK TESTLERİ
==================================================

Her dört grafik için test et:

Overall → hour click
Domestic Security → hour click
International Departure → hour click
International Arrival → hour click

Özellikle:

10.00'a tıklanınca 11.00 wait'i gösterilmemeli.

Her graph'ın state.detail/key seçimi bağımsız olmalı.

International Departure:

Passport wait
International Security wait

birbirinden ayırt edilebilir olmalı.

estimated_wait_minutes=null:

"Hesaplanamıyor"

göstermeli.

windows=[]:

"Bu saat için veri yok"

gibi güvenli state göstermeli.

================================================== 68. FINAL ACCEPTANCE'E EKLE
==================================================

Final acceptance checklist'e aşağıdakileri de ekle:

[ ] Her grafikte saatlik noktaya tıklayınca o saate ait wait gösteriliyor
[ ] Bir grafiğin hour selection'ı diğer grafikleri değiştirmiyor
[ ] International Departure detayında passport/security ayrımı korunuyor
[ ] Frontend wait hesaplamıyor
[ ] Source refresh yaklaşık 5 dakikalık cadence'e uygun
[ ] Aynı source tekrar gelirse duplicate passenger yaratılmıyor
[ ] Yeni veri yokken queue normal şekilde ilerliyor
[ ] Yeni flight yalnız bir kez mevcut queue hesabına dahil ediliyor
[ ] Delay stale demand'i migrate ediyor
[ ] Cancellation future demand'i kaldırıyor
[ ] Refresh queue'yu sıfırlamıyor
[ ] Refresh DB'yi destructive şekilde değiştirmiyor
[ ] Overlapping refresh duplicate üretmiyor
[ ] Prediction source scope yalnız airport'un ilgili operational day flight'ları
[ ] Flight operational date ile queue event timestamp birbirinden ayrılmış
[ ] Midnight crossing passenger kaybına yol açmıyor
[ ] Previous-day remaining backlog midnight'ta sessizce silinmiyor
[ ] 5 dakika source cadence queue timestep olarak kullanılmıyor
[ ] Same-data refresh ile queue progression birbirinden bağımsız
[ ] Passenger conservation her refresh sonrasında korunuyor

================================================== 69. FINAL RAPORA EKLE
==================================================

Final raporda ayrıca:

OPERATIONAL DAY FILTER:
PASS / FAIL

AIRPORT LOCAL TIMEZONE:
...

SAME-DATA 5-MIN REFRESH:
PASS / FAIL

QUEUE PROGRESSED WITHOUT NEW FLIGHT:
PASS / FAIL

NEW-FLIGHT DELTA INGESTION:
PASS / FAIL

DUPLICATE PASSENGER AFTER REFRESH:
0 / X

MIDNIGHT QUEUE CARRY:
PASS / FAIL

GRAPH HOUR CLICK WAIT:
PASS / FAIL

INTERNATIONAL DEPARTURE WAIT BREAKDOWN:
PASS / FAIL

REFRESH DATABASE SAFETY:
PASS / FAIL

PASSENGER CONSERVATION ACROSS REFRESH:
PASS / FAIL

değerlerini SAYISAL kanıtlarla raporla.
