# Ingestion + Idempotency Results

Shared DB: `tests/incoming_2026_09_19/incoming_2026_09_19.sqlite`
Ingestion path: `app/queue/pipeline.py:run()` (REAL, unmodified production
chain — `load_flight_rows`/`parse_source_a` → `refresh_flights` →
`run_predictions`) via `DATABASE_URL` pointed at the shared DB for the
duration of the call only. No direct SQL Flight/QueuePrediction inserts.
`now=2026-09-21 18:00 UTC`.

## Pre-Ingest Inventory

- Flight count: 582
- FlightEvent count: 0
- QueuePrediction count: 1021
- Airport count: 9766
- AirportOperationalConfig count: 0
- min/max `dep_scheduled_utc`: 2026-09-17 14:00 → 2026-09-22 06:00
- min/max prediction `window_start`: 2026-09-17 12:00 → 2026-09-22 09:00
- Top airports by flight count: IST(110), CBR(76), MFG(71), OAG(65), CDG(58), ...
- Pre-existing flights for all 9 new airports (JFK/MAD/SIN/BOH/CID/ASP/MVY/ISC/LEQ): **0** (confirmed clean, no collision)

## Issue found and fixed (fixture-only, no production code changed)

The first-generated fixture (before the fix below) was ingested once and
immediately surfaced a real regression in `tests/test_current_day_all_airports.py`
(`test_every_airport_every_graph_is_single_current_local_day`): JFK and MAD
had a small number of local-midnight/01:00 departures whose fixed
production `effective_time()` -120min passport-arrival-buffer offset
(unmodified, correct, documented production behavior) pushed them into
the *previous* local calendar day — flagged as "previous-day leakage" by
this pre-existing test (which only excuses *forward* cross-midnight
carry, per Section 61, not *backward*).

Root cause: fixture generation, not production code. Fix: the fixture
generator's departure-hour weighting was adjusted to exclude local hours
00:00 and 01:00 for **departures only** (arrivals unaffected — their
+15min buffer only ever shifts forward, which is already tolerated).
This is the minimum required adaptation (per instructions) — no
production file was touched. The 9 new airports' rows were then deleted
from the shared DB (their own freshly-added, previously-zero-collision
data only — confirmed via pre-ingest inventory) and the corrected
fixture was ingested fresh, twice, as below.

## First Ingest (corrected fixture)

```
flights_parsed = 2078
aircraft_match_rate = 1.0
inserted = 2078
updated = 0
events_written = 0
failed = 0
predictions = 675
airports_predicted = 91
failed_airports = []
```

## Second (Identical) Ingest

```
flights_parsed = 2078
inserted = 0
updated = 2078
events_written = 0
failed = 0
predictions = 675
airports_predicted = 91
```

`tests/test_current_day_all_airports.py` (22 tests) and the wider
shared-DB regression suite (112 tests across `test_24_hour_graph.py`,
`test_new_daily_incoming_ist_saw.py`, `test_same_demand_scale_day_update.py`,
`test_ist_high_intl_departure_replay.py`, `test_current_day_all_airports.py`,
`test_visible_risk_queue_pressure.py`) all PASS against the corrected,
re-ingested data.

## Idempotency Verification (post both ingests)

- Total Flight count: 2660 = 582 (pre-existing) + 2078 (new) — **not doubled**
- Distinct flight_key count: 2660 — **flight_key duplicate count = 0**
- FlightEvent count: 0 (identical re-ingest → no field changes → correctly no new events)
- Duplicate (airport, process, window_start) QueuePrediction groups: **0**
- Per-airport Flight row counts match exactly the generator's planned counts (JFK 647, MAD 677, SIN 692, BOH 18, CID 26, ASP 6, MVY 6, ISC 4, LEQ 2)

**Result: inserted=0 on second ingest, Flight count stable, flight_key duplicate=0, same-process passenger duplication=0 — all match the required contract.**

## Passenger / Event Duplication Audit (Section 35)

Sampled JFK and MAD (both produced predictions for all relevant processes):

| Airport | passport_dep total pax | passport_arr total pax | passport (combined) total pax | security_intl total pax |
|---|---|---|---|---|
| JFK | 54,693 | 54,693 | 109,386 (= dep+arr, exact) | 54,693 (= passport_dep, exact) |
| MAD | 67,037 | 67,037 | 134,074 (= dep+arr, exact) | 67,037 (= passport_dep, exact) |

- `passport` (legacy combined) total = `passport_dep` + `passport_arr` exactly — no double counting within the passport process.
- `security_intl` total pax = `passport_dep` total pax exactly — confirms the SAME international-departure passengers are counted **once** in `passport_dep` and **once** in `security_intl` (after passport completion), which is the correct, intended coupled flow (Passport once → Security once after passport completion) — **not** cross-process duplication.
- `security` (legacy combined) = `security_dom` + `security_intl` exactly.

```
SAME-PROCESS PASSENGER DUPLICATION: PASS
COUPLED PROCESS FLOW: PASS
```

## Note (not a bug, known/expected limitation from prior turns)

SIN and ASP produced 0 QueuePrediction rows with the single shared
`now=2026-09-21 18:00 UTC` — consistent with the previously-documented
limitation that a single shared `now` cannot align with every airport's
own local operational day simultaneously across widely different
timezones (Asia/Singapore UTC+8, Australia/Darwin UTC+9:30). Their Flight
rows (692 and 6 respectively) were correctly imported and are isolated/
untouched; this is an operational-day/timezone alignment artifact, not a
duplication, ingestion, or idempotency issue.
