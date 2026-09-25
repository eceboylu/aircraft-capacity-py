Tabii. 15:00 kalkışlı bir dış hat uçuşu için saatlik ve modelde kullanması kolay bir başlangıç dağılımı istiyorsan şöyle yapardım:
Havaalanına geliş Kalkışa kalan süre Yolcu oranı
11:00–12:00 3–4 saat önce %10
12:00–13:00 2–3 saat önce %35
13:00–14:00 1–2 saat önce %45
14:00–15:00 0–1 saat önce %10
Toplam %100

Mesela uçakta 300 yolcu varsa:
11–12 → 30 kişi
12–13 → 105 kişi
13–14 → 135 kişi
14–15 → 30 kişi
Burada yine 105 kişinin tam 12:00'de bir anda geldiğini varsaymamalısın. Saatlik %35, o saat boyunca dağıtılacak toplam yolcu miktarı olsun.
Örneğin sistemin 5 dakikalık simülasyon yapıyorsa, 12:00–13:00 arasındaki 105 kişi yaklaşık 105 / 12 = 8–9 kişi / 5 dk olarak sisteme girer.
Bu yapı senin Flight Queue için bence oldukça kullanışlı: uçuş bazında saatlik show-up yüzdesi → 5 dakikalık passenger event'lere dönüştür → o sırada security/passport kuyruğu da yolcu işlemeye devam etsin.

14

Eğer uçuş 14:00'te kalkıyorsa, aynı dağılımı 1 saat erkene kaydırabiliriz:
Havaalanına geliş Kalkışa kalan Yolcu oranı
10:00–11:00 3–4 saat %10
11:00–12:00 2–3 saat %35
12:00–13:00 1–2 saat %45
13:00–14:00 0–1 saat %10
Toplam %100

Örneğin 200 yolcu varsa sırasıyla 20 → 70 → 90 → 20 kişi olur.
Yani sabit saatlere bağlı değil; uçuş saati neyse sistem bu profili otomatik olarak geriye doğru kaydırabilir.

14.00 in %10 ı 13-14.00 arası ise %45i 12.00-13.00 arası ise bune göre diğer yüzdelikler nasıl olur

Aynen, verdiğimiz dağılıma göre 14:00 uçuşu için yüzdelerin tamamı şöyle:
Saat aralığı Kalkıştan önce Gelen yolcu %
10:00–11:00 3–4 saat önce %10
11:00–12:00 2–3 saat önce %35
12:00–13:00 1–2 saat önce %45
13:00–14:00 0–1 saat önce %10
TOPLAM %100

Yani kodda uçuş saatine göre düşünürsek:
-4h → -3h = %10
-3h → -2h = %35
-2h → -1h = %45
-1h → 0h = %10
Örneğin 300 yolcu varsa: 30 + 105 + 135 + 30 = 300 yolcu.
Ama burada önemli bir nokta var: Bu yüzdeler bizim oluşturduğumuz basitleştirilmiş model dağılımı, gerçek ölçülmüş evrensel yüzdeler değil. Flight Queue'da production default olarak kullanacaksak domestic/international ayrımını da yapmamız daha sağlıklı olur.

dün 20:55
Bunu grafiklere yaydığımı düşün şimdi 12-13.00 grafiği için
17.00yolcularından %10
16.00 dan%35
15.00dan %45
14.00 dan % 10
Du ya mesela şimdi bunu şu şekil yapıcaz mesela 13.40 uçağı var max 3-4 saat diyoruz ya 3-4 saat aralığı 9.40-10.40 arası oluyor bunu biz nasıl yerleştiricez grafiğe eğer grafiği 9-10 arası 10-11 diye grafiğe koyacaksak

Aynen, burada kritik nokta bu. 13:40 uçuşunun %10luk 3–4 saat önce gelen kısmı 09:40–10:40'a düşüyor. Ama grafiğin sabit 09:00–10:00, 10:00–11:00 şeklindeyse bu %10'u tek bir bara koymamalısın; iki bara süreyle orantılı bölmelisin.
09:40–10:40 toplam 60 dakika:

- 09:40–10:00 = 20 dakika → %10 × 20/60 = %3.33
- 10:00–10:40 = 40 dakika → %10 × 40/60 = %6.67
  Aynı işlemi diğer show-up aralıklarına da yapıyoruz.
  13:40 uçuşunun tamamı
  Bizim profilimiz:
  Uçuşa kalan Gerçek zaman Uçuş yolcularının oranı
  3–4 saat 09:40–10:40 %10
  2–3 saat 10:40–11:40 %35
  1–2 saat 11:40–12:40 %45
  0–1 saat 12:40–13:40 %10

Şimdi bunu saatlik grafik barlarına dağıtalım:
Grafik saati Hesap 13:40 uçuşundan gelen
09:00–10:00 %10 × 20/60 %3.33
10:00–11:00 %10×40/60 + %35×20/60 %18.33
11:00–12:00 %35×40/60 + %45×20/60 %38.33
12:00–13:00 %45×40/60 + %10×20/60 %33.33
13:00–14:00 %10×40/60 %6.67
TOPLAM %100

Mesela uçakta 300 yolcu varsa grafik yaklaşık şöyle olur:
09–10 → 10 kişi
10–11 → 55 kişi
11–12 → 115 kişi
12–13 → 100 kişi
13–14 → 20 kişi
Toplam = 300.
Dolayısıyla senin sistemde uçuşun 13:00, 13:40, 13:55 olması hiç problem değil. Her uçuş için önce gerçek show-up zaman aralıklarını oluşturuyoruz, sonra her saatlik grafik bucket'ıyla ne kadar kesiştiğini hesaplayıp o oranda yolcuyu o saate yazıyoruz.
Bu yöntem özellikle senin sistem için güzel, çünkü 13:40 uçuşunu zorla 10:00, 11:00, 12:00 gibi yuvarlamıyoruz. Dakika hassasiyetini koruyoruz ama frontend grafiği yine temiz şekilde 09–10 / 10–11 / 11–12... gösterebiliyor.
O zaman grafikte nasıl göstericez onu anlamadım
Şöyle düşün: grafiğin sütunları yine saatlik kalacak. Arkada 5 dakikalık hesap yapmamız kullanıcının 5 dakikalık grafik göreceği anlamına gelmiyor.

Örneğin 12:00–13:00 arkasında sistem şunları hesaplamış olsun:

Saat O anda bekleme
12:00 2 dk
12:05 4 dk
12:10 7 dk
12:15 10 dk
12:20 15 dk
12:25 20 dk
12:30 25 dk
12:35 22 dk
12:40 18 dk
12:45 13 dk
12:50 8 dk
12:55 4 dk
Ama kullanıcıya bunların hepsini göstermiyoruz.

Bunlardan 12:00–13:00 için tek bir değer çıkarıyoruz. Örneğin yolcu ağırlıklı ortalama 15 dakika çıktı.

Grafik:

Bekleme
│
25│
20│
15│ █
10│ █ █ █
5│ █ █ █
0└──────────────────────
11-12 12-13 13-14

           ↑
         15 dk

Yani senin mevcut 24 saatlik grafiğinde her saat için bir bar/nokta var:

10–11 → 4 dk
11–12 → 8 dk
12–13 → 15 dk
13–14 → 21 dk
14–15 → 12 dk
15–16 → 6 dk
12–13'ün 15 dk olması ne demek?
“12:00 ile 13:00 arasında bu kontrol noktasına gelen bir yolcunun tahmini ortalama bekleme süresi 15 dakika.”

Bu, saat 12:05'te kesin 15 dakika bekleneceği anlamına gelmez. 12:05'te 4 dakika, 12:30'da 25 dakika olabilir. 15 dakika o saatlik grafiğin özetidir.

Ben tooltip'e biraz daha bilgi de koyardım:

12:00–13:00

Average wait 15 min
Peak wait 25 min
Passengers 500
Peak queue 130
Ana grafikte sadece 15 dk gösterilir. Detay isteyen kullanıcı üzerine geldiğinde diğerlerini görür.

Ve senin dört grafiğin varsa bunu her process için ayrı yapabiliriz:

Domestic Security → saatlik average wait
International Security → saatlik average wait
Departure Passport → saatlik average wait
Arrival Passport → saatlik average wait

Böylece arkada çok detaylı dakika/5-dakika simülasyonu çalışırken frontend 24 tane temiz saatlik değer göstermeye devam eder.

Ama bunu her saat için her grafikteki saat dilimi için ve her uçak her yolcu için yapmak zorunda biliyosun dimi ve domestic departure mi internationl departure mi arrival mı sonra ayrılacak departure passport ve security hesabı ve arrival hesabı . Ayrıca her havaalanının ve o havaalanındaki domestic departure security domestic arrival international departure securty ve international departure pasaport gişelerinin birde international arrivaldaki pasaport gişelerinin kendine özel değerleri olacak ayrıca international departure pasaporttan çıkan securitye gidecek ve orda da akış devam edecek biyandan pasaporttan giden yolcular securitye giderken securitydekiler akmaya devam edecek o sırada arkadan formül aynı pasaport kısmına gelen yolcu var mı bakacak. Yani bu her havalanı için özel bir genel grafik gerektiriyo yanında her havaalanına özel gişe security hesapları gerektiriyor bunu nasıl yapıcaz
Evet. Anlattığın sistem artık basitçe “uçuş sayısını al → Erlang-C çalıştır → grafik çiz” değil. Her havalimanının kendi kaynakları olan, yolcuların aşamalar arasında aktığı stateful bir kuyruk simülasyonu oluyor.

