"""Prometheus metrics exporter for pyflows."""

import logging
import sqlite3
import threading
from datetime import datetime, timezone

from prometheus_client import (  # type: ignore[import-untyped]
    Gauge,
    start_http_server,
)

log = logging.getLogger("pyflows.metrics")

# ── Metrics definitions ─────────────────────────────────────────────────────

files_gauge = Gauge(
    "pyflows_files",
    "Number of files per library and status",
    ["library", "status"],
)

encode_duration = Gauge(
    "pyflows_current_encode_duration_seconds",
    "Duration of the current encode in seconds",
)

encodes_total = Gauge(
    "pyflows_encodes_total",
    "Total encodes by status across all libraries",
    ["status"],
)

service_up = Gauge(
    "pyflows_up",
    "pyflows service is running",
)
service_up.set(1)


# ── Collector loop ───────────────────────────────────────────────────────────

def _collect(db_path: str) -> None:
    """Update all gauges from DB."""
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            statuses = ("pending", "processing", "failed", "skipped", "completed")

            # Per-library stats
            libraries = [
                row[0] for row in
                cur.execute("SELECT DISTINCT library FROM files WHERE library IS NOT NULL")
            ]
            for library in libraries:
                for status in statuses:
                    n = cur.execute(
                        "SELECT count(*) FROM files WHERE library=? AND status=?",
                        (library, status),
                    ).fetchone()[0]
                    files_gauge.labels(library=library, status=status).set(n)

            # Global totals
            for status in statuses:
                n = cur.execute(
                    "SELECT count(*) FROM files WHERE status=?", (status,)
                ).fetchone()[0]
                encodes_total.labels(status=status).set(n)

            # Current encode duration
            row = cur.execute(
                "SELECT started_at FROM files WHERE status='processing' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                try:
                    started = datetime.fromisoformat(row[0])
                    now = datetime.now(timezone.utc)
                    encode_duration.set((now - started).total_seconds())
                except ValueError:
                    encode_duration.set(0)
            else:
                encode_duration.set(0)
    except sqlite3.Error as e:
        log.warning("metrics: db collection error: %s", e)


def _collector_loop(db_path: str, interval: int = 15) -> None:
    while True:
        _collect(db_path)
        threading.Event().wait(interval)


# ── Public API ───────────────────────────────────────────────────────────────

def start_metrics_server(port: int, db_path: str, interval: int = 15) -> None:
    """Start Prometheus metrics HTTP server and background collector."""
    # Background collector thread
    t = threading.Thread(
        target=_collector_loop,
        args=(db_path, interval),
        daemon=True,
        name="metrics-collector",
    )
    t.start()

    # HTTP server (prometheus_client built-in)
    start_http_server(port)
    log.info(
        "Prometheus metrics available",
        extra={"event": "metrics_started", "port": port},
    )
