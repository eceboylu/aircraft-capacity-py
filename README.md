<div align="center">

# ✈️ Airport Queue Monitor

**Forecasts how busy airport security and passport control will be, straight from the flight schedule.**

For every airport it scores queue risk in 15-minute windows and explains **why** a window is busy.

![Python](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-D71F00?logo=sqlalchemy&logoColor=white)
![MySQL](https://img.shields.io/badge/MySQL-8-4479A1?logo=mysql&logoColor=white)
![AirLabs](https://img.shields.io/badge/Data-AirLabs%20API-0A66C2)
![pytest](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)
![systemd](https://img.shields.io/badge/deploy-systemd-30D475?logo=linux&logoColor=white)

</div>

---

## What it does

Given an airport's flight schedule, the system answers:

- How busy will **security** and **passport control** be over the next hours?
- Which 15-minute windows are at risk, and what is the expected wait in minutes?
- What is **causing** the pressure: several wide-body arrivals at once, delays piling up, a wave of international passengers?
- How much should this forecast be **trusted**?

Results are shown per airport in a web dashboard and are also available through a JSON API.

## Highlights

| | |
|---|---|
| 🛫 **Aircraft type → capacity** | Resolves seat counts from ICAO aircraft types (101 verified types plus fallback variants and family matches) |
| 👥 **Passenger demand model** | Uses load factor, range-based arrival buffers and delays to work out when passengers reach each queue |
| 🧮 **Queueing theory** | Erlang-C for passport control, an event-driven queue simulation and backlog tracking |
| 🛂 **4 separate queues** | Passport departures, passport arrivals, domestic security and international security are modelled independently |
| 🔍 **Reason detection** | 9 detection rules: clustering, wide-bodies, delay compression, cancellations, aircraft changes, diversions… |
| 📊 **Confidence score** | Every forecast reports how reliable it is, reduced transparently for each missing piece of data |
| 🌍 **Multi-airport** | Each airport is computed on its own scale, desk and staff configuration and time zone |
| 🔄 **Live data** | AirLabs API integration; a worker refreshes data and forecasts every 5 minutes |
| 🧹 **Controlled retention** | 48-hour operational retention behind two independent safety switches (off by default) |
| ✅ **Extensive tests** | 76 test files: mathematical golden tests, realistic flight scenarios, end-to-end and front-end tests |

## Architecture

```mermaid
flowchart LR
    AL[AirLabs API<br/>schedules + flights] --> W
    F[Source files<br/>data/] --> W
    W[Worker<br/>app.worker<br/>every 5 min] --> P[Pipeline<br/>ingestion → refresh → forecast]
    P --> DB[(MySQL)]
    DB --> API[Web server<br/>app.web.server<br/>read-only]
    API --> UI[Airport Queue Monitor<br/>dashboard]
```

The worker and the web server are **two separate processes**: if one crashes, the other keeps running. The web server only reads from the database; all computation happens in the worker. If a worker run fails, the last successful forecasts stay on screen.

### Forecast flow

```mermaid
flowchart TD
    A[Flight schedule] --> B[Normalise + match aircraft type]
    B --> C[Aircraft type → seat capacity]
    C --> D[Passenger demand<br/>load factor, arrival buffer, delay]
    D --> E[Routing rules<br/>domestic / international · departure / arrival]
    E --> F[15-minute windows]
    F --> G[Queue model<br/>Erlang-C · event-driven simulation]
    G --> H[Risk + wait time]
    G --> I[9 reason detectors]
    G --> J[Confidence score]
    H & I & J --> K[(queue_predictions)]
```

## API

The server only accepts `GET` requests.

| Endpoint | Returns |
|---|---|
| `GET /` | Dashboard |
| `GET /api/airports` | Tracked airports (`["ADB", "IST", "SAW"]`) |
| `GET /api/airports/directory` | Airports with their names |
| `GET /api/airports/{iata}/predictions` | Overall status, security, passport and breakdowns |

## Getting started

### Requirements

- Python 3
- MySQL 8 (or PostgreSQL)
- An AirLabs API key (only for live data)

### Installation

```bash
git clone https://github.com/eceboylu/aircraft-capacity-py.git
cd aircraft-capacity-py

python -m venv venv
venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
```

`DATABASE_URL` is **required**. Without it the app stops with a clear error instead of silently falling back to SQLite.

```bash
# Windows (PowerShell)
$env:DATABASE_URL = "mysql+pymysql://user:password@localhost:3306/airport_queue?charset=utf8mb4"
```

### Running

```bash
# 1) Aircraft type → capacity reference data (run in its own process)
venv\Scripts\python -m app.seed

# 2) One-off run: source data → database → forecasts
venv\Scripts\python -m app.queue.pipeline

# 3) Dashboard and API  →  http://localhost:8000
venv\Scripts\python -m app.web.server

# 4) Continuous refresh (every 5 minutes)
venv\Scripts\python -m app.worker

# Tests
venv\Scripts\python -m pytest -q
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | `mysql+pymysql://…` or `postgresql://…` |
| `AIRLABS_API_KEY` | For live data | AirLabs API key; never written to code or logs |
| `RETENTION_ENABLED` | No | When `true`, old records are cleaned up (default `false`) |
| `RETENTION_DRY_RUN` | No | Nothing is deleted until this is `false`; otherwise it only reports (default `true`) |
| `LOG_LEVEL` | No | Default `INFO` |

Example file: [`deploy/systemd/aircraft-capacity.env.example`](deploy/systemd/aircraft-capacity.env.example). Real values are **never** committed.

## Production

Two `systemd` services run on a Linux server:

| Service | Role |
|---|---|
| `airport-queue-web.service` | Dashboard and API (`app.web.server`) |
| `airport-queue-worker.service` | Refresh and forecast every 5 minutes (`app.worker`), with a single-instance lock |

Setup steps: [`deploy/systemd/README.md`](deploy/systemd/README.md)

## Project structure

```
app/
├── models.py, service.py, seed.py   Aircraft type → capacity resolver
├── db.py                            Database connection (DATABASE_URL required)
├── worker.py                        Background refresh process
├── web/
│   ├── server.py                    Read-only HTTP server + JSON API
│   └── static/index.html            Dashboard
└── queue/
    ├── core/                        Erlang-C, event-driven queue, scoring (pure maths)
    ├── domain/                      Demand, routing rules, airport scale, staffing
    ├── reasons/                     Reason detection rules
    ├── ingestion/                   AirLabs client, source parsing, upserts
    ├── engine.py                    Multi-airport orchestration
    ├── pipeline.py                  End-to-end runner
    ├── reporting.py, api.py         Reporting and API data layer
    └── retention.py                 Controlled data retention
data/                                Sample source data, airport and aircraft lists
deploy/systemd/                      Service and timer units
tests/                               Unit, scenario, end-to-end and front-end tests
```

---

# Technical documentation

The sections below describe the model's core design decisions and assumptions. Some details have evolved as the project grew; the comments in the code and the tests are the source of truth for current behaviour.

The system was built in three parts:

| Part | Scope | Location |
|---|---|---|
| 1 | Aircraft type (ICAO) → passenger capacity resolver | `app/models.py`, `app/db.py`, `app/seed.py`, `app/service.py` |
| 2 | Data merging, demand, queue model, reason detection | `app/queue/` |
| 3 | Persistence, multi-airport orchestration, reporting | `app/queue/engine.py`, `app/queue/reporting.py` |

Parts 2 and 3 **import and use** Part 1 without modifying it.

> **Note on `app.seed`:** the script loads three sources: 101 verified types (`yolcu_ucaklari.json`, the primary source), 11 fallback variants (`curated_fallback.json`) and 65 family / general aviation rows.
>
> `app.seed.run()` drops its tables with `drop_first=True`. Because the script does not import `app.queue.models`, the queue tables are not part of that drop; but if the queue models are imported first in the same process, the flight and forecast tables are dropped too. Always run the seed in its own process.

## Data flow

```
data/Delays - Type Departures.json ─┐
data/Delays - Type Arrivals.json  ──┤ Source A: time, terminal, status
                                    │
data/response-delays.json ──────────┤ Source B: aircraft_icao
                                    │
data/flight_airports.sql ───────────┘ airport + country + time zone
                 │
                 ▼
     ingestion/sources.py   (STAGE 0: normalise + enrich)
                 │
                 ▼
     ingestion/refresh.py   (upsert + FlightEvent)
                 │
                 ▼
     engine.py              (STAGES 2-8: flow, window, score, reason, confidence)
                 │
                 ▼
     reporting.py           (STAGE 9: hourly + summary data structures)
```

**Source A ↔ Source B matching** uses the flight number (`"AC 72"` → `"AC72"`). When no match is found, `aircraft_icao` stays `None`; Part 1 then falls back to `unknown_default` (150 seats) and the forecast's confidence drops. The system **does not crash** in this case.

> In the sample source files, Source B is a global, point-in-time ADS-B snapshot with little overlap with the 200 flights in Source A. A real run therefore shows a low `aircraft_match_rate` and confidence around 0.40. This is not a bug; it is an honest reflection of the data.

## Module map

```
app/queue/
├── constants.py              thresholds and explicit assumptions
├── models.py                 STAGE 8 tables
├── config.py                 STAGE 1 airport config resolution
├── baseline.py               clustering baseline (None until data accumulates)
├── engine.py                 orchestration + persistence
├── reporting.py              STAGE 9 data layer
├── pipeline.py               end-to-end runner
├── core/
│   ├── erlang.py             Erlang-C (pure maths)
│   └── scoring.py            security load score + passport Erlang-C
├── domain/
│   ├── flight_rules.py       load factor, buffer, delay
│   ├── demand.py             passenger demand + window aggregation
│   └── flows.py              STAGE 2 routing rules
├── reasons/detector.py       9 reason detection functions
└── ingestion/
    ├── sources.py            Source A/B parsing + enrichment
    ├── airports_import.py    flight_airports.sql import
    └── refresh.py            upsert + change events
```

The `core/`, `domain/` and `reasons/` layers **never touch the database**; they can all be tested with mock objects. Only the `engine`, `reporting`, `baseline`, `config` and `ingestion` layers use the database.

## Routing rules (STAGE 2)

| Flight | Security | Passport |
|---|---|---|
| Domestic + Departure | ✔ | — |
| Domestic + Arrival | — | — |
| International + Departure | ✔ | ✔ |
| International + Arrival | — | ✔ |
| Cancelled / Diverted | — | — |

Because `domestic_arrival` feeds no queue, it does not appear in the reporting structure **at all**.

## Security and passport use different formulas

**Security: a simple load signal.** The real number of lanes, desks and staff is unknown, so no queueing model is built. The signal compares how many flights fall in a window against the historical average. `estimated_wait_minutes` is **never** produced and is always `null`. When no history has accumulated yet, the risk is `UNKNOWN`; no fake baseline is generated.

**Passport: full Erlang-C.** The number of desks (c) and service rate (μ) come from the airport config, and the wait time is produced in minutes. When ρ ≥ 1 the queue is theoretically unsustainable: the risk is `CRITICAL` and `estimated_wait_minutes = null`.

## Explicit assumptions

The following are **not live data**; they are clearly marked static estimates and must be kept together with their comments in the code.

| Assumption | Value | Where |
|---|---|---|
| Load factor: domestic | 0.78 | `constants.py` |
| Load factor: short / medium / long-haul international | 0.82 / 0.84 / 0.88 | `constants.py` |
| Buffer before reaching security (no boarding time) | 45 / 60 / 90 min | `constants.py` |
| Buffer before reaching passport control (landing + walking) | 15 min | `constants.py` |
| Desk efficiency multiplier (2 people ≠ 2×) | 1.5 | `AirportOperationalConfig` |
| Demand window | 15 min | `constants.py` |

Flight duration is not provided directly either: it is a **distance proxy** derived from `arrScheduled − depScheduled`.

**Time precedence:** both the moment a flight reaches the queue and its delay are read in the order `actual > estimated > scheduled`. Both must use the same source: if the delay were read from `estimated` while the window was picked from `scheduled`, passengers of a flight that has not happened yet would be written to a window they will never be in, and the window they actually arrive in would look empty. Future flights have no `actual` yet; that is exactly the case the forecasting system is built for.

**The default airport config** (4 desks, 8 staff, 0.5 passengers/min per person) is a sample, not a measurement. With these values a single wide-body's passengers already exceed capacity in a 15-minute window; a real airport needs its own row in the `airport_operational_configs` table. Every forecast computed with the default loses 0.05 confidence.

## Multi-airport architecture

Computation always runs on **a single airport's** flight list; `run_predictions` repeats it for every airport in the system. Airports never see each other's flights, config or baseline.

Adding a new airport means adding **one row** to the `airport_operational_configs` table. No code change is needed; without a row the default config is used with `is_default=True`.

## Reason detection (STAGE 6)

**All** nine detection functions run for every window:

1. **clustering**: window flight count / historical average > 1.4
2. **widebody**: at least 2 aircraft with ≥250 seats, or a share above 30%
3. **delay_compression**: at least 2 delayed flights in the same window (a single delay becomes `single_delay`, an informational note that does not raise the risk)
4. **utilization**: ρ > 0.9 (passport only)
5. **intl_share**: more than 60% of flights are international (passport only)
6. **arrival_bank**: arrivals in the window ≥ the configured threshold (passport only)
7. **cancellation**: a cancelled flight is present
8. **aircraft_change**: an `AIRCRAFT_CHANGED` event with a non-zero capacity delta
9. **diversion**: `status == "diverted"`

When none of them crosses its threshold, a single `normal` item is written; the reason list is **never empty**. No free or generic text is generated: every message is filled from a fixed template.

Scoring adds two transparency notes of its own: `no_baseline` (no history yet for security) and `capacity_exceeded` (ρ ≥ 1 for passport). These are not detection functions; they explain why a score is `UNKNOWN` or `CRITICAL`.

There is no separate code for combinations: several reasons firing in the same window already is a combination.

## Confidence score (STAGE 7)

```
1.00
 -0.10  load factor is always a static assumption
 -0.10  boarding buffer is always derived
 -0.15  aircraft type match rate < 0.80
 -0.10  a flight in the window still has aircraft_icao = None
 -0.10  the clustering baseline has not accumulated yet
 -0.05  the airport config is the default
        (floor 0.10)
```

## Database and growth protection

- `flights`, `queue_predictions`, `historical_flight_counts`: **upserted** on every refresh, no new rows are opened. `queue_predictions` is unique on `(airport_iata, process, window_start)`.
- `flight_events`: a row is opened **only on a real change**: aircraft type change, crossing the delay threshold, cancellation, diversion. If the same data arrives again, nothing is written.
- `airports`: imported once from `flight_airports.sql`.
- No forecast row is written for a window without flights.
- When a flight is delayed into another window, the forecast for its old window is **deleted**. `queue_predictions` is a current-state table: it is derived deterministically from the flights table, and any row missing from a new computation is stale by definition. Long-term memory lives in `historical_flight_counts`, not in this table.

Baseline unit: "the average number of flights **in one 15-minute window** for that airport + process + hour + day of the week". Observations are recorded *after* the forecasts are computed, so a window never feeds its own baseline.

## Reporting (STAGE 9)

Two data structures are produced. Neither **triggers a new computation**; both read the stored `queue_predictions` rows.

- `hourly_report(session, iata, date, resolver)` → `/airports/{iata}/hourly`
- `summary_report(session, date)` → `/airports/summary`

An hourly row summarises that hour's 15-minute windows: risk = the worst window, ratio / ρ / wait = the highest value, confidence = the lowest value, reasons = deduplicated by code.

This layer does not draw charts; when an HTTP framework (FastAPI etc.) is added, these two functions can be used directly as endpoint bodies.

## Data sources the system does NOT have

There is no code, config, placeholder or variable for the following seven items; the system knows nothing about them:

1. Staff shortage feed
2. Equipment failure / lane closure signal
3. E-gate status
4. Passport system outage status
5. Weather disruption API
6. Live / real load factor
7. Boarding time

## Scenarios that cannot be detected

These are **deliberately not modelled**; no placeholder or commented-out code was left for them either.

- **Rebooking distribution.** Which flights passengers are moved to after a cancellation is unknown. The cancellation itself is detected (reason 7) and removed from demand, but the redistribution of passengers is **not modelled numerically**.
- **Connecting flight banks.** There is no transfer passenger data. Arrival bank (reason 6) is only a partial proxy.
- **Staff shortage + peak hour.** No staff data.
- **Equipment failure + high demand.** No equipment data.
- **Security incident + lane closure.** No lane status data.
- **Passport system failure + international wave.** No system status data.
- **E-gate failure + high volume.** No e-gate data.
- **Downstream effect of a diversion.** A diverted flight is removed from demand and noted, but its effect at the airport where passengers actually land is not modelled.

## Production scheduler (systemd)

The original design ran `python -m app.queue.pipeline` (see `app/queue/pipeline.py:main()`) as a periodic **batch**: each run performs ingestion → refresh → prediction → DB and then exits. No extra Python scheduler (APScheduler, Celery, a Redis queue, etc.) was added; on Linux production servers a `systemd timer` + `systemd service` take that role. The unit files live in `deploy/systemd/`. The continuous worker (`app.worker`) described in the Production section above was added later.

| File | Purpose |
|---|---|
| `deploy/systemd/aircraft-capacity.service` | Runs `python -m app.queue.pipeline` once (`Type=oneshot`) |
| `deploy/systemd/aircraft-capacity.timer` | Triggers the service at :00 and :30 every hour (`Persistent=true`) |
| `deploy/systemd/aircraft-capacity.env.example` | Example format for `EnvironmentFile=`; contains NO real secrets |

### Installation

```bash
# 1) Unprivileged system user + application directory
sudo useradd --system --home /opt/aircraft-capacity --shell /usr/sbin/nologin aircraft-capacity
sudo mkdir -p /opt/aircraft-capacity
# (copy or clone the repo into /opt/aircraft-capacity, then:)
cd /opt/aircraft-capacity
sudo -u aircraft-capacity python -m venv venv
sudo -u aircraft-capacity venv/bin/pip install -r requirements.txt

# 2) Secrets - OUTSIDE the repo, in a file with restricted permissions
sudo mkdir -p /etc/aircraft-capacity
sudo cp deploy/systemd/aircraft-capacity.env.example /etc/aircraft-capacity/aircraft-capacity.env
sudo chmod 600 /etc/aircraft-capacity/aircraft-capacity.env
sudo chown aircraft-capacity:aircraft-capacity /etc/aircraft-capacity/aircraft-capacity.env
# ^ edit this file with the REAL AIRLABS_API_KEY / DATABASE_URL values.

# 3) Install the unit files
sudo cp deploy/systemd/aircraft-capacity.service deploy/systemd/aircraft-capacity.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

### Enabling / starting the timer

```bash
sudo systemctl enable --now aircraft-capacity.timer
```

This does not start `aircraft-capacity.service` DIRECTLY; it only installs the timer. The first real run happens at the next :00 or :30 (thanks to `Persistent=true`, a run missed while the server was down is made up at the next boot).

### Checking status

```bash
systemctl status aircraft-capacity.timer     # is the timer active, when is the next trigger
systemctl list-timers aircraft-capacity.timer
systemctl status aircraft-capacity.service   # result of the last run (including exit code)
```

A critical error (see the exit-code decision in `main()`) shows up under `systemctl --failed`; a partial airport failure (`failed_airports`) does NOT mark the service as failed, it is only logged as a WARNING.

### Viewing logs

```bash
journalctl -u aircraft-capacity.service -f              # follow live
journalctl -u aircraft-capacity.service --since "2h ago" # history
```

The routine INFO logs produced by `app/logging_config.py` (run started/completed, ingestion/refresh/prediction summaries, duration) are read from here. No separate log file or rotation is needed; journald handles it.

### Manual (one-off) run

```bash
sudo systemctl start aircraft-capacity.service
# or, for development/debugging, directly:
sudo -u aircraft-capacity /opt/aircraft-capacity/venv/bin/python -m app.queue.pipeline
```

### Overlap protection

There is **no** separate lock file or PID check: systemd does NOT start a second instance when the same unit (`aircraft-capacity.service`) is already running and the timer fires again. If a run takes longer than 30 minutes, this guarantee prevents the next trigger from overlapping.

---

## Tests

```
tests/test_core_math.py         Erlang-C, risk bands, confidence
tests/test_domain_rules.py      load factor, buffer, delay, window
tests/test_ingestion.py         normalisation, enrichment, upsert, events
tests/test_reason_detection.py  9 detection functions and their thresholds
tests/test_engine.py            all 14 scenarios (engine level)
tests/test_reporting.py         persistence, growth protection, STAGE 9 outputs
```

These are the original core test files; the `tests/` directory now holds 76 test files, including realistic multi-airport replays, front-end contract tests and production hardening checks.

Scenario tests only cover situations that can be derived from real data. No tests were written for staff, equipment, e-gates, weather or passport system outages; those features do not exist in the system.
