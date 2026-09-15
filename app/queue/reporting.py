"""
AŞAMA 9 - Görselleştirme veri katmanı.

Bu modül grafik çizmez; grafiğin ihtiyaç duyduğu agregatif yapıyı
hazırlar. YENİ HESAP TETİKLEMEZ - sadece `queue_predictions` ve
`flights` tablolarından okur.

İki çıktı:
  hourly_report(session, iata, date)  -> /airports/{iata}/hourly
  summary_report(session, date)       -> /airports/summary

Saatlik satır, o saatin 15 dakikalık pencerelerinin özetidir:
  risk        -> saatin EN KÖTÜ penceresi (RISK_ORDER'a göre)
  ratio/rho   -> saatin en yüksek değeri
  wait        -> saatin en uzun beklemesi
  confidence  -> saatin EN DÜŞÜK güveni (en kötümser olan)
  reasons     -> pencerelerin nedenleri, koda göre tekilleştirilmiş

domestic_arrival hiçbir yerde görünmez: kuyruğu beslemiyor (AŞAMA 2).
"""

import json
from datetime import date as date_type, datetime, time, timedelta

from sqlalchemy import select

from .constants import (
    PROCESS_PASSPORT,
    PROCESS_SECURITY,
    RISK_ORDER,
    RISK_UNKNOWN,
)
from .domain.demand import DemandCalculator, effective_time
from .domain.flows import REPORTED_FLOWS, flow_of
from .models import Flight, QueuePrediction


def _hour_key(moment: datetime) -> str:
    return f"{moment.hour:02d}:00"


def _risk_rank(risk: str) -> int:
    return RISK_ORDER.get(risk, -1)


def _worst_risk(risks) -> str:
    """Saatin riski = en kötü pencerenin riski."""
    ranked = sorted(risks, key=_risk_rank, reverse=True)
    return ranked[0] if ranked else RISK_UNKNOWN


def _max_or_none(values):
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _min_or_none(values):
    present = [v for v in values if v is not None]
    return min(present) if present else None


def _merge_reasons(rows) -> list[dict]:
    """
    Saat içindeki pencerelerin nedenlerini birleştirir.

    Aynı kod birden çok pencerede tetiklendiyse en yüksek
    metric_value'lu olan tutulur - saatin en belirgin örneği.
    """
    best: dict[str, dict] = {}
    for row in rows:
        for reason in json.loads(row.reasons or "[]"):
            code = reason.get("code")
            current = best.get(code)
            if current is None or reason.get("metric_value", 0) > current.get(
                "metric_value", 0
            ):
                best[code] = reason
    return [
        {
            "code": r["code"],
            "severity": r["severity"],
            "message": r["message"],
        }
        for r in best.values()
    ]


def _day_bounds(day: date_type) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def _predictions_for_day(session, airport_iata: str, day: date_type):
    start, end = _day_bounds(day)
    return list(session.execute(
        select(QueuePrediction).where(
            QueuePrediction.airport_iata == airport_iata,
            QueuePrediction.window_start >= start,
            QueuePrediction.window_start < end,
        ).order_by(QueuePrediction.window_start)
    ).scalars().all())


def _process_block(rows) -> dict:
    """Bir sürecin saatlik özeti."""
    return {
        "risk": _worst_risk([r.risk for r in rows]),
        "baseline_ratio": _max_or_none([r.baseline_ratio for r in rows]),
        "utilization": _max_or_none([r.utilization for r in rows]),
        # Security'de bu değer her zaman None kalır (YASAK 1).
        "estimated_wait_minutes": _max_or_none(
            [r.estimated_wait_minutes for r in rows]
        ),
        "expected_passengers": sum(r.expected_passengers for r in rows),
        "flight_count": sum(r.flight_count for r in rows),
        "confidence": _min_or_none([r.confidence for r in rows]),
        "reasons": _merge_reasons(rows),
    }


def _empty_flows() -> dict:
    return {
        flow: {"flight_count": 0, "passengers": 0} for flow in REPORTED_FLOWS
    }


