"""
ADIM (Passenger Demand Calibration) - resolved aircraft SEAT capacity'yi
(`AircraftCapacityService`'in DEĞİŞMEDEN döndürdüğü değer) GERÇEK
passengers-per-movement kanıtına göre kalibre eden, saf/deterministik
bir "occupancy factor" katmanı.

SAF KATMAN: veritabanına/ağa dokunmaz, sadece sabit bir lookup + saf
aritmetik. `AircraftCapacityService`/resolver zinciri (airline-specific
exact -> weighted fleet -> aircraft capacity -> family -> fallback)
HİÇ DEĞİŞTİRİLMEDİ - bu modül SADECE resolver'ın ÇÖZDÜĞÜ koltuk
sayısını, resolve işleminden SONRA, bir çarpanla ayarlar.

KAPSAM (kasıtlı, dar): factor tablosu SADECE bu turda GERÇEK kaynaktan
araştırılmış 9 havalimanı içindir (IST/SAW/AMS/JMK/TZX/KOI/ISC/SOG/JTY).
`occupancy_factor_for()` tablomda OLMAYAN HERHANGİ bir havalimanı için
1.0 (mevcut/eski %100-koltuk davranışı, DEĞİŞMEDEN) döner - production'daki
binlerce diğer havalimanı (ve onları kullanan mevcut testler) bu ADIM'dan
HİÇ ETKİLENMEZ. Bu "global fallback" seviyesi (Bölüm 8/seviye 3) BİLİNÇLİ
olarak 1.0 seçildi - araştırılmamış bir havalimanı için rastgele/varsayımsal
bir doluluk oranı İCAT ETMEMEK için (Bölüm 4/"Do not invent missing values").

METODOLOJİ (her havalimanı için, SOURCE/DATE/VALUE):

Faktör = (gerçek yıllık passengers) / (gerçek yıllık air-transport-
movements) / (bu fixture'ın o ölçek-tier'i için sentetik ağırlıklı
ortalama koltuk kapasitesi - `generate_fixture.py:MIX`).

  LARGE ortalama koltuk (MIX["large"], ağırlıklı): 220.32
  MEDIUM ortalama koltuk (MIX["medium"], ağırlıklı): 136.6
  SMALL ortalama koltuk (MIX["small"], ağırlıklı): 44.45

IST  - SOURCE: iGA Istanbul Airport 2024 tam-yıl figürleri (Aviation Week
       Network, "iGA Istanbul Airport: 2024 is the Year of Investments" -
       iGA'nın kendi açıkladığı rakamları aktaran havacılık ticaret basını).
       DATE: 2024 tam yıl. VALUE: passengers=80,800,000 (17.9M domestic +
       62.9M international), movements=517,285 (117,764 domestic +
       399,521 international).
       real pax/movement = 80,800,000 / 517,285 = 156.19
       factor = 156.19 / 220.32 = 0.7089

SAW  - SOURCE: DHMİ (Devlet Hava Meydanları İşletmesi - Türkiye resmi
       havalimanı otoritesi), Hürriyet Daily News/Daily Sabah üzerinden
       aktarılan 2025 tam-yıl rakamları. DATE: 2025 tam yıl.
       VALUE: passengers=48,407,318, movements=275,537.
       real pax/movement = 48,407,318 / 275,537 = 175.66
       factor = 175.66 / 220.32 = 0.7973

AMS  - SOURCE: Royal Schiphol Group resmi basın açıklaması
       ("Schiphol in 2025: quieter aircraft, more satisfied travellers
       and solid financial results", news.schiphol.com). DATE: 2025 tam
       yıl. VALUE: passengers=68,800,000 (SADECE Schiphol - Schiphol
       Group'un TÜMÜ değil), ATM=477,552.
       real pax/movement = 68,800,000 / 477,552 = 144.05
       factor = 144.05 / 220.32 = 0.6540

JMK  - SOURCE: Fraport Greece (Mikonos Havalimanı'nın işletmecisi) aylık
       trafik açıklamaları, Wikipedia üzerinden aktarılan 2024 tam-yıl
       toplamı. DATE: 2024 tam yıl. VALUE: passengers=1,613,638,
       movements=17,286.
       real pax/movement = 1,613,638 / 17,286 = 93.36
       factor = 93.36 / 136.6 = 0.6835

TZX  - SOURCE: DHMİ, Wikipedia (Trabzon Havalimanı) üzerinden aktarılan
       2025 tam-yıl toplamı. DATE: 2025 tam yıl. VALUE: passengers=
       3,874,640, movements=26,507.
       real pax/movement = 3,874,640 / 26,507 = 146.17
       HAM faktör = 146.17 / 136.6 = 1.0700 - BU FİZİKSEL OLARAK
       GEÇERSİZ (tek bir uçuşun kalibre edilmiş talebi, o uçuşun KENDİ
       çözülmüş koltuk sayısını AŞAMAZ - >1.0 hiçbir gerçek doluluk
       oranı olamaz, MEDIUM MIX'in ortalama koltuk sayısının TZX'in
       gerçek filo karışımından - muhtemelen daha büyük narrowbody
       ağırlıklı - daha küçük olmasından kaynaklanan bir MIX/gerçek-filo
       UYUMSUZLUĞU'dur, gerçek doluluk oranı DEĞİL). Bu yüzden ham kanıt
       AÇIKÇA raporlanır AMA fiziksel tavan olan 0.95'e (endüstri
       standardı gerçekçi maksimum load factor) SINIRLANDI - Bölüm 8'in
       scale-level fallback'ine düşülmedi, çünkü mevcut TEK diğer MEDIUM
       kanıtı (KOI) çok farklı bir operasyonel karaktere (ada-içi küçük
       uçak) ait ve TZX'e ONDAN daha uygunsuz bir vekil olurdu - bu yüzden
       KENDİ kanıtını (yönü/büyüklüğü) 0.95 tavanıyla SINIRLAYIP kullanmak,
       tamamen ilgisiz bir havalimanına düşmekten daha sadıktır (AÇIKÇA
       raporlanan bir mühendislik kararı, GİZLİ bir varsayım DEĞİL).
       factor = 0.95 (capped; raw=1.0700)

KOI  - SOURCE: HIAL (Highlands and Islands Airports Limited) resmi basın
       açıklaması. DATE: passengers 2024/25 mali yılı, movements 2025
       takvim yılı (dönemler tam örtüşmüyor - en iyi mevcut eşleşme,
       AÇIKÇA işaretli). VALUE: passengers=137,744, movements=10,274 (ATM).
       real pax/movement = 137,744 / 10,274 = 13.40
       factor = 13.40 / 136.6 = 0.0981

ISC  - SOURCE: UK Civil Aviation Authority (CAA) resmi havalimanı
       istatistikleri. DATE: 2024 tam yıl (passengers/movements AYNI yıl,
       en iyi eşleşen çift). VALUE: passengers=68,086, movements=9,082.
       real pax/movement = 68,086 / 9,082 = 7.497
       factor = 7.497 / 44.45 = 0.1687

SOG  - SOURCE: Avinor (Norveç devlet havalimanı işletmecisi) 2015 aylık
       trafik raporu, Wikipedia üzerinden aktarılan. DATE: 2014 tam yıl -
       ESKİ veri, daha yeni movements verisi bulunamadı (2024/2025 için
       SADECE passengers bulundu, movements YOK) - "Do not invent missing
       values" ilkesiyle en iyi GERÇEK eşleşen çift kullanıldı, AÇIKÇA
       eski olarak işaretli. VALUE: passengers=70,244, movements=5,735.
       real pax/movement = 70,244 / 5,735 = 12.25
       factor = 12.25 / 44.45 = 0.2757

JTY  - SOURCE: Wikipedia (Astypalaia Island National Airport, HCAA
       kaynaklı). DATE: 2018 tam yıl - ESKİ veri, daha güncel eşleşen
       (passengers+movements AYNI yıl) çift bulunamadı (2025 için sadece
       kısmi aylık veri vardı). VALUE: passengers=14,029, movements=617.
       real pax/movement = 14,029 / 617 = 22.74
       factor = 22.74 / 44.45 = 0.5116

Hiçbiri "queue wait'i iyi göstermek için seçilmedi" (Bölüm 27) - her biri
YUKARIDAKİ yayınlanmış kaynak çiftinden DOĞRUDAN türetildi; TEK istisnai
mühendislik kararı TZX'in 0.95 fiziksel tavanı (gerekçesi yukarıda, "no
cheating" ilkesiyle TUTARLI - üst sınır SABİT/evrensel bir endüstri
tavanı, bu havalimanına özel keyfi bir seçim değil).
"""
from __future__ import annotations

