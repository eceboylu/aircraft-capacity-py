"""
Merkezi logging kurulumu.

Bu modül SADECE root logger'ın seviyesini/formatını kurar. Uygulama
modülleri kendi logger'larını standart desenle alır:

    logger = logging.getLogger(__name__)

`configure_logging()` bir KÜTÜPHANE fonksiyonu değildir - sadece CLI
giriş noktalarında (app/seed.py, app/queue/pipeline.py __main__
blokları) bir kez çağrılır. Diğer modüller (refresh.py, engine.py vb.)
logging çıktısını KENDİLERİ yapılandırmaz; onlar sadece log kaydı
üretir, root konfigürasyonuna dokunmazlar - bu, kütüphane kodunun
çağıranın logging kurulumunu ezmemesi için standart bir pratiktir.

LOG_LEVEL ortam değişkeni desteklenir; verilmezse production-safe
varsayılan INFO kullanılır. Geçersiz bir değer verilirse (typo vb.)
sessizce INFO'ya düşülür - konfigürasyon hatası uygulamayı çökertmez.
"""

import logging
import os

DEFAULT_LOG_LEVEL = "INFO"


def configure_logging(default_level: str = DEFAULT_LOG_LEVEL) -> None:
    """
    Root logger'ı kurar. Birden fazla çağrılması güvenlidir -
    `logging.basicConfig()` zaten bir handler varsa no-op'tur.
    """
    level_name = os.environ.get("LOG_LEVEL", default_level).upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