def hourly_flows(session, airport_iata: str, day: date_type, resolver) -> dict:
    """
    Saat başına akış kırılımı: uçuş sayısı + tahmini yolcu.

    Uçuş, kuyruğa yansıdığı ana (effective_time) göre saatlenir;
    böylece akış tablosu ile kuyruk pencereleri aynı zamanı gösterir.
    """
    start, end = _day_bounds(day)
    demand = DemandCalculator(resolver)

    flights = session.execute(
        select(Flight).where(Flight.airport_iata == airport_iata)
    ).scalars().all()

    hours: dict[str, dict] = {}
    for flight in flights:
        flow = flow_of(flight)
        if flow is None:          # domestic arrival - kuyruğu beslemiyor
            continue
        moment = effective_time(flight)
        if moment is None or not (start <= moment < end):
            continue

        bucket = hours.setdefault(_hour_key(moment), _empty_flows())
        bucket[flow]["flight_count"] += 1
        bucket[flow]["passengers"] += demand.passenger_demand(flight)

    return hours


def hourly_report(
    session, airport_iata: str, day: date_type, resolver
) -> dict:
    """
    /airports/{iata}/hourly karşılığı.

    Yeni hesap YAPMAZ; QueuePrediction satırlarını saatlere toplar.
    Akış kırılımı için uçuş tablosunu okur.
    """
    predictions = _predictions_for_day(session, airport_iata, day)
    flows = hourly_flows(session, airport_iata, day, resolver)

    by_hour: dict[str, dict[str, list]] = {}
    for row in predictions:
        hour = _hour_key(row.window_start)
        by_hour.setdefault(hour, {}).setdefault(row.process, []).append(row)

    hourly = []
    for hour in sorted(set(by_hour) | set(flows)):
        processes = by_hour.get(hour, {})
        entry = {
            "hour": hour,
            "flows": flows.get(hour, _empty_flows()),
        }
        for process in (PROCESS_SECURITY, PROCESS_PASSPORT):
            rows = processes.get(process)
            entry[process] = _process_block(rows) if rows else None
        hourly.append(entry)

    return {
        "airport": airport_iata,
        "date": day.isoformat(),
        "hourly": hourly,
    }


def _peak_of(rows, process: str) -> dict:
    """
    Sürecin en yoğun saati: önce en kötü risk, eşitlikte en çok yolcu.
    """
    by_hour: dict[str, list] = {}
    for row in rows:
        if row.process == process:
            by_hour.setdefault(_hour_key(row.window_start), []).append(row)

    if not by_hour:
        return {"hour": None, "risk": None, "wait_minutes": None}

    blocks = {hour: _process_block(items) for hour, items in by_hour.items()}
    peak_hour = max(
        blocks,
        key=lambda h: (
            _risk_rank(blocks[h]["risk"]),
            blocks[h]["expected_passengers"],
        ),
    )
    return {
        "hour": peak_hour,
        "risk": blocks[peak_hour]["risk"],
        "wait_minutes": blocks[peak_hour]["estimated_wait_minutes"],
    }


def summary_report(
    session, day: date_type, airports: list[str] | None = None
) -> dict:
    """
    /airports/summary karşılığı - her havalimanı için tepe saatler.

    Havalimanı listesi verilmezse o güne tahmini olan TÜM
    havalimanları raporlanır; her biri kendi satırında, birbirini
    etkilemeden.
    """
    start, end = _day_bounds(day)

    if airports is None:
        airports = list(session.execute(
            select(QueuePrediction.airport_iata)
            .where(
                QueuePrediction.window_start >= start,
                QueuePrediction.window_start < end,
            )
            .distinct()
            .order_by(QueuePrediction.airport_iata)
        ).scalars().all())

    rows_out = []
    for code in airports:
        rows = _predictions_for_day(session, code, day)
        security = _peak_of(rows, PROCESS_SECURITY)
        passport = _peak_of(rows, PROCESS_PASSPORT)
        rows_out.append({
            "airport": code,
            "peak_security_hour": security["hour"],
            "peak_security_risk": security["risk"],
            "peak_passport_hour": passport["hour"],
            # Security için dakika ASLA üretilmez; sadece passport'ta var.
            "peak_passport_wait_minutes": passport["wait_minutes"],
            "peak_passport_risk": passport["risk"],
        })

    return {"date": day.isoformat(), "airports": rows_out}
