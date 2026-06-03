"""Huey task definitions for scanning and encoding."""

import logging
import random
import re
import signal
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Callable

from huey import SqliteHuey, crontab  # type: ignore[import-not-found]
from watchdog.observers import Observer  # type: ignore[import-not-found]
from watchdog.events import FileSystemEventHandler  # type: ignore[import-not-found]

from pyflows.config import PyflowsConfig, LibraryConfig
from pyflows.db import FileDB, FileStatus, compute_file_hash
from pyflows.logging_utils import log_event
from pyflows.notify import Notifier
from pyflows.pipeline import encode_file
from pyflows.scanner import _probe_codec, scan_library
from pyflows.webhook import start_webhook_server

log = logging.getLogger(__name__)

# Module-level state set by start_daemon
_config: PyflowsConfig | None = None
_huey: SqliteHuey | None = None

EVERY_MINUTE = crontab(minute='*')

# Task references (set by _register_tasks)
_encode_task: Callable[..., Any] | None = None

_TRANSIENT_ERROR_MARKERS = (
    "insufficient disk space",
    "timed out",
    "temporarily unavailable",
    "resource busy",
    "device or resource busy",
    "input/output error",
    "i/o error",
    "no space left",
)


def _get_config() -> PyflowsConfig:
    """Return the module-level config, raising if not initialized."""
    assert _config is not None, "pyflows not initialized — call init_huey first"
    return _config


def init_huey(config: PyflowsConfig) -> SqliteHuey:
    global _config, _huey
    _config = config
    _huey = SqliteHuey(
        filename=str(Path(config.general.db_path).parent / "huey.db"),
        immediate=False,
    )
    return _huey


def _register_tasks() -> Callable[..., Any]:
    """Register Huey tasks. Must be called after init_huey."""
    global _encode_task
    assert _huey is not None, "Huey not initialized — call init_huey first"
    huey = _huey

    @huey.periodic_task(EVERY_MINUTE)  # type: ignore[untyped-decorator]
    def scan_all() -> None:
        _do_scan_all(respect_schedule=True)

    @huey.periodic_task(EVERY_MINUTE)  # type: ignore[untyped-decorator]
    def release_held_files() -> None:
        _do_release_held_files()

    @huey.task()  # type: ignore[untyped-decorator]
    def encode(file_path: str, profile_name: str) -> None:
        _do_encode(file_path, profile_name)

    _encode_task = encode

    encode_ref: Callable[..., Any] = encode
    return encode_ref


def _scan_library_if_due(db: FileDB, lib: LibraryConfig, respect_schedule: bool) -> None:
    if respect_schedule and not db.should_scan_library(lib.name, lib.scan_interval):
        return

    config = _get_config()
    new_files = scan_library(
        lib,
        db,
        stable_for_seconds=config.general.stable_for_seconds,
        ffprobe_path=config.general.ffprobe_path,
        priority_codecs=config.resolved_priority_codecs(),
    )
    db.record_library_scan(lib.name)
    log_event(
        log,
        logging.INFO,
        "library_scanned",
        "Library scan completed",
        library=lib.name,
        scan_interval=lib.scan_interval,
        new_files=len(new_files),
    )
    # new_files is already sorted by codec_priority (HW-decodable first),
    # so hevc/av1/vp9 files enter Huey's FIFO queue ahead of h264/unknown.
    for file_path, profile, _codec in new_files:
        assert _encode_task is not None
        _encode_task(file_path, profile)


def _do_scan_all(respect_schedule: bool = False) -> None:
    """Scan all libraries and queue encode tasks for new/changed files."""
    config = _get_config()
    with FileDB(config.general.db_path) as db:
        for lib in config.libraries:
            _scan_library_if_due(db, lib, respect_schedule)


def _is_transient_error(error: str) -> bool:
    lowered = error.lower()
    return any(marker in lowered for marker in _TRANSIENT_ERROR_MARKERS)


