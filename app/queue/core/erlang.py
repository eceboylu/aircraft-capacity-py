
import math


def erlang_c_probability(c: int, traffic: float) -> float:
    if c <= 0:
        return 1.0
    if traffic <= 0:
        return 0.0
    if traffic >= c:
        return 1.0

    numerator = (traffic ** c) / (math.factorial(c) * (1 - traffic / c))
    sum_terms = sum((traffic ** k) / math.factorial(k) for k in range(c))

    return numerator / (sum_terms + numerator)


def erlang_c_wait_time(c: int, lam: float, mu: float) -> float:
    capacity_rate = c * mu
    if capacity_rate <= lam:
        raise ValueError(
            "Kapasite talebi karşılamıyor; Wq tanımsız. "
            "Çağıran taraf rho >= 1 durumunu önceden ele almalı."
        )

    probability = erlang_c_probability(c, lam / mu)
    return probability / (capacity_rate - lam)
