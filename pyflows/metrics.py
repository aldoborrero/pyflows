"""Prometheus metrics exporter for pyflows."""

import logging
import sqlite3
import threading
from datetime import datetime, timezone

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

from pyflows.ffmpeg import get_current_progress

log = logging.getLogger("pyflows.metrics")

files_gauge = Gauge(
    "pyflows_files_by_status",
    "Number of files per library and status",
    ["library", "status"],
)

encode_duration = Gauge(
    "pyflows_current_encode_duration_seconds",
    "Duration of the current encode in seconds",
)

files_by_status = Gauge(
    "pyflows_files_status",
    "Total files by status across all libraries",
    ["status"],
)

encode_progress_us = Gauge(
    "pyflows_encode_progress_microseconds",
    "Current encode progress in microseconds",
)

encode_speed = Gauge(
    "pyflows_encode_speed",
    "Current encode speed multiplier",
)

service_up = Gauge(
    "pyflows_up",
    "pyflows service is running",
)
service_up.set(1)


def _collect(db_path: str) -> None:
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.cursor()

        for row in cur.execute(
            "SELECT library, status, count(*) FROM files "
            "WHERE library IS NOT NULL GROUP BY library, status"
        ):
            files_gauge.labels(library=row[0], status=row[1]).set(row[2])

        for row in cur.execute(
            "SELECT status, count(*) FROM files GROUP BY status"
        ):
            files_by_status.labels(status=row[0]).set(row[1])

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

        progress = get_current_progress()
        encode_progress_us.set(progress.out_time_us)
        encode_speed.set(progress.speed)
    except sqlite3.Error as e:
        log.warning("metrics: db collection error: %s", e)
    finally:
        if conn is not None:
            conn.close()


def _collector_loop(db_path: str, stop_event: threading.Event, interval: int = 15) -> None:
    while not stop_event.is_set():
        _collect(db_path)
        stop_event.wait(interval)


def start_metrics_server(
    port: int, db_path: str, interval: int = 15, stop_event: threading.Event | None = None,
) -> threading.Event:
    if stop_event is None:
        stop_event = threading.Event()
    t = threading.Thread(
        target=_collector_loop,
        args=(db_path, stop_event, interval),
        daemon=True,
        name="metrics-collector",
    )
    t.start()
    start_http_server(port)
    log.info(
        "Prometheus metrics available",
        extra={"event": "metrics_started", "port": port},
    )
    return stop_event
