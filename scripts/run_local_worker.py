"""
Windows-safe local/dev continuous refresh loop.

`python -m app.worker` refuses to start on Windows by design (see
app/worker.py's ADIM "Worker Single-Instance Lock" - the flock(2)
single-instance protection needs the POSIX `fcntl` module, which does
not exist on Windows; the worker fails fast rather than start
unprotected). That lock only matters when MULTIPLE worker processes
could race on the same production DB - irrelevant for one local dev
terminal running the real, unmodified refresh loop.

This script does NOT touch app/worker.py. It imports and calls
`run_forever()` directly - the exact same function `app.worker.main()`
calls after acquiring the lock - so the real 5-minute cadence, the
real per-cycle error isolation, and the real retention integration are
all reused unmodified. The only thing skipped is the lock acquisition
itself.

Usage (PowerShell):
    $env:DATABASE_URL = "mysql+pymysql://bench_app:bench_app_pw@127.0.0.1:3309/aircraft_capacity_local_dev?charset=utf8mb4"
    $env:QUEUE_LOCAL_SOURCE_MODE = "generated"
    venv\\Scripts\\python.exe scripts\\run_local_worker.py

Ctrl+C to stop.
"""
import logging
import sys
from pathlib import Path

REPO = Path(r"C:\Users\assistant\aircraft-capacity-py")
sys.path.insert(0, str(REPO))

from app.worker import run_forever  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("scripts.run_local_worker")

if __name__ == "__main__":
    logger.info("Starting LOCAL DEV refresh loop (no single-instance lock - Windows has no fcntl). "
                 "Same run_forever() as production, 5-min cadence. Ctrl+C to stop.")
    try:
        run_forever()
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
