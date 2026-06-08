"""Huey task definitions for scanning and encoding."""

import logging
import random
import re
import signal
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Callable

from huey import SqliteHuey, crontab  # type: ignore[import-not-found]
from watchdog.observers import Observer  # type: ignore[import-not-found]
from watchdog.events import FileSystemEventHandler  # type: ignore[import-not-found]

from pyflows.config import PyflowsConfig, LibraryConfig, ProfileConfig
from pyflows.db import FileDB, FileStatus, compute_file_hash
from pyflows.hooks import run_hooks
from pyflows.logging_utils import log_event
from pyflows.notify import Notifier
from pyflows.pipeline import EncodeStatus, encode_file
from pyflows.ffmpeg import terminate_active_encode
from pyflows.scanner import _probe_codec, scan_library
from pyflows.webhook import start_webhook_server

log = logging.getLogger(__name__)

EVERY_MINUTE = crontab(minute='*')

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


@dataclass
class DaemonState:
    config: PyflowsConfig
    huey: SqliteHuey
    encode_task: Callable[[str, str], object]
    scanning_enabled: threading.Event = field(default_factory=threading.Event)
    encoding_enabled: threading.Event = field(default_factory=threading.Event)
    watcher_enabled: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.scanning_enabled.set()
        self.encoding_enabled.set()
        self.watcher_enabled.set()


_state: DaemonState | None = None


def _get_state() -> DaemonState:
    if _state is None:
        raise RuntimeError("pyflows not initialized — call init_huey first")
    return _state


def get_pause_state() -> dict[str, bool]:
    s = _get_state()
    return {
        "scanning": s.scanning_enabled.is_set(),
        "encoding": s.encoding_enabled.is_set(),
        "watcher": s.watcher_enabled.is_set(),
    }


def set_pause_state(component: str, enabled: bool) -> bool:
    s = _get_state()
    events = {"scanning": s.scanning_enabled, "encoding": s.encoding_enabled, "watcher": s.watcher_enabled}
    ev = events.get(component)
    if ev is None:
        return False
    if enabled:
        ev.set()
    else:
        ev.clear()
    log_event(log, logging.INFO, "pause_state_changed",
              f"{'Resumed' if enabled else 'Paused'} {component}",
              component=component, enabled=enabled)
    return True


def init_huey(config: PyflowsConfig) -> SqliteHuey:
    global _state
    huey = SqliteHuey(
        filename=str(Path(config.general.db_path).parent / "huey.db"),
        immediate=False,
    )

    @huey.periodic_task(EVERY_MINUTE)  # type: ignore[untyped-decorator]
    def scan_all() -> None:
        _scan_all(respect_schedule=True)

    @huey.periodic_task(EVERY_MINUTE)  # type: ignore[untyped-decorator]
    def release_held_files() -> None:
        _release_held_files()

    @huey.task()  # type: ignore[untyped-decorator]
    def encode(file_path: str, profile_name: str) -> None:
        _encode_file(file_path, profile_name)

    _state = DaemonState(config=config, huey=huey, encode_task=encode)
    return huey


