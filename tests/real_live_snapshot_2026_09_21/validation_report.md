# Real 21/09/2026 IST/SIN/JFK Snapshot — Conversion & Validation

## Source files (user-provided, Desktop)
- IST: `ıst.txt` — 631 rows
- SIN: `sın.txt` — 177 rows
- JFK: `jfk.txt` — 279 rows (22 header/furniture lines stripped)

All three are ARRIVALS boards (confirmed by Status values Landed/Active/
Scheduled/Canceled and by the "Arrival" column holding the ORIGIN
city+IATA of a flight landing at the target airport, not a destination).
No departures data was provided this turn for any of the three airports.

## Conversion rules applied
- Schema target: existing `app/queue/ingestion/sources.py:parse_source_a_record()`
  snake_case fields (`dep_iata`, `arr_iata`, `arr_time_utc`,
  `arr_estimated_utc`, `status`, `location`, `direction`, `arr_gate`) —
  no new schema invented.
- `direction` = `"arrival"` for every row (all three sources are arrivals
  boards).
- `dep_iata` = IATA code extracted from the parenthesized code at the end
  of the source "Arrival" column (e.g. `"KAYSERI (ASR)"` → `ASR`).
  `arr_iata` = the target airport itself (IST/SIN/JFK).
- `location` copied verbatim from the source's own Domestic/International
  column (lowercased) — never re-derived.
- `arr_time_utc`/`arr_estimated_utc` = source's local Scheduled/Estimated
  time on 21/09/2026, converted with real `zoneinfo` timezones
  (IST→Europe/Istanbul, SIN→Asia/Singapore, JFK→America/New_York) to UTC.
  Real minutes preserved exactly (no rounding to a generic profile).
- `status` mapped: `Landed`→`landed`, `Active`→`active`,
  `Scheduled`→`scheduled`, `Canceled`→`cancelled` (spelling fixed to match
  `EXCLUDED_STATUSES = ("cancelled", "diverted")` — the source's American
  "Canceled" would NOT have matched the existing exclusion set otherwise).
- No departure-side fields (`dep_time_utc` etc.) are present — the source
  doesn't provide them, and none were fabricated. `arr_scheduled_utc`
  alone is sufficient for `canonical_operational_time()`'s arrival-branch.
- `aircraft_icao` = `null` for all 1087 rows — no real Source B (ADS-B)
  data was provided this turn; production's existing capacity-resolver
  fallback (`aircraft_match_found=False` → unknown-aircraft default)
  handles this exactly as it does for any other under-matched flight.
- Cancelled rows are converted (visible in the JSON) but excluded from
  queue demand by the existing, unmodified `EXCLUDED_STATUSES` check in
  `engine.py`/`domain/demand.py` — not by any new rule.
- No flight outside the source's own real time window was invented
  (IST rows only 10:15–22:55 local, SIN only 08:05–23:55, JFK only
  04:10–15:30 — the 24-bucket graph still shows all 24 hours because the
  unrepresented hours are legitimate real zero-demand, padded by the
  existing `_pad_series_to_24_hours()`, not synthetic flights).

## Output files
- `Delays - Type Arrivals.json` — the three airports' real arrival rows
  merged into the single file `file_source_a()` reads for the
  `"arrival"` direction (the production schema is one physical file per
  direction, not per airport).
- `Delays - Type Departures.json` — empty response (no real departure data
  this turn).
- `flights_live.json` — empty response (kept for schema completeness; the
  actual ingest passes an explicit empty Source B callable, not this file,
  since Source B's real filename is `response-delays.json`, not
  `flights_live.json`).
- `real_live.sqlite` — isolated SQLite DB (own `DATABASE_URL`), built via
  the unmodified production pipeline (`app/queue/pipeline.py:run()`),
  airport/scale reference loaded from the real production `data/`
  directory (read-only), flight data from this folder only. This file is
  intentionally SEPARATE from `tests/incoming_2026_09_19/incoming_2026_09_19.sqlite`
  — see note below.

## Important note: shared DB was NOT reused for this data
`tests/incoming_2026_09_19/incoming_2026_09_19.sqlite` turned out to be a
dual-purpose file: besides being the sandbox this session's earlier turns
added realistic 9-airport fixtures to, several pre-existing regression
tests (`test_ist_high_intl_departure_replay.py` and others) directly
re-ingest their own dense-IST-departure JSON fixtures into that exact
file as part of test execution. Replacing IST/SIN/JFK there with this
real arrivals-only data broke 5 of those tests. That change was reverted
(the shared DB was rebuilt via the same two-stage replay + 9-airport
fixture re-ingest that originally built it) and this real snapshot was
ingested into a brand-new, isolated file instead
(`tests/real_live_snapshot_2026_09_21/real_live.sqlite`).
