"""Library directory scanner and file watcher."""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pyflows.config import LibraryConfig
from pyflows.db import FileDB, compute_file_hash
from pyflows.logging_utils import log_event
from pyflows.probe import probe_file

log = logging.getLogger(__name__)


def _probe_codec(path: str, ffprobe_path: str) -> str:
    """Return the video codec for a file, or empty string on any error."""
    try:
        result = probe_file(path, ffprobe_path=ffprobe_path)
        return result.video.codec if result.video else ""
    except Exception as e:
        log_event(log, logging.WARNING, "probe_codec_failed",
                  "Failed to probe video codec", file_path=path, reason=str(e))
        return ""


def codec_priority(codec: str, priority_codecs: list[str] | set[str]) -> int:
    """Lower number = higher priority."""
    return 0 if codec in priority_codecs else 1


def scan_library(lib: LibraryConfig, db: FileDB, stable_for_seconds: int = 0,
                 ffprobe_path: str = "ffprobe", priority_codecs: list[str] | None = None) -> list[tuple[str, str, str]]:
    """Walk a library directory and queue new/changed files.

    Probes each new/changed file with ffprobe to capture the video codec so the
    caller can enqueue HW-decodable files (hevc/av1/vp9) ahead of others.

    Returns a list of (path, profile, codec) tuples for newly queued files,
    sorted by codec_priority() so Pipeline-1-eligible files come first.
    """
    root = Path(lib.path)
    if not root.exists():
        log_event(log, logging.WARNING, "library_missing", "Library path does not exist", library=lib.name, path=str(root))
        return []

    extensions = {f".{ext}" for ext in lib.extensions}
    queued: list[tuple[str, str, str]] = []
    priority_set = {codec.lower() for codec in (priority_codecs or [])}

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue

        file_path = str(path)
        stat = path.stat()
        size = stat.st_size

        if stable_for_seconds > 0:
            observed_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if observed_mtime > datetime.now(timezone.utc) - timedelta(seconds=stable_for_seconds):
                hold_until = datetime.now(timezone.utc) + timedelta(seconds=stable_for_seconds)
                db.record_file_event(file_path, lib.name, lib.profile, size, observed_mtime, hold_until)
                log_event(
                    log,
                    logging.INFO,
                    "file_hold_set",
                    "Deferring recently modified file until stable",
                    file_path=file_path,
                    library=lib.name,
                    hold_until=hold_until.isoformat(),
                )
                continue

        existing = db.get(file_path)
        if existing is not None and existing["size"] == size and existing["status"] in ("completed", "skipped", "pending", "processing"):
            continue

        file_hash = compute_file_hash(file_path)

        changed = db.upsert(
            path=file_path,
            library=lib.name,
            profile=lib.profile,
            file_hash=file_hash,
            size=size,
        )
        if changed:
            # Only probe codec when the file actually needs processing —
            # avoids ~3 min of ffprobe calls per scan on already-completed files.
            codec = _probe_codec(file_path, ffprobe_path)
            db.update_video_codec(file_path, codec)
            queued.append((file_path, lib.profile, codec))
            log_event(log, logging.INFO, "file_queued", "Queued file for processing",
                      file_path=file_path, library=lib.name, profile=lib.profile, codec=codec)

    # Sort: HW-decodable (Pipeline 1) files first so they enter Huey's queue ahead
    # of h264/unknown files. Within each priority tier, preserve filesystem order.
    queued.sort(key=lambda t: codec_priority(t[2], priority_set))
    return queued