Ve bunu her yolcu için ayrı bir Python nesnesi yaratıp milyonlarca yolcuyu tek tek dolaşarak yapmak zorunda değiliz. Yolcuları akış miktarları halinde taşıyabiliriz. Bu hem daha hızlı hem de senin grafiklerin için yeterli.

Yapıyı şöyle kurardım
Her havalimanının bağımsız bir state'i olacak:

IST
│
├── Domestic Departure
│ └── Security
│
├── Domestic Arrival
│ └── [queue yoksa doğrudan çıkış]
│
├── International Departure
│ ├── Passport
│ │ ↓
│ └── Security
│
└── International Arrival
└── Passport

SAW
│
├── Domestic Departure → Security
├── International Departure → Passport → Security
└── International Arrival → Passport

AMS
│
└── kendi config + kendi queue'ları
Ve IST'nin değerleri SAW'ı kesinlikle etkilemeyecek.

Her havalimanı için config'te örneğin:

AirportOperationalConfig(
airport="IST",

    domestic_departure_security_lanes=20,

    international_departure_passport_counters=35,
    international_departure_security_lanes=25,

    international_arrival_passport_counters=40,

    passport_service_rate=...,
    security_service_rate=...

)

gibi ayrı kapasite değerleri bulunacak.

Asıl motor: zaman ilerleyecek
Bence bunu anlamanın en kolay yolu IST saat 12:00–12:05 örneği.

O anda sistem önce uçuşlardan gelen talebe bakıyor.

Diyelim:

Domestic Departure show-up
→ 70 kişi

International Departure show-up
→ 100 kişi

International Arrival
→ 120 kişi
Bunları route ediyoruz:

70 Domestic Departure
↓
Domestic Security

100 International Departure
↓
Departure Passport
↓
International Security

120 International Arrival
↓
Arrival Passport
Sonra aynı 5 dakikalık zaman adımında her queue kendi kapasitesine göre çalışıyor.

International Departure özellikle önemli
Senin dediğin tam olarak şu:

                         YENİ UÇUŞLAR
                             │
                             ▼
                    yeni departure yolcuları
                             │
                             ▼
                    DEPARTURE PASSPORT
                    ┌──────────────────┐
                    │ mevcut queue     │
                    │ + yeni yolcu     │
                    │ - işlenen yolcu  │
                    └────────┬─────────┘
                             │
                      passporttan çıkan
                             │
                             ▼
                         transfer
                             │
                             ▼
                 INTERNATIONAL SECURITY
                    ┌──────────────────┐
                    │ mevcut queue     │
                    │ + passport çıkış │
                    │ - işlenen yolcu  │
                    └──────────────────┘

Bu zincirleme queue.

Mesela 12:00–12:05:

Passport:

önceden bekleyen 40
yeni gelen 100
───
toplam 140

5 dk kapasitesi 80
───
kalan queue 60
O 80 kişi passport'tan çıktı.

Ama bu 80 kişiyi sistemden silmiyoruz.

International Security'ye aktarıyoruz:

Security eski queue 30
Passport'tan gelen 80
───
Security demand 110

Security 5dk capacity 70
───
Security queue 40
Aynı anda arkadan yeni yolcular Passport'a gelmeye devam ediyor.

Sonra saat 12:05–12:10
Passport sıfırlanmıyor:

Önceki passport queue = 60

-

12:05–12:10 yeni show-up = 90

= 150

Passport işler = 80

Kalan = 70
Passport'tan çıkan 80:

                  ↓

International Security

önceki queue = 40

- passport çıkışı = 80

= 120

security işler = 70

kalan = 50
Sonra 12:10...

Sonra 12:15...

24 saat boyunca böyle ilerliyor.

Bu senin söylediğin:

bir yandan passport'a yeni yolcu geliyor, passport yolcu işliyor, çıkan security'ye gidiyor ve security de kendi kuyruğunu eritiyor

mantığının aynısı.

Arrival tamamen ayrı state
International Arrival için:

Aircraft arrival
↓
arrival release/buffer
↓
Arrival Passport
↓
Exit
Departure Passport'taki yoğunluk Arrival Passport'u etkilemez eğer fiziksel olarak farklı gişe havuzları kullanıyorlarsa.

Config buna göre belirlenir.

Domestic Departure da ayrı
Domestic flight
↓
show-up
↓
Domestic Security
↓
Gate
Passport yok.

Domestic Arrival için mevcut kuralın buysa:

Domestic Arrival
↓
No queue model
Ve bütün bunların üzerinde tek bir zaman motoru olacak
Kod açısından kritik nokta bu.

Her grafik için ayrı algoritma yazmayacağız.

Tek engine:

