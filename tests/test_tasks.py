from datetime import datetime, timedelta, timezone
from pathlib import Path

from pyflows.config import load_config
from pyflows.db import FileDB, FileStatus, compute_file_hash
from pyflows import tasks
from pyflows.tasks import _do_release_held_files, _is_transient_error


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

    _do_release_held_files()

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