def _scan_library_if_due(db: FileDB, lib: LibraryConfig, respect_schedule: bool) -> None:
    if respect_schedule and not db.should_scan_library(lib.name, lib.scan_interval):
        return

    state = _get_state()
    new_files = scan_library(
        lib,
        db,
        stable_for_seconds=state.config.general.stable_for_seconds,
        ffprobe_path=state.config.general.ffprobe_path,
        priority_codecs=state.config.resolved_priority_codecs(),
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
    for file_path, profile, _codec in new_files:
        state.encode_task(file_path, profile)


def _scan_all(respect_schedule: bool = False) -> None:
    """Scan all libraries and queue encode tasks for new/changed files."""
    state = _get_state()
    if not state.scanning_enabled.is_set():
        return
    with FileDB(state.config.general.db_path) as db:
        for lib in state.config.libraries:
            _scan_library_if_due(db, lib, respect_schedule)


def _is_transient_error(error: str) -> bool:
    lowered = error.lower()
    return any(marker in lowered for marker in _TRANSIENT_ERROR_MARKERS)


def _release_held_files() -> None:
    state = _get_state()
    config = state.config
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
                state.encode_task(path, str(record["profile"]))
                log_event(log, logging.INFO, "file_stable_ready", "Queued stable held file",
                          file_path=path, library=str(record["library"]), codec=codec)


def _select_best_file(
    file_path: str,
    profile_name: str,
    db: FileDB,
    priority_codecs: list[str],
) -> tuple[str, str]:
    """Pick a higher-priority pending file if one exists, re-queuing the original."""
    best = db.get_next_pending(priority_codecs=priority_codecs)
    if best is not None and best["path"] != file_path:
        state = _get_state()
        state.encode_task(file_path, profile_name)
        log_event(log, logging.INFO, "encode_priority_swap",
                  "Swapped to higher-priority file",
                  original=file_path, selected=best["path"])
        return best["path"], best["profile"]
    return file_path, profile_name


def _handle_encode_success(
    db: FileDB,
    file_path: str,
    final_path: str,
    profile_name: str,
    profile: ProfileConfig,
    notifier: Notifier,
    config: PyflowsConfig,
) -> None:
    """Update DB, notify, and run hooks after a successful encode."""
    if final_path != file_path:
        db.rename_path(file_path, final_path)
    output_size = Path(final_path).stat().st_size
    new_hash = compute_file_hash(final_path)
    db.update_hash(final_path, new_hash, output_size)
    db.update_status(final_path, FileStatus.COMPLETED,
                     output_codec=profile.video.codec, output_size=output_size)
    log_event(
        log, logging.INFO, "encode_completed", "Completed file encode",
        file_path=final_path, profile=profile_name,
        output_codec=profile.video.codec, output_size=output_size,
    )
    arr_source, arr_id = db.get_arr_metadata(final_path)
    notifier.on_success(final_path, arr_source=arr_source, arr_id=arr_id)
    run_hooks(config.hooks.post_encode, "post_encode", final_path,
              profile=profile_name, output_path=final_path, status="completed",
              timeout=config.hooks.timeout)


def _handle_encode_failure(
    db: FileDB,
    file_path: str,
    error: str,
    transient: bool,
    profile_name: str,
    config: PyflowsConfig,
    notifier: Notifier,
) -> None:
    """Schedule a retry for transient errors or mark the file as permanently failed."""
    retry_count, _ = db.get_retry_info(file_path)
    next_retry_count = retry_count + 1
    if transient and retry_count < config.general.max_retries:
        delay = int(config.general.retry_backoff_seconds * (2 ** retry_count) * random.uniform(0.5, 1.5))
        next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        db.schedule_retry(file_path, error, next_retry_count, next_retry_at)
        log_event(
            log, logging.WARNING, "encode_retry_scheduled",
            "Scheduled retry for failed encode",
            file_path=file_path, profile=profile_name, reason=error,
            retry_count=next_retry_count, next_retry_at=next_retry_at.isoformat(),
        )
    else:
        db.update_status(file_path, FileStatus.FAILED, error=error)
        log_event(
            log, logging.ERROR, "encode_failed", "Failed file encode",
            file_path=file_path, profile=profile_name, reason=error,
            retry_count=next_retry_count if transient else retry_count,
            terminal=not transient or retry_count >= config.general.max_retries,
        )
        notifier.on_failure(file_path, error)
        run_hooks(config.hooks.on_failure, "on_failure", file_path,
                  profile=profile_name, status="failed", error=error,
                  timeout=config.hooks.timeout)


def _encode_file(file_path: str, profile_name: str) -> None:
    """Encode a single file using its profile.

    Before starting, checks whether a higher-priority pending file exists in
    the DB (hevc/av1/vp9 over h264/unknown).  If so, re-queues the requested
    file and encodes the higher-priority one instead.  This lets existing Huey
    queue entries benefit from priority ordering even though Huey itself is FIFO.
    """
    state = _get_state()
    if not state.encoding_enabled.is_set():
        state.encode_task(file_path, profile_name)
        return
    config = state.config
    if profile_name not in config.profiles:
        log_event(log, logging.ERROR, "unknown_profile", "Unknown profile", profile=profile_name)
        return

    with FileDB(config.general.db_path) as db:
        file_path, profile_name = _select_best_file(
            file_path, profile_name, db, config.resolved_priority_codecs())

        if not db.claim_for_processing(file_path):
            log_event(log, logging.DEBUG, "encode_claim_failed",
                      "File already claimed by another worker",
                      file_path=file_path)
            return

    if profile_name not in config.profiles:
        log_event(log, logging.ERROR, "unknown_profile", "Unknown profile after priority swap", profile=profile_name)
        return

    if not Path(file_path).exists():
        with FileDB(config.general.db_path) as db:
            db.update_status(file_path, FileStatus.FAILED, error="File no longer exists on disk")
        return

    profile = config.profiles[profile_name]
    notifier = Notifier(config.notifications)

    with FileDB(config.general.db_path) as db:

        if config.hooks.pre_encode:
            if not run_hooks(config.hooks.pre_encode, "pre_encode", file_path, profile=profile_name, timeout=config.hooks.timeout):
                db.update_status(file_path, FileStatus.FAILED, error="pre_encode hook failed")
                return

        try:
            result = encode_file(
                input_path=file_path,
                profile=profile,
                temp_dir=config.general.temp_dir,
                vaapi_device=config.general.vaapi_device,
                ffmpeg_path=config.general.ffmpeg_path,
                ffprobe_path=config.general.ffprobe_path,
                hardware_config=config.hardware,
                stall_timeout=config.general.stall_timeout,
                startup_timeout=config.general.startup_timeout,
            )
        except Exception as exc:
            db.update_status(file_path, FileStatus.FAILED, error=str(exc))
            log_event(log, logging.ERROR, "encode_exception",
                      "Unexpected exception during encode",
                      file_path=file_path, profile=profile_name, error=str(exc))
            return

        if result.status == EncodeStatus.SKIPPED:
            db.update_status(file_path, FileStatus.SKIPPED)
            log_event(log, logging.INFO, "encode_skipped", "Skipped file",
                      file_path=file_path, profile=profile_name)
            run_hooks(config.hooks.on_skip, "on_skip", file_path,
                      profile=profile_name, status="skipped",
                      timeout=config.hooks.timeout)
        elif result.status == EncodeStatus.COMPLETED:
            _handle_encode_success(db, file_path, result.final_path,
                                   profile_name, profile, notifier, config)
        else:
            _handle_encode_failure(db, file_path, result.error, result.transient,
                                   profile_name, config, notifier)


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
        if _state is not None and not _state.watcher_enabled.is_set():
            return
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


def start_daemon(config: PyflowsConfig, metrics_stop: threading.Event | None = None) -> None:
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

    state = _get_state()

    # Clean stale pyflows temp files (pattern: stem_8hexchars.ext)
    _TEMP_PATTERN = re.compile(r"^.+_[0-9a-f]{8}\..+$")
    temp_dir = Path(config.general.temp_dir)
    if temp_dir.exists():
        for f in temp_dir.iterdir():
            if f.is_file() and _TEMP_PATTERN.match(f.name):
                f.unlink()
                log_event(log, logging.INFO, "temp_file_cleaned", "Cleaned stale temp file", file_path=str(f))

    # Start webhook server (both modes)
    webhook_server = start_webhook_server(config, state.encode_task)

    # File watcher and scanner (daemon mode only)
    observer = None
    handler = None
    if mode == "daemon":
        observer = Observer()
        handler = _MediaFileHandler(config, state.encode_task)
        for lib in config.libraries:
            if Path(lib.path).exists():
                observer.schedule(handler, lib.path, recursive=True)
        observer.start()

        # Run initial scan (respects scan_interval to avoid re-scanning on restart)
        _scan_all(respect_schedule=True)

    # Start Huey consumer in current thread (blocks)
    shutdown = threading.Event()

    def signal_handler(sig: int, frame: FrameType | None) -> None:
        log_event(log, logging.INFO, "shutdown_requested", "Shutting down daemon")
        terminate_active_encode()
        shutdown.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if config.general.workers > 1:
        log_event(
            log,
            logging.INFO,
            "workers_config",
            f"Running with {config.general.workers} worker threads",
            workers=config.general.workers,
        )

    log_event(log, logging.INFO, "daemon_started", "pyflows daemon started",
              mode=mode, workers=config.general.workers)
    consumer = state.huey.create_consumer(workers=config.general.workers, worker_type="thread")
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
        if metrics_stop is not None:
            metrics_stop.set()
        log_event(log, logging.INFO, "daemon_stopped", "pyflows daemon stopped")
