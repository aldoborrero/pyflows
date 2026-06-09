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


# --- Fencing token tests ---


def test_claim_with_token_and_start(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        assert db.claim_with_token("/media/test.mkv", "tok-1", needs_gpu=True) is True
        record = db.get("/media/test.mkv")
        assert record["status"] == "processing"
        assert record["dispatch_token"] == "tok-1"
        assert record["needs_gpu"] == 1
        assert record["started_at"] is None
        assert db.start_encode("/media/test.mkv", "tok-1") is True
        record = db.get("/media/test.mkv")
        assert record["started_at"] is not None


def test_start_encode_rejects_duplicate(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        assert db.start_encode("/media/test.mkv", "tok-1") is True
        assert db.start_encode("/media/test.mkv", "tok-1") is False


def test_start_encode_rejects_wrong_token(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        assert db.start_encode("/media/test.mkv", "wrong") is False


def test_release_claim(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        assert db.release_claim("/media/test.mkv", "tok-1") is True
        record = db.get("/media/test.mkv")
        assert record["status"] == "pending"
        assert record["dispatch_token"] is None


def test_fail_attempt_by_token(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        assert db.fail_attempt("tok-1", "crash") is True
        record = db.get("/media/test.mkv")
        assert record["status"] == "failed"
        assert record["error"] == "crash"
        assert record["dispatch_token"] is None
        assert record["needs_gpu"] == 0


def test_fail_attempt_from_committing(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        db.transition_to_committing("/media/test.mkv", "tok-1",
                                     temp_path="/tmp/out.mkv", target_path="/media/test.mkv",
                                     expected_size=500, expected_hash="def456")
        assert db.fail_attempt("tok-1", "commit failed") is True
        record = db.get("/media/test.mkv")
        assert record["status"] == "failed"
        assert record["commit_temp_path"] is None


def test_skip_with_token(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        assert db.skip_with_token("/media/test.mkv", "tok-1") is True
        record = db.get("/media/test.mkv")
        assert record["status"] == "skipped"


def test_transition_to_committing(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        assert db.transition_to_committing(
            "/media/test.mkv", "tok-1",
            temp_path="/tmp/out.mkv", target_path="/media/test.mkv",
            expected_size=500, expected_hash="hash123") is True
        record = db.get("/media/test.mkv")
        assert record["status"] == "committing"
        assert record["commit_temp_path"] == "/tmp/out.mkv"
        assert record["expected_output_hash"] == "hash123"


def test_complete_commit(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        db.transition_to_committing("/media/test.mkv", "tok-1",
                                     temp_path="/tmp/o.mkv", target_path="/media/test.mkv",
                                     expected_size=500, expected_hash="h1")
        assert db.complete_commit("tok-1", final_path="/media/test.mkv",
                                  output_codec="hevc", output_size=500,
                                  output_hash="h1") is True
        record = db.get("/media/test.mkv")
        assert record["status"] == "completed"
        assert record["dispatch_token"] is None
        assert record["commit_temp_path"] is None


def test_complete_commit_requires_committing(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        assert db.complete_commit("tok-1", final_path="/media/test.mkv",
                                  output_codec="hevc", output_size=500,
                                  output_hash="h1") is False


def test_capacity_counts_include_committing(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/media/a.mkv", "Movies", "movie", "a", 1000, "h264")
        db.upsert("/media/b.mkv", "Movies", "movie", "b", 2000, "h264")
        db.claim_with_token("/media/a.mkv", "tok-1", needs_gpu=True)
        db.claim_with_token("/media/b.mkv", "tok-2")
        db.transition_to_committing("/media/b.mkv", "tok-2",
                                     temp_path="/tmp/b.mkv", target_path="/media/b.mkv",
                                     expected_size=1500, expected_hash="h2")
        assert db.count_active() == 2
        assert db.gpu_active_count() == 1
        assert db.library_active_counts() == {"Movies": 2}


def test_get_pending_batch_excludes_libraries(tmp_path):
    with FileDB(str(tmp_path / "test.db")) as db:
        db.upsert("/lib_a/f.mkv", "Library A", "test", "a", 1000, "h264")
        db.upsert("/lib_b/f.mkv", "Library B", "test", "b", 2000, "h264")
        batch = db.get_pending_batch(exclude_libraries={"Library A"})
        assert len(batch) == 1
        assert batch[0]["library"] == "Library B"


def test_reconcile_committing_target_exists(tmp_path):
    db_path = str(tmp_path / "test.db")
    target = tmp_path / "output.mkv"
    target.write_bytes(b"x" * 500)
    with FileDB(db_path) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        expected_hash = compute_file_hash(str(target))
        db.transition_to_committing("/media/test.mkv", "tok-1",
                                     temp_path="/tmp/temp.mkv",
                                     target_path=str(target),
                                     expected_size=500,
                                     expected_hash=expected_hash)
        count = db.reconcile_committing()
        assert count == 1
        record = db.get(str(target))
        assert record is not None
        assert record["status"] == "completed"


def test_reconcile_committing_target_missing(tmp_path):
    db_path = str(tmp_path / "test.db")
    with FileDB(db_path) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        db.transition_to_committing("/media/test.mkv", "tok-1",
                                     temp_path="/tmp/missing.mkv",
                                     target_path="/media/output.mkv",
                                     expected_size=500,
                                     expected_hash="nope")
        count = db.reconcile_committing()
        assert count == 1
        record = db.get("/media/test.mkv")
        assert record["status"] == "failed"
        assert record["dispatch_token"] is None


def test_reconcile_committing_stale_only(tmp_path):
    from datetime import timedelta
    db_path = str(tmp_path / "test.db")
    with FileDB(db_path) as db:
        db.upsert("/media/test.mkv", "Movies", "movie", "abc", 1000, "h264")
        db.claim_with_token("/media/test.mkv", "tok-1")
        db.transition_to_committing("/media/test.mkv", "tok-1",
                                     temp_path="/tmp/t.mkv", target_path="/media/o.mkv",
                                     expected_size=500, expected_hash="h")
        from datetime import datetime, timezone
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        count = db.reconcile_committing(older_than=future)
        assert count == 1