#: Seviye 1 (airport-specific) - araştırılmış 9 havalimanı, YUKARIDAKİ
#: SOURCE/DATE/VALUE metodolojisinden türetilmiş.
AIRPORT_OCCUPANCY_FACTORS: dict[str, float] = {
    "IST": 0.7089,
    "SAW": 0.7973,
    "AMS": 0.6540,
    "JMK": 0.6835,
    "TZX": 0.95,   # capped (raw evidence-derived ratio = 1.0700)
    "KOI": 0.0981,
    "ISC": 0.1687,
    "SOG": 0.2757,
    "JTY": 0.5116,
}

#: Seviye 2 (scale-level) - bu turda hiçbir havalimanı buraya düşmedi
#: (9'unun da Seviye-1 airport-specific kanıtı vardı, TZX capped olarak
#: Seviye-1'de kaldı) - yine de Bölüm 8'in istediği hiyerarşiyi kodda
#: SOMUT tutmak için tanımlı, gelecekte airport-specific kanıt
#: bulunamayan bir LARGE/MEDIUM/SMALL havalimanı için kullanılabilir.
SCALE_OCCUPANCY_FACTORS: dict[str, float] = {
    "large": 0.7201,   # mean(IST, SAW, AMS)
    "medium": 0.5772,  # mean(JMK, TZX-capped, KOI)
    "small": 0.3187,   # mean(ISC, SOG, JTY)
}

