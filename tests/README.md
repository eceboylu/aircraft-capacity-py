# Running the tests (MySQL required)

This project is MySQL-only end to end — `app/db.py` requires
`DATABASE_URL` to be set and rejects anything that isn't a MySQL
connection (no SQLite, no Postgres, no silent fallback). The test
suite follows the same rule: `tests/conftest.py` does **not** spin up
its own SQLite database — the `db_session` fixture talks to the real
`app.db.engine`, i.e. whatever MySQL server `DATABASE_URL` points at.

## One-time setup (local dev)

If you're using the `docker-compose.yml` at the repo root (see
`deploy/docker/README.md`), bring it up first:

```bash
docker compose up -d
```

Then create a dedicated **test** database on that same MySQL server —
deliberately separate from `aircraft_capacity` (your real dev data) so
the test suite can freely drop/recreate tables without touching it:

```bash
docker exec <local_mysql container> mysql -uroot -plocal_root_pw -e "
CREATE DATABASE IF NOT EXISTS aircraft_capacity_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON aircraft_capacity_test.* TO 'app'@'%';
FLUSH PRIVILEGES;
"
```

(Find the container name with `docker compose ps` — it's
`<project>-local_mysql-1`.)

## Running pytest

```bash
DATABASE_URL="mysql+pymysql://app:app_local_pw@127.0.0.1:3307/aircraft_capacity_test?charset=utf8mb4" \
    python -m pytest -q
```

`DATABASE_URL` must be set for **every** pytest run, even for test
files that don't touch the database — several test modules import
`app.queue.pipeline`, which imports `app.db` at module level, and
`app.db` raises immediately if `DATABASE_URL` is missing or isn't
MySQL. This is intentional, not an oversight: there is no "tests get a
free pass" exception anywhere in this codebase.

Point `DATABASE_URL` at a real MySQL server in CI the same way — any
MySQL 8.x instance works, it doesn't have to be this specific Docker
container, as long as the database the URL names already exists (MySQL
won't create it for you) and the user has full privileges on it.

## What `db_session` does to that database

The `db_session` fixture (`tests/conftest.py`) runs
`Base.metadata.drop_all()` + `create_all()` against the test database
**before every test that uses it** — full isolation, but destructive to
whatever is in that specific database. Never point `DATABASE_URL` at a
database you care about when running tests.
