# Production auto-refresh deployment (systemd)

Two independent, always-on services — neither depends on the other being healthy:

- **`airport-queue-web.service`** — `python -m app.web.server` (read-only HTTP API + static frontend). Never touches ingestion.
- **`airport-queue-worker.service`** — `python -m app.worker` (background loop: reads source data, parses, `refresh_flights`, `run_predictions`, every 5 minutes). Never opens a socket.

## Install

```bash
sudo cp deploy/systemd/airport-queue-web.service /etc/systemd/system/
sudo cp deploy/systemd/airport-queue-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now airport-queue-web.service
sudo systemctl enable --now airport-queue-worker.service
```

Edit `WorkingDirectory`, `User`/`Group`, the venv path in `ExecStart`, and (if not using the default `database.sqlite` next to the project) uncomment `Environment=DATABASE_URL=...` in both units to point at the same database file.

## Operational guarantees

- No manual `python -m app.queue.pipeline` run is ever required — the worker service performs that same refresh automatically, forever, every 5 minutes.
- A crashed or failing worker cycle is logged (`journalctl -u airport-queue-worker`) and retried on the next cycle; it never brings down `airport-queue-web`, and the API keeps serving the last successfully persisted predictions in the meantime (upsert persistence — old rows are only overwritten by a newer *complete* run).
- Restarting either service does not require restarting the other.

## Checking status

```bash
systemctl status airport-queue-web airport-queue-worker
journalctl -u airport-queue-worker -f   # watch live refresh cycles
```