#: Seviye 3 (global fallback) - araştırılmamış HERHANGİ bir havalimanı
#: için 1.0 (mevcut/eski %100-koltuk davranışı, DEĞİŞMEDEN) - production'daki
#: diğer tüm havalimanları (ve onları kullanan mevcut testler) bu ADIM'dan
#: ETKİLENMEZ. Rastgele/varsayımsal bir sayı İCAT EDİLMEDİ.
GLOBAL_OCCUPANCY_FALLBACK = 1.0


def occupancy_factor_for(airport_iata: str | None, scale: str | None = None) -> tuple[float, str]:
    """
    Bir havalimanı için kullanılacak occupancy factor'ü VE hangi
    hiyerarşi seviyesinden geldiğini döner - `(factor, level)`.

    `level` in {"airport_specific", "scale_level", "global_fallback"} -
    Bölüm 8: "Clearly report which airport used which level. Do NOT
    silently default." çağıran taraf bu ikinci değeri raporlayabilir.
    """
    code = (airport_iata or "").upper()
    if code in AIRPORT_OCCUPANCY_FACTORS:
        return AIRPORT_OCCUPANCY_FACTORS[code], "airport_specific"
    if scale in SCALE_OCCUPANCY_FACTORS:
        return SCALE_OCCUPANCY_FACTORS[scale], "scale_level"
    return GLOBAL_OCCUPANCY_FALLBACK, "global_fallback"
