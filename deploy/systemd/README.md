# Production auto-refresh deployment (systemd)

Two independent, always-on services — neither depends on the other being healthy, and **only these two** should ever be installed/enabled on a production host:

- **`airport-queue-web.service`** — `python -m app.web.server` (read-only HTTP API + static frontend). Never touches ingestion, never runs a migration.
- **`airport-queue-worker.service`** — `python -m app.worker` (background loop: reads source data, parses, `refresh_flights`, `run_predictions`, every 5 minutes; also runs the 48h retention cleanup cycle internally — see "Retention scheduling" below). Never opens a socket. Enforces a single-instance `flock(2)` lock (`/run/airport-queue/worker.lock`) so a second accidental `app.worker` process exits immediately instead of writing to the DB concurrently.

The legacy `aircraft-capacity.service`/`aircraft-capacity.timer` pattern (see "Legacy units" at the bottom) must **not** be installed alongside these — it has no lock and no coordination with the worker, so running both would let two independent processes write to the same `DATABASE_URL` at once.

## Production deploy procedure (single source of truth)

Run every step in this order, every deploy. Steps 4–7 (backup/verify/migrate) only touch the database; steps 8–11 only touch systemd state; nothing here ever needs `alembic downgrade` (see Rollback).

1. **Code update** — `git pull` / checkout the release commit on the server.
2. **venv / dependencies** — `venv/bin/pip install -r requirements.txt` (installs the pinned `alembic` too — see `alembic/env.py`).
3. **DATABASE_URL / secrets** — confirm `/etc/aircraft-capacity/aircraft-capacity.env` has the real `DATABASE_URL`, `AIRLABS_API_KEY`, etc. (format documented in `deploy/systemd/aircraft-capacity.env.example`; secrets are never edited into the unit files themselves — both units only reference the path via `EnvironmentFile=`).
4. **Backup** — `python scripts/mysql_backup.py` (see "MySQL backup, restore rehearsal" below).
5. **Restore verify** — `python scripts/mysql_restore_verify.py <backup file>` into a throwaway DB; require all checks to report `PASS` before continuing.
6. **Alembic `stamp head`** — **only** the first time this host adopts Alembic on a database that already has the full schema from `create_all()` (a pre-Alembic deploy). Never run this on a database that is already tracked by Alembic — it would falsely mark it up to date without checking. One-time, not part of routine deploys.
7. **Alembic `upgrade head`** — every deploy that introduces a new migration. Safe to run even when there is nothing new (no-op).
8. **`sudo systemctl daemon-reload`** — picks up any unit-file changes shipped in this deploy.
9. **Legacy service/timer cleanup** — see the exact commands below. Only needed the first time a host is moved off the legacy pattern; a no-op on hosts that never had it installed. Never touches `airport-queue-worker.service`/`airport-queue-web.service`.
10. **Enable/start `airport-queue-worker.service`** — `sudo systemctl enable --now airport-queue-worker.service` (or `restart` if already running).
11. **Enable/start `airport-queue-web.service`** — `sudo systemctl enable --now airport-queue-web.service` (or `restart` if already running).
12. **Status check** — `systemctl status airport-queue-web airport-queue-worker` — both `active (running)`.
13. **Journal check** — `journalctl -u airport-queue-worker -n 50` — a recent `worker refresh #N ok: {...}` line with no exception traceback.
14. **Health endpoint check** — `curl -s http://127.0.0.1:8000/health` — `"status": "healthy"` (or `"degraded"` immediately after a fresh start, before the first refresh cycle completes; `"unhealthy"` is the only failure state).

### Legacy service/timer cleanup (step 9)

Only run this on a host that has the old units installed (check first with `systemctl list-unit-files | grep aircraft-capacity`). It disables and stops only the legacy unit names below — it never touches `airport-queue-worker.service`/`airport-queue-web.service`, so it is safe to run unconditionally even if the legacy units were never installed (the commands simply report "not loaded"/no-op in that case):

```bash
sudo systemctl disable --now aircraft-capacity.timer
sudo systemctl disable --now aircraft-capacity.service
sudo systemctl daemon-reload
sudo systemctl reset-failed
```

