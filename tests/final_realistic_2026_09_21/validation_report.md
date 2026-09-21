# Realistic 9-Airport Dataset — Validation Report (2026-09-21)

Throughput-calibrated **synthetic validation dataset**. Annual passenger
throughput figures are REAL/REFERENCE (official sources, see per-airport
tables below). Generated flight schedules, banks, routes and
estimated/actual timings are SYNTHETIC — not real published timetables.
Generated "production demand" comes from `AircraftCapacityService.resolve()`
seat capacity directly (no load factor) — it approximates real boarded
passengers but is not identical to them (see Model Realism Limitation).

## Airport Selection & Real Throughput

| Tier | Airport | ICAO | Country | Timezone | Annual Passengers | Source | Year |
|---|---|---|---|---|---|---|---|
| LARGE | JFK | KJFK | US | America/New_York | 63,300,000 | Port Authority of NY & NJ (official ATR) | 2024 |
| LARGE | MAD | LEMD | ES | Europe/Madrid | 66,196,984 | AENA (official) | 2024 |
| LARGE | SIN | WSSS | SG | Asia/Singapore | 67,700,000 | Changi Airport Group (official) | 2024 |
| MEDIUM | BOH | EGHH | GB | Europe/London | ~1,000,000+ | Bournemouth Airport (official press) | 2024 |
| MEDIUM | CID | KCID | US | America/Chicago | 1,500,000 (759,978 enplanements) | Eastern Iowa Airport / FAA | 2024 |
| MEDIUM | ASP | YBAS | AU | Australia/Darwin | 356,813 | BITRE (Australian gov.) | FY2024-25 |
| SMALL | MVY | KMVY | US | America/New_York | 161,000 (83,419 enplanements) | FAA | 2024 |
| SMALL | ISC | EGHE | GB | Europe/London | 68,086 | UK CAA | 2024 |
| SMALL | LEQ | EGHC | GB | Europe/London | 49,671 | UK CAA | 2024 |

Scale resolved via the real production scale resolver
(`import_airport_scales()` against `data/*_olcekli_havaalanlari.txt`) —
none UNKNOWN. Zero collision with existing 2026-09-21 shared-DB fixture
data (IST/CBR/MFG/OAG).

## Fixture Generation Result (target vs. generated — no load factor)

| Airport | Target/day (annual÷365) | Flight count | Generated demand | Difference % |
|---|---|---|---|---|
| JFK | 173,425 | 647 | 173,110 | -0.2% |
| MAD | 181,362 | 677 | 181,628 | +0.1% |
| SIN | 185,479 | 692 | 185,004 | -0.3% |
| BOH | 2,740 | 18 | 2,732 | -0.3% |
| CID | 4,110 | 26 | 4,116 | +0.1% |
| ASP | 978 | 6 | 972 | -0.6% |
| MVY | 441 | 6 | 452 | +2.5% |
| ISC | 187 | 4 | 194 | +3.7% |
| LEQ | 136 | 2 | 140 | +2.9% |

Total: 2,078 synthetic flights (1,038 departures + 1,040 arrivals). No
minimum flight-count floor was applied for fixture-size convenience —
LARGE airports genuinely required hundreds of flights each to stay
within a fraction of a percent of their real daily reference.

## Aircraft Mix (all resolved by real `AircraftCapacityService`, none UNKNOWN)

- **LARGE**: A320(150)/A20N(180)/A21N(220)/B738(162) narrowbody + B772(305)/B744(416)/A388(555) widebody
- **MEDIUM**: A320/B738 narrowbody + E170(70)/E190(114)/CRJ9(90) regional jet
- **SMALL**: DHC6(19)/SF34(34)/AT72(72)/DH8D(78) turboprop + occasional E170

## Model Realism Limitation

Production demand = resolved seat capacity, no load factor (0.78/0.82/
0.84/0.88 were NOT reintroduced). This dataset is a
**THROUGHPUT-CALIBRATED SYNTHETIC VALIDATION DATASET**: annual throughput
is REAL REFERENCE, generated production demand is SYNTHETIC MODEL DEMAND
— the two are reported separately above, never conflated.

## Ingestion (real production pipeline, `tests/incoming_2026_09_19/incoming_2026_09_19.sqlite`)

See `INGEST_RESULTS.md` (pre-ingest inventory, first/second ingest
summaries, idempotency verification, passenger-duplication audit).

## Update: Researched Per-Airport Hourly Distribution (supersedes generic template)

The original fixture used one generic 06-09/11-14/15-18/19-22 bank
template for all 9 airports. This was replaced with **per-airport,
web-researched** departure/arrival hour profiles — see
`_generate_fixture.py` (`DEP_HOUR_PROFILES`/`ARR_HOUR_PROFILES`) for the
exact weights and inline source citations.

| Airport | Hourly distribution basis | Key real-world sources |
|---|---|---|
| JFK | **REAL** | TripWaffle JFK busyness dataset — departure peak 19:00, arrival peak 12:00, quiet 04:00 |
| MAD | DERIVED | Parkos/FlightQueue secondary sources — dual peak 06-09 & 16-19, quiet 11-14 |
| SIN | **REAL** | TripWaffle SIN busyness dataset — departure peak 09:00, arrival peak 17:00 |
| BOH | DERIVED | Bournemouth Airport's own stated "morning departures peak, midday arrivals peak" pattern |
| CID | DERIVED | flightsfrom.com/kupi.com confirm 01:50-23:59 operating span; hub-feeder wave reasoning (AA/DL/UA/WN) |
| ASP | **REAL** | FlightsFrom.com ADL-ASP published schedule — activity window 07:45-14:35, no early/late flights |
| MVY | DERIVED | Cape Air high-frequency commuter operating model — continuous daytime, no dominant single peak |
| ISC | **REAL** | Skybus official timetable — 08:15-17:20 daytime-only window |
| LEQ | **REAL** | Skybus official timetable — 08:50-17:55 daytime-only window |

Aircraft mix for LARGE was also adjusted to be more narrowbody-weighted
(widebody share 40%→25%), matching real major-hub movement composition
(majority of movements are narrowbody even at international hubs) —
this required MORE, smaller flights to reach the same daily target,
which further reduces synthetic per-flight/per-hour footprint.

### Structural finding (NOT a fixture bug — reported per Section 9/10 instruction, production untouched)

Even with correctly-researched, non-uniform hourly shapes, JFK/MAD/SIN
still show very large event-driven passport waits. Root-caused and
confirmed **not** to be a concentration/distribution artifact:

```
JFK passport_dep: total daily demand 58,121 pax vs total daily capacity 19,200 (20 servers×40/hr×24h) = 3.03x over
JFK passport_arr: total daily demand 58,121 pax vs total daily capacity 28,800 (30 servers×40/hr×24h) = 2.02x over
MAD passport_dep: 68,721 vs 19,200 = 3.58x over
SIN passport_dep: 93,006 vs 19,200 = 4.84x over
```

This is a **daily-total-demand-vs-fixed-large-tier-capacity** mismatch:
the real annual-throughput-derived daily target for a mega-hub (JFK/MAD/SIN)
is several times larger than what the generic "large" scale tier's fixed
20/30-server passport pool can process across an entire 24h day, *no
matter how the same total is redistributed across hours*. Real JFK/MAD/SIN
run far more than 20-30 passport booths in practice; `SCALE_RESOURCES`
(`domain/airport_scale.py`) is a generic reference constant, not airport-
specific. Fixing this would require changing production capacity
constants or lowering the daily demand target below the real annual/365
reference — both explicitly out of scope this turn. Reported honestly,
not hidden or worked around.
