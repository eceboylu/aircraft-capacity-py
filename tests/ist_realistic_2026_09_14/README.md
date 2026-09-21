# IST realistic replay fixture — 2026-09-14

This directory contains synthetic, derived test data shaped like the existing
AirLabs payloads. It is not a published Istanbul Airport timetable.

The fixture is scoped to IST:

- departures always have `dep_iata=IST`;
- arrivals always have `arr_iata=IST`;
- partner airports remain populated for route analysis;
- the schedule covers every local hour and models broad morning, midday,
  evening, and overnight traffic banks;
- production queue, operational-day, routing, capacity, and graph code is not
  modified by this fixture.

## Files

- `Delays - Type Departures.json`: AirLabs-shaped departure records.
- `Delays - Type Arrivals.json`: AirLabs-shaped arrival records.
- `flights_live.json`: valid empty Source B payload; Source A already contains
  recognized aircraft ICAO values.
- `ist_realistic.sqlite`: isolated replay database, including predictions.
- `analysis_report.json`: hourly traffic, routes, mix, graph counts, and queue
  peak metrics.
- `generate_fixture.py`: deterministic fixture generator.
- `replay_and_report.py`: existing replay pipeline adapter and report writer.

## Rebuild

From the repository root:

```powershell
.\venv\Scripts\python.exe tests\ist_realistic_2026_09_14\generate_fixture.py
.\venv\Scripts\python.exe tests\ist_realistic_2026_09_14\replay_and_report.py
```

Both scripts write only to this directory. Production `database.sqlite` and
production `data/*` are not written.

## Local browser

The database already contains predictions, so a worker is not required.

```powershell
$env:DATABASE_URL = "sqlite:///$PWD/tests/ist_realistic_2026_09_14/ist_realistic.sqlite"
.\venv\Scripts\python.exe -m app.web.server --port 8000
```

Open `http://localhost:8000/?now=2026-09-14T12:00:00`.
