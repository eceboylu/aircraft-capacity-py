"""
Standart Erlang-C formülü. SAF FONKSİYON - veritabanı, dosya veya
ağ bağımlılığı yoktur, sadece sayı alır sayı döndürür.

Passport ve security aynı matematik çekirdeğinden bu fonksiyonları
kullanır; server sayısı ve servis hızı süreç config'inden gelir.
"""

import math


def erlang_c_probability(c: int, traffic: float) -> float:
    """
    Bir yolcunun beklemek zorunda kalma olasılığı (C formülü).

    c       : kanal (gişe) sayısı
    traffic : trafik yoğunluğu, a = lambda / mu (Erlang cinsinden)

    traffic >= c ise sistem kararsızdır; bu durumda çağıran taraf
    zaten rho >= 1 kontrolüyle erken döner, yine de burada 1.0
    (kesin bekleme) döndürülür - negatif/sonsuz değer sızdırılmaz.
    """
    if c <= 0:
        return 1.0
    if traffic <= 0:
        return 0.0
    if traffic >= c:
        return 1.0

    # a^c / (c! * (1 - a/c))
    numerator = (traffic ** c) / (math.factorial(c) * (1 - traffic / c))
    # Toplam: k=0..c-1 için a^k / k!
    sum_terms = sum((traffic ** k) / math.factorial(k) for k in range(c))

    return numerator / (sum_terms + numerator)


def erlang_c_wait_time(c: int, lam: float, mu: float) -> float:
    """
    Ortalama kuyruk bekleme süresi Wq (dakika).

    lam : dakikada gelen yolcu (lambda)
    mu  : gişe başına dakikada işlenen yolcu (mu)

    Wq = C(c, lambda/mu) / (c*mu - lambda)
    """
    capacity_rate = c * mu
    if capacity_rate <= lam:
        raise ValueError(
            "Kapasite talebi karşılamıyor; Wq tanımsız. "
            "Çağıran taraf rho >= 1 durumunu önceden ele almalı."
        )

    probability = erlang_c_probability(c, lam / mu)
    return probability / (capacity_rate - lam)
