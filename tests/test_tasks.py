from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from pyflows.config import load_config
from pyflows.db import FileDB, FileStatus, compute_file_hash
from pyflows import tasks
from pyflows.tasks import (
    _release_held_files,
    _handle_encode_failure,
    _handle_encode_success,
    _is_transient_error,
    _select_best_file,
)


def test_transient_error_classification() -> None:
    assert _is_transient_error("Insufficient disk space") is True
    assert _is_transient_error("ffmpeg timed out after 10 seconds") is True
    assert _is_transient_error("Encode failed: unsupported codec") is False


def test_schedule_retry_roundtrip(tmp_config) -> None:
    config = load_config(tmp_config)
    with FileDB(config.general.db_path) as db:
        db.upsert(
            "/media/test.mkv",
            library="Movies",
            profile="test",
            file_hash="abc123",
            size=1000,
            video_codec="h264",
        )
        next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.schedule_retry("/media/test.mkv", "Insufficient disk space", 1, next_retry_at)

        record = db.get("/media/test.mkv")
        assert record is not None
        assert record["status"] == FileStatus.PENDING
        assert record["retry_count"] == 1
        assert record["error"] == "Insufficient disk space"
        retry_count, retry_at = db.get_retry_info("/media/test.mkv")
        assert retry_count == 1
        assert retry_at is not None
        assert retry_at == datetime.fromisoformat(record["next_retry_at"])


def test_release_held_files_queues_stable_file(tmp_config) -> None:
    config = load_config(tmp_config)
    media_file = Path(config.libraries[0].path) / "stable.mkv"
    media_file.write_bytes(b"x" * 1024)

    queued: list[tuple[str, str]] = []
    tasks._config = config
    tasks._encode_task = lambda path, profile: queued.append((path, profile))

    with FileDB(config.general.db_path) as db:
        observed_mtime = datetime.fromtimestamp(media_file.stat().st_mtime, tz=timezone.utc)
        hold_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.record_file_event(str(media_file), "Test Library", "test", media_file.stat().st_size, observed_mtime, hold_until)

    _release_held_files()

    assert queued == [(str(media_file), "test")]
    with FileDB(config.general.db_path) as db:
        record = db.get(str(media_file))
        assert record is not None
        assert record["hold_until"] is None
        assert record["file_hash"] == compute_file_hash(str(media_file))


def test_upsert_respects_retry_deadline(tmp_config) -> None:
    config = load_config(tmp_config)
    with FileDB(config.general.db_path) as db:
        db.upsert(
            "/media/test.mkv",
            library="Movies",
            profile="test",
            file_hash="abc123",
            size=1000,
            video_codec="h264",
        )
        future_retry = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.schedule_retry("/media/test.mkv", "Insufficient disk space", 1, future_retry)
        assert db.upsert("/media/test.mkv", "Movies", "test", "abc123", 1000, "h264") is False

        past_retry = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.schedule_retry("/media/test.mkv", "Insufficient disk space", 1, past_retry)
        assert db.upsert("/media/test.mkv", "Movies", "test", "abc123", 1000, "h264") is True


