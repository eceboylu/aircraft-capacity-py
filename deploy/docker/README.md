# Local MySQL/phpMyAdmin stack (Docker, HTTPS)

Bu, projenin geliştirme sırasında kullandığı MySQL + phpMyAdmin
container'larını (`docker-compose.yml`, repo kökünde) ve önlerindeki
HTTPS reverse-proxy'yi (`nginx/nginx.conf`) belgeler.

## Neden bu compose dosyası eklendi

Daha önce bu 4 container ad-hoc `docker run` komutlarıyla, hiçbir
compose/restart policy olmadan çalıştırılıyordu - bu yüzden Docker
Desktop/host her yeniden başladığında hepsi `Exited (255)` durumunda
kalıp KENDİLİĞİNDEN geri gelmiyordu. `docker-compose.yml` artık
`restart: unless-stopped` ile bunu otomatik hale getiriyor.

Eski container'ların KULLANDIĞI GERÇEK MySQL verisi (Docker volume'ları)
`external: true` ile bu compose dosyasına AYNEN bağlandı - hiçbir veri
kaybı olmadı (aynı `flights`/`queue_predictions`/
`airport_operational_configs`/vb. tabloları).

## Servisler

| Servis                | Amaç                                    | Port (host)      |
|------------------------|------------------------------------------|-------------------|
| `local_mysql`          | Ana geliştirme DB'si (`aircraft_capacity`) | `3307` (MySQL, TLS destekli - MySQL kendi native TLS'ini de sunar) |
| `local_phpmyadmin`     | Ana DB'nin phpMyAdmin'i (düz HTTP)        | `8082`            |
| `benchmark_mysql`      | Ayrı benchmark DB'si (`aircraft_capacity_bench`) | `3309`      |
| `benchmark_phpmyadmin` | Benchmark DB'nin phpMyAdmin'i (düz HTTP)  | `8081`            |
| `https_proxy`          | nginx - yukarıdaki iki phpMyAdmin'i HTTPS üzerinden sunar | `8443` (local), `8444` (benchmark) |

Düz HTTP portlar (`8081`/`8082`) BİLİNÇLİ OLARAK kaldırılmadı - mevcut
bookmark/scriptler kırılmasın diye. HTTPS bunlara EK olarak geldi.

## HTTPS neden "sertifikalı" ve tarayıcı neden "Bağlantınız güvenli" diyor

Bu makine (localhost) internetten erişilebilir bir public domain'e sahip
DEĞİL - bu yüzden Let's Encrypt gibi genel-geçer bir CA'dan gerçek/
herkesin güvendiği bir sertifika ALINAMAZ (ACME doğrulaması internetten
80/443'e erişim ister). Bunun yerine [mkcert](https://github.com/FiloSottile/mkcert)
kullanıldı:

1. `mkcert -install` bu Windows makinesinin sistem sertifika deposuna
   (ve Firefox kullanıyorsan onun kendi NSS deposuna) mkcert'in ürettiği
   **yerel bir kök CA'yı** ekler - bu ADIM zaten bu makinede
   YAPILMIŞTI (`mkcert -install` "already installed" dedi).
2. `mkcert -cert-file localhost.pem -key-file localhost-key.pem
   localhost 127.0.0.1 ::1` bu yerel CA ile imzalı, `localhost` için
   geçerli bir sertifika üretir - `deploy/docker/certs/` altında (bu
   dosyalar **gitignore'lu** - private key asla commit edilmez).
3. nginx bu sertifikayla 8443/8444'te TLS sonlandırır.

SONUÇ: bu makinedeki bir tarayıcı `https://localhost:8443` açtığında
sertifika zincirini KENDİ güvendiği bir CA'ya kadar doğrulayabildiği
için gerçekten "Bağlantınız güvenli" gösterir, HİÇBİR uyarı ÇIKMAZ -
ama bu SADECE bu makine için geçerlidir (mkcert kök CA'sı SADECE bu
makinenin sertifika deposuna eklendi). Başka bir bilgisayardan
`https://<bu-makinenin-ip'si>:8443` açılırsa güvenli GÖRÜNMEZ (o
makine mkcert'in kök CA'sını tanımıyor).

Eğer ileride bu servisleri gerçek bir public domain + internete açık
bir sunucudan yayınlamak istersen (ör. `db.seninalanadin.com`), o zaman
certbot/Let's Encrypt ile HERKESİN tarayıcısında geçerli, ücretsiz bir
sertifika alınabilir - o farklı bir kurulum gerektirir (DNS + 80/443
portlarının internetten erişilebilir olması).

## Sertifika yenileme / başka bir makineye taşıma

mkcert sertifikaları uzun ömürlüdür (bu üretilen sertifika ~2028'e
kadar geçerli). Başka bir geliştirici makinesinde bu stack'i çalıştırmak
için:

```bash
mkcert -install   # o makinede mkcert kurulu olmalı
cd deploy/docker/certs
mkcert -cert-file localhost.pem -key-file localhost-key.pem localhost 127.0.0.1 ::1
cd ../../..
docker compose up -d
```

## Komutlar

```bash
docker compose up -d        # stack'i başlat (idempotent)
docker compose ps           # durum
docker compose logs -f local_mysql
docker compose down         # container'ları durdur/kaldır (volume'lar KORUNUR - `external: true`)
```

`docker compose down -v` KULLANMAYIN - `-v` compose'un YÖNETTİĞİ
volume'ları siler; bizimkiler `external: true` olduğu için normalde
silinmez, ama garanti altına almak için bu bayrağı hiç kullanmayın.
