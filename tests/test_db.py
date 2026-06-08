# nix/packages/pyflows/tests/test_db.py
"""Tests for SQLite file tracking database."""

from datetime import datetime, timedelta, timezone

import pytest  # type: ignore[import-not-found]

from pyflows.db import FileDB, FileStatus, compute_file_hash


def test_create_tables(tmp_path):
    """Database creates tables on init."""
    with FileDB(str(tmp_path / "test.db")):
        pass
    assert (tmp_path / "test.db").exists()


def test_upsert_and_get(tmp_path):
    """Can insert a file and retrieve it by path."""
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", library="Movies", profile="movie",
                  file_hash="abc123", size=1000, video_codec="h264")
        record = db.get("/media/test.mkv")
        assert record is not None
        assert record["status"] == FileStatus.PENDING
        assert record["video_codec"] == "h264"


def test_upsert_updates_on_hash_change(tmp_path):
    """Re-upserting with different hash resets status to pending."""
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", library="Movies", profile="movie",
                  file_hash="abc123", size=1000, video_codec="h264")
        db.update_status("/media/test.mkv", FileStatus.COMPLETED)
        # File changed (different hash)
        db.upsert("/media/test.mkv", library="Movies", profile="movie",
                  file_hash="def456", size=2000, video_codec="h264")
        record = db.get("/media/test.mkv")
        assert record["status"] == FileStatus.PENDING
        assert record["file_hash"] == "def456"


def test_skip_if_same_hash(tmp_path):
    """Upserting same hash on completed file returns False (skip)."""
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", library="Movies", profile="movie",
                  file_hash="abc123", size=1000, video_codec="h264")
        db.update_status("/media/test.mkv", FileStatus.COMPLETED)
        changed = db.upsert("/media/test.mkv", library="Movies", profile="movie",
                             file_hash="abc123", size=1000, video_codec="h264")
        assert changed is False


def test_terminal_failed_file_with_same_hash_is_not_requeued(tmp_path):
    """Terminal failed files are not re-queued unless retry scheduling marks them pending again."""
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", library="Movies", profile="movie",
                  file_hash="abc123", size=1000, video_codec="h264")
        db.update_status("/media/test.mkv", FileStatus.FAILED, error="boom")
        changed = db.upsert("/media/test.mkv", library="Movies", profile="movie",
                             file_hash="abc123", size=1000, video_codec="h264")
        assert changed is False


def test_get_pending(tmp_path):
    """get_by_status returns only matching status."""
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/a.mkv", library="Movies", profile="movie",
                  file_hash="a", size=100, video_codec="h264")
        db.upsert("/media/b.mkv", library="Movies", profile="movie",
                  file_hash="b", size=200, video_codec="hevc")
        db.update_status("/media/b.mkv", FileStatus.SKIPPED)
        pending = db.get_by_status(FileStatus.PENDING)
        assert len(pending) == 1
        assert pending[0]["path"] == "/media/a.mkv"


def test_get_next_pending_uses_configured_priority_codecs(tmp_path):
    """get_next_pending prefers configured priority codecs over FIFO order."""
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/a.mkv", library="Movies", profile="movie",
                  file_hash="a", size=100, video_codec="h264")
        db.upsert("/media/b.mkv", library="Movies", profile="movie",
                  file_hash="b", size=200, video_codec="vp9")
        best = db.get_next_pending(priority_codecs=["vp9"])
        assert best is not None
        assert best["path"] == "/media/b.mkv"


def test_get_history(tmp_path):
    """get_history returns completed/failed/skipped sorted by completed_at DESC."""
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/a.mkv", library="Movies", profile="movie",
                  file_hash="a", size=100, video_codec="h264")
        db.upsert("/media/b.mkv", library="Movies", profile="movie",
                  file_hash="b", size=200, video_codec="h264")
        db.upsert("/media/c.mkv", library="Movies", profile="movie",
                  file_hash="c", size=300, video_codec="h264")
        db.update_status("/media/a.mkv", FileStatus.COMPLETED)
        db.update_status("/media/b.mkv", FileStatus.FAILED, error="test error")
        # c is still pending — should not appear
        history = db.get_history(limit=10)
        assert len(history) == 2
        # b was completed_at after a
        assert history[0]["path"] == "/media/b.mkv"
        assert history[1]["path"] == "/media/a.mkv"


def test_context_manager(tmp_path):
    """FileDB works as a context manager."""
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", library="Movies", profile="movie",
                  file_hash="abc", size=100, video_codec="h264")
        record = db.get("/media/test.mkv")
        assert record is not None