def test_upsert_respects_hold_and_retry_deadline(tmp_config) -> None:
    config = load_config(tmp_config)
    with FileDB(config.general.db_path) as db:
        db.upsert(
            "/media/test.mkv",
            library="Movies",
            profile="test",
            file_hash="abc123",
            size=1000,
            video_codec="h264",
        )
        future_retry = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.schedule_retry("/media/test.mkv", "Insufficient disk space", 1, future_retry)
        db.record_file_event(
            "/media/test.mkv",
            "Movies",
            "test",
            1000,
            datetime.now(timezone.utc),
            datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        assert db.upsert("/media/test.mkv", "Movies", "test", "abc123", 1000, "h264") is False


def test_disk_space_error_is_transient():
    """Verify disk space error from pipeline matches transient error markers for retry."""
    assert _is_transient_error("Insufficient disk space") is True
    assert _is_transient_error("Failed to replace original: [Errno 28] No space left on device") is True


def test_disk_space_error_triggers_retry(tmp_config) -> None:
    """Disk space failure from encode_file triggers retry scheduling, not terminal failure."""
    config = load_config(tmp_config)
    with FileDB(config.general.db_path) as db:
        db.upsert("/media/test.mkv", library="Test Library", profile="test",
                  file_hash="abc123", size=1000, video_codec="h264")
        db.update_status("/media/test.mkv", FileStatus.PROCESSING)

        notifier = MagicMock()
        _handle_encode_failure(db, "/media/test.mkv", "Insufficient disk space", True,
                               "test", config, notifier)

        record = db.get("/media/test.mkv")
        assert record["status"] == FileStatus.PENDING
        assert record["retry_count"] == 1
        assert record["next_retry_at"] is not None
        notifier.on_failure.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for the decomposed _encode_file helpers
# ---------------------------------------------------------------------------


def _setup_processing_file(db: FileDB, path: str, video_codec: str = "h264") -> None:
    """Insert a file and transition it to PROCESSING status."""
    db.upsert(path, library="Test Library", profile="test", file_hash="abc", size=1000, video_codec=video_codec)
    db.update_status(path, FileStatus.PROCESSING)


def test_handle_encode_success_updates_db(tmp_config, tmp_path) -> None:
    config = load_config(tmp_config)
    notifier = MagicMock()

    output_file = tmp_path / "output.mkv"
    output_file.write_bytes(b"encoded-content" * 100)
    final_path = str(output_file)

    with FileDB(config.general.db_path) as db:
        _setup_processing_file(db, final_path)

        profile = config.profiles["test"]
        _handle_encode_success(db, final_path, final_path, "test", profile, notifier, config)

        record = db.get(final_path)
        assert record is not None
        assert record["status"] == FileStatus.COMPLETED
        assert record["output_codec"] == profile.video.codec
        assert record["file_hash"] == compute_file_hash(final_path)

    notifier.on_success.assert_called_once()


def test_handle_encode_success_with_rename(tmp_config, tmp_path) -> None:
    config = load_config(tmp_config)
    notifier = MagicMock()

    output_file = tmp_path / "movie.mkv"
    output_file.write_bytes(b"encoded-content" * 100)
    old_path = str(tmp_path / "movie.mp4")
    new_path = str(output_file)

    with FileDB(config.general.db_path) as db:
        _setup_processing_file(db, old_path)

        profile = config.profiles["test"]
        _handle_encode_success(db, old_path, new_path, "test", profile, notifier, config)

        assert db.get(new_path) is not None
        assert db.get(old_path) is None
        record = db.get(new_path)
        assert record["status"] == FileStatus.COMPLETED


def test_handle_encode_failure_transient_schedules_retry(tmp_config) -> None:
    config = load_config(tmp_config)
    notifier = MagicMock()
    file_path = "/media/transient.mkv"

    with FileDB(config.general.db_path) as db:
        _setup_processing_file(db, file_path)

        _handle_encode_failure(db, file_path, "Insufficient disk space", True, "test", config, notifier)

        record = db.get(file_path)
        assert record is not None
        assert record["status"] == FileStatus.PENDING
        assert record["retry_count"] == 1
        assert record["next_retry_at"] is not None

    notifier.on_failure.assert_not_called()


def test_handle_encode_failure_terminal_marks_failed(tmp_config) -> None:
    config = load_config(tmp_config)
    notifier = MagicMock()
    file_path = "/media/terminal.mkv"

    with FileDB(config.general.db_path) as db:
        _setup_processing_file(db, file_path)

        _handle_encode_failure(db, file_path, "unsupported codec", False, "test", config, notifier)

        record = db.get(file_path)
        assert record is not None
        assert record["status"] == FileStatus.FAILED
        assert record["error"] == "unsupported codec"

    notifier.on_failure.assert_called_once_with(file_path, "unsupported codec")


def test_handle_encode_failure_exhausted_retries_marks_failed(tmp_config) -> None:
    config = load_config(tmp_config)
    notifier = MagicMock()
    file_path = "/media/exhausted.mkv"

    with FileDB(config.general.db_path) as db:
        _setup_processing_file(db, file_path)

        # Set retry_count to max_retries, then transition back to PROCESSING
        next_retry = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.schedule_retry(file_path, "previous error", config.general.max_retries, next_retry)
        db.update_status(file_path, FileStatus.PROCESSING)

        _handle_encode_failure(db, file_path, "Insufficient disk space", True, "test", config, notifier)

        record = db.get(file_path)
        assert record is not None
        assert record["status"] == FileStatus.FAILED

    notifier.on_failure.assert_called_once()


def test_select_best_file_no_swap(tmp_config) -> None:
    config = load_config(tmp_config)
    file_path = "/media/only_file.mkv"

    with FileDB(config.general.db_path) as db:
        db.upsert(file_path, library="Test Library", profile="test", file_hash="abc", size=1000, video_codec="h264")

        result_path, result_profile = _select_best_file(
            file_path, "test", db, priority_codecs=["hevc", "av1", "vp9"],
        )

    assert result_path == file_path
    assert result_profile == "test"


def test_select_best_file_swaps_to_priority(tmp_config) -> None:
    config = load_config(tmp_config)
    h264_path = "/media/h264_file.mkv"
    vp9_path = "/media/vp9_file.mkv"

    mock_encode = MagicMock()
    original_encode_task = tasks._encode_task
    tasks._encode_task = mock_encode

    try:
        with FileDB(config.general.db_path) as db:
            db.upsert(h264_path, library="Test Library", profile="test", file_hash="abc1", size=1000, video_codec="h264")
            db.upsert(vp9_path, library="Test Library", profile="test", file_hash="abc2", size=2000, video_codec="vp9")

            result_path, result_profile = _select_best_file(
                h264_path, "test", db, priority_codecs=["vp9"],
            )

        assert result_path == vp9_path
        assert result_profile == "test"
        mock_encode.assert_called_once_with(h264_path, "test")
    finally:
        tasks._encode_task = original_encode_task