for airport in airports:

    state = create_airport_state(airport)

    for time in simulation_timeline:

        # 1 — O anda uçuşlardan gelen yeni yolcular
        demand = calculate_flight_demand(
            airport,
            time
        )

        # 2 — Domestic departure
        state.domestic_security.add(
            demand.domestic_departure
        )

        # 3 — International departure
        state.departure_passport.add(
            demand.international_departure
        )

        passport_completed = (
            state.departure_passport.process()
        )

        # Passport'tan çıkan Security'ye
        state.international_security.add(
            passport_completed
        )

        state.international_security.process()

        # 4 — International arrival
        state.arrival_passport.add(
            demand.international_arrival

Gerçek implementation bundan daha detaylı olur ama mimarinin özü bu.

Grafikler ise simülasyondan SONRA oluşturulur
Bu çok önemli.

Grafikler queue hesaplamasını yönetmeyecek.

Queue engine önce örneğin:

IST — 12:00
IST — 12:05
IST — 12:10
IST — 12:15
...
snapshot'larını üretir.

Her snapshot'ta:

domestic_security:
arrivals
processed
queue
wait

departure_passport:
arrivals
processed
queue
wait

international_security:
arrivals
processed
queue
wait

arrival_passport:
arrivals
processed
queue
wait
olur.

Sonra API bunları saatlik toplar:

5 dakikalık simulation
↓
12:00
12:05
12:10
...
12:55
↓
hourly aggregation
↓
12:00–13:00
Ve frontend'e:

{
"airport": "IST",
"hour": "12:00",

"domestic_security": {
"passengers": 420,
"average_wait": 7,
"peak_wait": 12
},

"departure_passport": {
"passengers": 510,
"average_wait": 16,
"peak_wait": 24
},

"international_security": {
"passengers": 495,
"average_wait": 11,
"peak_wait": 18
},

"arrival_passport": {
"passengers": 620,
"average_wait": 21,
"peak_wait": 34
}
}

gibi sonuç gönderilebilir.

Böylece senin mevcut 4 graph frontend mantığı da bozulmak zorunda değil.

Bir önemli düzeltme daha
International Departure için Passport'tan çıkan kişiyi aynı saniyede Security'ye ışınlamamak daha gerçekçi.

Örneğin:

Passport tamamlandı
↓
3–8 dk walking / transition
↓
International Security
gibi deterministic bir transfer delay koyabiliriz.

O zaman:

12:05 Passport'tan 80 kişi çıktı

→ 12:10/12:15 civarında
Security demand'e eklenir.
Bu da ayrı bir event queue ile çok temiz çözülür.

Erlang-C burada nereye oturuyor?
Burada önceki konuşmamızdaki önemli noktaya geliyoruz.

Artık elimizde gerçek bir state var:

queue(t-1) + arrivals(t) - processed(t)

Bu yüzden queue uzunluğunu Erlang-C'nin üretmesine gerek yok.

Erlang-C'yi her process için:

λ = geliş hızı
μ = servis hızı
c = aktif gişe/lane

ile bekleme davranışının istatistiksel tahmini tarafında kullanabiliriz.

Ama queue state'i:

önceki kuyruk + yeni gelen − işlenen

şeklinde zaman boyunca taşınmalı.

Bu ikisini doğru ayırmak önemli; aksi halde hem simülasyon kuyruğunu hem Erlang-C kuyruğunu üst üste bindirip yolcuyu iki kez sayma riski oluşur.

Yani projenin yeni merkezi aslında grafikler değil:

Airport Queue Simulation Engine

olmalı.

Her havaalanı → kendi config'i → kendi uçuşları → kendi passenger show-up'ı → kendi dört bağımsız/bağlantılı queue state'i → 5 dakikalık snapshot'lar → en son 24 saatlik dört grafik.

Bu mimariyi kurduğumuzda 78 havalimanı da olsa algoritma değişmiyor; sadece her havalimanının config'i ve o günkü uçuşları değişiyor.

Peki burada dediğin 12.40 ta uçaşı olan yolcuların %10 luk diliminin 8.40-9.40 a geldiğini biliyoruz bunu her uçak yolcu ve saatlık dilim için yapıcaz ama bu yolcuyu koymaya 8.40-9.40 saatimiz yok bi kısmını 8-9 bi kısmını 9-10 a koymalıyız bu hesaplamayı bu mimarinin neresinde nasıl yapıcaz ve her uçak saatlik dilim havaalanı ve domastic internationel gibi daha demin dediklerimi nasıl ayırıp yapıyoruz
Evet. Ve bu hesap queue formülünden önce, Passenger Demand → Show-up Distribution katmanında yapılmalı. Yani 08:40–09:40 diye bir grafik sütunu yaratmıyoruz. Önce yolcunun gerçek zaman üzerindeki gelişini hesaplıyoruz; sonra bunu 08–09 ve 09–10 grafik dilimlerine topluyoruz.

12:40 uçuşu üzerinden gidelim
Diyelim uçakta 300 tahmini yolcu var ve profil:

4–3 saat → %10
3–2 saat → %35
2–1 saat → %45
1–0 saat → %10
Gerçek show-up aralıkları:

08:40–09:40 → 30 kişi
09:40–10:40 → 105 kişi
10:40–11:40 → 135 kişi
11:40–12:40 → 30 kişi
Ama bunları doğrudan saatlik grafiğe koymayacağız.

Önce 5 dakikalık timeline'a çevireceğiz.

İlk %10 = 30 kişi:

08:40–08:45 → 2.5
08:45–08:50 → 2.5
08:50–08:55 → 2.5
08:55–09:00 → 2.5

09:00–09:05 → 2.5
09:05–09:10 → 2.5
...
09:35–09:40 → 2.5
Çünkü:

30 kişi / 12 adet 5-dk slot = 2.5 kişi/slot

Bu nedenle 08–09 = 10 kişi, 09–10'a bu profilden 20 kişi düşüyor.

Ama 09–10'a hemen sonraki %35 profili de giriyor:

09:40–10:00 = 20 dakika

105 kişinin 20/60'ı:

35 kişi

Dolayısıyla 12:40 uçuşunun toplam katkısı:

08–09 → 10

09–10 → 20 + 35
→ 55

10–11 → 70 + 45
→ 115

11–12 → 90 + 10
→ 100

12–13 → 20
Toplam yine 300.

Bu mimarinin tam olarak neresinde?
Akışımızı biraz daha netleştirelim:

                  AIRPORT
                     ↓
                   FLIGHT
                     ↓
         ┌───────────┴───────────┐
         ↓                       ↓
      DOMESTIC              INTERNATIONAL
         ↓                       ↓
     ARR / DEP                ARR / DEP
                                 ↓
                     Aircraft capacity
                             ×
                        load factor
                             ↓
                    PASSENGER DEMAND
                             ↓
                  SHOW-UP DISTRIBUTION    ← BURASI
                             ↓
                     5-MIN EVENTS
                             ↓
                       QUEUE ENGINE
                             ↓
                    5-MIN SNAPSHOTS
                             ↓
                  HOURLY AGGREGATION
                             ↓
                         GRAPH

Yani 08:40–09:40'ı 08–09 / 09–10'a bölme işi queue engine'in görevi değil.

show-up distribution katmanının görevi.

Peki 78 havalimanı + binlerce uçuş?
Burada da her kombinasyon için özel kod yazmıyoruz.

Her uçuş zaten şuna benzer bilgi taşıyor:

flight = {
"airport": "IST",
"departure": "12:40",

    "direction": "departure",
    "international": True,

    "passengers": 300

}

Show-up generator bu uçuşu alıyor.

Önce route belirliyor:

if flight.is_domestic and flight.is_departure:
route = "DOMESTIC_SECURITY"

elif flight.is_international and flight.is_departure:
route = "DEPARTURE_PASSPORT"

elif flight.is_international and flight.is_arrival:
route = "ARRIVAL_PASSPORT"

elif flight.is_domestic and flight.is_arrival:
route = None

Senin mevcut routing kuralınla uyumlu.

Sonra event oluşturuyor
12:40 IST International Departure için:

IST
International Departure
Flight TKxxx
300 passengers
şuna dönüşüyor:

08:40 → Departure Passport +2.5
08:45 → Departure Passport +2.5
08:50 → Departure Passport +2.5
...
09:40 → Departure Passport +8.75
09:45 → Departure Passport +8.75
...
Buradaki önemli şey:

Bu yolcular henüz International Security'ye yazılmıyor.

Çünkü International Departure:

show-up → Passport → Security

akışına sahip.

Aynı anda başka uçuş geliyor
Mesela IST'de:

TK1 → 12:40 → International → 300
TK2 → 12:40 → International → 200
TK3 → 12:45 → International → 250
TK4 → 13:10 → International → 180
TK5 → 13:30 → Domestic → 220
Her biri aynı fonksiyondan geçiyor.

Sonuç örneğin 09:00–09:05 için:

IST

International Departure:
TK1 → 2.5
TK2 → 1.67
TK3 → 2.08
TK4 → ...
────────────────
Departure Passport arrivals = X

Domestic Departure:
TK5 → ...
────────────────
Domestic Security arrivals = Y
Yani uçuşları kaybetmiyoruz ama queue seviyesinde aynı process'e giden yolcuları topluyoruz.

Havalimanları da kesinlikle karışmıyor
Event'in anahtarı aslında şöyle:

airport

- process
- timestamp
  Örneğin:

IST | DEPARTURE_PASSPORT | 09:00 → 42.5
IST | DOMESTIC_SECURITY | 09:00 → 31.2

SAW | DEPARTURE_PASSPORT | 09:00 → 21.8
SAW | DOMESTIC_SECURITY | 09:00 → 18.4

AMS | DEPARTURE_PASSPORT | 09:00 → ...
Böylece IST'nin yolcusu SAW'a hiçbir zaman karışamaz.

Sonra Queue Engine devralıyor
Mesela elimizde:

IST
09:00–09:05

Departure Passport

önceki queue = 25
yeni flight show-up = 42
oldu.

Queue:

25 + 42 = 67 kişi
IST config'ten:

IST departure passport
counter_count = 10
service_rate = ...
alıyoruz.

Diyelim 5 dakikada 40 kişi işlenebiliyor:

67 - 40 = 27

09:05 queue = 27
Passport'tan çıkan 40 kişi ise:

40
↓
transfer/walking delay
↓
International Security
event'i oluyor.

Sonraki adım:

09:05–09:10

Passport:
27 eski

- yeni uçuşlardan 38
  = 65

işlenen = 40
kalan = 25

International Security:
önceki security queue

- biraz önce Passport'tan çıkanlar
- varsa transferden şimdi ulaşanlar
  → işler
  → yeni security queue
  Ve bu sürekli devam ediyor.

En son grafik oluşturuluyor
İşte ancak bütün bunlardan sonra 08–09 / 09–10 grafiğini oluşturuyoruz.

5 dakikalık sonuçlarımız:

08:00
08:05
08:10
...
08:55
toplanıp:

08:00–09:00

oluyor.

Sonra:

09:00
09:05
...
09:55
→ 09:00–10:00

oluyor.

Dolayısıyla sistemin iç dünyasında aslında “08–09 grafiği” diye bir şey yok. Sistem gerçek timestamp'lerle çalışıyor.

Grafik sadece sonuçları sonradan saatlik paketliyor.

Kod mimarisini de buna göre ayırmalıyız
Senin aircraft-capacity-py için mantıksal olarak şu ayrım çok temiz olur:

Flight data
↓
Demand Generator
│ aircraft capacity
│ load factor
↓
Passenger Demand
↓
Show-Up Generator ← 12:40 → 08:40–12:40 dağılımı
↓
5-minute Events
↓
Routing
├── Domestic Departure → Domestic Security
├── Intl Departure → Departure Passport
├── Intl Arrival → Arrival Passport
└── Domestic Arrival → none
↓
Airport Simulation Engine
│
├── Departure Passport
│ ↓
│ transfer
│ ↓
├── International Security
│
├── Domestic Security
│
└── Arrival Passport
↓
5-minute Queue Snapshots
↓
Hourly Aggregator ← 08:00–09:00 burada oluşuyor
↓
API
↓
4 Graph Frontend
En önemli tasarım kararı şu: uçuşları en başta saatlik grafik kutularına bölüp sonra kuyruk hesaplamaya çalışmayalım. Önce 5 dakikalık gerçek zaman çizelgesini üretelim, bütün queue akışını bunun üzerinde çalıştıralım; saatlik grafik en son oluşsun.

Böyle yaptığımızda 12:40, 12:43, 16:57 uçuşu olması fark etmiyor; aynı algoritma otomatik olarak doğru 5 dakikalık slotlara ve sonunda doğru saatlik grafiğe dağıtıyor.

Hangi hesaplamaları hangi kısımda yapıcaz nasıl kurucaz bunu bana detaylı anlat ve bütün anlattıklarıma göre seninle konultuğumuz bütin detayları formülleri içeren bir yazı ver eksiksiz bunu okuyan kişi direk projeyi anlayıp detaylara hakim olup yapabilmeli o sevşyede ver hangi aşamada hangi formül nasıl kullanılıyo bunu ver bana ayrıca uçakların kapasitesini icao kodlarından buluyo bunuda unutma
Evet. Şimdi bunu tek bir teknik tasarım dokümanı gibi, baştan sona kurayım. Buradaki kritik fikir şu:

Uçuş verisi → ICAO'dan kapasite → beklenen yolcu → domestic/international + arrival/departure ayrımı → departure show-up dağılımı → 5 dakikalık yolcu akışı → havalimanına özel Passport/Security kuyrukları → process'ler arası yolcu transferi → bekleme hesabı → saatlik aggregation → 24 saatlik grafikler.

Ve bütün bunlar her havalimanı için bağımsız çalışacak.

1. Sistemin amacı
   Sistem her havalimanı için dört temel operasyonel akışı hesaplayacak:

Yolcu tipi İlk işlem Sonraki işlem
Domestic Departure Domestic Security Gate
Domestic Arrival Queue yok Exit
International Departure Departure Passport International Security → Gate
International Arrival Arrival Passport Exit
Dolayısıyla temel queue'larımız:

1. DOMESTIC_DEPARTURE_SECURITY
2. INTERNATIONAL_DEPARTURE_PASSPORT
3. INTERNATIONAL_DEPARTURE_SECURITY
4. INTERNATIONAL_ARRIVAL_PASSPORT
   Bunların counter/lane sayısı, servis hızı, verimliliği vs. havalimanına özel olacak.

5. Sistemde zaman nasıl işleyecek?
   En önemli mimari karar:

Queue motoru saatlik çalışmayacak.

Örneğin:

08:00
08:05
08:10
08:15
...
23:55
şeklinde 5 dakikalık simulation step kullanacağız.

Ama frontend:

08–09
09–10
10–11
...
şeklinde saatlik gösterebilir.

Yani:

5 dakika = hesaplama çözünürlüğü

1 saat = görüntüleme çözünürlüğü

Bunları birbirinden ayırıyoruz.

3. AŞAMA — Ham uçuş verisini normalize et
   Her uçuş için minimum olarak şunları elde etmeliyiz:

flight_id
flight_number

airline_iata
airline_icao

aircraft_icao

departure_airport
arrival_airport

departure_time
arrival_time

estimated_departure
estimated_arrival

actual_departure
actual_arrival

status
Ve bizim simülasyon yaptığımız havalimanına göre:

direction = ARRIVAL / DEPARTURE
belirlenir.

Örneğin IST simülasyonu:

IST → FRA
= DEPARTURE

FRA → IST
= ARRIVAL 4. AŞAMA — Aircraft ICAO → uçak kapasitesi
Bunu özellikle unutmuyoruz.

Uçuş:

aircraft_icao = A321
getiriyorsa sistem bunu kapasite dataset'inde arayacak.

Senin mevcut:

data/yolcu_ucaklari.json
gibi kapasite verinden:

A321
→ typicalCapacity
bulunacak.

Mantık:

Flight
↓
aircraft_icao
↓
AircraftCapacityService
↓
typical passenger capacity
Örneğin tamamen temsili:

aircraft_icao = A321
capacity = 220
Ama:

220 = 220 yolcu geliyor demek değil.

Bu yalnızca uçak kapasitesi.

5. AŞAMA — Beklenen yolcu sayısı
   Burada load factor uygulanıyor.

Mevcut sistemindeki mantık:

Domestic → 0.78
International short → 0.82
International medium → 0.84
International long → 0.88
Unknown → 0.83
Uçuş süresi:

d
u
r
a
t
i
o
n
=
a
r
r
i
v
a
l
s
c
h
e
d
u
l
e
d
−
d
e
p
a
r
t
u
r
e
s
c
h
e
d
u
l
e
d
duration=arrival
scheduled
​
−departure
scheduled
​

Sonra:

P
a
s
s
e
n
g
e
r
D
e
m
a
n
d
=
A
i
r
c
r
a
f
t
C
a
p
a
c
i
t
y
×
L
o
a
d
F
a
c
t
o
r
PassengerDemand=AircraftCapacity×LoadFactor
Örneğin:

capacity = 220
load factor = 0.84
220
×
0.84
=
184.8
220×0.84=184.8
Yaklaşık:

185 yolcu

Bu uçuşun queue modeline girecek beklenen passenger demand'i.

6. AŞAMA — Domestic / International belirle
   Sonra uçuşun route tipi belirleniyor.

Örneğin:

IST → AYT
Domestic Departure

IST → FRA
International Departure

FRA → IST
International Arrival

AYT → IST
Domestic Arrival
Böylece:

                  FLIGHT
                    │
          ┌─────────┴─────────┐
          │                   │
      DOMESTIC          INTERNATIONAL
          │                   │
      ┌───┴───┐           ┌───┴───┐
      │       │           │       │
     DEP     ARR         DEP     ARR

7. AŞAMA — Routing
   Burada yolcunun hangi queue'ya gireceğine karar veriyoruz.

Domestic Departure
Aircraft passenger demand
↓
Show-up
↓
DOMESTIC SECURITY
↓
Gate
Domestic Arrival
Mevcut modelimize göre:

Aircraft
↓
Arrival
↓
No modeled queue
International Departure
Show-up
↓
DEPARTURE PASSPORT
↓
Walking / transfer
↓
INTERNATIONAL SECURITY
↓
Gate
International Arrival
Aircraft arrival
↓
release buffer
↓
ARRIVAL PASSPORT
↓
Exit
Burada departure ve arrival aynı hesap değil.

8. AŞAMA — Departure Show-Up Profile
   İşte konuştuğumuz %10 / %35 / %45 / %10 burada uygulanıyor.

Şimdilik model varsayımımız:

Departure'a kalan süre Yolcu oranı
4–3 saat %10
3–2 saat %35
2–1 saat %45
1–0 saat %10
Toplam:

0.10

- 0.35
- 0.45
- # 0.10
  1
  0.10+0.35+0.45+0.10=1
  Bu bir model varsayımıdır; gerçek havaalanı verisi elde edilirse configurable/calibrated yapılmalı.

9. Örnek — 12:40 uçuşu
   Diyelim:

Departure = 12:40
Passenger demand = 300
Dağılım:

%10
300
×
0.10
=
30
300×0.10=30
08:40–09:40
30 yolcu
%35
300
×
0.35
=
105
300×0.35=105
09:40–10:40
105 yolcu
%45
300
×
0.45
=
135
300×0.45=135
10:40–11:40
135 yolcu
%10
300
×
0.10
=
30
300×0.10=30
11:40–12:40
30 yolcu
Toplam:

30

- 105
- 135
- # 30
  300
  30+105+135+30=300

10. AŞAMA — Show-up'ı 5 dakikalık event'lere böl
    Burada çok önemli bir ayrım var.

08:40–09:40 diye grafik bucket yaratmayacağız.

Bu 30 kişiyi 5 dakikalık simulation timeline'a dağıtacağız.

60 dakika:

60
/
5
=
12
60/5=12
slot.

Dolayısıyla:

30
/
12
=
2.5
30/12=2.5
kişi / 5 dakika.

08:40 → 2.5
08:45 → 2.5
08:50 → 2.5
08:55 → 2.5

09:00 → 2.5
09:05 → 2.5
...
09:35 → 2.5
Bu fractional expected demand. Gerçekte yarım insan yok; fakat model beklenen yolcu sayılarıyla çalıştığı için decimal değer kullanmak doğru. UI'da gerekirse yuvarlarız.

11. Saat sınırına denk gelmeyen uçuş problemi böyle çözülüyor
    12:40 uçuşunun ilk 30 yolcusu:

08:40–09:40
Grafik açısından:

08–09 içinde 20 dakika var.

30
×
20
60
=
10
30×
60
20
​
=10
09–10 içinde 40 dakika:

30
×
40
60
=
20
30×
60
40
​
=20
Ama ikinci show-up aralığı:

09:40–10:40
105 kişi
09–10'a 20 dakika katkı yapıyor:

105
×
20
60
=
35
105×
60
20
​
=35
Dolayısıyla 09–10:

20

- # 35
  55
  20+35=55
  Bu şekilde 12:40 uçuşunun saatlik dağılımı:

08–09 → 10
09–10 → 55
10–11 → 115
11–12 → 100
12–13 → 20
Kontrol:

10

- 55
- 115
- 100
- # 20
  300
  10+55+115+100+20=300
  Hiçbir yolcu kaybolmadı veya iki kere sayılmadı.

12. Bunu her uçuş için ayrı yapıyoruz
    Diyelim IST:

TK001 12:40 International 300
TK002 12:40 International 200
TK003 12:45 International 250
TK004 13:10 International 180
TK005 13:30 Domestic 220
Her uçuş:

aircraft ICAO çözümleme,
capacity,
load factor,
passenger demand,
domestic/international,
arrival/departure,
show-up,
5-minute events
işlemlerinden bağımsız olarak geçiyor.

Daha sonra aynı:

airport + process + timestamp
event'leri birleştiriliyor.

13. Event'in ana kimliği
    Mantıksal olarak:

Airport

- Process
- Timestamp
  Örneğin:

IST | DOMESTIC_SECURITY | 09:00 → 31.4

IST | DEPARTURE_PASSPORT | 09:00 → 42.8

SAW | DOMESTIC_SECURITY | 09:00 → 20.1

SAW | DEPARTURE_PASSPORT | 09:00 → 18.7
Bu yüzden hiçbir havalimanı birbirine karışmıyor.

14. AŞAMA — Havalimanına özel operasyonel config
    Şimdi queue hesaplamasına geçiyoruz.

Her airport kendi config'ine sahip.

Örneğin mantıksal olarak:

IST

Domestic Security
lane_count
service_rate
efficiency

Departure Passport
counter_count
service_rate
efficiency

International Security
lane_count
service_rate
efficiency

Arrival Passport
counter_count
service_rate
efficiency
SAW tamamen farklı olabilir.

AMS tamamen farklı olabilir.

Yani:

C
a
p
a
c
i
t
y
I
S
T
≠
C
a
p
a
c
i
t
y
S
A
W
Capacity
IST
​


=Capacity
SAW
​

olması normal.

15. Server kavramı
    Passport için server:

aktif passport counter

Security için server:

aktif security lane

Dolayısıyla:

# c

s
e
r
v
e
r

c
o
u
n
t
c=server count
Örneğin:

Departure Passport:
c = 35 counters
veya:

Domestic Security:
c = 18 lanes 16. Service rate
Bir server'ın dakikada işleyebildiği yolcu:

# μ

p
a
s
s
e
n
g
e
r
s
/
s
e
r
v
e
r
/
m
i
n
u
t
e
μ=passengers/server/minute
Örneğin bir passport counter ortalama:

0.6 passenger/minute
işliyorsa:

# μ

0.6
μ=0.6
35 counter:

35
×
0.6
=
21
35×0.6=21
kişi/dakika teorik kapasite.

Efficiency uygulanıyorsa:

μ
e
f
f
e
c
t
i
v
e
=
μ
b
a
s
e
×
e
f
f
i
c
i
e
n
c
y
μ
effective
​
=μ
base
​
×efficiency
Ama burada mevcut config'indeki service_rate_per_staff × efficiency_multiplier ile yeni counter/lane modelini tek tanım altında standardize etmemiz gerekir; counter başına mı staff başına mı rate kullandığımız belirsiz kalmamalı.

17. 5 dakikalık servis kapasitesi
    Simulation step:

Δ
t
=
5
Δt=5
dakika.

Process capacity:

S
e
r
v
i
c
e
C
a
p
a
c
i
t
y
=
c
×
μ
×
Δ
t
ServiceCapacity=c×μ×Δt
Örneğin:

c = 10
μ = 0.8 passenger/min
Δt = 5 min
10
×
0.8
×
5
=
40
10×0.8×5=40
Yani 5 dakikada beklenen kapasite:

40 yolcu.

18. AŞAMA — Stateful queue
    İşte “bir yandan geliyor, bir yandan eriyor” kısmı.

Her process kendi queue state'ini taşır.

A
v
a
i
l
a
b
l
e
t
=
Q
t
−
1

- A
  t
  Available
  t
  ​
  =Q
  t−1
  ​
  +A
  t
  ​

Burada:

Q
t
−
1
Q
t−1
​
= önceki slot'tan kalan queue
A
t
A
t
​
= bu 5 dakikada gelen yeni yolcular
İşlenen:

S
t
=
min
⁡
(
A
v
a
i
l
a
b
l
e
t
,
C
a
p
a
c
i
t
y
t
)
S
t
​
=min(Available
t
​
,Capacity
t
​
)
Yeni queue:

Q
t
=
max
⁡
(
0
,
A
v
a
i
l
a
b
l
e
t
−
C
a
p
a
c
i
t
y
t
)
Q
t
​
=max(0,Available
t
​
−Capacity
t
​
)
veya eşdeğer:

Q
t
=
max
⁡
(
0
,
Q
t
−
1

- A
  t
  −
  S
  t
  )
  Q
  t
  ​
  =max(0,Q
  t−1
  ​
  +A
  t
  ​
  −S
  t
  ​
  )

19. Örnek
    09:00:

önceki queue = 25
yeni arrivals = 42
25

- # 42
  67
  25+42=67
  Capacity:

40
Processed:

m
i
n
(
67
,
40
)
=
40
min(67,40)=40
Queue:

67
−
40
=
27
67−40=27
09:05:

old queue = 27
new arrivals = 38
27

- # 38
  65
  27+38=65
  40 işlenir:

65
−
40
=
25
65−40=25
queue kalır.

Yani queue 09:05 geldi diye sıfırlanmaz.

Aynı şekilde:

10:00 geldi diye de sıfırlanmaz.

Saatler yalnızca grafik sınırıdır.

20. International Departure zinciri
    Burada sistemin en önemli bölümlerinden biri var.

International Departure yolcusu:

Show-up
↓
Departure Passport
↓
Security
Dolayısıyla Passport'ta işlenen yolcuları kaybetmiyoruz.

Örneğin:

Passport

old queue = 25
arrivals = 42
capacity = 40

processed = 40
queue = 27
Bu:

processed = 40
kişi bir downstream event oluşturuyor.

21. Passport → Security transferi
    Daha gerçekçi model:

Passport
↓
walking/transition
↓
Security
Örneğin transfer süresi:

T
t
r
a
n
s
f
e
r
=
5

d
a
k
i
k
a
T
transfer
​
=5 dakika
ise:

09:00–09:05 Passport processed = 40
kişinin Security arrival event'i:

09:05/sonrası → International Security
olur.

İleride sabit 5 dakika yerine distribution da kullanılabilir.

22. Security kendi halinde çalışmaya devam ediyor
    Passport'tan yolcu geliyor diye Security durmuyor.

Örneğin:

09:05 International Security

old queue = 30
Passport'tan gelen = 40
30

- # 40
  70
  30+40=70
  Security capacity 50 ise:

p
r
o
c
e
s
s
e
d
=
m
i
n
(
70
,
50
)
=
50
processed=min(70,50)=50
q
u
e
u
e
=
20
queue=20
Aynı anda Passport:

eski queue

- yeni uçuşlardan gelen passenger show-up

* Passport capacity
  hesaplamaya devam ediyor.

Yani iki queue aynı timeline üzerinde eşzamanlı ilerliyor.

23. Domestic Departure
    Daha basit:

Flight
↓
ICAO capacity
↓
load factor
↓
passenger demand
↓
show-up
↓
5-min arrivals
↓
Domestic Security
Burada Passport yok.

Queue:

Q
t
=
max
⁡
(
0
,
Q
t
−
1

- A
  t
  −
  C
  t
  )
  Q
  t
  ​
  =max(0,Q
  t−1
  ​
  +A
  t
  ​
  −C
  t
  ​
  )

24. International Arrival
    Arrival'da departure show-up kullanılmaz.

Mevcut modelimiz:

effective arrival

- 15 min passport release buffer
  Yani:

T
p
a
s
s
p
o
r
t
=
T
a
r
r
i
v
a
l

- 15
  m
  i
  n
  T
  passport
  ​
  =T
  arrival
  ​
  +15min
  Sonra yolcular Arrival Passport'a giriyor.

Burada ileride uçaktan boşalma + yürüme dağılımı yapabiliriz; fakat mevcut source-of-truth modelde +15 dakika buffer korunabilir.

Sonra:

International Arrival
↓
Arrival Passport
↓
Exit
Arrival Passport'un:

counter_count
service_rate
efficiency
değerleri ayrıca airport config'ten gelir.

25. Domestic Arrival
    Mevcut kapsamda:

Domestic Arrival
→ queue modeline dahil değil
Ama flight DB'de durmaya devam eder.

26. Erlang-C nerede?
    Burada özellikle dikkat etmeliyiz.

Erlang-C klasik bir M/M/c steady-state queue modelidir. Arrival rate
λ
λ, server başına service rate
μ
μ ve server sayısı
c
c kullanır. Kullanım:

# ρ

λ
c
μ
ρ=
cμ
λ
​

ve stabil steady-state için:

ρ
<
1
ρ<1
olması gerekir.

Offered load:

# a

λ
μ
a=
μ
λ
​

27. Erlang-C waiting probability
    P
    w
    a
    i
    t
    =
    a
    c
    c
    !
    1
    1
    −
    ρ
    ∑
    k
    =
    0
    c
    −
    1
    a
    k
    k
    !

- a
  c
  c
  !
  1
  1
  −
  ρ
  P
  wait
  ​
  =
  ∑
  k=0
  c−1
  ​

k!
a
k

​

- c!
  a
  c

​

1−ρ
1
​

c!
a
c

​

1−ρ
1
​

​

Bu, gelen bir yolcunun bütün server'ları dolu bulup bekleme ihtimalidir.

28. Erlang-C average waiting time
    W
    q
    =
    P
    w
    a
    i
    t
    c
    μ
    −
    λ
    W
    q
    ​
    =
    cμ−λ
    P
    wait
    ​

​

Bu ortalama queue waiting time'dır.

Ama önemli:

Erlang-C'yi stateful queue'nun yerine koymuyoruz.

Çünkü bizim gerçek problemimiz transient:

08:40 → uçuş A geliyor
08:45 → A + B
08:50 → başka uçuş
09:00 → yoğunluk
09:15 → azalıyor
ve Passport → Security gibi downstream routing var.

Klasik Erlang-C ise Poisson arrivals, exponential service times, eşdeğer paralel server'lar ve steady-state varsayımlarına dayanır.

29. İki modeli birbirine eklemeyeceğiz
    Şunu yapmayacağız:

W
a
i
t
=
B
a
c
k
l
o
g
W
a
i
t

- E
  r
  l
  a
  n
  g
  C
  W
  a
  i
  t
  Wait=BacklogWait+ErlangCWait
  Bu double counting yaratabilir.

Stateful simulation bize gerçek zamanlı backlog'u verir.

Erlang-C ise ayrı bir stochastic queue metriği olabilir.

Örneğin snapshot:

09:00

queue_length = 27
utilization = 0.84

erlang_p_wait = 0.62
erlang_wq = 6.8 min
Ama final passenger-facing wait'in hangi modelden geleceğini tek bir contract olarak tanımlamalıyız.

30. Ben final wait'i nasıl kurardım?
    Yeni mimaride ana kaynak olarak stateful simulation kullanırdım.

Basit ilk approximation:

B
a
c
k
l
o
g
W
a
i
t
≈
Q
c
μ
BacklogWait≈
cμ
Q
​

Örneğin:

queue = 100
total service rate = 10 passenger/min
100
/
10
=
10
m
i
n
100/10=10min
Fakat bunun daha gelişmiş hali FIFO queue age / passenger cohorts ile hesaplanmalı.

Çünkü arkadan gelen kişinin önünde kaç kişi olduğunu biliyoruz.

31. Cohort mantığı
    Her kişiyi Python object olarak saklamamız gerekmiyor.

Şöyle batch/cohort tutabiliriz:

09:00
Flight TK001
25.4 passenger

09:05
Flight TK002
18.2 passenger

09:10
Flight TK001
20.8 passenger
Queue FIFO çalışır.

Server 40 kişi işliyorsa en eski cohort'tan tüketir.

Böylece gerçekten:

“09:10'da gelen yolcu kaç dakika sonra Passport'tan çıktı?”

hesaplanabilir.

Bu, bekleme süresi için sadece queue/capacity yapmaktan daha güçlüdür.

32. Passenger-facing wait
    Bir cohort:

arrived_queue_at = 09:05
service_started_at = 09:17
ise:

W
a
i
t
=
09
:
17
−
09
:
05
=
12
m
i
n
Wait=09:17−09:05=12min
Bu gerçek simulation wait'i olur.

Bir cohort'un bir kısmı farklı zamanda işlenirse weighted average kullanılır.

33. Risk
    Senin son belirlediğin passenger-facing risk:

wait < 5
→ LOW

5 <= wait < 15
→ MEDIUM

15 <= wait < 30
→ HIGH

wait >= 30
→ CRITICAL

wait = None
→ UNKNOWN

no demand
→ LOW
Burada utilization risk değildir.

Utilization ayrı operational metric.

34. Utilization
    5 dakikalık interval için:

# ρ

λ
c
μ
ρ=
cμ
λ
​

veya operasyonel simulation açısından:

U
t
i
l
i
z
a
t
i
o
n
=
p
r
o
c
e
s
s
e
d
a
v
a
i
l
a
b
l
e

s
e
r
v
i
c
e

c
a
p
a
c
i
t
y
Utilization=
available service capacity
processed
​

gibi gerçek kullanılan kapasite de ayrıca tutulabilir.

İkisini isimlendirmede ayırmak daha iyi:

offered_utilization
actual_capacity_usage 35. 5 dakikalık snapshot
Her:

airport

- process
- 5-minute timestamp
  için şunları kaydetmek isteriz:

arrivals
processed
queue_start
queue_end

capacity
active_servers

service_rate

average_wait
peak_wait

utilization

erlang_p_wait
erlang_wq

risk
Örneğin:

IST
DEPARTURE_PASSPORT
09:05

arrivals = 42
processed = 40

queue_start = 25
queue_end = 27

average_wait = 8.4
peak_wait = 13.1

utilization = 1.00

risk = MEDIUM 36. Saatlik grafik bundan SONRA oluşuyor
Frontend hiçbir uçuş hesabı yapmıyor.

API 5 dakikalık snapshot'ları alıyor:

12:00
12:05
12:10
...
12:55
ve:

12–13
haline getiriyor.

37. Saatlik passenger sayısı
    P
    a
    s
    s
    e
    n
    g
    e
    r
    s
    h
    o
    u
    r
    =
    ∑
    a
    r
    r
    i
    v
    a
    l
    s
    i
    Passengers
    hour
    ​
    =∑arrivals
    i
    ​

12 tane 5 dakikalık interval varsa:

A
h
o
u
r
=
A
1

- A
  2
- ⋯
- A
  12
  A
  hour
  ​
  =A
  1
  ​
  +A
  2
  ​
  +⋯+A
  12
  ​

38. Saatlik average wait
    Basit 12 snapshot ortalaması yapmayacağız.

Çünkü:

12:05 → 5 yolcu
12:30 → 100 yolcu
aynı ağırlığa sahip olmamalı.

Passenger-weighted:

W
h
o
u
r
=
∑
(
A
i
×
W
i
)
∑
A
i
W
hour
​
=
∑A
i
​

∑(A
i
​
×W
i
​
)
​

Daha da doğrusu cohort modelinde:

W
h
o
u
r
=
∑
(
p
a
s
s
e
n
g
e
r
s
j
×
a
c
t
u
a
l
W
a
i
t
j
)
∑
p
a
s
s
e
n
g
e
r
s
j
W
hour
​
=
∑passengers
j
​

∑(passengers
j
​
×actualWait
j
​
)
​

39. Peak wait
    P
    e
    a
    k
    W
    a
    i
    t
    h
    o
    u
    r
    =
    max
    ⁡
    (
    W
    i
    )
    PeakWait
    hour
    ​
    =max(W
    i
    ​
    )
    Bu tooltip'te gösterilebilir.

40. Peak queue
    P
    e
    a
    k
    Q
    u
    e
    u
    e
    h
    o
    u
    r
    =
    max
    ⁡
    (
    Q
    i
    )
    PeakQueue
    hour
    ​
    =max(Q
    i
    ​
    )
41. Grafikte ne göreceğiz?
    Örneğin:

IST
International Departure Passport

08–09 → 4 min
09–10 → 7 min
10–11 → 13 min
11–12 → 21 min
12–13 → 16 min
13–14 → 9 min
12–13 üzerine gelince:

12:00–13:00

Passengers 510
Average wait 16 min
Peak wait 27 min
Peak queue 143
Risk HIGH 42. Her havalimanında dört grafik
Örneğin IST:

IST

GRAPH 1
Domestic Departure Security

GRAPH 2
International Departure Passport

GRAPH 3
International Departure Security

GRAPH 4
International Arrival Passport
SAW:

SAW

aynı dört process
ama SAW'ın:

- uçuşları
- aircraft'ları
- yolcuları
- counter'ları
- lane'leri
- service rate'leri
- queue state'leri
  kullanılır.

Dolayısıyla aynı engine çalışır.

43. Airport isolation
    Bunu özellikle kod seviyesinde garanti etmeliyiz.

State:

AirportSimulationState["IST"]
AirportSimulationState["SAW"]
AirportSimulationState["AMS"]
gibi bağımsız olmalı.

IST queue:

SAW queue'ya asla eklenemez.

Ana grouping key:

airport_iata

- process
- timestamp

44. Process isolation
    Aynı airport içindeki process'ler de ayrı.

IST + DOMESTIC_SECURITY
IST + DEPARTURE_PASSPORT
IST + INTERNATIONAL_SECURITY
IST + ARRIVAL_PASSPORT
Tek bağlantı:

DEPARTURE_PASSPORT
↓
completed passengers
↓
transfer
↓
INTERNATIONAL_SECURITY
Bu explicit routing olacak.

45. Flight isolation
    Her flight'ın demand'i önce bağımsız hesaplanmalı.

Örneğin aynı anda 6 uçak:

TK1 → 300
TK2 → 180
TK3 → 220
TK4 → 195
TK5 → 160
TK6 → 250
Her biri kendi:

ICAO
capacity
load factor
route
departure time
show-up profile
bilgisiyle hesaplanır.

En son aynı queue event'inde birleştirilir.

46. Neden önce uçuşları toplamayacağız?
    Şunu yaparsak:

12:00–13:00
6 flights
= 1,300 passengers
ve direkt queue'ya verirsek yanlış.

Çünkü:

12:05 departure
12:20 departure
12:40 departure
12:55 departure
aynı show-up zamanlarına sahip değil.

Bu yüzden:

Önce flight-level demand → sonra time distribution → en son aggregation.

47. Delay değişirse
    Estimated/actual departure değişirse show-up'ın hangi zamana bağlanacağı ayrıca açık bir business rule olmalı.

Örneğin:

scheduled = 12:40
estimated = 13:30
gerçek hayatta bütün yolcular show-up profilini birebir 50 dakika kaydırmaz.

Bu yüzden ilk versiyonda scheduled departure'a bağlı show-up daha stabil olabilir; delay compression ayrı reason/signal olarak tutulabilir.

Bunu implementation sırasında mevcut delay logic'inle birlikte net contract yapmalıyız.

48. Cancelled / Diverted
    Mevcut kural:

Cancelled
→ demand yok

Diverted
→ normal queue demand'ine dahil etme
Bu korunmalı.

49. Missing aircraft ICAO
    aircraft_icao yoksa sistem çökmemeli.

Senin mevcut capacity resolver katmanların kullanılabilir:

1. exact aircraft/airline mapping
2. weighted/airline mapping
3. aircraft capacity
4. aircraft family
5. default capacity
   Mevcut default:

150
gibi fallback olabilir.

Ama confidence düşürülür.

50. GA aircraft
    Mevcut:

counts_toward_passenger_total = false
olan general aviation tiplerinde:

P
a
s
s
e
n
g
e
r
D
e
m
a
n
d
=
0
PassengerDemand=0
queue demand'ine sokmayız.

51. Confidence
    Tahminin güveni ayrıca tutulmalı.

Mevcut cezaların:

load_factor
boarding_buffer
low_match_rate
null_aircraft
no_baseline
default_config
gibi sebepleri korunabilir.

Queue wait ile confidence birbirinden farklı şeyler:

wait = 18 min
confidence = 0.72
olabilir.

52. Önerdiğim kod katmanları
    Mevcut repository'ne uyarlarken mantıksal olarak:

INGESTION
↓
Flight normalization
↓
Flight classification
↓
AircraftCapacityService
↓
PassengerDemandService
↓
ShowUpDistributionService
↓
PassengerEventGenerator
↓
AirportSimulationEngine
↓
QueueProcess
↓
TransferEventQueue
↓
QueueSnapshot
↓
HourlyAggregator
↓
API
↓
Frontend
Bu separation çok önemli.

53. PassengerDemandService
    Sorumluluğu sadece:

ICAO → capacity
route → load factor

capacity × load factor
olmalı.

Queue bilmemeli.

Graph bilmemeli.

54. ShowUpDistributionService
    Sadece:

departure timestamp

- passenger demand
- profile
  alır.

Çıktı:

timestamp → passenger arrivals
Örneğin:

08:40 → 2.5
08:45 → 2.5
...
Airport queue kapasitesini bilmesine gerek yok.

55. RoutingService
    Şuna karar verir:

DOM DEP
→ DOMESTIC_SECURITY

INT DEP
→ DEPARTURE_PASSPORT

INT ARR
→ ARRIVAL_PASSPORT

DOM ARR
→ NONE 56. AirportSimulationEngine
Asıl orkestratör.

Pseudo-code:

for airport in airports:

    config = load_airport_config(airport)
    flights = load_airport_flights(airport)

    events = generate_events(flights)

    state = create_airport_state(config)

    for timestamp in timeline:

        arrivals = events[timestamp]

        add_domestic_security_arrivals()

        add_departure_passport_arrivals()

        add_arrival_passport_arrivals()

        process_departure_passport()

        schedule_completed_passport_to_security()

        apply_due_security_transfers()

        process_domestic_security()

        process_international_security()

        process_arrival_passport()

        calculate_metrics()

        save_snapshot()

Buradaki işlem sırası deterministik olmalı.

57. Transfer Event Queue
    Özellikle International Departure için:

Passport processed
↓
TransferEvent(
airport=IST,
source=DEP_PASSPORT,
destination=INT_SECURITY,
passenger_count=40,
due_time=09:10
)
09:10 olduğunda:

International Security arrivals += 40
Böylece iki queue'yu temiz biçimde bağlıyoruz.

58. Database tarafı
    Mevcut QueuePrediction tek başına artık bütün transient bilgiyi taşımakta yetersiz kalabilir.

Mantıksal olarak en azından snapshot seviyesinde:

airport_iata
process
timestamp

arrivals
processed

queue_start
queue_end

active_servers
capacity

average_wait
peak_wait

utilization

risk
confidence
tutmak çok faydalı.

Ama production DB'ye her 5 dakikalık ara sonucu kalıcı yazmak zorunda değiliz; storage/retention tasarımına göre transient hesap + aggregate persistence yapılabilir.

59. API
    API'nin görevi hesap yapmak olmamalı.

Sadece sonucu sunmalı:

GET /airport/IST/queues
mantıksal response:

airport = IST
date = ...

domestic_security[]
departure_passport[]
international_security[]
arrival_passport[]
Her biri 24 saatlik bucket.

60. Frontend
    Frontend:

queue formülü çalıştırmaz.

ICAO çözmez.

show-up hesaplamaz.

Erlang-C çalıştırmaz.

Sadece API sonucunu gösterir.

Bu separation ileride çok işimize yarar.

61. Baştan sona TEK uçuş örneği
    Şimdi bütün sistemi bağlayalım.

Flight:

Airport: IST

Flight: TKxxx
Direction: Departure
Type: International

Departure: 12:40

Aircraft ICAO: ABCD
Kapasite servisi:

ABCD
↓
capacity = 300
Diyelim load factor:

0.84
Passenger demand:

300
×
0.84
=
252
300×0.84=252
Show-up:

08:40–09:40
252 × .10
= 25.2

09:40–10:40
252 × .35
= 88.2

10:40–11:40
252 × .45
= 113.4

11:40–12:40
252 × .10
= 25.2
5-minute events:

İlk bölüm:

25.2
/
12
=
2.1
25.2/12=2.1
08:40 → 2.1
08:45 → 2.1
...
09:35 → 2.1
Diğer profiller de aynı şekilde.

Routing:

International Departure olduğu için:

events
↓
IST Departure Passport
09:00 örneği:

old queue = 20
all flights new arrivals = 35
a
v
a
i
l
a
b
l
e
=
55
available=55
IST Departure Passport config:

c
μ
efficiency
üzerinden 5dk capacity diyelim:

40
p
r
o
c
e
s
s
e
d
=
40
processed=40
q
u
e
u
e
=
15
queue=15
Passport'tan çıkan:

40
kişi transfer event oluşturur.

09:05/09:10
→ International Security
Security:

old security queue = 10
passport arrivals = 40
a
v
a
i
l
a
b
l
e
=
50
available=50
Security 5dk capacity:

45
p
r
o
c
e
s
s
e
d
=
45
processed=45
q
u
e
u
e
=
5
queue=5
Sonraki 5 dakika her şey tekrar edilir.

62. Ve aynı anda başka uçuşlar da var
    Motor sadece TKxxx'i çalıştırmıyor.

Aynı anda:

TKxxx
LHxxx
PCxxx
BAxxx
EKxxx
...
her uçuş önce kendi passenger events'ini oluşturuyor.

Sonra:

IST
09:00
DEPARTURE_PASSPORT
için hepsi toplanıyor.

Bu yüzden sistem flight-aware ama queue process aggregate çalışıyor.

Bu istediğimiz şey.

63. Bütün havalimanları için
    Aynı kod:

IST
↓
IST flights
↓
IST config
↓
IST simulation

SAW
↓
SAW flights
↓
SAW config
↓
SAW simulation

AMS
↓
AMS flights
↓
AMS config
↓
AMS simulation
şeklinde çalışır.

Yeni havalimanı eklemek için yeni queue algoritması yazmayız.

Config + flight data yeterli olmalı.

64. En önemli veri akışı
    Projeyi geliştirecek kişinin kafasında şu zincir kesinlikle oturmalı:

                      RAW FLIGHT
                           │
                           ▼
                   Normalize Flight
                           │
                           ▼
                 Determine Airport
                           │
                           ▼
                  ARRIVAL / DEPARTURE
                           │
                           ▼
                DOMESTIC / INTERNATIONAL
                           │
                           ▼
                     aircraft_icao
                           │
                           ▼
                 AIRCRAFT CAPACITY
                           │
                           ▼
                      LOAD FACTOR
                           │
                           ▼
                   PASSENGER DEMAND
                           │
              ┌────────────┴────────────┐
              │                         │
          DEPARTURE                  ARRIVAL
              │                         │
              ▼                         ▼

    SHOW-UP PROFILE ARRIVAL BUFFER
    │ │
    ▼ ▼
    5-MIN EVENTS 5-MIN EVENTS
    │ │
    └────────────┬────────────┘
    ▼
    ROUTING
    │
    ┌───────────────┼─────────────────┐
    ▼ ▼ ▼
    DOMESTIC SECURITY DEP PASSPORT ARR PASSPORT
    │
    ▼
    PROCESS
    │
    ▼
    TRANSFER EVENT
    │
    ▼
    INTERNATIONAL SECURITY
    │
    ▼
    QUEUE PROCESS
    │
    ▼
    5-MIN SNAPSHOTS
    │
    ▼
    HOURLY AGGREGATION
    │
    ▼
    API
    │
    ▼
    24-HOUR GRAPHS

65. Uygulama sırası
    Bunu repository'de tek seferde devasa bir değişiklik olarak yapmazdım. Sıra şöyle olmalı:

Mevcut ICAO → capacity resolver'ı doğrula.
Flight classification'ı doğrula: domestic/international + arrival/departure.
Passenger demand'i doğrula.
Configurable departure show-up profile ekle.
Flight demand'i 5 dakikalık event'lere çevir.
Airport/process/timestamp isolation testlerini yaz.
Generic stateful QueueProcess oluştur.
Domestic Security'yi buna geçir.
Departure Passport'u geçir.
Passport → Security transfer event'ini ekle.
International Security'yi geçir.
Arrival Passport'u geçir.
FIFO cohort wait hesabını ekle.
Erlang-C'yi auxiliary metric olarak bağla.
Wait-based risk'i bağla.
5 dakika → saatlik aggregator yaz.
API'yi yeni aggregate modeline bağla.
Mevcut dört grafiği API'ye bağla.
Multi-airport replay çalıştır.
Full test + conservation test + stress test.
Bu sırayla gidersek bir hata olduğunda hangi katmanda olduğunu bulabiliriz.

66. Mutlaka yazılması gereken matematiksel invariant testleri
    Bu sistemde normal unit testlerden bile önemli birkaç kontrol var.

Passenger conservation
Bir departure flight için:

∑
S
h
o
w
U
p
E
v
e
n
t
s
=
P
a
s
s
e
n
g
e
r
D
e
m
a
n
d
∑ShowUpEvents=PassengerDemand
Örneğin 252:

∑
e
v
e
n
t
s
=
252
∑events=252
olmalı.

Queue conservation
Her interval:

Q
s
t
a
r
t

- A
  r
  r
  i
  v
  a
  l
  s
  =
  P
  r
  o
  c
  e
  s
  s
  e
  d
- Q
  e
  n
  d
  Q
  start
  ​
  +Arrivals=Processed+Q
  end
  ​

mutlaka doğru olmalı.

Passport → Security conservation
International Departure için:

P
a
s
s
p
o
r
t
P
r
o
c
e
s
s
e
d
=
S
e
c
u
r
i
t
y
T
r
a
n
s
f
e
r
S
c
h
e
d
u
l
e
d
PassportProcessed=SecurityTransferScheduled
transfer delay tamamlandığında:

T
r
a
n
s
f
e
r
D
u
e
=
S
e
c
u
r
i
t
y
A
r
r
i
v
a
l
s
F
r
o
m
P
a
s
s
p
o
r
t
TransferDue=SecurityArrivalsFromPassport
olmalı.

Airport isolation
IST passenger:

→ SAW queue
asla girememeli.

Process isolation
Domestic passenger:

→ Departure Passport
girememeli.

No negative queue
Q
t
≥
0
Q
t
​
≥0
her zaman.

Capacity limit
P
r
o
c
e
s
s
e
d
t
≤
C
a
p
a
c
i
t
y
t
Processed
t
​
≤Capacity
t
​

her zaman.

Bunlar sistemi gerçekten güvenilir yapar.

67. Bu modelin özeti
    Biz artık:

“Bu saatte 500 yolcu var, Erlang-C'ye 500 ver.”

demiyoruz.

Biz:

“Hangi uçuşlar var? Her uçuşun ICAO'su ne? Uçağın kapasitesi ne? Beklenen doluluk ne? Kaç yolcu bekliyoruz? Uçuş domestic mi international mı? Arrival mı departure mı? Departure ise bu yolcular uçuş öncesinde hangi dakikalarda geliyor? Hangi havalimanında? Hangi queue'ya girmeleri gerekiyor? O havalimanında o anda kaç gişe/lane açık? Önceki 5 dakikadan kaç kişi kaldı? Bu 5 dakikada kaç kişi geldi? Kaçı işlendi? Passport'tan çıkanların kaçı ve ne zaman Security'ye ulaştı? Security'nin önceki kuyruğu neydi? Yolcular gerçekte ne kadar bekledi? Sonra bu 5 dakikalık sonuçların 12 tanesini birleştirince kullanıcının 12–13 grafiğinde ne göstermeliyiz?”

sorularını sırayla çözüyoruz.

Bu, konuştuğumuz bütün parçaları tek bir tutarlı modele bağlayan mimari.

Erlang-C tarafındaki
ρ
ρ, waiting probability ve
W
q
W
q
​
formülleri standart M/M/c formülleriyle uyumludur; fakat gerçek havaalanı akışındaki zamana bağlı talep ve Passport→Security bağlantısı nedeniyle bizim stateful simulation katmanımız ayrıca gereklidir.

Ve bence implementasyonda en kritik nokta şu olacak: önce bu mimarinin üzerine testleri yazıp, sonra mevcut aircraft-capacity-py dosyalarını hangi sırayla dönüştüreceğimizi çıkarmak. Böylece mevcut çalışan ICAO capacity, multi-airport, API ve graph tarafını gereksiz yere kırmadan yeni motoru yerleştirebiliriz.

Evet bu bütün bunları yapmak istiyorum güncrl ptojeme ama bütün bu kullanılacaklar benim json dosyalarımdaki verilere göre gelecek yani bu yapıyı mevcut projeme entegre edeceğiz uygun şekilfe
Evet, tam olarak öyle. Yeni, projeden kopuk bir sistem kurmayacağız. Bu mimariyi mevcut aircraft-capacity-py projenin içine, mevcut JSON/API → DB → pipeline → engine → API → frontend akışını koruyarak entegre edeceğiz.

Özellikle kaynak tarafında senin mevcut verilerin source of truth olacak: uçuş saatleri/yönleri/status bilgileri uçuş JSON/API verisinden; aircraft_icao uçuş/enrichment verisinden; uçak kapasitesi yolcu_ucaklari.json içindeki ICAO eşleşmesinden; havalimanı IATA/ICAO/country/timezone bilgileri mevcut airport verinden gelecek. Bunların üzerine bizim hesapladığımız load_factor → passenger_demand → show-up → queue katmanı binecek.

En önemli nokta da şu: hemen kod değiştirmeye başlamamak. Önce mevcut repository'de hangi verinin şu anda hangi dosyada üretildiğini ve hangi fonksiyonların zaten bu işi yaptığını çıkarmalıyız. Çünkü örneğin ICAO→capacity resolver, domestic/international routing, multi-airport config, pipeline ve queue altyapısının önemli bir kısmı sende zaten var. Bunları ikinci kez yazmak yerine yeni show-up/stateful-flow mimarisine bağlamak gerekiyor.

Bence entegrasyon sıramız şu olmalı:

Mevcut JSON'ların gerçek field'larını ve mevcut kodun bunları nerede okuduğunu çıkaracağız. aircraft_icao → capacity, airport classification, arrival/departure, domestic/international, load factor ve mevcut effective_time akışını tek tek doğrulayacağız.
Sonra departure show-up katmanını ekleyeceğiz. Örneğin 12:40 international departure'ın hesaplanan 252 yolcusunu %10/%35/%45/%10 ile gerçek timestamp aralıklarına, ardından 5 dakikalık event'lere dağıtacağız. Saatlik bucket'a burada zorla sokmayacağız.
Ardından mevcut queue engine'i stateful hale getireceğiz: Q_end = max(0, Q_start + arrivals - processed). Her airport ve process'in state'i ayrı olacak.
Routing'i mevcut verilerden kuracağız: Domestic Departure → Domestic Security, International Departure → Departure Passport → International Security, International Arrival → Arrival Passport, Domestic Arrival → modeled queue yok.
International Departure'ta Passport'tan gerçekten işlenen yolcular için transfer event üretip Security'ye aktaracağız. Böylece Passport yeni yolcu almaya devam ederken Security de eski kuyruğunu işlemeye devam edecek.
Her airport'un mevcut/oluşturulacak AirportOperationalConfig değerlerinden kendi counter/lane/service kapasitesini kullanacağız. IST'nin kuyruğu SAW'ın değerleriyle hesaplanmayacak.
En son 5 dakikalık simulation sonuçlarını 08–09, 09–10 gibi saatlere aggregate edip mevcut 24 saatlik grafik API'sine vereceğiz. Frontend hesap yapmayacak; sadece backend sonucunu gösterecek.
Son aşamada conservation, ICAO-capacity, routing, airport isolation, Passport→Security, queue carry-over, missing JSON field, 24-hour aggregation ve binlerce uçuş stress testlerini çalıştıracağız.
Yani hedef architecture gerçekten şu olacak:

MEVCUT JSON / API DATA
↓
Flight ingestion / normalization
↓
airport + flight + aircraft_icao
↓
ICAO → yolcu_ucaklari.json → aircraft capacity
↓
capacity × route-based load factor
↓
PASSENGER DEMAND (flight-level)
↓
Domestic / International
Arrival / Departure
↓
┌──────────────────────────────────────┐
│ DEPARTURE │
│ passenger show-up profile │
│ %10 / %35 / %45 / %10 │
│ ↓ │
│ 5-minute passenger events │
└──────────────────────────────────────┘

┌──────────────────────────────────────┐
│ ARRIVAL │
│ mevcut arrival/release mantığı │
│ ↓ │
│ 5-minute passenger events │
└──────────────────────────────────────┘
↓
AIRPORT + PROCESS + TIMESTAMP
↓
┌──────────────────────────────────────┐
│ Airport Simulation State │
│ │
│ Domestic Departure │
│ → Domestic Security │
│ │
│ International Departure │
│ → Departure Passport │
│ → transfer │
│ → International Security │
│ │
│ International Arrival │
│ → Arrival Passport │
└──────────────────────────────────────┘
↓
Q_start + arrivals
↓
process according to that airport's
counter/lane/service capacity
↓
processed + Q_end
↓
wait / utilization / risk
↓
5-MINUTE SNAPSHOTS
↓
HOURLY AGGREGATION
↓
08–09 / 09–10 / 10–11 ...
↓
MEVCUT API
↓
MEVCUT 24-HOUR GRAPHS
Ve burada JSON'lara hesap sonucu uydurmayacağız. Tam tersine JSON'daki gerçek flight/aircraft/airport bilgisi giriş olacak; hesaplanan şeyler passenger_demand, show_up_events, queue, processed, wait, risk gibi türetilmiş değerler olacak.

Şimdi bunu mevcut projene gerçekten entegre etmeye geçerken benim ilk yapmak isteyeceğim şey repo üzerinde kod değiştirmeden bir entegrasyon audit'i: mevcut app/queue/pipeline.py, engine.py, event_queue.py, models.py, config.py, api.py, db.py, ingestion kodları ve JSON yapılarının gerçek halini okuyup yukarıdaki her kutunun şu anda projede tam olarak hangi fonksiyona karşılık geldiğini çıkarmak. Ondan sonra “bu dosya kalıyor / bu fonksiyon değişiyor / buraya yeni katman geliyor” şeklinde dosya ve fonksiyon seviyesinde implementasyon planı çıkarabiliriz. Bu, mevcut çalışan kısımları kırmadan ilerlemenin en güvenli yolu.