def _do_release_held_files() -> None:
    config = _get_config()
    now = datetime.now(timezone.utc)
    with FileDB(config.general.db_path) as db:
        for record in db.list_ready_held_files(now):
            path = str(record["path"])
            p = Path(path)
            if not p.exists():
                db.clear_hold(path)
                continue
            if record["status"] == FileStatus.PROCESSING:
                continue
            stat = p.stat()
            observed_size = record["last_observed_size"]
            observed_mtime = record["last_observed_mtime"]
            current_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if observed_size != stat.st_size or observed_mtime != current_mtime.isoformat():
                hold_until = now + timedelta(seconds=config.general.stable_for_seconds)
                db.record_file_event(
                    path,
                    str(record["library"]),
                    str(record["profile"]),
                    stat.st_size,
                    current_mtime,
                    hold_until,
                )
                log_event(
                    log,
                    logging.INFO,
                    "file_hold_extended",
                    "Extended file hold because file changed again",
                    file_path=path,
                    hold_until=hold_until.isoformat(),
                )
                continue

            file_hash = compute_file_hash(path)
            changed = db.upsert(
                path=path,
                library=str(record["library"]),
                profile=str(record["profile"]),
                file_hash=file_hash,
                size=stat.st_size,
            )
            db.clear_hold(path)
            if changed:
                codec = _probe_codec(path, config.general.ffprobe_path)
                db.update_video_codec(path, codec)
                assert _encode_task is not None
                _encode_task(path, str(record["profile"]))
                log_event(log, logging.INFO, "file_stable_ready", "Queued stable held file",
                          file_path=path, library=str(record["library"]), codec=codec)


def _do_encode(file_path: str, profile_name: str) -> None:
    """Encode a single file using its profile.

    Before starting, checks whether a higher-priority pending file exists in
    the DB (hevc/av1/vp9 over h264/unknown).  If so, re-queues the requested
    file and encodes the higher-priority one instead.  This lets existing Huey
    queue entries benefit from priority ordering even though Huey itself is FIFO.
    """
    config = _get_config()
    if profile_name not in config.profiles:
        log_event(log, logging.ERROR, "unknown_profile", "Unknown profile", profile=profile_name)
        return

    # Priority check: pick the best pending file instead of blindly encoding
    # whatever Huey dequeued.  Only swap if a different file ranks higher.
    #
    # SAFETY: this check is NOT atomic with update_status(PROCESSING) below.
    # With workers=1 (the required and default setting) there is no race.
    # With workers>1 two workers could both select the same file from
    # get_next_pending() and double-encode it.  The warning in start_daemon()
    # guards against misconfiguration.
    with FileDB(config.general.db_path) as _check_db:
        best = _check_db.get_next_pending(priority_codecs=config.resolved_priority_codecs())
        if best is not None and best["path"] != file_path:
            # Re-queue the requested file and encode the better candidate.
            assert _encode_task is not None
            _encode_task(file_path, profile_name)
            original_path = file_path
            file_path = best["path"]
            profile_name = best["profile"]
            log_event(log, logging.INFO, "encode_priority_swap",
                      "Swapped to higher-priority file",
                      original=original_path, selected=file_path)

    if profile_name not in config.profiles:
        log_event(log, logging.ERROR, "unknown_profile", "Unknown profile after priority swap", profile=profile_name)
        return

    profile = config.profiles[profile_name]
    notifier = Notifier(config.notifications)

    with FileDB(config.general.db_path) as db:
        db.update_status(file_path, FileStatus.PROCESSING)

        try:
            success, error, final_path = encode_file(
                input_path=file_path,
                profile=profile,
                temp_dir=config.general.temp_dir,
                vaapi_device=config.general.vaapi_device,
                ffmpeg_path=config.general.ffmpeg_path,
                ffprobe_path=config.general.ffprobe_path,
                hardware_config=config.hardware,
                stall_timeout=config.general.stall_timeout,
            )
        except Exception as exc:
            db.update_status(file_path, FileStatus.FAILED, error=str(exc))
            log_event(
                log,
                logging.ERROR,
                "encode_exception",
                "Unexpected exception during encode",
                file_path=file_path,
                profile=profile_name,
                error=str(exc),
            )
            return

        if error == "skipped":
            db.update_status(file_path, FileStatus.SKIPPED)
            log_event(log, logging.INFO, "encode_skipped", "Skipped file", file_path=file_path, profile=profile_name)
        elif success:
            if final_path != file_path:
                db.rename_path(file_path, final_path)
            output_size = Path(final_path).stat().st_size
            new_hash = compute_file_hash(final_path)
            db.update_hash(final_path, new_hash, output_size)
            db.update_status(final_path, FileStatus.COMPLETED,
                             output_codec=profile.video.codec, output_size=output_size)
            log_event(
                log,
                logging.INFO,
                "encode_completed",
                "Completed file encode",
                file_path=final_path,
                profile=profile_name,
                output_codec=profile.video.codec,
                output_size=output_size,
            )
            arr_source, arr_id = db.get_arr_metadata(final_path)
            notifier.on_success(final_path, arr_source=arr_source, arr_id=arr_id)
        else:
            retry_count, _ = db.get_retry_info(file_path)
            next_retry_count = retry_count + 1
            if _is_transient_error(error) and retry_count < config.general.max_retries:
                delay = int(config.general.retry_backoff_seconds * (2 ** retry_count) * random.uniform(0.5, 1.5))
                next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                db.schedule_retry(file_path, error, next_retry_count, next_retry_at)
                log_event(
                    log,
                    logging.WARNING,
                    "encode_retry_scheduled",
                    "Scheduled retry for failed encode",
                    file_path=file_path,
                    profile=profile_name,
                    reason=error,
                    retry_count=next_retry_count,
                    next_retry_at=next_retry_at.isoformat(),
                )
            else:
                db.update_status(file_path, FileStatus.FAILED, error=error)
                log_event(
                    log,
                    logging.ERROR,
                    "encode_failed",
                    "Failed file encode",
                    file_path=file_path,
                    profile=profile_name,
                    reason=error,
                    retry_count=next_retry_count if _is_transient_error(error) else retry_count,
                    terminal=not _is_transient_error(error) or retry_count >= config.general.max_retries,
                )
                notifier.on_failure(file_path, error)


