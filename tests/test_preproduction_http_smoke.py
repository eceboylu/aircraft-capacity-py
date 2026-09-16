"""Real HTTP smoke coverage for the production read-only server contract."""

from datetime import datetime, timezone
import json
from threading import Thread
from urllib.request import urlopen

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.queue.constants import (
    PROCESS_PASSPORT,
    PROCESS_SECURITY_DOMESTIC,
    PROCESS_SECURITY_INTL,
)
from app.queue.models import Airport, QueuePrediction
import app.web.server as server_module


def _prediction(process: str) -> QueuePrediction:
    return QueuePrediction(
        airport_iata="IST",
        process=process,
        window_start=datetime(2026, 9, 16, 8, 0),
        window_end=datetime(2026, 9, 16, 9, 0),
        flight_count=1,
        expected_passengers=180,
        baseline_ratio=1.0 if process != PROCESS_PASSPORT else None,
        flight_ratio=1.0 if process != PROCESS_PASSPORT else None,
        passenger_ratio=1.0 if process != PROCESS_PASSPORT else None,
        utilization=0.5 if process == PROCESS_PASSPORT else None,
        estimated_wait_minutes=0.1 if process == PROCESS_PASSPORT else None,
        risk="LOW",
        reasons="[]",
        confidence=0.8,
        calculated_at=datetime.now(timezone.utc),
    )


def test_real_http_get_contract_for_all_frontend_endpoints(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    session.add(Airport(
        iata_code="IST",
        airport_name="Istanbul Airport",
        country_code="TR",
    ))
    session.add_all([
        _prediction(PROCESS_SECURITY_DOMESTIC),
        _prediction(PROCESS_SECURITY_INTL),
        _prediction(PROCESS_PASSPORT),
    ])
    session.commit()
    session.close()

    monkeypatch.setattr(server_module, "get_session", lambda: SessionLocal())
    httpd = server_module.ThreadingHTTPServer(
        ("127.0.0.1", 0), server_module.QueueMonitorHandler
    )
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    try:
        with urlopen(base + "/", timeout=5) as response:
            assert response.status == 200
            assert "text/html" in response.headers["Content-Type"]
            assert b"airport-content" in response.read()

        with urlopen(base + "/api/airports", timeout=5) as response:
            assert response.status == 200
            assert json.load(response) == ["IST"]

        with urlopen(base + "/api/airports/directory", timeout=5) as response:
            assert response.status == 200
            assert json.load(response) == [
                {"iata": "IST", "name": "Istanbul Airport"}
            ]

        with urlopen(base + "/api/airports/IST/predictions", timeout=5) as response:
            assert response.status == 200
            payload = json.load(response)
            assert payload["airport"] == "IST"
            assert set((
                "overall",
                "domestic_security",
                "international_security",
                "international_passport",
            )).issubset(payload)
            for key in (
                "overall",
                "domestic_security",
                "international_security",
                "international_passport",
            ):
                assert payload[key]["windows"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