def test_record_file_event_and_ready_hold(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        observed_mtime = datetime.now(timezone.utc) - timedelta(seconds=30)
        hold_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.record_file_event("/media/test.mkv", "Movies", "movie", 1234, observed_mtime, hold_until)

        record = db.get("/media/test.mkv")
        assert record is not None
        assert record["hold_until"] == hold_until.isoformat()
        assert record["last_observed_size"] == 1234

        ready = db.list_ready_held_files(datetime.now(timezone.utc))
        assert len(ready) == 1
        assert ready[0]["path"] == "/media/test.mkv"

        db.clear_hold("/media/test.mkv")
        cleared = db.get("/media/test.mkv")
        assert cleared is not None
        assert cleared["hold_until"] is None


def test_upsert_respects_future_hold(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", library="Movies", profile="movie", file_hash="abc123", size=1000, video_codec="h264")
        hold_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        observed_mtime = datetime.now(timezone.utc)
        db.record_file_event("/media/test.mkv", "Movies", "movie", 1000, observed_mtime, hold_until)

        assert db.upsert("/media/test.mkv", "Movies", "movie", "abc123", 1000, "h264") is False


def test_upsert_hash_change_preserves_hold_gate(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", library="Movies", profile="movie", file_hash="abc123", size=1000, video_codec="h264")
        db.update_status("/media/test.mkv", FileStatus.COMPLETED)
        hold_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        observed_mtime = datetime.now(timezone.utc)
        db.record_file_event("/media/test.mkv", "Movies", "movie", 2000, observed_mtime, hold_until)

        assert db.upsert("/media/test.mkv", "Movies", "movie", "def456", 2000, "h264") is False
        record = db.get("/media/test.mkv")
        assert record is not None
        assert record["file_hash"] == "def456"
        assert record["hold_until"] == hold_until.isoformat()


def test_library_scan_schedule_tracking(tmp_path):
    """Library scans are due initially and suppressed until the interval passes."""
    with FileDB(str(tmp_path / "test.db")) as db:
        assert db.should_scan_library("Movies", 3600) is True
        db.record_library_scan("Movies")
        assert db.should_scan_library("Movies", 3600) is False

        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        db.conn.execute(
            "UPDATE library_scans SET last_scan_at=? WHERE library=?",
            (old, "Movies"),
        )
        db.conn.commit()
        assert db.should_scan_library("Movies", 3600) is True


def test_compute_file_hash(tmp_path):
    """Partial hash is deterministic for the same content."""
    f = tmp_path / "test.bin"
    f.write_bytes(b"x" * 200000)  # 200KB
    h1 = compute_file_hash(str(f))
    h2 = compute_file_hash(str(f))
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_compute_file_hash_small_file(tmp_path):
    """Hash works correctly for files smaller than HASH_CHUNK."""
    f = tmp_path / "small.bin"
    f.write_bytes(b"y" * 1000)  # 1KB — smaller than 64KB chunk
    h1 = compute_file_hash(str(f))
    h2 = compute_file_hash(str(f))
    assert h1 == h2
    assert len(h1) == 64


def test_compute_file_hash_medium_file(tmp_path):
    """Hash works for files between 64KB and 128KB (previously overlapping)."""
    f = tmp_path / "medium.bin"
    f.write_bytes(b"z" * 90000)  # 90KB — between 64KB and 128KB
    h1 = compute_file_hash(str(f))
    h2 = compute_file_hash(str(f))
    assert h1 == h2
    assert len(h1) == 64


def test_claim_for_processing_success(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", library="Movies", profile="movie",
                  file_hash="abc123", size=1000, video_codec="h264")
        assert db.claim_for_processing("/media/test.mkv") is True
        record = db.get("/media/test.mkv")
        assert record["status"] == FileStatus.PROCESSING
        assert record["started_at"] is not None


def test_claim_for_processing_double_claim(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", library="Movies", profile="movie",
                  file_hash="abc123", size=1000, video_codec="h264")
        assert db.claim_for_processing("/media/test.mkv") is True
        assert db.claim_for_processing("/media/test.mkv") is False


def test_claim_for_processing_wrong_status(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", library="Movies", profile="movie",
                  file_hash="abc123", size=1000, video_codec="h264")
        db.update_status("/media/test.mkv", FileStatus.COMPLETED)
        assert db.claim_for_processing("/media/test.mkv") is False


def test_claim_for_processing_nonexistent(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        assert db.claim_for_processing("/media/nonexistent.mkv") is False