class _MediaFileHandler(FileSystemEventHandler):  # type: ignore[misc]
    """Watchdog handler that records media file changes and defers processing until stable."""

    def __init__(self, config: PyflowsConfig, encode_task: Callable[..., Any]) -> None:
        self.config = config
        self._encode_task = encode_task
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._lib_map: dict[str, tuple[LibraryConfig, set[str]]] = {}
        for lib in config.libraries:
            for ext in lib.extensions:
                self._lib_map.setdefault(lib.path, (lib, set()))[1].add(f".{ext}")

    def on_created(self, event: Any) -> None:
        if event.is_directory:
            return
        self._debounce(event.src_path)

    def on_modified(self, event: Any) -> None:
        if event.is_directory:
            return
        self._debounce(event.src_path)

    def on_moved(self, event: Any) -> None:
        if event.is_directory:
            return
        self._debounce(event.dest_path)

    def _debounce(self, path: str) -> None:
        """Coalesce repeated file events before recording deferred processing state."""
        with self._lock:
            if path in self._timers:
                self._timers[path].cancel()
            delay = self.config.general.watcher_event_debounce_seconds
            timer = threading.Timer(delay, self._handle, args=[path])
            self._timers[path] = timer
            timer.start()

    def stop(self) -> None:
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()

    def _handle(self, path: str) -> None:
        with self._lock:
            self._timers.pop(path, None)
        p = Path(path)
        if not p.exists():
            return
        if any(path.endswith(suffix) for suffix in self.config.general.ignore_suffixes):
            log_event(log, logging.INFO, "watcher_ignored", "Ignored watcher event for temporary/incomplete file", file_path=path)
            return
        for lib_path, (lib, exts) in self._lib_map.items():
            if p.is_relative_to(lib_path) and p.suffix.lower() in exts:
                stat = p.stat()
                observed_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                hold_until = datetime.now(timezone.utc) + timedelta(seconds=self.config.general.stable_for_seconds)
                log_event(log, logging.INFO, "watcher_detected", "File watcher detected change", file_path=path, library=lib.name)
                with FileDB(self.config.general.db_path) as db:
                    db.record_file_event(path, lib.name, lib.profile, stat.st_size, observed_mtime, hold_until)
                log_event(log, logging.INFO, "file_hold_set", "Deferred watcher-detected file until stable", file_path=path, library=lib.name, hold_until=hold_until.isoformat())
                return