If the legacy `.service`/`.timer` files were ever copied into `/etc/systemd/system/`, also remove them there (the repo's own copies under `deploy/systemd/` are kept for reference only — see "Legacy units" below — and are never read by systemd unless copied out):

```bash
sudo rm -f /etc/systemd/system/aircraft-capacity.service /etc/systemd/system/aircraft-capacity.timer
sudo systemctl daemon-reload
```

### Rollback

There is no `alembic downgrade` step in this procedure and none should be added — the current baseline migration's `downgrade()` drops every application table (verified: it is the schema's root revision, so there is nothing earlier to fall back to). If a deploy needs to be rolled back, stop both services, restore the pre-deploy backup (step 4/5's file) into a new database via `scripts/mysql_restore_verify.py`, point `DATABASE_URL` at that restored database, then start both services again — this is the same restore path exercised in step 5, just used for real. It preserves the failed database untouched for investigation instead of destroying it.

## First-time install (new host, nothing installed yet)

```bash
sudo cp deploy/systemd/airport-queue-web.service /etc/systemd/system/
sudo cp deploy/systemd/airport-queue-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now airport-queue-web.service
sudo systemctl enable --now airport-queue-worker.service
```

Edit `WorkingDirectory`, `User`/`Group`, and the venv path in `ExecStart` in both unit files first if they differ from the shipped defaults (`/opt/airport-queue`, user/group `airportqueue`). Both units read `DATABASE_URL` (and other secrets) from `EnvironmentFile=/etc/aircraft-capacity/aircraft-capacity.env` — never edit a URL/secret directly into the unit file.

## Retention scheduling

Retention has no systemd unit of its own — it is not a separate timer/service. `app/worker.py:run_forever()` runs it internally, on its own 48-hour monotonic counter, fully decoupled from the 5-minute refresh loop (a refresh failure never skips/delays retention and vice versa — each has its own `try/except` in the loop body). Because it lives inside the single-instance `airport-queue-worker.service` process (protected by the same `flock`), there is exactly one place retention can run from — it cannot run concurrently with itself or with a second worker.

## MySQL backup, restore rehearsal, and schema migration

Requirements: MySQL 8 client tools (`mysqldump` and `mysql`) must be on
`PATH`; the project virtualenv must contain the normal application
dependencies. The scripts read credentials from environment variables and
pass them to MySQL through a temporary mode-0600 option file. They never print
the URL/password or place the password in the process command line.

The backup uses `--single-transaction --quick`, so all InnoDB tables are read
from one consistent snapshot without taking a global read lock. Do not run
schema-changing DDL while the dump is in progress. MyISAM tables, if ever
introduced, are not covered by the transaction guarantee.

Before every production migration, load the same protected environment file
used by the services and create the backup:

```bash
set -a
. /etc/aircraft-capacity/aircraft-capacity.env
set +a

python scripts/mysql_backup.py
```

`MYSQL_BACKUP_DIR` chooses the output directory. Files are named
`<database>_YYYYmmddTHHMMSSZ.sql.gz`. `MYSQL_BACKUP_RETENTION_DAYS=0` (the
default) deletes nothing. A positive value opts in to age-based deletion,
which runs only after a new backup succeeds and only for matching backup files
for the same database.

Prove the backup before migrating by restoring it into a new, empty temporary
database. The account in `MYSQL_RESTORE_URL` needs `CREATE DATABASE`; the
database named in that URL must not exist. The script refuses an existing DB,
then compares every base-table row count, compares `alembic_version`, and runs
a real ORM query against `aircraft_capacity`:

```bash
# Put MYSQL_RESTORE_URL=... in this root-owned mode-0600 file; do not put
# the credential directly in shell history.
set -a
. /etc/aircraft-capacity/mysql-restore.env
set +a
python scripts/mysql_restore_verify.py /var/backups/aircraft-capacity/mysql/aircraft_YYYYmmddTHHMMSSZ.sql.gz
```

Run the rehearsal while writes are paused, or immediately around the backup;
otherwise a live source can legitimately gain rows after the snapshot and the
live-source row-count comparison will report a mismatch.

Once all three verification lines report `PASS`, continue with steps 6–14 of
the "Production deploy procedure" above. Never run `alembic upgrade head`
until the backup and restore rehearsal have succeeded, and never use
`alembic downgrade` for rollback — see "Rollback" above.

### Alembic details

Schema changes are managed by Alembic (`alembic/versions/`), never by the
app itself at startup or at any point during a refresh cycle -
`airport-queue-worker.service`/`airport-queue-web.service` never invoke
`alembic` in any form. `app.db.init_db()` (still called at the top of every
`pipeline.run()`, i.e. every 5-minute worker cycle) only calls
`Base.metadata.create_all()`, which is a no-op against tables that already
match the current schema — it is not how schema changes are meant to be
applied, Alembic is.

## Operational guarantees

- No manual `python -m app.queue.pipeline` run is ever required — the worker service performs that same refresh automatically, forever, every 5 minutes.
- A crashed or failing worker cycle is logged (`journalctl -u airport-queue-worker`) and retried on the next cycle; it never brings down `airport-queue-web`, and the API keeps serving the last successfully persisted predictions in the meantime (upsert persistence — old rows are only overwritten by a newer *complete* run).
- Restarting either service does not require restarting the other.

## Checking status

```bash
systemctl status airport-queue-web airport-queue-worker
journalctl -u airport-queue-worker -f   # watch live refresh cycles
```

## Legacy units - do NOT install

`aircraft-capacity.service` and `aircraft-capacity.timer` in this same
directory are a **deprecated, pre-`app/worker.py` deployment pattern**
(a 30-minute systemd-timer-triggered one-shot batch run of
`python -m app.queue.pipeline`, with no worker lock, no internal
refresh loop, and neither of `pipeline.run()`'s 48h safety filters
enabled - see the file headers of both units for the full detail).

**Never enable them on a server that also runs `airport-queue-worker.service`.**
Both would write to the same `DATABASE_URL` independently and
uncoordinated: `app.worker`'s `flock` only protects against a second
`app.worker` process (verified live — a second instance exits immediately
with "duplicate", and the kernel releases the lock the instant the holder
dies, by any means, with no stale-lock window). `app/queue/pipeline.py`'s
own `main()` has no lock at all, so `aircraft-capacity.service` running the
old batch path would write to the same tables completely uncoordinated with
the worker. Install only the two services documented at the top of this
file. If a host still has the legacy units installed, remove them using the
exact commands in "Legacy service/timer cleanup (step 9)" above — do not
run `systemctl disable`/`stop` against `airport-queue-worker.service` or
`airport-queue-web.service` while doing so.