def start_daemon(config: PyflowsConfig) -> None:
    """Start the pyflows daemon.

    Modes:
      - "daemon": Full mode — file watcher + periodic scanner + webhook + Huey worker.
      - "webhook": Webhook-only — no file watcher or scanner. Only processes files
        received via webhook. Still uses Huey for task queuing.
    """
    mode = config.general.mode
    huey = init_huey(config)

    # Crash recovery: reset any stale 'processing' records
    with FileDB(config.general.db_path) as db:
        reset = db.reset_processing()
        if reset > 0:
            log_event(log, logging.INFO, "processing_reset", "Reset stale processing records", count=reset)

        # Backfill video_codec for pending files that predate codec probing.
        if mode == "daemon":
            rows = db.get_pending_without_codec()
            if rows:
                log_event(log, logging.INFO, "codec_backfill_start",
                          "Probing codecs for pending files without codec data",
                          count=len(rows))
                for row in rows:
                    path = str(row["path"])
                    codec = _probe_codec(path, config.general.ffprobe_path)
                    if codec:
                        db.update_video_codec(path, codec)
                log_event(log, logging.INFO, "codec_backfill_done",
                          "Codec backfill complete", count=len(rows))

    # Clean stale pyflows temp files (pattern: stem_8hexchars.ext)
    _TEMP_PATTERN = re.compile(r"^.+_[0-9a-f]{8}\..+$")
    temp_dir = Path(config.general.temp_dir)
    if temp_dir.exists():
        for f in temp_dir.iterdir():
            if f.is_file() and _TEMP_PATTERN.match(f.name):
                f.unlink()
                log_event(log, logging.INFO, "temp_file_cleaned", "Cleaned stale temp file", file_path=str(f))

    # Register Huey tasks (needed in both modes for the worker)
    encode_task = _register_tasks()

    # Start webhook server (both modes)
    webhook_server = start_webhook_server(config, encode_task)

    # File watcher and scanner (daemon mode only)
    observer = None
    handler = None
    if mode == "daemon":
        observer = Observer()
        handler = _MediaFileHandler(config, encode_task)
        for lib in config.libraries:
            if Path(lib.path).exists():
                observer.schedule(handler, lib.path, recursive=True)
        observer.start()

        # Run initial scan
        _do_scan_all(respect_schedule=False)

    # Start Huey consumer in current thread (blocks)
    shutdown = threading.Event()

    def signal_handler(sig: int, frame: FrameType | None) -> None:
        log_event(log, logging.INFO, "shutdown_requested", "Shutting down daemon")
        shutdown.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if config.general.workers > 1:
        log_event(
            log,
            logging.WARNING,
            "workers_unsafe",
            "workers > 1 is not safe with priority-swap encoding: two workers may "
            "race on get_next_pending() and double-encode the same file. "
            "Set workers: 1 in config to eliminate this risk.",
            workers=config.general.workers,
        )

    log_event(log, logging.INFO, "daemon_started", "pyflows daemon started",
              mode=mode, workers=config.general.workers)
    consumer = huey.create_consumer(workers=config.general.workers, worker_type="thread")
    consumer.start()

    try:
        shutdown.wait()
    finally:
        consumer.stop()
        if handler is not None:
            handler.stop()
        if observer is not None:
            observer.stop()
            observer.join()
        if webhook_server is not None:
            webhook_server.shutdown()
        log_event(log, logging.INFO, "daemon_stopped", "pyflows daemon stopped")
